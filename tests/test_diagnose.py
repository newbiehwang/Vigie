"""서비스별 진단 절차 채점 (mcp/lambda_mcp/diagnose.py, mcp/app.py의 diagnoseService, services/llm/audit.py)

moto 위에 정상인 환경(ALB → EC2 두 대, Lambda 함수, 비공개 버킷)을 만들고, 장애를 하나씩 심은 뒤
절차가 원인 층을 맞히는지 본다. 원인 층이 기대와 정확히 같아야 하고(더 많이 짚어도 틀림), 기대한 증상 층도 있어야 한다.

    시나리오                                  심은 장애                                  원인 층        증상 층
    alb-healthy                               없음                                       없음
    alb-targets-stopped                       대상 두 대 중지 + StopInstances 기록          L2 · L5        L4
    alb-target-sg-blocked                     대상 보안 그룹에서 ALB 출처 규칙 삭제 + 기록   L2 · L3
    alb-no-registered-targets                 대상 등록 해제                              L4
    alb-subnet-without-igw                    ALB 서브넷의 인터넷 게이트웨이 경로 삭제        L3
    alb-target-5xx-after-deploy               대상 5xx 급증 + 직전 배포(시작 템플릿) 기록     L2             L5
    ec2-healthy                               없음                                       없음
    ec2-stopped                               인스턴스 중지 + 기록                          L2 · L5        L4
    ec2-nacl-blocks-ssh                       NACL이 보안 그룹이 연 22번을 거부              L3
    ec2-cpu-credits-exhausted                 CPU 크레딧 0 · CPU 100%                     L5
    lambda-healthy                            없음                                       없음
    lambda-reserved-concurrency-zero          예약 동시성 0 + 스로틀 + 기록                 L2 · L6
    lambda-timeout                            실행 시간이 제한에 닿음 + 'Task timed out'     L5
    lambda-vpc-without-nat                    NAT 없는 VPC에 붙이고 시간 초과                L3             L5
    lambda-access-denied                      로그에 AccessDenied                          L6             L5
    s3-private-healthy                        없음                                       없음
    s3-public-policy                          차단 해제 + 공개 정책 + PutBucketPolicy 기록    L2 · L6
    s3-public-acl                             차단 해제 + public-read ACL                  L6
    s3-deny-all-policy                        모든 주체 거부 정책                           L6
    s3-public-policy-but-restricted           공개 정책이지만 RestrictPublicBuckets가 막음   없음 (L6 주의)
    rds-healthy                               없음                                       없음
    rds-stopped                               DB 중지 + StopDBInstance 기록                 L2 · L5
    rds-security-group-closed                 DB 보안 그룹의 3306 규칙 삭제 + 기록           L2 · L3
    rds-storage-almost-full                   여유 저장 공간 0.8GiB / 20GiB                 L6
    rds-burst-balance-drained                 gp2 버스트 크레딧 0 · 읽기 지연 320ms          L5             L7
    vpc-healthy                               web-1 → web-2:22                            없음
    vpc-destination-port-closed               web-1 → web-2:5432 (목적지 보안 그룹이 막음)    L3
    vpc-nacl-blocks-replies                   NACL이 나가는 임시 포트를 거부                  L3
    vpc-destination-stopped                   목적지 중지 + 기록                             L2 · L5
    vpc-no-route-to-internet                  기본 경로 없는 사설 서브넷 → 인터넷             L3
    vpc-igw-without-public-ip                 공인 IP 없이 인터넷 게이트웨이 경로             L3
    vpc-nat-port-exhaustion                   NAT ErrorPortAllocation                     L6
    credential-quiet                          키 사용 없음                                 없음
    credential-persistence                    CreateUser·CreateAccessKey·AttachUserPolicy   L2 · L6
    credential-mining                         다른 리전 두 곳에 RunInstances                 L2 · L5        L3
    credential-secrets                        GetSecretValue + PutBucketPolicy             L2 · L7
    credential-recon-only                     권한 거부 6건                                 없음           L6
    credential-logging-stopped                StopLogging                                 L2
    credential-bots-v3-recon                  BOTS v3의 유출 키 그대로: IP 3곳 · 거부 4건      없음           L3 · L6
    cost-flat                                 비용 그대로                                   없음
    cost-nat-bytes-spike                      NAT 처리 요금 $0.8 → $46/일 + CreateNatGateway  L2 · L3
    cost-gpu-instances                        p3.2xlarge $0 → $147/일 + RunInstances         L2 · L5        L6
    cost-log-ingestion-spike                  CloudWatch 로그 수집 $2.1 → $38/일              L7

CloudTrail 조회(LookupEvents)는 moto가 지원하지 않아 diagnose.recent_changes·key_activity·account_changes를 가짜로 바꿔
변경 기록과 키 사용 기록을 심는다. Cost Explorer의 날짜별 비용도 moto에 넣을 수 없어 _daily_costs를 가짜로 바꾸고,
응답을 읽는 부분은 test_daily_costs_reads_both_group_keys가 따로 본다.
가짜는 절차가 넘긴 관련 자원 이름·ID에 든 기록만 돌려주므로, 절차가 어떤 자원을 관련 있다고 보는지도 함께 확인한다.
"""
import copy
import io
import json
import time
import zipfile
from datetime import datetime, timedelta, timezone

import boto3
import pytest

from conftest import load_service_module
from test_approvals import ORIGIN, FakeResponse, env  # noqa: F401 (env는 fixture)

REGION = boto3.session.Session().region_name
NOW = datetime.now(timezone.utc)
RECENT = NOW - timedelta(minutes=10)


@pytest.fixture
def diagnose(aws):
    return load_service_module("mcp", "lambda_mcp.diagnose")


