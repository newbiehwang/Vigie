"""사용자 관리 (services/llm/user_admin.py, cloudformation/llm.yaml의 UserAdmin*)

- 관리자만: 토큰의 그룹에 더해 Cognito에 지금 그룹과 정지 여부를 다시 묻는다 (토큰은 최대 1시간 남는다)
- 목록(그룹·상태), 그룹 넣기·빼기, 정지·해제, 초대. 삭제는 없다
- 자기 admins를 빼거나 자기를 정지할 수 없고, 마지막 관리자를 빼거나 정지할 수 없다
- 바꾸기 전에 감사 로그에 남기고, 남기지 못하면 바꾸지 않는다
- Cognito를 바꾸는 권한은 사용자 관리 Lambda 역할에만 있다 (LLM 역할에는 없다)
"""
import json

import boto3
import pytest

from conftest import load_service_module
from test_approvals import AUDIT_TABLE, ORIGIN, create_tables, statements


@pytest.fixture
def pool(aws, monkeypatch):
    """관리자 alice, 승인자 bob, 일반 사용자 carol이 있는 User Pool."""
    cognito = boto3.client("cognito-idp")
    pool_id = cognito.create_user_pool(PoolName="vigie-user-pool-test", UsernameAttributes=["email"])["UserPool"]["Id"]
    for group in ("admins", "approvers"):
        cognito.create_group(UserPoolId=pool_id, GroupName=group)
    users = {}
    for name in ("alice", "bob", "carol"):
        email = f"{name}@example.com"
        created = cognito.admin_create_user(UserPoolId=pool_id, Username=email, MessageAction="SUPPRESS",
                                            UserAttributes=[{"Name": "email", "Value": email}])
        users[name] = created["User"]["Username"]
    cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=users["alice"], GroupName="admins")
    cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=users["bob"], GroupName="approvers")
    monkeypatch.setenv("USER_POOL_ID", pool_id)
    create_tables()
    module = load_service_module("services/llm", "user_admin")
    return {"cognito": cognito, "id": pool_id, "users": users, "module": module,
            "audit": boto3.resource("dynamodb").Table(AUDIT_TABLE)}


def claims_of(pool, name="alice", groups="admins"):
    claims = {"sub": pool["users"][name], "cognito:username": pool["users"][name], "email": f"{name}@example.com"}
    if groups:
        claims["cognito:groups"] = groups
    return claims


def call(pool, method, path, name="alice", groups="admins", body=None, params=None):
    """(상태 코드, 본문) — 권한 부여자를 통과한 요청과 같은 모양으로 route를 부른다."""
    module = pool["module"]
    event = {"httpMethod": method, "path": path, "queryStringParameters": params,
             "body": json.dumps(body) if body is not None else None,
             "requestContext": {"authorizer": {"claims": claims_of(pool, name, groups) if name else {}}}}
    try:
        return module.route(event, cognito=pool["cognito"], audit_table=pool["audit"])
    except module.UserAdminError as error:
        return error.status, {"error": str(error)}


def groups_of(pool, name):
    found = pool["cognito"].admin_list_groups_for_user(UserPoolId=pool["id"], Username=pool["users"][name])
    return sorted(group["GroupName"] for group in found["Groups"])


def admin_rows(pool):
    return [item for item in pool["audit"].scan()["Items"] if item["kind"] == "admin"]


# ---------------------------------------------------------------- 권한

@pytest.mark.parametrize("name, groups, status", [
    (None, None, 401),                 # 로그인하지 않음
    ("carol", None, 403),              # 일반 사용자
    ("bob", "approvers", 403),         # 승인자만으로는 안 된다
])
def test_only_admins_can_manage_users(pool, name, groups, status):
    assert call(pool, "GET", "/users", name=name, groups=groups)[0] == status


def test_token_is_not_trusted_alone(pool):
    # carol의 토큰에 admins가 남아 있어도(그룹을 뺀 직후) Cognito의 지금 그룹이 아니면 막는다
    status, body = call(pool, "GET", "/users", name="carol", groups="admins")
    assert status == 403 and "다시 로그인" in body["error"]
    # 정지된 관리자의 토큰도 막는다
    pool["cognito"].admin_add_user_to_group(UserPoolId=pool["id"], Username=pool["users"]["bob"], GroupName="admins")
    pool["cognito"].admin_disable_user(UserPoolId=pool["id"], Username=pool["users"]["bob"])
    status, body = call(pool, "GET", "/users", name="bob", groups="[admins approvers]")
    assert status == 403 and "정지" in body["error"]


