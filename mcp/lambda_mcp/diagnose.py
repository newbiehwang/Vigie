"""서비스별 진단 절차 (런북을 코드로 옮긴 것): 자원 하나를 정해진 차례로 확인하고 층마다 판정을 낸다.

    diagnoseService(service, resource, hours)   (app.py의 도구)
        └─▶ run() ──▶ 서비스별 절차 (_alb · _ec2 · _lambda · _s3)
                         ├─ 조회 API·지표·로그로 항목(check)을 하나씩 확인한다. 항목 = 층 · 이름 · 판정 · 찾은 것 · 근거(API·지표 이름)
                         └─ 층마다 항목을 모아 층의 판정을 정하고, 원인 층과 요약을 만든다

왜 절차로 두나
- 모델에게 "원인을 찾아 줘"라고만 맡기면 매번 다른 도구를 다른 차례로 부르고, 무엇을 확인했는지 남지 않는다.
- 현업 런북처럼 층을 고정하고(아래 L1~L7) 층마다 무엇을 볼지 코드로 정해 두면, 같은 장애에는 같은 판정이 나오고
  판정마다 근거가 붙는다. 모델은 이 판정을 읽고 설명·다음 조치를 쓴다 (판정을 바꾸지 않는다).
- tests/test_diagnose.py가 moto로 장애를 심고(대상 중지, 보안 그룹 차단, 예약 동시성 0, 버킷 공개 …) 원인 층을 맞히는지 채점한다.

진단 층 (요청이 지나가는 길을 층으로 고정했다. 서비스마다 층이 가리키는 부품이 다르다: SERVICES의 components)
    L1 AWS 자체        AWS 쪽 장애인가 (하드웨어 상태 검사·예정 이벤트. Health API는 Business Support 이상이라 사람이 확인)
    L2 변경            직전에 무엇이 바뀌었나 (CloudTrail 쓰기 이벤트 중 이 자원과 관련된 것)
    L3 입구·네트워크    요청이 목적지까지 가나 (서브넷 경로, 보안 그룹, NACL)
    L4 로드 밸런서·게이트웨이  앞단이 뒤에 닿나 (리스너, 대상 그룹, 대상 헬스, ELB가 만든 5xx)
    L5 컴퓨팅          실행이 되나 (인스턴스 상태·상태 검사·CPU, Lambda 오류·시간 초과)
    L6 권한·한도        막혔나 (동시성, 권한 거부, 공개 설정, 연결 한도)
    L7 데이터·의존성    뒤가 느리거나 죽었나 (대상 응답 시간, 이벤트 소스, 데이터 보호)

판정 (층의 판정 = 그 층 항목 중 가장 무거운 것. 무거운 차례: 원인 > 증상 > 주의 > 정상 > 확인 불가 > 해당 없음)
    cause    원인      이 층에서 장애를 설명하는 것을 찾았다 (예: 대상 인스턴스가 모두 멈춤)
    symptom  증상      이상이 보이지만 다른 층의 결과다 (예: 대상이 모두 unhealthy → 원인은 컴퓨팅이나 네트워크)
    warn     주의      지금 장애의 원인은 아니지만 위험한 설정 (예: 버전 관리 꺼짐)
    ok       정상
    unknown  확인 불가  볼 수 없었다 (권한 없음, 지표 없음, API가 지원 플랜에 묶임)
    skip     해당 없음  이 서비스에는 없는 층 (예: S3의 컴퓨팅)
- L2 변경은 다른 층에 원인·증상이 있을 때만 원인(계기)이 된다. 장애가 없으면 변경이 있어도 정상이다.
- 원인이 여럿이면 모두 원인이다. 요약은 L2가 아닌 원인(무엇이 고장 났나)을 먼저, L2(무엇이 계기였나)를 뒤에 쓴다.

모두 조회 API만 부른다 (Describe*·Get*·List*·LookupEvents·FilterLogEvents·GetMetricData). AWS를 바꾸지 않는다.
보안 그룹·NACL·버킷 공개 여부·CloudTrail을 읽으므로 관리자 전용 도구다 (risk.py의 ADMIN_ONLY).
"""
import ipaddress
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------- 층과 판정
LAYERS: List[Tuple[str, str]] = [
    ("L1", "AWS 자체"),
    ("L2", "변경"),
    ("L3", "입구·네트워크"),
    ("L4", "로드 밸런서·게이트웨이"),
    ("L5", "컴퓨팅"),
    ("L6", "권한·한도"),
    ("L7", "데이터·의존성"),
]
LAYER_NAMES = dict(LAYERS)

CAUSE, SYMPTOM, WARN, OK, UNKNOWN, SKIP = "cause", "symptom", "warn", "ok", "unknown", "skip"
WEIGHT = {CAUSE: 5, SYMPTOM: 4, WARN: 3, OK: 2, UNKNOWN: 1, SKIP: 0}  # 층의 판정 = 가장 무거운 항목

DEFAULT_HOURS = 1
MAX_HOURS = 72
CHANGE_LEAD_HOURS = 1  # 변경은 보는 시간보다 1시간 앞에서부터 찾는다 (증상 직전의 변경이 계기인 경우가 많다)
CHANGE_LIMIT = 10  # 층에 남길 변경의 최대 수
TRAIL_PAGES = 4  # LookupEvents 쪽수 상한 (한 쪽 50건. 초당 2회 한도가 있어 넉넉히 끊는다)
LOG_EVENT_LIMIT = 500  # 로그에서 분류할 최대 줄 수
CPU_HIGH = 90.0  # CPU 최대값이 이 이상이면 과부하 증상
NEAR_TIMEOUT = 0.95  # 실행 시간이 제한의 95% 이상이면 시간 초과로 본다
ITERATOR_AGE_MS = 60_000  # 스트림 처리 지연 (1분)
BURSTABLE = ("t2.", "t3.", "t3a.", "t4g.")  # CPU 크레딧을 쓰는 인스턴스 종류
TARGET_GROUP_LIMIT = 30  # EC2 진단에서 인스턴스가 든 대상 그룹을 찾을 때 볼 최대 대상 그룹 수 (그룹마다 API 한 번)

HEALTH_NOTE = ("AWS Health API는 Business Support 이상에서만 열려 조회하지 않았습니다. "
               "콘솔의 계정별 Health Dashboard에서 이 리전·AZ의 이벤트를 확인하세요")

INTERNET_ADDRESS = "203.0.113.10"  # NACL을 인터넷 쪽에서 평가할 때 쓰는 대표 주소 (문서용 주소 대역)


class Diagnosis:
    """진단 한 번의 항목을 모으고 층의 판정·요약을 만든다."""

    def __init__(self, service: str, resource: str, hours: int, now: datetime):
        self.service = service
        self.resource = resource
        self.hours = hours
        self.now = now
        self.start = now - timedelta(hours=hours)
        self._checks: Dict[str, List[Dict[str, str]]] = {layer: [] for layer, _ in LAYERS}

    def check(self, layer: str, name: str, status: str, finding: str, evidence: str = "") -> None:
        self._checks[layer].append({"name": name, "status": status, "finding": finding, "evidence": evidence})

    def skip(self, layer: str, finding: str) -> None:
        self.check(layer, "해당 없음", SKIP, finding)

    def status_of(self, layer: str) -> str:
        checks = self._checks[layer]
        return max((c["status"] for c in checks), key=WEIGHT.get) if checks else SKIP

    def has_trouble(self, except_layer: str = "") -> bool:
        """다른 층에 원인이나 증상이 있나 (L2 변경을 계기로 볼지 정할 때)."""
        return any(self.status_of(layer) in (CAUSE, SYMPTOM) for layer, _ in LAYERS if layer != except_layer)

    def result(self, components: Dict[str, str], title: str) -> Dict[str, Any]:
        layers = []
        for layer, name in LAYERS:
            status = self.status_of(layer)
            checks = self._checks[layer]
            lead = next((c for c in checks if c["status"] == status), None)  # 층의 판정을 정한 첫 항목
            layers.append({"id": layer, "name": name, "component": components.get(layer, ""), "status": status,
                           "finding": lead["finding"] if lead else "", "checks": checks})
        causes = [layer["id"] for layer in layers if layer["status"] == CAUSE]
        symptoms = [layer["id"] for layer in layers if layer["status"] == SYMPTOM]
        return {"service": self.service, "service_name": title, "resource": self.resource, "hours": self.hours,
                "checked_at": self.now.isoformat(timespec="seconds"), "layers": layers, "causes": causes,
                "symptoms": symptoms, "summary": _summary(layers),
                "note": "판정은 정해진 진단 절차(lambda_mcp/diagnose.py)가 조회 결과로 낸 것입니다. "
                        "원인 층의 근거를 먼저 확인하고, 확인 불가 층은 사람이 확인하세요"}


