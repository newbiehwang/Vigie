"""장애 재현용 가짜 AWS 환경: 정상인 환경(world)과 거기에 심을 장애 시나리오

진단 채점 테스트(tests/test_diagnose.py)와 로컬 실행(local/stack.py)이 같은 코드를 쓴다.
어느 쪽이든 moto가 켜진 상태(mock_aws)에서 부른다. 이 모듈은 AWS에 직접 닿는 설정을 하지 않는다.

    정상 환경 (build_world)
      기본 VPC + 인터넷 게이트웨이 경로
      web-alb (ALB) → web-1 · web-2 (t3.micro, 보안 그룹 web은 ALB에서 80, VPC 안에서 22만 받는다)
      orders-api (Lambda, 제한 시간 3초) · vigie-diag-private (비공개 버킷)
      orders-db (RDS MySQL, Multi-AZ, 웹 서버 보안 그룹에서만 3306)
      ci-deployer (IAM 사용자와 액세스 키) · 평소의 하루 비용 (BASELINE_COSTS)

    시나리오 (SCENARIOS): 이름 · 서비스 · 자원 · 심은 장애 · 기대 원인 층 · 기대 증상 층 · 대화창에서 물어볼 말

moto에 없는 것은 world에 기록으로 심고 진단 절차의 조회 함수를 바꿔 끼운다 (install_fakes).
- CloudTrail 조회(LookupEvents): 자원 변경 기록(trail) · 키 사용 기록(activity) · 비용을 늘린 변경(cost_changes)
- Cost Explorer 날짜별 비용(GetCostAndUsage): costs (평소 base, 최근 3일 recent)
지표·로그는 넣은 시각이 진단 창(최근 1시간) 안에 들어야 하므로, 넣은 값을 기억해 두고 refresh로 지금 시각에 다시 넣는다.
장애가 넣은 지표는 장애가 남아 있는 동안에만 다시 넣는다 (put_metric의 while_). 대화에서 승인해 되돌리면(인스턴스 시작 등)
더 넣지 않아, 마지막으로 넣은 값이 한 시간 안에 진단 창 밖으로 밀려난다: 실제 AWS처럼 복구 직후에는 지난 증상이 남고
한 시간 뒤에는 정상으로 돌아온다. 평소 값을 덮어쓴 지표였다면 그때부터 평소 값을 다시 넣는다.
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import boto3

# 정상 환경의 이름들 (시나리오의 질문과 자원이 이 이름을 쓴다)
ALB, TARGET_GROUP = "web-alb", "web-tg"
FUNCTION = "orders-api"
BUCKET = "vigie-diag-private"
DB = "orders-db"
IAM_USER = "ci-deployer"
LOG_GROUP = f"/aws/lambda/{FUNCTION}"
LOG_STREAM = "2026/09/28/[$LATEST]abc"

# 진단 절차가 모두 되돌려 놓는 서비스 (reset_world): Vigie 자신의 테이블(DynamoDB)·설정(SSM)은 남긴다
WORLD_SERVICES = ("ec2", "elbv2", "autoscaling", "lambda", "logs", "cloudwatch", "s3", "rds", "iam")

# 비용: 평소의 하루 금액 (서비스, 사용 유형, USD). APN2는 서울 리전의 사용 유형 접두어
BASELINE_COSTS = [
    ("Amazon Elastic Compute Cloud - Compute", "APN2-BoxUsage:t3.micro", 6.2),
    ("EC2 - Other", "APN2-NatGateway-Hours", 1.4),
    ("EC2 - Other", "APN2-NatGateway-Bytes", 0.8),
    ("AmazonCloudWatch", "APN2-DataProcessing-Bytes", 2.1),
    ("Amazon Relational Database Service", "APN2-InstanceUsage:db.t3.micro", 4.9),
    ("Amazon Simple Storage Service", "APN2-TimedStorage-ByteHrs", 0.6),
]


def _clock(moment: datetime) -> str:
    """진단 절차의 근거 글과 같은 시각 표기 (한국 시간 월-일 시:분)."""
    return (moment.astimezone(timezone.utc) + timedelta(hours=9)).strftime("%m-%d %H:%M")


def client(world: dict, service: str):
    return boto3.client(service, region_name=world["region"])


# ---------------------------------------------------------------- 지표 · 로그 (넣은 값을 기억해 refresh로 다시 넣는다)
@dataclass
class Metric:
    """다시 넣을 지표 하나. while_가 있으면 장애 지표다: 그 조건이 참인 동안(장애가 남은 동안)만 다시 넣는다.
    fallback은 장애가 덮어쓴 평소 값 (value, unit). 장애가 풀리면 그 값으로 돌아간다."""
    value: float
    unit: str
    while_: Optional[Callable[[dict], bool]] = None
    fallback: Optional[tuple] = None


def _write_metric(world: dict, namespace: str, name: str, value: float, dimensions: dict, unit: str,
                  at: Optional[datetime] = None) -> None:
    client(world, "cloudwatch").put_metric_data(Namespace=namespace, MetricData=[
        {"MetricName": name, "Timestamp": at or datetime.now(timezone.utc) - timedelta(minutes=10), "Value": value,
         "Unit": unit, "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()]}])


def put_metric(world: dict, namespace: str, name: str, value: float, dimensions: dict, unit: str = "Count",
               while_: Optional[Callable[[dict], bool]] = None) -> None:
    """지표를 넣고 refresh가 다시 넣도록 기억한다. while_: 장애가 남아 있나 (world를 받아 참·거짓)."""
    key = (namespace, name, tuple(sorted(dimensions.items())))
    previous = world["metrics"].get(key)
    fallback = (previous.value, previous.unit) if while_ and previous and previous.while_ is None else None
    world["metrics"][key] = Metric(value, unit, while_, fallback)
    _write_metric(world, namespace, name, value, dimensions, unit)


def put_logs(world: dict, group: str, lines: list, at_ms: Optional[int] = None) -> None:
    if at_ms is None:
        world["logs"].append((group, list(lines)))
    stamp = at_ms or int(time.time() * 1000) - 5 * 60 * 1000
    client(world, "logs").put_log_events(logGroupName=group, logStreamName=LOG_STREAM, logEvents=[
        {"timestamp": stamp + index, "message": line} for index, line in enumerate(lines)])


def refresh(world: dict) -> int:
    """넣어 둔 지표·로그를 지금 시각에 다시 넣는다 (진단 창 '최근 1시간' 안에 머물도록). 다시 넣은 지표 수.
    장애 지표는 장애가 풀렸으면 더 넣지 않는다 (덮어쓴 평소 값이 있으면 그 값으로 돌아간다)."""
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    for key, metric in list(world["metrics"].items()):
        if metric.while_ is not None and not metric.while_(world):
            if metric.fallback is None:
                del world["metrics"][key]
                continue
            metric = world["metrics"][key] = Metric(*metric.fallback)
        namespace, name, dimensions = key
        _write_metric(world, namespace, name, metric.value, dict(dimensions), metric.unit, at=now)
    for group, lines in list(world["logs"]):
        put_logs(world, group, lines, at_ms=int(now.timestamp() * 1000))
    return len(world["metrics"])


# ---------------------------------------------------------------- 정상인 환경
def build_world(region: str) -> dict:
    """정상인 환경을 만들고, 시나리오가 쓸 ID와 심을 기록 목록을 담은 dict를 돌려준다."""
    world = {"region": region, "metrics": {}, "logs": [],
             "trail": [],          # 심은 CloudTrail 쓰기 이벤트 (자원 변경)
             "activity": [],       # 심은 키 사용 기록 (자격 증명)
             "cost_changes": [],   # 심은 비용을 늘리는 변경
             "costs": {"base": {}, "recent": {}}}  # (서비스, 사용 유형) → 하루 금액
    ec2, elb = client(world, "ec2"), client(world, "elbv2")
    vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]
    vpc_id = vpc["VpcId"]
    subnets = [s for s in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]][:2]
    # 인터넷 게이트웨이 경로를 기본 라우팅 테이블에 둔다 (moto의 기본 VPC에는 로컬 경로뿐이다)
    igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc_id)
    main = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                                              {"Name": "association.main", "Values": ["true"]}])["RouteTables"][0]
    ec2.create_route(RouteTableId=main["RouteTableId"], DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)

    alb_sg = ec2.create_security_group(GroupName=ALB, Description="alb", VpcId=vpc_id)["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=alb_sg, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
    web_sg = ec2.create_security_group(GroupName="web", Description="web", VpcId=vpc_id)["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=web_sg, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "UserIdGroupPairs": [{"GroupId": alb_sg}]},
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": vpc["CidrBlock"]}]}])

    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    instances = {}
    for index, subnet in enumerate(subnets, start=1):
        instances[f"web-{index}"] = ec2.run_instances(
            ImageId=image, MinCount=1, MaxCount=1, InstanceType="t3.micro", SubnetId=subnet["SubnetId"],
            SecurityGroupIds=[web_sg], TagSpecifications=[{"ResourceType": "instance", "Tags": [
                {"Key": "Name", "Value": f"web-{index}"}]}])["Instances"][0]["InstanceId"]

    lb = elb.create_load_balancer(Name=ALB, Subnets=[s["SubnetId"] for s in subnets],
                                  SecurityGroups=[alb_sg], Scheme="internet-facing")["LoadBalancers"][0]
    tg = elb.create_target_group(Name=TARGET_GROUP, Protocol="HTTP", Port=80, VpcId=vpc_id,
                                 HealthCheckPath="/health")["TargetGroups"][0]
    elb.register_targets(TargetGroupArn=tg["TargetGroupArn"], Targets=[{"Id": i} for i in instances.values()])
    elb.create_listener(LoadBalancerArn=lb["LoadBalancerArn"], Protocol="HTTP", Port=80,
                        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg["TargetGroupArn"]}])
    lb_dimension = {"LoadBalancer": lb["LoadBalancerArn"].split(":loadbalancer/")[-1]}
    world.update({"vpc": vpc_id, "subnets": [s["SubnetId"] for s in subnets], "main_route_table": main["RouteTableId"],
                  "alb_sg": alb_sg, "web_sg": web_sg, "instances": instances, "lb": lb, "tg": tg,
                  "lb_dimension": lb_dimension, "igw": igw, "image": image, "bucket": BUCKET})
    put_metric(world, "AWS/ApplicationELB", "RequestCount", 1200, lb_dimension)
    put_metric(world, "AWS/ApplicationELB", "TargetResponseTime", 0.21, lb_dimension, unit="Seconds")
    for instance_id in instances.values():
        put_metric(world, "AWS/EC2", "CPUUtilization", 35, {"InstanceId": instance_id}, unit="Percent")
        put_metric(world, "AWS/EC2", "CPUCreditBalance", 120, {"InstanceId": instance_id})

    role = client(world, "iam").create_role(RoleName=f"{FUNCTION}-role", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("handler.py", "def handle(event, context):\n    return {}\n")
    client(world, "lambda").create_function(FunctionName=FUNCTION, Runtime="python3.13", Role=role,
                                            Handler="handler.handle", Code={"ZipFile": package.getvalue()},
                                            Timeout=3, MemorySize=256)
    logs = client(world, "logs")
    logs.create_log_group(logGroupName=LOG_GROUP)
    logs.create_log_stream(logGroupName=LOG_GROUP, logStreamName=LOG_STREAM)
    put_logs(world, LOG_GROUP, ["START RequestId: 1", "REPORT RequestId: 1 Duration: 812.40 ms"])
    put_metric(world, "AWS/Lambda", "Invocations", 500, {"FunctionName": FUNCTION})
    put_metric(world, "AWS/Lambda", "Duration", 812.4, {"FunctionName": FUNCTION}, unit="Milliseconds")

    s3 = client(world, "s3")
    s3.create_bucket(Bucket=BUCKET, **({} if region == "us-east-1" else
                                       {"CreateBucketConfiguration": {"LocationConstraint": region}}))
    s3.put_public_access_block(Bucket=BUCKET, PublicAccessBlockConfiguration={
        key: True for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")})
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})

    # RDS: 웹 서버 보안 그룹에서만 3306을 받는 MySQL (Multi-AZ, 백업 7일)
    db_sg = ec2.create_security_group(GroupName=DB, Description="db", VpcId=vpc_id)["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=db_sg, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 3306, "ToPort": 3306, "UserIdGroupPairs": [{"GroupId": web_sg}]}])
    client(world, "rds").create_db_instance(
        DBInstanceIdentifier=DB, DBInstanceClass="db.t3.micro", Engine="mysql", AllocatedStorage=20,
        MasterUsername="admin", MasterUserPassword="not-a-real-password", VpcSecurityGroupIds=[db_sg],
        StorageType="gp2", MultiAZ=True, BackupRetentionPeriod=7)
    db_dimension = {"DBInstanceIdentifier": DB}
    put_metric(world, "AWS/RDS", "CPUUtilization", 22, db_dimension, unit="Percent")
    put_metric(world, "AWS/RDS", "FreeStorageSpace", 15 * 1024 ** 3, db_dimension, unit="Bytes")
    put_metric(world, "AWS/RDS", "FreeableMemory", 400 * 1024 ** 2, db_dimension, unit="Bytes")
    put_metric(world, "AWS/RDS", "BurstBalance", 98, db_dimension, unit="Percent")
    put_metric(world, "AWS/RDS", "DatabaseConnections", 12, db_dimension)

    # 자격 증명: 배포용 IAM 사용자와 키 (키 값은 moto가 실행할 때 만든다)
    iam = client(world, "iam")
    iam.create_user(UserName=IAM_USER)
    access_key = iam.create_access_key(UserName=IAM_USER)["AccessKey"]["AccessKeyId"]

    for service, usage, daily in BASELINE_COSTS:
        world["costs"]["base"][(service, usage)] = daily
        world["costs"]["recent"][(service, usage)] = daily
    world.update({"db_sg": db_sg, "db_dimension": db_dimension, "access_key": access_key})
    return world


def cost_rows(costs: dict) -> list:
    """Cost Explorer의 날짜별 행 (diagnose._daily_costs와 같은 모양): 앞 14일은 base, 최근 3일은 recent의 하루 금액."""
    today = datetime.now(timezone.utc).date()
    rows = []
    for back in range(1, 18):
        day = (today - timedelta(days=back)).isoformat()
        amounts = costs["recent"] if back <= 3 else costs["base"]
        rows += [(day, service, usage, amount) for (service, usage), amount in amounts.items()]
    return rows


def install_fakes(diagnose, world: dict, patch: Callable = setattr) -> None:
    """moto에 없는 조회(CloudTrail·Cost Explorer)를 world의 기록을 읽는 가짜로 바꾼다.
    patch: 테스트는 monkeypatch.setattr (끝나면 되돌린다), 로컬 실행은 setattr.
    가짜는 절차가 넘긴 관련 자원 이름·ID에 든 기록만 돌려주므로, 절차가 어떤 자원을 관련 있다고 보는지도 드러난다."""
    def fake_changes(clients, identifiers, start, end):
        wanted = set(identifiers)
        return [event for event in world["trail"] if event["resource"] in wanted]

    patch(diagnose, "recent_changes", fake_changes)
    patch(diagnose, "key_activity", lambda clients, key, start, end: [
        e for e in world["activity"] if e.get("key", world["access_key"]) == key])
    patch(diagnose, "account_changes", lambda clients, start, end: list(world["cost_changes"]))
    patch(diagnose, "_daily_costs", lambda ce, start, end: cost_rows(world["costs"]))


def reset_world() -> None:
    """정상 환경이 쓰는 서비스의 moto 상태를 비운다 (Vigie 자신의 DynamoDB 테이블·SSM 설정은 그대로)."""
    from moto.backends import get_backend
    from moto.core import DEFAULT_ACCOUNT_ID
    for service in WORLD_SERVICES:
        for backend in list(get_backend(service)[DEFAULT_ACCOUNT_ID].values()):
            backend.reset()


# 변경 이벤트가 어느 서비스의 API인가 (홈의 '최근 변경'에 보이는 CloudTrail eventSource)
EVENT_SOURCES = {"PutBucketPublicAccessBlock": "s3", "PutBucketPolicy": "s3", "StopDBInstance": "rds",
                 "PutFunctionConcurrency20171031": "lambda"}


def change(world: dict, event: str, resource: str, user: str = "deploy-bot") -> None:
    """자원 변경 기록(CloudTrail 쓰기 이벤트) 하나를 심는다 (diagnose.recent_changes가 돌려주는 모양 + 시각 epoch)."""
    moment = datetime.now(timezone.utc) - timedelta(minutes=12)
    world["trail"].append({"time": _clock(moment), "event": event, "user": user, "resource": resource,
                           "epoch": int(moment.timestamp())})


class TrailLookup:
    """moto에 없는 CloudTrail LookupEvents 대신 world에 심은 변경 기록을 CloudTrail 응답 모양으로 돌려준다.
    홈 대시보드 수집(services/dashboard/collector.collect_changes)이 get_paginator('lookup_events')로 부른다."""

    def __init__(self, world_of: Callable[[], Optional[dict]]):
        self.world_of = world_of

    def get_paginator(self, operation: str):
        assert operation == "lookup_events"
        return self

    def paginate(self, StartTime: datetime, EndTime: datetime, **_) -> list:  # noqa: N803 (boto3 인자 이름)
        world = self.world_of() or {"trail": []}
        events = [{"EventTime": datetime.fromtimestamp(change["epoch"], timezone.utc), "EventName": change["event"],
                   "Username": change["user"],
                   "EventSource": f"{EVENT_SOURCES.get(change['event'], 'ec2')}.amazonaws.com",
                   "Resources": [{"ResourceName": change["resource"]}]}
                  for change in world["trail"] if StartTime.timestamp() <= change["epoch"] <= EndTime.timestamp()]
        return [{"Events": sorted(events, key=lambda e: e["EventTime"], reverse=True)}]


def act(world: dict, event: str, *, read_only: bool = False, error: str = "", region: str = "", source: str = "",
        ip: str = "198.51.100.23", times: int = 1) -> None:
    """키 사용 기록 하나를 심는다 (diagnose.key_activity가 돌려주는 모양)."""
    for _ in range(times):
        world["activity"].append({"time": _clock(datetime.now(timezone.utc) - timedelta(hours=3)), "event": event,
                                  "source": source or "ec2.amazonaws.com", "ip": ip, "agent": "aws-cli/2.17.0",
                                  "region": region or world["region"], "error": error, "read_only": read_only})


def spike(world: dict, service: str, usage: str, recent: float) -> None:
    world["costs"]["recent"][(service, usage)] = recent


def private_ip(world: dict, name: str) -> str:
    instance = client(world, "ec2").describe_instances(InstanceIds=[world["instances"][name]])
    return instance["Reservations"][0]["Instances"][0]["PrivateIpAddress"]


def fill(text: Optional[str], world: dict) -> Optional[str]:
    """시나리오의 자리 표시를 world 값으로 채운다: {key} 액세스 키, {web-2} 사설 IP, {id:web-1} 인스턴스 ID."""
    if text is None:
        return None
    for name, instance_id in world["instances"].items():
        text = text.replace(f"{{id:{name}}}", instance_id)
        if f"{{{name}}}" in text:
            text = text.replace(f"{{{name}}}", private_ip(world, name))
    return text.replace("{key}", world["access_key"])


# ---------------------------------------------------------------- 장애가 남아 있나 (장애 지표의 while_)
def targets_down(world) -> bool:
    """웹 대상 중 하나라도 실행 중이 아니다 (대화에서 승인해 모두 시작하면 거짓)."""
    reservations = client(world, "ec2").describe_instances(
        InstanceIds=list(world["instances"].values()))["Reservations"]
    return any(i["State"]["Name"] != "running" for r in reservations for i in r["Instances"])


def target_port_closed(world) -> bool:
    """대상 보안 그룹이 ALB에서 오는 80번을 받지 않는다."""
    group = client(world, "ec2").describe_security_groups(GroupIds=[world["web_sg"]])["SecurityGroups"][0]
    return not any(rule.get("FromPort") == 80 and any(pair.get("GroupId") == world["alb_sg"]
                                                      for pair in rule.get("UserIdGroupPairs", []))
                   for rule in group.get("IpPermissions", []))


# ---------------------------------------------------------------- 심을 장애: ALB · EC2 · Lambda · S3
def stop_targets(world):
    ec2 = client(world, "ec2")
    ec2.stop_instances(InstanceIds=list(world["instances"].values()))
    for instance_id in world["instances"].values():
        change(world, "StopInstances", instance_id, user="alice")
    put_metric(world, "AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", 340, world["lb_dimension"], while_=targets_down)
    put_metric(world, "AWS/ApplicationELB", "HTTPCode_ELB_503_Count", 340, world["lb_dimension"], while_=targets_down)


def block_target_sg(world):
    client(world, "ec2").revoke_security_group_ingress(GroupId=world["web_sg"], IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "UserIdGroupPairs": [{"GroupId": world["alb_sg"]}]}])
    change(world, "RevokeSecurityGroupIngress", world["web_sg"], user="alice")
    put_metric(world, "AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", 120, world["lb_dimension"],
               while_=target_port_closed)
    put_metric(world, "AWS/ApplicationELB", "HTTPCode_ELB_504_Count", 120, world["lb_dimension"],
               while_=target_port_closed)


def deregister_targets(world):
    client(world, "elbv2").deregister_targets(TargetGroupArn=world["tg"]["TargetGroupArn"],
                                              Targets=[{"Id": i} for i in world["instances"].values()])


def remove_igw_route(world):
    client(world, "ec2").delete_route(RouteTableId=world["main_route_table"], DestinationCidrBlock="0.0.0.0/0")


def target_5xx_after_deploy(world):
    put_metric(world, "AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", 410, world["lb_dimension"])
    for instance_id in world["instances"].values():
        change(world, "CreateLaunchTemplateVersion", instance_id)


def stop_web_1(world):
    client(world, "ec2").stop_instances(InstanceIds=[world["instances"]["web-1"]])
    change(world, "StopInstances", world["instances"]["web-1"], user="alice")


def _default_acl(world):
    return client(world, "ec2").describe_network_acls(Filters=[{"Name": "vpc-id", "Values": [world["vpc"]]},
                                                               {"Name": "default", "Values": ["true"]}])["NetworkAcls"][0]


def nacl_denies_ssh(world):
    client(world, "ec2").create_network_acl_entry(
        NetworkAclId=_default_acl(world)["NetworkAclId"], RuleNumber=50, Protocol="6", RuleAction="deny",
        Egress=False, CidrBlock="0.0.0.0/0", PortRange={"From": 22, "To": 22})


def exhaust_cpu_credits(world):
    instance_id = world["instances"]["web-1"]
    put_metric(world, "AWS/EC2", "CPUCreditBalance", 0, {"InstanceId": instance_id})
    put_metric(world, "AWS/EC2", "CPUUtilization", 100, {"InstanceId": instance_id}, unit="Percent")


def zero_reserved_concurrency(world):
    client(world, "lambda").put_function_concurrency(FunctionName=FUNCTION, ReservedConcurrentExecutions=0)
    put_metric(world, "AWS/Lambda", "Throttles", 230, {"FunctionName": FUNCTION})
    change(world, "PutFunctionConcurrency20171031", FUNCTION, user="alice")


def lambda_timeouts(world):
    put_metric(world, "AWS/Lambda", "Duration", 3000, {"FunctionName": FUNCTION}, unit="Milliseconds")
    put_metric(world, "AWS/Lambda", "Errors", 42, {"FunctionName": FUNCTION})
    put_logs(world, LOG_GROUP, ["2026-09-28T11:55:00Z 1 Task timed out after 3.00 seconds",
                                "REPORT RequestId: 2 Duration: 3000.00 ms Status: timeout"])


def lambda_in_vpc_without_nat(world):
    ec2 = client(world, "ec2")
    # NAT 없는 사설 서브넷: 경로가 로컬뿐인 전용 라우팅 테이블을 붙여 흔한 '사설 서브넷' 모양을 만든다
    subnet = ec2.create_subnet(VpcId=world["vpc"], CidrBlock="172.31.200.0/24")["Subnet"]["SubnetId"]
    table = ec2.create_route_table(VpcId=world["vpc"])["RouteTable"]["RouteTableId"]
    ec2.associate_route_table(RouteTableId=table, SubnetId=subnet)
    client(world, "lambda").update_function_configuration(
        FunctionName=FUNCTION, VpcConfig={"SubnetIds": [subnet], "SecurityGroupIds": [world["web_sg"]]})
    lambda_timeouts(world)


def lambda_access_denied(world):
    put_metric(world, "AWS/Lambda", "Errors", 17, {"FunctionName": FUNCTION})
    put_logs(world, LOG_GROUP, [
        "[ERROR] ClientError: An error occurred (AccessDeniedException) when calling the GetItem operation: "
        f"User: arn:aws:sts::123456789012:assumed-role/{FUNCTION}-role/{FUNCTION} is not authorized to perform: "
        "dynamodb:GetItem"])


def make_public_policy(world, restrict=False):
    s3 = client(world, "s3")
    s3.put_public_access_block(Bucket=BUCKET, PublicAccessBlockConfiguration={
        "BlockPublicAcls": False, "IgnorePublicAcls": False, "BlockPublicPolicy": False,
        "RestrictPublicBuckets": restrict})
    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Sid": "PublicRead", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
         "Resource": f"arn:aws:s3:::{BUCKET}/*"}]}))
    if not restrict:
        change(world, "PutBucketPublicAccessBlock", BUCKET, user="alice")
        change(world, "PutBucketPolicy", BUCKET, user="alice")


def make_restricted_public_policy(world):
    make_public_policy(world, restrict=True)


def make_public_acl(world):
    s3 = client(world, "s3")
    s3.delete_public_access_block(Bucket=BUCKET)
    s3.put_bucket_acl(Bucket=BUCKET, ACL="public-read")


def deny_everyone(world):
    client(world, "s3").put_bucket_policy(Bucket=BUCKET, Policy=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Sid": "LockDown", "Effect": "Deny", "Principal": "*",
                                                "Action": "s3:*", "Resource": f"arn:aws:s3:::{BUCKET}/*"}]}))


# ---------------------------------------------------------------- 심을 장애: RDS · VPC 연결 · 자격 증명 · 비용
def stop_db(world):
    client(world, "rds").stop_db_instance(DBInstanceIdentifier=DB)
    change(world, "StopDBInstance", DB, user="alice")


def close_db_sg(world):
    client(world, "ec2").revoke_security_group_ingress(GroupId=world["db_sg"], IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 3306, "ToPort": 3306, "UserIdGroupPairs": [{"GroupId": world["web_sg"]}]}])
    change(world, "RevokeSecurityGroupIngress", world["db_sg"], user="alice")


def fill_db_storage(world):
    put_metric(world, "AWS/RDS", "FreeStorageSpace", 0.8 * 1024 ** 3, world["db_dimension"], unit="Bytes")


def drain_burst_balance(world):
    put_metric(world, "AWS/RDS", "BurstBalance", 0, world["db_dimension"], unit="Percent")
    put_metric(world, "AWS/RDS", "ReadLatency", 0.32, world["db_dimension"], unit="Seconds")


def stop_web_2(world):
    client(world, "ec2").stop_instances(InstanceIds=[world["instances"]["web-2"]])
    change(world, "StopInstances", world["instances"]["web-2"], user="alice")


def nacl_blocks_replies(world):
    client(world, "ec2").create_network_acl_entry(
        NetworkAclId=_default_acl(world)["NetworkAclId"], RuleNumber=50, Protocol="6", RuleAction="deny",
        Egress=True, CidrBlock="0.0.0.0/0", PortRange={"From": 1024, "To": 65535})


def private_worker(world, *, route=None, public_ip=False):
    """사설 서브넷(전용 라우팅 테이블)에 worker 인스턴스 하나. route: 0.0.0.0/0의 대상 {'NatGatewayId': …} 등."""
    ec2 = client(world, "ec2")
    subnet = ec2.create_subnet(VpcId=world["vpc"], CidrBlock="172.31.210.0/24")["Subnet"]["SubnetId"]
    ec2.modify_subnet_attribute(SubnetId=subnet, MapPublicIpOnLaunch={"Value": public_ip})
    table = ec2.create_route_table(VpcId=world["vpc"])["RouteTable"]["RouteTableId"]
    ec2.associate_route_table(RouteTableId=table, SubnetId=subnet)
    if route:
        ec2.create_route(RouteTableId=table, DestinationCidrBlock="0.0.0.0/0", **route)
    ec2.run_instances(ImageId=world["image"], MinCount=1, MaxCount=1, InstanceType="t3.micro", SubnetId=subnet,
                      SecurityGroupIds=[world["web_sg"]],
                      TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "worker"}]}])


def worker_without_route(world):
    private_worker(world)


def worker_igw_without_public_ip(world):
    private_worker(world, route={"GatewayId": world["igw"]})


def worker_nat_port_exhaustion(world):
    ec2 = client(world, "ec2")
    allocation = ec2.allocate_address(Domain="vpc")["AllocationId"]
    nat = ec2.create_nat_gateway(SubnetId=world["subnets"][0], AllocationId=allocation)["NatGateway"]["NatGatewayId"]
    private_worker(world, route={"NatGatewayId": nat})
    put_metric(world, "AWS/NATGateway", "ErrorPortAllocation", 5120, {"NatGatewayId": nat})
    put_metric(world, "AWS/NATGateway", "PacketsDropCount", 830, {"NatGatewayId": nat})


def leak_persistence(world):
    for event in ("CreateUser", "CreateAccessKey", "AttachUserPolicy"):
        act(world, event, source="iam.amazonaws.com")
    act(world, "GetCallerIdentity", read_only=True, source="sts.amazonaws.com")


def leak_mining(world):
    act(world, "RunInstances", region="ap-south-1", times=2)
    act(world, "RunInstances", region="sa-east-1", times=2)
    act(world, "DescribeRegions", read_only=True)


def leak_secrets(world):
    act(world, "GetSecretValue", read_only=True, source="secretsmanager.amazonaws.com", times=2)
    act(world, "PutBucketPolicy", source="s3.amazonaws.com")


def leak_recon_only(world):
    for event in ("ListBuckets", "ListUsers", "DescribeInstances", "ListRoles", "GetAccountAuthorizationDetails",
                  "ListSecrets"):
        act(world, event, read_only=True, error="AccessDenied")


def leak_bots_v3_recon(world):
    """Splunk BOTS v3(Frothly)의 유출 키 web_admin: 11분 동안 IP 세 곳에서 서비스 네 곳을 더듬었고 모두 거부됐다."""
    for event, source, ip, error in (("ListAccessKeys", "iam.amazonaws.com", "209.107.196.112", "AccessDenied"),
                                     ("ListBuckets", "s3.amazonaws.com", "139.198.18.205", "AccessDenied"),
                                     ("DescribeAccountAttributes", "ec2.amazonaws.com", "82.102.18.111",
                                      "Client.UnauthorizedOperation"),
                                     ("GetUser", "iam.amazonaws.com", "82.102.18.111", "AccessDenied")):
        act(world, event, read_only=True, error=error, source=source, ip=ip)


def leak_evasion(world):
    act(world, "StopLogging", source="cloudtrail.amazonaws.com")


def nat_bytes_spike(world):
    spike(world, "EC2 - Other", "APN2-NatGateway-Bytes", 46.0)
    world["cost_changes"].append({"time": _clock(datetime.now(timezone.utc) - timedelta(days=3)),
                                  "event": "CreateNatGateway", "user": "alice", "resource": "nat-0a1b2c3d4e5f"})


def gpu_instances(world):
    spike(world, "Amazon Elastic Compute Cloud - Compute", "APN2-BoxUsage:p3.2xlarge", 146.9)
    world["cost_changes"].append({"time": _clock(datetime.now(timezone.utc) - timedelta(days=3)),
                                  "event": "RunInstances", "user": IAM_USER, "resource": "i-0f00ba4"})


def log_ingestion_spike(world):
    spike(world, "AmazonCloudWatch", "APN2-DataProcessing-Bytes", 38.4)


# ---------------------------------------------------------------- 시나리오
@dataclass(frozen=True)
class Scenario:
    name: str
    service: str                  # diagnoseService의 service
    resource: str                 # diagnoseService의 resource (자리 표시는 fill로 채운다)
    fault: Optional[Callable]     # 심을 장애 (없으면 정상)
    causes: frozenset             # 기대 원인 층
    symptoms: frozenset           # 기대 증상 층 (이것들은 꼭 있어야 한다)
    about: str                    # 심은 장애 (사람이 읽는 설명)
    ask: str                      # 대화창에서 물어볼 말의 예
    target: Optional[str] = None  # VPC 연결의 목적지 "IP:포트"


def _s(name, service, resource, fault, causes, symptoms, about, ask, target=None):
    return Scenario(name, service, resource, fault, frozenset(causes), frozenset(symptoms), about, ask, target)


SCENARIOS = [
    _s("alb-healthy", "alb", ALB, None, [], [], "없음", "web-alb 상태를 진단해 줘"),
    _s("alb-targets-stopped", "alb", ALB, stop_targets, ["L2", "L5"], ["L4"],
       "대상 두 대 중지 + StopInstances 기록", "web-alb에서 503이 나요. 원인 찾아 줘"),
    _s("alb-target-sg-blocked", "alb", ALB, block_target_sg, ["L2", "L3"], [],
       "대상 보안 그룹에서 ALB 출처 규칙 삭제 + 기록", "web-alb가 504를 내요. 왜 그래?"),
    _s("alb-no-registered-targets", "alb", ALB, deregister_targets, ["L4"], [],
       "대상 등록 해제", "web-alb로 들어온 요청이 서버에 안 가요"),
    _s("alb-subnet-without-igw", "alb", ALB, remove_igw_route, ["L3"], [],
       "ALB 서브넷의 인터넷 게이트웨이 경로 삭제", "밖에서 web-alb에 접속이 안 돼요"),
    _s("alb-target-5xx-after-deploy", "alb", ALB, target_5xx_after_deploy, ["L2"], ["L5"],
       "대상 5xx 급증 + 직전 배포(시작 템플릿) 기록", "배포하고 나서 web-alb에서 5xx가 늘었어요"),
    _s("ec2-healthy", "ec2", "web-1", None, [], [], "없음", "web-1 인스턴스 상태를 진단해 줘"),
    _s("ec2-stopped", "ec2", "{id:web-1}", stop_web_1, ["L2", "L5"], ["L4"],
       "인스턴스 중지 + 기록", "web-1에 접속이 안 돼요"),
    _s("ec2-nacl-blocks-ssh", "ec2", "{id:web-1}", nacl_denies_ssh, ["L3"], [],
       "NACL이 보안 그룹이 연 22번을 거부", "web-1에 SSH(22번)가 안 붙어요"),
    _s("ec2-cpu-credits-exhausted", "ec2", "{id:web-1}", exhaust_cpu_credits, ["L5"], [],
       "CPU 크레딧 0 · CPU 100%", "web-1이 갑자기 느려졌어요"),
    _s("lambda-healthy", "lambda", FUNCTION, None, [], [], "없음", "orders-api 함수 상태를 진단해 줘"),
    _s("lambda-reserved-concurrency-zero", "lambda", FUNCTION, zero_reserved_concurrency, ["L2", "L6"], [],
       "예약 동시성 0 + 스로틀 + 기록", "orders-api 호출이 전부 스로틀돼요"),
    _s("lambda-timeout", "lambda", FUNCTION, lambda_timeouts, ["L5"], [],
       "실행 시간이 제한에 닿음 + 'Task timed out'", "orders-api가 자꾸 시간 초과로 실패해요"),
    _s("lambda-vpc-without-nat", "lambda", FUNCTION, lambda_in_vpc_without_nat, ["L3"], ["L5"],
       "NAT 없는 VPC에 붙이고 시간 초과", "orders-api를 VPC에 붙인 뒤로 시간 초과가 나요"),
    _s("lambda-access-denied", "lambda", FUNCTION, lambda_access_denied, ["L6"], ["L5"],
       "로그에 AccessDenied", "orders-api가 오류를 내요. 원인 찾아 줘"),
    _s("s3-private-healthy", "s3", BUCKET, None, [], [], "없음", f"{BUCKET} 버킷이 안전한지 봐 줘"),
    _s("s3-public-policy", "s3", BUCKET, make_public_policy, ["L2", "L6"], [],
       "차단 해제 + 공개 정책 + PutBucketPolicy 기록", f"{BUCKET} 버킷이 공개된 것 같아요"),
    _s("s3-public-acl", "s3", BUCKET, make_public_acl, ["L6"], [],
       "차단 해제 + public-read ACL", f"{BUCKET} 버킷을 아무나 읽을 수 있대요"),
    _s("s3-deny-all-policy", "s3", BUCKET, deny_everyone, ["L6"], [],
       "모든 주체 거부 정책", f"{BUCKET} 버킷에 아무도 접근을 못 해요"),
    _s("s3-public-policy-but-restricted", "s3", BUCKET, make_restricted_public_policy, [], [],
       "공개 정책이지만 RestrictPublicBuckets가 막음 (L6 주의)", f"{BUCKET}에 공개 정책이 있는데 위험한가요?"),
    _s("rds-healthy", "rds", DB, None, [], [], "없음", "orders-db 상태를 진단해 줘"),
    _s("rds-stopped", "rds", DB, stop_db, ["L2", "L5"], [],
       "DB 중지 + StopDBInstance 기록", "orders-db에 연결이 안 돼요"),
    _s("rds-security-group-closed", "rds", DB, close_db_sg, ["L2", "L3"], [],
       "DB 보안 그룹의 3306 규칙 삭제 + 기록", "앱에서 orders-db 3306 연결이 끊겼어요"),
    _s("rds-storage-almost-full", "rds", DB, fill_db_storage, ["L6"], [],
       "여유 저장 공간 0.8GiB / 20GiB", "orders-db 쓰기가 곧 막힐 것 같대요. 확인해 줘"),
    _s("rds-burst-balance-drained", "rds", DB, drain_burst_balance, ["L5"], ["L7"],
       "gp2 버스트 크레딧 0 · 읽기 지연 320ms", "orders-db 쿼리가 갑자기 느려졌어요"),
    _s("vpc-healthy", "vpc", "web-1", None, [], [], "web-1 → web-2:22",
       "web-1에서 web-2({web-2}) 22번으로 연결되는지 확인해 줘", target="{web-2}:22"),
    _s("vpc-destination-port-closed", "vpc", "web-1", None, ["L3"], [], "web-1 → web-2:5432 (목적지 보안 그룹이 막음)",
       "web-1에서 web-2({web-2})의 5432번으로 연결이 안 돼요", target="{web-2}:5432"),
    _s("vpc-nacl-blocks-replies", "vpc", "web-1", nacl_blocks_replies, ["L3"], [], "NACL이 나가는 임시 포트를 거부",
       "web-1에서 web-2({web-2}) 22번 연결이 시간 초과돼요", target="{web-2}:22"),
    _s("vpc-destination-stopped", "vpc", "web-1", stop_web_2, ["L2", "L5"], [], "목적지 중지 + 기록",
       "web-1에서 web-2({web-2}) 22번으로 연결이 안 돼요", target="{web-2}:22"),
    _s("vpc-no-route-to-internet", "vpc", "worker", worker_without_route, ["L3"], [],
       "기본 경로 없는 사설 서브넷 → 인터넷", "worker 인스턴스가 52.95.110.1:443에 못 나가요",
       target="52.95.110.1:443"),
    _s("vpc-igw-without-public-ip", "vpc", "worker", worker_igw_without_public_ip, ["L3"], [],
       "공인 IP 없이 인터넷 게이트웨이 경로", "worker에서 52.95.110.1:443으로 나가는 연결이 안 돼요",
       target="52.95.110.1:443"),
    _s("vpc-nat-port-exhaustion", "vpc", "worker", worker_nat_port_exhaustion, ["L6"], [],
       "NAT ErrorPortAllocation", "worker의 바깥 연결(52.95.110.1:443)이 자꾸 끊겨요", target="52.95.110.1:443"),
    _s("credential-quiet", "credential", "{key}", None, [], [], "키 사용 없음",
       "액세스 키 {key}가 유출된 것 같은데 확인해 줘"),
    _s("credential-persistence", "credential", "{key}", leak_persistence, ["L2", "L6"], [],
       "CreateUser·CreateAccessKey·AttachUserPolicy", "액세스 키 {key}가 유출됐다는 알림이 왔어요"),
    _s("credential-mining", "credential", IAM_USER, leak_mining, ["L2", "L5"], ["L3"],
       "다른 리전 두 곳에 RunInstances", f"{IAM_USER} 사용자 키로 이상한 활동이 있는지 봐 줘"),
    _s("credential-secrets", "credential", "{key}", leak_secrets, ["L2", "L7"], [],
       "GetSecretValue + PutBucketPolicy", "액세스 키 {key}가 GitHub에 올라갔었대요. 무슨 일이 있었는지 봐 줘"),
    _s("credential-recon-only", "credential", "{key}", leak_recon_only, [], ["L6"],
       "권한 거부 6건", "액세스 키 {key}로 거부된 요청이 많대요"),
    _s("credential-logging-stopped", "credential", "{key}", leak_evasion, ["L2"], [],
       "StopLogging", "액세스 키 {key} 사용 기록 좀 확인해 줘"),
    _s("credential-bots-v3-recon", "credential", "{key}", leak_bots_v3_recon, [], ["L3", "L6"],
       "BOTS v3의 유출 키 그대로: IP 3곳 · 거부 4건", "액세스 키 {key}가 여러 IP에서 쓰였대요"),
    _s("cost-flat", "cost", "account", None, [], [], "비용 그대로", "요즘 AWS 비용이 튄 게 있어?"),
    _s("cost-nat-bytes-spike", "cost", "account", nat_bytes_spike, ["L2", "L3"], [],
       "NAT 처리 요금 $0.8 → $46/일 + CreateNatGateway", "최근 며칠 AWS 비용이 확 늘었어요. 원인 찾아 줘"),
    _s("cost-gpu-instances", "cost", "account", gpu_instances, ["L2", "L5"], ["L6"],
       "p3.2xlarge $0 → $147/일 + RunInstances", "이번 주 EC2 비용이 튀었어요"),
    _s("cost-log-ingestion-spike", "cost", "account", log_ingestion_spike, ["L7"], [],
       "CloudWatch 로그 수집 $2.1 → $38/일", "CloudWatch 비용이 갑자기 늘었어요"),
]

BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}
