"""GET /dashboard: 홈 대시보드. 수집 Lambda(services/dashboard)가 구역마다 모아 둔 값(common.dashboard_store)과
요청할 때 바로 읽는 값(승인 대기·이 앱에서 실행한 변경)을 합쳐 화면 모양(frontend/src/types/dashboard.ts)으로 돌려준다.
AWS는 부르지 않는다 (DynamoDB만 읽는다).

구역을 화면으로
    alarms    → alarms, 문제 리소스(울리는 알람)
    errors    → errors (48칸 중 뒤 24칸이 지난 24시간, 앞 24칸이 그 전 24시간)
    resources → resources의 목록 + usage(함수별 오류·호출, EC2 CPU)로 상태를 정한다
    changes   → changes (CloudTrail) + 감사 로그의 executed(이 앱에서 승인해 실행). 같은 요청 ID면 한 번만
    cost      → cost
    (실시간)   → approvals (볼 수 있는 대기 요청만, approvals.can_view)
- 한 번도 모으지 못한 구역은 null이다. 화면은 그 칸에 '데이터가 존재하지 않습니다.'를 보인다.
- sections에 구역마다 모은 때·성공 여부를 담아, 화면이 '모으지 못함 · 마지막 성공 N분 전'을 보일 수 있게 한다.
- changes(최근 변경)는 관리자에게만 준다 (show_changes). 누가 무엇을 바꿨는지가 CloudTrail과 감사 로그에서 오므로,
  대화의 CloudTrail 조회(lookup_events)·감사 로그와 같이 관리자(admins 그룹)만 본다 (docs/threat-model.md T41).
  일반 사용자에게는 null이고 sections에도 넣지 않는다. 화면은 null이면 카드를 그리지 않는다.
- changes는 최근 MAX_CHANGES개까지만 준다. 카드 높이가 고정이라 한 번에 그만큼만 보인다.
  24시간 안의 전체 건수는 changesTotal (API에만 두고 화면에는 보이지 않는다).
"""
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from boto3.dynamodb.conditions import Key

SECTIONS = ("alarms", "resources", "errors", "changes", "usage", "cost")
MAX_RESOURCE_ROWS = 40  # 표에 보일 줄 수 (문제·주의를 먼저 모두, 남는 자리에 정상)
FAIL_ERROR_RATE = 0.05  # 오류율이 이보다 높으면 문제
IDLE_CPU = 5.0  # 14일 CPU 평균이 이보다 낮으면 유휴 인스턴스 (MCP의 find_ec2_waste와 같은 기준)
MAX_APP_CHANGES = 30
MAX_CHANGES = 10  # 최근 변경 카드에 보일 수 (카드 높이가 이만큼으로 고정, frontend의 MAX_CHANGES와 같게)
STATUS_ORDER = ("fail", "warn", "ok", "none")