def _summary(layers: List[Dict[str, Any]]) -> str:
    by_id = {layer["id"]: layer for layer in layers}
    causes = [layer for layer in layers if layer["status"] == CAUSE]
    symptoms = [layer for layer in layers if layer["status"] == SYMPTOM]
    if causes:
        parts = [f"{layer['id']} {layer['name']}: {layer['finding']}" for layer in causes if layer["id"] != "L2"]
        if by_id["L2"]["status"] == CAUSE:
            if not parts:  # 고장 난 곳은 못 찾았지만 증상 직전에 변경이 있었다 → 그 변경이 가장 유력하다
                return (f"원인 — 증상 직전의 변경(L2): {by_id['L2']['finding']} / 증상 — "
                        + " / ".join(f"{layer['id']} {layer['name']}: {layer['finding']}" for layer in symptoms))
            parts.append(f"계기 L2 변경: {by_id['L2']['finding']}")
        return "원인 — " + " / ".join(parts)
    if symptoms:
        return ("원인 층을 특정하지 못했습니다. 증상 — "
                + " / ".join(f"{layer['id']} {layer['name']}: {layer['finding']}" for layer in symptoms))
    return "이 절차가 보는 범위에서는 이상이 없습니다"


# ---------------------------------------------------------------- AWS 조회 도우미
class Clients:
    """리전별 boto3 클라이언트를 처음 쓸 때 만든다 (진단마다 필요한 서비스만)."""

    def __init__(self, region: str):
        self.region = region
        self._made: Dict[Tuple[str, str], Any] = {}

    def __call__(self, service: str, region: Optional[str] = None):
        key = (service, region or self.region)
        if key not in self._made:
            self._made[key] = boto3.client(service, region_name=key[1])
        return self._made[key]


def _code(error: Exception) -> str:
    return error.response.get("Error", {}).get("Code", "") if isinstance(error, ClientError) else type(error).__name__


def _metrics(cloudwatch, namespace: str, dimensions: Dict[str, str], specs: List[Tuple[str, str, str]],
             start: datetime, end: datetime) -> Dict[str, Optional[float]]:
    """지표 여럿을 GetMetricData 한 번으로. specs: (키, 지표 이름, 통계) → {키: 값}. 데이터가 없으면 None."""
    period = max(60, int((end - start).total_seconds()) // 60 * 60)
    queries = [{"Id": f"m{index}", "ReturnData": True,
                "MetricStat": {"Metric": {"Namespace": namespace, "MetricName": metric,
                                          "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()]},
                               "Period": period, "Stat": stat}}
               for index, (_, metric, stat) in enumerate(specs)]
    values: Dict[str, Optional[float]] = {key: None for key, _, _ in specs}
    for result in cloudwatch.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)["MetricDataResults"]:
        key, _, stat = specs[int(result["Id"][1:])]
        points = result.get("Values") or []
        if points:
            values[key] = (sum(points) if stat == "Sum" else max(points) if stat == "Maximum"
                           else min(points) if stat == "Minimum" else sum(points) / len(points))
    return values


def _count(value: Optional[float]) -> int:
    return int(value or 0)


def _clock(moment: Any) -> str:
    """한국 시간 월-일 시:분 (근거 글에 쓴다)."""
    if not isinstance(moment, datetime):
        return str(moment)
    return (moment.astimezone(timezone.utc) + timedelta(hours=9)).strftime("%m-%d %H:%M")


def recent_changes(clients: Clients, identifiers: Iterable[str], start: datetime, end: datetime) -> List[Dict[str, str]]:
    """CloudTrail의 최근 쓰기 이벤트 중 이 자원과 관련된 것 (L2 변경). 조회만 (LookupEvents, 최근 90일, 무료).
    LookupEvents는 조건을 하나만 받아서 ReadOnly=false로 쓰기 이벤트만 받은 뒤 자원 이름·ID로 거른다.
    tests/test_diagnose.py가 이 함수를 가짜로 바꿔 변경을 심는다 (moto는 LookupEvents를 지원하지 않는다)."""
    wanted = [identifier for identifier in identifiers if identifier]
    trail = clients("cloudtrail")
    kwargs: Dict[str, Any] = {"LookupAttributes": [{"AttributeKey": "ReadOnly", "AttributeValue": "false"}],
                              "StartTime": start, "EndTime": end, "MaxResults": 50}
    found: List[Dict[str, str]] = []
    for _ in range(TRAIL_PAGES):
        page = trail.lookup_events(**kwargs)
        for event in page.get("Events", []):
            names = {resource.get("ResourceName", "") for resource in event.get("Resources", [])}
            raw = event.get("CloudTrailEvent", "")
            # 이름은 정확히 같은 값만 (짧은 이름이 다른 글자 속에 섞여 걸리지 않게), ARN은 포함 여부로
            hit = next((w for w in wanted if w in names or f'"{w}"' in raw or (w.startswith("arn:") and w in raw)),
                       None)
            if hit:
                found.append({"time": _clock(event.get("EventTime")), "event": event.get("EventName", ""),
                              "user": event.get("Username", ""), "resource": hit})
        if not page.get("NextToken") or len(found) >= CHANGE_LIMIT:
            break
        kwargs["NextToken"] = page["NextToken"]
    return found[:CHANGE_LIMIT]


def _check_changes(d: Diagnosis, clients: Clients, identifiers: Iterable[str]) -> None:
    """L2 변경. 다른 층의 판정이 모두 나온 뒤에 부른다 (장애가 있을 때만 변경을 계기로 본다)."""
    try:
        changes = recent_changes(clients, identifiers, d.start - timedelta(hours=CHANGE_LEAD_HOURS), d.now)
    except Exception as error:  # 권한 없음, 모킹 환경 등
        d.check("L2", "CloudTrail 쓰기 이벤트", UNKNOWN, f"CloudTrail을 조회하지 못했습니다 ({_code(error)})",
                "cloudtrail:LookupEvents")
        return
    if not changes:
        d.check("L2", "CloudTrail 쓰기 이벤트", OK,
                f"최근 {d.hours + CHANGE_LEAD_HOURS}시간 동안 관련 쓰기 이벤트가 없습니다 (CloudTrail은 보통 5분쯤 늦게 보입니다)",
                "cloudtrail:LookupEvents ReadOnly=false")
        return
    listed = ", ".join(f"{c['time']} {c['event']} ({c['user'] or '?'} → {c['resource']})" for c in changes[:3])
    more = f" 외 {len(changes) - 3}건" if len(changes) > 3 else ""
    if d.has_trouble(except_layer="L2"):
        d.check("L2", "증상 직전의 변경", CAUSE, f"관련 변경 {len(changes)}건: {listed}{more}",
                "cloudtrail:LookupEvents ReadOnly=false")
    else:
        d.check("L2", "관련 변경", OK, f"변경 {len(changes)}건이 있었지만 다른 층에 이상이 없습니다: {listed}{more}",
                "cloudtrail:LookupEvents ReadOnly=false")


# ---------------------------------------------------------------- 네트워크 평가 (L3)
def _route_table(ec2, subnet_id: str, vpc_id: str) -> Dict[str, Any]:
    """서브넷의 라우팅 테이블 (명시적 연결이 없으면 VPC의 기본 라우팅 테이블)."""
    tables = ec2.describe_route_tables(Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}])["RouteTables"]
    if not tables:
        tables = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                                                    {"Name": "association.main", "Values": ["true"]}])["RouteTables"]
    return tables[0] if tables else {"Routes": []}


def _default_route(table: Dict[str, Any]) -> Dict[str, Any]:
    return next((route for route in table.get("Routes", [])
                 if route.get("DestinationCidrBlock") == "0.0.0.0/0" and route.get("State", "active") == "active"), {})


def _network_acl(ec2, subnet_id: str, vpc_id: str) -> Dict[str, Any]:
    acls = ec2.describe_network_acls(Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}])["NetworkAcls"]
    if not acls:
        acls = ec2.describe_network_acls(Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                                                  {"Name": "default", "Values": ["true"]}])["NetworkAcls"]
    return acls[0] if acls else {}


def nacl_decision(acl: Dict[str, Any], egress: bool, port: int, address: str) -> Tuple[bool, Optional[int]]:
    """NACL이 이 방향·TCP 포트·주소를 허용하나. 규칙 번호가 작은 것부터 처음 맞는 규칙이 정한다 (없으면 거부).
    NACL이 없으면(조회 못 함) 허용으로 본다."""
    if not acl:
        return True, None
    ip = ipaddress.ip_address(address)
    for entry in sorted((e for e in acl.get("Entries", []) if e.get("Egress") == egress), key=lambda e: e["RuleNumber"]):
        if str(entry.get("Protocol")) not in ("-1", "6"):
            continue
        if str(entry.get("Protocol")) == "6":
            ports = entry.get("PortRange") or {}
            if not ports.get("From", 0) <= port <= ports.get("To", 65535):
                continue
        cidr = entry.get("CidrBlock") or entry.get("Ipv6CidrBlock")
        if not cidr:
            continue
        network = ipaddress.ip_network(cidr)
        if network.version != ip.version or ip not in network:
            continue
        return entry.get("RuleAction") == "allow", entry["RuleNumber"]
    return False, None


