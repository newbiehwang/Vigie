"""사용자 관리 (관리자 화면의 '사용자 관리' 탭): Cognito 사용자 목록 · 권한 · 정지 · 초대

이 파일은 LLM Lambda와 같은 코드 묶음에 들어 있지만 **다른 Lambda(vigie-user-admin-<env>)가 다른 역할로** 실행한다.
Cognito 사용자를 바꾸는 권한은 그 역할에만 있고, 모델을 돌리는 LLM Lambda 역할에는 없다 (cloudformation/llm.yaml).

권한은 세 단계이고, 위 단계는 아래 단계를 모두 할 수 있다. Cognito 그룹 이름은 바꾸지 않는다
(그룹 이름을 바꾸면 CloudFormation이 그룹을 새로 만들어 구성원이 빠진다)
    member   일반 사용자  그룹 없음            질문·조회만 (스스로 가입하면 이 권한)
    decider  결정자       approvers            위에 더해 AI가 요청한 변경 작업을 승인·거절
    admin    관리자       admins + approvers   위에 더해 감사 로그·사용자 관리

API (API Gateway, Cognito 권한 부여자)
    GET    /users?q=<이메일 앞부분>&cursor=<다음 쪽>   목록 (권한·상태 포함)
    POST   /users                {"email": ...}          초대 (임시 비밀번호가 든 메일, 7일). 일반 사용자로 시작한다
    PUT    /users/{username}/role  {"role": ...}         권한 바꾸기 (member, decider, admin)
    POST   /users/{username}/disable                     정지 (그 사용자의 갱신 토큰도 모두 무효로)
    POST   /users/{username}/enable                      정지 해제
{username}은 Cognito 사용자 이름이다. 이메일로 로그인하는 풀이라 sub와 같은 UUID이고, 목록이 돌려준다.

권한
- 토큰의 그룹이 admins이고, Cognito에 **다시 물어도** admins이며 정지되지 않은 계정이어야 한다. 그룹을 빼거나 정지해도
  토큰은 최대 1시간 남아 있으므로, 사람을 바꾸는 이 API는 토큰만 믿지 않는다.
- 삭제는 없다. 정지로 충분하고 되돌릴 수 없어서다 (deploy.sh도 사용자를 지우지 않는다).

사고 막기
- 자기 권한을 관리자 아래로 내리거나 자기 계정을 정지할 수 없다 (실수로 잠기지 않게)
- 마지막 관리자(정지되지 않은 admins)를 내리거나 정지할 수 없다 (아무도 관리할 수 없게 되지 않게)
- 바꾸기 전에 감사 로그에 남긴다. 남기지 못하면 바꾸지 않는다. 바꾸다 실패하면 실패도 남긴다 (audit.admin_event)

알아 둘 한계
- 정지해도 이미 발급된 ID·액세스 토큰은 만료(최대 1시간)까지 쓸 수 있다. 갱신 토큰은 바로 무효가 된다.
- 권한을 바꾼 사람의 화면·권한은 그 사람이 다시 로그인하거나 토큰이 갱신된 뒤에 바뀐다.
"""
import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional

import boto3

from audit import AuditLog, CloudWatchSink, is_admin
from redaction import Redactor

ADMIN_GROUP = "admins"
MANAGED_GROUPS = ("admins", "approvers")  # 이 화면이 다루는 그룹
# 권한 → 그 권한이 속하는 그룹 (모듈 설명). 관리자는 결정자의 일도 한다
ROLE_GROUPS = {"member": (), "decider": ("approvers",), "admin": ("admins", "approvers")}
PAGE_SIZE = 50
EMAIL = re.compile(r"^[^@\s\"\\]+@[^@\s\"\\]+\.[^@\s\"\\]+$")
QUERY = re.compile(r"^[^\"\\]{0,100}$")  # Cognito ListUsers의 Filter 글자 안에 들어가므로 따옴표·역슬래시는 받지 않는다


class UserAdminError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _client():
    return boto3.client("cognito-idp")


def _pool() -> str:
    pool = os.environ.get("USER_POOL_ID")
    if not pool:
        raise UserAdminError(503, "User Pool이 설정되지 않았습니다")
    return pool