def _lambda_row(function: Dict[str, Any], usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    name = function["name"]
    row: Dict[str, Any] = {"id": name, "kind": "Lambda"}
    counts = (usage or {}).get("functions", {}).get(name)
    if counts is None:
        return {**row, "status": "none", "detail": "사용량 데이터 없음"}
    errors, invocations = counts.get("errors", 0), counts.get("invocations", 0)
    row["errors24h"] = errors
    if not invocations:
        return {**row, "status": "none", "detail": "호출 없음"}
    rate = errors / invocations
    if rate >= FAIL_ERROR_RATE:
        return {**row, "status": "fail", "detail": f"오류율 {rate * 100:.1f}% (호출 {invocations:,}번)"}
    if errors:
        return {**row, "status": "warn", "detail": f"오류 {errors}건 (호출 {invocations:,}번)"}
    return {**row, "status": "ok", "detail": f"오류 없음 (호출 {invocations:,}번)"}


def _ec2_row(instance: Dict[str, Any], usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    row: Dict[str, Any] = {"id": instance["id"], "kind": "EC2"}
    if instance.get("name"):
        row["label"] = instance["name"]
    state = instance.get("state")
    cpu = (usage or {}).get("ec2Cpu", {}).get(instance["id"])
    if state != "running":
        return {**row, "status": "none", "detail": f"상태 {state}"}
    if instance.get("checks") == "impaired":
        return {**row, "status": "fail", "detail": "상태 검사 실패"}
    if cpu is not None and cpu < IDLE_CPU:
        return {**row, "status": "warn", "detail": f"최근 14일 CPU 평균 {cpu}% · 유휴"}
    if instance.get("checks") == "initializing":
        return {**row, "status": "ok", "detail": "상태 검사 진행 중"}
    return {**row, "status": "ok", "detail": "상태 검사 통과" + (f" · CPU 평균 {cpu}%" if cpu is not None else "")}


def _s3_row(bucket: Dict[str, Any]) -> Dict[str, Any]:
    off = bucket.get("blockOff") or []
    if off:
        return {"id": bucket["name"], "kind": "S3", "status": "warn",
                "detail": f"퍼블릭 액세스 차단 {len(off)}개 해제"}
    return {"id": bucket["name"], "kind": "S3", "status": "ok", "detail": "퍼블릭 액세스 차단 모두 적용"}


def build_resources(alarms: Optional[Dict[str, Any]], resources: Optional[Dict[str, Any]],
                    usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """리소스 표의 줄과 상태별 개수. 개수는 모든 리소스로 세고, 줄은 문제·주의를 먼저 MAX_RESOURCE_ROWS개까지."""
    rows: List[Dict[str, Any]] = []
    for alarm in (alarms or {}).get("firing", []):
        rows.append({"id": alarm["name"], "kind": "Alarm", "status": "fail",
                     "detail": f"ALARM · {alarm.get('metric')}"})
    for function in (resources or {}).get("lambdas", []):
        rows.append(_lambda_row(function, usage))
    for instance in (resources or {}).get("ec2", []):
        rows.append(_ec2_row(instance, usage))
    for bucket in (resources or {}).get("s3", []):
        rows.append(_s3_row(bucket))
    counts = {status: sum(row["status"] == status for row in rows) for status in STATUS_ORDER}
    rows.sort(key=lambda row: STATUS_ORDER.index(row["status"]))  # 문제 먼저 (같은 상태 안에서는 모은 차례)
    return {"rows": rows[:MAX_RESOURCE_ROWS], "counts": counts, "total": len(rows)}


def build_findings(resources: Optional[Dict[str, Any]], usage: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """개선 권고: 퍼블릭 차단이 꺼진 S3 · 유휴 EC2 · 보존 기간 미설정 로그 그룹 (화면은 보기만 한다)."""
    if not resources:
        return []
    findings = []
    open_buckets = [b["name"] for b in resources.get("s3", []) if b.get("blockOff")]
    if open_buckets:
        findings.append({"kind": "public-s3", "status": "fail",
                         "title": f"퍼블릭 액세스 차단 미적용 S3 버킷 {len(open_buckets)}개",
                         "detail": _names(open_buckets)})
    cpu = (usage or {}).get("ec2Cpu", {})
    idle = [i.get("name") or i["id"] for i in resources.get("ec2", [])
            if i.get("state") == "running" and cpu.get(i["id"]) is not None and cpu[i["id"]] < IDLE_CPU]
    if idle:
        findings.append({"kind": "idle-ec2", "status": "warn", "title": f"유휴 EC2 인스턴스 {len(idle)}대",
                         "detail": f"{_names(idle)} · 최근 14일 CPU 평균 {IDLE_CPU:g}% 미만"})
    logs = resources.get("logs") or {}
    if logs.get("neverExpire"):
        findings.append({"kind": "log-retention", "status": "warn",
                         "title": f"보존 기간 미설정 로그 그룹 {logs['neverExpire']}개",
                         "detail": _names([g['name'] for g in logs.get('groups', [])])})
    return findings


def _names(names: List[str], shown: int = 2) -> str:
    return ", ".join(names[:shown]) + (f" 외 {len(names) - shown}개" if len(names) > shown else "")


def build_errors(errors: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not errors:
        return None
    hourly = list(errors.get("hourly") or [])
    hourly = [0] * (48 - len(hourly)) + hourly[-48:]
    return {"total24h": sum(hourly[24:]), "previous24h": sum(hourly[:24]), "hourly": hourly[24:]}


def build_changes(trail: Optional[Dict[str, Any]], app_changes: List[Dict[str, Any]],
                  now: int) -> List[Dict[str, Any]]:
    """24시간 안의 변경, 최근 것부터. 이 앱에서 실행한 변경이 CloudTrail에도 있으면(같은 요청 ID) 이 앱 쪽만 남긴다."""
    ours = {change.get("requestId") for change in app_changes if change.get("requestId")}
    changes = [{"at": c["at"], "source": "app", "actor": c["actor"], "summary": c["summary"]} for c in app_changes]
    for event in (trail or {}).get("events", []):
        if event.get("requestId") and event["requestId"] in ours:
            continue
        what = event["eventName"] + (f" · {event['resource']}" if event.get("resource")
                                     else f" ({event.get('eventSource')})")
        changes.append({"at": event.get("at") or 0, "source": "cloudtrail", "actor": event.get("actor") or "",
                        "summary": what})
    return sorted((c for c in changes if now - (c["at"] or 0) <= 86400), key=lambda c: c["at"], reverse=True)


def app_changes_from_audit(audit_table, now: int) -> List[Dict[str, Any]]:
    """감사 로그의 날짜 인덱스(by-day)에서 지난 24시간의 executed 사건 (이 앱에서 승인해 실행한 변경).
    날짜는 UTC로 저장되어 있어 오늘·어제 이틀을 읽는다."""
    if audit_table is None:
        return []
    moment = datetime.fromtimestamp(now, timezone.utc)
    since = (moment - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    changes = []
    for day in (moment.date(), (moment - timedelta(days=1)).date()):
        kwargs: Dict[str, Any] = {"IndexName": "by-day", "ScanIndexForward": False,
                                  "KeyConditionExpression": Key("day").eq(day.isoformat()) & Key("at").gte(since)}
        while True:
            page = audit_table.query(**kwargs)
            for item in page.get("Items", []):
                if item.get("kind") == "action" and item.get("event") == "executed":
                    changes.append({"at": _epoch_of(item["at"]), "actor": item.get("email") or item.get("userId"),
                                    "summary": item.get("summary") or item.get("tool"),
                                    "requestId": item.get("awsRequestId")})
            if not page.get("LastEvaluatedKey") or len(changes) >= MAX_APP_CHANGES:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return changes[:MAX_APP_CHANGES]


def _epoch_of(at: str) -> int:
    """감사 로그의 at ("2026-09-27T05:33:21.123Z#…")을 epoch 초로."""
    stamp = at.split("#", 1)[0].rstrip("Z")
    return int(datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp())


def pending_approvals(approval_store, can_view: Callable[..., bool], caller_id: str, groups: List[str],
                      now: int) -> Dict[str, Any]:
    """이 사용자가 볼 수 있는 승인 대기 요청 수와 가장 빠른 만료. 승인 요청 테이블은 작아서(기록 TTL) 한 번 훑는다."""
    if approval_store is None or not caller_id:
        return {"pending": 0}
    pending = []
    kwargs: Dict[str, Any] = {"FilterExpression": "#s = :pending",
                              "ExpressionAttributeNames": {"#s": "status"},
                              "ExpressionAttributeValues": {":pending": "pending"}}
    while True:
        page = approval_store.table.scan(**kwargs)
        pending.extend(item for item in page.get("Items", [])
                       if int(item.get("expiresAt", 0)) > now and can_view(item, caller_id, groups))
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    result: Dict[str, Any] = {"pending": len(pending)}
    if pending:
        result["soonestExpiresAt"] = min(int(item["expiresAt"]) for item in pending)
    return result


def build_view(sections: Dict[str, Dict[str, Any]], *, env: str, region: str, approvals: Dict[str, Any],
               app_changes: List[Dict[str, Any]], now: Optional[int] = None,
               show_changes: bool = True) -> Dict[str, Any]:
    """저장된 구역들(DashboardStore.get_all) + 실시간 값 → 화면 모양 (types/dashboard.ts의 DashboardData).
    show_changes=False(일반 사용자)면 최근 변경을 빼고 null로 둔다."""
    now = now or int(time.time())
    shown = [name for name in SECTIONS if name in sections and (show_changes or name != "changes")]

    def data(name: str) -> Optional[Dict[str, Any]]:
        return (sections.get(name) or {}).get("data")

    resources = build_resources(data("alarms"), data("resources"), data("usage"))
    changes = build_changes(data("changes"), app_changes, now) if show_changes else None
    collected = [s.get("collectedAt") for s in sections.values() if s.get("collectedAt")]
    return {
        "generatedAt": max(collected) if collected else None,
        "env": env,
        "region": region,
        "sections": {name: {key: sections[name].get(key) for key in ("ok", "error", "collectedAt", "lastSuccessAt")}
                     for name in shown},
        "alarms": data("alarms"),
        "errors": build_errors(data("errors")),
        "cost": data("cost"),
        "approvals": approvals,
        "resources": resources["rows"],
        "resourceCounts": resources["counts"],
        "resourceTotal": resources["total"],
        "changes": changes[:MAX_CHANGES] if changes is not None else None,
        "changesTotal": len(changes) if changes is not None else None,
        "findings": build_findings(data("resources"), data("usage")),
    }