def sg_opens(groups: List[Dict[str, Any]], port: int, from_groups: Iterable[str] = (),
             from_networks: Iterable[Any] = ()) -> List[str]:
    """보안 그룹들이 이 TCP 포트를 연 출처 목록. from_groups·from_networks를 주면 그 출처와 겹치는 규칙만 센다.
    접두사 목록은 내용을 모르니 연 것으로 본다."""
    from_groups, from_networks = set(from_groups), list(from_networks)
    sources: List[str] = []
    for group in groups:
        for rule in group.get("IpPermissions", []):
            if rule.get("IpProtocol") not in ("-1", "tcp", "6"):
                continue
            if rule.get("IpProtocol") != "-1" and not rule.get("FromPort", 0) <= port <= rule.get("ToPort", 65535):
                continue
            for pair in rule.get("UserIdGroupPairs", []):
                if not from_groups or pair.get("GroupId") in from_groups:
                    sources.append(pair.get("GroupId", ""))
            for block in rule.get("IpRanges", []) + rule.get("Ipv6Ranges", []):
                cidr = block.get("CidrIp") or block.get("CidrIpv6")
                network = ipaddress.ip_network(cidr)
                if not from_networks or any(network.version == n.version and network.overlaps(n) for n in from_networks):
                    sources.append(cidr)
            sources += [p.get("PrefixListId", "") for p in rule.get("PrefixListIds", [])]
    return sources


def _groups(ec2, group_ids: Iterable[str]) -> List[Dict[str, Any]]:
    ids = sorted(set(group_ids))
    return ec2.describe_security_groups(GroupIds=ids)["SecurityGroups"] if ids else []


