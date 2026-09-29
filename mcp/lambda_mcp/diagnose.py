"""서비스별 진단 절차 (런북을 코드로 옮긴 것): 자원 하나를 정해진 차례로 확인하고 층마다 판정을 낸다.

    diagnoseService(service, resource, hours, target)   (app.py의 도구)
        └─▶ run() ──▶ 서비스별 절차 (_alb · _ec2 · _lambda · _s3 · _rds · _vpc · _credential · _cost)
                         ├─ 조회 API·지표·로그로 항목(check)을 하나씩 확인한다. 항목 = 층 · 이름 · 판정 · 찾은 것 · 근거(API·지표 이름)
                         └─ 층마다 항목을 모아 층의 판정을 정하고, 원인 층과 요약을 만든다

왜 절차로 두나
- 모델에게 "원인을 찾아 줘"라고만 맡기면 매번 다른 도구를 다른 차례로 부르고, 무엇을 확인했는지 남지 않는다.
- 현업 런북처럼 층을 고정하고(아래 L1~L7) 층마다 무엇을 볼지 코드로 정해 두면, 같은 장애에는 같은 판정이 나오고
  판정마다 근거가 붙는다. 모델은 이 판정을 읽고 설명·다음 조치를 쓴다 (판정을 바꾸지 않는다).
- tests/test_diagnose.py가 moto로 장애를 심고(대상 중지, 보안 그룹 차단, 예약 동시성 0, 버킷 공개 …) 원인 층을 맞히는지 채점한다.

진단 층 (요청이 지나가는 길을 층으로 고정했다. 서비스마다 층이 가리키는 부품이 다르다: SERVICES의 components)
    L1 AWS              AWS 쪽 장애인가 (하드웨어 상태 검사·예정 이벤트. Health API는 Business Support 이상이라 사람이 확인)
    L2 리소스 변경 기록  직전에 누가 어떤 리소스를 바꿨나 (CloudTrail 쓰기 이벤트 중 이 자원과 관련된 것)
    L3 네트워크 경로     요청이 목적지까지 가나 (서브넷 경로, 보안 그룹, NACL)
    L4 로드 밸런서·게이트웨이  앞단이 뒤에 닿나 (리스너, 대상 그룹, 대상 헬스, ELB가 만든 5xx)
    L5 인스턴스·실행 환경  실행이 되나 (인스턴스 상태·상태 검사·CPU, Lambda 오류·시간 초과)
    L6 권한·한도        막혔나 (동시성, 권한 거부, 공개 설정, 연결 한도)
    L7 데이터·의존성    뒤가 느리거나 죽었나 (대상 응답 시간, 이벤트 소스, 데이터 보호)

판정 (층의 판정 = 그 층 항목 중 가장 무거운 것. 무거운 차례: 원인 > 증상 > 주의 > 정상 > 확인 불가 > 해당 없음)
    cause    원인      이 층에서 장애를 설명하는 것을 찾았다 (예: 대상 인스턴스가 모두 멈춤)
    symptom  증상      이상이 보이지만 다른 층의 결과다 (예: 대상이 모두 unhealthy → 원인은 인스턴스나 네트워크 경로)
    warn     주의      지금 장애의 원인은 아니지만 위험한 설정 (예: 버전 관리 꺼짐)
    ok       정상
    unknown  확인 불가  볼 수 없었다 (권한 없음, 지표 없음, API가 지원 플랜에 묶임)
    skip     해당 없음  이 서비스에는 없는 층 (예: S3의 인스턴스·실행 환경)
- L2 리소스 변경 기록은 다른 층에 원인·증상이 있을 때만 원인(계기)이 된다. 장애가 없으면 변경이 있어도 정상이다.
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
    ("L1", "AWS"),
    ("L2", "리소스 변경 기록"),
    ("L3", "네트워크 경로"),
    ("L4", "로드 밸런서·게이트웨이"),
    ("L5", "인스턴스·실행 환경"),
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

    def __init__(self, service: str, resource: str, hours: int, now: datetime, target: str = ""):
        self.service = service
        self.resource = resource
        self.target = target  # VPC 연결 진단의 목적지 (IP:포트, 인스턴스 ID:포트)
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
        """다른 층에 원인이나 증상이 있나 (L2 리소스 변경 기록을 계기로 볼지 정할 때)."""
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
        return {"service": self.service, "service_name": title, "resource": self.resource,
                **({"target": self.target} if self.target else {}), "hours": self.hours,
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
            if not parts and not symptoms:  # 변경 자체가 사고다 (자격 증명: 기록 끄기 등)
                return f"원인 — L2 {LAYER_NAMES['L2']}: {by_id['L2']['finding']}"
            if not parts:  # 고장 난 곳은 못 찾았지만 증상 직전에 변경이 있었다 → 그 변경이 가장 유력하다
                return (f"원인 — 증상 직전의 변경(L2): {by_id['L2']['finding']} / 증상 — "
                        + " / ".join(f"{layer['id']} {layer['name']}: {layer['finding']}" for layer in symptoms))
            parts.append(f"계기 L2 {LAYER_NAMES['L2']}: {by_id['L2']['finding']}")
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
    """CloudTrail의 최근 쓰기 이벤트 중 이 자원과 관련된 것 (L2 리소스 변경 기록). 조회만 (LookupEvents, 최근 90일, 무료).
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
    """L2 리소스 변경 기록. 다른 층의 판정이 모두 나온 뒤에 부른다 (장애가 있을 때만 변경을 계기로 본다)."""
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
             from_networks: Iterable[Any] = (), egress: bool = False) -> List[str]:
    """보안 그룹들이 이 TCP 포트를 연 상대 목록 (egress=False면 들어오는 출처, True면 나가는 목적지).
    from_groups·from_networks를 주면 그 상대와 겹치는 규칙만 센다. 접두사 목록은 내용을 모르니 연 것으로 본다."""
    from_groups, from_networks = set(from_groups), list(from_networks)
    sources: List[str] = []
    for group in groups:
        for rule in group.get("IpPermissionsEgress" if egress else "IpPermissions", []):
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


def find_instance(ec2, reference: str) -> Dict[str, Any]:
    """인스턴스 ID, Name 태그, 사설 IP로 인스턴스 하나를 찾는다. 없거나 여럿이면 ValueError."""
    if reference.startswith("i-"):
        try:
            found = list(_instances(ec2, [reference]).values())
        except ClientError as error:
            if _code(error).startswith("InvalidInstanceID"):
                raise ValueError(f"인스턴스가 없습니다: {reference}")
            raise
    else:
        try:
            ipaddress.ip_address(reference)
            name, value = "private-ip-address", reference
        except ValueError:
            name, value = "tag:Name", reference
        found = [i for page in ec2.get_paginator("describe_instances").paginate(
                    Filters=[{"Name": name, "Values": [value]}])
                 for r in page.get("Reservations", []) for i in r.get("Instances", [])
                 if i.get("State", {}).get("Name") != "terminated"]
    if len(found) > 1:
        raise ValueError(f"{reference}에 맞는 인스턴스가 {len(found)}대입니다. 인스턴스 ID로 지정하세요: "
                         + ", ".join(i["InstanceId"] for i in found[:5]))
    if not found:
        raise ValueError(f"인스턴스가 없습니다: {reference}")
    return found[0]


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

    # L1 AWS
    _check_hardware(d, ec2, instance_ids)

    # L3 네트워크 경로: 서브넷 경로, ALB 보안 그룹, 대상 보안 그룹, NACL
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

    # L5 인스턴스·실행 환경 (대상 EC2)
    if instances:
        _check_instances(d, ec2, cloudwatch, instances, network_blamed=bool(network_blamed_targets),
                         health_reasons=health_reasons)
    elif targets:
        d.check("L5", "대상", UNKNOWN, "대상이 EC2 인스턴스가 아닙니다 (IP·Lambda 대상은 이 절차가 보지 않습니다)",
                "elbv2:DescribeTargetHealth")
    else:
        d.skip("L5", "등록된 대상이 없어 볼 인스턴스가 없습니다")
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
    instance = find_instance(ec2, d.resource)
    instances = {instance["InstanceId"]: instance}
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

    # L5 인스턴스·실행 환경
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

    # L5 인스턴스·실행 환경 (함수 실행)
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
    d.skip("L5", "S3는 관리형 저장소라 인스턴스·실행 환경 층이 없습니다")

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