def _attribute(user: Dict[str, Any], name: str) -> Optional[str]:
    attributes = user.get("Attributes") or user.get("UserAttributes") or []
    return next((a.get("Value") for a in attributes if a.get("Name") == name), None)


def _members(cognito, group: str) -> set:
    """그룹의 사용자 이름 (쪽을 넘겨 모두)."""
    names, token = set(), None
    while True:
        kwargs = {"UserPoolId": _pool(), "GroupName": group, "Limit": 60}
        if token:
            kwargs["NextToken"] = token
        page = cognito.list_users_in_group(**kwargs)
        names.update(user["Username"] for user in page.get("Users", []))
        token = page.get("NextToken")
        if not token:
            return names


def _enabled_admins(cognito) -> List[str]:
    names, token = [], None
    while True:
        kwargs = {"UserPoolId": _pool(), "GroupName": ADMIN_GROUP, "Limit": 60}
        if token:
            kwargs["NextToken"] = token
        page = cognito.list_users_in_group(**kwargs)
        names += [user["Username"] for user in page.get("Users", []) if user.get("Enabled", True)]
        token = page.get("NextToken")
        if not token:
            return names


def role_of(groups) -> str:
    """그룹 → 권한. admins가 있으면 관리자 (예전에 admins만 넣은 계정도 관리자로 보인다)."""
    names = set(groups or [])
    if ADMIN_GROUP in names:
        return "admin"
    return "decider" if "approvers" in names else "member"


def _view(user: Dict[str, Any], groups: List[str], caller: str) -> Dict[str, Any]:
    created = user.get("UserCreateDate")
    return {
        "username": user["Username"],
        "email": _attribute(user, "email"),
        "name": _attribute(user, "name"),
        "status": user.get("UserStatus"),  # CONFIRMED, FORCE_CHANGE_PASSWORD(초대 후 첫 로그인 전), UNCONFIRMED 등
        "enabled": bool(user.get("Enabled", True)),
        "createdAt": created.isoformat() if hasattr(created, "isoformat") else created,
        "groups": groups,
        "role": role_of(groups),
        "isSelf": user["Username"] == caller,
    }


