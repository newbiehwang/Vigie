"""AWS 공식 Pricing MCP 서버 (mcp/lambda_mcp/official.py)

- 가격표 조회 도구 4개만 붙인다
- 로컬 파일 경로를 받아 여는 도구(CDK·Terraform 분석)는 뺀다: Lambda 안에서는 자격 증명이 든 파일까지 읽을 수 있다
- 파일을 쓰는 보고서 도구, 가격 파일 주소 도구, Bedrock 설계 예시 도구도 뺀다
- Pricing API는 moto가 흉내 내지 않아, 공식 서버가 만드는 Pricing 클라이언트만 가짜로 바꿔 끼운다
"""
import json

import pytest
import yaml

from conftest import ROOT
from test_approvals import env  # noqa: F401 (fixture)

INCLUDED = {"get_pricing_service_codes", "get_pricing_service_attributes", "get_pricing_attribute_values",
            "get_pricing"}
EXCLUDED = {"analyze_cdk_project", "analyze_terraform_project", "generate_cost_report", "get_price_list_urls",
            "get_bedrock_patterns"}

LAMBDA_PRICE = {
    "product": {"productFamily": "Serverless", "sku": "SKU1",
                "attributes": {"servicecode": "AWSLambda", "regionCode": "ap-southeast-2", "group": "AWS-Lambda-Requests",
                               "usagetype": "APS2-Request"}},
    "serviceCode": "AWSLambda",
    "terms": {"OnDemand": {"SKU1.T1": {"priceDimensions": {"SKU1.T1.D1": {
        "unit": "Requests", "description": "AWS Lambda - Requests", "pricePerUnit": {"USD": "0.0000002000"},
        "beginRange": "0", "endRange": "Inf"}}, "sku": "SKU1", "effectiveDate": "2026-09-01T00:00:00Z",
        "offerTermCode": "T1", "termAttributes": {}}}},
}


class FakePricing:
    def __init__(self):
        self.calls = []

    def describe_services(self, **params):
        self.calls.append(("describe_services", params))
        return {"Services": [{"ServiceCode": "AWSLambda", "AttributeNames": ["regionCode", "group"]},
                             {"ServiceCode": "AmazonS3", "AttributeNames": ["regionCode"]}]}

    def get_products(self, **params):
        self.calls.append(("get_products", params))
        return {"PriceList": [json.dumps(LAMBDA_PRICE)]}


@pytest.fixture
def pricing(env, monkeypatch):  # noqa: F811
    import awslabs.aws_pricing_mcp_server.server as server
    fake = FakePricing()
    monkeypatch.setattr(server, "create_pricing_client", lambda *args, **kwargs: fake)
    tools = {t["name"]: t for t in json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]}
    return env, tools, fake


def test_only_price_lookup_tools_are_attached(pricing):
    _, tools, _ = pricing
    assert INCLUDED <= set(tools) and not (EXCLUDED & set(tools))
    assert all(tools[name]["_meta"]["vigie/risk"] == "read" for name in INCLUDED)


def test_file_reading_tools_cannot_be_called(pricing):
    env, _, _ = pricing
    # 목록에 없을 뿐 아니라 이름으로 불러도 없다: 모델이 경로를 골라 Lambda 안의 파일을 읽을 수 없다
    for name in ("analyze_terraform_project", "analyze_cdk_project"):
        with pytest.raises(Exception, match="not found"):
            env["mcp"].call_tool(name, {"project_path": "/proc/self"})


def test_get_pricing_returns_list_prices(pricing):
    env, _, fake = pricing
    result = env["mcp"].call_tool("get_pricing", {"service_code": "AWSLambda", "region": "ap-southeast-2"})
    assert not result.get("isError"), result
    assert "0.0000002" in json.dumps(result)
    name, params = fake.calls[-1]
    assert name == "get_products" and params["ServiceCode"] == "AWSLambda"
    assert {"Field": "regionCode", "Type": "EQUALS", "Value": "ap-southeast-2"} in params["Filters"]


def test_service_codes(pricing):
    env, _, _ = pricing
    result = env["mcp"].call_tool("get_pricing_service_codes", {})
    assert not result.get("isError") and "AWSLambda" in json.dumps(result)


def test_pricing_api_region_follows_the_deployment_region(pricing):
    # Pricing API는 몇 리전에만 있다. 아시아 태평양 리전은 가장 가까운 Pricing API 리전으로 간다 (공식 서버의 동작)
    from awslabs.aws_pricing_mcp_server.pricing_client import get_pricing_region
    assert get_pricing_region("ap-southeast-2") == "ap-south-1"


def test_mcp_role_gets_only_price_lookup_permissions():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    template = yaml.load((ROOT / "cloudformation" / "llm.yaml").read_text(encoding="utf-8"), Loader=Loader)
    statements = template["Resources"]["McpLambdaExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"]
    actions = {a for s in statements for a in s["Action"] if a.startswith("pricing:")}
    assert actions == {"pricing:DescribeServices", "pricing:GetAttributeValues", "pricing:GetProducts"}