def put_metric(namespace, name, value, dimensions, unit="Count"):
    boto3.client("cloudwatch").put_metric_data(Namespace=namespace, MetricData=[
        {"MetricName": name, "Timestamp": RECENT, "Value": value, "Unit": unit,
         "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()]}])


def put_logs(group, lines):
    logs = boto3.client("logs")
    stamp = int(time.time() * 1000) - 5 * 60 * 1000
    logs.put_log_events(logGroupName=group, logStreamName="2026/09/28/[$LATEST]abc",
                        logEvents=[{"timestamp": stamp + index, "message": line} for index, line in enumerate(lines)])


# ---------------------------------------------------------------- 정상인 환경
@pytest.fixture
def world(diagnose, monkeypatch):
    ec2, elb = boto3.client("ec2"), boto3.client("elbv2")
    vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]
    vpc_id = vpc["VpcId"]
    subnets = [s for s in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]][:2]
    # 인터넷 게이트웨이 경로를 기본 라우팅 테이블에 둔다 (moto의 기본 VPC에는 로컬 경로뿐이다)
    igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc_id)
    main = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                                              {"Name": "association.main", "Values": ["true"]}])["RouteTables"][0]
    ec2.create_route(RouteTableId=main["RouteTableId"], DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)

    alb_sg = ec2.create_security_group(GroupName="web-alb", Description="alb", VpcId=vpc_id)["GroupId"]
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

    lb = elb.create_load_balancer(Name="web-alb", Subnets=[s["SubnetId"] for s in subnets],
                                  SecurityGroups=[alb_sg], Scheme="internet-facing")["LoadBalancers"][0]
    tg = elb.create_target_group(Name="web-tg", Protocol="HTTP", Port=80, VpcId=vpc_id,
                                 HealthCheckPath="/health")["TargetGroups"][0]
    elb.register_targets(TargetGroupArn=tg["TargetGroupArn"], Targets=[{"Id": i} for i in instances.values()])
    elb.create_listener(LoadBalancerArn=lb["LoadBalancerArn"], Protocol="HTTP", Port=80,
                        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg["TargetGroupArn"]}])
    lb_dimension = {"LoadBalancer": lb["LoadBalancerArn"].split(":loadbalancer/")[-1]}
    put_metric("AWS/ApplicationELB", "RequestCount", 1200, lb_dimension)
    put_metric("AWS/ApplicationELB", "TargetResponseTime", 0.21, lb_dimension, unit="Seconds")
    for instance_id in instances.values():
        put_metric("AWS/EC2", "CPUUtilization", 35, {"InstanceId": instance_id}, unit="Percent")
        put_metric("AWS/EC2", "CPUCreditBalance", 120, {"InstanceId": instance_id})

    role = boto3.client("iam").create_role(RoleName="orders-api-role", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("handler.py", "def handle(event, context):\n    return {}\n")
    boto3.client("lambda").create_function(FunctionName="orders-api", Runtime="python3.13", Role=role,
                                           Handler="handler.handle", Code={"ZipFile": package.getvalue()},
                                           Timeout=3, MemorySize=256)
    logs = boto3.client("logs")
    logs.create_log_group(logGroupName="/aws/lambda/orders-api")
    logs.create_log_stream(logGroupName="/aws/lambda/orders-api", logStreamName="2026/09/28/[$LATEST]abc")
    put_logs("/aws/lambda/orders-api", ["START RequestId: 1", "REPORT RequestId: 1 Duration: 812.40 ms"])
    put_metric("AWS/Lambda", "Invocations", 500, {"FunctionName": "orders-api"})
    put_metric("AWS/Lambda", "Duration", 812.4, {"FunctionName": "orders-api"}, unit="Milliseconds")

    s3 = boto3.client("s3")
    bucket = "vigie-diag-private"
    s3.create_bucket(Bucket=bucket, **({} if REGION == "us-east-1" else
                                       {"CreateBucketConfiguration": {"LocationConstraint": REGION}}))
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        key: True for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")})
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})

    # RDS: 웹 서버 보안 그룹에서만 3306을 받는 MySQL (Multi-AZ, 백업 7일)
    db_sg = ec2.create_security_group(GroupName="orders-db", Description="db", VpcId=vpc_id)["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=db_sg, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 3306, "ToPort": 3306, "UserIdGroupPairs": [{"GroupId": web_sg}]}])
    boto3.client("rds").create_db_instance(
        DBInstanceIdentifier="orders-db", DBInstanceClass="db.t3.micro", Engine="mysql", AllocatedStorage=20,
        MasterUsername="admin", MasterUserPassword="not-a-real-password", VpcSecurityGroupIds=[db_sg],
        StorageType="gp2", MultiAZ=True, BackupRetentionPeriod=7)
    db_dimension = {"DBInstanceIdentifier": "orders-db"}
    put_metric("AWS/RDS", "CPUUtilization", 22, db_dimension, unit="Percent")
    put_metric("AWS/RDS", "FreeStorageSpace", 15 * 1024 ** 3, db_dimension, unit="Bytes")
    put_metric("AWS/RDS", "FreeableMemory", 400 * 1024 ** 2, db_dimension, unit="Bytes")
    put_metric("AWS/RDS", "BurstBalance", 98, db_dimension, unit="Percent")
    put_metric("AWS/RDS", "DatabaseConnections", 12, db_dimension)

    # 자격 증명: 배포용 IAM 사용자와 키 (키 값은 moto가 실행할 때 만든다)
    iam = boto3.client("iam")
    iam.create_user(UserName="ci-deployer")
    access_key = iam.create_access_key(UserName="ci-deployer")["AccessKey"]["AccessKeyId"]

    trail = []  # 심은 CloudTrail 쓰기 이벤트 (자원 변경)
    activity = []  # 심은 키 사용 기록 (자격 증명)
    cost_changes = []  # 심은 비용을 늘리는 변경
    costs = {"base": {}, "recent": {}}  # (서비스, 사용 유형) → 하루 금액

    def fake_changes(clients, identifiers, start, end):
        wanted = set(identifiers)
        return [event for event in trail if event["resource"] in wanted]

    monkeypatch.setattr(diagnose, "recent_changes", fake_changes)
    monkeypatch.setattr(diagnose, "key_activity",
                        lambda clients, key, start, end: [e for e in activity if e.get("key", access_key) == key])
    monkeypatch.setattr(diagnose, "account_changes", lambda clients, start, end: list(cost_changes))
    monkeypatch.setattr(diagnose, "_daily_costs", lambda ce, start, end: cost_rows(costs))
    for service, usage, daily in BASELINE_COSTS:
        costs["base"][(service, usage)] = daily
        costs["recent"][(service, usage)] = daily
    return {"vpc": vpc_id, "subnets": [s["SubnetId"] for s in subnets], "main_route_table": main["RouteTableId"],
            "alb_sg": alb_sg, "web_sg": web_sg, "db_sg": db_sg, "instances": instances, "lb": lb, "tg": tg,
            "lb_dimension": lb_dimension, "db_dimension": db_dimension, "bucket": bucket, "trail": trail,
            "access_key": access_key, "activity": activity, "cost_changes": cost_changes, "costs": costs,
            "igw": igw, "image": image}


# 비용: 평소의 하루 금액 (서비스, 사용 유형, USD)
BASELINE_COSTS = [
    ("Amazon Elastic Compute Cloud - Compute", "APN2-BoxUsage:t3.micro", 6.2),
    ("EC2 - Other", "APN2-NatGateway-Hours", 1.4),
    ("EC2 - Other", "APN2-NatGateway-Bytes", 0.8),
    ("AmazonCloudWatch", "APN2-DataProcessing-Bytes", 2.1),
    ("Amazon Relational Database Service", "APN2-InstanceUsage:db.t3.micro", 4.9),
    ("Amazon Simple Storage Service", "APN2-TimedStorage-ByteHrs", 0.6),
]


def cost_rows(costs):
    """Cost Explorer의 날짜별 행 (_daily_costs와 같은 모양): 앞 14일은 base, 최근 3일은 recent의 하루 금액."""
    today = datetime.now(timezone.utc).date()
    rows = []
    for back in range(1, 18):
        day = (today - timedelta(days=back)).isoformat()
        amounts = costs["recent"] if back <= 3 else costs["base"]
        rows += [(day, service, usage, amount) for (service, usage), amount in amounts.items()]
    return rows


def change(world, event, resource, user="deploy-bot"):
    world["trail"].append({"time": "09-28 20:55", "event": event, "user": user, "resource": resource})


# ---------------------------------------------------------------- 심을 장애
def stop_targets(world):
    ec2 = boto3.client("ec2")
    ec2.stop_instances(InstanceIds=list(world["instances"].values()))
    for instance_id in world["instances"].values():
        change(world, "StopInstances", instance_id, user="alice")
    put_metric("AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", 340, world["lb_dimension"])
    put_metric("AWS/ApplicationELB", "HTTPCode_ELB_503_Count", 340, world["lb_dimension"])


def block_target_sg(world):
    ec2 = boto3.client("ec2")
    ec2.revoke_security_group_ingress(GroupId=world["web_sg"], IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "UserIdGroupPairs": [{"GroupId": world["alb_sg"]}]}])
    change(world, "RevokeSecurityGroupIngress", world["web_sg"], user="alice")
    put_metric("AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", 120, world["lb_dimension"])
    put_metric("AWS/ApplicationELB", "HTTPCode_ELB_504_Count", 120, world["lb_dimension"])


