"""Vigie MCP 서버 (Lambda Function URL, Streamable HTTP)

도구는 두 곳에서 온다.
1. AWS 공식 MCP 서버 (awslabs, lambda_mcp/official.py가 이 서버에 붙인다)
   - CloudWatch: 로그 그룹 조회, Logs Insights 쿼리, 로그 이상 탐지, 메트릭 조회·분석, 알람·알람 기록
   - AWS 문서: 문서 검색·읽기·추천
   - 비용: Cost Explorer (billing-cost-management 서버의 Cost Explorer 부분만)
   - CloudTrail: 최근 90일 관리 이벤트 조회 (누가 언제 어떤 API를 불렀나. 유료인 CloudTrail Lake 도구는 뺐다)
   - Pricing: 공개 가격표 조회 (이 설정이면 월 얼마인지. 로컬 파일을 읽거나 쓰는 도구는 뺐다)
   - IAM: 사용자·역할·그룹·정책 조회와 권한 시뮬레이션 (읽기 전용. 변경 도구는 뺐다)
   - 네트워크: VPC·서브넷·보안 그룹·NACL·라우팅·ENI 조회, VPC 흐름 로그, 경로 추적 (모두 조회)
3. S3 조회 도구 (공식 S3 서버가 없다. S3 Tables 서버는 일반 버킷용이 아니다)
   - 버킷 목록, 버킷 보안 점검, 버킷 크기·객체 수(CloudWatch 지표), 객체 목록(이름·크기·날짜)
   - 객체 내용은 읽지 않는다: 데이터가 계정 밖(Claude)으로 나가지 않게. IAM도 s3:GetObject를 명시적으로 거부한다
4. EC2 조회 도구 (공식 EC2 서버가 없다)
   - 인스턴스 목록, CPU 사용률 순위, 상태 검사, 비용 낭비 찾기(연결 안 된 볼륨·오래 멈춘 인스턴스·쓰지 않는 탄력적 IP)
   - 사용자 데이터·콘솔 출력·Windows 암호는 읽지 않는다 (비밀 값이 흔히 들어 있다). IAM도 명시적으로 거부한다
2. 이 파일에 직접 둔 도구 (공식 서버가 없거나 폐기된 것)
   - CloudWatch 대시보드 목록·요약 (공식 CloudWatch 서버에 대시보드 도구가 없다)
   - 아키텍처 다이어그램 (공식 diagram 서버는 PyPI에서 폐기되었다. 폐기 전 공식 서버를 옮겨 온 코드)
   - 차트 (AntV 차트 서비스, AWS와 무관)
   - 변경 도구 4개 (사람이 승인해야 실행된다)
     로그 보존 기간, 알람 알림 켜기·끄기: 이 환경의 Vigie 리소스만
     EC2 인스턴스 중지·시작: 이 리전의 모든 인스턴스 (통제는 사람의 승인과 MCP의 승인 재확인이 한다)
     S3 퍼블릭 액세스 차단 켜기: 보안을 강화하는 방향만 (끄는 도구는 없다)

변경 도구의 통제 (lambda_mcp/risk.py, lambda_mcp/approval.py)
- tools/list가 도구마다 위험도를 붙인다. LLM Lambda는 변경 도구를 바로 실행하지 않고 승인 요청을 만든다.
- 사용자가 승인하면 LLM Lambda가 작업 ID를 붙여 부르고, 이 서버가 승인 테이블을 직접 다시 확인한 뒤 한 번만 실행한다.
- 도구 안에서도 이름으로 이 환경의 Vigie 리소스인지 확인하고, IAM도 같은 범위(vigie-*)로만 허용한다.
"""
import os
import re
import json
import boto3
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from botocore.exceptions import ClientError
from lambda_mcp.approval import ApprovalGate
from lambda_mcp.lambda_mcp import LambdaMCPServer
from lambda_mcp.official import OfficialTools
from lambda_mcp.diagram_utils import (
    generate_diagram,
    get_diagram_examples,
    list_diagram_icons
)
from lambda_mcp.mcp_types import DiagramType
from lambda_mcp.chart_utils import generate_chart_url, validate_chart_data


# Get session table name from environment variable
session_table = os.environ.get('MCP_SESSION_TABLE', f'vigie-mcp-sessions-{os.environ.get("ENV", "dev")}')
aws_region = os.environ.get("AWS_REGION", "ap-northeast-2")

cloudwatch_client = boto3.client('cloudwatch', region_name=aws_region)
logs_client = boto3.client('logs', region_name=aws_region)
environment = os.environ.get("ENV", "dev")

# Initialize the MCP server
mcp_server = LambdaMCPServer(name="cloudguard", version="1.0.0", session_table=session_table)
# 공식 서버는 여기서 불러오지 않고 도구 목록이 처음 필요할 때 불러온다 (콜드 스타트, lambda_mcp/official.py)
mcp_server.attach(OfficialTools.default())
# 변경 도구 실행 직전의 승인 재확인. 테이블 이름이 없으면(로컬 등) 변경 도구는 실행되지 않는다
_pending_table_name = os.environ.get("PENDING_ACTIONS_TABLE")
if _pending_table_name:
    mcp_server.approval_gate = ApprovalGate(boto3.resource("dynamodb", region_name=aws_region).Table(_pending_table_name))


@mcp_server.tool()
def list_cloudwatch_dashboards() -> Dict[str, Any]:
    """
    Lists all CloudWatch dashboards in the AWS account.

    Returns:
        A dictionary containing the list of dashboard names and their ARNs.
    """
    try:
        dashboards = []
        paginator = cloudwatch_client.get_paginator('list_dashboards')
        for page in paginator.paginate():
            for entry in page.get('DashboardEntries', []):
                dashboards.append({
                    'DashboardName': entry.get('DashboardName'),
                    'DashboardArn': entry.get('DashboardArn')
                })

        return {
            'status': 'success',
            'dashboard_count': len(dashboards),
            'dashboards': dashboards
        }

    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@mcp_server.tool()
def get_dashboard_summary(dashboard_name: str) -> Dict[str, Any]:
    """
    Retrieves and summarizes the configuration of a specified CloudWatch dashboard.

    Args:
        dashboard_name: The name of the CloudWatch dashboard.

    Returns:
        A summary of the dashboard's widgets and their configurations.
    """
    try:
        # Fetch the dashboard configuration
        response = cloudwatch_client.get_dashboard(DashboardName=dashboard_name)
        dashboard_body = response.get('DashboardBody', '{}')
        dashboard_config = json.loads(dashboard_body)

        # Summarize the widgets in the dashboard
        widgets_summary = []
        for widget in dashboard_config.get('widgets', []):
            widget_summary = {
                'type': widget.get('type'),
                'x': widget.get('x'),
                'y': widget.get('y'),
                'width': widget.get('width'),
                'height': widget.get('height'),
                'properties': widget.get('properties', {})
            }
            widgets_summary.append(widget_summary)

        return {
            'dashboard_name': dashboard_name,
            'widgets_count': len(widgets_summary),
            'widgets_summary': widgets_summary
        }

    except Exception as e:
        return {'status': 'error', 'message': str(e)}


# ---------------------------------------------------------------- S3 조회 (객체 내용은 읽지 않는다)
# 버킷 이름·설정·크기·객체 이름만 본다. 객체 내용(GetObject)은 코드에서 읽지 않고, IAM에서도 명시적으로 거부한다
# (llm.yaml). 계정 밖(Claude)으로 나가는 것은 메타데이터뿐이다.