class UserAdmin:
    """관리자 한 사람의 요청 하나. 권한을 확인한 뒤에만 만든다 (authorize)."""

    def __init__(self, cognito, caller: str, claims: Dict[str, Any], audit: AuditLog):
        self.cognito = cognito
        self.caller = caller  # 부른 사람의 Cognito 사용자 이름
        self.claims = claims
        self.audit = audit

    @classmethod
    def authorize(cls, claims: Dict[str, Any], cognito=None, audit_table=None, audit_sink=None) -> "UserAdmin":
        caller = (claims or {}).get("cognito:username") or (claims or {}).get("sub")
        if not caller:
            raise UserAdminError(401, "로그인이 필요합니다")
        if not is_admin(claims):
            raise UserAdminError(403, "사용자 관리는 관리자(admins 그룹)만 할 수 있습니다")
        cognito = cognito or _client()
        # 토큰은 그룹을 빼도 최대 1시간 남는다: 사람을 바꾸는 API라 Cognito에 지금 그룹을 다시 묻는다
        live = cognito.admin_list_groups_for_user(UserPoolId=_pool(), Username=caller).get("Groups", [])
        if ADMIN_GROUP not in {group.get("GroupName") for group in live}:
            raise UserAdminError(403, "관리자 권한이 빠졌습니다. 다시 로그인해 주세요")
        # 정지된 계정의 토큰도 만료(최대 1시간)까지는 권한 부여자를 통과한다
        if not cognito.admin_get_user(UserPoolId=_pool(), Username=caller).get("Enabled", True):
            raise UserAdminError(403, "정지된 계정입니다")
        audit = AuditLog(audit_table, audit_sink, Redactor(), user_id=(claims or {}).get("sub") or caller,
                         email=(claims or {}).get("email"), source="web", request_id=str(uuid.uuid4()),
                         session_id=None, model_id=None, question="")
        return cls(cognito, caller, claims, audit)

    # ---------------------------------------------------------------- 조회
    def list(self, query: str = "", cursor: Optional[str] = None) -> Dict[str, Any]:
        if not QUERY.match(query or ""):
            raise UserAdminError(400, "검색어에 따옴표·역슬래시는 쓸 수 없습니다 (100자까지)")
        kwargs: Dict[str, Any] = {"UserPoolId": _pool(), "Limit": PAGE_SIZE}
        if query:
            kwargs["Filter"] = f'email ^= "{query}"'
        if cursor:
            kwargs["PaginationToken"] = cursor
        page = self.cognito.list_users(**kwargs)
        members = {group: _members(self.cognito, group) for group in MANAGED_GROUPS}
        users = [_view(user, [g for g in MANAGED_GROUPS if user["Username"] in members[g]], self.caller)
                 for user in page.get("Users", [])]
        return {"users": users, "cursor": page.get("PaginationToken"), "groups": list(MANAGED_GROUPS)}

    def _get(self, username: str) -> Dict[str, Any]:
        try:
            user = self.cognito.admin_get_user(UserPoolId=_pool(), Username=username)
        except self.cognito.exceptions.UserNotFoundException:
            raise UserAdminError(404, "사용자를 찾을 수 없습니다")
        return {"username": user["Username"], "email": _attribute(user, "email"),
                "enabled": bool(user.get("Enabled", True))}

    def _view_of(self, username: str) -> Dict[str, Any]:
        user = self.cognito.admin_get_user(UserPoolId=_pool(), Username=username)
        groups = self.cognito.admin_list_groups_for_user(UserPoolId=_pool(), Username=username).get("Groups", [])
        names = {group.get("GroupName") for group in groups}
        return _view(user, [g for g in MANAGED_GROUPS if g in names], self.caller)

    # ---------------------------------------------------------------- 바꾸기 (감사 로그 → 바꾸기 → 실패면 실패도 기록)
    def _change(self, event: str, target: Dict[str, Any], apply, group: Optional[str] = None,
                extra: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.audit.admin_event(event, target, group, extra=extra)
        except Exception as error:
            print(f"감사 로그 저장 실패로 사용자 변경을 멈춤: {error}")
            raise UserAdminError(503, "감사 로그를 남기지 못해 바꾸지 않았습니다. 잠시 뒤 다시 시도해 주세요")
        try:
            apply()
        except Exception as error:
            failure = error if isinstance(error, UserAdminError) else \
                UserAdminError(502, f"Cognito가 요청을 처리하지 못했습니다: {error}")
            try:
                self.audit.admin_event(event, target, group, ok=False, error=str(failure), extra=extra)
            except Exception as audit_error:
                print(f"실패 기록도 남기지 못함: {audit_error}")
            raise failure

    def set_role(self, username: str, role: Any) -> Dict[str, Any]:
        if role not in ROLE_GROUPS:
            raise UserAdminError(400, f"권한은 {', '.join(ROLE_GROUPS)} 중 하나여야 합니다")
        target = self._get(username)
        current = {g.get("GroupName") for g in self.cognito.admin_list_groups_for_user(
            UserPoolId=_pool(), Username=username).get("Groups", [])} & set(MANAGED_GROUPS)
        wanted = set(ROLE_GROUPS[role])
        if current == wanted:
            return self._view_of(username)  # 바뀌는 것이 없다 (기록하지 않는다)
        if ADMIN_GROUP in current and ADMIN_GROUP not in wanted:
            if username == self.caller:
                raise UserAdminError(409, "자기 관리자 권한은 내릴 수 없습니다 (다른 관리자에게 부탁하세요)")
            admins = _enabled_admins(self.cognito)
            if username in admins and len(admins) <= 1:
                raise UserAdminError(409, "마지막 관리자는 내릴 수 없습니다. 다른 관리자를 먼저 지정하세요")

        def apply():
            for group in sorted(wanted - current):
                self.cognito.admin_add_user_to_group(UserPoolId=_pool(), Username=username, GroupName=group)
            for group in sorted(current - wanted):
                self.cognito.admin_remove_user_from_group(UserPoolId=_pool(), Username=username, GroupName=group)

        self._change("role_changed", target, apply, extra={"fromRole": role_of(current), "toRole": role})
        return self._view_of(username)

    def set_enabled(self, username: str, enabled: bool) -> Dict[str, Any]:
        target = self._get(username)
        if not enabled:
            if username == self.caller:
                raise UserAdminError(409, "자기 계정은 정지할 수 없습니다")
            admins = _enabled_admins(self.cognito)
            if username in admins and len(admins) <= 1:
                raise UserAdminError(409, "마지막 관리자는 정지할 수 없습니다. 다른 관리자를 먼저 지정하세요")

        def apply():
            if enabled:
                self.cognito.admin_enable_user(UserPoolId=_pool(), Username=username)
            else:
                self.cognito.admin_disable_user(UserPoolId=_pool(), Username=username)
                # 갱신 토큰을 모두 무효로 한다 (이미 받은 ID·액세스 토큰은 만료까지 남는다)
                self.cognito.admin_user_global_sign_out(UserPoolId=_pool(), Username=username)

        self._change("enabled" if enabled else "disabled", target, apply)
        return self._view_of(username)

    def invite(self, email: str) -> Dict[str, Any]:
        email = (email or "").strip()
        if not EMAIL.match(email) or len(email) > 254:
            raise UserAdminError(400, "올바른 이메일 주소가 아닙니다")
        # 이미 있는 사람은 기록 전에 걸러 낸다 (감사 로그에 '초대'가 헛되이 남지 않게)
        try:
            self.cognito.admin_get_user(UserPoolId=_pool(), Username=email)
            raise UserAdminError(409, "이미 있는 사용자입니다")
        except self.cognito.exceptions.UserNotFoundException:
            pass
        created: Dict[str, Any] = {}

        def apply():
            try:
                user = self.cognito.admin_create_user(
                    UserPoolId=_pool(), Username=email, DesiredDeliveryMediums=["EMAIL"],
                    UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}])
            except self.cognito.exceptions.UsernameExistsException:
                raise UserAdminError(409, "이미 있는 사용자입니다")
            created["username"] = user["User"]["Username"]

        self._change("invited", {"username": None, "email": email}, apply)
        return self._view_of(created["username"])