def deregister_targets(world):
    boto3.client("elbv2").deregister_targets(TargetGroupArn=world["tg"]["TargetGroupArn"],
                                             Targets=[{"Id": i} for i in world["instances"].values()])


def remove_igw_route(world):
    boto3.client("ec2").delete_route(RouteTableId=world["main_route_table"], DestinationCidrBlock="0.0.0.0/0")


def target_5xx_after_deploy(world):
    put_metric("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", 410, world["lb_dimension"])
    for instance_id in world["instances"].values():
        change(world, "CreateLaunchTemplateVersion", instance_id)


def stop_web_1(world):
    boto3.client("ec2").stop_instances(InstanceIds=[world["instances"]["web-1"]])
    change(world, "StopInstances", world["instances"]["web-1"], user="alice")


def nacl_denies_ssh(world):
    ec2 = boto3.client("ec2")
    acl = ec2.describe_network_acls(Filters=[{"Name": "vpc-id", "Values": [world["vpc"]]},
                                             {"Name": "default", "Values": ["true"]}])["NetworkAcls"][0]
    ec2.create_network_acl_entry(NetworkAclId=acl["NetworkAclId"], RuleNumber=50, Protocol="6", RuleAction="deny",
                                 Egress=False, CidrBlock="0.0.0.0/0", PortRange={"From": 22, "To": 22})


def exhaust_cpu_credits(world):
    instance_id = world["instances"]["web-1"]
    put_metric("AWS/EC2", "CPUCreditBalance", 0, {"InstanceId": instance_id})
    put_metric("AWS/EC2", "CPUUtilization", 100, {"InstanceId": instance_id}, unit="Percent")


