"""거버넌스 지표 (CloudWatch EMF: 로그 한 줄로 사용자 지표를 발행한다)

    print({"_aws": {...지표 정의...}, "Environment": "dev", "ToolCalls": 3, ...})
      └─ CloudWatch Logs가 이 줄을 읽어 Vigie/Governance 네임스페이스의 지표로 만든다 (PutMetricData 호출 없이)

Lambda 로그 형식이 JSON이어도 된다: Lambda는 logging 라이브러리로 쓴 로그만 JSON으로 감싸고,
print로 쓴 JSON 줄은 다시 감싸지 않는다 (EMF가 그대로 남는다).

지표 (차원: Environment 하나. 차원 값이 늘면 지표 수와 비용이 늘어서 도구 이름 등은 넣지 않는다)
- ToolCalls, ToolErrors: 도구 호출·실패 수 (질문 하나 단위로 모아 보낸다)
- InjectionSuspected: 지시문처럼 보이는 문구가 든 도구 결과 수 (injection.py) → 알람
- RedactedValues: Claude로 보내기 전에 가린 값의 수 (redaction.py)
- ApprovalRequested, ApprovalApproved, ApprovalDenied: 변경 작업 승인 요청·승인·거절 (approvals.py) → 거절 반복 알람
- ActionFailed: 승인했지만 실행하지 못한 변경 작업
"""
import json
import os
import time
from typing import Dict

NAMESPACE = "Vigie/Governance"
METRIC_NAMES = ["ToolCalls", "ToolErrors", "InjectionSuspected", "RedactedValues",
                "ApprovalRequested", "ApprovalApproved", "ApprovalDenied", "ActionFailed"]


def emf_record(values: Dict[str, int], environment: str) -> Dict:
    """EMF 형식의 로그 한 줄 (값이 있는 지표만)."""
    present = {name: int(value) for name, value in values.items() if name in METRIC_NAMES and value}
    return {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": NAMESPACE,
                "Dimensions": [["Environment"]],
                "Metrics": [{"Name": name, "Unit": "Count"} for name in present],
            }],
        },
        "Environment": environment,
        **present,
    }


def emit(values: Dict[str, int]) -> None:
    """지표를 발행한다. 보낼 값이 하나도 없으면 아무것도 쓰지 않는다. 실패해도 요청은 계속한다."""
    try:
        record = emf_record(values, os.environ.get("ENV", "dev"))
        if record["_aws"]["CloudWatchMetrics"][0]["Metrics"]:
            print(json.dumps(record, ensure_ascii=False))
    except Exception as error:  # 지표는 보조 정보다
        print(f"지표 발행 실패 (계속 진행): {error}")
