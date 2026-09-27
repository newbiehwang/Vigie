"""Chat History: 토큰 sub 기반 사용자 식별과 세션 소유자 확인 (moto DynamoDB)"""
import json

import boto3
import pytest

from conftest import load_service_module

ORIGIN = "https://test.abc.amplifyapp.com"


@pytest.fixture
def chs(aws):
    boto3.client("dynamodb").create_table(
        TableName="vigie-chat-history-test",
        AttributeDefinitions=[{"AttributeName": "sessionId", "AttributeType": "S"},
                              {"AttributeName": "userId", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "sessionId", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[{"IndexName": "UserIdIndex",
                                 "KeySchema": [{"AttributeName": "userId", "KeyType": "HASH"}],
                                 "Projection": {"ProjectionType": "ALL"}}],
        BillingMode="PAY_PER_REQUEST",
    )
    return load_service_module("services/chat-history", "chat_history_service")


def event(sub=None, query=None):
    ctx = {"authorizer": {"claims": {"sub": sub}}} if sub else {}
    return {"requestContext": ctx, "queryStringParameters": query}


def call(chs, path, method, sub, body=None, query=None):
    res = chs.handle_chat_history_request(path, method, body or {}, event(sub, query), ORIGIN)
    return res["statusCode"], json.loads(res["body"]) if res["body"] else None


def create(chs, sub, title="대화"):
    status, body = call(chs, "/sessions", "POST", sub, {"title": title})
    assert status == 200
    return body["sessionId"]


def test_create_uses_token_sub_not_client_user_id(chs):
    status, body = call(chs, "/sessions", "POST", "alice", {"userId": "bob", "title": "t"})
    assert status == 200 and body["userId"] == "alice"


def test_list_returns_only_callers_sessions_ignoring_query_user_id(chs):
    create(chs, "alice", "a1")
    create(chs, "bob", "b1")
    status, body = call(chs, "/sessions", "GET", "alice", query={"userId": "bob"})
    assert status == 200
    assert [s["title"] for s in body["sessions"]] == ["a1"]


def test_owner_can_add_and_read_messages(chs):
    sid = create(chs, "alice")
    status, _ = call(chs, f"/sessions/{sid}/messages", "POST", "alice", {"sender": "user", "text": "안녕"})
    assert status == 200
    status, body = call(chs, f"/sessions/{sid}/messages", "GET", "alice")
    assert status == 200 and [m["text"] for m in body["messages"]] == ["안녕"]


@pytest.mark.parametrize("path_suffix,method", [
    ("", "GET"), ("", "PUT"), ("", "DELETE"), ("/messages", "GET"), ("/messages", "POST"),
])
def test_other_user_gets_404_and_cannot_modify(chs, path_suffix, method):
    sid = create(chs, "alice")
    status, _ = call(chs, f"/sessions/{sid}{path_suffix}", method, "mallory",
                     {"title": "hacked", "sender": "user", "text": "x"})
    assert status == 404
    # 원본 세션은 그대로 남아 있어야 한다
    status, body = call(chs, f"/sessions/{sid}", "GET", "alice")
    assert status == 200 and body["title"] == "대화"


def test_delete_all_only_deletes_callers_sessions(chs):
    create(chs, "alice")
    bob_sid = create(chs, "bob")
    status, body = call(chs, "/sessions", "DELETE", "alice", query={"userId": "bob"})
    assert status == 200 and body["deletedCount"] == 1
    assert call(chs, f"/sessions/{bob_sid}", "GET", "bob")[0] == 200


def test_missing_claims_is_unauthorized(chs):
    assert call(chs, "/sessions", "GET", None)[0] == 401
