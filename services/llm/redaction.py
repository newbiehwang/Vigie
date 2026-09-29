"""민감정보 가리기 (도구 결과 등이 계정 밖의 Claude API로 나가기 전에)

    AWS 계정 ── 도구 결과 ──▶ [가리기] ──▶ Claude API (계정 밖)
                                  │
    도구 호출 ◀── [되돌리기] ◀── Claude가 고른 도구 입력 (가명이 들어 있을 수 있다)

가리는 값은 두 종류로 나눠 다르게 다룬다.
1. 비밀 값 (비밀 액세스 키, 세션 토큰, API 토큰, JWT, 개인 키, 접속 주소의 비밀번호)
   - [REDACTED:종류]로 바꾸고 되돌리지 않는다. 모델이 알 필요가 없고, 알면 안 되는 값이다.
2. 식별자 (12자리 AWS 계정 ID, 액세스 키 ID, 이메일)
   - 요청마다 같은 값은 같은 가명으로 바꾼다 (예: 123456789012 → ********9012,
     AKIA…2QXA(20자) → AKIA********2QXA, alice@example.com → a***@example.com).
   - 모델이 그 가명을 도구 입력에 넣으면 도구를 부르기 직전에 원래 값으로 되돌린다.
     ARN 안의 계정 ID를 가려도 그 ARN으로 다시 조회하는 흐름이 깨지지 않는다.
   - 액세스 키 ID(AKIA·ASIA…)는 키를 가리키는 이름이고, 비밀은 따로 있는 비밀 액세스 키다.
     "키 AKIA…가 유출됐대요"라는 질문에 진단 도구(diagnoseService의 credential)가 그 키를 조회해야 하므로
     되돌릴 수 없게 지우지 않고 가명으로 다룬다. 앞 네 글자(장기 키 AKIA, 임시 키 ASIA)는 남긴다.
   - 모델이 가명만 보므로 답변과 화면에도 가명만 남는다.

오탐을 줄이는 원칙
- 12자리 숫자만으로는 계정 ID로 보지 않는다 (바이트 수, 요청 ID(UUID)의 마지막 12자리 등과 모양이 같다).
  이 Lambda가 속한 계정 ID(AWS_ACCOUNT_ID)는 어디에 있든 가리고, 다른 계정 ID는 ARN · ECR 주소 ·
  "AccountId": 같은 이름 뒤에 올 때만 가린다.
- 40자 비밀 키는 모양만으로는 git 커밋 해시·base64 값과 구분되지 않아서, secret_access_key 같은 이름 뒤에 올 때만 가린다.

Redactor는 요청 하나에 하나씩 만든다 (가명 표가 요청마다 따로여야 다른 사람의 값이 섞이지 않는다).
"""
import hashlib
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Tuple

# 모든 패턴은 re.A(ASCII)로 만든다. 기본(유니코드)으로 두면 한국어 글자도 단어 글자라서
# "키 AKIA…가"처럼 값 바로 뒤에 조사가 붙을 때 \b가 잡히지 않아 가려지지 않는다.

# ---------------------------------------------------------------- 비밀 값 (되돌리지 않는다)
# (종류, 패턴, 값이 들어 있는 그룹 번호). 그룹 번호가 0이면 찾은 글자 전체를 바꾸고,
# 1 이상이면 그 그룹만 바꾼다 (앞의 이름·구분자는 남겨 무엇이 가려졌는지 알 수 있게 한다)
SECRET_PATTERNS: List[Tuple[str, re.Pattern, int]] = [
    # 개인 키는 여러 줄이라 가장 먼저 통째로 바꾼다
    ("private_key", re.compile(
        r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----.*?-----END (?:[A-Z]+ )*PRIVATE KEY-----", re.S | re.A), 0),
    # 로그인 토큰(Cognito ID·액세스 토큰 등). header.payload.signature
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", re.A), 0),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}", re.A), 0),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}|\bxapp-[A-Za-z0-9-]{10,}", re.A), 0),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/(?:services|workflows)/[A-Za-z0-9/_-]+", re.A), 0),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}", re.A), 0),
    # 비밀 키·세션 토큰은 이름 뒤에 올 때만 (모양만으로는 해시·base64 값과 구분되지 않는다)
    ("aws_secret_key", re.compile(
        r"(?i)((?:aws_?)?secret_?access_?key[\\\"'\s:=]{1,8})([A-Za-z0-9/+]{40})(?![A-Za-z0-9/+=])", re.A), 2),
    ("aws_session_token", re.compile(
        r"(?i)((?:aws_?)?session_?token[\\\"'\s:=]{1,8})([A-Za-z0-9/+=]{100,})", re.A), 2),
    # 접속 주소에 들어 있는 비밀번호 (postgres://user:비밀번호@host)
    ("url_password", re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@\"']+:)([^@\s/\"']+)(@)", re.I | re.A), 2),
]