def zero_reserved_concurrency(world):
    boto3.client("lambda").put_function_concurrency(FunctionName="orders-api", ReservedConcurrentExecutions=0)
    put_metric("AWS/Lambda", "Throttles", 230, {"FunctionName": "orders-api"})
    change(world, "PutFunctionConcurrency20171031", "orders-api", user="alice")


def lambda_timeouts(world):
    put_metric("AWS/Lambda", "Duration", 3000, {"FunctionName": "orders-api"}, unit="Milliseconds")
    put_metric("AWS/Lambda", "Errors", 42, {"FunctionName": "orders-api"})
    put_logs("/aws/lambda/orders-api", ["2026-09-28T11:55:00Z 1 Task timed out after 3.00 seconds",
                                        "REPORT RequestId: 2 Duration: 3000.00 ms Status: timeout"])


def lambda_in_vpc_without_nat(world):
    ec2 = boto3.client("ec2")
    # NAT 없는 사설 서브넷 (인터넷 게이트웨이 경로만 있는 기본 라우팅 테이블도 Lambda에는 쓸모가 없지만,
    # 여기서는 경로가 로컬뿐인 전용 라우팅 테이블을 붙여 흔한 '사설 서브넷' 모양을 만든다)
    subnet = ec2.create_subnet(VpcId=world["vpc"], CidrBlock="172.31.200.0/24")["Subnet"]["SubnetId"]
    table = ec2.create_route_table(VpcId=world["vpc"])["RouteTable"]["RouteTableId"]
    ec2.associate_route_table(RouteTableId=table, SubnetId=subnet)
    boto3.client("lambda").update_function_configuration(
        FunctionName="orders-api", VpcConfig={"SubnetIds": [subnet], "SecurityGroupIds": [world["web_sg"]]})
    lambda_timeouts(world)


def lambda_access_denied(world):
    put_metric("AWS/Lambda", "Errors", 17, {"FunctionName": "orders-api"})
    put_logs("/aws/lambda/orders-api", [
        "[ERROR] ClientError: An error occurred (AccessDeniedException) when calling the GetItem operation: "
        "User: arn:aws:sts::123456789012:assumed-role/orders-api-role/orders-api is not authorized to perform: "
        "dynamodb:GetItem"])


def make_public_policy(world, restrict=False):
    s3 = boto3.client("s3")
    s3.put_public_access_block(Bucket=world["bucket"], PublicAccessBlockConfiguration={
        "BlockPublicAcls": False, "IgnorePublicAcls": False, "BlockPublicPolicy": False,
        "RestrictPublicBuckets": restrict})
    s3.put_bucket_policy(Bucket=world["bucket"], Policy=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Sid": "PublicRead", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
         "Resource": f"arn:aws:s3:::{world['bucket']}/*"}]}))
    if not restrict:
        change(world, "PutBucketPublicAccessBlock", world["bucket"], user="alice")
        change(world, "PutBucketPolicy", world["bucket"], user="alice")


def make_public_acl(world):
    s3 = boto3.client("s3")
    s3.delete_public_access_block(Bucket=world["bucket"])
    s3.put_bucket_acl(Bucket=world["bucket"], ACL="public-read")


def deny_everyone(world):
    boto3.client("s3").put_bucket_policy(Bucket=world["bucket"], Policy=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Sid": "LockDown", "Effect": "Deny", "Principal": "*",
                                                "Action": "s3:*", "Resource": f"arn:aws:s3:::{world['bucket']}/*"}]}))


# (시나리오, 서비스, 자원 → world에서 고른다, 장애, 기대 원인 층, 기대 증상 층)
SCENARIOS = [
    ("alb-healthy", "alb", "web-alb", None, set(), set()),
    ("alb-targets-stopped", "alb", "web-alb", stop_targets, {"L2", "L5"}, {"L4"}),
    ("alb-target-sg-blocked", "alb", "web-alb", block_target_sg, {"L2", "L3"}, set()),
    ("alb-no-registered-targets", "alb", "web-alb", deregister_targets, {"L4"}, set()),
    ("alb-subnet-without-igw", "alb", "web-alb", remove_igw_route, {"L3"}, set()),
    ("alb-target-5xx-after-deploy", "alb", "web-alb", target_5xx_after_deploy, {"L2"}, {"L5"}),
    ("ec2-healthy", "ec2", "web-1", None, set(), set()),
    ("ec2-stopped", "ec2", "web-1", stop_web_1, {"L2", "L5"}, {"L4"}),
    ("ec2-nacl-blocks-ssh", "ec2", "web-1", nacl_denies_ssh, {"L3"}, set()),
    ("ec2-cpu-credits-exhausted", "ec2", "web-1", exhaust_cpu_credits, {"L5"}, set()),
    ("lambda-healthy", "lambda", "orders-api", None, set(), set()),
    ("lambda-reserved-concurrency-zero", "lambda", "orders-api", zero_reserved_concurrency, {"L2", "L6"}, set()),
    ("lambda-timeout", "lambda", "orders-api", lambda_timeouts, {"L5"}, set()),
    ("lambda-vpc-without-nat", "lambda", "orders-api", lambda_in_vpc_without_nat, {"L3"}, {"L5"}),
    ("lambda-access-denied", "lambda", "orders-api", lambda_access_denied, {"L6"}, {"L5"}),
    ("s3-private-healthy", "s3", "vigie-diag-private", None, set(), set()),
    ("s3-public-policy", "s3", "vigie-diag-private", make_public_policy, {"L2", "L6"}, set()),
    ("s3-public-acl", "s3", "vigie-diag-private", make_public_acl, {"L6"}, set()),
    ("s3-deny-all-policy", "s3", "vigie-diag-private", deny_everyone, {"L6"}, set()),
    ("s3-public-policy-but-restricted", "s3", "vigie-diag-private",
     lambda world: make_public_policy(world, restrict=True), set(), set()),
]


