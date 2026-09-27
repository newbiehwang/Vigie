"""Cognito 로그인 화면(managed login)에 올릴 로고 이미지를 만든다 (cloudformation/base.yaml의 ManagedLoginBranding.Assets).

CloudFormation은 파일을 읽지 못하므로 이미지를 base64 글자로 템플릿에 적어 둔다. 원본은 frontend/src/assets/brand의 SVG이고,
Cognito가 받는 SVG 요소·속성만 남긴다 (로고는 svg·path, xmlns·viewBox·width·height·fill·d. title·role·aria-label은 뺀다.
배경은 그라디언트(defs·linearGradient·radialGradient·stop·rect)로 쓴 login-background.svg를 한 줄로 줄여 그대로 쓴다.
허용 목록: https://docs.aws.amazon.com/cognito/latest/developerguide/managed-login-brandingeditor.html).

    python3 scripts/cognito_branding.py           # 템플릿에 붙일 base64 값을 보여 준다
    python3 scripts/cognito_branding.py --check   # 템플릿의 값이 원본 로고와 같은지 확인한다 (테스트도 같은 것을 본다)

로고를 바꾸면 이 스크립트로 값을 다시 만들어 base.yaml에 붙인다.
"""
import base64
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "frontend" / "src" / "assets" / "brand"
TEMPLATE = ROOT / "cloudformation" / "base.yaml"


def _svg_parts(name: str):
    """브랜드 SVG의 viewBox와 윤곽선(path d)들."""
    text = (BRAND / name).read_text(encoding="utf-8")
    view_box = re.search(r'viewBox="([^"]+)"', text).group(1)
    paths = re.findall(r'<path[^>]* d="([^"]+)"', text)
    return view_box, paths


def _svg(view_box: str, width: int, height: int, fill: str, paths) -> str:
    body = "".join(f'<path fill="{fill}" d="{d}"/>' for d in paths)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box}" width="{width}" height="{height}">'
            f'{body}</svg>')


def _minified(name: str) -> str:
    """이미 Cognito가 받는 요소만으로 쓴 SVG를 한 줄로 (줄바꿈·들여쓰기만 뺀다)."""
    text = (BRAND / name).read_text(encoding="utf-8")
    return re.sub(r">\s+<", "><", text.strip())


def assets() -> dict:
    """카테고리 → SVG 글자.
    FORM_LOGO: 로그인 상자 위의 워드마크 (남색). FAVICON_SVG: 탭 아이콘, 앱과 같은 V (바탕 투명).
    PAGE_BACKGROUND: 파란·남색 번짐 배경 (login-background.svg. 앱의 로그인 전환 화면도 같은 파일을 쓴다)."""
    logo_box, logo_paths = _svg_parts("vigie-logo.svg")
    mark_box, mark_paths = _svg_parts("vigie-mark.svg")
    return {
        "FORM_LOGO": _svg(logo_box, 461, 242, "#232F3E", logo_paths),
        "FAVICON_SVG": _svg(mark_box, 64, 64, "#232F3E", mark_paths),
        "PAGE_BACKGROUND": _minified("login-background.svg"),
    }


def encoded() -> dict:
    return {category: base64.b64encode(svg.encode("utf-8")).decode("ascii") for category, svg in assets().items()}


def template_bytes() -> dict:
    """base.yaml의 ManagedLoginBranding에 적힌 카테고리 → base64."""
    text = TEMPLATE.read_text(encoding="utf-8")
    found = re.findall(r"Category: (\w+)\n(?:\s+\w+: [^\n]+\n)*?\s+Bytes: ([A-Za-z0-9+/=]+)", text)
    return dict(found)


def main(argv) -> int:
    values = encoded()
    if "--check" in argv:
        current = template_bytes()
        stale = [category for category, value in values.items() if current.get(category) != value]
        if stale:
            print("base.yaml의 로고 값이 원본과 다릅니다: " + ", ".join(stale))
            print("python3 scripts/cognito_branding.py 로 새 값을 만들어 붙이세요.")
            return 1
        print("base.yaml의 로고 값이 원본과 같습니다.")
        return 0
    for category, value in values.items():
        print(f"{category} ({len(value)}자)\n{value}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
