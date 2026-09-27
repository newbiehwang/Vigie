"""S3 조회 도구 (mcp/app.py): 버킷 목록, 보안 점검, 크기·객체 수, 객체 목록. 객체 내용은 읽지 않는다

moto로 실제 S3·CloudWatch API까지 부른다. LLM Lambda가 부르는 것과 같은 길(JSON-RPC → lambda_handler)로 확인한다.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
import yaml

from conftest import ROOT
from test_approvals import env  # noqa: F401 (fixture)

SECURE, OPEN = "vigie-secure-test", "legacy-open-bucket"
PUBLIC_POLICY = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
                                                         "Resource": f"arn:aws:s3:::{OPEN}/*"}]}


@pytest.fixture
def s3_env(env):  # noqa: F811
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket=SECURE)
    s3.put_public_access_block(Bucket=SECURE, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_encryption(Bucket=SECURE, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]})
    s3.put_bucket_versioning(Bucket=SECURE, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_ownership_controls(Bucket=SECURE, OwnershipControls={"Rules": [
        {"ObjectOwnership": "BucketOwnerEnforced"}]})
    s3.put_bucket_lifecycle_configuration(Bucket=SECURE, LifecycleConfiguration={"Rules": [
        {"ID": "expire", "Status": "Enabled", "Filter": {"Prefix": "tmp/"}, "Expiration": {"Days": 7}}]})
    for key in ("logs/2026/a.json", "logs/2026/b.json", "logs/2025/c.json", "readme.txt"):
        s3.put_object(Bucket=SECURE, Key=key, Body=b"secret-content-never-read")

    s3.create_bucket(Bucket=OPEN)
    s3.put_bucket_policy(Bucket=OPEN, Policy=json.dumps(PUBLIC_POLICY))
    return env


def call(env, name, args=None):  # noqa: F811
    result = env["mcp"].call_admin(name, args or {})  # 보안 점검·객체 목록은 관리자 전용
    body = json.loads(result["content"][0]["text"])  # 직접 둔 도구의 결과는 JSON이다
    assert body["status"] == "success", body
    return body


def test_s3_tools_are_registered_as_read(s3_env):
    tools = {t["name"]: t for t in json.loads(s3_env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]}
    for name in ("listS3Buckets", "checkS3BucketSecurity", "getS3BucketSize", "listS3Objects"):
        assert tools[name]["_meta"]["vigie/risk"] == "read"
    # 기본값이 있는 인자는 필수가 아니다 (등록 코드가 기본값을 읽는다)
    assert tools["checkS3BucketSecurity"]["inputSchema"]["required"] == []
    assert tools["listS3Objects"]["inputSchema"]["required"] == ["bucket_name"]
    assert tools["listS3Objects"]["inputSchema"]["properties"]["max_keys"]["type"] == "integer"


def test_list_buckets_with_region(s3_env):
    body = call(s3_env, "listS3Buckets")
    regions = {b["name"]: b["region"] for b in body["buckets"]}
    assert SECURE in regions and OPEN in regions
    assert all(regions[name] for name in (SECURE, OPEN))


def test_security_check_of_one_bucket(s3_env):
    [bucket] = call(s3_env, "checkS3BucketSecurity", {"bucket_name": SECURE})["buckets"]
    assert bucket["versioning"] == "Enabled" and bucket["encryption"] == ["aws:kms"]
    assert bucket["object_ownership"] == "BucketOwnerEnforced" and bucket["lifecycle_rules"] == 1
    assert bucket["policy_is_public"] is False and bucket["findings"] == []


class PublicPolicyS3:
    """moto는 버킷 정책이 공개인지 계산하지 않는다(PolicyStatus가 빈 값). 실제 AWS처럼 OPEN 버킷만 공개로 답한다."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    def get_bucket_policy_status(self, Bucket):
        return {"PolicyStatus": {"IsPublic": Bucket == OPEN}}


def test_security_audit_of_all_buckets_finds_the_open_one(s3_env, monkeypatch):
    app = s3_env["app"]
    real = app._s3
    monkeypatch.setattr(app, "_s3", lambda region=None: PublicPolicyS3(real(region)))
    body = call(s3_env, "checkS3BucketSecurity")
    by_name = {b["bucket"]: b for b in body["buckets"]}
    assert body["checked"] >= 2 and by_name[SECURE]["findings"] == []
    findings = " ".join(by_name[OPEN]["findings"])
    assert "퍼블릭 액세스 차단" in findings and "버전 관리" in findings
    assert by_name[OPEN]["policy_is_public"] is True and "버킷 정책이 공개" in findings
    assert body["buckets_with_findings"] >= 1