def layer(result, layer_id):
    return next(item for item in result["layers"] if item["id"] == layer_id)


@pytest.mark.parametrize("name,service,resource,fault,causes,symptoms", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_procedure_finds_the_planted_cause(diagnose, world, name, service, resource, fault, causes, symptoms):
    if fault:
        fault(world)
    resource = world["instances"].get(resource, resource)  # EC2는 Name 태그 대신 ID로도 한 번씩 확인한다
    result = diagnose.run(service, resource if name != "ec2-healthy" else "web-1", hours=1, region=REGION)
    got = set(result["causes"])
    assert got == causes, f"{name}: 원인 층 {sorted(got)} (기대 {sorted(causes)})\n{result['summary']}"
    assert symptoms <= set(result["symptoms"]), f"{name}: 증상 층 {result['symptoms']} (기대 {sorted(symptoms)})"
    # 요약은 원인이 있으면 원인부터, 없으면 이상 없음이나 증상
    assert result["summary"].startswith("원인 —") == bool(causes)
    # 모든 층이 판정과 근거를 가진다 (해당 없음 말고는 어느 API·지표를 봤는지)
    assert [item["id"] for item in result["layers"]] == ["L1", "L2", "L3", "L4", "L5", "L6", "L7"]
    for item in result["layers"]:
        assert item["status"] in diagnose.WEIGHT and item["component"] and item["checks"]
        for check in item["checks"]:
            assert check["status"] == "skip" or check["evidence"], (item["id"], check)


def test_scenarios_with_details(diagnose, world):
    """채점 말고 찾은 것의 내용도 본다: 사람이 읽을 근거가 맞는 자원을 가리키나."""
    stop_targets(world)
    result = diagnose.run("alb", "web-alb", region=REGION)
    assert "2대 모두" in layer(result, "L5")["finding"] and "web-1" in layer(result, "L5")["finding"]
    assert "Target.InvalidState" in layer(result, "L4")["finding"]
    assert "StopInstances" in layer(result, "L2")["finding"] and "alice" in layer(result, "L2")["finding"]
    assert result["summary"].index("L5") < result["summary"].index("계기 L2")  # 무엇이 고장 났나 → 무엇이 계기였나
    assert layer(result, "L1")["status"] == "ok"  # 시스템 상태 검사 정상 (Health API는 확인 불가 항목으로 남는다)
    assert any(c["status"] == "unknown" and "Health" in c["finding"] for c in layer(result, "L1")["checks"])


def test_blocked_security_group_names_the_target_and_port(diagnose, world):
    block_target_sg(world)
    finding = layer(diagnose.run("alb", "web-alb", region=REGION), "L3")["finding"]
    assert "80" in finding and "web-1" in finding


def test_changes_without_trouble_are_not_blamed(diagnose, world):
    """장애가 없으면 변경이 있어도 L2는 정상이다 (계기로 보지 않는다)."""
    change(world, "ModifyInstanceAttribute", world["instances"]["web-1"])
    result = diagnose.run("ec2", world["instances"]["web-1"], region=REGION)
    assert result["causes"] == [] and layer(result, "L2")["status"] == "ok" and "1건" in layer(result, "L2")["finding"]


def test_cloudtrail_unavailable_is_unknown_not_ok(diagnose, world, monkeypatch):
    def fail(*_):
        raise RuntimeError("AccessDenied")
    monkeypatch.setattr(diagnose, "recent_changes", fail)
    assert layer(diagnose.run("s3", world["bucket"], region=REGION), "L2")["status"] == "unknown"


def test_ec2_grace_period_zero_is_a_warning(diagnose, world, monkeypatch):
    ec2, autoscaling = boto3.client("ec2"), boto3.client("autoscaling")
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    autoscaling.create_launch_configuration(LaunchConfigurationName="web-lc", ImageId=image, InstanceType="t3.micro")
    autoscaling.create_auto_scaling_group(AutoScalingGroupName="web-asg", LaunchConfigurationName="web-lc",
                                          MinSize=1, MaxSize=2, VPCZoneIdentifier=world["subnets"][0],
                                          HealthCheckType="ELB", HealthCheckGracePeriod=0)
    instance_id = autoscaling.describe_auto_scaling_groups()["AutoScalingGroups"][0]["Instances"][0]["InstanceId"]
    # moto는 명시한 0초를 무시하고 300초로 저장한다. 실제 API처럼 0초를 돌려주도록 응답만 고친다
    clients = diagnose.Clients(REGION)
    groups = clients("autoscaling")
    original = groups.describe_auto_scaling_groups

    def zero_grace(**kwargs):
        response = original(**kwargs)
        for group in response["AutoScalingGroups"]:
            group["HealthCheckGracePeriod"] = 0
        return response

    monkeypatch.setattr(groups, "describe_auto_scaling_groups", zero_grace)
    result = diagnose.run("ec2", instance_id, region=REGION, clients=clients)
    assert any(c["status"] == "warn" and "유예" in c["finding"] for c in layer(result, "L5")["checks"])
    assert layer(result, "L6")["status"] == "ok"  # 그룹에 실패한 활동이 없다


@pytest.mark.parametrize("service,resource,message", [
    ("mq", "broker-1", "진단할 수 있는 서비스"),
    ("alb", "no-such-alb", "로드 밸런서가 없습니다"),
    ("ec2", "i-0123456789abcdef0", "인스턴스가 없습니다"),
    ("lambda", "no-such-function", "함수가 없습니다"),
    ("ec2", "", "resource"),
])
def test_bad_input_is_a_clear_error(diagnose, world, service, resource, message):
    with pytest.raises(ValueError, match=message):
        diagnose.run(service, resource, region=REGION)


# ---------------------------------------------------------------- RDS · VPC 연결 · 자격 증명 · 비용
def stop_db(world):
    boto3.client("rds").stop_db_instance(DBInstanceIdentifier="orders-db")
    change(world, "StopDBInstance", "orders-db", user="alice")


def close_db_sg(world):
    boto3.client("ec2").revoke_security_group_ingress(GroupId=world["db_sg"], IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 3306, "ToPort": 3306, "UserIdGroupPairs": [{"GroupId": world["web_sg"]}]}])
    change(world, "RevokeSecurityGroupIngress", world["db_sg"], user="alice")


