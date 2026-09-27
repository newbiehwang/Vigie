"""GET /dashboard (services/llm/dashboard_view.py, llm_service.handle_dashboard).
모아 둔 구역 + 승인 대기 + 감사 로그의 실행 기록을 화면 모양으로 합치는지 본다. AWS는 부르지 않는다 (DynamoDB는 moto)."""
import json
import time
from datetime import datetime, timezone

import boto3
import pytest

from conftest import load_service_module
from test_approvals import AUDIT_TABLE, PENDING_TABLE, create_tables

DASHBOARD_TABLE = "wga-dashboard-test"
ORIGIN = "https://test.abc.amplifyapp.com"
NOW = 1_790_000_000


@pytest.fixture
def view():
    return load_service_module("services/llm", "dashboard_view")


def section(data, collected=NOW - 60, ok=True, error=None, last=None):
    return {"data": data, "ok": ok, "error": error, "collectedAt": collected,
            "lastSuccessAt": last if last is not None else collected}


RESOURCES = {
    "lambdas": [{"name": "wga-llm-test"}, {"name": "wga-mcp-test"}, {"name": "wga-idle-test"},
                {"name": "wga-new-test"}],
    "ec2": [{"id": "i-web", "name": "web", "state": "running", "checks": "ok"},
            {"id": "i-batch", "name": "batch", "state": "running", "checks": "ok"},
            {"id": "i-bad", "state": "running", "checks": "impaired"},
            {"id": "i-off", "state": "stopped", "checks": None}],
    "s3": [{"name": "open-bucket", "blockOff": ["BlockPublicAcls", "BlockPublicPolicy"]},
           {"name": "locked", "blockOff": []}],
    "logs": {"neverExpire": 3, "groups": [{"name": "/a", "storedBytes": 9}, {"name": "/b", "storedBytes": 5}]},
}
USAGE = {
    "functions": {"wga-llm-test": {"errors": 10, "invocations": 100},   # 10% → 문제
                  "wga-mcp-test": {"errors": 1, "invocations": 1000},   # 오류 있음 → 주의
                  "wga-idle-test": {"errors": 0, "invocations": 0}},    # 호출 없음 → 데이터 없음
    "ec2Cpu": {"i-web": 40.0, "i-batch": 1.2},
}


def test_resources_get_status_from_state_and_usage(view):
    result = view.build_resources({"firing": [{"name": "wga-test-api-5xx", "metric": "5XXError > 5 (5분)"}]},
                                  RESOURCES, USAGE)
    rows = {row["id"]: row for row in result["rows"]}

    assert rows["wga-test-api-5xx"]["status"] == "fail" and rows["wga-test-api-5xx"]["kind"] == "Alarm"
    assert rows["wga-llm-test"]["status"] == "fail" and rows["wga-llm-test"]["detail"].startswith("오류율 10.0%")
    assert rows["wga-mcp-test"]["status"] == "warn" and rows["wga-mcp-test"]["errors24h"] == 1
    assert rows["wga-idle-test"]["status"] == "none"
    assert rows["wga-new-test"]["detail"] == "사용량을 아직 모으지 않았습니다"  # usage를 모으기 전에 생긴 함수
    assert rows["i-batch"]["status"] == "warn" and "놀고 있음" in rows["i-batch"]["detail"]
    assert rows["i-bad"]["status"] == "fail" and rows["i-off"]["status"] == "none"
    assert rows["i-web"]["label"] == "web" and rows["i-web"]["status"] == "ok"
    assert rows["open-bucket"]["detail"] == "퍼블릭 액세스 차단 2개 꺼짐"
    assert result["counts"] == {"fail": 3, "warn": 3, "ok": 2, "none": 3}
    assert [row["status"] for row in result["rows"]][:3] == ["fail"] * 3  # 문제 먼저


def test_resource_rows_are_capped_but_counted_in_full(view):
    many = {"lambdas": [{"name": f"fn-{n}"} for n in range(60)], "ec2": [], "s3": [], "logs": {}}
    usage = {"functions": {f"fn-{n}": {"errors": 0, "invocations": 5} for n in range(60)}}
    usage["functions"]["fn-59"] = {"errors": 5, "invocations": 5}

    result = view.build_resources(None, many, usage)

    assert len(result["rows"]) == view.MAX_RESOURCE_ROWS and result["total"] == 60
    assert result["rows"][0]["id"] == "fn-59"  # 문제는 잘리지 않는다
    assert result["counts"]["ok"] == 59


def test_findings(view):
    findings = {f["kind"]: f for f in view.build_findings(RESOURCES, USAGE)}

    assert findings["public-s3"]["status"] == "fail" and findings["public-s3"]["detail"] == "open-bucket"
    assert findings["idle-ec2"]["title"] == "놀고 있는 EC2 1대" and findings["idle-ec2"]["detail"].startswith("batch")
    assert findings["log-retention"]["title"] == "영구 보관 로그 그룹 3개"
    assert all(f["question"] for f in findings.values())


def test_errors_split_last_and_previous_24_hours(view):
    hourly = [1] * 24 + [2] * 24
    assert view.build_errors({"hourly": hourly}) == {"total24h": 48, "previous24h": 24, "hourly": [2] * 24}
    assert view.build_errors({"hourly": [5]})["hourly"][-1] == 5  # 칸이 모자라면 앞을 0으로 채운다
    assert view.build_errors(None) is None