S3_OBJECT_LIMIT = 100  # 객체 목록 한 번에 최대
S3_AUDIT_LIMIT = 50  # 버킷을 지정하지 않은 보안 점검에서 볼 최대 버킷 수
_s3_clients: Dict[str, Any] = {}
_cloudwatch_clients: Dict[str, Any] = {}


def _s3(region: Optional[str] = None):
    """리전별 S3 클라이언트 (버킷 설정 조회는 버킷이 있는 리전으로 보내야 한다)."""
    region = region or aws_region
    if region not in _s3_clients:
        _s3_clients[region] = boto3.client('s3', region_name=region)
    return _s3_clients[region]


def _cloudwatch(region: str):
    """리전별 CloudWatch 클라이언트 (S3 지표는 버킷이 있는 리전에 쌓인다)."""
    if region == aws_region:
        return cloudwatch_client
    if region not in _cloudwatch_clients:
        _cloudwatch_clients[region] = boto3.client('cloudwatch', region_name=region)
    return _cloudwatch_clients[region]


def _bucket_region(bucket_name: str) -> str:
    """버킷의 리전. HeadBucket 응답 헤더(x-amz-bucket-region)는 어느 리전 버킷이든 실제 리전을 알려 준다
    (GetBucketLocation은 한 리전을 빈 값으로 돌려줘 리전 이름을 코드에 적어야 한다)."""
    try:
        response = _s3().head_bucket(Bucket=bucket_name)
        headers = response.get('ResponseMetadata', {}).get('HTTPHeaders', {})
    except ClientError as error:  # 다른 리전 버킷이면 301과 함께 리전을 알려 준다
        headers = error.response.get('ResponseMetadata', {}).get('HTTPHeaders', {})
        if 'x-amz-bucket-region' not in headers:
            raise
    return headers.get('x-amz-bucket-region') or aws_region


def _error_code(error: ClientError) -> str:
    return error.response.get('Error', {}).get('Code', '')


@mcp_server.tool()
def list_s3_buckets() -> Dict[str, Any]:
    """
    Lists the S3 buckets in this AWS account with their region and creation date. Object contents are never read.

    Returns:
        The buckets (name, region, creation date).
    """
    try:
        buckets = []
        for page in _s3().get_paginator('list_buckets').paginate():
            for bucket in page.get('Buckets', []):
                buckets.append({'name': bucket['Name'],
                                'region': bucket.get('BucketRegion') or _bucket_region(bucket['Name']),
                                'created': bucket.get('CreationDate')})
        return {'status': 'success', 'bucket_count': len(buckets), 'buckets': buckets}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def _security_of(bucket_name: str) -> Dict[str, Any]:
    """버킷 하나의 보안 설정과 눈여겨볼 점 (조회만)."""
    region = _bucket_region(bucket_name)
    s3 = _s3(region)
    result: Dict[str, Any] = {'bucket': bucket_name, 'region': region}
    findings: List[str] = []

    try:
        block = s3.get_public_access_block(Bucket=bucket_name)['PublicAccessBlockConfiguration']
    except ClientError as error:
        if _error_code(error) != 'NoSuchPublicAccessBlockConfiguration':
            raise
        block = {}
    result['public_access_block'] = block
    off = [key for key in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')
           if not block.get(key)]
    if off:
        findings.append(f"퍼블릭 액세스 차단이 꺼진 항목: {', '.join(off)} (계정 수준 설정은 따로 확인)")

    try:
        result['policy_is_public'] = bool(
            s3.get_bucket_policy_status(Bucket=bucket_name).get('PolicyStatus', {}).get('IsPublic', False))
    except ClientError as error:
        if _error_code(error) != 'NoSuchBucketPolicy':
            raise
        result['policy_is_public'] = False  # 버킷 정책이 없다
    if result['policy_is_public']:
        findings.append("버킷 정책이 공개(퍼블릭)입니다")

    try:
        rules = s3.get_bucket_encryption(Bucket=bucket_name)['ServerSideEncryptionConfiguration']['Rules']
        result['encryption'] = [rule.get('ApplyServerSideEncryptionByDefault', {}).get('SSEAlgorithm') for rule in rules]
    except ClientError as error:
        if _error_code(error) != 'ServerSideEncryptionConfigurationNotFoundError':
            raise
        result['encryption'] = []
    if not result['encryption']:
        findings.append("기본 암호화 설정이 없습니다")

    versioning = s3.get_bucket_versioning(Bucket=bucket_name)
    result['versioning'] = versioning.get('Status', 'Disabled')
    result['mfa_delete'] = versioning.get('MFADelete', 'Disabled')
    if result['versioning'] != 'Enabled':
        findings.append("버전 관리가 꺼져 있습니다 (실수로 지우거나 덮어쓰면 되돌릴 수 없음)")

    try:
        ownership = s3.get_bucket_ownership_controls(Bucket=bucket_name)['OwnershipControls']['Rules']
        result['object_ownership'] = ownership[0].get('ObjectOwnership') if ownership else None
    except ClientError as error:
        if _error_code(error) != 'OwnershipControlsNotFoundError':
            raise
        result['object_ownership'] = None
    if result['object_ownership'] != 'BucketOwnerEnforced':
        findings.append("ACL이 꺼져 있지 않습니다 (객체 소유권이 BucketOwnerEnforced가 아님)")

    try:
        result['lifecycle_rules'] = len(s3.get_bucket_lifecycle_configuration(Bucket=bucket_name).get('Rules', []))
    except ClientError as error:
        if _error_code(error) != 'NoSuchLifecycleConfiguration':
            raise
        result['lifecycle_rules'] = 0

    result['findings'] = findings
    return result