def fill_db_storage(world):
    put_metric("AWS/RDS", "FreeStorageSpace", 0.8 * 1024 ** 3, world["db_dimension"], unit="Bytes")


def drain_burst_balance(world):
    put_metric("AWS/RDS", "BurstBalance", 0, world["db_dimension"], unit="Percent")
    put_metric("AWS/RDS", "ReadLatency", 0.32, world["db_dimension"], unit="Seconds")


def stop_web_2(world):
    boto3.client("ec2").stop_instances(InstanceIds=[world["instances"]["web-2"]])
    change(world, "StopInstances", world["instances"]["web-2"], user="alice")


def nacl_blocks_replies(world):
    ec2 = boto3.client("ec2")
    acl = ec2.describe_network_acls(Filters=[{"Name": "vpc-id", "Values": [world["vpc"]]},
                                             {"Name": "default", "Values": ["true"]}])["NetworkAcls"][0]
    ec2.create_network_acl_entry(NetworkAclId=acl["NetworkAclId"], RuleNumber=50, Protocol="6", RuleAction="deny",
                                 Egress=True, CidrBlock="0.0.0.0/0", PortRange={"From": 1024, "To": 65535})


def private_worker(world, *, route=None, public_ip=False):
    """사설 서브넷(전용 라우팅 테이블)에 worker 인스턴스 하나. route: 0.0.0.0/0의 대상 {'NatGatewayId': …} 등."""
    ec2 = boto3.client("ec2")
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
    ec2 = boto3.client("ec2")
    allocation = ec2.allocate_address(Domain="vpc")["AllocationId"]
    nat = ec2.create_nat_gateway(SubnetId=world["subnets"][0], AllocationId=allocation)["NatGateway"]["NatGatewayId"]
    private_worker(world, route={"NatGatewayId": nat})
    put_metric("AWS/NATGateway", "ErrorPortAllocation", 5120, {"NatGatewayId": nat})
    put_metric("AWS/NATGateway", "PacketsDropCount", 830, {"NatGatewayId": nat})


def act(world, event, *, read_only=False, error="", region="", source="", ip="198.51.100.23", times=1):
    """키 사용 기록 하나를 심는다 (key_activity가 돌려주는 모양)."""
    for _ in range(times):
        world["activity"].append({"time": "09-28 03:12", "event": event, "source": source or "ec2.amazonaws.com",
                                  "ip": ip, "agent": "aws-cli/2.17.0", "region": region or REGION,
                                  "error": error, "read_only": read_only})


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


def spike(world, service, usage, recent):
    world["costs"]["recent"][(service, usage)] = recent


def nat_bytes_spike(world):
    spike(world, "EC2 - Other", "APN2-NatGateway-Bytes", 46.0)
    world["cost_changes"].append({"time": "09-25 14:02", "event": "CreateNatGateway", "user": "alice",
                                  "resource": "nat-0a1b2c3d4e5f"})


def gpu_instances(world):
    spike(world, "Amazon Elastic Compute Cloud - Compute", "APN2-BoxUsage:p3.2xlarge", 146.9)
    world["cost_changes"].append({"time": "09-25 02:40", "event": "RunInstances", "user": "ci-deployer",
                                  "resource": "i-0f00ba4"})


def log_ingestion_spike(world):
    spike(world, "AmazonCloudWatch", "APN2-DataProcessing-Bytes", 38.4)


# (시나리오, 서비스, 자원, 목적지, 장애, 기대 원인 층, 기대 증상 층). 자원·목적지의 {web-2} 따위는 world에서 채운다
SCENARIOS_MORE = [
    ("rds-healthy", "rds", "orders-db", None, None, set(), set()),
    ("rds-stopped", "rds", "orders-db", None, stop_db, {"L2", "L5"}, set()),
    ("rds-security-group-closed", "rds", "orders-db", None, close_db_sg, {"L2", "L3"}, set()),
    ("rds-storage-almost-full", "rds", "orders-db", None, fill_db_storage, {"L6"}, set()),
    ("rds-burst-balance-drained", "rds", "orders-db", None, drain_burst_balance, {"L5"}, {"L7"}),
    ("vpc-healthy", "vpc", "web-1", "{web-2}:22", None, set(), set()),
    ("vpc-destination-port-closed", "vpc", "web-1", "{web-2}:5432", None, {"L3"}, set()),
    ("vpc-nacl-blocks-replies", "vpc", "web-1", "{web-2}:22", nacl_blocks_replies, {"L3"}, set()),
    ("vpc-destination-stopped", "vpc", "web-1", "{web-2}:22", stop_web_2, {"L2", "L5"}, set()),
    ("vpc-no-route-to-internet", "vpc", "worker", "52.95.110.1:443", worker_without_route, {"L3"}, set()),
    ("vpc-igw-without-public-ip", "vpc", "worker", "52.95.110.1:443", worker_igw_without_public_ip, {"L3"}, set()),
    ("vpc-nat-port-exhaustion", "vpc", "worker", "52.95.110.1:443", worker_nat_port_exhaustion, {"L6"}, set()),
    ("credential-quiet", "credential", "{key}", None, None, set(), set()),
    ("credential-persistence", "credential", "{key}", None, leak_persistence, {"L2", "L6"}, set()),
    ("credential-mining", "credential", "ci-deployer", None, leak_mining, {"L2", "L5"}, {"L3"}),
    ("credential-secrets", "credential", "{key}", None, leak_secrets, {"L2", "L7"}, set()),
    ("credential-recon-only", "credential", "{key}", None, leak_recon_only, set(), {"L6"}),
    ("credential-logging-stopped", "credential", "{key}", None, leak_evasion, {"L2"}, set()),
    ("credential-bots-v3-recon", "credential", "{key}", None, leak_bots_v3_recon, set(), {"L3", "L6"}),
    ("cost-flat", "cost", "account", None, None, set(), set()),
    ("cost-nat-bytes-spike", "cost", "account", None, nat_bytes_spike, {"L2", "L3"}, set()),
    ("cost-gpu-instances", "cost", "account", None, gpu_instances, {"L2", "L5"}, {"L6"}),
    ("cost-log-ingestion-spike", "cost", "account", None, log_ingestion_spike, {"L7"}, set()),
]