# ---------------------------------------------------------------- 식별자 (가명으로 바꾸고 도구 호출 때 되돌린다)
ACCOUNT_ID = "account_id"
ACCESS_KEY_ID = "aws_access_key_id"
EMAIL = "email"

# 장기 키(AKIA)와 임시 키(ASIA) 등의 액세스 키 ID. 역할·사용자의 고유 ID(AROA·AIDA)는 가리지 않는다
ACCESS_KEY_ID_PATTERN = re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b", re.A)

# 다른 계정 ID는 이 자리에 있을 때만 계정 ID로 본다. 모두 두 번째 그룹이 계정 ID다
ACCOUNT_ID_PATTERNS: List[re.Pattern] = [
    # ARN: arn:aws:iam::123456789012:role/x, arn:aws:logs:ap-northeast-2:123456789012:log-group:...
    re.compile(r"(\barn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:)(\d{12})(?=:)", re.A),
    # ECR 주소: 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com
    re.compile(r"(?<![\w.])()(\d{12})(?=\.dkr\.ecr\.)", re.A),
    # "AccountId": "123456789012", account_id=123456789012, Account 123456789012, OwnerId ...
    re.compile(r"(?i)(\b(?:aws_?)?(?:account|owner)(?:[_ -]?id)?[\\\"'\s:=]{1,8})(\d{12})(?!\d)", re.A),
]
EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b", re.A)


