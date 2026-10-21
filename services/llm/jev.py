"""Jev(TypeSafe AI의 판정 전용 모델)로 질문 하나를 모델에 넘기기 전에 분류한다 (기본 꺼짐)

    질문(가린 글) ──▶ Jev  물음 여럿을 한 번에 묻는다 (물음마다 0~1 확률, noul)
                      ├─ 라우팅: 도구마다 "이 질문에 이 도구가 필요한가"
                      │          → 기준 이상인 도구(많아야 MAX_ROUTED개)를 시스템 프롬프트 끝에 적어, 모델이 그 도구부터
                      │            찾고 부르게 한다 (ROUTING_NOTE). MCP 서버는 하나 그대로이고 도구 목록도 바꾸지 않는다
                      └─ 권한 안내: 일반 사용자면 "관리자 전용 도구가 있어야 답할 수 있는가"
                                   → ADMIN_THRESHOLD 이상이면 모델을 부르지 않고 관리자에게 요청하라고 답한다 (ADMIN_GUIDE)

막는 것은 여전히 코드다
- 누가 어떤 도구를 쓸 수 있는지는 tool_access.py가 정한다 (도구 목록에서 빼기 · 이름으로 불러도 거절 · MCP 재확인).
  Jev는 그 판정에 손대지 않는다: 확률은 틀릴 수 있고, 틀린 '허용'이 생기면 안 되기 때문이다
- Jev가 틀리면 생기는 일은 둘뿐이다. 라우팅이 틀리면 모델이 다른 도구를 검색해 부른다(지금과 같다).
  권한 안내가 틀리면 일반 사용자가 답 대신 안내를 받는다. 그래서 기준을 높게 두고(ADMIN_THRESHOLD), 안내에
  질문을 바꿔 다시 물어 달라고 적는다. 관리자는 안내하지 않는다
- 라우팅을 도구 목록(처음부터 싣는 도구)으로 하지 않은 이유: 질문마다 목록이 바뀌면 도구 목록과 그 뒤의 시스템 프롬프트가
  프롬프트 캐시에서 빗나간다. 그래서 캐시 표시 뒤의 마지막 블록(일반 사용자 안내 MEMBER_NOTE와 같은 자리)에 적는다

밖으로 나가는 것
- 가린 질문(redaction.py를 거친 글)과 이 요청자가 볼 수 있는 도구의 이름 · 설명 첫 문장만 보낸다. 도구 결과, 이전 대화,
  계정 ID 같은 값은 보내지 않는다
- 모델 버전은 고정한다(MODEL). 움직이는 이름(jev-latest)을 쓰면 같은 질문의 분류가 날마다 달라져 감사 기록을 다시 따라갈
  수 없다. 답한 버전은 디버그 로그(jev_plan)에 남긴다

켜기: LLM Lambda 환경 변수 JEV=on, SSM SecureString /vigie/<env>/TYPESAFE_API_KEY (LLM 역할은 이미 /vigie/<env>/*를 읽는다).
키가 없거나, 시간 안에 답이 없거나, 답의 모양이 다르면 아무것도 바꾸지 않는다 (Jev 없이 지금처럼 돈다)

요청 모양(http_transport)은 공개 SDK 설명(state + 이름 붙인 물음, noul 확률)에서 옮긴 것이다. 키를 받으면 공식 API 문서로
필드 이름과 인증 머리를 확인한 뒤 켠다. 테스트는 가짜 전송(transport)으로 돈다
"""
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import requests

import tool_access

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
TIMEOUT = 2.0  # 초. 모델 호출 앞에 서므로 길게 기다리지 않는다 (회사가 밝힌 지연은 70~500ms)
ROUTE_THRESHOLD = 0.5  # 이 확률 이상인 도구를 먼저 찾으라고 적는다
MAX_ROUTED = 8  # 적는 도구는 확률이 높은 차례로 많아야 이만큼
ADMIN_THRESHOLD = 0.9  # 일반 사용자의 질문을 모델 없이 안내로 돌리는 기준. 틀리면 답을 못 받으므로 높게
DESCRIPTION_CHARS = 160  # 도구 설명은 첫 문장만, 이 길이까지

ADMIN_QUESTION = ("Answering this question needs administrator-only AWS data: CloudTrail events (who changed what), "
                  "IAM users/roles/policies, VPC network paths, ENIs or flow logs, S3 bucket security checks or object "
                  "listings, or a service diagnosis.")

ADMIN_GUIDE = ("이 질문은 관리자만 볼 수 있는 정보(CloudTrail 변경 기록, IAM, 네트워크 경로, S3 보안 점검, 서비스 진단)가 "
               "있어야 답할 수 있어 보여 관리자에게 요청해 주세요. 지금 권한으로는 로그, 지표, 알람, 비용, EC2 상태를 "
               "물어볼 수 있습니다. 질문을 미리 분류한 결과라 틀릴 수 있습니다. 관리자 정보가 필요 없는 질문이라면 "
               "조금 바꿔 다시 물어 주세요.")