# ---------------------------------------------------------------- Lambda 진입점 (API Gateway 프록시)

def _audit_targets():
    table_name, group = os.environ.get("AUDIT_TABLE"), os.environ.get("AUDIT_LOG_GROUP")
    table = boto3.resource("dynamodb").Table(table_name) if table_name else None
    return table, CloudWatchSink(group)


def route(event: Dict[str, Any], cognito=None, audit_table=None, audit_sink=None):
    """(상태 코드, 본문). 경로는 /users 아래만 온다."""
    method = event.get("httpMethod", "")
    parts = [part for part in (event.get("path") or "").split("/") if part]
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims") or {}
    if not parts or parts[0] != "users":
        raise UserAdminError(404, "없는 경로입니다")
    admin = UserAdmin.authorize(claims, cognito, audit_table, audit_sink)
    params = event.get("queryStringParameters") or {}

    def body() -> Dict[str, Any]:
        try:
            data = json.loads(event.get("body") or "{}")
        except ValueError:
            raise UserAdminError(400, "본문이 JSON이 아닙니다")
        return data if isinstance(data, dict) else {}

    if len(parts) == 1 and method == "GET":
        return 200, admin.list(params.get("q") or "", params.get("cursor"))
    if len(parts) == 1 and method == "POST":
        return 200, admin.invite(body().get("email"))
    if len(parts) == 3 and parts[2] == "role" and method == "PUT":
        return 200, admin.set_role(parts[1], body().get("role"))
    if len(parts) == 3 and parts[2] in ("disable", "enable") and method == "POST":
        return 200, admin.set_enabled(parts[1], enabled=parts[2] == "enable")
    raise UserAdminError(404, "없는 경로입니다")


def lambda_handler(event, context):
    from common.utils import cors_response  # 계층(layers/common)에 있다
    origin = (event.get("headers") or {}).get("origin", "")
    if event.get("httpMethod") == "OPTIONS":
        return cors_response(200, "", origin)
    try:
        table, sink = _audit_targets()
        status, body = route(event, audit_table=table, audit_sink=sink)
        return cors_response(status, body, origin)
    except UserAdminError as error:
        return cors_response(error.status, {"error": str(error)}, origin)
    except Exception as error:
        print(f"사용자 관리 오류: {error}")
        return cors_response(500, {"error": "사용자 관리 중 오류가 났습니다"}, origin)