@mcp_server.tool()
def check_s3_bucket_security(bucket_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Checks the security settings of an S3 bucket: public access block, whether the bucket policy is public, default
    encryption, versioning (and MFA delete), object ownership (ACLs disabled) and lifecycle rules, with a list of
    findings. Omit bucket_name to check every bucket in the account (useful for "do we have any public buckets?").
    Object contents are never read.

    Args:
        bucket_name: Bucket to check. Omit to check all buckets (up to 50).

    Returns:
        The settings and findings for each bucket.
    """
    try:
        names = [bucket_name] if bucket_name else [
            bucket['Name'] for page in _s3().get_paginator('list_buckets').paginate()
            for bucket in page.get('Buckets', [])]
        checked, skipped = [], []
        for name in names[:S3_AUDIT_LIMIT]:
            try:
                checked.append(_security_of(name))
            except ClientError as error:  # 권한이 없거나 막힌 버킷은 건너뛰고 이유를 남긴다
                skipped.append({'bucket': name, 'error': _error_code(error) or str(error)})
        return {'status': 'success', 'checked': len(checked), 'total_buckets': len(names),
                'buckets_with_findings': sum(1 for bucket in checked if bucket['findings']),
                'buckets': checked, 'skipped': skipped,
                **({'note': f'버킷이 많아 처음 {S3_AUDIT_LIMIT}개만 점검했습니다'} if len(names) > S3_AUDIT_LIMIT else {})}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@mcp_server.tool()
def get_s3_bucket_size(bucket_name: str) -> Dict[str, Any]:
    """
    Gets the size (bytes) and object count of an S3 bucket from the daily CloudWatch storage metrics that S3 publishes
    for free (BucketSizeBytes, NumberOfObjects). Does not list objects. The metrics are updated once a day.

    Args:
        bucket_name: The bucket name.

    Returns:
        Size per storage class, total size and object count, with the metric date.
    """
    try:
        region = _bucket_region(bucket_name)
        cloudwatch = _cloudwatch(region)
        metrics = []
        for page in cloudwatch.get_paginator('list_metrics').paginate(
                Namespace='AWS/S3', Dimensions=[{'Name': 'BucketName', 'Value': bucket_name}]):
            metrics += [m for m in page.get('Metrics', []) if m['MetricName'] in ('BucketSizeBytes', 'NumberOfObjects')]
        if not metrics:
            return {'status': 'success', 'bucket': bucket_name, 'region': region,
                    'message': 'S3 저장소 지표가 아직 없습니다 (하루에 한 번 발행되며 빈 버킷은 없을 수 있음)'}
        end = datetime.now(timezone.utc)
        queries = [{'Id': f'm{index}', 'ReturnData': True,
                    'MetricStat': {'Metric': metric, 'Period': 86400, 'Stat': 'Average'}}
                   for index, metric in enumerate(metrics)]
        data = cloudwatch.get_metric_data(MetricDataQueries=queries, StartTime=end - timedelta(days=3), EndTime=end)
        sizes, objects, dates = {}, None, []
        results = {result['Id']: result for result in data['MetricDataResults']}  # 결과는 Id로 짝짓는다
        for query in queries:
            result = results.get(query['Id'], {})
            if not result.get('Values'):
                continue
            latest = max(range(len(result['Timestamps'])), key=lambda i: result['Timestamps'][i])
            dates.append(result['Timestamps'][latest])
            metric = query['MetricStat']['Metric']
            storage = next((d['Value'] for d in metric['Dimensions'] if d['Name'] == 'StorageType'), '')
            if metric['MetricName'] == 'BucketSizeBytes':
                sizes[storage] = int(result['Values'][latest])
            else:
                objects = int(result['Values'][latest])
        return {'status': 'success', 'bucket': bucket_name, 'region': region, 'size_bytes_by_storage_type': sizes,
                'total_size_bytes': sum(sizes.values()), 'object_count': objects,
                'metric_date': max(dates) if dates else None}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@mcp_server.tool()
def list_s3_objects(bucket_name: str, prefix: Optional[str] = None, max_keys: Optional[int] = None) -> Dict[str, Any]:
    """
    Lists objects under a prefix in an S3 bucket, one "folder" level at a time: object keys with size, last modified
    date and storage class, plus sub-prefixes. Object contents are never read.

    Args:
        bucket_name: The bucket name.
        prefix: Key prefix (folder), e.g. logs/2026/. Omit for the top level.
        max_keys: Maximum number of objects to return (1-100, default 50).

    Returns:
        Objects and sub-prefixes under the prefix, and whether more objects exist.
    """
    try:
        limit = max(1, min(int(max_keys or 50), S3_OBJECT_LIMIT))
        response = _s3(_bucket_region(bucket_name)).list_objects_v2(
            Bucket=bucket_name, Prefix=prefix or '', Delimiter='/', MaxKeys=limit)
        return {'status': 'success', 'bucket': bucket_name, 'prefix': prefix or '',
                'objects': [{'key': o['Key'], 'size_bytes': o['Size'], 'last_modified': o['LastModified'],
                             'storage_class': o.get('StorageClass')} for o in response.get('Contents', [])],
                'prefixes': [p['Prefix'] for p in response.get('CommonPrefixes', [])],
                'is_truncated': response.get('IsTruncated', False)}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


# ---------------------------------------------------------------- EC2 조회 (이 Lambda의 리전)
# 인스턴스·볼륨·탄력적 IP의 설정과 상태, CloudWatch 지표만 본다.
# 사용자 데이터(user data)·콘솔 출력·Windows 암호는 비밀 값이 흔히 들어 있어 읽지 않는다: 코드에서 부르지 않고
# IAM에서도 명시적으로 거부한다 (llm.yaml). DescribeInstances 응답에는 사용자 데이터가 들어 있지 않다.

ec2_client = boto3.client('ec2', region_name=aws_region)
EC2_LIST_LIMIT = 200  # 인스턴스 목록 한 번에 최대
EC2_METRIC_BATCH = 500  # GetMetricData 한 번에 넣을 수 있는 쿼리 수


def _name_of(tags: Optional[List[Dict[str, str]]]) -> Optional[str]:
    return next((tag['Value'] for tag in tags or [] if tag.get('Key') == 'Name'), None)


def _instances(filters: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    found = []
    for page in ec2_client.get_paginator('describe_instances').paginate(Filters=filters or []):
        for reservation in page.get('Reservations', []):
            found += reservation.get('Instances', [])
    return found


# 멈춘 인스턴스의 StateTransitionReason: "User initiated (2026-09-01 10:00:00 GMT)"
_STOPPED_AT = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?:GMT|UTC)\)")


def _stopped_at(instance: Dict[str, Any]) -> Optional[datetime]:
    match = _STOPPED_AT.search(instance.get('StateTransitionReason') or '')
    return datetime.strptime(match.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc) if match else None


@mcp_server.tool()
def list_ec2_instances(state: Optional[str] = None) -> Dict[str, Any]:
    """
    Lists EC2 instances in this deployment's region: ID, Name tag, type, state, availability zone, launch time,
    private/public IP, and tags. User data and console output are never read.

    Args:
        state: Filter by state (pending, running, stopping, stopped, shutting-down, terminated). Omit for all.

    Returns:
        The instances (up to 200) and a count per state.
    """
    try:
        filters = [{'Name': 'instance-state-name', 'Values': [state]}] if state else None
        instances = _instances(filters)
        items = [{'instance_id': i['InstanceId'], 'name': _name_of(i.get('Tags')), 'type': i.get('InstanceType'),
                  'state': i.get('State', {}).get('Name'), 'availability_zone': i.get('Placement', {}).get('AvailabilityZone'),
                  'launch_time': i.get('LaunchTime'), 'private_ip': i.get('PrivateIpAddress'),
                  'public_ip': i.get('PublicIpAddress'), 'platform': i.get('PlatformDetails'),
                  'lifecycle': i.get('InstanceLifecycle', 'on-demand'),
                  'tags': {t['Key']: t['Value'] for t in i.get('Tags', [])}} for i in instances]
        counts: Dict[str, int] = {}
        for item in items:
            counts[item['state']] = counts.get(item['state'], 0) + 1
        return {'status': 'success', 'region': aws_region, 'instance_count': len(items), 'by_state': counts,
                'instances': items[:EC2_LIST_LIMIT],
                **({'note': f'인스턴스가 많아 처음 {EC2_LIST_LIMIT}개만 보여 줍니다'} if len(items) > EC2_LIST_LIMIT else {})}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@mcp_server.tool()
def get_ec2_cpu_ranking(hours: Optional[int] = None, top: Optional[int] = None) -> Dict[str, Any]:
    """
    Ranks running EC2 instances by CPU utilization over the last N hours (average and maximum, from CloudWatch
    CPUUtilization), in a single metric query. Use this for "which instance had the highest CPU?".

    Args:
        hours: Look-back window in hours (1-336, default 24).
        top: Number of instances to return (1-50, default 10).

    Returns:
        Instances sorted by maximum CPU (%), with average CPU and Name tag.
    """
    try:
        hours = max(1, min(int(hours or 24), 336))
        top = max(1, min(int(top or 10), 50))
        running = _instances([{'Name': 'instance-state-name', 'Values': ['running']}])
        if not running:
            return {'status': 'success', 'region': aws_region, 'hours': hours, 'instances': [],
                    'message': '실행 중인 인스턴스가 없습니다'}
        end = datetime.now(timezone.utc)
        period = hours * 3600  # 기간 전체를 한 점으로 모은다 (평균·최대)
        queries, by_id = [], {}
        for index, instance in enumerate(running[:EC2_METRIC_BATCH // 2]):
            metric = {'Namespace': 'AWS/EC2', 'MetricName': 'CPUUtilization',
                      'Dimensions': [{'Name': 'InstanceId', 'Value': instance['InstanceId']}]}
            for stat in ('Average', 'Maximum'):
                query_id = f'{stat[:3].lower()}{index}'
                queries.append({'Id': query_id, 'MetricStat': {'Metric': metric, 'Period': period, 'Stat': stat}})
                by_id[query_id] = (instance, stat)
        results = cloudwatch_client.get_metric_data(MetricDataQueries=queries, StartTime=end - timedelta(hours=hours),
                                                    EndTime=end)['MetricDataResults']
        ranking: Dict[str, Dict[str, Any]] = {}
        for result in results:  # 결과는 Id로 짝짓는다 (순서에 기대지 않는다)
            instance, stat = by_id[result['Id']]
            entry = ranking.setdefault(instance['InstanceId'], {
                'instance_id': instance['InstanceId'], 'name': _name_of(instance.get('Tags')),
                'type': instance.get('InstanceType'), 'average_cpu_percent': None, 'max_cpu_percent': None})
            if result.get('Values'):
                value = max(result['Values']) if stat == 'Maximum' else sum(result['Values']) / len(result['Values'])
                entry['max_cpu_percent' if stat == 'Maximum' else 'average_cpu_percent'] = round(value, 2)
        ordered = sorted(ranking.values(), key=lambda e: (e['max_cpu_percent'] is None, -(e['max_cpu_percent'] or 0)))
        return {'status': 'success', 'region': aws_region, 'hours': hours, 'running_instances': len(running),
                'instances': ordered[:top],
                'without_data': [e['instance_id'] for e in ordered if e['max_cpu_percent'] is None]}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@mcp_server.tool()
def get_ec2_status_checks(instance_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Gets EC2 status checks (system and instance reachability) and scheduled maintenance events. Omit instance_id to
    check all instances in this deployment's region and list only the ones with problems or events.

    Args:
        instance_id: Instance to check. Omit to check all instances.

    Returns:
        Status per instance (ok, impaired, insufficient-data, ...) and scheduled events.
    """
    try:
        kwargs: Dict[str, Any] = {'IncludeAllInstances': True}
        if instance_id:
            kwargs['InstanceIds'] = [instance_id]
        statuses = []
        for page in ec2_client.get_paginator('describe_instance_status').paginate(**kwargs):
            for status in page.get('InstanceStatuses', []):
                statuses.append({
                    'instance_id': status['InstanceId'], 'state': status.get('InstanceState', {}).get('Name'),
                    'system_status': status.get('SystemStatus', {}).get('Status'),
                    'instance_status': status.get('InstanceStatus', {}).get('Status'),
                    'scheduled_events': [{'code': e.get('Code'), 'description': e.get('Description'),
                                          'not_before': e.get('NotBefore')} for e in status.get('Events', [])]})
        problems = [s for s in statuses if s['scheduled_events'] or
                    {s['system_status'], s['instance_status']} - {'ok', 'not-applicable', None}]
        return {'status': 'success', 'region': aws_region, 'checked': len(statuses),
                'instances': statuses if instance_id else problems,
                **({} if instance_id else {'note': '문제가 있거나 예정된 이벤트가 있는 인스턴스만 보여 줍니다'})}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@mcp_server.tool()
def find_ec2_waste(stopped_days: Optional[int] = None) -> Dict[str, Any]:
    """
    Finds EC2 resources that cost money without being used, in this deployment's region: EBS volumes not attached
    to any instance, instances stopped for a long time (their EBS volumes are still billed), and Elastic IPs not
    associated with anything (public IPv4 addresses are billed hourly).

    Args:
        stopped_days: Report instances stopped for at least this many days (default 7).

    Returns:
        Unattached volumes (size, type, age), long-stopped instances, and unassociated Elastic IPs.
    """
    try:
        stopped_days = max(0, int(stopped_days if stopped_days is not None else 7))
        now = datetime.now(timezone.utc)
        volumes = []
        for page in ec2_client.get_paginator('describe_volumes').paginate(
                Filters=[{'Name': 'status', 'Values': ['available']}]):
            for volume in page.get('Volumes', []):
                created = volume.get('CreateTime')
                volumes.append({'volume_id': volume['VolumeId'], 'name': _name_of(volume.get('Tags')),
                                'size_gib': volume.get('Size'), 'type': volume.get('VolumeType'),
                                'created': created, 'age_days': (now - created).days if created else None})
        stopped = []
        for instance in _instances([{'Name': 'instance-state-name', 'Values': ['stopped']}]):
            since = _stopped_at(instance)
            days = (now - since).days if since else None
            if days is None or days >= stopped_days:
                stopped.append({'instance_id': instance['InstanceId'], 'name': _name_of(instance.get('Tags')),
                                'type': instance.get('InstanceType'), 'stopped_since': since, 'stopped_days': days})
        addresses = [{'allocation_id': a.get('AllocationId'), 'public_ip': a.get('PublicIp'),
                      'name': _name_of(a.get('Tags'))}
                     for a in ec2_client.describe_addresses().get('Addresses', [])
                     if not a.get('AssociationId') and not a.get('InstanceId') and not a.get('NetworkInterfaceId')]
        return {'status': 'success', 'region': aws_region,
                'unattached_volumes': volumes, 'unattached_volume_gib': sum(v['size_gib'] or 0 for v in volumes),
                'long_stopped_instances': stopped, 'unassociated_elastic_ips': addresses,
                'note': '금액은 Pricing 도구(get_pricing)나 Cost Explorer로 확인하세요. 지우기 전에 스냅샷·용도를 확인하세요'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


# ---------------------------------------------------------------- 변경 도구 (승인 필요)
# 범위: 이 환경(ENV)의 Vigie 리소스만. 이름 규칙은 CloudFormation과 같다
#   Lambda 로그 그룹 /aws/lambda/vigie-<서비스>-<env>, 알람 vigie-<env>-<이름>
# 오류는 dict로 돌려주지 않고 예외로 올린다 (서버가 실패로 기록하고 승인 테이블에 failed를 남긴다)

# CloudWatch Logs가 받는 보존 기간(일)
RETENTION_DAYS = [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922,
                  3288, 3653]


def _trail(event_source: str, event_name: str, response: Dict[str, Any]) -> Dict[str, Any]:
    """이 변경이 CloudTrail에 남긴 이벤트를 찾을 단서. CloudTrail 이벤트의 requestID가 AWS API 응답의 요청 ID와 같다.
    감사 로그(앱에서 누가 승인했나)와 CloudTrail(AWS에서 무엇이 바뀌었나)을 이 요청 ID로 잇는다."""
    return {"event_source": event_source, "event_name": event_name,
            "request_id": (response or {}).get("ResponseMetadata", {}).get("RequestId")}


def _check_log_group(log_group_name: str) -> None:
    if not (log_group_name.startswith("/aws/lambda/vigie-") and log_group_name.endswith(f"-{environment}")):
        raise ValueError(f"이 환경의 Vigie Lambda 로그 그룹(/aws/lambda/vigie-*-{environment})만 바꿀 수 있습니다: "
                         f"{log_group_name}")


def _current_retention(log_group_name: str):
    for page in logs_client.get_paginator('describe_log_groups').paginate(logGroupNamePrefix=log_group_name):
        for group in page.get('logGroups', []):
            if group.get('logGroupName') == log_group_name:
                return group.get('retentionInDays')  # 없으면 영구 보관
    raise ValueError(f"로그 그룹이 없습니다: {log_group_name}")


def _retention_text(days) -> str:
    return "영구 보관" if days is None else f"{days}일"


@mcp_server.tool()
def set_log_retention(log_group_name: str, retention_days: int) -> Dict[str, Any]:
    """
    Changes the retention period of a Vigie Lambda log group in this environment. This modifies AWS resources, so it
    runs only after the user approves it; calling it creates an approval request instead of running immediately.

    Args:
        log_group_name: Log group name, e.g. /aws/lambda/vigie-llm-dev (only /aws/lambda/vigie-*-<env> is allowed).
        retention_days: New retention in days. One of 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653.

    Returns:
        The log group with the retention before and after the change.
    """
    preview = preview_log_retention(log_group_name, retention_days)
    response = logs_client.put_retention_policy(logGroupName=log_group_name, retentionInDays=retention_days)
    return {"status": "success", **preview, "cloudtrail": _trail("logs.amazonaws.com", "PutRetentionPolicy", response)}


def _preview(target: str, before: Any, after: Any, summary: str, warning: Optional[str] = None) -> Dict[str, Any]:
    """변경 도구의 미리 보기 (승인 카드에 보일 내용).
    - target: 바뀌는 리소스 (로그 그룹·알람 이름 등). 카드의 '대상' 칩
    - before/after: 지금 값과 바뀔 값. 카드의 '30일 → 14일'
    - warning: 이 변경의 영향 (없으면 뺀다). 카드의 영향 줄
    - summary: 한 줄 요약. 영향은 괄호로 붙인다. 승인 기록·감사 로그·결과 설명(모델에게 주는 글)에 쓰고,
      target·warning이 없는 예전 승인 요청의 카드는 이 요약을 그대로 보인다
    """
    preview = {"target": target, "before": before, "after": after,
               "summary": summary + (f" ({warning})" if warning else "")}
    if warning:
        preview["warning"] = warning
    return preview


@mcp_server.preview("setLogRetention")
def preview_log_retention(log_group_name: str, retention_days: int) -> Dict[str, Any]:
    _check_log_group(log_group_name)
    if retention_days not in RETENTION_DAYS:
        raise ValueError(f"보존 기간은 {RETENTION_DAYS} 중 하나여야 합니다: {retention_days}")
    before = _current_retention(log_group_name)
    warning = "지난 로그 일부가 지워질 수 있습니다" if before is None or retention_days < before else None
    return _preview(log_group_name, _retention_text(before), _retention_text(retention_days),
                    f"{log_group_name} 로그 보존 기간 {_retention_text(before)} → {_retention_text(retention_days)}",
                    warning)


def _check_alarm(alarm_name: str) -> None:
    if not alarm_name.startswith(f"vigie-{environment}-"):
        raise ValueError(f"이 환경의 Vigie 알람(vigie-{environment}-*)만 바꿀 수 있습니다: {alarm_name}")
    # 거버넌스 알람(인젝션 의심 등)은 끌 수 없다: 인젝션으로 속은 요청이 감시 장치부터 끄는 것을 막는다 (IAM도 Deny)
    if alarm_name.startswith(f"vigie-{environment}-governance-"):
        raise ValueError(f"거버넌스 알람은 바꿀 수 없습니다: {alarm_name}")


def _alarm_actions_enabled(alarm_name: str) -> bool:
    alarms = cloudwatch_client.describe_alarms(AlarmNames=[alarm_name])
    found = alarms.get('MetricAlarms', []) + alarms.get('CompositeAlarms', [])
    if not found:
        raise ValueError(f"알람이 없습니다: {alarm_name}")
    return bool(found[0].get('ActionsEnabled'))


def _actions_text(enabled: bool) -> str:
    return "알림 켜짐" if enabled else "알림 꺼짐"


@mcp_server.tool()
def set_alarm_actions(alarm_name: str, enabled: bool) -> Dict[str, Any]:
    """
    Turns notifications (alarm actions) of a Vigie CloudWatch alarm in this environment on or off, e.g. to silence an
    alarm during maintenance. This modifies AWS resources, so it runs only after the user approves it; calling it
    creates an approval request instead of running immediately.

    Args:
        alarm_name: Alarm name (only vigie-<env>-* is allowed).
        enabled: true to turn notifications on, false to turn them off.

    Returns:
        The alarm with the notification state before and after the change.
    """
    preview = preview_alarm_actions(alarm_name, enabled)
    if enabled:
        response = cloudwatch_client.enable_alarm_actions(AlarmNames=[alarm_name])
    else:
        response = cloudwatch_client.disable_alarm_actions(AlarmNames=[alarm_name])
    event_name = "EnableAlarmActions" if enabled else "DisableAlarmActions"
    return {"status": "success", **preview, "cloudtrail": _trail("monitoring.amazonaws.com", event_name, response)}


@mcp_server.preview("setAlarmActions")
def preview_alarm_actions(alarm_name: str, enabled: bool) -> Dict[str, Any]:
    _check_alarm(alarm_name)
    before = _alarm_actions_enabled(alarm_name)
    warning = "알람이 울려도 알림이 가지 않습니다" if not enabled else None
    return _preview(alarm_name, _actions_text(before), _actions_text(enabled),
                    f"{alarm_name} {_actions_text(before)} → {_actions_text(enabled)}", warning)


EC2_ACTIONS = {"stop": ("running", "stopped"), "start": ("stopped", "running")}  # 동작 → (필요한 지금 상태, 바뀔 상태)


def _instance(instance_id: str) -> Dict[str, Any]:
    reservations = ec2_client.describe_instances(InstanceIds=[instance_id]).get('Reservations', [])
    instances = [i for r in reservations for i in r.get('Instances', [])]
    if not instances:
        raise ValueError(f"인스턴스가 없습니다: {instance_id}")
    return instances[0]


@mcp_server.tool()
def set_ec2_instance_state(instance_id: str, action: str) -> Dict[str, Any]:
    """
    Stops or starts an EC2 instance in this deployment's region. This modifies AWS resources, so it runs only after
    the user approves it; calling it creates an approval request instead of running immediately.

    Args:
        instance_id: The instance ID, e.g. i-0123456789abcdef0.
        action: "stop" or "start".

    Returns:
        The instance with the state before and after the request.
    """
    preview = preview_ec2_instance_state(instance_id, action)
    if action == "stop":
        response = ec2_client.stop_instances(InstanceIds=[instance_id])
        event_name = "StopInstances"
    else:
        response = ec2_client.start_instances(InstanceIds=[instance_id])
        event_name = "StartInstances"
    return {"status": "success", **preview, "cloudtrail": _trail("ec2.amazonaws.com", event_name, response)}


@mcp_server.preview("setEc2InstanceState")
def preview_ec2_instance_state(instance_id: str, action: str) -> Dict[str, Any]:
    if action not in EC2_ACTIONS:
        raise ValueError(f'action은 "stop" 또는 "start"여야 합니다: {action}')
    instance = _instance(instance_id)
    current = instance.get('State', {}).get('Name')
    needed, target = EC2_ACTIONS[action]
    if current != needed:
        raise ValueError(f"{instance_id}는 지금 {current} 상태라 {action}할 수 없습니다 ({needed} 상태여야 함)")
    name = _name_of(instance.get('Tags'))
    label = f"{instance_id}" + (f" ({name})" if name else "")
    warning = "중지하면 이 인스턴스의 서비스가 멈춥니다. 인스턴스 저장소의 데이터는 사라집니다" if action == "stop" else None
    return _preview(label, current, target, f"EC2 {label} {current} → {target}", warning)


PUBLIC_ACCESS_BLOCK_ALL = {"BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True,
                           "RestrictPublicBuckets": True}


def _public_access_block(bucket_name: str) -> Dict[str, bool]:
    try:
        block = _s3(_bucket_region(bucket_name)).get_public_access_block(
            Bucket=bucket_name)['PublicAccessBlockConfiguration']
    except ClientError as error:
        if _error_code(error) != 'NoSuchPublicAccessBlockConfiguration':
            raise
        block = {}
    return {key: bool(block.get(key)) for key in PUBLIC_ACCESS_BLOCK_ALL}


@mcp_server.tool()
def enable_s3_public_access_block(bucket_name: str) -> Dict[str, Any]:
    """
    Turns on all four S3 Block Public Access settings for a bucket (BlockPublicAcls, IgnorePublicAcls,
    BlockPublicPolicy, RestrictPublicBuckets). This only makes a bucket more private; there is no tool to turn it off.
    It modifies AWS resources, so it runs only after the user approves it; calling it creates an approval request.

    Args:
        bucket_name: The bucket name.

    Returns:
        The bucket with the settings before and after the change.
    """
    preview = preview_s3_public_access_block(bucket_name)
    response = _s3(_bucket_region(bucket_name)).put_public_access_block(
        Bucket=bucket_name, PublicAccessBlockConfiguration=PUBLIC_ACCESS_BLOCK_ALL)
    return {"status": "success", **preview,
            "cloudtrail": _trail("s3.amazonaws.com", "PutBucketPublicAccessBlock", response)}


@mcp_server.preview("enableS3PublicAccessBlock")
def preview_s3_public_access_block(bucket_name: str) -> Dict[str, Any]:
    before = _public_access_block(bucket_name)
    off = [key for key, on in before.items() if not on]
    if not off:
        raise ValueError(f"{bucket_name}은 퍼블릭 액세스 차단이 이미 모두 켜져 있습니다")
    security = _security_of(bucket_name)
    warning = ("버킷 정책이 공개입니다. 차단을 켜면 정적 웹사이트 등 공개 접근이 끊깁니다"
               if security.get('policy_is_public') else None)
    return _preview(bucket_name, f"꺼진 항목 {len(off)}개 ({', '.join(off)})", "4개 모두 켜짐",
                    f"S3 {bucket_name} 퍼블릭 액세스 차단 {len(off)}개 항목 켜기", warning)


@mcp_server.tool()
def generate_architecture_diagram(
        code: str,
        filename: str = None,
        timeout: int = 30
) -> Dict[str, Any]:
    """
    Generate an architecture diagram from Python code using the diagrams package.

    This tool accepts Python code that uses the diagrams package DSL and generates
    a PNG diagram, then uploads it to S3 and returns a presigned URL.

    Args:
        code: Python code string using the diagrams package DSL
        filename: Optional output filename (without extension)
        timeout: Timeout in seconds for diagram generation

    Returns:
        Dictionary with the S3 URL and status information
    """
    try:
        result = generate_diagram(code, filename, timeout)
        # DiagramGenerateResponse 객체를 딕셔너리로 변환
        return {
            'status': result.status,
            'url': result.url,
            's3_key': result.s3_key,
            'message': result.message
        }
    except Exception as e:
        return {
            'status': 'error',
            'message': f'Error generating diagram: {str(e)}'
        }


@mcp_server.tool()
def get_diagram_code_examples(
        diagram_type: str = "all"
) -> Dict[str, Any]:
    """
    Get example code for different types of architecture diagrams.

    This tool provides ready-to-use example code for various diagram types.
    Use these examples to understand the syntax and capabilities of the diagrams package.

    Args:
        diagram_type: Type of diagram example to return (aws, sequence, flow, class, k8s, onprem, custom, all)

    Returns:
        Dictionary with example code for the requested diagram type(s)
    """
    try:
        # Convert string to DiagramType enum
        if diagram_type.lower() == "aws":
            dtype = DiagramType.AWS
        elif diagram_type.lower() == "k8s":
            dtype = DiagramType.K8S
        elif diagram_type.lower() == "flow":
            dtype = DiagramType.FLOW
        elif diagram_type.lower() == "sequence":
            dtype = DiagramType.SEQUENCE
        elif diagram_type.lower() == "class":
            dtype = DiagramType.CLASS
        elif diagram_type.lower() == "onprem":
            dtype = DiagramType.ONPREM
        elif diagram_type.lower() == "custom":
            dtype = DiagramType.CUSTOM
        else:
            dtype = DiagramType.ALL

        result = get_diagram_examples(dtype)
        # DiagramExampleResponse 객체를 딕셔너리로 변환
        return {
            'examples': result.examples
        }
    except Exception as e:
        return {
            'status': 'error',
            'message': f'Error getting examples: {str(e)}'
        }


@mcp_server.tool()
def list_available_diagram_icons(
        provider_filter: str = None,
        service_filter: str = None
) -> Dict[str, Any]:
    """
    List available icons from the diagrams package with optional filtering.

    This tool helps you discover what icons are available for creating diagrams.
    Call without filters to see all providers, or use filters to narrow down results.

    Args:
        provider_filter: Filter icons by provider name (e.g., "aws", "gcp", "k8s")
        service_filter: Filter icons by service name (e.g., "compute", "database", "network")

    Returns:
        Dictionary with available providers, services, and icons
    """
    try:
        result = list_diagram_icons(provider_filter, service_filter)
        # DiagramIconsResponse 객체를 딕셔너리로 변환
        return {
            'providers': result.providers,
            'filtered': result.filtered,
            'filter_info': result.filter_info
        }
    except Exception as e:
        return {
            'status': 'error',
            'message': f'Error listing icons: {str(e)}'
        }


@mcp_server.tool()
def generate_line_chart(
        data: str,
        width: int = 600,
        height: int = 400,
        title: str = "",
        axis_x_title: str = "",
        axis_y_title: str = "",
        stack: bool = False
) -> Dict[str, Any]:
    """
    Generate a line chart to show trends over time.

    Args:
        data: JSON string of data for line chart, such as '[{"time": "2015", "value": 23}]'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)
        title: Title of the chart
        axis_x_title: X-axis title
        axis_y_title: Y-axis title
        stack: Whether stacking is enabled for multi-series data

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        chart_data = json.loads(data)

        if not validate_chart_data(chart_data, ["time", "value"]):
            return {
                "status": "error",
                "message": "Invalid data format. Each item must have 'time' and 'value' fields."
            }

        options = {
            "data": chart_data,
            "width": width,
            "height": height,
            "title": title,
            "axisXTitle": axis_x_title,
            "axisYTitle": axis_y_title,
            "stack": stack
        }

        result = generate_chart_url("line", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for data parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating line chart: {str(e)}"
        }


@mcp_server.tool()
def generate_bar_chart(
        data: str,
        width: int = 600,
        height: int = 400,
        title: str = "",
        axis_x_title: str = "",
        axis_y_title: str = "",
        group: bool = False,
        stack: bool = True
) -> Dict[str, Any]:
    """
    Generate a bar chart for numerical comparisons among different categories.

    Args:
        data: JSON string of data for bar chart, such as '[{"category": "Category A", "value": 10}]'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)
        title: Title of the chart
        axis_x_title: X-axis title
        axis_y_title: Y-axis title
        group: Whether grouping is enabled (requires 'group' field in data)
        stack: Whether stacking is enabled (requires 'group' field in data)

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        chart_data = json.loads(data)

        if not validate_chart_data(chart_data, ["category", "value"]):
            return {
                "status": "error",
                "message": "Invalid data format. Each item must have 'category' and 'value' fields."
            }

        options = {
            "data": chart_data,
            "width": width,
            "height": height,
            "title": title,
            "axisXTitle": axis_x_title,
            "axisYTitle": axis_y_title,
            "group": group,
            "stack": stack
        }

        result = generate_chart_url("bar", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for data parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating bar chart: {str(e)}"
        }


@mcp_server.tool()
def generate_pie_chart(
        data: str,
        width: int = 600,
        height: int = 400,
        title: str = "",
        inner_radius: float = 0.0
) -> Dict[str, Any]:
    """
    Generate a pie chart to show the proportion of parts.

    Args:
        data: JSON string of data for pie chart, such as '[{"category": "Category A", "value": 27}]'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)
        title: Title of the chart
        inner_radius: Inner radius for donut chart (0-1, default: 0)

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        chart_data = json.loads(data)

        if not validate_chart_data(chart_data, ["category", "value"]):
            return {
                "status": "error",
                "message": "Invalid data format. Each item must have 'category' and 'value' fields."
            }

        options = {
            "data": chart_data,
            "width": width,
            "height": height,
            "title": title,
            "innerRadius": inner_radius
        }

        result = generate_chart_url("pie", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for data parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating pie chart: {str(e)}"
        }


@mcp_server.tool()
def generate_scatter_chart(
        data: str,
        width: int = 600,
        height: int = 400,
        title: str = "",
        axis_x_title: str = "",
        axis_y_title: str = ""
) -> Dict[str, Any]:
    """
    Generate a scatter chart to show the relationship between two variables.

    Args:
        data: JSON string of data for scatter chart, such as '[{"x": 10, "y": 15}]'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)
        title: Title of the chart
        axis_x_title: X-axis title
        axis_y_title: Y-axis title

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        chart_data = json.loads(data)

        if not validate_chart_data(chart_data, ["x", "y"]):
            return {
                "status": "error",
                "message": "Invalid data format. Each item must have 'x' and 'y' fields."
            }

        options = {
            "data": chart_data,
            "width": width,
            "height": height,
            "title": title,
            "axisXTitle": axis_x_title,
            "axisYTitle": axis_y_title
        }

        result = generate_chart_url("scatter", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for data parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating scatter chart: {str(e)}"
        }