class Redactor:
    """요청 하나의 가리기·되돌리기. 가명 표와 가린 값의 개수를 요청이 끝날 때까지 들고 있다."""

    def __init__(self, known_account_ids: Iterable[str] = ()):
        # 이 Lambda가 속한 계정 등 이미 아는 계정 ID는 문맥 없이도 가린다
        self._known = [account for account in known_account_ids if re.fullmatch(r"\d{12}", account or "")]
        self._known_pattern = (re.compile(r"(?<!\d)(" + "|".join(map(re.escape, self._known)) + r")(?!\d)", re.A)
                               if self._known else None)
        self._alias: Dict[Tuple[str, str], str] = {}  # (종류, 원래 값) → 가명
        self._original: Dict[str, str] = {}  # 가명 → 원래 값
        self._seen = set()  # 가린 값 (종류, 해시). 같은 값을 여러 곳에서 가려도 한 번만 센다
        self.counts: Counter = Counter()  # 종류별로 가린 서로 다른 값의 수 (감사 로그·추론 데이터에 남긴다)

    # ---------------------------------------------------------------- 가리기
    def text(self, value: str) -> str:
        """글자 하나를 가린다. 비밀 값을 먼저 가리고(그 안의 계정 ID·이메일이 가명 표에 남지 않게) 식별자를 가린다."""
        if not value:
            return value
        for kind, pattern, group in SECRET_PATTERNS:
            value = pattern.sub(lambda match, k=kind, g=group: self._secret(match, k, g), value)
        value = ACCESS_KEY_ID_PATTERN.sub(lambda match: self._pseudonym(ACCESS_KEY_ID, match.group(0)), value)
        for pattern in ACCOUNT_ID_PATTERNS:
            value = pattern.sub(lambda match: match.group(1) + self._pseudonym(ACCOUNT_ID, match.group(2)), value)
        if self._known_pattern is not None:
            value = self._known_pattern.sub(lambda match: self._pseudonym(ACCOUNT_ID, match.group(1)), value)
        return EMAIL_PATTERN.sub(lambda match: self._pseudonym(EMAIL, match.group(0)), value)

    def redact(self, value: Any) -> Any:
        """글자·목록·사전을 따라 들어가며 글자를 모두 가린 사본을 돌려준다 (숫자 등은 그대로)."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        return value

    def secrets_only(self, value: Any) -> Any:
        """비밀 값만 가린 사본 (계정 ID·액세스 키 ID·이메일은 그대로). 계정 안에 남는 감사 로그용 (audit.py).
        누가 어느 계정의 무엇을 조회했는지 추적하려면 식별자는 남아 있어야 하고, 비밀 값은 감사 로그에도 남기지 않는다.
        밖으로 나간 값이 아니므로 counts에는 세지 않는다."""
        if isinstance(value, str):
            for kind, pattern, group in SECRET_PATTERNS:
                value = pattern.sub(lambda match, k=kind, g=group: self._secret(match, k, g, count=False), value)
            return value
        if isinstance(value, list):
            return [self.secrets_only(item) for item in value]
        if isinstance(value, dict):
            return {key: self.secrets_only(item) for key, item in value.items()}
        return value

    # ---------------------------------------------------------------- 되돌리기 (도구 호출 직전에만)
    def restore(self, value: Any) -> Any:
        """모델이 도구 입력에 넣은 가명을 원래 값으로 되돌린 사본. 비밀 값([REDACTED:…])은 되돌리지 않는다."""
        if isinstance(value, str):
            # 긴 가명부터 바꾼다 (********9012#2 안의 ********9012를 먼저 바꾸면 #2가 남는다)
            for alias in sorted(self._original, key=len, reverse=True):
                if alias in value:
                    value = value.replace(alias, self._original[alias])
            return value
        if isinstance(value, list):
            return [self.restore(item) for item in value]
        if isinstance(value, dict):
            return {key: self.restore(item) for key, item in value.items()}
        return value

    # ---------------------------------------------------------------- 내부
    def _count(self, kind: str, original: str) -> None:
        key = (kind, hashlib.sha256(original.encode()).hexdigest())
        if key not in self._seen:
            self._seen.add(key)
            self.counts[kind] += 1

    def _secret(self, match: re.Match, kind: str, group: int, count: bool = True) -> str:
        if count:
            self._count(kind, match.group(group))
        mark = f"[REDACTED:{kind}]"
        if group == 0:
            return mark
        # 가릴 그룹만 바꾸고 앞뒤(이름, 구분자, @ 등)는 남긴다
        whole, start, end = match.group(0), match.start(group) - match.start(0), match.end(group) - match.start(0)
        return whole[:start] + mark + whole[end:]

    def _pseudonym(self, kind: str, original: str) -> str:
        known = self._alias.get((kind, original))
        if known:
            return known
        if kind == ACCOUNT_ID:
            alias = "********" + original[-4:]  # 콘솔처럼 끝 네 자리만 보인다 (계정을 구분할 수는 있게)
        elif kind == ACCESS_KEY_ID:
            alias = original[:4] + "********" + original[-4:]  # 키 종류(AKIA·ASIA)와 끝 네 자리
        else:
            local, domain = original.split("@", 1)
            alias = f"{local[0]}***@{domain}"
        # 끝 네 자리가 같은 다른 계정, 첫 글자·도메인이 같은 다른 이메일은 뒤에 번호를 붙여 구분한다
        # (가명 하나가 원래 값 둘을 가리키면 되돌릴 때 틀린 값이 들어간다).
        # 번호를 @ 앞에 넣으면(a***2@…) 그 가명을 다시 가릴 때 2@…가 이메일로 잡히므로 맨 뒤에 붙인다
        base, number = alias, 1
        while alias in self._original:
            number += 1
            alias = f"{base}#{number}"
        self._alias[(kind, original)] = alias
        self._original[alias] = original
        self._count(kind, original)
        return alias