def fill(text, world):
    if text is None:
        return None
    ips = {name: boto3.client("ec2").describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
           ["PrivateIpAddress"] for name, instance_id in world["instances"].items()}
    return text.replace("{key}", world["access_key"]).replace("{web-2}", ips["web-2"])


@pytest.mark.parametrize("name,service,resource,target,fault,causes,symptoms", SCENARIOS_MORE,
                         ids=[s[0] for s in SCENARIOS_MORE])
def test_more_procedures_find_the_planted_cause(diagnose, world, name, service, resource, target, fault, causes,
                                                symptoms):
    if fault:
        fault(world)
    result = diagnose.run(service, fill(resource, world), region=REGION, target=fill(target, world))
    got = set(result["causes"])
    assert got == causes, f"{name}: 원인 층 {sorted(got)} (기대 {sorted(causes)})\n{result['summary']}"
    assert symptoms <= set(result["symptoms"]), f"{name}: 증상 층 {result['symptoms']} (기대 {sorted(symptoms)})"
    assert result["summary"].startswith("원인 —") == bool(causes)
    for item in result["layers"]:
        assert item["status"] in diagnose.WEIGHT and item["component"] and item["checks"]
        for check in item["checks"]:
            assert check["status"] == "skip" or check["evidence"], (item["id"], check)


def test_logging_stopped_alone_reads_as_the_cause(diagnose, world):
    leak_evasion(world)
    assert diagnose.run("credential", world["access_key"], region=REGION)["summary"] == (
        "원인 — L2 변경: 감사 기록·탐지를 끄려 했습니다: StopLogging 1건")


def test_credential_findings_name_what_to_do(diagnose, world):
    leak_persistence(world)
    result = diagnose.run("credential", world["access_key"], region=REGION)
    assert result["hours"] == 24  # 자격 증명은 하루를 본다
    l6 = layer(result, "L6")
    assert "CreateUser" in l6["finding"] and "CreateAccessKey" in l6["finding"]
    assert any("비활성화" in c["finding"] for c in l6["checks"])  # 지우지 말고 비활성화
    assert any(c["status"] == "unknown" and "GetObject" in c["finding"] for c in layer(result, "L7")["checks"])


def test_vpc_needs_a_target_and_explains_same_subnet(diagnose, world):
    with pytest.raises(ValueError, match="target"):
        diagnose.run("vpc", "web-1", region=REGION)
    with pytest.raises(ValueError, match="DNS"):
        diagnose.run("vpc", "web-1", region=REGION, target="db.internal:5432")
    result = diagnose.run("vpc", "web-1", region=REGION, target=fill("{web-2}:22", world))
    assert result["target"].endswith(":22") and layer(result, "L6")["status"] == "skip"


def test_cost_ignores_hours_and_filters_by_service(diagnose, world):
    gpu_instances(world)
    log_ingestion_spike(world)
    result = diagnose.run("cost", "Compute", hours=1, region=REGION)
    assert result["hours"] == 72 and set(result["causes"]) == {"L2", "L5"}  # CloudWatch 급증은 거른 서비스 밖
    assert "p3.2xlarge" in layer(result, "L5")["finding"]
    with pytest.raises(ValueError, match="account"):
        diagnose.run("cost", "Amazon Bedrock", region=REGION)


@pytest.mark.parametrize("service,usage,expected", [
    ("EC2 - Other", "APN2-NatGateway-Bytes", "L3"),
    ("Amazon Elastic Compute Cloud - Compute", "APN2-DataTransfer-Out-Bytes", "L3"),
    ("Amazon Elastic Load Balancing", "APN2-LCUUsage", "L4"),
    ("Amazon Elastic Compute Cloud - Compute", "APN2-BoxUsage:c5.large", "L5"),
    ("AWS Lambda", "APN2-Lambda-GB-Second", "L5"),
    ("EC2 - Other", "APN2-EBS:VolumeUsage.gp3", "L7"),
    ("AmazonCloudWatch", "APN2-DataProcessing-Bytes", "L7"),
    ("Amazon Simple Storage Service", "APN2-Requests-Tier1", "L7"),
])
def test_cost_layer_of_usage_types(diagnose, service, usage, expected):
    assert diagnose.cost_layer(service, usage) == expected