@mcp_server.tool()
def generate_area_chart(
        data: str,
        width: int = 600,
        height: int = 400,
        title: str = "",
        axis_x_title: str = "",
        axis_y_title: str = "",
        stack: bool = False
) -> Dict[str, Any]:
    """
    Generate an area chart to show data trends under continuous independent variables.

    Args:
        data: JSON string of data for area chart, such as '[{"time": "2018", "value": 99.9}]'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)
        title: Title of the chart
        axis_x_title: X-axis title
        axis_y_title: Y-axis title
        stack: Whether stacking is enabled (requires 'group' field in data)

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        chart_data = json.loads(data)

        if not validate_chart_data(chart_data, ["time", "value"]):
            return {
                "status": "error",
                "message": "Invalid data format. Each item must have 'time' and 'value' fields."
            }

        options = {
            "data": chart_data,
            "width": width,
            "height": height,
            "title": title,
            "axisXTitle": axis_x_title,
            "axisYTitle": axis_y_title,
            "stack": stack
        }

        result = generate_chart_url("area", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for data parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating area chart: {str(e)}"
        }


@mcp_server.tool()
def generate_word_cloud_chart(
        data: str,
        width: int = 600,
        height: int = 400,
        title: str = ""
) -> Dict[str, Any]:
    """
    Generate a word cloud chart to show word frequency through text size variation.

    Args:
        data: JSON string of data for word cloud, such as '[{"text": "word", "value": 4.272}]'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)
        title: Title of the chart

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        chart_data = json.loads(data)

        if not validate_chart_data(chart_data, ["text", "value"]):
            return {
                "status": "error",
                "message": "Invalid data format. Each item must have 'text' and 'value' fields."
            }

        options = {
            "data": chart_data,
            "width": width,
            "height": height,
            "title": title
        }

        result = generate_chart_url("word-cloud", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for data parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating word cloud chart: {str(e)}"
        }