# ---------------------------------------------------------------- RDS
DB_PORTS = {"mysql": 3306, "mariadb": 3306, "aurora-mysql": 3306, "postgres": 5432, "aurora-postgresql": 5432,
            "oracle-ee": 1521, "oracle-se2": 1521, "sqlserver-ee": 1433, "sqlserver-se": 1433, "sqlserver-ex": 1433,
            "sqlserver-web": 1433}
FREE_STORAGE_RATIO = 0.1  # 여유 저장 공간이 할당의 10% 아래면 한도에 닿은 것으로 본다
FREE_MEMORY_BYTES = 100 * 1024 * 1024  # 여유 메모리 최소가 100MB 아래면 스왑·연결 거부 증상
REPLICA_LAG_SECONDS = 60
DISK_LATENCY_SECONDS = 0.1  # 읽기·쓰기 지연 100ms
# RDS 상태 → 판정과 뜻 (DescribeDBInstances DBInstanceStatus)
DB_STATUS = {
    "stopped": (CAUSE, "인스턴스가 멈춰 있습니다"),
    "stopping": (CAUSE, "인스턴스가 멈추는 중입니다"),
    "failed": (CAUSE, "인스턴스가 복구할 수 없는 상태입니다 (AWS 지원 또는 스냅샷 복원)"),
    "incompatible-parameters": (CAUSE, "파라미터 그룹 값이 맞지 않아 시작하지 못합니다 (메모리 관련 값부터 확인)"),
    "incompatible-network": (CAUSE, "서브넷·IP가 부족하거나 서브넷 그룹이 바뀌어 네트워크를 붙이지 못합니다"),
    "inaccessible-encryption-credentials": (CAUSE, "KMS 키를 쓸 수 없어 암호화된 저장소를 열지 못합니다"),
    "restore-error": (CAUSE, "복원 중 오류가 났습니다"),
    "storage-full": (CAUSE, "저장 공간이 가득 찼습니다"),
    "rebooting": (SYMPTOM, "재시작 중입니다"),
    "modifying": (SYMPTOM, "설정을 바꾸는 중입니다"),
    "upgrading": (SYMPTOM, "엔진을 올리는 중입니다"),
    "maintenance": (SYMPTOM, "유지 관리 중입니다"),
    "starting": (SYMPTOM, "시작하는 중입니다"),
    "storage-optimization": (OK, "저장소 최적화 중입니다 (서비스는 됩니다)"),
    "backing-up": (OK, "백업 중입니다 (서비스는 됩니다)"),
}