# ---------------------------------------------------------------- 조회

def test_list_shows_roles_status_and_self(pool):
    status, body = call(pool, "GET", "/users")
    assert status == 200 and body["groups"] == ["admins", "approvers"]
    users = {user["email"]: user for user in body["users"]}
    # 권한 세 단계: 관리자(admins만 넣은 예전 계정도 관리자) · 결정자(approvers) · 일반 사용자(그룹 없음)
    assert users["alice@example.com"]["role"] == "admin" and users["alice@example.com"]["isSelf"] is True
    assert users["bob@example.com"]["role"] == "decider" and users["bob@example.com"]["isSelf"] is False
    assert users["carol@example.com"]["role"] == "member" and users["carol@example.com"]["enabled"] is True
    assert users["carol@example.com"]["username"] == pool["users"]["carol"]


def test_search_by_email_prefix_and_reject_filter_injection(pool):
    status, body = call(pool, "GET", "/users", params={"q": "bo"})
    assert status == 200 and [user["email"] for user in body["users"]] == ["bob@example.com"]
    # 검색어는 Cognito Filter 글자 안에 들어간다: 따옴표로 조건을 바꾸지 못하게 받지 않는다
    assert call(pool, "GET", "/users", params={"q": 'x" or email ^= "'})[0] == 400


# ---------------------------------------------------------------- 바꾸기

def role(pool, name, value, caller="alice"):
    return call(pool, "PUT", f"/users/{pool['users'][name]}/role", name=caller, body={"role": value})


def test_role_changes_are_audited(pool):
    # 일반 사용자 → 결정자 → 관리자(결정자의 일도 한다) → 일반 사용자
    for value, groups in (("decider", ["approvers"]), ("admin", ["admins", "approvers"]), ("member", [])):
        status, body = role(pool, "carol", value)
        assert status == 200 and body["role"] == value and groups_of(pool, "carol") == groups
    # 같은 권한으로 바꾸면 아무것도 하지 않고 기록도 남기지 않는다
    assert role(pool, "carol", "member")[0] == 200

    rows = sorted(admin_rows(pool), key=lambda row: row["at"])
    assert [(r["event"], r["fromRole"], r["toRole"], r["targetEmail"], r["status"]) for r in rows] == [
        ("role_changed", "member", "decider", "carol@example.com", "ok"),
        ("role_changed", "decider", "admin", "carol@example.com", "ok"),
        ("role_changed", "admin", "member", "carol@example.com", "ok")]
    assert all(r["decidedBy"] == pool["users"]["alice"] and r["userId"] == pool["users"]["alice"] for r in rows)


def test_only_known_roles_and_users(pool):
    carol = pool["users"]["carol"]
    assert role(pool, "carol", "superuser")[0] == 400
    assert call(pool, "PUT", "/users/no-such-user/role", body={"role": "decider"})[0] == 404
    assert call(pool, "DELETE", f"/users/{carol}")[0] == 404  # 삭제 경로는 없다
    assert call(pool, "POST", f"/users/{carol}/groups/admins")[0] == 404  # 그룹을 하나씩 넣는 경로도 없다
    assert admin_rows(pool) == []


def test_admins_cannot_lock_themselves_out(pool):
    alice = pool["users"]["alice"]
    for value in ("decider", "member"):
        status, body = role(pool, "alice", value)
        assert status == 409 and "자기" in body["error"]
    assert call(pool, "POST", f"/users/{alice}/disable")[0] == 409
    assert groups_of(pool, "alice") == ["admins"] and admin_rows(pool) == []


def test_last_admin_cannot_be_removed_or_disabled(pool):
    # 부른 사람이 그 관리자가 아닌 경우의 방어 (권한 확인을 거친 뒤라 보통은 자기 자신 규칙이 먼저 막는다)
    module = pool["module"]
    import audit
    from redaction import Redactor
    log = audit.AuditLog(pool["audit"], None, Redactor(), user_id="ops", email=None, source="web",
                         request_id="r-1", session_id=None, model_id=None, question="")
    admin = module.UserAdmin(pool["cognito"], "someone-else", {}, log)
    alice = pool["users"]["alice"]
    for action in (lambda: admin.set_role(alice, "decider"), lambda: admin.set_enabled(alice, False)):
        with pytest.raises(module.UserAdminError) as error:
            action()
        assert error.value.status == 409 and "마지막 관리자" in str(error.value)
    # 관리자가 둘이면 다른 관리자는 내릴 수 있다
    assert role(pool, "bob", "admin")[1]["role"] == "admin"
    status, body = role(pool, "bob", "decider")
    assert status == 200 and body["role"] == "decider" and groups_of(pool, "bob") == ["approvers"]


