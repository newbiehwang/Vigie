"""Cognito 인증 메일 꾸미기 (Custom message Lambda 트리거, cloudformation/base.yaml의 UserPool.LambdaConfig).

Cognito가 메일을 보내기 직전에 이 함수를 부르고, 돌려준 제목·본문(HTML)으로 보낸다. 메일마다 다르게 쓴다.
    triggerSource                       메일                         넣어야 하는 자리
    CustomMessage_SignUp                가입 인증 코드               {####}
    CustomMessage_ResendCode            가입 인증 코드 (다시 보냄)   {####}
    CustomMessage_ForgotPassword        비밀번호 재설정 코드         {####}
    CustomMessage_AdminCreateUser       초대 (임시 비밀번호)         {username}, {####}
    CustomMessage_UpdateUserAttribute   이메일 변경 인증 코드        {####}
    CustomMessage_VerifyUserAttribute   이메일 인증 코드             {####}
- 코드·임시 비밀번호는 이 함수가 알지 못한다. Cognito가 준 자리 표시(request.codeParameter 등)를 본문에 넣으면
  Cognito가 보내기 직전에 실제 값으로 바꾼다. 자리 표시가 빠지면 Cognito는 메일을 보내지 않으므로 늘 넣는다.
- 위에 없는 메일(MFA 코드 등)은 건드리지 않는다 (Cognito 기본 문구).
- 로고는 프론트엔드에 올린 PNG(frontend/public/vigie-email-logo.png)를 주소로 건다. 메일 프로그램은 SVG를 잘 못 그린다.
- 색은 웹과 같은 Midnight Ink (frontend/src/styles.css: 글자 #0a1628, 주 색 #1e5aa8, 바탕 #f5f7fa, 선 #d6dee8).
  글꼴은 Pretendard를 먼저 적지만 메일 프로그램은 웹 글꼴을 받지 않으므로, 설치된 곳에서만 쓰이고 나머지는 시스템 글꼴이다.
- AWS를 부르지 않는다 (글자만 만든다). 실패하면 Cognito가 메일을 보내지 못하므로 오류 없이 끝나게 단순하게 둔다.
"""
import os
from html import escape
from typing import Any, Dict, Optional

# Cognito가 정한 코드 유효 시간 (가입 인증 코드 24시간, 비밀번호 재설정 코드 1시간, 초대 임시 비밀번호 7일: base.yaml)
SIGN_UP_HOURS = 24
RESET_HOURS = 1
INVITE_DAYS = 7


def _config() -> Dict[str, str]:
    app_url = os.environ.get("APP_URL", "").rstrip("/")
    return {"app_url": app_url, "logo_url": os.environ.get("LOGO_URL", "")}


def _code_box(code: str) -> str:
    return (f'<div style="margin:20px 0;padding:16px 0;border-radius:10px;background:#e9eff6;'
            f'text-align:center;font-family:Menlo,Consolas,monospace;font-size:28px;font-weight:700;'
            f'letter-spacing:6px;color:#0a1628;">{code}</div>')


def _button(url: str, label: str) -> str:
    if not url:
        return ""
    return (f'<p style="margin:24px 0 8px;text-align:center;"><a href="{escape(url)}" '
            f'style="display:inline-block;padding:12px 28px;border-radius:10px;background:#1e5aa8;color:#ffffff;'
            f'font-weight:700;font-size:15px;text-decoration:none;">{escape(label)}</a></p>')


def _layout(title: str, body: str, logo_url: str) -> str:
    """메일 틀: 옅은 바탕 위 흰 카드, 맨 위 로고. 메일 프로그램이 CSS 파일을 읽지 못하므로 style을 요소마다 적는다."""
    # 높이도 적는다: 이미지를 막는 메일 프로그램에서도 로고 자리가 원래 비율(360×188)만큼만 비게
    logo = (f'<img src="{escape(logo_url)}" width="120" height="63" alt="Vigie" '
            f'style="display:block;margin:0 auto 20px;border:0;">'
            if logo_url else
            '<p style="margin:0 0 20px;text-align:center;font-family:Georgia,serif;font-style:italic;font-size:32px;'
            'color:#0a1628;">Vigie</p>')
    return (
        '<div style="margin:0;padding:32px 16px;background:#f5f7fa;">'
        '<div style="max-width:480px;margin:0 auto;padding:32px 28px;border:1px solid #d6dee8;border-radius:14px;'
        'background:#ffffff;font-family:Pretendard,-apple-system,BlinkMacSystemFont,\'Apple SD Gothic Neo\',\'Malgun Gothic\','
        'sans-serif;color:#37485e;font-size:14px;line-height:1.6;">'
        f'{logo}'
        f'<h1 style="margin:0 0 12px;font-size:19px;color:#0a1628;">{escape(title)}</h1>'
        f'{body}'
        '<p style="margin:28px 0 0;padding-top:16px;border-top:1px solid #e8edf2;font-size:12px;color:#596d87;">'
        '이 메일은 Vigie가 자동으로 보냈습니다. 요청한 적이 없다면 무시해도 됩니다. 코드는 누구에게도 알려 주지 마세요.</p>'
        '</div></div>'
    )