def _rds(d: Diagnosis, clients: Clients) -> List[str]:
    rds, ec2, cloudwatch = clients("rds"), clients("ec2"), clients("cloudwatch")
    try:
        db = rds.describe_db_instances(DBInstanceIdentifier=d.resource)["DBInstances"][0]
    except ClientError as error:
        if _code(error) == "DBInstanceNotFound":
            raise ValueError(f"DB 인스턴스가 없습니다: {d.resource}")
        raise
    ident = db["DBInstanceIdentifier"]
    status = db.get("DBInstanceStatus", "")
    port = (db.get("Endpoint") or {}).get("Port") or DB_PORTS.get(db.get("Engine", ""), 3306)
    group_ids = [g["VpcSecurityGroupId"] for g in db.get("VpcSecurityGroups", []) if g.get("VpcSecurityGroupId")]
    values = _metrics(cloudwatch, "AWS/RDS", {"DBInstanceIdentifier": ident}, [
        ("cpu", "CPUUtilization", "Maximum"),
        ("memory", "FreeableMemory", "Minimum"),
        ("storage", "FreeStorageSpace", "Minimum"),
        ("connections", "DatabaseConnections", "Maximum"),
        ("burst", "BurstBalance", "Minimum"),
        ("credits", "CPUCreditBalance", "Minimum"),
        ("lag", "ReplicaLag", "Maximum"),
        ("read_latency", "ReadLatency", "Maximum"),
        ("write_latency", "WriteLatency", "Maximum"),
    ], d.start, d.now)

    # L1: RDS가 알린 장애·장애 조치·유지 관리 (DescribeEvents, 최근 14일까지 무료로 조회)
    try:
        events = rds.describe_events(SourceIdentifier=ident, SourceType="db-instance",
                                     StartTime=d.start, EndTime=d.now)["Events"]
        for event in events:
            categories = set(event.get("EventCategories", []))
            message = f"{_clock(event.get('Date'))} {event.get('Message', '')}"
            if "failure" in categories:
                d.check("L1", "RDS 이벤트 (장애)", CAUSE, f"RDS가 장애를 알렸습니다: {message}", "rds:DescribeEvents failure")
            elif categories & {"failover", "recovery", "maintenance"}:
                d.check("L1", "RDS 이벤트", SYMPTOM,
                        f"{'·'.join(sorted(categories))}: {message} (이 동안 연결이 끊기고 DNS가 새 인스턴스를 가리킵니다)",
                        "rds:DescribeEvents")
        if d.status_of("L1") == SKIP:
            d.check("L1", "RDS 이벤트", OK, "보는 시간 동안 장애·장애 조치·유지 관리 이벤트가 없습니다", "rds:DescribeEvents")
    except Exception as error:
        d.check("L1", "RDS 이벤트", UNKNOWN, f"RDS 이벤트를 조회하지 못했습니다 ({_code(error)})", "rds:DescribeEvents")
    d.check("L1", "AWS Health 이벤트", UNKNOWN, HEALTH_NOTE, "health:DescribeEvents")

    # L3: 보안 그룹이 DB 포트를 여나, 서브넷 NACL이 막지 않나
    groups = _groups(ec2, group_ids)
    sources = sg_opens(groups, port)
    if not sources:
        d.check("L3", "DB 보안 그룹", CAUSE,
                f"DB 보안 그룹이 포트 {port}을(를) 어디에도 열지 않아 애플리케이션이 연결하지 못합니다 (연결 시간 초과로 보입니다)",
                "rds:DescribeDBInstances VpcSecurityGroups · ec2:DescribeSecurityGroups")
    elif db.get("PubliclyAccessible") and any(s in ("0.0.0.0/0", "::/0") for s in sources):
        d.check("L3", "DB 보안 그룹", WARN, f"퍼블릭 접근이 켜져 있고 포트 {port}이(가) 인터넷 전체에 열려 있습니다",
                "ec2:DescribeSecurityGroups")
    vpc_id = (db.get("DBSubnetGroup") or {}).get("VpcId", "")
    subnet_ids = [s["SubnetIdentifier"] for s in (db.get("DBSubnetGroup") or {}).get("Subnets", [])][:3]
    if sources and subnet_ids:
        cidrs = [s for s in sources if "/" in s and ":" not in s and s != "0.0.0.0/0"]
        if any(s.startswith("sg-") for s in sources) and vpc_id:  # 보안 그룹 출처는 VPC 안에서 온다
            cidrs += [v["CidrBlock"] for v in ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"]]
        for subnet_id in subnet_ids:
            acl = _network_acl(ec2, subnet_id, vpc_id)
            for cidr in cidrs[:3]:
                inbound, rule = nacl_decision(acl, False, port, _first_host(cidr))
                outbound, rule_out = nacl_decision(acl, True, 40000, _first_host(cidr))
                if not inbound or not outbound:
                    d.check("L3", "DB 서브넷 NACL", CAUSE,
                            f"DB 서브넷 {subnet_id}의 NACL이 {cidr}와의 통신을 막습니다 "
                            + (f"(들어오는 {port}, 규칙 {rule or '*'})" if not inbound
                               else f"(나가는 임시 포트, 규칙 {rule_out or '*'})"), "ec2:DescribeNetworkAcls")
                    break
    if d.status_of("L3") == SKIP:
        d.check("L3", "보안 그룹·NACL", OK, f"포트 {port}을(를) {', '.join(sources[:3])}에 열었고 NACL이 막지 않습니다",
                "ec2:DescribeSecurityGroups · DescribeNetworkAcls")

    d.skip("L4", "RDS Proxy 같은 앞단은 이 절차가 보지 않습니다")

    # L5: 인스턴스 상태, CPU, 메모리, 버스트 크레딧
    verdict = DB_STATUS.get(status)
    if verdict and status != "storage-full":
        d.check("L5", "DB 상태", verdict[0], f"상태 {status}: {verdict[1]}", "rds:DescribeDBInstances DBInstanceStatus")
    if values["cpu"] is not None and values["cpu"] >= CPU_HIGH:
        d.check("L5", "CPU 사용률", SYMPTOM, f"CPU가 최대 {values['cpu']:.0f}%까지 올랐습니다 (느린 쿼리·잠금 대기를 확인)",
                "AWS/RDS CPUUtilization Maximum")
    if values["memory"] is not None and values["memory"] < FREE_MEMORY_BYTES:
        d.check("L5", "여유 메모리", SYMPTOM, f"여유 메모리가 최소 {values['memory'] / 1024 / 1024:.0f}MB까지 떨어졌습니다 (스왑·연결 거부)",
                "AWS/RDS FreeableMemory Minimum")
    if values["burst"] is not None and values["burst"] < 5:
        d.check("L5", "EBS 버스트 크레딧", CAUSE,
                f"gp2 버스트 크레딧이 {values['burst']:.0f}%까지 떨어져 IOPS가 기준 성능으로 묶였습니다 (gp3로 바꾸거나 용량 증설)",
                "AWS/RDS BurstBalance Minimum")
    if db.get("DBInstanceClass", "").startswith(("db.t2.", "db.t3.", "db.t4g.")) and values["credits"] is not None \
            and values["credits"] < 1:
        d.check("L5", "CPU 크레딧", CAUSE, f"CPU 크레딧이 바닥나(최소 {values['credits']:.1f}) 기준 성능으로 묶였습니다",
                "AWS/RDS CPUCreditBalance Minimum")
    if d.status_of("L5") == SKIP:
        d.check("L5", "DB 인스턴스", OK,
                f"상태 {status}" + (f", CPU 최대 {values['cpu']:.0f}%" if values["cpu"] is not None else ""),
                "rds:DescribeDBInstances · AWS/RDS CPUUtilization")

    # L6: 저장 공간 한도, 연결 수
    allocated = int(db.get("AllocatedStorage") or 0) * 1024 ** 3
    if status == "storage-full" or (values["storage"] is not None and allocated
                                    and values["storage"] < allocated * FREE_STORAGE_RATIO):
        free = f"{values['storage'] / 1024 ** 3:.1f}GiB" if values["storage"] is not None else "0"
        autoscale = db.get("MaxAllocatedStorage", 0) > db.get("AllocatedStorage", 0)
        d.check("L6", "저장 공간", CAUSE,
                f"여유 저장 공간 {free} / 할당 {db.get('AllocatedStorage')}GiB — 가득 차면 쓰기가 멈춥니다"
                + (" (저장 공간 자동 확장이 켜져 있지만 6시간에 한 번만 늘어납니다)" if autoscale else " (자동 확장이 꺼져 있습니다)"),
                "AWS/RDS FreeStorageSpace Minimum · rds:DescribeDBInstances AllocatedStorage")
    else:
        d.check("L6", "저장 공간", OK,
                f"여유 저장 공간 최소 {values['storage'] / 1024 ** 3:.1f}GiB / 할당 {db.get('AllocatedStorage')}GiB"
                if values["storage"] is not None else "저장 공간 지표가 없습니다", "AWS/RDS FreeStorageSpace")
    if values["connections"] is not None:
        d.check("L6", "연결 수", OK,
                f"연결 수 최대 {values['connections']:.0f} (max_connections는 클래스 메모리 공식이라 비교하지 않습니다. "
                "'Too many connections'가 보이면 파라미터 그룹과 연결 풀을 확인)", "AWS/RDS DatabaseConnections Maximum")

    # L7: 복제 지연, 디스크 지연, 백업·Multi-AZ (복구할 수 있나)
    if values["lag"] is not None and values["lag"] >= REPLICA_LAG_SECONDS:
        d.check("L7", "복제 지연", SYMPTOM, f"복제 지연이 최대 {values['lag']:.0f}초입니다 (읽기 복제본의 데이터가 늦음)",
                "AWS/RDS ReplicaLag Maximum")
    latency = max(values["read_latency"] or 0, values["write_latency"] or 0)
    if latency >= DISK_LATENCY_SECONDS:
        d.check("L7", "디스크 지연", SYMPTOM, f"디스크 읽기·쓰기 지연이 최대 {latency * 1000:.0f}ms입니다 (IOPS 한도·버스트 크레딧)",
                "AWS/RDS ReadLatency · WriteLatency Maximum")
    if not db.get("BackupRetentionPeriod"):
        d.check("L7", "자동 백업", WARN, "자동 백업이 꺼져 있어 특정 시점으로 복구할 수 없습니다", "rds:DescribeDBInstances BackupRetentionPeriod")
    if not db.get("MultiAZ"):
        d.check("L7", "Multi-AZ", WARN, "단일 AZ라 AZ나 호스트 장애 때 넘겨받을 대기 인스턴스가 없습니다", "rds:DescribeDBInstances MultiAZ")
    if d.status_of("L7") == SKIP:
        d.check("L7", "복제·디스크·백업", OK, "복제·디스크 지연이 없고 백업과 Multi-AZ가 켜져 있습니다",
                "AWS/RDS ReplicaLag · ReadLatency · rds:DescribeDBInstances")
    return [ident, db.get("DBInstanceArn", ""), *group_ids,
            *[g.get("DBParameterGroupName", "") for g in db.get("DBParameterGroups", [])]]


# ---------------------------------------------------------------- VPC 연결 (출발지 → 목적지:포트)
EPHEMERAL_PORT = 40000  # 응답이 돌아오는 임시 포트의 대표 값 (1024-65535)
NAT_METRICS = [("port_errors", "ErrorPortAllocation", "Sum"), ("drops", "PacketsDropCount", "Sum")]


def _parse_target(target: str) -> Tuple[str, int]:
    host, _, port = (target or "").strip().rpartition(":")
    if not host or not port.isdigit():
        raise ValueError("vpc 진단에는 target이 필요합니다: 목적지 IP:포트나 인스턴스 ID:포트 (예: 10.0.2.15:5432)")
    return host, int(port)


def _best_route(table: Dict[str, Any], address: str) -> Dict[str, Any]:
    """주소에 맞는 경로 중 접두사가 가장 긴 것 (라우팅 테이블이 고르는 것과 같다)."""
    ip = ipaddress.ip_address(address)
    matches = [(ipaddress.ip_network(r["DestinationCidrBlock"]).prefixlen, r) for r in table.get("Routes", [])
               if r.get("DestinationCidrBlock") and ip in ipaddress.ip_network(r["DestinationCidrBlock"])]
    return max(matches, key=lambda m: m[0])[1] if matches else {}


def _vpc(d: Diagnosis, clients: Clients) -> List[str]:
    ec2, cloudwatch = clients("ec2"), clients("cloudwatch")
    host, port = _parse_target(d.target)
    source = find_instance(ec2, d.resource)
    destination: Optional[Dict[str, Any]] = None
    if host.startswith("i-"):
        destination = find_instance(ec2, host)
        address = destination.get("PrivateIpAddress", "")
    else:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise ValueError(f"목적지는 IP나 인스턴스 ID로 주세요 (DNS 이름은 이 절차가 풀지 않습니다): {host}")
        address = host
        try:
            destination = find_instance(ec2, host) if ipaddress.ip_address(host).is_private else None
        except ValueError:
            destination = None  # VPC 밖(다른 VPC·온프레미스)의 사설 IP
    source_ip = source.get("PrivateIpAddress", "")
    vpc_id = source.get("VpcId", "")
    vpc_cidrs = [ipaddress.ip_network(a["CidrBlock"])
                 for v in ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"] for a in v.get("CidrBlockAssociationSet", [])
                 if a.get("CidrBlockState", {}).get("State", "associated") == "associated"] if vpc_id else []
    inside = any(ipaddress.ip_address(address) in cidr for cidr in vpc_cidrs)
    ends = [source] + ([destination] if destination else [])

    _check_hardware(d, ec2, [i["InstanceId"] for i in ends])

    # L5: 양쪽 인스턴스가 실행 중인가
    for role, instance in (("출발지", source), ("목적지", destination)):
        if instance and instance.get("State", {}).get("Name") != "running":
            reason = instance.get("StateReason", {}).get("Message") or instance.get("StateTransitionReason") or ""
            d.check("L5", f"{role} 인스턴스", CAUSE,
                    f"{role} {_label(instance)}이(가) {instance.get('State', {}).get('Name')} 상태입니다"
                    + (f" ({reason})" if reason else ""), "ec2:DescribeInstances State")
    if d.status_of("L5") == SKIP:
        d.check("L5", "양쪽 인스턴스", OK, "출발지" + ("와 목적지" if destination else "") + " 인스턴스가 실행 중입니다",
                "ec2:DescribeInstances")

    # L3: 보안 그룹 (나가는 규칙 → 들어오는 규칙), NACL (서브넷 경계를 넘을 때만), 경로
    source_groups = _groups(ec2, [g["GroupId"] for g in source.get("SecurityGroups", [])])
    destination_group_ids = [g["GroupId"] for g in (destination or {}).get("SecurityGroups", [])]
    if not sg_opens(source_groups, port, from_groups=destination_group_ids,
                    from_networks=[ipaddress.ip_network(f"{address}/32")], egress=True):
        d.check("L3", "출발지 보안 그룹 (나가는 규칙)", CAUSE,
                f"{_label(source)}의 보안 그룹이 {address}:{port}로 나가는 것을 허용하지 않습니다", "ec2:DescribeSecurityGroups")
    if destination:
        if not sg_opens(_groups(ec2, destination_group_ids), port,
                        from_groups=[g["GroupId"] for g in source_groups],
                        from_networks=[ipaddress.ip_network(f"{source_ip}/32")]):
            d.check("L3", "목적지 보안 그룹 (들어오는 규칙)", CAUSE,
                    f"{_label(destination)}의 보안 그룹이 {source_ip}(또는 출발지 보안 그룹)에서 오는 {port} 포트를 허용하지 않습니다",
                    "ec2:DescribeSecurityGroups")
    source_subnet = source.get("SubnetId", "")
    destination_subnet = (destination or {}).get("SubnetId", "")
    if source_subnet and source_subnet != destination_subnet:  # 같은 서브넷 안의 통신은 NACL을 지나지 않는다
        acl = _network_acl(ec2, source_subnet, vpc_id)
        out_ok, out_rule = nacl_decision(acl, True, port, address)
        back_ok, back_rule = nacl_decision(acl, False, EPHEMERAL_PORT, address)
        if not out_ok or not back_ok:
            d.check("L3", "출발지 서브넷 NACL", CAUSE,
                    f"출발지 서브넷 {source_subnet}의 NACL이 "
                    + (f"나가는 {port} 포트를 막습니다 (규칙 {out_rule or '*'})" if not out_ok
                       else f"돌아오는 임시 포트(1024-65535)를 막습니다 (규칙 {back_rule or '*'}). NACL은 상태를 기억하지 않아 응답 방향도 열어야 합니다"),
                    "ec2:DescribeNetworkAcls")
        if destination_subnet:
            acl = _network_acl(ec2, destination_subnet, destination.get("VpcId", vpc_id))
            in_ok, in_rule = nacl_decision(acl, False, port, source_ip)
            reply_ok, reply_rule = nacl_decision(acl, True, EPHEMERAL_PORT, source_ip)
            if not in_ok or not reply_ok:
                d.check("L3", "목적지 서브넷 NACL", CAUSE,
                        f"목적지 서브넷 {destination_subnet}의 NACL이 "
                        + (f"들어오는 {port} 포트를 막습니다 (규칙 {in_rule or '*'})" if not in_ok
                           else f"응답이 나가는 임시 포트(1024-65535)를 막습니다 (규칙 {reply_rule or '*'}). NACL은 상태를 기억하지 않습니다"),
                        "ec2:DescribeNetworkAcls")
    route: Dict[str, Any] = {}
    if not inside and source_subnet:
        table = _route_table(ec2, source_subnet, vpc_id)
        route = _best_route(table, address)
        target_id = next((route.get(k) for k in ("NatGatewayId", "GatewayId", "TransitGatewayId",
                                                 "VpcPeeringConnectionId", "NetworkInterfaceId", "InstanceId")
                          if route.get(k)), "")
        if not route:
            d.check("L3", "경로", CAUSE, f"출발지 서브넷 {source_subnet}의 라우팅 테이블에 {address}로 가는 경로가 없습니다",
                    "ec2:DescribeRouteTables")
        elif route.get("State") == "blackhole":
            d.check("L3", "경로", CAUSE,
                    f"{route.get('DestinationCidrBlock')} 경로가 blackhole입니다 (가리키던 {target_id or 'NAT·피어링'}이 지워졌습니다)",
                    "ec2:DescribeRouteTables State")
        elif str(target_id).startswith("igw-") and not source.get("PublicIpAddress"):
            d.check("L3", "경로", CAUSE,
                    f"인터넷 게이트웨이로 나가는 경로지만 {_label(source)}에 공인 IP가 없어 응답이 돌아오지 않습니다 (NAT 게이트웨이가 필요)",
                    "ec2:DescribeRouteTables · DescribeInstances PublicIpAddress")
        elif str(target_id).startswith(("pcx-", "tgw-")):
            d.check("L3", "경로", WARN,
                    f"{target_id}로 나갑니다. 상대 VPC의 돌아오는 경로와 보안 그룹은 이 절차가 보지 않습니다",
                    "ec2:DescribeRouteTables")
    try:
        flow_logs = ec2.describe_flow_logs(Filters=[{"Name": "resource-id", "Values": [vpc_id]}])["FlowLogs"]
    except Exception:
        flow_logs = []
    if d.status_of("L3") == SKIP:
        d.check("L3", "보안 그룹·NACL·경로", OK,
                "양쪽 보안 그룹, NACL(응답 방향 포함), 경로가 이 연결을 허용합니다"
                + ("" if inside else f" (경로 {route.get('DestinationCidrBlock', '')})"),
                "ec2:DescribeSecurityGroups · DescribeNetworkAcls · DescribeRouteTables")
    d.check("L3", "흐름 로그", OK if flow_logs else UNKNOWN,
            "흐름 로그가 있어 실제 ACCEPT·REJECT를 확인할 수 있습니다 (get_vpc_flow_logs)" if flow_logs
            else "흐름 로그가 없어 실제로 거부됐는지는 확인할 수 없습니다 (설정 평가만 했습니다)", "ec2:DescribeFlowLogs")

    d.skip("L4", "로드 밸런서를 거치지 않는 인스턴스 사이의 직접 연결을 봅니다")

    # L6: NAT 게이트웨이 상태와 포트 할당 한도 (같은 목적지로 동시에 55,000개)
    nat_id = route.get("NatGatewayId", "")
    if nat_id:
        nat = ec2.describe_nat_gateways(NatGatewayIds=[nat_id])["NatGateways"][0]
        values = _metrics(cloudwatch, "AWS/NATGateway", {"NatGatewayId": nat_id}, NAT_METRICS, d.start, d.now)
        if nat.get("State") != "available":
            d.check("L6", "NAT 게이트웨이", CAUSE, f"{nat_id} 상태가 {nat.get('State')}입니다 ({nat.get('FailureMessage', '')})",
                    "ec2:DescribeNatGateways")
        if _count(values["port_errors"]):
            d.check("L6", "NAT 포트 할당", CAUSE,
                    f"{nat_id}가 포트를 할당하지 못한 횟수 {_count(values['port_errors'])}건 "
                    "(같은 목적지 IP·포트로 동시 연결 55,000개 한도. 연결 재사용·NAT 추가·목적지 분산)",
                    "AWS/NATGateway ErrorPortAllocation")
        if _count(values["drops"]):
            d.check("L6", "NAT 패킷 버림", SYMPTOM, f"{nat_id}가 버린 패킷 {_count(values['drops'])}개", "AWS/NATGateway PacketsDropCount")
        if d.status_of("L6") == SKIP:
            d.check("L6", "NAT 게이트웨이", OK, f"{nat_id}가 사용 가능하고 포트 할당 오류가 없습니다",
                    "ec2:DescribeNatGateways · AWS/NATGateway ErrorPortAllocation")
    else:
        d.skip("L6", "NAT 게이트웨이를 거치지 않습니다")

    d.skip("L7", "DNS 해석은 흐름 로그에 남지 않습니다. 이름으로 연결이 안 되면 Route 53 Resolver 쿼리 로그와 VPC의 DNS 설정을 확인하세요")
    related = [i["InstanceId"] for i in ends] + [g["GroupId"] for g in source_groups] + destination_group_ids
    related += [source_subnet, destination_subnet, nat_id, vpc_id]
    return related


# ---------------------------------------------------------------- 자격 증명 유출 (액세스 키 하나 또는 IAM 사용자)
PERSISTENCE_EVENTS = {"CreateUser", "CreateAccessKey", "CreateLoginProfile", "UpdateLoginProfile", "AttachUserPolicy",
                      "PutUserPolicy", "AttachRolePolicy", "PutRolePolicy", "CreateRole", "UpdateAssumeRolePolicy",
                      "AddUserToGroup", "AttachGroupPolicy", "CreatePolicyVersion", "SetDefaultPolicyVersion",
                      "DeactivateMFADevice", "CreateVirtualMFADevice"}
COMPUTE_EVENTS = {"RunInstances", "RequestSpotInstances", "RequestSpotFleet", "CreateFleet", "CreateFunction20150331",
                  "CreateComputeEnvironment", "CreateCluster", "CreateService", "RunTask"}
DATA_EVENTS = {"GetSecretValue", "GetParameter", "GetParameters", "GetParametersByPath", "PutBucketPolicy",
               "PutBucketAcl", "DeleteBucketPolicy", "DeletePublicAccessBlock", "DeleteBucketPublicAccessBlock",
               "ModifySnapshotAttribute", "ModifyDBSnapshotAttribute", "ModifyDBClusterSnapshotAttribute",
               "CopySnapshot", "CreateDBSnapshot", "DeleteBucket"}
EVASION_EVENTS = {"StopLogging", "DeleteTrail", "UpdateTrail", "PutEventSelectors", "DeleteFlowLogs",
                  "DeleteDetector", "DisableSecurityHub", "DeleteConfigurationRecorder", "StopConfigurationRecorder"}
GLOBAL_SOURCES = ("iam.", "sts.", "signin.", "organizations.", "cloudfront.", "route53.", "support.", "health.")
DENIED_RECON = 3  # 권한 거부가 이 이상이면 권한을 더듬어 본 흔적 (Splunk BOTS v3의 유출 키: 11분 동안 서비스 4곳에 거부 4건)
MANY_IPS = 3  # 키 하나를 이 이상의 IP에서 쓰면 증상 (사람 한 명·CI 하나는 보통 한두 곳)
KEY_AGE_DAYS = 90


def key_activity(clients: Clients, access_key_id: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """이 액세스 키가 부른 API (CloudTrail LookupEvents AccessKeyId, 관리 이벤트, 읽기·쓰기 모두).
    tests/test_diagnose.py가 이 함수를 가짜로 바꿔 공격 흔적을 심는다 (moto는 LookupEvents를 지원하지 않는다)."""
    trail = clients("cloudtrail")
    kwargs: Dict[str, Any] = {"LookupAttributes": [{"AttributeKey": "AccessKeyId", "AttributeValue": access_key_id}],
                              "StartTime": start, "EndTime": end, "MaxResults": 50}
    found: List[Dict[str, Any]] = []
    for _ in range(TRAIL_PAGES):
        page = trail.lookup_events(**kwargs)
        for event in page.get("Events", []):
            try:
                raw = json.loads(event.get("CloudTrailEvent") or "{}")
            except ValueError:
                raw = {}
            found.append({"time": _clock(event.get("EventTime")), "event": event.get("EventName", ""),
                          "source": raw.get("eventSource", event.get("EventSource", "")),
                          "ip": raw.get("sourceIPAddress", ""), "agent": str(raw.get("userAgent", ""))[:60],
                          "region": raw.get("awsRegion", ""), "error": raw.get("errorCode", ""),
                          "read_only": bool(raw.get("readOnly", event.get("ReadOnly") == "true"))})
        if not page.get("NextToken"):
            break
        kwargs["NextToken"] = page["NextToken"]
    return found


def _names(events: List[Dict[str, Any]], limit: int = 4) -> str:
    counts: Dict[str, int] = {}
    for event in events:
        counts[event["event"]] = counts.get(event["event"], 0) + 1
    ordered = sorted(counts.items(), key=lambda item: -item[1])
    return ", ".join(f"{name} {n}건" for name, n in ordered[:limit]) + (f" 외 {len(ordered) - limit}종" if len(ordered) > limit else "")


def _is_admin(iam, user: str) -> bool:
    """사용자에게 붙은 정책에 관리자 권한(AdministratorAccess 또는 모든 작업·모든 자원 허용)이 있나."""
    for policy in iam.list_attached_user_policies(UserName=user).get("AttachedPolicies", []):
        if policy["PolicyName"] == "AdministratorAccess":
            return True
        if ":aws:policy/" in policy["PolicyArn"]:
            continue
        version = iam.get_policy(PolicyArn=policy["PolicyArn"])["Policy"]["DefaultVersionId"]
        document = iam.get_policy_version(PolicyArn=policy["PolicyArn"], VersionId=version)["PolicyVersion"]["Document"]
        document = json.loads(document) if isinstance(document, str) else document
        statements = document.get("Statement", [])
        for statement in statements if isinstance(statements, list) else [statements]:
            actions = statement.get("Action", [])
            resources = statement.get("Resource", [])
            if statement.get("Effect") == "Allow" and "*" in (actions if isinstance(actions, list) else [actions]) \
                    and "*" in (resources if isinstance(resources, list) else [resources]):
                return True
    return False


def _credential(d: Diagnosis, clients: Clients) -> None:
    iam = clients("iam")
    reference = d.resource
    if reference.startswith(("AKIA", "ASIA")):
        try:
            user = iam.get_access_key_last_used(AccessKeyId=reference).get("UserName")
        except ClientError as error:
            raise ValueError(f"이 계정의 액세스 키가 아닙니다: {reference} ({_code(error)})")
        if not user:
            raise ValueError(f"키의 사용자를 찾지 못했습니다: {reference}")
        keys = [k for k in iam.list_access_keys(UserName=user)["AccessKeyMetadata"] if k["AccessKeyId"] == reference]
    else:
        user = reference
        try:
            keys = iam.list_access_keys(UserName=user)["AccessKeyMetadata"]
        except ClientError as error:
            if _code(error) == "NoSuchEntity":
                raise ValueError(f"IAM 사용자가 없습니다: {user}")
            raise
    events: List[Dict[str, Any]] = []
    lookup_error = None
    for key in keys[:2]:
        try:
            events += key_activity(clients, key["AccessKeyId"], d.start, d.now)
        except Exception as error:
            lookup_error = _code(error)
    ok = [e for e in events if not e["error"]]
    writes = [e for e in ok if not e["read_only"]]
    denied = [e for e in events if e["error"] in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                                                  "Client.UnauthorizedOperation")]

    d.skip("L1", "AWS 쪽 장애가 아니라 자격 증명 사고입니다")

    # L2: 이 키가 바꾼 것 (쓰기 이벤트). 기록을 끄려 한 흔적은 따로
    if lookup_error and not events:
        d.check("L2", "키가 부른 API", UNKNOWN, f"CloudTrail을 조회하지 못했습니다 ({lookup_error})", "cloudtrail:LookupEvents AccessKeyId")
    else:
        evasion = [e for e in ok if e["event"] in EVASION_EVENTS]
        if evasion:
            d.check("L2", "기록 끄기", CAUSE, f"감사 기록·탐지를 끄려 했습니다: {_names(evasion)}", "cloudtrail:LookupEvents AccessKeyId")
        if writes:
            d.check("L2", "키가 바꾼 것", CAUSE,
                    f"최근 {d.hours}시간 동안 쓰기 {len(writes)}건: {_names(writes)} (처음 {writes[-1]['time']}, 마지막 {writes[0]['time']})",
                    "cloudtrail:LookupEvents AccessKeyId")
        else:
            d.check("L2", "키가 바꾼 것", OK, f"최근 {d.hours}시간 동안 성공한 쓰기가 없습니다 (호출 {len(events)}건)",
                    "cloudtrail:LookupEvents AccessKeyId")

    # L3: 어디서 썼나 (IP·도구·리전)
    ips = sorted({e["ip"] for e in events if e["ip"]})
    regions = sorted({e["region"] for e in events if e["region"] and not e["source"].startswith(GLOBAL_SOURCES)
                      and e["region"] != clients.region})
    agents = sorted({e["agent"].split(" ")[0] for e in events if e["agent"]})
    if regions:
        d.check("L3", "쓴 리전", SYMPTOM,
                f"배포 리전({clients.region}) 밖 {len(regions)}곳에서 호출했습니다: {', '.join(regions[:5])} (채굴은 여러 리전에 퍼뜨리는 경우가 많습니다)",
                "cloudtrail awsRegion")
    if len(ips) >= MANY_IPS:
        d.check("L3", "쓴 IP", SYMPTOM, f"IP {len(ips)}곳에서 썼습니다: {', '.join(ips[:4])} …", "cloudtrail sourceIPAddress")
    if events and d.status_of("L3") == SKIP:
        d.check("L3", "쓴 곳", OK, f"IP {', '.join(ips[:3]) or '-'} · 도구 {', '.join(agents[:3]) or '-'}", "cloudtrail sourceIPAddress·userAgent")
    elif not events:
        d.check("L3", "쓴 곳", OK if not lookup_error else UNKNOWN, "보는 시간 동안 이 키로 부른 API가 없습니다", "cloudtrail:LookupEvents")

    d.skip("L4", "앞단이 없는 API 호출입니다")

    # L5: 인스턴스·함수 남용 (채굴의 흔한 흔적) + GuardDuty
    compute = [e for e in ok if e["event"] in COMPUTE_EVENTS]
    if compute:
        where = sorted({e["region"] for e in compute if e["region"]})
        d.check("L5", "인스턴스·함수 생성", CAUSE,
                f"{_names(compute)} ({', '.join(where[:4])}) — 채굴용 인스턴스를 띄우는 흔한 흔적입니다. 모든 리전을 확인하세요",
                "cloudtrail:LookupEvents RunInstances")
    try:
        guardduty = clients("guardduty")
        detectors = guardduty.list_detectors().get("DetectorIds", [])
        finding_ids = []
        for detector in detectors[:1]:
            finding_ids = guardduty.list_findings(DetectorId=detector, FindingCriteria={"Criterion": {
                "resource.accessKeyDetails.accessKeyId": {"Eq": [k["AccessKeyId"] for k in keys[:2]]}}},
                MaxResults=20).get("FindingIds", [])
            if finding_ids:
                types = {f["Type"] for f in guardduty.get_findings(DetectorId=detector, FindingIds=finding_ids)["Findings"]}
                d.check("L5" if any("CryptoCurrency" in t for t in types) else "L6", "GuardDuty 탐지", CAUSE,
                        f"GuardDuty가 이 키에 대해 {len(finding_ids)}건을 탐지했습니다: {', '.join(sorted(types)[:3])}",
                        "guardduty:ListFindings accessKeyId")
        if not detectors:
            d.check("L5", "GuardDuty", UNKNOWN, "GuardDuty가 꺼져 있어 채굴·비정상 호출 탐지를 볼 수 없습니다", "guardduty:ListDetectors")
    except Exception as error:
        d.check("L5", "GuardDuty", UNKNOWN, f"GuardDuty를 조회하지 못했습니다 ({_code(error)})", "guardduty:ListFindings")
    if d.status_of("L5") in (SKIP, UNKNOWN):
        d.check("L5", "인스턴스·함수 생성", OK, "인스턴스·함수·컨테이너를 만든 흔적이 없습니다", "cloudtrail:LookupEvents")

    # L6: 권한 — 지속성 확보, 권한 더듬기, 키 상태·권한 범위
    persistence = [e for e in ok if e["event"] in PERSISTENCE_EVENTS]
    if persistence:
        d.check("L6", "지속성 확보", CAUSE,
                f"다른 자격 증명·권한을 만들었습니다: {_names(persistence)} → 새로 생긴 사용자·키·정책도 함께 막아야 합니다",
                "cloudtrail:LookupEvents (IAM 쓰기)")
    if len(denied) >= DENIED_RECON:
        d.check("L6", "거부된 시도", SYMPTOM, f"권한 거부 {len(denied)}건: {_names(denied)} (권한을 더듬어 본 흔적)",
                "cloudtrail errorCode AccessDenied")
    try:
        admin = _is_admin(iam, user)
    except Exception:
        admin = False
    if admin:
        d.check("L6", "권한 범위", WARN, f"{user}에게 관리자 권한이 있어 피해 범위가 계정 전체입니다", "iam:ListAttachedUserPolicies")
    for key in keys:
        age = (d.now - key["CreateDate"]).days if isinstance(key.get("CreateDate"), datetime) else None
        if key.get("Status") == "Active":
            d.check("L6", "키 상태", WARN if (writes or persistence or compute) else OK,
                    f"{key['AccessKeyId'][:8]}… 활성" + (f", {age}일 전에 만듦" if age is not None else "")
                    + (" → 지우지 말고 먼저 비활성화하세요 (지우면 CloudTrail 조사가 어려워집니다)" if writes or persistence or compute else ""),
                    "iam:ListAccessKeys")
        else:
            d.check("L6", "키 상태", OK, f"{key['AccessKeyId'][:8]}… 이미 비활성화되어 있습니다", "iam:ListAccessKeys")
        if age is not None and age > KEY_AGE_DAYS and key.get("Status") == "Active":
            d.check("L6", "키 나이", WARN, f"{age}일 동안 바꾸지 않은 키입니다 (90일마다 교체 권장)", "iam:ListAccessKeys CreateDate")

    # L7: 데이터 접근·노출 (관리 이벤트로 보이는 것만. S3 객체 읽기는 데이터 이벤트라 보이지 않는다)
    data = [e for e in ok if e["event"] in DATA_EVENTS]
    if data:
        d.check("L7", "데이터 접근", CAUSE, f"비밀 값 조회·데이터 공개 시도: {_names(data)}", "cloudtrail:LookupEvents")
    else:
        d.check("L7", "데이터 접근", OK, "비밀 값 조회·버킷 공개·스냅샷 공유 흔적이 없습니다", "cloudtrail:LookupEvents")
    d.check("L7", "S3 객체 읽기", UNKNOWN,
            "S3 GetObject는 데이터 이벤트라 관리 이벤트 조회로 보이지 않습니다 (데이터 이벤트 추적·S3 서버 액세스 로그로 확인)",
            "cloudtrail 데이터 이벤트")
    return None


# ---------------------------------------------------------------- 비용 급증 (계정 또는 서비스 하나)
RECENT_DAYS = 3  # 최근 며칠 (오늘은 반영이 늦어 뺀다)
BASELINE_DAYS = 14  # 비교할 앞 기간
SPIKE_RATIO = 1.5  # 하루 평균이 1.5배 이상이고
SPIKE_MIN_USD = 5.0  # 하루 5달러 이상 늘었으면 급증
COST_EVENTS = {"RunInstances", "CreateNatGateway", "CreateDBInstance", "ModifyDBInstance", "CreateDBCluster",
               "CreateCluster", "CreateService", "UpdateService", "CreateFunction20150331", "PutProvisionedConcurrencyConfig",
               "CreateVolume", "ModifyVolume", "AllocateAddress", "CreateVpcEndpoint", "CreateTransitGatewayVpcAttachment",
               "PutBucketReplication", "PutRetentionPolicy", "PutSubscriptionFilter", "CreateLoadBalancer",
               "StartInstances", "ModifyInstanceAttribute", "CreateAutoScalingGroup", "UpdateAutoScalingGroup",
               "CreateEndpoint", "CreateNotebookInstance", "PutMetricStream"}
COMPUTE_SERVICES = ("Amazon Elastic Compute Cloud - Compute", "AWS Lambda", "Amazon Elastic Container Service",
                    "Amazon Relational Database Service", "Amazon SageMaker", "Amazon Bedrock", "Amazon EC2 Container Registry (ECR)")
DATA_SERVICES = ("Amazon Simple Storage Service", "AmazonCloudWatch", "Amazon DynamoDB", "AWS Backup",
                 "Amazon Elastic File System", "AWS CloudTrail")
EDGE_SERVICES = ("Amazon Elastic Load Balancing", "Amazon API Gateway", "Amazon CloudFront")
COST_LAYER_NAMES = {"L3": "데이터 전송·NAT", "L4": "앞단 (로드 밸런서·API Gateway·CloudFront)",
                    "L5": "인스턴스·실행 환경", "L7": "저장·로그·요청"}


def cost_layer(service: str, usage: str) -> str:
    """비용 항목 하나가 어느 층의 돈인가 (사용 유형 이름의 조각으로 가른다)."""
    if any(k in usage for k in ("DataTransfer", "NatGateway", "VpcEndpoint", "TransitGateway", "-Out-Bytes",
                                "-In-Bytes", "PublicIPv4")):
        return "L3"
    if service in EDGE_SERVICES or any(k in usage for k in ("LoadBalancerUsage", "LCUUsage")):
        return "L4"
    if service in DATA_SERVICES or any(k in usage for k in ("TimedStorage", "EBS:", "VolumeUsage", "SnapshotUsage",
                                                            "DataProcessing-Bytes", "Requests-Tier", "ReadRequestUnits",
                                                            "WriteRequestUnits", "TimedBackup", "Storage")):
        return "L7"
    return "L5"


def account_changes(clients: Clients, start: datetime, end: datetime) -> List[Dict[str, str]]:
    """계정 전체의 쓰기 이벤트 중 비용을 늘리는 것 (COST_EVENTS). 조회만 (LookupEvents ReadOnly=false).
    tests/test_diagnose.py가 가짜로 바꾼다."""
    trail = clients("cloudtrail")
    kwargs: Dict[str, Any] = {"LookupAttributes": [{"AttributeKey": "ReadOnly", "AttributeValue": "false"}],
                              "StartTime": start, "EndTime": end, "MaxResults": 50}
    found: List[Dict[str, str]] = []
    for _ in range(TRAIL_PAGES):
        page = trail.lookup_events(**kwargs)
        found += [{"time": _clock(e.get("EventTime")), "event": e.get("EventName", ""), "user": e.get("Username", ""),
                   "resource": next((r.get("ResourceName", "") for r in e.get("Resources", [])), "")}
                  for e in page.get("Events", []) if e.get("EventName") in COST_EVENTS]
        if not page.get("NextToken") or len(found) >= CHANGE_LIMIT:
            break
        kwargs["NextToken"] = page["NextToken"]
    return found[:CHANGE_LIMIT]


def _daily_costs(ce, start: str, end: str) -> List[Tuple[str, str, str, float]]:
    """(날짜, 서비스, 사용 유형, 금액 USD). GetCostAndUsage 한 번 (요청마다 0.01달러), 쪽이 넘으면 이어서."""
    rows: List[Tuple[str, str, str, float]] = []
    kwargs: Dict[str, Any] = {"TimePeriod": {"Start": start, "End": end}, "Granularity": "DAILY",
                              "Metrics": ["UnblendedCost"],
                              "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}]}
    for _ in range(5):
        page = ce.get_cost_and_usage(**kwargs)
        for day in page.get("ResultsByTime", []):
            for group in day.get("Groups", []):
                service, usage = (group.get("Keys") + ["", ""])[:2]
                rows.append((day["TimePeriod"]["Start"], service, usage,
                             float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", 0) or 0)))
        if not page.get("NextPageToken"):
            break
        kwargs["NextPageToken"] = page["NextPageToken"]
    return rows


def _cost(d: Diagnosis, clients: Clients) -> None:
    today = d.now.date()
    recent_start = today - timedelta(days=RECENT_DAYS)
    base_start = recent_start - timedelta(days=BASELINE_DAYS)
    rows = _daily_costs(clients("ce"), base_start.isoformat(), today.isoformat())
    wanted = d.resource.strip().lower()
    if wanted not in ("account", "all", "전체", "계정"):
        rows = [r for r in rows if wanted in r[1].lower()]
        if not rows:
            raise ValueError(f"최근 {RECENT_DAYS + BASELINE_DAYS}일 비용에 '{d.resource}'가 든 서비스가 없습니다 (계정 전체는 account)")
    totals: Dict[Tuple[str, str], List[float]] = {}
    for day, service, usage, amount in rows:
        entry = totals.setdefault((service, usage), [0.0, 0.0])
        entry[0 if day >= recent_start.isoformat() else 1] += amount
    spikes: Dict[str, List[Tuple[float, str]]] = {}
    for (service, usage), (recent, base) in totals.items():
        recent_avg, base_avg = recent / RECENT_DAYS, base / BASELINE_DAYS
        delta = recent_avg - base_avg
        if delta >= SPIKE_MIN_USD and (base_avg == 0 or recent_avg / base_avg >= SPIKE_RATIO):
            spikes.setdefault(cost_layer(service, usage), []).append(
                (delta, f"{service} · {usage} 하루 평균 ${base_avg:,.2f} → ${recent_avg:,.2f} (+${delta:,.2f})"))
    recent_total = sum(v[0] for v in totals.values()) / RECENT_DAYS
    base_total = sum(v[1] for v in totals.values()) / BASELINE_DAYS

    d.skip("L1", "AWS 쪽 가격 변경·청구 오류는 이 절차가 보지 않습니다 (Billing 콘솔·지원 문의)")
    evidence = f"ce:GetCostAndUsage DAILY SERVICE·USAGE_TYPE (최근 {RECENT_DAYS}일 vs 그 전 {BASELINE_DAYS}일 평균)"
    for layer in ("L3", "L4", "L5", "L7"):
        found = sorted(spikes.get(layer, []), reverse=True)
        if found:
            d.check(layer, f"{COST_LAYER_NAMES[layer]} 비용", CAUSE,
                    " / ".join(text for _, text in found[:3]) + (f" 외 {len(found) - 3}건" if len(found) > 3 else ""), evidence)
        else:
            d.check(layer, f"{COST_LAYER_NAMES[layer]} 비용", OK, "크게 늘어난 항목이 없습니다", evidence)

    # L6: 예산과 이상 탐지 감시
    try:
        account = clients("sts").get_caller_identity()["Account"]
        budgets = clients("budgets").describe_budgets(AccountId=account).get("Budgets", [])
        over = [b for b in budgets if float(b.get("CalculatedSpend", {}).get("ActualSpend", {}).get("Amount", 0) or 0)
                > float(b.get("BudgetLimit", {}).get("Amount", 0) or 0) > 0]
        forecast_over = [b for b in budgets
                         if float(b.get("CalculatedSpend", {}).get("ForecastedSpend", {}).get("Amount", 0) or 0)
                         > float(b.get("BudgetLimit", {}).get("Amount", 0) or 0) > 0 and b not in over]
        if over:
            d.check("L6", "예산", SYMPTOM, f"예산을 넘었습니다: {', '.join(b['BudgetName'] for b in over[:3])}", "budgets:DescribeBudgets")
        if forecast_over:
            d.check("L6", "예산 예측", WARN, f"이달 예측이 예산을 넘습니다: {', '.join(b['BudgetName'] for b in forecast_over[:3])}",
                    "budgets:DescribeBudgets ForecastedSpend")
        if not budgets:
            d.check("L6", "예산", WARN, "예산이 없어 급증을 알림으로 먼저 알 수 없습니다", "budgets:DescribeBudgets")
        elif not over and not forecast_over:
            d.check("L6", "예산", OK, f"예산 {len(budgets)}개 모두 한도 안입니다", "budgets:DescribeBudgets")
    except Exception as error:
        d.check("L6", "예산", UNKNOWN, f"예산을 조회하지 못했습니다 ({_code(error)})", "budgets:DescribeBudgets")
    try:
        monitors = clients("ce").get_anomaly_monitors().get("AnomalyMonitors", [])
        d.check("L6", "이상 탐지", OK if monitors else WARN,
                f"Cost Anomaly Detection 감시 {len(monitors)}개 (최대 하루 늦게 알립니다)" if monitors
                else "Cost Anomaly Detection 감시가 없습니다", "ce:GetAnomalyMonitors")
    except Exception as error:
        d.check("L6", "이상 탐지", UNKNOWN, f"이상 탐지 감시를 조회하지 못했습니다 ({_code(error)})", "ce:GetAnomalyMonitors")

    # L2: 비용을 늘리는 변경 (급증이 있을 때만 계기)
    try:
        changes = account_changes(clients, datetime.combine(recent_start, datetime.min.time(), timezone.utc)
                                  - timedelta(days=1), d.now)
        listed = ", ".join(f"{c['time']} {c['event']} ({c['user'] or '?'} → {c['resource'] or '-'})" for c in changes[:3])
        if changes and spikes:
            d.check("L2", "비용을 늘리는 변경", CAUSE, f"급증 직전·동안의 변경 {len(changes)}건: {listed}", "cloudtrail:LookupEvents ReadOnly=false")
        else:
            d.check("L2", "비용을 늘리는 변경", OK,
                    f"변경 {len(changes)}건이 있었지만 급증이 없습니다: {listed}" if changes else "비용을 늘리는 변경이 없습니다",
                    "cloudtrail:LookupEvents ReadOnly=false")
    except Exception as error:
        d.check("L2", "비용을 늘리는 변경", UNKNOWN, f"CloudTrail을 조회하지 못했습니다 ({_code(error)})", "cloudtrail:LookupEvents")
    d.check("L6", "하루 평균", OK if recent_total < base_total * SPIKE_RATIO or recent_total - base_total < SPIKE_MIN_USD else SYMPTOM,
            f"{'계정 전체' if wanted in ('account', 'all', '전체', '계정') else d.resource} 하루 평균 ${base_total:,.2f} → ${recent_total:,.2f} "
            "(Cost Explorer는 최대 하루 늦게 반영되어 오늘은 뺐습니다)", evidence)
    return None


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
        "L4": "앞단 (CloudFront 등)", "L5": "관리형 (인스턴스 없음)", "L6": "퍼블릭 액세스 차단 · 정책 · ACL · 요청 한도",
        "L7": "버전 관리 (복구 가능성)"}},
    "rds": {"title": "RDS", "run": _rds, "components": {
        "L1": "RDS 이벤트 (장애·장애 조치·유지 관리)", "L2": "CloudTrail 쓰기 이벤트 (DB·파라미터·보안 그룹)",
        "L3": "DB 보안 그룹 · 서브넷 NACL", "L4": "RDS Proxy (앞단)", "L5": "DB 인스턴스 · CPU · 메모리 · 버스트 크레딧",
        "L6": "저장 공간 · 연결 수", "L7": "복제 지연 · 디스크 지연 · 백업 · Multi-AZ"}},
    "vpc": {"title": "VPC 연결", "run": _vpc, "components": {
        "L1": "양쪽 인스턴스 호스트", "L2": "CloudTrail 쓰기 이벤트 (보안 그룹·NACL·경로)",
        "L3": "보안 그룹 · NACL · 라우팅 테이블 · 흐름 로그", "L4": "로드 밸런서 (거치지 않음)", "L5": "출발지 · 목적지 인스턴스",
        "L6": "NAT 게이트웨이 포트 한도", "L7": "DNS 해석"}},
    "credential": {"title": "자격 증명 유출", "run": _credential, "default_hours": 24, "components": {
        "L1": "AWS (해당 없음)", "L2": "이 키가 바꾼 것 (쓰기 이벤트·기록 끄기)", "L3": "쓴 곳 (IP · 도구 · 리전)",
        "L4": "앞단 (해당 없음)", "L5": "인스턴스·함수 남용 (채굴 흔적 · GuardDuty)", "L6": "지속성 확보 · 권한 더듬기 · 키 상태",
        "L7": "데이터 접근 (비밀 값 · 버킷 공개 · 스냅샷 공유)"}},
    "cost": {"title": "비용 급증", "run": _cost, "fixed_hours": RECENT_DAYS * 24, "components": {
        "L1": "AWS 가격·청구 (해당 없음)", "L2": "비용을 늘리는 변경", "L3": "데이터 전송 · NAT",
        "L4": "로드 밸런서 · API Gateway · CloudFront", "L5": "인스턴스·실행 환경 (EC2 · Lambda · RDS · 컨테이너)",
        "L6": "예산 · 이상 탐지 감시", "L7": "저장 · 로그 · 요청"}},
}
ALIASES = {"elb": "alb", "elbv2": "alb", "loadbalancer": "alb", "instance": "ec2", "function": "lambda", "bucket": "s3",
           "db": "rds", "database": "rds", "network": "vpc", "connectivity": "vpc", "iam": "credential",
           "key": "credential", "access_key": "credential", "leak": "credential", "billing": "cost", "ce": "cost"}


def run(service: str, resource: str, hours: Optional[int] = None, region: str = "",
        clients: Optional[Clients] = None, now: Optional[datetime] = None, target: Optional[str] = None) -> Dict[str, Any]:
    """진단 한 번. 잘못된 입력(모르는 서비스, 없는 자원)은 ValueError.
    - 보는 시간: 서비스마다 기본값이 있다 (자격 증명은 24시간). 비용은 날짜로 비교해 hours를 쓰지 않는다
    - L2: 절차가 관련 자원 목록을 돌려주면 여기서 그 자원의 변경을 찾는다. None이면 절차가 L2를 스스로 채웠다
      (자격 증명: 그 키가 한 쓰기, 비용: 계정 전체의 비용을 늘리는 변경)"""
    key = ALIASES.get((service or "").strip().lower(), (service or "").strip().lower())
    if key not in SERVICES:
        raise ValueError(f"진단할 수 있는 서비스는 {', '.join(SERVICES)}입니다: {service}")
    if not (resource or "").strip():
        raise ValueError("resource(ALB 이름·인스턴스 ID·함수 이름·버킷 이름·DB 식별자·출발지 인스턴스·"
                         "액세스 키나 사용자·account)가 필요합니다")
    spec = SERVICES[key]
    hours = spec.get("fixed_hours") or max(1, min(int(hours or spec.get("default_hours", DEFAULT_HOURS)), MAX_HOURS))
    clients = clients or Clients(region)
    d = Diagnosis(key, resource.strip(), hours, now or datetime.now(timezone.utc), (target or "").strip())
    related = spec["run"](d, clients)
    if related is not None:
        _check_changes(d, clients, related)  # 다른 층이 모두 나온 뒤 (모듈 설명의 L2)
    return d.result(spec["components"], spec["title"])

