"""도구 검색(services/llm/tool_search.py) 전후 측정.

    uv run --no-project --python 3.12 --with-requirements requirements-dev.txt python scripts/measure_tool_search.py [모드]

모드
- size  (기본, 무료): 실제 MCP 서버(mcp/app.py)의 tools/list를 로컬에서 받아, 모델에 처음부터 싣는 도구 정의의
        크기를 도구 검색을 끈 경우와 켠 경우로 비교한다. AWS·Anthropic에 요청하지 않는다 (moto, 가짜 자격 증명).
- count (무료, API 키 필요): Anthropic 토큰 계산 API(count_tokens)로 두 경우의 입력 토큰을 정확히 센다.
        토큰 계산 API는 서버 도구(검색 도구)를 받지 않아, 켠 경우는 '처음부터 싣는 도구'만 센다 (검색 도구 정의 제외).
- eval  (유료, API 키 필요): 질문 EVAL_SET을 두 경우로 실제 모델에 보내, 첫 도구 선택이 맞는지와 입력 토큰을 잰다.
        도구는 실행하지 않는다: 모델이 처음 부르려는 도구 이름만 보고 멈춘다 (AWS에 요청하지 않는다).
        질문 수 × 2번 모델을 부르므로 비용이 든다. 실행 전에 요청 수를 보여 주고 --yes가 없으면 멈춘다.

API 키는 환경 변수 ANTHROPIC_API_KEY에서만 읽는다 (명령 인자로 받지 않는다).
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 질문과 '처음 부르면 맞는' 도구. 로그·지표 질문은 처음부터 싣는 도구로, 나머지는 검색해야 찾는 도구로 답한다
EVAL_SET = [
    ("최근 1시간 동안 vigie-llm-dev Lambda 오류 로그 보여줘", {"describe_log_groups", "execute_log_insights_query"}),
    ("지금 울리고 있는 알람 있어?", {"get_active_alarms"}),
    ("API Gateway 5XX 알람이 언제 울렸었는지 이력 보여줘", {"get_alarm_history", "get_active_alarms"}),
    ("vigie-llm-dev 함수의 지난 24시간 실행 시간(Duration) 추이 알려줘", {"get_metric_data", "get_metric_metadata"}),
    ("CloudWatch 대시보드 목록 보여줘", {"listCloudwatchDashboards"}),
    ("Lambda 동시성 제한에 대한 AWS 문서 찾아줘", {"search_documentation"}),
    ("이번 달 서비스별 비용 알려줘", {"cost-explorer"}),
    ("어제 누가 보안 그룹 규칙을 바꿨어?", {"lookup_events"}),
    ("서울 리전 t3.medium 온디맨드 한 달 요금 얼마야?",
     {"get_pricing", "get_pricing_service_codes", "get_pricing_service_attributes", "get_pricing_attribute_values"}),
    ("IAM 사용자 목록 보여줘", {"list_users"}),
    ("vigie-llm 역할이 dynamodb:PutItem을 할 수 있어?", {"simulate_principal_policy", "list_roles"}),
    ("10.0.1.25 IP가 어떤 네트워크 인터페이스에 붙어 있어?", {"find_ip_address", "get_path_trace_methodology"}),
    ("이 계정에 VPC가 몇 개야?", {"list_vpcs"}),
    ("퍼블릭으로 열린 S3 버킷 있어?", {"checkS3BucketSecurity", "listS3Buckets"}),
    ("가장 큰 S3 버킷이 뭐야?", {"getS3BucketSize", "listS3Buckets"}),
    ("지난 24시간 동안 CPU를 가장 많이 쓴 EC2 인스턴스는?", {"getEc2CpuRanking"}),
    ("안 쓰는 EBS 볼륨이나 연결 안 된 탄력적 IP 있어?", {"findEc2Waste"}),
    ("EC2 인스턴스 상태 검사에 실패한 것 있어?", {"getEc2StatusChecks", "listEc2Instances"}),
    ("i-0abc1234567890def 인스턴스 중지해줘", {"setEc2InstanceState", "listEc2Instances"}),
    ("vigie-llm-dev 로그 그룹 보존 기간을 14일로 바꿔줘", {"setLogRetention", "describe_log_groups"}),
    ("Vigie 서버리스 아키텍처 다이어그램 그려줘",
     {"getDiagramCodeExamples", "listAvailableDiagramIcons", "generateArchitectureDiagram"}),
]

API = "https://api.anthropic.com/v1"


# ---------------------------------------------------------------- 도구 목록 (로컬)

def load_tools():
    """실제 MCP 서버의 tools/list (위험도 표시 포함). moto 위에서 돌려 AWS에 요청하지 않는다."""
    os.environ.update({"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                       "AWS_SESSION_TOKEN": "testing", "ENV": "measure",
                       "MCP_SESSION_TABLE": "vigie-mcp-sessions-measure"})
    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
    os.environ.setdefault("AWS_REGION", os.environ["AWS_DEFAULT_REGION"])
    for path in (ROOT / "layers", ROOT / "mcp", ROOT / "services" / "llm"):
        sys.path.insert(0, str(path))

    import boto3
    from moto import mock_aws

    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName=os.environ["MCP_SESSION_TABLE"], BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}])
        import app

        def rpc(method, session=None):
            headers = {"content-type": "application/json", **({"mcp-session-id": session} if session else {})}
            return app.lambda_handler({"httpMethod": "POST", "headers": headers, "body": json.dumps(
                {"jsonrpc": "2.0", "id": "1", "method": method, "params": {}})}, None)

        session = rpc("initialize")["headers"]["MCP-Session-Id"]
        return json.loads(rpc("tools/list", session)["body"])["result"]["tools"]


def anthropic_tools(tools, enabled, model="claude-sonnet-5"):
    """LLM Lambda가 보내는 것과 같은 도구 목록 (mcp_anthropic_client._convert_tools_format)."""
    from mcp_anthropic_client import AnthropicMCPClient
    client = AnthropicMCPClient(mcp_url="https://example.invalid", api_key=None, model_id=model)
    client.tools = tools
    client.tool_search = enabled
    client.role = "admin"  # 모든 도구를 싣는 경우를 잰다 (일반 사용자는 관리자 전용 도구가 빠져 더 작다)
    return client._convert_tools_format()


def in_context(tools):
    """모델에 처음부터 보이는 도구 정의 (지연한 도구는 검색해야 들어온다)."""
    return [tool for tool in tools if not tool.get("defer_loading")]


def size(tools):
    text = json.dumps(tools, ensure_ascii=False)
    return len(text), len(text.encode("utf-8"))


def size_report(tools):
    off, on = anthropic_tools(tools, False), anthropic_tools(tools, True)
    rows = [("끔 (모든 도구)", in_context(off)), ("켬 (검색 도구 + 자주 쓰는 도구)", in_context(on))]
    print(f"MCP 도구 {len(tools)}개\n")
    print(f"{'':32} {'처음부터 싣는 도구':>16} {'글자 수':>10} {'UTF-8 바이트':>12}")
    for label, loaded in rows:
        chars, octets = size(loaded)
        print(f"{label:32} {len(loaded):>16} {chars:>10,} {octets:>12,}")
    before, after = size(rows[0][1])[0], size(rows[1][1])[0]
    print(f"\n처음부터 싣는 정의가 {100 * (1 - after / before):.1f}% 줄었다 (글자 수 기준)")
    print("\n정의가 큰 도구 10개 (켜면 검색해야 들어온다):")
    for tool in sorted(off, key=lambda t: -size([t])[0])[:10]:
        print(f"  {tool['name']:40} {size([tool])[0]:>8,}자")
    return {"tools": len(tools), "before_chars": before, "after_chars": after}


# ---------------------------------------------------------------- Anthropic API (키 필요)

def api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("환경 변수 ANTHROPIC_API_KEY가 필요합니다")
    return key


def post(path, payload):
    import requests
    response = requests.post(f"{API}/{path}", json=payload, timeout=120, headers={
        "x-api-key": api_key(), "anthropic-version": "2023-06-01", "content-type": "application/json"})
    if response.status_code != 200:
        sys.exit(f"Anthropic API 오류 {response.status_code}: {response.text[:500]}")
    return response.json()


def system_prompt(enabled):
    from system_prompt import build_system_prompt
    import tool_search
    text = build_system_prompt(datetime.now(timezone.utc))
    return text + tool_search.SYSTEM_HINT if enabled else text


def count_report(tools, model):
    """count_tokens는 서버 도구를 받지 않는다: 켠 경우는 처음부터 싣는 도구만 센다 (검색 도구 정의만큼 적게 나온다)."""
    results = {}
    for label, enabled in (("끔", False), ("켬", True)):
        loaded = [t for t in in_context(anthropic_tools(tools, enabled, model)) if "type" not in t]
        loaded = [{k: v for k, v in t.items() if k != "cache_control"} for t in loaded]
        body = post("messages/count_tokens", {"model": model, "system": system_prompt(enabled), "tools": loaded,
                                               "messages": [{"role": "user", "content": "안녕"}]})
        results[label] = body["input_tokens"]
        print(f"도구 검색 {label}: 입력 {body['input_tokens']:,} 토큰 (시스템 프롬프트 + 처음부터 싣는 도구 {len(loaded)}개)")
    print(f"\n요청 한 번의 입력이 {100 * (1 - results['켬'] / results['끔']):.1f}% 줄었다")
    return results


def first_tool(tools, enabled, model, question):
    """질문 하나: 모델이 처음 부르려는 도구(실행하지 않는다)와 입력 토큰."""
    payload = {"model": model, "max_tokens": 4096, "system": system_prompt(enabled),
               "tools": anthropic_tools(tools, enabled, model), "messages": [{"role": "user", "content": question}]}
    usage = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    searches = []
    for _ in range(3):  # 서버 도구가 길어지면(pause_turn) 받은 그대로 다시 보내 이어 간다
        body = post("messages", payload)
        for key, name in (("input", "input_tokens"), ("cache_write", "cache_creation_input_tokens"),
                          ("cache_read", "cache_read_input_tokens"), ("output", "output_tokens")):
            usage[key] += body.get("usage", {}).get(name) or 0
        content = body.get("content", [])
        searches += [b.get("input") for b in content if b.get("type") == "server_tool_use"]
        called = next((b["name"] for b in content if b.get("type") == "tool_use"), None)
        if called or body.get("stop_reason") != "pause_turn":
            return called, usage, searches
        payload["messages"] = payload["messages"] + [{"role": "assistant", "content": content}]
    return None, usage, searches


def eval_report(tools, model, yes, input_price, output_price):
    requests_needed = len(EVAL_SET) * 2
    print(f"질문 {len(EVAL_SET)}개 × (끔, 켬) = 모델 요청 약 {requests_needed}번 ({model}). 비용이 듭니다.")
    if not yes:
        sys.exit("실행하려면 --yes를 붙이세요")
    summary = {}
    for label, enabled in (("끔", False), ("켬", True)):
        correct, totals, rows = 0, {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}, []
        for question, expected in EVAL_SET:
            called, usage, searches = first_tool(tools, enabled, model, question)
            ok = called in expected
            correct += ok
            for key in totals:
                totals[key] += usage[key]
            rows.append((ok, question, called, searches))
            time.sleep(0.5)
        print(f"\n[도구 검색 {label}] 첫 도구가 맞은 질문 {correct}/{len(EVAL_SET)}")
        for ok, question, called, searches in rows:
            print(f"  {'O' if ok else 'X'} {question[:34]:36} → {called}  {searches if searches else ''}")
        processed = totals["input"] + totals["cache_write"] + totals["cache_read"]
        print(f"  입력 {processed:,} 토큰 (캐시 없이 {totals['input']:,} · 캐시 쓰기 {totals['cache_write']:,}"
              f" · 캐시 읽기 {totals['cache_read']:,}), 출력 {totals['output']:,}")
        if input_price is not None:
            # 캐시 쓰기(5분)는 입력 단가의 1.25배, 캐시 읽기는 0.1배 (Anthropic 가격표)
            cost = (totals["input"] + 1.25 * totals["cache_write"] + 0.1 * totals["cache_read"]) * input_price
            cost += totals["output"] * (output_price or 0)
            print(f"  비용 약 ${cost / 1_000_000:.4f}")
        summary[label] = {"correct": correct, **totals}
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", nargs="?", default="size", choices=["size", "count", "eval"])
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--yes", action="store_true", help="eval: 비용이 드는 모델 요청을 실제로 보낸다")
    parser.add_argument("--input-price", type=float, help="eval: 입력 단가 (USD / 100만 토큰)")
    parser.add_argument("--output-price", type=float, help="eval: 출력 단가 (USD / 100만 토큰)")
    args = parser.parse_args()

    tools = load_tools()
    if args.mode == "size":
        size_report(tools)
    elif args.mode == "count":
        count_report(tools, args.model)
    else:
        eval_report(tools, args.model, args.yes, args.input_price, args.output_price)


if __name__ == "__main__":
    main()
