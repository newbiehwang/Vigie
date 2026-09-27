"""홈 대시보드 수집기: AWS에서 운영 현황을 구역(section)마다 모은다. 모은 값은 dashboard_store에 저장하고,
홈(GET /dashboard, services/llm)은 저장된 값을 읽기만 한다 (홈을 열 때마다 AWS를 부르지 않는다).

구역과 모으는 때 (값이 얼마나 빨리 바뀌고, 부르는 데 돈이 드는지에 맞췄다)
    구역        내용                                              언제                                  비용
    alarms      울리는 알람, 전체 알람 수                          알람 상태 변경 이벤트 + 5분마다        무료 (Describe)
    resources   Lambda·EC2(상태 검사)·S3(퍼블릭 차단)·영구 보관 로그  EC2 상태·설정 변경 이벤트 + 5분마다     무료 (Describe·List·Get)
    errors      계정 전체 Lambda 오류, 1시간 단위 48시간             5분마다                               메트릭 1개
    changes     CloudTrail 쓰기 이벤트 24시간                        CloudTrail 변경 이벤트 + 5분마다        무료 (LookupEvents)
    usage       함수별 오류·호출 24시간, EC2 CPU 평균 14일            1시간마다                             함수·인스턴스 수만큼 메트릭
    cost        이번 달 일별·서비스별 비용, 지난달 이맘때, 월말 예상    하루 1번                              Cost Explorer 2번 ($0.02)
- 비용은 AWS가 하루 몇 번만 갱신하므로 더 자주 불러도 새 값이 없다 (Cost Explorer는 부를 때마다 $0.01).
- 메트릭(GetMetricData)은 요청한 메트릭 수만큼 요금이 붙으므로, 함수별·인스턴스별 값은 1시간마다만 모은다.
- 범위는 이 리전 전체다 (MCP 조회 도구와 같다). 많은 계정에서도 한 번에 끝나도록 구역마다 개수에 상한을 둔다.

각 collect_* 함수는 boto3 클라이언트를 받아 JSON으로 바꿀 수 있는 dict를 돌려준다 (저장 모양은 함수 설명에).
시각은 모두 epoch 초(UTC)다. 화면 모양(types/dashboard.ts)으로 합치는 일은 읽는 쪽(services/llm)이 한다.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

MAX_LAMBDAS = 200
MAX_INSTANCES = 100
MAX_BUCKETS = 100
MAX_LOG_GROUPS = 50  # 영구 보관 로그 그룹을 이만큼까지 적는다 (개수는 모두 센다)
MAX_CHANGES = 30
METRIC_BATCH = 500  # GetMetricData 한 번에 넣을 수 있는 질의 수

# 알람 비교 연산자 → 기호 (화면의 "5XXError > 5 (5분)")
OPERATORS = {
    "GreaterThanThreshold": ">",
    "GreaterThanOrEqualToThreshold": "≥",
    "LessThanThreshold": "<",
    "LessThanOrEqualToThreshold": "≤",
    "LessThanLowerOrGreaterThanUpperThreshold": "범위 밖",
    "LessThanLowerThreshold": "< 하한",
    "GreaterThanUpperThreshold": "> 상한",
}

PUBLIC_ACCESS_KEYS = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")

# 최근 변경에서 빼는 이벤트: 읽기 전용이 아니지만 운영자가 '바꿨다'고 보지 않는 것 (로그인·역할 전환·로그 쓰기·쿼리 등)
NOISE_EVENTS = {
    "AssumeRole", "AssumeRoleWithWebIdentity", "AssumeRoleWithSAML", "GetSessionToken", "ConsoleLogin",
    "CreateLogStream", "PutLogEvents", "StartQuery", "StopQuery", "Decrypt", "GenerateDataKey",
    "StartQueryExecution", "StopQueryExecution", "RenewRole", "InitiateAuth", "RespondToAuthChallenge",
}

# Cost Explorer의 서비스 이름 → 화면에 보일 짧은 이름
SERVICE_NAMES = {
    "AWS Lambda": "Lambda",
    "Amazon Elastic Compute Cloud - Compute": "EC2",
    "EC2 - Other": "EC2 기타",
    "AmazonCloudWatch": "CloudWatch",
    "Amazon Simple Storage Service": "S3",
    "Amazon DynamoDB": "DynamoDB",
    "Amazon API Gateway": "API Gateway",
    "Amazon Elastic Container Registry Public": "ECR Public",
    "Amazon EC2 Container Registry (ECR)": "ECR",
    "Amazon Cognito": "Cognito",
    "AWS Amplify": "Amplify",
    "Amazon Bedrock": "Bedrock",
    "AWS Key Management Service": "KMS",
    "AWS CloudTrail": "CloudTrail",
    "AWS Systems Manager": "Systems Manager",
    "Amazon Virtual Private Cloud": "VPC",
    "Tax": "세금",
}
TOP_SERVICES = 6  # 서비스별 비용은 큰 것 여섯 개 + 기타


def _epoch(value: Any) -> Optional[int]:
    if isinstance(value, datetime):
        return int(value.timestamp())
    return None


def _paginate(client, operation: str, key: str, limit: int, **kwargs) -> List[Dict[str, Any]]:
    """페이지를 넘기며 모은다 (limit개가 넘으면 멈춘다)."""
    items: List[Dict[str, Any]] = []
    for page in client.get_paginator(operation).paginate(**kwargs):
        items.extend(page.get(key, []))
        if len(items) >= limit:
            break
    return items[:limit]


# ---------------------------------------------------------------- alarms
def collect_alarms(cloudwatch) -> Dict[str, Any]:
    """울리는 알람과 전체 알람 수.
    {"total": 24, "firing": [{"name", "metric": "5XXError > 5 (5분)", "since": 울리기 시작한 때}]}"""
    total = 0
    firing = []
    for page in cloudwatch.get_paginator("describe_alarms").paginate(AlarmTypes=["MetricAlarm", "CompositeAlarm"]):
        for kind in ("MetricAlarms", "CompositeAlarms"):
            for alarm in page.get(kind, []):
                total += 1
                if alarm.get("StateValue") != "ALARM":
                    continue
                firing.append({
                    "name": alarm["AlarmName"],
                    "metric": _alarm_metric_text(alarm),
                    "since": _epoch(alarm.get("StateUpdatedTimestamp")),
                })
    firing.sort(key=lambda a: a["since"] or 0)  # 오래 울린 것부터
    return {"total": total, "firing": firing}


def _alarm_metric_text(alarm: Dict[str, Any]) -> str:
    if not alarm.get("MetricName"):  # 복합 알람이나 메트릭 수식 알람
        return alarm.get("AlarmDescription") or "복합 알람"
    op = OPERATORS.get(alarm.get("ComparisonOperator", ""), "")
    threshold = alarm.get("Threshold")
    threshold_text = f"{threshold:g}" if isinstance(threshold, (int, float)) else ""
    period = alarm.get("Period")
    period_text = f" ({period // 60}분)" if isinstance(period, int) and period >= 60 else ""
    return f"{alarm['MetricName']} {op} {threshold_text}{period_text}".strip()


# ---------------------------------------------------------------- resources
def collect_resources(lambda_client, ec2, s3, logs) -> Dict[str, Any]:
    """리소스의 지금 설정과 상태 (메트릭 없이, 무료 조회만).
    {"lambdas": [{"name", "runtime"}],
     "ec2": [{"id", "name", "state", "checks": "ok"|"impaired"|"initializing"|None}],
     "s3": [{"name", "blockOff": [꺼진 퍼블릭 차단 항목]}],
     "logs": {"neverExpire": 영구 보관 그룹 수, "groups": [{"name", "storedBytes"}]}}"""
    lambdas = [{"name": f["FunctionName"], "runtime": f.get("Runtime")}
               for f in _paginate(lambda_client, "list_functions", "Functions", MAX_LAMBDAS)]

    reservations = _paginate(ec2, "describe_instances", "Reservations", MAX_INSTANCES,
                             Filters=[{"Name": "instance-state-name",
                                       "Values": ["pending", "running", "stopping", "stopped"]}])
    instances = [i for r in reservations for i in r.get("Instances", [])][:MAX_INSTANCES]
    checks: Dict[str, str] = {}
    running = [i["InstanceId"] for i in instances if i.get("State", {}).get("Name") == "running"]
    if running:
        for status in ec2.describe_instance_status(InstanceIds=running).get("InstanceStatuses", []):
            parts = (status.get("InstanceStatus", {}).get("Status"), status.get("SystemStatus", {}).get("Status"))
            checks[status["InstanceId"]] = ("impaired" if "impaired" in parts
                                            else "initializing" if "initializing" in parts else "ok")
    ec2_items = [{
        "id": i["InstanceId"],
        "name": next((t["Value"] for t in i.get("Tags", []) if t.get("Key") == "Name"), None),
        "state": i.get("State", {}).get("Name"),
        "checks": checks.get(i["InstanceId"]),
    } for i in instances]

    buckets = s3.list_buckets().get("Buckets", [])[:MAX_BUCKETS]
    s3_items = []
    for bucket in buckets:
        name = bucket["Name"]
        try:
            config = s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
            off = [key for key in PUBLIC_ACCESS_KEYS if not config.get(key)]
        except s3.exceptions.ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code != "NoSuchPublicAccessBlockConfiguration":
                continue  # 다른 리전이라 읽을 수 없거나 권한이 없으면 건너뛴다 (이 버킷은 판정하지 않는다)
            off = list(PUBLIC_ACCESS_KEYS)  # 설정이 없으면 네 항목이 모두 꺼진 것
        s3_items.append({"name": name, "blockOff": off})

    never = 0
    groups = []
    for page in logs.get_paginator("describe_log_groups").paginate():
        for group in page.get("logGroups", []):
            if group.get("retentionInDays") is None:
                never += 1
                groups.append({"name": group["logGroupName"], "storedBytes": group.get("storedBytes", 0)})
    groups.sort(key=lambda g: g["storedBytes"], reverse=True)  # 많이 쌓인 것부터

    return {"lambdas": lambdas, "ec2": ec2_items, "s3": s3_items,
            "logs": {"neverExpire": never, "groups": groups[:MAX_LOG_GROUPS]}}


# ---------------------------------------------------------------- errors (5분마다) · usage (1시간마다)
def _hour_floor(now: datetime) -> datetime:
    return now.replace(minute=0, second=0, microsecond=0)


def collect_errors(cloudwatch, now: Optional[datetime] = None) -> Dict[str, Any]:
    """계정 전체 Lambda 오류, 1시간 단위 48칸 (오래된 것부터, 마지막 칸은 지금 진행 중인 시간).
    {"hourly": [48개], "end": 마지막 칸이 끝나는 때}. 화면은 뒤 24칸을 지난 24시간, 앞 24칸을 그 전 24시간으로 본다.
    Lambda는 함수 이름 없이 계정 전체 합계 메트릭(AWS/Lambda Errors)을 따로 낸다: 메트릭 1개로 끝난다."""
    now = now or datetime.now(timezone.utc)
    end = _hour_floor(now) + timedelta(hours=1)
    start = end - timedelta(hours=48)
    result = cloudwatch.get_metric_data(
        MetricDataQueries=[{"Id": "errors", "ReturnData": True, "MetricStat": {
            "Metric": {"Namespace": "AWS/Lambda", "MetricName": "Errors"}, "Period": 3600, "Stat": "Sum"}}],
        StartTime=start, EndTime=end)
    hourly = [0] * 48
    for series in result.get("MetricDataResults", []):
        for stamp, value in zip(series.get("Timestamps", []), series.get("Values", [])):
            index = int((stamp - start).total_seconds() // 3600)
            if 0 <= index < 48:
                hourly[index] += int(round(value))
    return {"hourly": hourly, "end": int(end.timestamp())}


def collect_usage(cloudwatch, functions: List[str], instances: List[str],
                  now: Optional[datetime] = None) -> Dict[str, Any]:
    """함수별 오류·호출 수(24시간)와 EC2 CPU 평균(14일). resources 구역에서 읽은 이름으로 묻는다.
    {"functions": {이름: {"errors", "invocations"}}, "ec2Cpu": {인스턴스 ID: 평균 %}}"""
    now = now or datetime.now(timezone.utc)
    queries = []
    names: Dict[str, tuple] = {}
    for n, name in enumerate(functions):
        for metric in ("Errors", "Invocations"):
            query_id = f"f{n}_{metric.lower()}"
            names[query_id] = ("function", name, metric)
            queries.append({"Id": query_id, "ReturnData": True, "MetricStat": {
                "Metric": {"Namespace": "AWS/Lambda", "MetricName": metric,
                           "Dimensions": [{"Name": "FunctionName", "Value": name}]},
                "Period": 86400, "Stat": "Sum"}})
    function_values: Dict[str, Dict[str, int]] = {name: {"errors": 0, "invocations": 0} for name in functions}

    def keep_function(query_id: str, values: List[float]) -> None:
        _, name, metric = names[query_id]
        function_values[name][metric.lower()] = int(round(sum(values)))

    _run_metric_queries(cloudwatch, queries, now - timedelta(hours=24), now, keep_function)

    cpu_queries = []
    for n, instance_id in enumerate(instances):
        query_id = f"i{n}"
        names[query_id] = ("instance", instance_id, "CPUUtilization")
        cpu_queries.append({"Id": query_id, "ReturnData": True, "MetricStat": {
            "Metric": {"Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
                       "Dimensions": [{"Name": "InstanceId", "Value": instance_id}]},
            "Period": 86400, "Stat": "Average"}})
    cpu: Dict[str, float] = {}

    def keep_cpu(query_id: str, values: List[float]) -> None:
        if values:  # 하루 평균들의 평균 (지표가 없는 날은 빠진다)
            cpu[names[query_id][1]] = round(sum(values) / len(values), 1)

    _run_metric_queries(cloudwatch, cpu_queries, now - timedelta(days=14), now, keep_cpu)
    return {"functions": function_values, "ec2Cpu": cpu}


def _run_metric_queries(cloudwatch, queries: List[Dict[str, Any]], start: datetime, end: datetime,
                        keep: Callable[[str, List[float]], None]) -> None:
    for offset in range(0, len(queries), METRIC_BATCH):
        kwargs: Dict[str, Any] = {"MetricDataQueries": queries[offset:offset + METRIC_BATCH],
                                  "StartTime": start, "EndTime": end}
        while True:
            result = cloudwatch.get_metric_data(**kwargs)
            for series in result.get("MetricDataResults", []):
                keep(series["Id"], list(series.get("Values", [])))
            if not result.get("NextToken"):
                break
            kwargs["NextToken"] = result["NextToken"]


# ---------------------------------------------------------------- changes
def collect_changes(cloudtrail, now: Optional[datetime] = None) -> Dict[str, Any]:
    """CloudTrail의 최근 24시간 쓰기 이벤트 (관리 이벤트, 최근 것부터 MAX_CHANGES개).
    {"events": [{"at", "actor", "eventName", "eventSource", "resource"}]}
    LookupEvents는 무료이고 트레일이 없어도 최근 90일을 읽을 수 있다 (초당 2번 제한이 있다)."""
    now = now or datetime.now(timezone.utc)
    events = []
    paginator = cloudtrail.get_paginator("lookup_events")
    for page in paginator.paginate(LookupAttributes=[{"AttributeKey": "ReadOnly", "AttributeValue": "false"}],
                                   StartTime=now - timedelta(hours=24), EndTime=now,
                                   PaginationConfig={"MaxItems": MAX_CHANGES * 4, "PageSize": 50}):
        for event in page.get("Events", []):
            name = event.get("EventName", "")
            if name in NOISE_EVENTS:
                continue
            resources = event.get("Resources") or []
            events.append({
                "at": _epoch(event.get("EventTime")),
                "actor": event.get("Username") or "알 수 없음",
                "eventName": name,
                "eventSource": (event.get("EventSource") or "").replace(".amazonaws.com", ""),
                "resource": resources[0].get("ResourceName") if resources else None,
            })
            if len(events) >= MAX_CHANGES:
                break
        if len(events) >= MAX_CHANGES:
            break
    events.sort(key=lambda e: e["at"] or 0, reverse=True)
    return {"events": events}


# ---------------------------------------------------------------- cost (하루 1번)
def collect_cost(ce, today: Optional[date] = None) -> Dict[str, Any]:
    """이번 달 일별·서비스별 비용, 지난달 같은 날짜까지의 합계, 월말 예상 (USD, 비용은 하루 늦게 확정된다).
    Cost Explorer를 2번 부른다: 지난달 1일부터 오늘 전날까지의 일별·서비스별 비용 1번 + 이번 달 남은 날 예상 1번.
    {"monthToDate", "lastMonthSamePeriod", "forecast", "daily": [{"date", "amount"}], "byService": [{"service", "amount"}]}"""
    today = today or datetime.now(timezone.utc).date()
    month_start = today.replace(day=1)
    last_month_start = (month_start - timedelta(days=1)).replace(day=1)
    days_before = (today - month_start).days  # 이번 달에서 확정된 날 수 (오늘은 아직 확정 전)

    daily: Dict[str, float] = {}
    by_service: Dict[str, float] = {}
    last_month_same = 0.0
    kwargs: Dict[str, Any] = {
        "TimePeriod": {"Start": last_month_start.isoformat(), "End": today.isoformat()},
        "Granularity": "DAILY", "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
    }
    if today > last_month_start:
        while True:
            result = ce.get_cost_and_usage(**kwargs)
            for period in result.get("ResultsByTime", []):
                day = date.fromisoformat(period["TimePeriod"]["Start"])
                for group in period.get("Groups", []):
                    amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                    if day >= month_start:
                        daily[day.isoformat()] = daily.get(day.isoformat(), 0.0) + amount
                        service = SERVICE_NAMES.get(group["Keys"][0], group["Keys"][0])
                        by_service[service] = by_service.get(service, 0.0) + amount
                    elif day.day <= days_before:  # 지난달의 같은 날짜까지 (이번 달 확정된 날 수만큼)
                        last_month_same += amount
            if not result.get("NextPageToken"):
                break
            kwargs["NextPageToken"] = result["NextPageToken"]

    month_to_date = sum(daily.values())
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    forecast = month_to_date
    try:
        remaining = ce.get_cost_forecast(TimePeriod={"Start": today.isoformat(), "End": next_month.isoformat()},
                                         Metric="UNBLENDED_COST", Granularity="MONTHLY")
        forecast += float(remaining["Total"]["Amount"])
    except Exception as error:  # 쓴 기록이 너무 적으면 예측하지 못한다. 확정된 금액을 그대로 둔다
        print(f"비용 예측을 받지 못함 (확정 금액만 둔다): {error}")

    services = sorted(by_service.items(), key=lambda item: item[1], reverse=True)
    top = [{"service": name, "amount": round(amount, 2)} for name, amount in services[:TOP_SERVICES]]
    rest = sum(amount for _, amount in services[TOP_SERVICES:])
    if rest > 0:
        top.append({"service": "기타", "amount": round(rest, 2)})
    return {
        "monthToDate": round(month_to_date, 2),
        "lastMonthSamePeriod": round(last_month_same, 2),
        "forecast": round(forecast, 2),
        "daily": [{"date": (month_start + timedelta(days=n)).isoformat(),
                   "amount": round(daily.get((month_start + timedelta(days=n)).isoformat(), 0.0), 2)}
                  for n in range(days_before)],
        "byService": top,
    }