def test_disable_and_enable(pool):
    carol = pool["users"]["carol"]
    status, body = call(pool, "POST", f"/users/{carol}/disable")
    assert status == 200 and body["enabled"] is False
    assert pool["cognito"].admin_get_user(UserPoolId=pool["id"], Username=carol)["Enabled"] is False
    status, body = call(pool, "POST", f"/users/{carol}/enable")
    assert status == 200 and body["enabled"] is True
    assert sorted(r["event"] for r in admin_rows(pool)) == ["disabled", "enabled"]


def test_invite(pool):
    status, body = call(pool, "POST", "/users", body={"email": "dave@example.com"})
    assert status == 200 and body["email"] == "dave@example.com" and body["status"] == "FORCE_CHANGE_PASSWORD"
    assert body["groups"] == [] and body["role"] == "member"  # 초대하면 일반 사용자로 시작한다
    assert [r["event"] for r in admin_rows(pool)] == ["invited"]
    # 이미 있는 사람·잘못된 주소는 기록 전에 거른다
    assert call(pool, "POST", "/users", body={"email": "dave@example.com"})[0] == 409
    assert call(pool, "POST", "/users", body={"email": "not-an-email"})[0] == 400
    assert len(admin_rows(pool)) == 1


def test_cognito_failure_is_recorded_too(pool, monkeypatch):
    def refuse(**kwargs):
        raise RuntimeError("TooManyRequestsException")

    monkeypatch.setattr(pool["cognito"], "admin_add_user_to_group", refuse)
    status, body = role(pool, "carol", "decider")
    assert status == 502 and "TooManyRequests" in body["error"]
    rows = sorted(admin_rows(pool), key=lambda row: row["at"])
    assert [(r["event"], r["status"]) for r in rows] == [("role_changed", "ok"), ("role_changed", "error")]
    assert "TooManyRequests" in rows[1]["error"]


def test_nothing_changes_without_an_audit_record(pool, monkeypatch):
    import audit

    def broken(self, *args, **kwargs):
        raise RuntimeError("DynamoDB unavailable")

    monkeypatch.setattr(audit.AuditLog, "admin_event", broken)
    status, _ = role(pool, "carol", "admin")
    assert status == 503 and groups_of(pool, "carol") == []


def test_lambda_handler(pool):
    event = {"httpMethod": "OPTIONS", "path": "/users", "headers": {"origin": ORIGIN}}
    assert pool["module"].lambda_handler(event, None)["statusCode"] == 200
    event = {"httpMethod": "GET", "path": "/users", "headers": {"origin": ORIGIN},
             "requestContext": {"authorizer": {"claims": claims_of(pool, "carol", None)}}}
    response = pool["module"].lambda_handler(event, None)
    assert response["statusCode"] == 403 and response["headers"]["Access-Control-Allow-Origin"] == ORIGIN


# ---------------------------------------------------------------- 권한 분리 (CloudFormation)

def test_only_the_user_admin_role_can_change_users():
    llm_actions = {a for s in statements("LlmLambdaExecutionRole") for a in s["Action"]}
    assert not any(action.startswith("cognito-idp:") for action in llm_actions)  # 모델을 돌리는 역할에는 없다

    cognito = [s for s in statements("UserAdminExecutionRole") if any(a.startswith("cognito-idp:") for a in s["Action"])]
    assert len(cognito) == 1 and "userpool/${UserPoolId}" in cognito[0]["Resource"]  # 이 환경의 풀만
    actions = set(cognito[0]["Action"])
    for forbidden in ("cognito-idp:AdminDeleteUser", "cognito-idp:AdminSetUserPassword",
                      "cognito-idp:AdminUpdateUserAttributes", "cognito-idp:DeleteGroup", "cognito-idp:*"):
        assert forbidden not in actions
    # 감사 로그는 추가만
    dynamo = {a for s in statements("UserAdminExecutionRole") for a in s["Action"] if a.startswith("dynamodb:")}
    assert dynamo == {"dynamodb:PutItem"}