@mcp_server.tool()
def generate_radar_chart(
        data: str,
        width: int = 600,
        height: int = 400,
        title: str = ""
) -> Dict[str, Any]:
    """
    Generate a radar chart to display multidimensional data (four dimensions or more).

    Args:
        data: JSON string of data for radar chart, such as '[{"name": "Design", "value": 70}]'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)
        title: Title of the chart

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        chart_data = json.loads(data)

        if not validate_chart_data(chart_data, ["name", "value"]):
            return {
                "status": "error",
                "message": "Invalid data format. Each item must have 'name' and 'value' fields."
            }

        options = {
            "data": chart_data,
            "width": width,
            "height": height,
            "title": title
        }

        result = generate_chart_url("radar", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for data parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating radar chart: {str(e)}"
        }


@mcp_server.tool()
def generate_column_chart(
        data: str,
        width: int = 600,
        height: int = 400,
        title: str = "",
        axis_x_title: str = "",
        axis_y_title: str = "",
        group: bool = True,
        stack: bool = False
) -> Dict[str, Any]:
    """
    Generate a column chart for comparing categorical data.

    Args:
        data: JSON string of data for column chart, such as '[{"category": "Beijing", "value": 825, "group": "Gas Car"}]'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)
        title: Title of the chart
        axis_x_title: X-axis title
        axis_y_title: Y-axis title
        group: Whether grouping is enabled (requires 'group' field in data)
        stack: Whether stacking is enabled (requires 'group' field in data)

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        chart_data = json.loads(data)

        if not validate_chart_data(chart_data, ["category", "value"]):
            return {
                "status": "error",
                "message": "Invalid data format. Each item must have 'category' and 'value' fields."
            }

        options = {
            "data": chart_data,
            "width": width,
            "height": height,
            "title": title,
            "axisXTitle": axis_x_title,
            "axisYTitle": axis_y_title,
            "group": group,
            "stack": stack
        }

        result = generate_chart_url("column", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for data parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating column chart: {str(e)}"
        }