def test_changes_merge_app_and_cloudtrail_without_duplicates(view):
    trail = {"events": [
        {"at": NOW - 100, "actor": "wga-mcp-role", "eventName": "PutRetentionPolicy", "resource": "/aws/lambda/x",
         "requestId": "req-app"},  # 이 앱에서 실행한 것과 같은 요청 → 뺀다
        {"at": NOW - 50, "actor": "park", "eventName": "StopInstances", "eventSource": "ec2", "resource": None,
         "requestId": "req-2"},
        {"at": NOW - 90_000, "actor": "old", "eventName": "Old", "resource": "r"},  # 24시간 넘음
    ]}
    app = [{"at": NOW - 100, "actor": "kim@example.com", "summary": "보존 기간 30일 → 14일", "requestId": "req-app"}]

    changes = view.build_changes(trail, app, NOW)

    assert changes == [
        {"at": NOW - 50, "source": "cloudtrail", "actor": "park", "summary": "StopInstances (ec2)"},
        {"at": NOW - 100, "source": "app", "actor": "kim@example.com", "summary": "보존 기간 30일 → 14일"},
    ]


def test_view_marks_missing_and_failed_sections(view):
    sections = {
        "alarms": section({"total": 2, "firing": []}),
        "cost": section({"monthToDate": 1.0}, collected=NOW - 30, ok=False, error="Throttling", last=NOW - 7200),
    }
    result = view.build_view(sections, env="test", region="ap-northeast-2", approvals={"pending": 0},
                             app_changes=[], now=NOW)

    assert result["generatedAt"] == NOW - 30
    assert result["errors"] is None and result["alarms"] == {"total": 2, "firing": []}
    assert result["cost"] == {"monthToDate": 1.0}  # 실패해도 지난 성공 값은 보인다
    assert result["sections"]["cost"] == {"ok": False, "error": "Throttling", "collectedAt": NOW - 30,
                                          "lastSuccessAt": NOW - 7200}
    assert "errors" not in result["sections"]  # 한 번도 모으지 않은 구역
    assert result["resources"] == [] and result["resourceCounts"] == {"fail": 0, "warn": 0, "ok": 0, "none": 0}


# ---------------------------------------------------------------- 핸들러 (moto DynamoDB)

@pytest.fixture
def llm(aws, monkeypatch):
    for name, value in {"PENDING_ACTIONS_TABLE": PENDING_TABLE, "AUDIT_TABLE": AUDIT_TABLE,
                        "DASHBOARD_TABLE": DASHBOARD_TABLE}.items():
        monkeypatch.setenv(name, value)
    create_tables()
    boto3.client("dynamodb").create_table(
        TableName=DASHBOARD_TABLE, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "section", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "section", "KeyType": "HASH"}])
    return load_service_module("services/llm", "llm_service")


def put_pending(requester, expires_in):
    boto3.resource("dynamodb").Table(PENDING_TABLE).put_item(Item={
        "actionId": f"{requester}-{expires_in}", "requesterId": requester, "status": "pending",
        "expiresAt": int(time.time()) + expires_in})


def put_executed(minutes_ago, request_id):
    moment = datetime.fromtimestamp(time.time() - minutes_ago * 60, timezone.utc)
    at = moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    boto3.resource("dynamodb").Table(AUDIT_TABLE).put_item(Item={
        "userId": "kim", "email": "kim@example.com", "at": f"{at}#action#a1#executed", "day": at[:10],
        "kind": "action", "event": "executed", "summary": "보존 기간 30일 → 14일", "awsRequestId": request_id})


def test_dashboard_reads_saved_sections_and_live_parts(llm):
    from common.dashboard_store import DashboardStore
    DashboardStore(boto3.resource("dynamodb").Table(DASHBOARD_TABLE)).put_section("alarms", {"total": 1, "firing": []})
    put_pending("alice", 300)
    put_pending("bob", 120)
    put_pending("alice", -10)  # 만료
    put_executed(5, "req-1")

    alice = llm.handle_dashboard({"sub": "alice", "email": "alice@example.com"}, ORIGIN)
    decider = llm.handle_dashboard({"sub": "dave", "cognito:groups": "approvers"}, ORIGIN)

    body = json.loads(alice["body"])
    assert alice["statusCode"] == 200
    assert body["alarms"] == {"total": 1, "firing": []} and body["env"] == "test"
    assert body["approvals"]["pending"] == 1  # 본인 요청만 (만료는 빼고)
    assert json.loads(decider["body"])["approvals"]["pending"] == 2  # 결정자는 모두
    assert [c["source"] for c in body["changes"]] == ["app"] and body["changes"][0]["actor"] == "kim@example.com"


def test_dashboard_requires_login_and_table(llm, monkeypatch):
    assert llm.handle_dashboard({}, ORIGIN)["statusCode"] == 401
    monkeypatch.setattr(llm, "dashboard_store", None)
    assert llm.handle_dashboard({"sub": "alice"}, ORIGIN)["statusCode"] == 503


def test_route(llm):
    handler = load_service_module("services/llm", "lambda_function")
    event = {"path": "/dashboard", "httpMethod": "GET", "headers": {"origin": ORIGIN},
             "requestContext": {"authorizer": {"claims": {"sub": "alice"}}}}
    assert handler.lambda_handler(event, None)["statusCode"] == 200
