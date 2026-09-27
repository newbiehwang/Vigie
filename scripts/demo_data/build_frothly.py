"""시연용 데이터(frontend/src/mock/demo/frothly.json)를 공개 데이터셋에서 뽑는다.

데모(npm run build:demo)는 AWS와 Claude를 부르지 않고 mock API(frontend/src/mock/api.ts)가 답한다.
그 답과 홈 대시보드가 실제 운영 기록처럼 보이도록, 공개된 실제(가상 회사) AWS 기록에서 필요한 부분만 골라 둔다.

    Splunk BOTS v3 (CC0 1.0, https://github.com/splunk/botsv3)
      가상 회사 Frothly의 AWS 계정 기록 (2018-08-20, 약 6시간, us-west-1 중심)
      ├─ aws:cloudtrail   → trail: 변경·로그인·거부된 호출 (조회 호출은 수만 센다)
      ├─ aws:cloudwatch   → metrics: EC2·RDS·ALB·Lambda 지표 (5분 간격 36개)
      ├─ aws:config:rule  → config: 규칙을 어긴 리소스
      └─ aws:description  → instances·securityGroups·eips (리소스 목록)
    FinOps Foundation FOCUS 1.0 Sample Data (CC BY 4.0, https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data)
      실제 AWS 청구를 익명화한 표 → cost: 서비스별 비중만 쓴다 (금액은 익명화로 흐트러져 있어 쓰지 않는다)

BOTS v3는 Splunk에 미리 색인된 형태로만 배포된다. Splunk 없이, 색인의 원본 저널(rawdata/journal.gz)에서
제어 문자 뒤에 오는 JSON 덩어리를 읽어 낸다 (JSON으로 남은 sourcetype만 나온다. VPC 흐름 로그 같은 글 줄은 쓰지 않는다).
지표 이벤트에는 시각이 원본에 없다(Splunk 메타데이터에 있다). 저널에 쌓인 차례를 시간 차례로 보고 5분씩 붙인다.

시각은 '마지막 기록으로부터 몇 초 전'(at, 음수)으로 저장한다. 화면은 지금 시각에 맞춰 옮겨 그린다.

실행 (파일은 저장소에 넣지 않는다. 335MB):
    curl -LO https://botsdataset.s3.amazonaws.com/botsv3/botsv3_data_set.tgz   # MD5 d7ccca99a01cff070dff3c139cdc10eb
    tar xzf botsv3_data_set.tgz
    curl -LO https://raw.githubusercontent.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data/main/FOCUS-1.0/focus_sample_100000.csv.gz
    python scripts/demo_data/build_frothly.py botsv3_data_set focus_sample_100000.csv.gz
"""
import collections
import csv
import glob
import gzip
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "frontend" / "src" / "mock" / "demo" / "frothly.json"

# 조회만 하는 호출 (수만 센다). 나머지(변경·로그인·거부된 호출)는 하나씩 남긴다
READ_PREFIXES = ("Describe", "List", "Get", "Lookup", "Head", "Search", "Check", "Decrypt", "AssumeRole",
                 "GenerateDataKey", "CreateLogStream", "PutEvaluations", "PutLogEvents", "StartAssessmentRun")
KEEP_READS = {"GetConsoleOutput"}  # 조회지만 보안상 볼 만한 것 (인스턴스 콘솔 출력)

# 지표 (이름, 차원) → 저장할 이름
METRICS = {
    ("CPUUtilization", "AutoScalingGroupName=[WebServers]"): "asgCpu",
    ("CPUUtilization", "InstanceId=[i-08e52f8b5a034012d]"): "forensicCpu",
    ("CPUUtilization", "DBInstanceIdentifier=[polaris]"): "rdsCpu",
    ("FreeStorageSpace", "DBInstanceIdentifier=[polaris]"): "rdsFreeStorage",
    ("RequestCount", "LoadBalancer=[app/FrothlyWebLB/c56b349d03f24dfb]"): "albRequests",
    ("HealthyHostCount", "AvailabilityZone=[us-west-1a],LoadBalancer=[app/FrothlyWebLB/c56b349d03f24dfb],"
                         "TargetGroup=[targetgroup/FrothlyWebServerGroup/f2e8d0cc265ce6f5]"): "healthyHosts",
    ("Errors", "FunctionName=[VPCFlowLogs],Resource=[VPCFlowLogs]"): "vpcFlowLogsErrors",
    ("Errors", "FunctionName=[RDSAuditLogs],Resource=[RDSAuditLogs]"): "rdsAuditLogsErrors",
    ("Invocations", "FunctionName=[VPCFlowLogs]"): "vpcFlowLogsInvocations",
    ("Invocations", "FunctionName=[RDSAuditLogs]"): "rdsAuditLogsInvocations",
    ("Duration", "FunctionName=[VPCFlowLogs]"): "vpcFlowLogsDuration",
    ("Duration", "FunctionName=[RDSAuditLogs]"): "rdsAuditLogsDuration",
}
PERIOD = 300


