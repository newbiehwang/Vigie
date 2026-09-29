"""AWS 공식 IAM MCP 서버 (mcp/lambda_mcp/official.py): 읽기 전용

IAM 변경(사용자·역할 생성, 정책 붙이기, 액세스 키 발급 등)은 네 겹으로 막는다.
  1. 변경 도구 17개를 도구 목록에서 뺀다 (official.py EXCLUDED_TOOLS)
  2. 서버 자체의 읽기 전용 모드를 켠다 (load_default_servers)
  3. 위험도 목록에 없는 도구는 MCP가 변경 도구로 보고 거절한다 (risk.py)
  4. MCP 역할에 IAM 쓰기 권한이 없다 (llm.yaml)
조회는 moto로 실제 IAM API까지 부른다.
"""
import json

import boto3
import pytest
import yaml

from conftest import ROOT
from test_approvals import env  # noqa: F401 (fixture)

READ_TOOLS = {"list_users", "get_user", "list_roles", "list_policies", "get_managed_policy_document",
              "simulate_principal_policy", "list_groups", "get_group", "get_user_policy", "get_role_policy",
              "list_user_policies", "list_role_policies"}
WRITE_TOOLS = {"add_user_to_group", "attach_group_policy", "attach_user_policy", "create_access_key", "create_group",
               "create_role", "create_user", "delete_access_key", "delete_group", "delete_role_policy", "delete_user",
               "delete_user_policy", "detach_group_policy", "detach_user_policy", "put_role_policy", "put_user_policy",
               "remove_user_from_group"}
ROLE = "vigie-llm-execution-role-test"
TRUST = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                                                 "Action": "sts:AssumeRole"}]}
INLINE = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "logs:DescribeLogGroups",
                                                  "Resource": "*"}]}


@pytest.fixture
def iam_env(env):  # noqa: F811
    tools = {t["name"]: t for t in json.loads(env["mcp"]._rpc("tools/list")["body"])["result"]["tools"]}
    iam = boto3.client("iam")
    iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(TRUST))
    iam.put_role_policy(RoleName=ROLE, PolicyName="LlmReadLogs", PolicyDocument=json.dumps(INLINE))
    iam.create_user(UserName="alice")
    iam.create_access_key(UserName="alice")
    return env, tools


def text_of(result):
    assert not result.get("isError"), result
    return json.dumps(result, ensure_ascii=False)


def test_only_read_tools_are_attached(iam_env):
    _, tools = iam_env
    assert READ_TOOLS <= set(tools) and not (WRITE_TOOLS & set(tools))
    assert all(tools[name]["_meta"]["vigie/risk"] == "read" for name in READ_TOOLS)


def test_write_tools_cannot_be_called_even_by_name(iam_env):
    env, _ = iam_env
    for name, args in (("create_user", {"user_name": "mallory"}), ("create_access_key", {"user_name": "alice"}),
                       ("attach_user_policy", {"user_name": "alice",
                                               "policy_arn": "arn:aws:iam::aws:policy/AdministratorAccess"})):
        with pytest.raises(Exception, match="not found"):
            env["mcp"].call_admin(name, args)
    users = [u["UserName"] for u in boto3.client("iam").list_users()["Users"]]
    assert users == ["alice"]
    assert len(boto3.client("iam").list_access_keys(UserName="alice")["AccessKeyMetadata"]) == 1


def test_user_tools_work_despite_the_upstream_ctx_bug(iam_env):
    # IAM 1.1.1의 list_users·get_user는 ctx가 필수 입력값으로 잘못 드러난다. 스키마에서 빼고 부를 때 채워 넣는다
    env, tools = iam_env
    for name in ("list_users", "get_user"):
        schema = tools[name]["inputSchema"]
        assert "ctx" not in schema.get("properties", {}) and "ctx" not in schema.get("required", [])
    assert "alice" in text_of(env["mcp"].call_admin("list_users", {}))
    assert "alice" in text_of(env["mcp"].call_admin("get_user", {"user_name": "alice"}))


def test_hidden_argument_patch_does_nothing_when_the_argument_is_gone(iam_env):
    # 공식 서버가 고쳐져 스키마에 ctx가 없어지면 보정하지 않는다 (없는 인자를 넣어 오류를 만들지 않는다)
    tools = iam_env[0]["app"].OfficialTools(lambda: [], hidden_arguments={"get_user": {"ctx": {"content": []}}})
    schema = {"type": "object", "properties": {"user_name": {"type": "string"}}, "required": ["user_name"]}
    assert tools._without_hidden("get_user", schema) == schema and tools._injected == {}


def test_server_runs_in_its_own_read_only_mode(iam_env):
    from awslabs.iam_mcp_server.context import Context
    assert Context.is_readonly() is True


def test_roles_and_inline_policies_can_be_read(iam_env):
    env, _ = iam_env
    assert ROLE in text_of(env["mcp"].call_admin("list_roles", {}))
    assert "LlmReadLogs" in text_of(env["mcp"].call_admin("list_role_policies", {"role_name": ROLE}))
    policy = text_of(env["mcp"].call_admin("get_role_policy", {"role_name": ROLE, "policy_name": "LlmReadLogs"}))
    assert "logs:DescribeLogGroups" in policy


def test_access_key_ids_in_results_are_pseudonymized_before_claude(iam_env):
    # get_user는 액세스 키 ID도 돌려준다. Claude로 보내기 전에 가명으로 바꾸고, 그 가명으로 다시 조회하면 되돌린다
    env, _ = iam_env
    raw = text_of(env["mcp"].call_admin("get_user", {"user_name": "alice"}))
    key_id = boto3.client("iam").list_access_keys(UserName="alice")["AccessKeyMetadata"][0]["AccessKeyId"]
    assert key_id in raw
    from redaction import Redactor
    redactor = Redactor()
    alias = key_id[:4] + "********" + key_id[-4:]
    masked = redactor.text(raw)
    assert key_id not in masked and alias in masked
    assert redactor.restore({"resource": alias}) == {"resource": key_id}


def test_mcp_role_has_no_iam_write_permissions():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    template = yaml.load((ROOT / "cloudformation" / "llm.yaml").read_text(encoding="utf-8"), Loader=Loader)
    statements = template["Resources"]["McpLambdaExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"]
    actions = {a for s in statements if s["Effect"] == "Allow" for a in s["Action"] if a.startswith("iam:")}
    assert actions and all(a.split(":")[1].startswith(("List", "Get", "Simulate")) for a in actions), actions
    assert "iam:CreateAccessKey" not in actions and "iam:PassRole" not in actions
