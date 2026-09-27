"""홈 대시보드 수집 Lambda. 두 가지로 불린다 (cloudformation의 Scheduler·EventBridge 규칙).
1) 정해진 때 (EventBridge Scheduler가 입력으로 구역을 준다)
     5분마다   {"sections": ["alarms", "resources", "errors", "changes"]}
     1시간마다 {"sections": ["usage"]}
     하루 1번  {"sections": ["cost"]}
2) AWS에서 무언가 바뀌었을 때 (EventBridge 이벤트) → 그 구역만 바로 다시 모은다 (몇 초 안에 홈에 보인다)
     알람 상태 변경 (aws.cloudwatch, CloudWatch Alarm State Change)      → alarms
     EC2 상태 변경 (aws.ec2, EC2 Instance State-change Notification)     → resources
     CloudTrail의 쓰기 API 호출 (AWS API Call via CloudTrail, 트레일 필요) → changes, resources (알람 설정이면 alarms도)
- 이벤트는 몰려 올 수 있다 (배포 한 번에 API 호출 수십 번). 이벤트로 불렸을 때는 그 구역을 MIN_EVENT_GAP_S 안에
  이미 모았으면 건너뛴다 (CloudTrail LookupEvents는 초당 2번 제한이 있다). 정해진 때의 수집은 늘 모은다.
- 구역마다 따로 모으고 따로 저장한다. 한 구역이 실패해도 나머지는 저장된다 (dashboard_store.put_error).
"""
import os
import time
from typing import Any, Callable, Dict, List

import boto3

import collector
from common.dashboard_store import DashboardStore

SECTIONS = ("alarms", "resources", "errors", "changes", "usage", "cost")
MIN_EVENT_GAP_S = 20

_clients: Dict[str, Any] = {}


def client(name: str):
    """boto3 클라이언트 (컨테이너마다 한 번 만들어 다시 쓴다).
    Cost Explorer는 전역 엔드포인트라 어느 리전의 클라이언트로 불러도 된다 (botocore가 알맞은 주소로 보낸다)."""
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def store() -> DashboardStore:
    return DashboardStore(boto3.resource("dynamodb").Table(os.environ["DASHBOARD_TABLE"]))


def sections_for(event: Dict[str, Any]) -> List[str]:
    """이 호출에서 모을 구역."""
    if isinstance(event.get("sections"), list):  # Scheduler 입력
        return [s for s in event["sections"] if s in SECTIONS]
    source = event.get("source")
    detail_type = event.get("detail-type")
    if source == "aws.cloudwatch" and detail_type == "CloudWatch Alarm State Change":
        return ["alarms"]
    if source == "aws.ec2" and detail_type == "EC2 Instance State-change Notification":
        return ["resources"]
    if detail_type == "AWS API Call via CloudTrail":
        sections = ["changes", "resources"]
        if (event.get("detail") or {}).get("eventSource") == "monitoring.amazonaws.com":
            sections.append("alarms")  # 알람을 만들거나 지우거나 설정을 바꿨다
        return sections
    return []


def _usage_targets(saved: Dict[str, Dict[str, Any]], fresh: Dict[str, Any]) -> Dict[str, List[str]]:
    """usage가 물을 함수·인스턴스: 이번에 모은 resources가 있으면 그것, 없으면 저장된 것."""
    resources = fresh.get("resources") or (saved.get("resources") or {}).get("data") or {}
    return {"functions": [f["name"] for f in resources.get("lambdas", [])],
            "instances": [i["id"] for i in resources.get("ec2", []) if i.get("state") != "stopped"]}


def collect(sections: List[str], dashboard: DashboardStore, from_event: bool,
            now: Callable[[], float] = time.time) -> Dict[str, str]:
    results: Dict[str, str] = {}
    fresh: Dict[str, Any] = {}
    saved: Dict[str, Dict[str, Any]] = {}
    if "usage" in sections and "resources" not in sections:
        saved = dashboard.get_all()
    # resources를 usage보다 먼저 (usage가 이름을 쓴다)
    for section in sorted(sections, key=lambda s: SECTIONS.index(s)):
        if from_event:
            last = dashboard.collected_at(section)
            if last is not None and now() - last < MIN_EVENT_GAP_S:
                results[section] = "skipped"
                continue
        try:
            if section == "alarms":
                data = collector.collect_alarms(client("cloudwatch"))
            elif section == "resources":
                data = collector.collect_resources(client("lambda"), client("ec2"), client("s3"), client("logs"))
            elif section == "errors":
                data = collector.collect_errors(client("cloudwatch"))
            elif section == "changes":
                data = collector.collect_changes(client("cloudtrail"))
            elif section == "usage":
                targets = _usage_targets(saved, fresh)
                data = collector.collect_usage(client("cloudwatch"), targets["functions"], targets["instances"])
            else:
                data = collector.collect_cost(client("ce"))
            fresh[section] = data
            dashboard.put_section(section, data, int(now()))
            results[section] = "ok"
        except Exception as error:
            print(f"대시보드 {section} 구역을 모으지 못함: {error}")
            try:
                dashboard.put_error(section, f"{type(error).__name__}: {error}", int(now()))
            except Exception as store_error:
                print(f"실패 표시도 저장하지 못함: {store_error}")
            results[section] = "error"
    return results


def lambda_handler(event, context):
    sections = sections_for(event or {})
    if not sections:
        print(f"모을 구역이 없는 이벤트 (무시): {(event or {}).get('detail-type')}")
        return {"sections": {}}
    results = collect(sections, store(), from_event="sections" not in (event or {}))
    print(f"대시보드 수집: {results}")
    return {"sections": results}
