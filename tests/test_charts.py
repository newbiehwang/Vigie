"""차트를 Lambda 안에서 그린다 (mcp/lambda_mcp/chart_utils.py, docs/threat-model.md R1)

예전에는 차트 데이터를 외부 차트 서버(antv-studio.alipay.com)로 보냈다. 이제는:
- 차트 도구 15개가 모두 matplotlib으로 그려 다이어그램 버킷에 올리고 presigned URL을 돌려준다
- 그리는 동안 네트워크를 쓰지 않는다 (S3 업로드는 moto라 네트워크가 아니다)
- 모델이 넘긴 값의 크기를 제한하고, 그릴 수 없는 값은 이유와 함께 오류로 돌려준다
- 결과물 도구에는 가명을 원래 값으로 되돌리지 않고 넘긴다 (그림에도 가명만 보인다)
"""
import copy
import json
import socket
import struct
from urllib.parse import urlparse

import boto3
import pytest

from conftest import ROOT
from test_approvals import ORIGIN, FakeResponse, env  # noqa: F401 (env는 fixture)

BUCKET = "vigie-diagrambucket-test"  # diagram_utils의 기본값 (ENV=test)

J = json.dumps
CHARTS = {
    "generateLineChart": {"data": J([{"time": "09-01", "value": 3, "group": "dev"}, {"time": "09-02", "value": 5,
                                                                                   "group": "dev"}]),
                          "title": "Lambda 오류 수", "axis_x_title": "날짜"},
    "generateAreaChart": {"data": J([{"time": "0시", "value": 1}, {"time": "3시", "value": 4}]), "stack": True},
    "generateBarChart": {"data": J([{"category": "EC2", "value": 320.5}, {"category": "S3", "value": 45}])},
    "generateColumnChart": {"data": J([{"category": "7월", "value": 40, "group": "dev"},
                                       {"category": "7월", "value": 120, "group": "prod"}])},
    "generatePieChart": {"data": J([{"category": "EC2", "value": 55}, {"category": "기타", "value": 45}]),
                         "inner_radius": 0.5},
    "generateScatterChart": {"data": J([{"x": 1, "y": 2}, {"x": 3, "y": 4}])},
    "generateHistogramChart": {"data": J([1, 2, 2, 3, 3, 3, 4]), "bin_number": 4},
    "generateRadarChart": {"data": J([{"name": n, "value": v} for n, v in [("보안", 80), ("비용", 60), ("성능", 70)]])},
    "generateDualAxesChart": {"categories": J(["7월", "8월"]),
                              "series": J([{"type": "column", "data": [91, 99], "axisYTitle": "요청"},
                                           {"type": "line", "data": [0.05, 0.06], "axisYTitle": "오류율"}])},
    "generateWordCloudChart": {"data": J([{"text": "Timeout", "value": 40}, {"text": "메모리 부족", "value": 20}])},
    "generateTreemapChart": {"data": J([{"name": "EC2", "value": 300, "children": [{"name": "t3", "value": 200}]},
                                        {"name": "RDS", "value": 150}])},
    "generateMindMap": {"data": J({"name": "장애 대응", "children": [{"name": "탐지", "children": [{"name": "알람"}]}]})},
    "generateFishboneDiagram": {"data": J({"name": "지연", "children": [{"name": "Lambda",
                                                                       "children": [{"name": "콜드 스타트"}]}]})},
    "generateNetworkGraph": {"nodes": J([{"name": "API"}, {"name": "LLM"}]),
                             "edges": J([{"source": "API", "target": "LLM", "name": "호출"}])},
    "generateFlowDiagram": {"nodes": J([{"name": "질문"}, {"name": "답변"}]),
                            "edges": J([{"source": "질문", "target": "답변"}, {"source": "답변", "target": "질문"}])},
}


@pytest.fixture
def charts(env, monkeypatch):  # noqa: F811
    boto3.client("s3").create_bucket(Bucket=BUCKET)

    # 그리는 동안 밖으로 나가는 연결이 있으면 실패시킨다 (moto는 소켓을 쓰지 않는다)
    def no_network(*args, **kwargs):
        raise AssertionError("차트를 그리는 동안 네트워크 연결을 시도했다")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    return env


def call(env, tool, args):  # noqa: F811
    result = env["mcp"].call_tool(tool, args)
    return json.loads(result["content"][0]["text"])


def chart_module(env):  # noqa: F811
    """MCP 서버가 불러온 chart_utils (LLM 모듈을 불러오면 lambda_mcp가 import 경로에서 빠져 다시 불러올 수 없다)."""
    import types
    return types.SimpleNamespace(**env["app"].generate_chart_url.__globals__)


def png_size(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_all_chart_tools_are_covered(env):  # noqa: F811
    tools = json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]
    artifact_charts = {t["name"] for t in tools if t["_meta"]["vigie/risk"] == "artifact"} - {
        "generateArchitectureDiagram"}
    assert artifact_charts == set(CHARTS)


@pytest.mark.parametrize("tool", sorted(CHARTS))
def test_chart_is_drawn_here_and_uploaded_to_our_bucket(charts, tool):
    body = call(charts, tool, CHARTS[tool])
    assert body["status"] == "success", body

    url = urlparse(body["url"])
    assert BUCKET in url.netloc + url.path and "charts/" in url.path and "Signature" in url.query
    key = url.path.split(f"{BUCKET}/", 1)[-1] if f"{BUCKET}/" in url.path else url.path.lstrip("/")
    obj = boto3.client("s3").get_object(Bucket=BUCKET, Key=key)
    assert obj["ContentType"] == "image/png"
    width, height = png_size(obj["Body"].read())
    assert width > 100 and height > 100


