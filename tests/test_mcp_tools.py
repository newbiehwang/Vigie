"""MCP 서버: 직접 둔 도구, AWS 공식 MCP 서버 도구(CloudWatch·문서·Cost Explorer), 세션 저장소 (moto)"""
import json

import boto3
import pytest

from conftest import load_service_module

SESSION_TABLE = "vigie-mcp-sessions-test"


@pytest.fixture
def app(aws, monkeypatch):
    monkeypatch.setenv("MCP_SESSION_TABLE", SESSION_TABLE)
    boto3.client("dynamodb").create_table(
        TableName=SESSION_TABLE,
        AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return load_service_module("mcp", "app")


def test_dashboards_list_and_summary(app):
    body = {"widgets": [{"type": "metric", "x": 0, "y": 0, "width": 12, "height": 6,
                         "properties": {"title": "Lambda Errors", "region": "us-east-1",
                                        "metrics": [["AWS/Lambda", "Errors"]]}}]}
    boto3.client("cloudwatch").put_dashboard(DashboardName="vigie-test-service", DashboardBody=json.dumps(body))

    listed = app.list_cloudwatch_dashboards()
    assert listed["status"] == "success"
    assert [d["DashboardName"] for d in listed["dashboards"]] == ["vigie-test-service"]

    summary = app.get_dashboard_summary("vigie-test-service")
    assert summary["widgets_count"] == 1
    assert summary["widgets_summary"][0]["properties"]["title"] == "Lambda Errors"


def test_tools_are_registered_with_schemas(app):
    # 직접 둔 도구의 이름은 함수 이름을 camelCase로 바꿔 등록된다 (lambda_mcp.LambdaMCPServer.tool)
    tools = app.mcp_server.tools
    for name in ["listCloudwatchDashboards", "getDashboardSummary", "generateArchitectureDiagram"]:
        assert name in tools
    assert tools["getDashboardSummary"]["inputSchema"]["required"] == ["dashboard_name"]
    # 공식 도구로 바뀐 옛 도구는 없다
    assert "fetchCloudwatchLogsForService" not in tools and "getDetailedBreakdownByDay" not in tools


def test_session_lifecycle(app):
    from lambda_mcp.session import SessionManager
    sessions = SessionManager(table_name=SESSION_TABLE)

    sid = sessions.create_session({"client": "llm"})
    assert sessions.get_session(sid)["client"] == "llm"
    assert sessions.update_session(sid, {"client": "slack"})
    assert sessions.get_session(sid)["client"] == "slack"
    assert sessions.delete_session(sid)
    assert sessions.get_session(sid) is None


# ---------------------------------------------------------------- AWS 공식 MCP 서버 도구 (lambda_mcp/official.py)
# LLM Lambda가 부르는 것과 같은 길(Function URL로 오는 JSON-RPC 요청 → lambda_handler)로 확인한다.

def rpc(app, method, params=None, session_id=None):
    headers = {"content-type": "application/json"}
    if session_id:
        headers["mcp-session-id"] = session_id
    event = {"httpMethod": "POST", "headers": headers,
             "body": json.dumps({"jsonrpc": "2.0", "id": "1", "method": method, "params": params or {}})}
    response = app.lambda_handler(event, None)
    return response, json.loads(response.get("body") or "{}")


@pytest.fixture
def session(app):
    response, _ = rpc(app, "initialize")
    return response["headers"]["MCP-Session-Id"]


def call(app, session, name, arguments):
    response, body = rpc(app, "tools/call", {"name": name, "arguments": arguments}, session)
    assert response["statusCode"] == 200, body
    return body["result"]


def test_tools_list_merges_official_and_own_tools(app, session):
    _, body = rpc(app, "tools/list", session_id=session)
    tools = {tool["name"]: tool for tool in body["result"]["tools"]}

    # 공식 CloudWatch · 문서 · Cost Explorer 도구와 직접 둔 도구가 한 목록에 있다
    for name in ["describe_log_groups", "execute_log_insights_query", "get_metric_data", "get_active_alarms",
                 "search_documentation", "read_documentation", "cost-explorer",
                 "listCloudwatchDashboards", "generateLineChart"]:
        assert name in tools, name
    # 뺀 도구 (PromQL, 로그 인덱스 추천, 일괄 Insights)
    for name in app.mcp_server.external._excluded:
        assert name not in tools
    # LLM Lambda는 properties만 옮겨 가므로 $ref가 남아 있으면 안 된다
    assert "$ref" not in json.dumps(body) and "$defs" not in json.dumps(body)
    # 참조를 풀어 넣은 스키마: get_metric_data의 dimensions 항목은 name·value를 가진 객체다
    dimensions = tools["get_metric_data"]["inputSchema"]["properties"]["dimensions"]
    assert "name" in json.dumps(dimensions) and "value" in json.dumps(dimensions)


def test_official_cloudwatch_tool_reads_alarms(app, session):
    cw = boto3.client("cloudwatch")
    cw.put_metric_alarm(AlarmName="vigie-llm-errors", MetricName="Errors", Namespace="AWS/Lambda", Statistic="Sum",
                        Period=60, EvaluationPeriods=1, Threshold=1, ComparisonOperator="GreaterThanThreshold")
    cw.set_alarm_state(AlarmName="vigie-llm-errors", StateValue="ALARM", StateReason="test")

    result = call(app, session, "get_active_alarms", {})

    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert "vigie-llm-errors" in result["content"][0]["text"]


def test_official_tool_error_is_a_result_not_an_rpc_error(app, session):
    # 필수 인자(alarm_name)가 없다. MCP 규약대로 isError 결과로 돌려줘야 모델이 읽고 다시 부를 수 있다
    result = call(app, session, "get_alarm_history", {})
    assert result["isError"] is True
    assert "alarm_name" in result["content"][0]["text"]


def test_official_cost_explorer_tool(app, session):
    # Cost Explorer 서버(fastmcp)는 MCP 세션이 있어야 도구가 돈다: in-memory 클라이언트로 부르는지 확인
    result = call(app, session, "cost-explorer", {
        "operation": "getCostAndUsage", "start_date": "2026-09-01", "end_date": "2026-09-20",
        "granularity": "DAILY", "metrics": '["UnblendedCost"]'})
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["status"] == "success"


def test_official_documentation_search(app, session, monkeypatch):
    import httpx

    # 문서 검색 API를 가짜 응답으로 바꾼다 (테스트에서 인터넷에 나가지 않게)
    def handler(request):
        assert "search" in str(request.url)
        return httpx.Response(200, json={"queryId": "q1", "suggestions": [{"textExcerptSuggestion": {
            "title": "Lambda 함수 로그 보기", "link": "https://docs.aws.amazon.com/lambda/latest/dg/monitoring-logs.html",
            "summary": "CloudWatch Logs로 Lambda 로그를 봅니다."}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *args, **kwargs: real_client(*args, transport=httpx.MockTransport(handler), **kwargs))

    result = call(app, session, "search_documentation", {"search_phrase": "lambda logs"})
    assert result["isError"] is False
    assert "monitoring-logs.html" in result["content"][0]["text"]


def test_unknown_tool_is_not_found(app, session):
    response, body = rpc(app, "tools/call", {"name": "fetch_cloudwatch_logs_for_service", "arguments": {}}, session)
    assert response["statusCode"] == 404 and "not found" in body["error"]["message"]


def test_inline_refs_resolves_nested_definitions():
    from conftest import load_service_module
    official = load_service_module("mcp", "lambda_mcp.official")
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/Dimension"}},
                       "loop": {"$ref": "#/$defs/Node"}},
        "$defs": {"Dimension": {"type": "object", "properties": {"name": {"type": "string"}}},
                  "Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
    }
    resolved = official._inline_refs(schema)
    assert resolved["properties"]["items"]["items"]["properties"]["name"] == {"type": "string"}
    assert "$defs" not in json.dumps(resolved) and "$ref" not in json.dumps(resolved)
    # 자기 자신을 가리키는 참조는 한 번만 풀고 멈춘다
    assert resolved["properties"]["loop"]["properties"]["child"] == {"type": "object"}


# ---------------------------------------------------------------- Lambda처럼 설치 폴더가 읽기 전용일 때
# Lambda 컨테이너에서 쓸 수 있는 곳은 /tmp뿐이다. 공식 서버 중 파일을 설치 폴더에 쓰려는 것이 있으면
# MCP Lambda가 시작하지 못한다 (billing-cost-management 서버가 import할 때 awslabs/logs 폴더를 만들려다 실패했다).
# 로컬·CI의 가상 환경은 쓸 수 있어서 위 테스트로는 드러나지 않으므로, 읽기 전용 설치 폴더를 만들어 확인한다.

READ_ONLY_CHECK = r"""
import json, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]  # 읽기 전용 awslabs, mcp 폴더
from moto import mock_aws

with mock_aws():
    from lambda_mcp.official import OfficialTools
    tools = OfficialTools.default()
    names = [schema["name"] for schema in tools.schemas()]  # 여기서 공식 서버를 불러온다
    import awslabs.billing_cost_management_mcp_server as billing
    content, is_error = tools.call("cost-explorer", {
        "operation": "getCostAndUsage", "start_date": "2026-09-01", "end_date": "2026-09-20",
        "granularity": "DAILY", "metrics": '["UnblendedCost"]'})
    print(json.dumps({"billing_file": billing.__file__, "names": names, "is_error": is_error}))
"""


def test_official_servers_start_when_package_dir_is_read_only(tmp_path):
    import os
    import stat
    import subprocess
    import sys
    from pathlib import Path

    import awslabs.billing_cost_management_mcp_server as billing

    if os.geteuid() == 0:
        pytest.skip("root는 읽기 전용 폴더에도 쓸 수 있어 확인할 수 없다")

    # 설치된 awslabs 폴더와 같은 모양의 폴더를 만든다: 하위 패키지는 링크로 두고 폴더 자체는 읽기 전용.
    # 공식 서버는 자기 파일 경로(__file__)를 기준으로 폴더를 찾으므로, 이 폴더 안에 쓰려고 하면 실패한다
    installed = Path(billing.__file__).resolve().parent.parent
    read_only = tmp_path / "awslabs"
    read_only.mkdir()
    for entry in installed.iterdir():
        if entry.name.endswith("_mcp_server") or entry.name == "__init__.py":
            (read_only / entry.name).symlink_to(entry)
    read_only.chmod(stat.S_IRUSR | stat.S_IXUSR)

    # 설정을 새로 읽도록 새 프로세스에서 import한다 (이 프로세스는 이미 공식 서버를 불러왔다)
    env = {k: v for k, v in os.environ.items() if k not in ("FASTMCP_LOG_FILE", "MCP_SQL_THRESHOLD")}
    mcp_dir = Path(__file__).resolve().parent.parent / "mcp"
    try:
        done = subprocess.run([sys.executable, "-c", READ_ONLY_CHECK, str(tmp_path), str(mcp_dir)],
                              env=env, capture_output=True, text=True, timeout=120)
    finally:
        read_only.chmod(stat.S_IRWXU)  # pytest가 임시 폴더를 지울 수 있게

    assert done.returncode == 0, done.stderr[-3000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["billing_file"].startswith(str(read_only))  # 정말 읽기 전용 폴더에서 불러왔는지
    assert "cost-explorer" in result["names"] and "describe_log_groups" in result["names"]
    assert "lookup_events" in result["names"]  # CloudTrail 서버도 읽기 전용 폴더에서 뜬다
    assert "get_pricing" in result["names"]  # Pricing 서버도
    assert "simulate_principal_policy" in result["names"]  # IAM 서버도
    assert "get_eni_details" in result["names"]  # 네트워크 서버도
    assert result["is_error"] is False


LAZY_CHECK = r"""
import json, sys
sys.path.insert(0, sys.argv[1])  # mcp 폴더
from moto import mock_aws

loaded = lambda: sorted(name for name in sys.modules if name.startswith("awslabs."))
with mock_aws():
    from lambda_mcp.official import OfficialTools
    tools = OfficialTools.default()
    before = loaded()
    tools.schemas()
    print(json.dumps({"before": before, "after": loaded()}))
"""


def test_official_servers_load_on_first_tools_list():
    # Lambda 초기화 단계(10초 제한)에서 무거운 공식 서버를 불러오지 않는다: 도구 목록이 처음 필요할 때 불러온다.
    # 이 프로세스는 이미 공식 서버를 불러왔으므로 새 프로세스에서 확인한다
    import subprocess
    import sys
    from pathlib import Path

    mcp_dir = Path(__file__).resolve().parent.parent / "mcp"
    done = subprocess.run([sys.executable, "-c", LAZY_CHECK, str(mcp_dir)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["before"] == []
    assert "awslabs.cloudwatch_mcp_server.server" in result["after"]