def _subnets(ec2, subnet_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    ids = sorted(set(subnet_ids))
    return {s["SubnetId"]: s for s in ec2.describe_subnets(SubnetIds=ids)["Subnets"]} if ids else {}


def _first_host(cidr: str) -> str:
    return str(next(ipaddress.ip_network(cidr).hosts()))


# ---------------------------------------------------------------- EC2 공통
def _instances(ec2, instance_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not instance_ids:
        return {}
    found = {}
    for page in ec2.get_paginator("describe_instances").paginate(InstanceIds=instance_ids):
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                found[instance["InstanceId"]] = instance
    return found


def _label(instance: Dict[str, Any]) -> str:
    name = next((t["Value"] for t in instance.get("Tags", []) if t.get("Key") == "Name"), None)
    return f"{instance['InstanceId']} ({name})" if name else instance["InstanceId"]


def _check_hardware(d: Diagnosis, ec2, instance_ids: List[str]) -> None:
    """L1: AWS 쪽 하드웨어 (시스템 상태 검사, 예정 이벤트)."""
    if instance_ids:
        statuses = ec2.describe_instance_status(InstanceIds=instance_ids, IncludeAllInstances=True)["InstanceStatuses"]
        impaired = [s["InstanceId"] for s in statuses if s.get("SystemStatus", {}).get("Status") == "impaired"]
        events = [f"{s['InstanceId']} {e.get('Code')}" for s in statuses for e in s.get("Events", [])
                  if not (e.get("Description") or "").startswith("[Completed]")]
        if impaired:
            d.check("L1", "시스템 상태 검사", CAUSE,
                    f"AWS 쪽 호스트·네트워크 문제로 시스템 상태 검사가 실패했습니다: {', '.join(impaired)} "
                    "(EBS 기반이면 중지 후 시작해 다른 호스트로 옮깁니다)", "ec2:DescribeInstanceStatus SystemStatus")
        elif events:
            d.check("L1", "예정된 이벤트", WARN, f"AWS가 예정한 이벤트가 있습니다: {', '.join(events)}",
                    "ec2:DescribeInstanceStatus Events")
        else:
            d.check("L1", "시스템 상태 검사", OK, f"인스턴스 {len(statuses)}대의 시스템 상태 검사가 정상입니다",
                    "ec2:DescribeInstanceStatus SystemStatus")
    d.check("L1", "AWS Health 이벤트", UNKNOWN, HEALTH_NOTE, "health:DescribeEvents")


def _check_instances(d: Diagnosis, ec2, cloudwatch, instances: Dict[str, Dict[str, Any]],
                     network_blamed: bool = False, health_reasons: Optional[Dict[str, str]] = None) -> None:
    """L5: 인스턴스 상태·OS 상태 검사·CPU·CPU 크레딧. health_reasons: 대상 헬스의 사유 코드 (ALB에서 온 경우)."""
    health_reasons = health_reasons or {}
    stopped = [i for i in instances.values() if i.get("State", {}).get("Name") != "running"]
    if stopped:
        # 여러 대면 한 항목으로 (전부 멈췄는지가 판단에 중요하다)
        first = stopped[0]
        reason = first.get("StateReason", {}).get("Message") or first.get("StateTransitionReason") or ""
        whole = f"{len(instances)}대 모두" if len(stopped) == len(instances) > 1 else f"{len(stopped)}대"
        d.check("L5", "인스턴스 상태", CAUSE,
                (f"인스턴스 {whole}가 실행 중이 아닙니다: " if len(stopped) > 1 else "")
                + ", ".join(f"{_label(i)} {i.get('State', {}).get('Name')}" for i in stopped[:4])
                + (f" 외 {len(stopped) - 4}대" if len(stopped) > 4 else "")
                + (f" ({reason})" if reason else ""), "ec2:DescribeInstances State·StateReason")
    running = [i["InstanceId"] for i in instances.values() if i.get("State", {}).get("Name") == "running"]
    if running:
        statuses = ec2.describe_instance_status(InstanceIds=running)["InstanceStatuses"]
        for status in statuses:
            if status.get("InstanceStatus", {}).get("Status") == "impaired":
                d.check("L5", "인스턴스 상태 검사", CAUSE,
                        f"{_label(instances[status['InstanceId']])}의 OS 수준 상태 검사가 실패했습니다 "
                        "(메모리 고갈, 커널·파일 시스템 오류, 네트워크 설정)", "ec2:DescribeInstanceStatus InstanceStatus")
    for instance_id in running:
        instance = instances[instance_id]
        specs = [("cpu", "CPUUtilization", "Maximum")]
        burstable = instance.get("InstanceType", "").startswith(BURSTABLE)
        if burstable:
            specs.append(("credits", "CPUCreditBalance", "Minimum"))
        values = _metrics(cloudwatch, "AWS/EC2", {"InstanceId": instance_id}, specs, d.start, d.now)
        if burstable and values.get("credits") is not None and values["credits"] < 1:
            d.check("L5", "CPU 크레딧", CAUSE,
                    f"{_label(instance)}의 CPU 크레딧이 바닥나(최소 {values['credits']:.1f}) 기준 성능으로 묶였습니다",
                    "AWS/EC2 CPUCreditBalance Minimum")
        if values["cpu"] is not None and values["cpu"] >= CPU_HIGH:
            d.check("L5", "CPU 사용률", SYMPTOM, f"{_label(instance)}의 CPU가 최대 {values['cpu']:.0f}%까지 올랐습니다",
                    "AWS/EC2 CPUUtilization Maximum")
    for instance_id, reason in health_reasons.items():
        if instance_id not in running:
            continue
        if reason in ("Target.FailedHealthChecks", "Target.ResponseCodeMismatch", "Target.Timeout"):
            what = {"Target.FailedHealthChecks": "헬스체크 포트에서 앱이 응답하지 않습니다 (프로세스·포트 확인)",
                    "Target.ResponseCodeMismatch": "헬스체크 경로가 기대한 코드를 주지 않습니다 (경로 없음 404, 리다이렉트, 깊은 헬스체크의 5xx)",
                    "Target.Timeout": "헬스체크 응답이 제한 시간 안에 오지 않습니다 (앱이 느리거나 멈춤)"}[reason]
            # 네트워크가 막혀 있으면 이 사유는 그 결과다 (L3가 원인)
            d.check("L5", "대상 헬스 사유", SYMPTOM if network_blamed else CAUSE,
                    f"{_label(instances[instance_id])}: {reason} — {what}", "elbv2:DescribeTargetHealth Reason")
    if not stopped and running and d.status_of("L5") == SKIP:
        d.check("L5", "인스턴스", OK, f"인스턴스 {len(running)}대가 실행 중이고 상태 검사가 정상입니다",
                "ec2:DescribeInstances · DescribeInstanceStatus")


# ---------------------------------------------------------------- ALB
def _alb(d: Diagnosis, clients: Clients) -> List[str]:
    elb, ec2, cloudwatch = clients("elbv2"), clients("ec2"), clients("cloudwatch")
    try:
        key = {"LoadBalancerArns": [d.resource]} if d.resource.startswith("arn:") else {"Names": [d.resource]}
        balancers = elb.describe_load_balancers(**key)["LoadBalancers"]
    except ClientError as error:
        if _code(error) == "LoadBalancerNotFound":
            raise ValueError(f"로드 밸런서가 없습니다: {d.resource}")
        raise
    lb = balancers[0]
    if lb.get("Type") != "application":
        raise ValueError(f"Application Load Balancer만 진단합니다 (이 로드 밸런서는 {lb.get('Type')})")
    lb_arn, vpc_id = lb["LoadBalancerArn"], lb.get("VpcId", "")
    lb_dimension = lb_arn.split(":loadbalancer/")[-1]  # app/<이름>/<ID>
    lb_subnet_ids = [zone["SubnetId"] for zone in lb.get("AvailabilityZones", []) if zone.get("SubnetId")]
    lb_groups = lb.get("SecurityGroups", [])
    internet_facing = lb.get("Scheme") == "internet-facing"

    listeners = elb.describe_listeners(LoadBalancerArn=lb_arn)["Listeners"]
    target_groups = elb.describe_target_groups(LoadBalancerArn=lb_arn)["TargetGroups"]
    health: Dict[str, List[Dict[str, Any]]] = {
        tg["TargetGroupArn"]: elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])["TargetHealthDescriptions"]
        for tg in target_groups}
    targets = [(tg, t) for tg in target_groups for t in health[tg["TargetGroupArn"]]]
    instance_ids = sorted({t["Target"]["Id"] for _, t in targets if t["Target"]["Id"].startswith("i-")})
    instances = _instances(ec2, instance_ids)

    # 지표 (보는 시간 전체의 합·최대)
    values = _metrics(cloudwatch, "AWS/ApplicationELB", {"LoadBalancer": lb_dimension}, [
        ("requests", "RequestCount", "Sum"),
        ("elb5xx", "HTTPCode_ELB_5XX_Count", "Sum"),
        ("elb502", "HTTPCode_ELB_502_Count", "Sum"),
        ("elb503", "HTTPCode_ELB_503_Count", "Sum"),
        ("elb504", "HTTPCode_ELB_504_Count", "Sum"),
        ("target5xx", "HTTPCode_Target_5XX_Count", "Sum"),
        ("connection_errors", "TargetConnectionErrorCount", "Sum"),
        ("rejected", "RejectedConnectionCount", "Sum"),
        ("unhealthy_routing", "UnhealthyRoutingRequestCount", "Sum"),
        ("response_time", "TargetResponseTime", "Maximum"),
    ], d.start, d.now)

    # L1 AWS 자체
    _check_hardware(d, ec2, instance_ids)

    # L3 입구·네트워크: 서브넷 경로, ALB 보안 그룹, 대상 보안 그룹, NACL
    subnets = _subnets(ec2, lb_subnet_ids + [i.get("SubnetId", "") for i in instances.values() if i.get("SubnetId")])
    lb_networks = [ipaddress.ip_network(subnets[s]["CidrBlock"]) for s in lb_subnet_ids if s in subnets]
    if internet_facing:
        for subnet_id in lb_subnet_ids:
            route = _default_route(_route_table(ec2, subnet_id, vpc_id))
            if not str(route.get("GatewayId", "")).startswith("igw-"):
                d.check("L3", "인터넷 경로", CAUSE,
                        f"인터넷용 ALB의 서브넷 {subnet_id}에 인터넷 게이트웨이로 가는 기본 경로(0.0.0.0/0 → igw-)가 없습니다",
                        "ec2:DescribeRouteTables")
    lb_group_rules = _groups(ec2, lb_groups)
    for listener in listeners:
        port = listener["Port"]
        opened = sg_opens(lb_group_rules, port)
        if not opened:
            d.check("L3", "ALB 보안 그룹", CAUSE, f"ALB 보안 그룹이 리스너 포트 {port}을(를) 어디에도 열지 않았습니다",
                    "ec2:DescribeSecurityGroups")
        elif internet_facing and not any(s in ("0.0.0.0/0", "::/0") or s.startswith("pl-") for s in opened):
            d.check("L3", "ALB 보안 그룹", WARN, f"리스너 포트 {port}이(가) 일부 주소({', '.join(opened[:3])})에만 열려 있습니다",
                    "ec2:DescribeSecurityGroups")
        for subnet_id in lb_subnet_ids:
            allowed, rule = nacl_decision(_network_acl(ec2, subnet_id, vpc_id), False, port,
                                          INTERNET_ADDRESS if internet_facing else _first_host(subnets[subnet_id]["CidrBlock"]))
            if not allowed:
                d.check("L3", "ALB 서브넷 NACL", CAUSE,
                        f"서브넷 {subnet_id}의 NACL이 들어오는 {port} 포트를 막습니다 (규칙 {rule or '*'})",
                        "ec2:DescribeNetworkAcls")
    network_blamed_targets = set()
    for tg, target in targets:
        instance = instances.get(target["Target"]["Id"])
        if not instance or instance.get("State", {}).get("Name") != "running":
            continue
        ports = {target["Target"].get("Port") or tg.get("Port")}
        if str(tg.get("HealthCheckPort", "traffic-port")).isdigit():
            ports.add(int(tg["HealthCheckPort"]))
        groups = _groups(ec2, [g["GroupId"] for g in instance.get("SecurityGroups", [])])
        for port in sorted(p for p in ports if p):
            if not sg_opens(groups, port, from_groups=lb_groups, from_networks=lb_networks):
                network_blamed_targets.add(instance["InstanceId"])
                d.check("L3", "대상 보안 그룹", CAUSE,
                        f"{_label(instance)}의 보안 그룹이 ALB에서 오는 {port} 포트를 허용하지 않습니다 "
                        "(ALB 보안 그룹이나 ALB 서브넷 대역을 출처로 열어야 합니다. 헬스체크 Target.Timeout의 흔한 원인)",
                        "ec2:DescribeSecurityGroups")
            subnet = subnets.get(instance.get("SubnetId", ""))
            if subnet and lb_networks:
                acl = _network_acl(ec2, subnet["SubnetId"], vpc_id)
                inbound, rule_in = nacl_decision(acl, False, port, _first_host(str(lb_networks[0])))
                outbound, rule_out = nacl_decision(acl, True, 40000, _first_host(str(lb_networks[0])))  # 임시 포트로 응답
                if not inbound or not outbound:
                    network_blamed_targets.add(instance["InstanceId"])
                    d.check("L3", "대상 서브넷 NACL", CAUSE,
                            f"대상 서브넷 {subnet['SubnetId']}의 NACL이 ALB와의 통신을 막습니다 "
                            + (f"(들어오는 {port} 포트, 규칙 {rule_in or '*'})" if not inbound
                               else f"(나가는 임시 포트 1024-65535, 규칙 {rule_out or '*'})"),
                            "ec2:DescribeNetworkAcls")
    if d.status_of("L3") == SKIP:
        d.check("L3", "경로·보안 그룹·NACL", OK,
                "서브넷 경로, ALB·대상 보안 그룹, NACL이 리스너와 대상 포트를 허용합니다",
                "ec2:DescribeRouteTables · DescribeSecurityGroups · DescribeNetworkAcls")

    # L4 로드 밸런서
    state = lb.get("State", {}).get("Code", "")
    if state == "failed":
        d.check("L4", "로드 밸런서 상태", CAUSE, f"로드 밸런서 상태가 failed입니다 ({lb.get('State', {}).get('Reason', '')})",
                "elbv2:DescribeLoadBalancers State")
    elif state == "active_impaired":
        d.check("L4", "로드 밸런서 상태", SYMPTOM, "로드 밸런서가 active_impaired 상태입니다 (확장 중이거나 일부 노드 문제)",
                "elbv2:DescribeLoadBalancers State")
    if not listeners:
        d.check("L4", "리스너", CAUSE, "리스너가 없어 어떤 요청도 받지 않습니다", "elbv2:DescribeListeners")
    if not target_groups:
        d.check("L4", "대상 그룹", CAUSE, "이 로드 밸런서에 연결된 대상 그룹이 없습니다", "elbv2:DescribeTargetGroups")
    health_reasons: Dict[str, str] = {}
    for tg in target_groups:
        described = health[tg["TargetGroupArn"]]
        name = tg.get("TargetGroupName", tg["TargetGroupArn"])
        if not described:
            d.check("L4", "등록된 대상", CAUSE,
                    f"대상 그룹 {name}에 등록된 대상이 없어 ALB가 503을 냅니다 "
                    "(Auto Scaling 그룹의 대상 그룹 연결을 확인)", "elbv2:DescribeTargetHealth")
            continue
        healthy = [t for t in described if t["TargetHealth"]["State"] == "healthy"]
        reasons: Dict[str, int] = {}
        for t in described:
            reason = t["TargetHealth"].get("Reason")
            if t["TargetHealth"]["State"] != "healthy" and reason:
                reasons[reason] = reasons.get(reason, 0) + 1
                health_reasons[t["Target"]["Id"]] = reason
        reason_text = ", ".join(f"{r} {n}" for r, n in reasons.items())
        elb_side = [r for r in reasons if r.startswith("Elb.") and r not in ("Elb.InitialHealthChecking",
                                                                             "Elb.RegistrationInProgress")]
        if elb_side:
            d.check("L4", "대상 헬스 (ALB 쪽 사유)", CAUSE, f"대상 그룹 {name}: ALB 쪽 사유 {', '.join(elb_side)}",
                    "elbv2:DescribeTargetHealth Reason")
        elif not healthy:
            d.check("L4", "대상 헬스", SYMPTOM,
                    f"대상 그룹 {name}의 대상 {len(described)}대가 모두 정상이 아닙니다 ({reason_text}). "
                    "모두 unhealthy면 ALB는 헬스와 무관하게 모든 대상으로 보냅니다 (fail-open)",
                    "elbv2:DescribeTargetHealth")
        elif len(healthy) < len(described):
            d.check("L4", "대상 헬스", SYMPTOM,
                    f"대상 그룹 {name}: {len(described)}대 중 {len(healthy)}대만 정상입니다 ({reason_text})",
                    "elbv2:DescribeTargetHealth")
    if _count(values["elb5xx"]):
        codes = " · ".join(f"{code} {_count(values[f'elb{code}'])}건" for code in (502, 503, 504)
                           if _count(values[f"elb{code}"]))
        d.check("L4", "ALB가 만든 5xx", SYMPTOM,
                f"ALB가 만든 5xx {_count(values['elb5xx'])}건" + (f" ({codes})" if codes else "")
                + f" / 요청 {_count(values['requests'])}건", "AWS/ApplicationELB HTTPCode_ELB_5XX_Count")
    if _count(values["unhealthy_routing"]):
        d.check("L4", "fail-open 라우팅", SYMPTOM,
                f"건강한 대상이 없어 헬스와 무관하게 보낸 요청 {_count(values['unhealthy_routing'])}건",
                "AWS/ApplicationELB UnhealthyRoutingRequestCount")
    if d.status_of("L4") == SKIP:
        d.check("L4", "리스너·대상 그룹·헬스", OK,
                f"리스너 {len(listeners)}개, 대상 {len(targets)}대가 모두 정상이고 ALB가 만든 5xx가 없습니다",
                "elbv2:DescribeListeners · DescribeTargetHealth · HTTPCode_ELB_5XX_Count")

    # L5 컴퓨팅 (대상 EC2)
    if instances:
        _check_instances(d, ec2, cloudwatch, instances, network_blamed=bool(network_blamed_targets),
                         health_reasons=health_reasons)
    elif targets:
        d.check("L5", "대상", UNKNOWN, "대상이 EC2 인스턴스가 아닙니다 (IP·Lambda 대상은 이 절차가 보지 않습니다)",
                "elbv2:DescribeTargetHealth")
    else:
        d.skip("L5", "등록된 대상이 없어 볼 컴퓨팅이 없습니다")
    if _count(values["target5xx"]):
        d.check("L5", "대상이 만든 5xx", SYMPTOM,
                f"대상(애플리케이션)이 만든 5xx {_count(values['target5xx'])}건 → 애플리케이션 로그에서 경로·대상별로 나눠 보세요",
                "AWS/ApplicationELB HTTPCode_Target_5XX_Count")
    if _count(values["connection_errors"]):
        d.check("L5", "대상 연결 오류", SYMPTOM,
                f"ALB가 대상과 연결하지 못한 횟수 {_count(values['connection_errors'])}건 (앱 프로세스 다운·포트 불일치·보안 그룹)",
                "AWS/ApplicationELB TargetConnectionErrorCount")

    # L6 권한·한도
    if _count(values["rejected"]):
        d.check("L6", "연결 한도", CAUSE, f"연결 한도에 닿아 거부한 연결 {_count(values['rejected'])}건",
                "AWS/ApplicationELB RejectedConnectionCount")
    else:
        d.check("L6", "연결 한도", OK, "거부한 연결이 없습니다", "AWS/ApplicationELB RejectedConnectionCount")

    # L7 데이터·의존성: 대상 응답 시간이 유휴 타임아웃에 붙으면 뒤쪽(DB·외부 API)이 느린 것
    idle = 60
    try:
        attributes = elb.describe_load_balancer_attributes(LoadBalancerArn=lb_arn)["Attributes"]
        idle = int(next((a["Value"] for a in attributes if a["Key"] == "idle_timeout.timeout_seconds"), idle))
    except Exception:
        pass
    if values["response_time"] is None:
        d.check("L7", "대상 응답 시간", UNKNOWN, "보는 시간 동안 대상 응답 시간 지표가 없습니다 (요청이 없었거나 대상에 닿지 못함)",
                "AWS/ApplicationELB TargetResponseTime")
    elif values["response_time"] >= idle * 0.9:
        d.check("L7", "대상 응답 시간", SYMPTOM,
                f"대상 응답 시간이 최대 {values['response_time']:.1f}초로 유휴 타임아웃({idle}초)에 닿았습니다 "
                "→ 대상 뒤의 DB·외부 API 지연을 확인하세요", "AWS/ApplicationELB TargetResponseTime Maximum")
    else:
        d.check("L7", "대상 응답 시간", OK, f"대상 응답 시간 최대 {values['response_time']:.2f}초 (유휴 타임아웃 {idle}초)",
                "AWS/ApplicationELB TargetResponseTime Maximum")

    related = [lb_arn, lb.get("LoadBalancerName", "")] + [tg["TargetGroupArn"] for tg in target_groups]
    related += lb_groups + instance_ids + [g["GroupId"] for i in instances.values() for g in i.get("SecurityGroups", [])]
    return related


