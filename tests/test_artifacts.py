"""결과물(차트·다이어그램) 참조 (services/llm/artifacts.py, mcp/lambda_mcp/chart_utils.py)

- 모델에는 artifact:// 참조만 간다: presigned URL(임시 자격 증명의 키 ID, 버킷 이름의 계정 ID)과 차트 데이터는 가지 않는다
  (예전에는 URL이 가리기를 거쳐 망가진 채 모델에 가서, 답변 속 이미지가 열리지 않았다)
- 화면은 inference.artifacts로 참조를 풀어 차트는 브라우저에서 그리고(spec), 나머지는 PNG를 보인다
- Slack은 참조를 풀 수 없어 실제 주소로 바꿔 보낸다
- 화면은 모델이 쓴 이미지 주소를 열지 않는다 (주소에 데이터를 실어 보내는 반출)
"""
import copy
import json

import boto3
import pytest

from conftest import ROOT, load_service_module
from test_approvals import ORIGIN, FakeResponse, env  # noqa: F401 (env는 fixture)

BUCKET = "wga-diagrambucket-test"
BAR = {"data": json.dumps([{"category": "EC2", "value": 320.5}, {"category": "S3", "value": 45}]),
       "title": "서비스별 비용"}


@pytest.fixture
def artifacts():
    load_service_module("services/llm", "artifacts")
    import artifacts
    return artifacts


def mcp_result(body):
    return {"content": [{"type": "text", "text": json.dumps(body)}]}


URL = "https://wga-diagrambucket-123456789012-test.s3.amazonaws.com/charts/2026/09/26/bar_ab12cd34.png?X-Amz-Sig=x"
SPEC = {"type": "bar", "options": {"data": [{"category": "EC2", "value": 1}]}}


# ---------------------------------------------------------------- 작은 단위

def test_model_gets_a_ref_and_the_screen_gets_url_and_spec(artifacts):
    box = artifacts.Artifacts()
    body = {"status": "success", "url": URL, "s3_key": "charts/2026/09/26/bar_ab12cd34.png", "chart_type": "bar",
            "message": "Chart generated successfully: bar", "spec": SPEC}
    for_model = json.loads(box.take("generateBarChart", mcp_result(body))["content"][0]["text"])

    ref = "artifact://charts/2026/09/26/bar_ab12cd34.png"
    assert for_model["ref"] == ref and ref in for_model["message"]
    assert not {"url", "spec", "s3_key"} & set(for_model)
    assert box.public() == [{"ref": ref, "url": URL, "kind": "chart", "spec": SPEC}]


def test_failures_and_unknown_shapes_pass_through(artifacts):
    box = artifacts.Artifacts()
    failed = mcp_result({"status": "error", "message": "Invalid chart data"})
    odd_key = mcp_result({"status": "success", "url": URL, "s3_key": "charts/a b?.png"})
    assert box.take("generateBarChart", failed) is failed
    assert box.take("generateBarChart", odd_key) is odd_key
    assert box.take("generateBarChart", {"content": []}) == {"content": []}
    assert box.public() == []


def test_diagram_has_no_spec(artifacts):
    box = artifacts.Artifacts()
    box.take("generateArchitectureDiagram", mcp_result({"status": "success", "url": URL,
                                                         "s3_key": "diagrams/2026/09/26/wga_1234abcd.png",
                                                         "message": "Diagram generated"}))
    assert box.public() == [{"ref": "artifact://diagrams/2026/09/26/wga_1234abcd.png", "url": URL,
                             "kind": "diagram"}]


def test_sizes_are_limited(artifacts):
    box = artifacts.Artifacts()
    big = {"type": "bar", "options": {"data": "x" * (artifacts.MAX_SPEC_TOTAL - 100)}}
    for i in range(artifacts.MAX_ARTIFACTS):
        box.take("generateBarChart", mcp_result({"status": "success", "url": URL, "s3_key": f"charts/{i}.png",
                                                 "chart_type": "bar", "spec": big if i < 2 else SPEC}))
    items = box.public()
    # spec 합계를 넘는 것은 PNG로만 보인다 (대화 기록 크기)
    assert "spec" in items[0] and "spec" not in items[1]
    # 답변 하나에 결과물은 정해진 수까지
    over = box.take("generateBarChart", mcp_result({"status": "success", "url": URL, "s3_key": "charts/x.png",
                                                    "chart_type": "bar"}))
    assert over["isError"] is True and len(box.public()) == artifacts.MAX_ARTIFACTS


def test_slack_gets_real_urls(artifacts):
    box = artifacts.Artifacts()
    box.take("generateBarChart", mcp_result({"status": "success", "url": URL, "s3_key": "charts/a.png",
                                             "chart_type": "bar"}))
    text = "비용\n![비용](artifact://charts/a.png)\n![없음](artifact://charts/unknown.png)"
    assert box.with_urls(text) == f"비용\n![비용]({URL})\n![없음](artifact://charts/unknown.png)"