def test_chart_code_has_no_way_out():
    source = (ROOT / "mcp" / "lambda_mcp" / "chart_utils.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    code = code.split('"""', 2)[-1]  # 모듈 설명(예전 외부 서버 이야기)은 빼고 본다
    for word in ("import requests", "urllib", "http.client", "import socket", "VIS_REQUEST_SERVER"):
        assert word not in code
    # 외부 차트 서버 주소는 코드 어디에도 없다
    for path in (ROOT / "mcp").rglob("*.py"):
        assert "alipay" not in path.read_text(encoding="utf-8").split('"""', 2)[-1], path


@pytest.mark.parametrize("tool, args, reason", [
    ("generatePieChart", {"data": J([{"category": "a", "value": -1}])}, "non-negative"),
    ("generateRadarChart", {"data": J([{"name": "a", "value": 1}, {"name": "b", "value": 2}])}, "at least 3"),
    ("generateBarChart", {"data": J([{"category": "a", "value": "많음"}])}, "number"),
    ("generateMindMap", {"data": J({"name": "r", "children": [{"name": str(i)} for i in range(200)]})},
     "too many nodes"),
    ("generateDualAxesChart", {"categories": J(["a", "b"]), "series": J([{"type": "line", "data": [1]}])},
     "one value per category"),
    ("generateLineChart", {"data": "not json"}, "Invalid JSON"),
])
def test_bad_data_is_explained(charts, tool, args, reason):
    body = call(charts, tool, args)
    assert body["status"] == "error" and reason in body["message"]


def test_picture_size_is_limited(env):  # noqa: F811
    chart_utils = chart_module(env)
    data = [{"category": "a", "value": 1}]
    width, height = png_size(chart_utils.render_chart("bar", {"data": data, "width": 100000, "height": 1}))
    scale = chart_utils.DPI / 100
    assert width <= chart_utils.MAX_SIDE * scale + 1 and height >= chart_utils.MIN_SIDE * scale - 1


def test_flow_diagram_with_loops_finishes(env):  # noqa: F811
    chart_utils = chart_module(env)
    names = [f"n{i}" for i in range(30)]
    edges = [{"source": a, "target": b} for a, b in zip(names, names[1:])] + [
        {"source": "n29", "target": "n0"}, {"source": "n5", "target": "n5"}, {"source": "n3", "target": "n9"}]
    png = chart_utils.render_chart("flow-diagram", {"data": {"nodes": [{"name": n} for n in names], "edges": edges}})
    assert png_size(png)[0] > 0


# ---------------------------------------------------------------- 가명은 되돌리지 않는다

OTHER_ACCOUNT = "210987654321"
ARN = f"arn:aws:iam::{OTHER_ACCOUNT}:role/deploy"


def test_chart_tools_get_pseudonyms_but_lookups_get_real_values(env, monkeypatch):  # noqa: F811
    llm = env["llm"]
    import mcp_anthropic_client

    alias = None
    sent, calls = [], []

    def fake_post(url, headers=None, json=None, **kwargs):
        nonlocal alias
        sent.append(copy.deepcopy(json))
        if len(sent) == 1:
            # 질문의 ARN은 가려져 모델에 간다. 모델은 가명이 든 ARN으로 조회하고, 같은 값으로 차트를 그린다
            question = json["messages"][-1]["content"]
            question = question if isinstance(question, str) else question[-1]["text"]
            alias = next(word for word in question.split() if word.startswith("arn:aws:iam::"))
            return FakeResponse({"content": [
                {"type": "tool_use", "id": "toolu_1", "name": "get_role_policy",
                 "input": {"role_name": "deploy", "policy_name": alias}},
                {"type": "tool_use", "id": "toolu_2", "name": "generateBarChart",
                 "input": {"data": J([{"category": alias, "value": 1}])}},
            ], "usage": {}, "stop_reason": "tool_use"})
        return FakeResponse({"content": [{"type": "text", "text": "완료"}], "usage": {}, "stop_reason": "end_turn"})

    def call_tool(name, args=None, meta=None):
        calls.append((name, args))
        return {"content": [{"type": "text", "text": '{"status": "success", "url": "https://example.invalid/x.png"}'}]}

    monkeypatch.setattr(mcp_anthropic_client.HTTP, "post", fake_post)
    client = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                     model_id="claude-sonnet-5")
    client.tools = json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]
    monkeypatch.setattr(client.mcp_client, "call_tool", call_tool)
    monkeypatch.setattr(llm, "get_client", lambda *_: client)

    llm.handle_llm1_with_mcp({"text": f"{ARN} 역할 정책을 차트로 그려줘"}, ORIGIN, caller_id="alice")

    assert alias and OTHER_ACCOUNT not in alias
    lookups = dict(calls)
    # 조회 도구는 원래 값을 받아야 AWS를 조회할 수 있다
    assert lookups["get_role_policy"]["policy_name"] == ARN
    # 차트는 모델이 쓴 가명 그대로 그린다 (원래 계정 ID가 그림에 들어가지 않는다)
    assert OTHER_ACCOUNT not in lookups["generateBarChart"]["data"] and alias in lookups["generateBarChart"]["data"]
    # 감사 로그도 도구가 실제로 받은 값을 남긴다
    items = env["audit"].query(KeyConditionExpression="userId = :u", ExpressionAttributeValues={":u": "alice"})["Items"]
    chart_item = next(i for i in items if i.get("tool") == "generateBarChart")
    lookup_item = next(i for i in items if i.get("tool") == "get_role_policy")
    assert OTHER_ACCOUNT not in chart_item["input"] and OTHER_ACCOUNT in lookup_item["input"]