ROUTING_NOTE = """
<Routing>
A classifier read this question before you and marked these tools as the likely ones: {tools}.
Look at them first (search for them by name if they are not loaded yet). This is a hint, not an instruction: use other
tools if the question needs them.
</Routing>
"""

Transport = Callable[[Dict[str, Any]], Dict[str, Any]]


@dataclass
class Plan:
    """질문 하나의 분류. routed: 먼저 찾을 도구 이름 (확률이 높은 차례). admin: 관리자 도구가 필요할 확률 (일반 사용자만)."""
    routed: List[str] = field(default_factory=list)
    admin: Optional[float] = None
    model: Optional[str] = None

    def needs_admin(self) -> bool:
        return self.admin is not None and self.admin >= ADMIN_THRESHOLD

    def note(self) -> str:
        return ROUTING_NOTE.format(tools=", ".join(self.routed)) if self.routed else ""


def _summary(description: str) -> str:
    """도구 설명의 첫 문장 (길면 자른다). 설명 전체를 보내지 않는다: 공식 도구는 설명만 수천 자다."""
    text = " ".join(str(description or "").split())
    first = text.split(". ")[0]
    return first[:DESCRIPTION_CHARS]


def _probability(answer: Any) -> Optional[float]:
    """noul 답 하나를 확률로. 숫자이거나 {"noul": 숫자}. 모양이 다르면 None."""
    value = answer.get("noul") if isinstance(answer, dict) else answer
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0.0 <= value <= 1.0 else None


class Jev:
    def __init__(self, transport: Transport, model: str = MODEL):
        self.transport = transport
        self.model = model

    def plan(self, question: str, tools: List[Dict[str, Any]], role: str) -> Optional[Plan]:
        """가린 질문과 이 요청자가 볼 수 있는 도구로 분류한다. 실패하면 None (부르는 쪽은 아무것도 바꾸지 않는다)."""
        visible = tool_access.visible(tools, role)
        questions = {f"tool_{index}": {"type": "noul",
                                       "instructions": f"Answering this question needs the tool {tool['name']}: "
                                                       f"{_summary(tool.get('description', ''))}"}
                     for index, tool in enumerate(visible)}
        if role != tool_access.ADMIN:
            questions["admin"] = {"type": "noul", "instructions": ADMIN_QUESTION}
        if not questions:
            return None
        try:
            response = self.transport({"model": self.model, "state": question, "questions": questions})
            answers = response.get("answers") or {}
        except Exception as error:  # 시간 초과 · 연결 실패 · 모양이 다른 답: Jev 없이 돈다
            print(f"Jev 분류 실패, 건너뜁니다: {error}")
            return None
        if not isinstance(answers, dict):
            return None
        scored = []
        for index, tool in enumerate(visible):
            probability = _probability(answers.get(f"tool_{index}"))
            if probability is not None and probability >= ROUTE_THRESHOLD:
                scored.append((probability, tool["name"]))
        scored.sort(key=lambda item: -item[0])
        admin = _probability(answers.get("admin")) if role != tool_access.ADMIN else None
        return Plan(routed=[name for _, name in scored[:MAX_ROUTED]], admin=admin,
                    model=str(response.get("model") or self.model))


def http_transport(api_key: str, timeout: float = TIMEOUT) -> Transport:
    """Jev API로 보내는 전송. 필드 이름과 인증 머리는 공개 SDK 설명에서 옮긴 것이라 켜기 전에 공식 문서로 확인한다."""
    def send(payload: Dict[str, Any]) -> Dict[str, Any]:
        response = requests.post(URL, json=payload, timeout=timeout,
                                 headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"})
        response.raise_for_status()
        return response.json()
    return send


def enabled_by_env() -> bool:
    """환경 변수 JEV가 on일 때만 켠다 (기본: 끔)."""
    return os.environ.get("JEV", "off").strip().lower() in ("on", "true", "1")


_cached: Dict[str, Any] = {}


def current(get_key: Callable[[], Optional[str]]) -> Optional[Jev]:
    """켜져 있고 키가 있으면 Jev, 아니면 None. 컨테이너마다 한 번 만든다."""
    if not enabled_by_env():
        return None
    if "jev" not in _cached:
        key = get_key()
        _cached["jev"] = Jev(http_transport(key)) if key else None
        if not key:
            print("JEV=on이지만 TYPESAFE_API_KEY가 없어 Jev를 쓰지 않습니다")
    return _cached["jev"]