@mcp_server.tool()
def generate_histogram_chart(
        data: str,
        width: int = 600,
        height: int = 400,
        title: str = "",
        axis_x_title: str = "",
        axis_y_title: str = "",
        bin_number: int = None
) -> Dict[str, Any]:
    """
    Generate a histogram chart to show frequency distribution of data.

    Args:
        data: JSON string of numeric data, such as '[78, 88, 60, 100, 95]'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)
        title: Title of the chart
        axis_x_title: X-axis title
        axis_y_title: Y-axis title
        bin_number: Number of bins for histogram (optional)

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        chart_data = json.loads(data)

        # Validate that data is a list of numbers
        if not isinstance(chart_data, list) or not all(isinstance(x, (int, float)) for x in chart_data):
            return {
                "status": "error",
                "message": "Invalid data format. Data must be a list of numbers."
            }

        options = {
            "data": chart_data,
            "width": width,
            "height": height,
            "title": title,
            "axisXTitle": axis_x_title,
            "axisYTitle": axis_y_title
        }

        if bin_number is not None:
            options["binNumber"] = bin_number

        result = generate_chart_url("histogram", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for data parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating histogram: {str(e)}"
        }


@mcp_server.tool()
def generate_treemap_chart(
        data: str,
        width: int = 600,
        height: int = 400,
        title: str = ""
) -> Dict[str, Any]:
    """
    Generate a treemap chart to display hierarchical data.

    Args:
        data: JSON string of hierarchical data, such as '[{"name": "Design", "value": 70, "children": [{"name": "Tech", "value": 20}]}]'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)
        title: Title of the chart

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        chart_data = json.loads(data)

        # Basic validation for treemap data structure
        if not isinstance(chart_data, list):
            return {
                "status": "error",
                "message": "Invalid data format. Data must be a list of tree nodes."
            }

        for item in chart_data:
            if not isinstance(item, dict) or "name" not in item or "value" not in item:
                return {
                    "status": "error",
                    "message": "Invalid data format. Each item must have 'name' and 'value' fields."
                }

        options = {
            "data": chart_data,
            "width": width,
            "height": height,
            "title": title
        }

        result = generate_chart_url("treemap", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for data parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating treemap: {str(e)}"
        }


