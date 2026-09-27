"""홈 대시보드 수집기 (services/dashboard). 실제 AWS는 부르지 않는다:
CloudWatch·EC2·S3·Logs·Lambda·DynamoDB는 moto로, Cost Explorer와 CloudTrail LookupEvents는 작은 가짜 클라이언트로 흉내 낸다
(moto가 두 API의 조회 결과를 그럴듯하게 만들지 못한다)."""
import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone

import boto3
import pytest

from conftest import load_service_module

TABLE = "wga-dashboard-test"


@pytest.fixture
def dash(aws, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TABLE", TABLE)
    boto3.client("dynamodb").create_table(
        TableName=TABLE, KeySchema=[{"AttributeName": "section", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "section", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
    handler = load_service_module("services/dashboard", "lambda_function")
    handler._clients.clear()
    import collector
    from common.dashboard_store import DashboardStore
    return {"handler": handler, "collector": collector,
            "store": DashboardStore(boto3.resource("dynamodb").Table(TABLE))}


# ---------------------------------------------------------------- 알람

def test_alarms_counts_all_and_lists_firing_with_metric_text(dash):
    cw = boto3.client("cloudwatch")
    for name in ("wga-test-api-5xx", "wga-test-llm-errors"):
        cw.put_metric_alarm(AlarmName=name, MetricName="5XXError", Namespace="AWS/ApiGateway", Statistic="Sum",
                            Period=300, EvaluationPeriods=1, Threshold=5,
                            ComparisonOperator="GreaterThanThreshold")
    cw.set_alarm_state(AlarmName="wga-test-api-5xx", StateValue="ALARM", StateReason="test")

    data = dash["collector"].collect_alarms(cw)

    assert data["total"] == 2
    assert [a["name"] for a in data["firing"]] == ["wga-test-api-5xx"]
    assert data["firing"][0]["metric"] == "5XXError > 5 (5분)" and data["firing"][0]["since"]


# ---------------------------------------------------------------- 리소스

def _zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("index.py", "def handler(event, context):\n    return 1\n")
    return buffer.getvalue()


def test_resources_reads_state_without_metrics(dash):
    role = boto3.client("iam").create_role(RoleName="r", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    boto3.client("lambda").create_function(FunctionName="wga-llm-test", Runtime="python3.12", Role=role,
                                           Handler="index.handler", Code={"ZipFile": _zip()})
    ec2 = boto3.client("ec2")
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    web = ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1, InstanceType="t3.micro", TagSpecifications=[
        {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "demo-web"}]}])["Instances"][0]["InstanceId"]
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket="locked")
    s3.put_public_access_block(Bucket="locked", PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.create_bucket(Bucket="open")  # 차단 설정이 없다 → 네 항목 모두 꺼짐
    logs = boto3.client("logs")
    logs.create_log_group(logGroupName="/aws/lambda/wga-llm-test")  # 영구 보관
    logs.create_log_group(logGroupName="/aws/lambda/wga-mcp-test")
    logs.put_retention_policy(logGroupName="/aws/lambda/wga-mcp-test", retentionInDays=14)

    data = dash["collector"].collect_resources(boto3.client("lambda"), ec2, s3, logs)

    assert data["lambdas"] == [{"name": "wga-llm-test", "runtime": "python3.12"}]
    assert data["ec2"][0]["id"] == web and data["ec2"][0]["name"] == "demo-web"
    assert data["ec2"][0]["state"] in ("pending", "running")
    assert {b["name"]: len(b["blockOff"]) for b in data["s3"]} == {"locked": 0, "open": 4}
    assert data["logs"]["neverExpire"] == 1
    assert [g["name"] for g in data["logs"]["groups"]] == ["/aws/lambda/wga-llm-test"]


# ---------------------------------------------------------------- 오류 · 사용량 (메트릭)

def test_errors_are_hourly_for_48_hours_from_one_account_metric(dash):
    cw = boto3.client("cloudwatch")
    now = datetime.now(timezone.utc).replace(minute=30, second=0, microsecond=0)
    cw.put_metric_data(Namespace="AWS/Lambda", MetricData=[
        {"MetricName": "Errors", "Timestamp": now - timedelta(minutes=10), "Value": 3},       # 지금 시간 칸
        {"MetricName": "Errors", "Timestamp": now - timedelta(hours=30), "Value": 2},         # 그 전 24시간
    ])

    data = dash["collector"].collect_errors(cw, now=now)

    assert len(data["hourly"]) == 48
    assert data["hourly"][-1] == 3 and sum(data["hourly"][:24]) == 2
    assert data["end"] == int((now.replace(minute=0) + timedelta(hours=1)).timestamp())


def test_usage_reads_function_errors_and_instance_cpu(dash):
    cw = boto3.client("cloudwatch")
    now = datetime.now(timezone.utc)
    dims = [{"Name": "FunctionName", "Value": "wga-llm-test"}]
    cw.put_metric_data(Namespace="AWS/Lambda", MetricData=[
        {"MetricName": "Errors", "Dimensions": dims, "Timestamp": now - timedelta(hours=2), "Value": 4},
        {"MetricName": "Invocations", "Dimensions": dims, "Timestamp": now - timedelta(hours=2), "Value": 100},
    ])
    cw.put_metric_data(Namespace="AWS/EC2", MetricData=[
        {"MetricName": "CPUUtilization", "Dimensions": [{"Name": "InstanceId", "Value": "i-1"}],
         "Timestamp": now - timedelta(days=1), "Value": 1.5}])

    data = dash["collector"].collect_usage(cw, ["wga-llm-test", "wga-idle-test"], ["i-1", "i-2"], now=now)

    assert data["functions"]["wga-llm-test"] == {"errors": 4, "invocations": 100}
    assert data["functions"]["wga-idle-test"] == {"errors": 0, "invocations": 0}  # 호출이 없으면 0
    assert data["ec2Cpu"] == {"i-1": 1.5}  # 지표가 없는 인스턴스는 빠진다


# ---------------------------------------------------------------- 최근 변경 (가짜 CloudTrail)

class FakeCloudTrail:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def get_paginator(self, name):
        assert name == "lookup_events"
        outer = self

        class Paginator:
            def paginate(self, **kwargs):
                outer.calls.append(kwargs)
                return [{"Events": outer.events}]
        return Paginator()


def test_changes_keep_write_events_and_drop_noise(dash):
    now = datetime.now(timezone.utc)
    trail = FakeCloudTrail([
        {"EventName": "PutRetentionPolicy", "EventTime": now - timedelta(minutes=5), "Username": "kim",
         "EventSource": "logs.amazonaws.com", "Resources": [{"ResourceName": "/aws/lambda/wga-llm-test"}]},
        {"EventName": "AssumeRole", "EventTime": now - timedelta(minutes=4), "Username": "x",
         "EventSource": "sts.amazonaws.com"},
        {"EventName": "StopInstances", "EventTime": now - timedelta(minutes=1), "Username": "park",
         "EventSource": "ec2.amazonaws.com", "Resources": []},
    ])

    data = dash["collector"].collect_changes(trail, now=now)

    assert [e["eventName"] for e in data["events"]] == ["StopInstances", "PutRetentionPolicy"]  # 최근 것부터
    assert data["events"][1] == {"at": int((now - timedelta(minutes=5)).timestamp()), "actor": "kim",
                                 "eventName": "PutRetentionPolicy", "eventSource": "logs",
                                 "resource": "/aws/lambda/wga-llm-test"}
    assert trail.calls[0]["LookupAttributes"] == [{"AttributeKey": "ReadOnly", "AttributeValue": "false"}]


# ---------------------------------------------------------------- 비용 (가짜 Cost Explorer)

class FakeCostExplorer:
    def __init__(self, days, forecast=None):
        self.days = days  # {date: {service: amount}}
        self.forecast = forecast
        self.calls = []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(("usage", kwargs))
        return {"ResultsByTime": [
            {"TimePeriod": {"Start": day.isoformat()},
             "Groups": [{"Keys": [service], "Metrics": {"UnblendedCost": {"Amount": str(amount)}}}
                        for service, amount in services.items()]}
            for day, services in sorted(self.days.items())]}

    def get_cost_forecast(self, **kwargs):
        self.calls.append(("forecast", kwargs))
        if self.forecast is None:
            raise RuntimeError("DataUnavailableException")
        return {"Total": {"Amount": str(self.forecast)}}


def test_cost_splits_this_month_last_month_and_services(dash):
    today = date(2026, 9, 4)  # 확정된 날: 9월 1~3일
    days = {date(2026, 8, d): {"AWS Lambda": 1.0} for d in range(1, 32)}
    days.update({date(2026, 9, d): {"AWS Lambda": 2.0, "Amazon Elastic Compute Cloud - Compute": 1.5, "Tax": 0.1}
                 for d in (1, 2, 3)})
    ce = FakeCostExplorer(days, forecast=40)

    data = dash["collector"].collect_cost(ce, today=today)

    assert data["monthToDate"] == 10.8
    assert data["lastMonthSamePeriod"] == 3.0  # 8월 1~3일만
    assert data["forecast"] == 50.8  # 확정 + 남은 날 예상
    assert [d["date"] for d in data["daily"]] == ["2026-09-01", "2026-09-02", "2026-09-03"]
    assert data["byService"][0] == {"service": "Lambda", "amount": 6.0}
    assert {s["service"] for s in data["byService"]} == {"Lambda", "EC2", "세금"}
    usage = ce.calls[0][1]
    assert usage["TimePeriod"] == {"Start": "2026-08-01", "End": "2026-09-04"} and usage["Granularity"] == "DAILY"
    assert len(ce.calls) == 2  # Cost Explorer는 2번만 (부를 때마다 요금)


def test_cost_keeps_confirmed_amount_when_forecast_fails_and_groups_small_services(dash):
    services = {f"Service {n}": float(10 - n) for n in range(8)}
    ce = FakeCostExplorer({date(2026, 9, 1): services}, forecast=None)

    data = dash["collector"].collect_cost(ce, today=date(2026, 9, 2))

    assert data["forecast"] == data["monthToDate"] == 52.0
    assert len(data["byService"]) == 7 and data["byService"][-1] == {"service": "기타", "amount": 7.0}  # 4 + 3


# ---------------------------------------------------------------- 저장

def test_store_keeps_last_success_when_a_section_fails(dash):
    store = dash["store"]
    store.put_section("alarms", {"total": 3, "firing": []}, now=100)
    store.put_error("alarms", "AccessDenied", now=200)
    store.put_error("cost", "Throttling", now=200)  # 한 번도 성공하지 못한 구역

    sections = store.get_all()

    assert sections["alarms"] == {"data": {"total": 3, "firing": []}, "ok": False, "error": "AccessDenied",
                                  "collectedAt": 200, "lastSuccessAt": 100}
    assert sections["cost"]["data"] is None and sections["cost"]["lastSuccessAt"] is None
    assert store.collected_at("alarms") == 200 and store.collected_at("errors") is None


# ---------------------------------------------------------------- 핸들러

def test_events_map_to_sections(dash):
    sections_for = dash["handler"].sections_for
    assert sections_for({"sections": ["cost", "bogus"]}) == ["cost"]
    assert sections_for({"source": "aws.cloudwatch", "detail-type": "CloudWatch Alarm State Change"}) == ["alarms"]
    assert sections_for({"source": "aws.ec2", "detail-type": "EC2 Instance State-change Notification"}) == [
        "resources"]
    assert sections_for({"source": "aws.logs", "detail-type": "AWS API Call via CloudTrail",
                         "detail": {"eventSource": "logs.amazonaws.com"}}) == ["changes", "resources"]
    assert "alarms" in sections_for({"source": "aws.monitoring", "detail-type": "AWS API Call via CloudTrail",
                                     "detail": {"eventSource": "monitoring.amazonaws.com"}})
    assert sections_for({"source": "aws.health"}) == []


def test_one_failing_section_does_not_stop_the_others(dash, monkeypatch):
    handler = dash["handler"]
    monkeypatch.setattr(handler.collector, "collect_alarms", lambda cw: {"total": 1, "firing": []})

    def broken(cw, now=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(handler.collector, "collect_errors", broken)

    result = handler.lambda_handler({"sections": ["alarms", "errors"]}, None)

    assert result == {"sections": {"alarms": "ok", "errors": "error"}}
    saved = dash["store"].get_all()
    assert saved["alarms"]["ok"] and saved["errors"]["error"] == "RuntimeError: boom"


def test_bursts_of_events_are_collected_once(dash, monkeypatch):
    handler = dash["handler"]
    calls = []
    monkeypatch.setattr(handler.collector, "collect_alarms", lambda cw: calls.append(1) or {"total": 0, "firing": []})
    event = {"source": "aws.cloudwatch", "detail-type": "CloudWatch Alarm State Change"}

    first = handler.collect(["alarms"], dash["store"], from_event=True, now=lambda: 1000)
    again = handler.collect(["alarms"], dash["store"], from_event=True, now=lambda: 1010)  # 10초 뒤: 건너뛴다
    later = handler.collect(["alarms"], dash["store"], from_event=True, now=lambda: 1030)
    scheduled = handler.collect(["alarms"], dash["store"], from_event=False, now=lambda: 1031)  # 정해진 때는 늘 모은다

    assert (first, again, later, scheduled) == ({"alarms": "ok"}, {"alarms": "skipped"}, {"alarms": "ok"},
                                                {"alarms": "ok"})
    assert len(calls) == 3
    assert handler.sections_for(event) == ["alarms"]


def test_usage_uses_saved_resource_names(dash, monkeypatch):
    handler = dash["handler"]
    dash["store"].put_section("resources", {"lambdas": [{"name": "wga-llm-test"}], "ec2": [
        {"id": "i-run", "state": "running"}, {"id": "i-stop", "state": "stopped"}], "s3": [], "logs": {}})
    seen = {}

    def fake_usage(cw, functions, instances, now=None):
        seen.update(functions=functions, instances=instances)
        return {"functions": {}, "ec2Cpu": {}}

    monkeypatch.setattr(handler.collector, "collect_usage", fake_usage)

    assert handler.lambda_handler({"sections": ["usage"]}, None) == {"sections": {"usage": "ok"}}
    assert seen == {"functions": ["wga-llm-test"], "instances": ["i-run"]}  # 멈춘 인스턴스의 CPU는 묻지 않는다
    assert json.loads(json.dumps(dash["store"].get_all()["usage"]["data"])) == {"functions": {}, "ec2Cpu": {}}