def test_list_objects_one_level_without_contents(s3_env):
    top = call(s3_env, "listS3Objects", {"bucket_name": SECURE})
    assert [o["key"] for o in top["objects"]] == ["readme.txt"] and top["prefixes"] == ["logs/"]
    logs = call(s3_env, "listS3Objects", {"bucket_name": SECURE, "prefix": "logs/2026/", "max_keys": 1})
    assert len(logs["objects"]) == 1 and logs["is_truncated"] is True
    assert "secret-content-never-read" not in json.dumps(top) + json.dumps(logs)


class FakeStorageMetrics:
    """S3 저장소 지표를 통제된 값으로 돌려주는 CloudWatch (moto는 버킷 지표를 스스로 만들어 넣은 값과 섞는다).
    저장소 클래스 10개 + 객체 수 = 지표 11개: 결과를 Id로 짝짓지 않으면 m10이 m1 뒤로 정렬되어 값이 엇갈린다."""

    STORAGE = ["StandardStorage", "StandardIAStorage", "OneZoneIAStorage", "ReducedRedundancyStorage",
               "GlacierInstantRetrievalStorage", "GlacierStorage", "DeepArchiveStorage", "IntelligentTieringFAStorage",
               "IntelligentTieringIAStorage", "IntelligentTieringAIAStorage"]

    def __init__(self, bucket):
        self.metrics = [{"Namespace": "AWS/S3", "MetricName": "BucketSizeBytes",
                         "Dimensions": [{"Name": "BucketName", "Value": bucket}, {"Name": "StorageType", "Value": st}]}
                        for st in self.STORAGE]
        self.metrics.append({"Namespace": "AWS/S3", "MetricName": "NumberOfObjects",
                             "Dimensions": [{"Name": "BucketName", "Value": bucket},
                                            {"Name": "StorageType", "Value": "AllStorageTypes"}]})

    def get_paginator(self, name):
        metrics = self.metrics

        class Paginator:
            def paginate(self, **kwargs):
                return [{"Metrics": metrics}]
        return Paginator()

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime):
        day = datetime.now(timezone.utc)
        results = []
        for query in MetricDataQueries:
            metric = query["MetricStat"]["Metric"]
            storage = metric["Dimensions"][1]["Value"]
            value = 7 if metric["MetricName"] == "NumberOfObjects" else (self.STORAGE.index(storage) + 1) * 1000
            results.append({"Id": query["Id"], "Timestamps": [day], "Values": [float(value)]})
        return {"MetricDataResults": list(reversed(results))}  # 순서에 기대지 않는지 보려고 뒤집는다


def test_bucket_size_from_cloudwatch_storage_metrics(s3_env, monkeypatch):
    app = s3_env["app"]
    monkeypatch.setattr(app, "_cloudwatch", lambda region: FakeStorageMetrics(SECURE))
    body = call(s3_env, "getS3BucketSize", {"bucket_name": SECURE})
    sizes = body["size_bytes_by_storage_type"]
    assert sizes == {st: (i + 1) * 1000 for i, st in enumerate(FakeStorageMetrics.STORAGE)}
    assert body["total_size_bytes"] == 55000 and body["object_count"] == 7


def test_bucket_without_storage_metrics_gets_a_note(s3_env, monkeypatch):
    class NoMetrics(FakeStorageMetrics):
        def __init__(self):
            self.metrics = []

    monkeypatch.setattr(s3_env["app"], "_cloudwatch", lambda region: NoMetrics())
    assert "지표가 아직 없습니다" in call(s3_env, "getS3BucketSize", {"bucket_name": OPEN})["message"]


def test_code_never_reads_object_contents():
    source = (Path(ROOT) / "mcp" / "app.py").read_text(encoding="utf-8")
    assert "get_object(" not in source and "download_file" not in source and "select_object_content" not in source


def test_iam_denies_reading_objects_outside_the_diagram_bucket():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_scalar(node)
                                 if isinstance(node, yaml.ScalarNode) else None)
    template = yaml.load((ROOT / "cloudformation" / "llm.yaml").read_text(encoding="utf-8"), Loader=Loader)
    statements = template["Resources"]["McpLambdaExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"]
    deny = next(s for s in statements if s["Effect"] == "Deny" and "s3:GetObject" in s["Action"])
    assert deny["NotResource"].startswith("arn:aws:s3:::vigie-diagrambucket-")
    # 다이어그램 버킷 말고는 객체를 읽거나 쓰는 Allow가 없다
    for statement in statements:
        if statement["Effect"] == "Allow" and {"s3:GetObject", "s3:PutObject"} & set(statement["Action"]):
            assert "vigie-diagrambucket-" in statement["Resource"]