@mcp_server.tool()
def generate_dual_axes_chart(
        categories: str,
        series: str,
        width: int = 600,
        height: int = 400,
        title: str = "",
        axis_x_title: str = ""
) -> Dict[str, Any]:
    """
    Generate a dual axes chart combining bar and line charts.

    Args:
        categories: JSON string of categories, such as '["2015", "2016", "2017"]'
        series: JSON string of series data, such as '[{"type": "column", "data": [91.9, 99.1, 101.6], "axisYTitle": "Sales"}, {"type": "line", "data": [0.055, 0.06, 0.062], "axisYTitle": "Ratio"}]'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)
        title: Title of the chart
        axis_x_title: X-axis title

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        categories_data = json.loads(categories)
        series_data = json.loads(series)

        if not isinstance(categories_data, list) or not categories_data:
            return {
                "status": "error",
                "message": "Categories must be a non-empty list."
            }

        if not isinstance(series_data, list) or not series_data:
            return {
                "status": "error",
                "message": "Series must be a non-empty list."
            }

        # Validate series data structure
        for series_item in series_data:
            if not isinstance(series_item, dict) or "type" not in series_item or "data" not in series_item:
                return {
                    "status": "error",
                    "message": "Each series item must have 'type' and 'data' fields."
                }

            if series_item["type"] not in ["column", "line"]:
                return {
                    "status": "error",
                    "message": "Series type must be 'column' or 'line'."
                }

        options = {
            "categories": categories_data,
            "series": series_data,
            "width": width,
            "height": height,
            "title": title,
            "axisXTitle": axis_x_title
        }

        result = generate_chart_url("dual-axes", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for categories or series parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating dual axes chart: {str(e)}"
        }


@mcp_server.tool()
def generate_mind_map(
        data: str,
        width: int = 600,
        height: int = 400
) -> Dict[str, Any]:
    """
    Generate a mind map chart to organize hierarchical information.

    Args:
        data: JSON string of mind map data, such as '{"name": "main topic", "children": [{"name": "topic 1", "children": [{"name": "subtopic 1-1"}]}]}'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        chart_data = json.loads(data)

        # Validate mind map data structure
        if not isinstance(chart_data, dict) or "name" not in chart_data:
            return {
                "status": "error",
                "message": "Invalid data format. Root node must have a 'name' field."
            }

        options = {
            "data": chart_data,
            "width": width,
            "height": height
        }

        result = generate_chart_url("mind-map", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for data parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating mind map: {str(e)}"
        }