# ---------------------------------------------------------------- EC2
def _ec2(d: Diagnosis, clients: Clients) -> List[str]:
    ec2, cloudwatch, elb, autoscaling = clients("ec2"), clients("cloudwatch"), clients("elbv2"), clients("autoscaling")
    if d.resource.startswith("i-"):
        try:
            instances = _instances(ec2, [d.resource])
        except ClientError as error:
            if _code(error).startswith("InvalidInstanceID"):
                raise ValueError(f"인스턴스가 없습니다: {d.resource}")
            raise
    else:  # Name 태그
        found = [i for page in ec2.get_paginator("describe_instances").paginate(
                    Filters=[{"Name": "tag:Name", "Values": [d.resource]}])
                 for r in page.get("Reservations", []) for i in r.get("Instances", [])
                 if i.get("State", {}).get("Name") != "terminated"]
        if len(found) > 1:
            raise ValueError(f"Name 태그가 {d.resource}인 인스턴스가 {len(found)}대입니다. 인스턴스 ID로 지정하세요: "
                             + ", ".join(i["InstanceId"] for i in found[:5]))
        instances = {i["InstanceId"]: i for i in found}
    if not instances:
        raise ValueError(f"인스턴스가 없습니다: {d.resource}")
    instance = next(iter(instances.values()))
    instance_id, vpc_id = instance["InstanceId"], instance.get("VpcId", "")
    group_ids = [g["GroupId"] for g in instance.get("SecurityGroups", [])]

    # L1
    _check_hardware(d, ec2, [instance_id])

    # L3: 보안 그룹이 연 포트를 NACL이 막지 않나, 공인 IP가 있는데 인터넷 경로가 없나
    groups = _groups(ec2, group_ids)
    opened_ports = sorted({rule.get("FromPort") for g in groups for rule in g.get("IpPermissions", [])
                           if rule.get("IpProtocol") in ("tcp", "6") and rule.get("FromPort") is not None})
    if not any(g.get("IpPermissions") for g in groups):
        d.check("L3", "보안 그룹", WARN, "보안 그룹에 들어오는 규칙이 하나도 없습니다 (의도한 것인지 확인)",
                "ec2:DescribeSecurityGroups")
    subnet_id = instance.get("SubnetId", "")
    if subnet_id:
        acl = _network_acl(ec2, subnet_id, vpc_id)
        for port in opened_ports[:5]:
            sources = sg_opens(groups, port)
            address = INTERNET_ADDRESS if any(s in ("0.0.0.0/0", "::/0") for s in sources) else None
            if address is None:
                cidr = next((s for s in sources if "/" in s and ":" not in s), None)
                address = _first_host(cidr) if cidr else None
            if address is None:
                continue
            allowed, rule = nacl_decision(acl, False, port, address)
            if not allowed:
                d.check("L3", "서브넷 NACL", CAUSE,
                        f"보안 그룹은 {port} 포트를 열었지만 서브넷 {subnet_id}의 NACL이 막습니다 (규칙 {rule or '*'})",
                        "ec2:DescribeNetworkAcls")
        if instance.get("PublicIpAddress"):
            route = _default_route(_route_table(ec2, subnet_id, vpc_id))
            if not str(route.get("GatewayId", "")).startswith("igw-"):
                d.check("L3", "인터넷 경로", CAUSE,
                        f"공인 IP가 있지만 서브넷 {subnet_id}에 인터넷 게이트웨이 경로가 없어 밖에서 닿지 않습니다",
                        "ec2:DescribeRouteTables")
    if d.status_of("L3") == SKIP:
        d.check("L3", "보안 그룹·NACL·경로", OK,
                f"보안 그룹이 연 포트({', '.join(map(str, opened_ports[:5])) or '없음'})를 NACL·경로가 막지 않습니다",
                "ec2:DescribeSecurityGroups · DescribeNetworkAcls · DescribeRouteTables")

    # L4: 이 인스턴스가 들어 있는 대상 그룹의 헬스
    memberships = []
    try:
        candidates = [tg for page in elb.get_paginator("describe_target_groups").paginate()
                      for tg in page.get("TargetGroups", [])
                      if tg.get("VpcId") == vpc_id and tg.get("TargetType", "instance") == "instance"]
        for tg in candidates[:TARGET_GROUP_LIMIT]:
            for t in elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])["TargetHealthDescriptions"]:
                if t["Target"]["Id"] == instance_id:
                    memberships.append((tg, t["TargetHealth"]))
    except Exception as error:
        d.check("L4", "대상 그룹", UNKNOWN, f"대상 그룹을 조회하지 못했습니다 ({_code(error)})", "elbv2:DescribeTargetGroups")
    health_reasons: Dict[str, str] = {}
    for tg, target_health in memberships:
        if target_health["State"] == "healthy":
            d.check("L4", "대상 헬스", OK, f"대상 그룹 {tg['TargetGroupName']}에서 healthy입니다", "elbv2:DescribeTargetHealth")
        else:
            health_reasons[instance_id] = target_health.get("Reason", "")
            d.check("L4", "대상 헬스", SYMPTOM,
                    f"대상 그룹 {tg['TargetGroupName']}에서 {target_health['State']} ({target_health.get('Reason', '')})",
                    "elbv2:DescribeTargetHealth")
    if not memberships and d.status_of("L4") == SKIP:
        d.skip("L4", "로드 밸런서 대상 그룹에 들어 있지 않습니다")

    # L5 컴퓨팅
    _check_instances(d, ec2, cloudwatch, instances, network_blamed=d.status_of("L3") == CAUSE,
                     health_reasons=health_reasons)

    # L6: Auto Scaling 그룹의 시작 실패, 헬스체크 유예 0초
    group_name = next((t["Value"] for t in instance.get("Tags", []) if t.get("Key") == "aws:autoscaling:groupName"), None)
    try:
        if not group_name:
            members = autoscaling.describe_auto_scaling_instances(InstanceIds=[instance_id])["AutoScalingInstances"]
            group_name = members[0]["AutoScalingGroupName"] if members else None
    except Exception:
        group_name = None
    if group_name:
        group = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[group_name])["AutoScalingGroups"][0]
        failed = [a for a in autoscaling.describe_scaling_activities(AutoScalingGroupName=group_name,
                                                                      MaxRecords=20)["Activities"]
                  if a.get("StatusCode") in ("Failed", "Cancelled") and a.get("StartTime", d.now) >= d.start]
        if failed:
            d.check("L6", "Auto Scaling 시작 실패", CAUSE,
                    f"그룹 {group_name}의 활동 {len(failed)}건 실패: {failed[0].get('StatusMessage', '')[:160]} "
                    "(용량 부족·vCPU 한도·시작 템플릿 오류)", "autoscaling:DescribeScalingActivities")
        else:
            d.check("L6", "Auto Scaling 활동", OK, f"그룹 {group_name}에 실패한 활동이 없습니다",
                    "autoscaling:DescribeScalingActivities")
        if group.get("HealthCheckType") == "ELB" and not group.get("HealthCheckGracePeriod"):
            d.check("L5", "헬스체크 유예", WARN,
                    f"그룹 {group_name}이 ELB 헬스체크를 쓰는데 유예 시간이 0초입니다 "
                    "(CLI·IaC 기본값. 부팅 중 unhealthy로 교체돼 시작·종료가 반복될 수 있습니다)",
                    "autoscaling:DescribeAutoScalingGroups HealthCheckGracePeriod")
    else:
        d.skip("L6", "Auto Scaling 그룹에 들어 있지 않습니다 (권한·한도는 AccessDenied 로그와 서비스 쿼터로 사람이 확인)")

    # L7
    d.skip("L7", "인스턴스 안의 애플리케이션 의존성(DB·외부 API)은 이 절차가 보지 않습니다 → 애플리케이션 로그·RDS 진단")
    return [instance_id] + group_ids + ([group_name] if group_name else [])