def test_daily_costs_reads_both_group_keys(diagnose):
    class FakeCostExplorer:
        def __init__(self):
            self.calls = []

        def get_cost_and_usage(self, **kwargs):
            self.calls.append(kwargs)
            if "NextPageToken" not in kwargs:
                return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-09-20"}, "Groups": [
                    {"Keys": ["AWS Lambda", "APN2-Request"], "Metrics": {"UnblendedCost": {"Amount": "1.25"}}}]}],
                        "NextPageToken": "p2"}
            return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-09-21"}, "Groups": []}]}

    ce = FakeCostExplorer()
    assert diagnose._daily_costs(ce, "2026-09-10", "2026-09-28") == [("2026-09-20", "AWS Lambda", "APN2-Request", 1.25)]
    assert len(ce.calls) == 2 and [g["Key"] for g in ce.calls[0]["GroupBy"]] == ["SERVICE", "USAGE_TYPE"]


def test_nacl_rules_are_evaluated_in_order(diagnose):
    acl = {"Entries": [
        {"RuleNumber": 100, "Protocol": "-1", "RuleAction": "allow", "Egress": False, "CidrBlock": "0.0.0.0/0"},
        {"RuleNumber": 90, "Protocol": "6", "RuleAction": "deny", "Egress": False, "CidrBlock": "10.0.0.0/8",
         "PortRange": {"From": 443, "To": 443}},
        {"RuleNumber": 32767, "Protocol": "-1", "RuleAction": "deny", "Egress": False, "CidrBlock": "0.0.0.0/0"}]}
    assert diagnose.nacl_decision(acl, False, 443, "10.1.2.3") == (False, 90)
    assert diagnose.nacl_decision(acl, False, 443, "203.0.113.10") == (True, 100)
    assert diagnose.nacl_decision(acl, True, 443, "10.1.2.3") == (False, None)  # 나가는 규칙이 없으면 거부


# ---------------------------------------------------------------- MCP 도구와 감사 로그
def admin_run(env, monkeypatch, replies):  # noqa: F811
    """관리자의 질문 하나: 가짜 모델이 replies대로 도구를 부르고, 도구는 실제 MCP 서버(moto)가 실행한다."""
    llm = env["llm"]
    import mcp_anthropic_client
    sent = []

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        return FakeResponse({"usage": {}, **replies[len(sent) - 1]})

    monkeypatch.setattr(mcp_anthropic_client.HTTP, "post", fake_post)
    client = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                     model_id="claude-sonnet-5")
    client.tools = json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]
    monkeypatch.setattr(client.mcp_client, "call_tool", env["mcp"].call_tool)
    monkeypatch.setattr(llm, "get_client", lambda *_: client)
    return json.loads(llm.handle_llm1_with_mcp({"text": "orders-api가 왜 실패해?"}, ORIGIN, caller_id="carol",
                                               caller_groups=["admins"])["body"])


def test_diagnose_tool_is_read_only_and_for_admins(env):  # noqa: F811
    tools = {t["name"]: t for t in json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]}
    meta = tools["diagnoseService"]["_meta"]
    assert meta["vigie/risk"] == "read" and meta["vigie/access"] == "admin"
    assert set(tools["diagnoseService"]["inputSchema"]["required"]) == {"service", "resource"}
    refused = env["mcp"].call_tool("diagnoseService", {"service": "s3", "resource": "x"})  # 관리자 표시 없음
    assert refused.get("isError") is True


def test_diagnosis_is_kept_in_the_audit_row(env, monkeypatch):  # noqa: F811
    boto3.client("s3").create_bucket(Bucket="open-bucket", ACL="public-read")
    call = {"type": "tool_use", "id": "toolu_diag", "name": "diagnoseService",
            "input": {"service": "s3", "resource": "open-bucket"}}
    admin_run(env, monkeypatch, [{"content": [call], "stop_reason": "tool_use"},
                                 {"content": [{"type": "text", "text": "ACL이 공개입니다."}], "stop_reason": "end_turn"}])
    items = env["audit"].query(KeyConditionExpression="userId = :u", ExpressionAttributeValues={":u": "carol"})["Items"]
    row = next(i for i in items if i.get("tool") == "diagnoseService")
    diagnosis = row["diagnosis"]
    assert diagnosis["service"] == "s3" and diagnosis["resource"] == "open-bucket" and diagnosis["causes"] == ["L6"]
    assert [item["id"] for item in diagnosis["layers"]] == ["L1", "L2", "L3", "L4", "L5", "L6", "L7"]
    l6 = next(item for item in diagnosis["layers"] if item["id"] == "L6")
    assert l6["status"] == "cause" and "AllUsers" in l6["finding"] and l6["component"]
    assert all({"name", "status", "finding", "evidence"} <= set(c) for c in l6["checks"])
    # 다른 도구 행에는 없다
    assert all("diagnosis" not in i for i in items if i.get("tool") not in (None, "diagnoseService"))


def test_diagnosis_of_ignores_other_tools_and_errors():
    audit = load_service_module("services/llm", "audit")
    text = json.dumps({"status": "error", "message": "버킷이 없습니다"})
    assert audit.diagnosis_of("diagnoseService", {"content": [{"type": "text", "text": text}]}) is None
    assert audit.diagnosis_of("listS3Buckets", {"content": [{"type": "text", "text": "{}"}]}) is None
    assert audit.diagnosis_of("diagnoseService", "not json") is None
    # VPC 연결은 목적지도 남긴다
    body = {"status": "success", "service": "vpc", "service_name": "VPC 연결", "resource": "web-1",
            "target": "10.0.2.15:5432", "hours": 1, "summary": "s", "causes": ["L3"], "layers": []}
    kept = audit.diagnosis_of("diagnoseService", {"content": [{"type": "text", "text": json.dumps(body)}]})
    assert kept["target"] == "10.0.2.15:5432" and kept["serviceName"] == "VPC 연결"