@mcp_server.tool()
def generate_network_graph(
        nodes: str,
        edges: str,
        width: int = 600,
        height: int = 400
) -> Dict[str, Any]:
    """
    Generate a network graph to show relationships between entities.

    Args:
        nodes: JSON string of nodes, such as '[{"name": "node1"}, {"name": "node2"}]'
        edges: JSON string of edges, such as '[{"source": "node1", "target": "node2", "name": "edge1"}]'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        nodes_data = json.loads(nodes)
        edges_data = json.loads(edges)

        # Validate nodes data
        if not isinstance(nodes_data, list) or not nodes_data:
            return {
                "status": "error",
                "message": "Nodes must be a non-empty list."
            }

        for node in nodes_data:
            if not isinstance(node, dict) or "name" not in node:
                return {
                    "status": "error",
                    "message": "Each node must have a 'name' field."
                }

        # Validate edges data
        if not isinstance(edges_data, list):
            return {
                "status": "error",
                "message": "Edges must be a list."
            }

        for edge in edges_data:
            if not isinstance(edge, dict) or "source" not in edge or "target" not in edge:
                return {
                    "status": "error",
                    "message": "Each edge must have 'source' and 'target' fields."
                }

        chart_data = {
            "nodes": nodes_data,
            "edges": edges_data
        }

        options = {
            "data": chart_data,
            "width": width,
            "height": height
        }

        result = generate_chart_url("network-graph", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for nodes or edges parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating network graph: {str(e)}"
        }


@mcp_server.tool()
def generate_flow_diagram(
        nodes: str,
        edges: str,
        width: int = 600,
        height: int = 400
) -> Dict[str, Any]:
    """
    Generate a flow diagram to show process steps and decision points.

    Args:
        nodes: JSON string of nodes, such as '[{"name": "Start"}, {"name": "Process"}]'
        edges: JSON string of edges, such as '[{"source": "Start", "target": "Process", "name": "flow"}]'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        nodes_data = json.loads(nodes)
        edges_data = json.loads(edges)

        # Validate nodes data
        if not isinstance(nodes_data, list) or not nodes_data:
            return {
                "status": "error",
                "message": "Nodes must be a non-empty list."
            }

        for node in nodes_data:
            if not isinstance(node, dict) or "name" not in node:
                return {
                    "status": "error",
                    "message": "Each node must have a 'name' field."
                }

        # Validate edges data
        if not isinstance(edges_data, list):
            return {
                "status": "error",
                "message": "Edges must be a list."
            }

        for edge in edges_data:
            if not isinstance(edge, dict) or "source" not in edge or "target" not in edge:
                return {
                    "status": "error",
                    "message": "Each edge must have 'source' and 'target' fields."
                }

        chart_data = {
            "nodes": nodes_data,
            "edges": edges_data
        }

        options = {
            "data": chart_data,
            "width": width,
            "height": height
        }

        result = generate_chart_url("flow-diagram", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for nodes or edges parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating flow diagram: {str(e)}"
        }


@mcp_server.tool()
def generate_fishbone_diagram(
        data: str,
        width: int = 600,
        height: int = 400
) -> Dict[str, Any]:
    """
    Generate a fishbone diagram to analyze causes and effects.

    Args:
        data: JSON string of fishbone data, such as '{"name": "main problem", "children": [{"name": "cause 1", "children": [{"name": "subcause 1-1"}]}]}'
        width: Width of the chart (default: 600)
        height: Height of the chart (default: 400)

    Returns:
        Dictionary with chart URL or error message
    """
    try:
        chart_data = json.loads(data)

        # Validate fishbone data structure
        if not isinstance(chart_data, dict) or "name" not in chart_data:
            return {
                "status": "error",
                "message": "Invalid data format. Root node must have a 'name' field."
            }

        options = {
            "data": chart_data,
            "width": width,
            "height": height
        }

        result = generate_chart_url("fishbone-diagram", options)
        return result

    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON format for data parameter"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error generating fishbone diagram: {str(e)}"
        }
# Define the lambda handler
def lambda_handler(event, context):
    """AWS Lambda handler function."""
    return mcp_server.handle_request(event, context)