"""공식 MCP 도구 목록 스냅샷 (mcp/lambda_mcp/official.py 모듈 설명).
- tools/list는 스냅샷 파일로 만든다: 공식 서버를 하나도 불러오지 않는다 (차가운 MCP Lambda의 첫 질문이 빨라진다)
- 도구를 부를 때는 그 도구의 서버 하나만 불러온다
- 스냅샷에는 보정 전 스키마를 적고, 읽을 때 이 Lambda의 리전으로 채운다
실제 공식 서버 대신 작은 FastMCP 서버 두 개로 확인한다."""
import json

import pytest
from fastmcp import FastMCP

from conftest import load_service_module


def make_servers():
    logs = FastMCP("logs")
    docs = FastMCP("docs")

    @logs.tool()
    def describe_log_groups(prefix: str = "") -> str:
        """로그 그룹 목록"""
        return f"groups:{prefix}"

    @logs.tool()
    def lookup_events(region: str) -> str:
        """이 리전의 이벤트"""
        return f"events:{region}"

    @docs.tool()
    def search_documentation(query: str) -> str:
        """문서 검색"""
        return f"docs:{query}"

    @docs.tool()
    def secret_tool() -> str:
        """목록에서 뺄 도구"""
        return "x"

    return {"logs": logs, "docs": docs}


@pytest.fixture
def official():
    return load_service_module("mcp", "lambda_mcp.official")


def tools_for(official, servers, path, imported=None, load_all=None):
    """스냅샷을 쓰는 OfficialTools. imported: 서버 하나를 불러올 때마다 이름을 적는다."""
    def import_server(key):
        if imported is not None:
            imported.append(key)
        return servers[key]

    return official.OfficialTools(
        load_all or (lambda: list(servers.values())), excluded={"secret_tool": "뺌"},
        region_from_env={"lookup_events"}, snapshot_path=str(path), server_keys=list(servers),
        import_server=import_server)


def test_tools_list_comes_from_the_snapshot_without_importing_servers(official, tmp_path, monkeypatch):
    servers = make_servers()
    path = tmp_path / "official_tools.json"
    assert tools_for(official, servers, path).write_snapshot(str(path)) == 3

    snapshot = json.loads(path.read_text())
    assert snapshot["version"] == official.SNAPSHOT_VERSION
    assert {tool["name"]: tool["server"] for tool in snapshot["tools"]} == {
        "describe_log_groups": "logs", "lookup_events": "logs", "search_documentation": "docs"}

    def must_not_load_all():
        raise AssertionError("스냅샷이 있으면 서버를 모두 불러오지 않는다")

    imported = []
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    tools = tools_for(official, servers, path, imported, load_all=must_not_load_all)

    names = [schema["name"] for schema in tools.schemas()]
    assert names == ["describe_log_groups", "lookup_events", "search_documentation"]
    assert tools.has("search_documentation") and not tools.has("secret_tool")
    assert imported == []  # 목록만으로는 아무것도 불러오지 않는다

    # 리전 보정은 읽을 때 이 Lambda의 리전으로 (스냅샷을 만든 곳의 리전이 아니다)
    finished = next(s for s in tools.schemas() if s["name"] == "lookup_events")["inputSchema"]
    raw = next(s for s in snapshot["tools"] if s["name"] == "lookup_events")["inputSchema"]
    assert finished["properties"]["region"]["default"] == "ap-southeast-2"
    assert "region" not in finished.get("required", [])  # 생략하면 채워 넣으므로 필수가 아니다
    assert "region" in raw["required"] and "default" not in raw["properties"]["region"]  # 스냅샷은 보정 전 그대로


def test_calling_a_tool_imports_only_its_server(official, tmp_path, monkeypatch):
    servers = make_servers()
    path = tmp_path / "official_tools.json"
    tools_for(official, servers, path).write_snapshot(str(path))
    imported = []
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    tools = tools_for(official, servers, path, imported, load_all=lambda: pytest.fail("모두 불러오지 않는다"))
    tools.schemas()

    content, is_error = tools.call("search_documentation", {"query": "lambda"})
    assert not is_error and content[0]["text"] == "docs:lambda"
    assert imported == ["docs"]

    tools.call("search_documentation", {"query": "s3"})  # 이미 불러온 서버는 다시 불러오지 않는다
    content, _ = tools.call("lookup_events", {})  # region을 빼면 이 Lambda의 리전으로 채운다
    assert content[0]["text"] == "events:ap-southeast-2"
    assert imported == ["docs", "logs"]


def test_without_a_snapshot_everything_is_collected_as_before(official, tmp_path):
    servers = make_servers()
    loaded = []

    def load_all():
        loaded.append(True)
        return list(servers.values())

    tools = tools_for(official, servers, tmp_path / "missing.json", load_all=load_all)
    assert len(tools.schemas()) == 3 and loaded == [True]
    content, _ = tools.call("describe_log_groups", {"prefix": "/aws"})
    assert content[0]["text"] == "groups:/aws"


def test_broken_or_old_snapshot_falls_back(official, tmp_path):
    servers = make_servers()
    for text in ("{not json", json.dumps({"version": 999, "tools": []}),
                 json.dumps({"version": official.SNAPSHOT_VERSION,
                             "tools": [{"name": "x", "inputSchema": {}, "server": "unknown"}]})):
        path = tmp_path / "official_tools.json"
        path.write_text(text)
        loaded = []
        tools = tools_for(official, servers, path, load_all=lambda: loaded.append(1) or list(servers.values()))
        assert len(tools.schemas()) == 3 and loaded == [1]


def test_default_uses_the_bundled_snapshot_path(official):
    tools = official.OfficialTools.default()
    assert tools._snapshot_path == official.SNAPSHOT_PATH
    assert tools._server_keys == list(official.SERVER_IMPORTERS)