def read_events(root: str):
    """저널에서 JSON 이벤트를 모두 읽는다."""
    decoder = json.JSONDecoder()
    start = re.compile(r'(?<=[\x00-\x1f\x7f-￿])\{"')
    for path in sorted(glob.glob(f"{root}/var/lib/splunk/botsv3/db/*/rawdata/journal.gz")):
        data = gzip.open(path).read().decode("utf-8", "replace")
        for match in start.finditer(data):
            try:
                obj, _ = decoder.raw_decode(data, match.start())
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def epoch(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def actor(identity: dict) -> str:
    if identity.get("userName"):
        return identity["userName"]
    arn = identity.get("arn") or ""
    if ":assumed-role/" in arn:
        return arn.split(":assumed-role/")[1].split("/")[0]
    return identity.get("invokedBy") or identity.get("type") or "unknown"


def target(event: dict) -> str:
    """사람이 읽을 대상 (버킷·인스턴스·보안 그룹·사용자)."""
    params = event.get("requestParameters") or {}
    if params.get("bucketName"):
        return params["bucketName"]
    if params.get("groupId"):
        return params["groupId"]
    if params.get("instanceId"):
        return params["instanceId"]
    items = ((params.get("instancesSet") or {}).get("items")) or []
    if items:
        return ", ".join(item.get("instanceId", "") for item in items)
    if params.get("userName"):
        return params["userName"]
    for key in ("alarmName", "autoScalingGroupName", "policyName"):
        if params.get(key):
            return params[key]
    if params.get("alarmNames"):
        return ", ".join(params["alarmNames"])
    return ""


def detail(event: dict) -> dict:
    """이벤트마다 답변에 쓸 사실 몇 가지."""
    params = event.get("requestParameters") or {}
    name = event["eventName"]
    if name == "ConsoleLogin":
        extra = event.get("additionalEventData") or {}
        return {"mfa": extra.get("MFAUsed") == "Yes",
                "result": (event.get("responseElements") or {}).get("ConsoleLogin")}
    if name == "PutBucketAcl":
        grants = (((params.get("AccessControlPolicy") or {}).get("AccessControlList") or {}).get("Grant")) or []
        public = sorted({g["Permission"] for g in grants
                         if (g.get("Grantee") or {}).get("URI", "").endswith("/global/AllUsers")})
        return {"publicPermissions": public}
    if name in ("AuthorizeSecurityGroupIngress", "RevokeSecurityGroupIngress"):
        rules = []
        for item in ((params.get("ipPermissions") or {}).get("items")) or []:
            ranges = [r.get("cidrIp") for r in ((item.get("ipRanges") or {}).get("items") or [])]
            ranges += [r.get("cidrIpv6") for r in ((item.get("ipv6Ranges") or {}).get("items") or [])]
            rules.append({"protocol": item.get("ipProtocol"), "port": item.get("fromPort"), "from": ranges})
        return {"rules": rules}
    if name == "UpdateAccessKey":
        return {"status": params.get("status"), "keyUser": params.get("userName")}
    return {}


def build(root: str, focus: str) -> dict:
    events = collections.defaultdict(list)
    for obj in read_events(root):
        keys = set(obj)
        if {"eventSource", "eventName"} <= keys:
            events["trail"].append(obj)
        elif {"metric_name", "metric_dimensions", "Average"} <= keys:
            events["metric"].append(obj)
        elif "ConfigRuleInvokedTime" in keys:
            events["config"].append(obj)
        elif {"instance_type", "image_id"} <= keys:
            events["instance"].append(obj)
        elif {"rules", "rules_egress"} <= keys:
            events["sg"].append(obj)
        elif {"allocation_id", "public_ip"} <= keys:
            events["eip"].append(obj)

    trail = sorted(events["trail"], key=lambda e: e["eventTime"])
    end = epoch(trail[-1]["eventTime"])
    kept, reads = [], collections.Counter()
    for event in trail:
        name = event["eventName"]
        is_read = name.startswith(READ_PREFIXES) and name not in KEEP_READS
        if is_read and event.get("errorCode") not in ("AccessDenied", "Client.UnauthorizedOperation"):
            reads[name] += 1
            continue
        kept.append({
            "at": epoch(event["eventTime"]) - end,
            "name": name,
            "source": event["eventSource"].split(".")[0],
            "actor": actor(event["userIdentity"]),
            "ip": event.get("sourceIPAddress"),
            "region": event.get("awsRegion"),
            "agent": (event.get("userAgent") or "")[:60],
            "target": target(event),
            **({"error": event["errorCode"]} if event.get("errorCode") else {}),
            **detail(event),
        })

    series = collections.defaultdict(list)
    for metric in events["metric"]:
        key = METRICS.get((metric["metric_name"], metric["metric_dimensions"]))
        if key:
            series[key].append(metric)
    metrics = {key: {"unit": points[0]["Unit"], "period": PERIOD,
                     "average": [round(p["Average"], 3) for p in points],
                     "maximum": [round(p["Maximum"], 3) for p in points],
                     "sum": [round(p["Sum"], 3) for p in points]}
               for key, points in series.items()}

    config = sorted({(e["EvaluationResultIdentifier"]["EvaluationResultQualifier"]["ConfigRuleName"],
                      e["EvaluationResultIdentifier"]["EvaluationResultQualifier"]["ResourceType"],
                      e["EvaluationResultIdentifier"]["EvaluationResultQualifier"]["ResourceId"])
                     for e in events["config"] if e["ComplianceType"] == "NON_COMPLIANT"})

    instances = {i["id"]: {"id": i["id"], "type": i["instance_type"], "state": i["state"],
                           "name": (i.get("tags") or {}).get("Name") or (i.get("tags") or {}).get(
                               "aws:autoscaling:groupName"),
                           "zone": i["placement"], "monitoring": bool(i.get("monitored"))}
                 for i in events["instance"]}
    groups = {}
    for group in events["sg"]:
        open_rules = [{"protocol": r["ip_protocol"], "port": r["from_port"]} for r in group["rules"]
                      if any(g.get("cidr_ip") == "0.0.0.0/0" for g in r.get("grants") or [])]
        if open_rules:
            groups[group["id"]] = {"id": group["id"], "name": group.get("name"), "openToWorld": open_rules,
                                   "instances": [i["id"] for i in group.get("instances") or []]}
    eips = sorted({(e["public_ip"], bool(e.get("instance_id"))) for e in events["eip"]})

    buckets = collections.Counter(target(e) for e in trail if e["eventSource"] == "s3.amazonaws.com" and target(e))
    users = sorted({actor(e["userIdentity"]) for e in trail if e["userIdentity"].get("type") == "IAMUser"}
                   | {(e.get("requestParameters") or {}).get("userName") for e in trail
                      if e["eventName"] == "ListAccessKeys" and (e.get("requestParameters") or {}).get("userName")})

    return {
        "sources": [
            {"name": "Splunk Boss of the SOC (BOTS) v3", "license": "CC0 1.0",
             "url": "https://github.com/splunk/botsv3"},
            {"name": "FinOps Foundation FOCUS 1.0 Sample Data", "license": "CC BY 4.0",
             "url": "https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data",
             "changes": "AWS 행의 서비스별 비중만 계산해 사용"},
        ],
        "company": "Frothly",
        "account": trail[-1]["recipientAccountId"] if trail[-1].get("recipientAccountId") else "622676721278",
        "region": "us-west-1",
        "originalEnd": trail[-1]["eventTime"],
        "trail": kept,
        "trailReads": {"total": sum(reads.values()), "top": reads.most_common(8)},
        "trailTotal": len(trail),
        # 사람(주체)마다 호출 수: 전체 · 조회가 아닌 호출 · 거부된 호출 (최소 권한 답변에 쓴다)
        "actors": [{"actor": name, **counts} for name, counts in sorted(
            actor_counts(trail).items(), key=lambda item: -item[1]["total"])],
        "metrics": metrics,
        "config": [{"rule": rule, "type": kind, "resource": resource} for rule, kind, resource in config],
        "instances": sorted(instances.values(), key=lambda i: i["id"]),
        "securityGroups": sorted(groups.values(), key=lambda g: g["id"]),
        "eips": [{"ip": ip, "attached": attached} for ip, attached in eips],
        "buckets": [{"name": name, "calls": calls} for name, calls in buckets.most_common()],
        "users": users,
        "cost": cost_shares(focus),
    }


def actor_counts(trail: list) -> dict:
    counts = collections.defaultdict(lambda: {"total": 0, "writes": 0, "denied": 0})
    for event in trail:
        entry = counts[actor(event["userIdentity"])]
        entry["total"] += 1
        if not event["eventName"].startswith(READ_PREFIXES) and event["eventName"] != "ConsoleLogin":
            entry["writes"] += 1
        if event.get("errorCode") in ("AccessDenied", "Client.UnauthorizedOperation"):
            entry["denied"] += 1
    return counts


def cost_shares(path: str) -> list:
    """FOCUS 샘플의 AWS 행으로 서비스별 비중 (%). 0.5% 미만은 '기타'로 묶는다."""
    totals = collections.Counter()
    with gzip.open(path, "rt") as handle:
        for row in csv.DictReader(handle):
            if row["ProviderName"] != "AWS":
                continue
            try:
                totals[row["ServiceName"]] += float(row["EffectiveCost"])
            except ValueError:
                pass
    total = sum(v for v in totals.values() if v > 0)
    shares, other = [], 0.0
    for service, amount in totals.most_common():
        share = amount / total * 100
        if share >= 0.5:
            shares.append({"service": service, "share": round(share, 1)})
        elif share > 0:
            other += share
    if other:
        shares.append({"service": "기타", "share": round(other, 1)})
    return shares


if __name__ == "__main__":
    data = build(sys.argv[1], sys.argv[2])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{OUT}: 변경·로그인 {len(data['trail'])}건, 지표 {len(data['metrics'])}개, "
          f"{OUT.stat().st_size // 1024}KB")