# ---------------------------------------------------------------- 처음부터 끝까지

def test_chart_reaches_the_screen_but_not_the_model(env, monkeypatch):  # noqa: F811
    boto3.client("s3").create_bucket(Bucket=BUCKET)
    llm = env["llm"]
    import mcp_anthropic_client

    sent = []

    def fake_post(url, headers=None, json=None, **kwargs):
        sent.append(copy.deepcopy(json))
        if len(sent) == 1:
            return FakeResponse({"content": [{"type": "tool_use", "id": "toolu_1", "name": "generateBarChart",
                                              "input": BAR}], "usage": {}, "stop_reason": "tool_use"})
        # 모델은 도구 결과의 ref를 답변에 넣는다
        result = json["messages"][-1]["content"][0]["content"]
        ref = result[result.index("artifact://"):].split('"')[0].split(")")[0]
        return FakeResponse({"content": [{"type": "text", "text": f"비용입니다.\n\n![서비스별 비용]({ref})"}],
                             "usage": {}, "stop_reason": "end_turn"})

    monkeypatch.setattr(mcp_anthropic_client.HTTP, "post", fake_post)
    client = mcp_anthropic_client.AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k",
                                                     model_id="claude-sonnet-5")
    client.tools = json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]
    monkeypatch.setattr(client.mcp_client, "call_tool", env["mcp"].call_tool)
    monkeypatch.setattr(llm, "get_client", lambda *_: client)

    body = json.loads(llm.handle_llm1_with_mcp({"text": "비용 차트 그려줘"}, ORIGIN, caller_id="alice")["body"])

    # 모델에게 간 도구 결과: 참조만 있고 주소·서명·데이터가 없다
    to_model = sent[1]["messages"][-1]["content"][0]["content"]
    assert "artifact://charts/" in to_model
    for word in ("https://", "X-Amz", BUCKET, "320.5", "spec"):
        assert word not in to_model, word
    assert "artifact://" in "".join(block["text"] for block in sent[0]["system"])  # 시스템 프롬프트가 참조를 그대로 옮기라고 한다

    # 화면에게: 답변의 참조와 같은 결과물이 주소·그릴 내용과 함께 있다
    [artifact] = body["inference"]["artifacts"]
    assert artifact["ref"] in body["answer"] and artifact["kind"] == "chart"
    assert BUCKET in artifact["url"] and artifact["url"].startswith("https://")
    assert artifact["spec"]["type"] == "bar" and artifact["spec"]["options"]["title"] == "서비스별 비용"
    # 주소가 가리기를 거치지 않아 실제로 열린다 (S3에 PNG가 있다)
    key = artifact["ref"].removeprefix("artifact://")
    assert boto3.client("s3").get_object(Bucket=BUCKET, Key=key)["ContentType"] == "image/png"


def test_chart_tool_returns_spec_only_when_small(env):  # noqa: F811
    boto3.client("s3").create_bucket(Bucket=BUCKET)
    small = json.loads(env["mcp"].call_tool("generateBarChart", BAR)["content"][0]["text"])
    assert small["spec"]["type"] == "bar" and small["s3_key"].startswith("charts/")
    big_data = json.dumps([{"category": f"항목 {i} " + "가" * 40, "value": i} for i in range(400)])
    big = json.loads(env["mcp"].call_tool("generateBarChart", {"data": big_data})["content"][0]["text"])
    assert big["status"] == "success" and "spec" not in big and big["s3_key"]


# ---------------------------------------------------------------- 화면 (정적 확인: 프런트엔드 테스트 도구가 없다)

def test_screen_never_opens_images_the_model_wrote():
    markdown = (ROOT / "frontend" / "src" / "utils" / "markdown.ts").read_text(encoding="utf-8")
    # 마크다운에서는 이미지를 열지 않는다: 모든 ![](주소)는 링크로만. 버킷 이름 모양으로 허용하지도 않는다
    # (S3 버킷 이름은 누구나 만들 수 있어 wga-diagrambucket-공격자 같은 버킷으로 우회된다)
    assert "<img" not in markdown and "markdown-image-link" in markdown
    # 결과물 이미지는 서버가 만든 목록(inference.artifacts)의 https 주소만 쓴다
    artifacts = (ROOT / "frontend" / "src" / "utils" / "artifacts.ts").read_text(encoding="utf-8")
    assert "protocol !== 'https:'" in artifacts
    view = (ROOT / "frontend" / "src" / "features" / "chat" / "ArtifactView.tsx").read_text(encoding="utf-8")
    assert view.count("<img") == 1 and "src={artifact.url}" in view