def message(trigger: str, request: Dict[str, Any], config: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
    """이 트리거의 {"subject", "html"}. 꾸미지 않는 트리거면 None (Cognito 기본 문구를 쓴다)."""
    config = config or _config()
    code = request.get("codeParameter") or "{####}"
    app_url, logo_url = config["app_url"], config["logo_url"]

    if trigger in ("CustomMessage_SignUp", "CustomMessage_ResendCode"):
        subject, title = "[Vigie] 가입 인증 코드", "이메일 주소를 확인해 주세요"
        body = ('<p style="margin:0;">Vigie 가입을 마치려면 아래 인증 코드를 가입 화면에 입력하세요.</p>'
                f'{_code_box(code)}'
                f'<p style="margin:0;color:#596d87;">코드는 {SIGN_UP_HOURS}시간 동안 쓸 수 있습니다.</p>')
    elif trigger == "CustomMessage_ForgotPassword":
        subject, title = "[Vigie] 비밀번호 재설정 코드", "비밀번호를 다시 정해 주세요"
        body = ('<p style="margin:0;">비밀번호 재설정을 요청하셨습니다. 아래 코드를 입력하고 새 비밀번호를 정하세요.</p>'
                f'{_code_box(code)}'
                f'<p style="margin:0;color:#596d87;">코드는 {RESET_HOURS}시간 동안 쓸 수 있습니다. '
                '요청하지 않았다면 이 메일을 무시하세요. 비밀번호는 바뀌지 않습니다.</p>')
    elif trigger == "CustomMessage_AdminCreateUser":
        username = request.get("usernameParameter") or "{username}"
        subject, title = "[Vigie] 계정이 만들어졌습니다", "Vigie에 초대되었습니다"
        body = ('<p style="margin:0;">관리자가 Vigie 계정을 만들었습니다. 아래 아이디와 임시 비밀번호로 로그인한 뒤 '
                '새 비밀번호를 정하세요.</p>'
                '<table role="presentation" style="width:100%;margin:20px 0;border-collapse:collapse;'
                'border-radius:10px;background:#e9eff6;">'
                '<tr><td style="padding:12px 16px 4px;color:#596d87;font-size:12px;">아이디</td></tr>'
                f'<tr><td style="padding:0 16px 10px;font-weight:700;color:#0a1628;">{username}</td></tr>'
                '<tr><td style="padding:4px 16px 4px;color:#596d87;font-size:12px;">임시 비밀번호</td></tr>'
                f'<tr><td style="padding:0 16px 14px;font-family:Menlo,Consolas,monospace;font-size:18px;'
                f'font-weight:700;color:#0a1628;">{code}</td></tr>'
                '</table>'
                f'<p style="margin:0;color:#596d87;">임시 비밀번호는 {INVITE_DAYS}일 동안 쓸 수 있습니다.</p>'
                f'{_button(app_url, "Vigie 열기")}')
    elif trigger in ("CustomMessage_UpdateUserAttribute", "CustomMessage_VerifyUserAttribute"):
        subject, title = "[Vigie] 이메일 인증 코드", "이메일 주소를 확인해 주세요"
        body = ('<p style="margin:0;">이메일 주소를 확인하려면 아래 인증 코드를 입력하세요.</p>'
                f'{_code_box(code)}')
    else:
        return None
    return {"subject": subject, "html": _layout(title, body, logo_url)}


def lambda_handler(event, context):
    built = message(event.get("triggerSource", ""), event.get("request") or {})
    if built:
        response = event.setdefault("response", {})
        response["emailSubject"] = built["subject"]
        response["emailMessage"] = built["html"]
    return event