# ---------------------------------------------------------------- Lambda
TIMEOUT_MARKS = ("Task timed out", "Status: timeout")
MEMORY_MARKS = ("Runtime.OutOfMemory", "Status: error Error Type: Runtime.OutOfMemory", "signal: killed")
DENIED_MARKS = ("AccessDenied", "is not authorized to perform", "UnauthorizedOperation")
LOG_PATTERN = '?"Task timed out" ?"Status: timeout" ?OutOfMemory ?"signal: killed" ?AccessDenied ?"not authorized"'


def _log_marks(logs, function_name: str, start: datetime, end: datetime) -> Dict[str, Any]:
    """함수 로그에서 시간 초과·메모리 부족·권한 거부 줄을 센다 (FilterLogEvents, 조회만)."""
    counts = {"timeout": 0, "memory": 0, "denied": 0}
    sample: Dict[str, str] = {}
    kwargs: Dict[str, Any] = {"logGroupName": f"/aws/lambda/{function_name}", "filterPattern": LOG_PATTERN,
                              "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000)}
    seen = 0
    while seen < LOG_EVENT_LIMIT:
        page = logs.filter_log_events(**kwargs)
        for event in page.get("events", []):
            seen += 1
            message = event.get("message", "")
            # 필터 패턴을 지원하지 않는 환경(모킹 등)도 있어 여기서 다시 가른다
            for kind, marks in (("timeout", TIMEOUT_MARKS), ("memory", MEMORY_MARKS), ("denied", DENIED_MARKS)):
                if any(mark in message for mark in marks):
                    counts[kind] += 1
                    sample.setdefault(kind, message.strip()[:160])
        if not page.get("nextToken"):
            break
        kwargs["nextToken"] = page["nextToken"]
    return {"counts": counts, "sample": sample}


def _lambda(d: Diagnosis, clients: Clients) -> List[str]:
    lam, cloudwatch, logs, ec2 = clients("lambda"), clients("cloudwatch"), clients("logs"), clients("ec2")
    try:
        config = lam.get_function_configuration(FunctionName=d.resource)
    except ClientError as error:
        if _code(error) == "ResourceNotFoundException":
            raise ValueError(f"함수가 없습니다: {d.resource}")
        raise
    name = config["FunctionName"]
    timeout_ms = int(config.get("Timeout", 3)) * 1000
    values = _metrics(cloudwatch, "AWS/Lambda", {"FunctionName": name}, [
        ("invocations", "Invocations", "Sum"),
        ("errors", "Errors", "Sum"),
        ("throttles", "Throttles", "Sum"),
        ("duration", "Duration", "Maximum"),
        ("concurrency", "ConcurrentExecutions", "Maximum"),
        ("iterator_age", "IteratorAge", "Maximum"),
        ("dropped", "AsyncEventsDropped", "Sum"),
    ], d.start, d.now)
    try:
        marks = _log_marks(logs, name, d.start, d.now)
        log_error = None
    except Exception as error:
        marks, log_error = {"counts": {"timeout": 0, "memory": 0, "denied": 0}, "sample": {}}, _code(error)
    counts = marks["counts"]
    timed_out = counts["timeout"] > 0 or (values["duration"] is not None and values["duration"] >= timeout_ms * NEAR_TIMEOUT)

    # L1
    d.check("L1", "AWS Health 이벤트", UNKNOWN, HEALTH_NOTE, "health:DescribeEvents")

    # L3: VPC 안의 함수는 NAT가 없으면 인터넷·AWS API(엔드포인트 없는 것)에 닿지 못해 시간 초과로 끝난다
    vpc = config.get("VpcConfig") or {}
    vpc_blamed = False
    if vpc.get("SubnetIds"):
        no_egress = []
        for subnet_id in vpc["SubnetIds"]:
            route = _default_route(_route_table(ec2, subnet_id, vpc.get("VpcId", "")))
            if not any(route.get(k) for k in ("NatGatewayId", "TransitGatewayId", "NetworkInterfaceId", "InstanceId")):
                no_egress.append(subnet_id)
        if no_egress and timed_out:
            vpc_blamed = True
            d.check("L3", "VPC 밖으로 나가는 경로", CAUSE,
                    f"VPC 안에서 도는데 서브넷 {', '.join(no_egress)}에 NAT 경로가 없어 인터넷·AWS API 호출이 시간 초과됩니다 "
                    "(인터넷 게이트웨이 경로는 Lambda에 쓸모가 없습니다. NAT나 VPC 엔드포인트가 필요)",
                    "lambda:GetFunctionConfiguration VpcConfig · ec2:DescribeRouteTables")
        elif no_egress:
            d.check("L3", "VPC 밖으로 나가는 경로", WARN,
                    f"서브넷 {', '.join(no_egress)}에 NAT 경로가 없습니다 (VPC 엔드포인트가 없는 서비스·인터넷은 호출할 수 없음)",
                    "ec2:DescribeRouteTables")
        else:
            d.check("L3", "VPC 경로", OK, "VPC 서브넷에 NAT 경로가 있습니다", "ec2:DescribeRouteTables")
    else:
        d.check("L3", "VPC", OK, "VPC 밖에서 실행되어 네트워크 설정의 영향을 받지 않습니다", "lambda:GetFunctionConfiguration")

    # L4
    d.skip("L4", "API Gateway·함수 URL 앞단은 이 절차가 보지 않습니다. API Gateway 뒤라면 함수의 스로틀이 클라이언트에 500으로 보입니다")

    # L5 컴퓨팅 (함수 실행)
    if config.get("State") in ("Failed", "Inactive"):
        d.check("L5", "함수 상태", CAUSE, f"함수 상태가 {config.get('State')}입니다 ({config.get('StateReason', '')})",
                "lambda:GetFunctionConfiguration State")
    if config.get("LastUpdateStatus") == "Failed":
        d.check("L5", "마지막 업데이트", CAUSE, f"마지막 배포가 실패했습니다 ({config.get('LastUpdateStatusReason', '')})",
                "lambda:GetFunctionConfiguration LastUpdateStatus")
    if timed_out:
        detail = (f"시간 초과 로그 {counts['timeout']}건" if counts["timeout"] else "") + (
            f", 실행 시간 최대 {values['duration'] / 1000:.1f}초" if values["duration"] is not None else "")
        d.check("L5", "시간 초과", SYMPTOM if vpc_blamed else CAUSE,
                f"제한 {timeout_ms // 1000}초에 걸려 끝났습니다 ({detail.strip(', ')})"
                + ("" if vpc_blamed else " → 느린 의존성 호출이나 제한 시간·메모리 설정을 확인하세요"),
                "AWS/Lambda Duration Maximum · 로그 'Task timed out'")
    if counts["memory"]:
        d.check("L5", "메모리 부족", CAUSE,
                f"메모리 부족으로 끝난 실행 {counts['memory']}건 (설정 {config.get('MemorySize')}MB)",
                "logs:FilterLogEvents Runtime.OutOfMemory")
    if _count(values["errors"]):
        d.check("L5", "함수 오류", SYMPTOM,
                f"오류 {_count(values['errors'])}건 / 호출 {_count(values['invocations'])}건", "AWS/Lambda Errors")
    if log_error:
        d.check("L5", "함수 로그", UNKNOWN, f"함수 로그를 읽지 못했습니다 ({log_error})", "logs:FilterLogEvents")
    if d.status_of("L5") in (SKIP, UNKNOWN):
        d.check("L5", "함수 실행", OK,
                f"호출 {_count(values['invocations'])}건에 오류·시간 초과가 없습니다", "AWS/Lambda Invocations·Errors·Duration")

    # L6 권한·한도: 예약 동시성, 스로틀, 권한 거부
    try:
        reserved = lam.get_function_concurrency(FunctionName=name).get("ReservedConcurrentExecutions")
    except Exception:
        reserved = None
    if reserved == 0:
        d.check("L6", "예약 동시성", CAUSE, "예약 동시성이 0이라 모든 호출이 스로틀됩니다 (함수를 끈 것과 같습니다)",
                "lambda:GetFunctionConcurrency")
    elif _count(values["throttles"]):
        at_limit = reserved is not None and values["concurrency"] is not None and values["concurrency"] >= reserved
        d.check("L6", "스로틀", CAUSE if at_limit else SYMPTOM,
                f"스로틀 {_count(values['throttles'])}건"
                + (f": 예약 동시성 {reserved}에 닿았습니다" if at_limit else " (계정 동시성 한도나 다른 함수의 사용량을 확인)"),
                "AWS/Lambda Throttles · ConcurrentExecutions")
    if counts["denied"]:
        d.check("L6", "권한 거부", CAUSE,
                f"권한 거부 로그 {counts['denied']}건: 실행 역할에 권한이 없습니다 — {marks['sample'].get('denied', '')}",
                "logs:FilterLogEvents AccessDenied")
    if d.status_of("L6") == SKIP:
        d.check("L6", "동시성·권한", OK,
                "스로틀과 권한 거부가 없습니다" + (f" (예약 동시성 {reserved})" if reserved else ""),
                "lambda:GetFunctionConcurrency · AWS/Lambda Throttles · 로그")

    # L7 데이터·의존성: 이벤트 소스 매핑, 스트림 지연, 비동기 이벤트 버림
    try:
        mappings = lam.list_event_source_mappings(FunctionName=name)["EventSourceMappings"]
    except Exception:
        mappings = []
    for mapping in mappings:
        source = mapping.get("EventSourceArn", "").split(":")[-1]
        result = mapping.get("LastProcessingResult", "")
        if mapping.get("State") in ("Disabled", "Disabling") and mapping.get("StateTransitionReason") != "USER_INITIATED":
            d.check("L7", "이벤트 소스", CAUSE, f"{source} 매핑이 {mapping.get('State')} ({mapping.get('StateTransitionReason')})",
                    "lambda:ListEventSourceMappings")
        elif result.startswith("PROBLEM"):
            d.check("L7", "이벤트 소스", CAUSE, f"{source} 매핑의 마지막 처리 결과: {result}", "lambda:ListEventSourceMappings")
    if values["iterator_age"] is not None and values["iterator_age"] >= ITERATOR_AGE_MS:
        d.check("L7", "스트림 처리 지연", SYMPTOM, f"IteratorAge 최대 {values['iterator_age'] / 1000:.0f}초",
                "AWS/Lambda IteratorAge")
    if _count(values["dropped"]):
        d.check("L7", "버린 비동기 이벤트", SYMPTOM, f"재시도 끝에 버린 비동기 이벤트 {_count(values['dropped'])}건 (DLQ·실패 대상 확인)",
                "AWS/Lambda AsyncEventsDropped")
    if d.status_of("L7") == SKIP:
        d.check("L7", "이벤트 소스·비동기", OK,
                f"이벤트 소스 매핑 {len(mappings)}개에 문제가 없습니다" if mappings else "이벤트 소스 매핑이 없고 버린 이벤트도 없습니다",
                "lambda:ListEventSourceMappings · AWS/Lambda IteratorAge")
    role_name = config.get("Role", "").split("/")[-1]
    return [name, config.get("FunctionArn", ""), role_name]


# ---------------------------------------------------------------- S3
PUBLIC_GRANTEES = ("http://acs.amazonaws.com/groups/global/AllUsers",
                   "http://acs.amazonaws.com/groups/global/AuthenticatedUsers")


def _everyone(principal: Any) -> bool:
    return principal == "*" or (isinstance(principal, dict) and "*" in (
        principal.get("AWS") if isinstance(principal.get("AWS"), list) else [principal.get("AWS")]))


def policy_findings(policy: Dict[str, Any]) -> Dict[str, List[str]]:
    """버킷 정책에서 조건 없이 모든 사람에게 허용·거부하는 문장을 찾는다 (GetBucketPolicyStatus를 보완)."""
    found: Dict[str, List[str]] = {"public": [], "deny_all": []}
    statements = policy.get("Statement", [])
    for statement in statements if isinstance(statements, list) else [statements]:
        if not _everyone(statement.get("Principal")) or statement.get("Condition"):
            continue
        actions = statement.get("Action", [])
        actions = actions if isinstance(actions, list) else [actions]
        label = statement.get("Sid") or ", ".join(actions)
        if statement.get("Effect") == "Allow":
            found["public"].append(label)
        elif statement.get("Effect") == "Deny" and any(a in ("s3:*", "*", "s3:GetObject") for a in actions):
            found["deny_all"].append(label)
    return found


def _s3(d: Diagnosis, clients: Clients) -> List[str]:
    s3 = clients("s3")
    try:
        response = s3.head_bucket(Bucket=d.resource)
        region = response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("x-amz-bucket-region") or clients.region
    except ClientError as error:
        headers = error.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        if _code(error) in ("404", "NoSuchBucket"):
            raise ValueError(f"버킷이 없습니다: {d.resource}")
        if "x-amz-bucket-region" not in headers:
            raise
        region = headers["x-amz-bucket-region"]
    s3 = clients("s3", region)

    d.check("L1", "AWS Health 이벤트", UNKNOWN, HEALTH_NOTE, "health:DescribeEvents")
    d.skip("L3", "VPC 엔드포인트 정책은 이 절차가 보지 않습니다 (VPC 안에서만 403이면 엔드포인트 정책을 확인)")
    d.skip("L4", "S3 앞단(CloudFront 등)은 이 절차가 보지 않습니다")
    d.skip("L5", "S3는 관리형 저장소라 컴퓨팅 층이 없습니다")

    # L6 권한·한도: 계정·버킷 퍼블릭 액세스 차단, 정책, ACL, 요청 오류
    account_block: Dict[str, bool] = {}
    try:
        account = clients("sts").get_caller_identity()["Account"]
        account_block = clients("s3control", region).get_public_access_block(
            AccountId=account)["PublicAccessBlockConfiguration"]
    except Exception:
        account_block = {}
    try:
        bucket_block = s3.get_public_access_block(Bucket=d.resource)["PublicAccessBlockConfiguration"]
    except ClientError as error:
        if _code(error) != "NoSuchPublicAccessBlockConfiguration":
            raise
        bucket_block = {}

    def blocked(key: str) -> bool:
        return bool(account_block.get(key) or bucket_block.get(key))

    try:
        policy = json.loads(s3.get_bucket_policy(Bucket=d.resource)["Policy"])
    except ClientError as error:
        if _code(error) != "NoSuchBucketPolicy":
            raise
        policy = {}
    found = policy_findings(policy)
    try:
        public_status = bool(s3.get_bucket_policy_status(Bucket=d.resource).get("PolicyStatus", {}).get("IsPublic"))
    except ClientError:
        public_status = False
    if found["public"] or public_status:
        what = ", ".join(found["public"]) or "정책 상태 IsPublic"
        if blocked("RestrictPublicBuckets"):
            d.check("L6", "버킷 정책", WARN,
                    f"정책이 모든 사람에게 허용하지만({what}) 퍼블릭 액세스 차단(RestrictPublicBuckets)이 막고 있습니다",
                    "s3:GetBucketPolicy · GetBucketPolicyStatus · GetPublicAccessBlock")
        else:
            d.check("L6", "버킷 정책", CAUSE, f"버킷 정책이 조건 없이 모든 사람에게 허용합니다: {what} → 공개 노출",
                    "s3:GetBucketPolicy · GetBucketPolicyStatus")
    if found["deny_all"]:
        d.check("L6", "버킷 정책", CAUSE,
                f"조건 없이 모든 주체를 거부하는 문장이 있습니다: {', '.join(found['deny_all'])} → 모든 요청이 403",
                "s3:GetBucketPolicy")
    grants = [f"{g['Grantee'].get('URI', '').split('/')[-1]} {g.get('Permission')}"
              for g in s3.get_bucket_acl(Bucket=d.resource).get("Grants", [])
              if g.get("Grantee", {}).get("URI") in PUBLIC_GRANTEES]
    if grants:
        if blocked("IgnorePublicAcls"):
            d.check("L6", "버킷 ACL", WARN, f"ACL이 공개({', '.join(grants)})지만 IgnorePublicAcls가 무시합니다",
                    "s3:GetBucketAcl · GetPublicAccessBlock")
        else:
            d.check("L6", "버킷 ACL", CAUSE, f"ACL이 모든 사람에게 열려 있습니다: {', '.join(grants)} → 공개 노출",
                    "s3:GetBucketAcl")
    off = [key for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
           if not blocked(key)]
    if off and d.status_of("L6") != CAUSE:
        d.check("L6", "퍼블릭 액세스 차단", WARN, f"계정·버킷 어느 쪽에서도 켜지 않은 항목: {', '.join(off)}",
                "s3control:GetPublicAccessBlock · s3:GetPublicAccessBlock")
    cloudwatch = clients("cloudwatch", region)
    errors = _metrics(cloudwatch, "AWS/S3", {"BucketName": d.resource, "FilterId": "EntireBucket"},
                      [("4xx", "4xxErrors", "Sum"), ("5xx", "5xxErrors", "Sum")], d.start, d.now)
    if errors["4xx"] is None and errors["5xx"] is None:
        d.check("L6", "요청 오류", UNKNOWN, "요청 지표가 없습니다 (버킷에 요청 지표를 켜야 403·503을 셀 수 있습니다)",
                "AWS/S3 4xxErrors · 5xxErrors (FilterId EntireBucket)")
    else:
        if _count(errors["4xx"]):
            d.check("L6", "4xx 오류", SYMPTOM, f"4xx 오류 {_count(errors['4xx'])}건 (403이면 정책·ACL·KMS 키 정책·엔드포인트 정책)",
                    "AWS/S3 4xxErrors")
        if _count(errors["5xx"]):
            d.check("L6", "5xx 오류", SYMPTOM, f"5xx 오류 {_count(errors['5xx'])}건 (503 SlowDown이면 접두사당 요청률 한도)",
                    "AWS/S3 5xxErrors")
    if d.status_of("L6") in (SKIP, UNKNOWN):
        d.check("L6", "공개·권한", OK, "퍼블릭 액세스 차단이 모두 켜져 있고 공개 정책·ACL이 없습니다",
                "s3:GetPublicAccessBlock · GetBucketPolicy · GetBucketAcl")

    # L7 데이터: 되돌릴 수 있나, 암호화
    versioning = s3.get_bucket_versioning(Bucket=d.resource).get("Status", "Disabled")
    if versioning != "Enabled":
        d.check("L7", "버전 관리", WARN, "버전 관리가 꺼져 있어 지우거나 덮어쓴 객체를 되돌릴 수 없습니다",
                "s3:GetBucketVersioning")
    else:
        d.check("L7", "버전 관리", OK, "버전 관리가 켜져 있습니다", "s3:GetBucketVersioning")
    return [d.resource, f"arn:aws:s3:::{d.resource}"]


# ---------------------------------------------------------------- 진입점
SERVICES: Dict[str, Dict[str, Any]] = {
    "alb": {"title": "Application Load Balancer", "run": _alb, "components": {
        "L1": "리전·AZ · 대상 EC2 호스트", "L2": "CloudTrail 쓰기 이벤트", "L3": "서브넷 경로 · 보안 그룹 · NACL",
        "L4": "ALB 리스너 · 대상 그룹 · 헬스체크", "L5": "대상 EC2", "L6": "연결 한도",
        "L7": "대상 뒤 DB·외부 API"}},
    "ec2": {"title": "EC2", "run": _ec2, "components": {
        "L1": "호스트 하드웨어 · 예정 이벤트", "L2": "CloudTrail 쓰기 이벤트", "L3": "보안 그룹 · NACL · 경로",
        "L4": "로드 밸런서 대상 그룹", "L5": "인스턴스 · OS · CPU 크레딧", "L6": "Auto Scaling 시작",
        "L7": "애플리케이션 의존성"}},
    "lambda": {"title": "Lambda", "run": _lambda, "components": {
        "L1": "Lambda 서비스", "L2": "CloudTrail 쓰기 이벤트 (배포·설정)", "L3": "VPC 서브넷 경로",
        "L4": "API Gateway · 함수 URL", "L5": "함수 실행 (오류·시간 초과·메모리)", "L6": "동시성 · 실행 역할 권한",
        "L7": "이벤트 소스 · 비동기 전달"}},
    "s3": {"title": "S3", "run": _s3, "components": {
        "L1": "S3 서비스", "L2": "CloudTrail 쓰기 이벤트 (정책·ACL·차단)", "L3": "VPC 엔드포인트",
        "L4": "앞단 (CloudFront 등)", "L5": "관리형 (컴퓨팅 없음)", "L6": "퍼블릭 액세스 차단 · 정책 · ACL · 요청 한도",
        "L7": "버전 관리 (복구 가능성)"}},
}
ALIASES = {"elb": "alb", "elbv2": "alb", "loadbalancer": "alb", "instance": "ec2", "function": "lambda", "bucket": "s3"}


def run(service: str, resource: str, hours: Optional[int] = None, region: str = "",
        clients: Optional[Clients] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """진단 한 번. 잘못된 입력(모르는 서비스, 없는 자원)은 ValueError."""
    key = ALIASES.get((service or "").strip().lower(), (service or "").strip().lower())
    if key not in SERVICES:
        raise ValueError(f"진단할 수 있는 서비스는 {', '.join(SERVICES)}입니다: {service}")
    if not (resource or "").strip():
        raise ValueError("resource(ALB 이름·인스턴스 ID·함수 이름·버킷 이름)가 필요합니다")
    hours = max(1, min(int(hours or DEFAULT_HOURS), MAX_HOURS))
    clients = clients or Clients(region)
    d = Diagnosis(key, resource.strip(), hours, now or datetime.now(timezone.utc))
    spec = SERVICES[key]
    related = spec["run"](d, clients)
    _check_changes(d, clients, related)  # 다른 층이 모두 나온 뒤 (모듈 설명의 L2)
    return d.result(spec["components"], spec["title"])

