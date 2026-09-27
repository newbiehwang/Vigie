"""Cognito 로그인 화면(managed login) 설정 (cloudformation/base.yaml, scripts/cognito_branding.py).
- 새 로그인 화면을 쓰려면 Essentials 요금제와 도메인의 ManagedLoginVersion 2가 함께 있어야 한다
- 템플릿에 base64로 적은 로고가 frontend/src/assets/brand의 원본과 같다 (로고를 바꾸고 다시 만들지 않으면 실패)
- 로고 SVG에는 Cognito가 받는 요소·속성만 있다
실제 AWS는 부르지 않는다."""
import base64
import re
import sys

import yaml

from conftest import ROOT

sys.path.insert(0, str(ROOT / "scripts"))
import cognito_branding  # noqa: E402


class CfnLoader(yaml.SafeLoader):
    pass


# !GetAtt·!Sub·!Ref 등은 글자 그대로 둔다
CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_scalar(node)
                                if isinstance(node, yaml.ScalarNode) else None)


def resources():
    return yaml.load((ROOT / "cloudformation" / "base.yaml").read_text(encoding="utf-8"),
                     Loader=CfnLoader)["Resources"]


def test_managed_login_needs_essentials_and_version_2():
    res = resources()
    assert res["UserPool"]["Properties"]["UserPoolTier"] == "ESSENTIALS"
    assert res["UserPoolDomain"]["Properties"]["ManagedLoginVersion"] == 2
    branding = res["ManagedLoginBranding"]
    assert branding["Type"] == "AWS::Cognito::ManagedLoginBranding"
    assert branding["Properties"]["ClientId"] == "UserPoolClient"
    assert branding["Properties"]["UseCognitoProvidedValues"] is False  # true면 Settings·Assets를 줄 수 없다


def test_logo_bytes_in_template_match_the_brand_files():
    assert cognito_branding.main(["--check"]) == 0
    assets = {a["Category"]: a for a in resources()["ManagedLoginBranding"]["Properties"]["Assets"]}
    assert set(assets) == {"FORM_LOGO", "FAVICON_SVG", "PAGE_BACKGROUND"}
    assert all(a["Extension"] == "SVG" and a["ColorMode"] == "LIGHT" for a in assets.values())
    logo = base64.b64decode(assets["FORM_LOGO"]["Bytes"]).decode("utf-8")
    original = (ROOT / "frontend" / "src" / "assets" / "brand" / "vigie-logo.svg").read_text(encoding="utf-8")
    assert re.search(r' d="([^"]+)"', logo).group(1) == re.search(r' d="([^"]+)"', original).group(1)


# Cognito 문서의 허용 목록 가운데 여기서 쓰는 것 (요소·속성 이름은 대소문자를 가리지 않고 비교한다)
ALLOWED_ELEMENTS = {"svg", "path", "defs", "lineargradient", "radialgradient", "stop", "rect"}
ALLOWED_ATTRIBUTES = {"xmlns", "viewbox", "width", "height", "fill", "d", "preserveaspectratio", "id", "x1", "y1",
                      "x2", "y2", "gradientunits", "cx", "cy", "r", "offset", "stop-color", "stop-opacity"}


def test_svgs_use_only_elements_and_attributes_cognito_accepts():
    for svg in cognito_branding.assets().values():
        assert {name.lower() for name in re.findall(r"<(\w+)", svg)} <= ALLOWED_ELEMENTS
        assert {name.lower() for name in re.findall(r"\s([\w:-]+)=", svg)} <= ALLOWED_ATTRIBUTES


def test_page_background_is_on_and_shared_with_the_app():
    settings = resources()["ManagedLoginBranding"]["Properties"]["Settings"]
    assert settings["components"]["pageBackground"]["image"]["enabled"] is True
    # 앱의 로그인 전환 화면(LoginSplash)도 같은 배경 파일을 쓴다
    splash = (ROOT / "frontend" / "src" / "components" / "layout" / "LoginSplash.tsx").read_text(encoding="utf-8")
    assert "assets/brand/login-background.svg" in splash


def test_colors_are_rrggbbaa():
    settings = resources()["ManagedLoginBranding"]["Properties"]["Settings"]
    colors = re.findall(r"'(?:color|backgroundColor|textColor|borderColor)': '([^']+)'", str(settings))
    assert colors and all(re.fullmatch(r"[0-9a-f]{8}", color) for color in colors)
