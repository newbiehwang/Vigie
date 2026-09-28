"""떠 있는 로컬 장애 재현 서버(local/stack.py)에 시나리오를 심고 되돌린다.

    python -m local.scenario list                      # 시나리오 목록 (지금 심긴 것에 ▶)
    python -m local.scenario apply alb-targets-stopped # 정상 환경을 새로 만들고 장애를 심는다
    python -m local.scenario show                      # 지금 심긴 시나리오와 물어볼 말
    python -m local.scenario reset                     # 정상 환경으로 되돌린다
    python -m local.scenario refresh                   # 지표·로그를 지금 시각에 다시 넣는다 (서버가 10분마다 알아서 한다)

apply는 환경을 통째로 다시 만든다 (앞 시나리오가 남지 않는다). 대화창에서는 출력된 '물어볼 말'처럼 물으면 된다.
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
import urllib.error
import urllib.request

from local.stack import DEFAULT_PORT

# 사용자 환경의 프록시 설정을 타지 않고 로컬 서버로 바로 간다
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(base: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base.rstrip("/") + path, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json"} if data else {})
    try:
        with _OPENER.open(request, timeout=120) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise SystemExit(json.loads(error.read() or b"{}").get("error") or f"HTTP {error.code}")
    except urllib.error.URLError:
        raise SystemExit(f"로컬 서버에 닿지 않습니다: {base}\n먼저 python -m local.stack 으로 띄웁니다")


def layers(ids: list) -> str:
    return " · ".join(ids) or "없음"


def pad(text: str, width: int) -> str:
    """터미널 폭 기준으로 채운다 (한글은 두 칸)."""
    cells = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - cells)


def show(current: dict) -> None:
    if not current.get("scenario"):
        print("지금 환경: 정상 (심은 장애 없음)")
        return
    print(f"시나리오   {current['scenario']}")
    print(f"심은 장애  {current['about']}")
    target = f" → {current['target']}" if current.get("target") else ""
    print(f"진단 대상  {current['service']} · {current['resource']}{target}")
    print(f"기대 판정  원인 {layers(current['causes'])} / 증상 {layers(current['symptoms'])}")
    print(f"물어볼 말  {current['ask']}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="로컬 장애 재현 서버의 시나리오를 바꾼다")
    parser.add_argument("--url", default=f"http://127.0.0.1:{DEFAULT_PORT}", help="local.stack 주소")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="시나리오 목록")
    commands.add_parser("show", help="지금 심긴 시나리오")
    commands.add_parser("apply", help="시나리오 심기").add_argument("name")
    commands.add_parser("reset", help="정상 환경으로 되돌리기")
    commands.add_parser("refresh", help="지표·로그를 지금 시각에 다시 넣기")
    args = parser.parse_args(argv)

    if args.command == "list":
        listing = call(args.url, "/_local/scenarios")
        current = listing["current"].get("scenario")
        width = max(len(s["name"]) for s in listing["scenarios"])
        for s in listing["scenarios"]:
            mark = "▶" if s["name"] == current else " "
            print(f"{mark} {s['name']:<{width}}  원인 {pad(layers(s['causes']), 12)} 증상 {pad(layers(s['symptoms']), 8)} "
                  f"{s['about']}")
    elif args.command == "show":
        show(call(args.url, "/_local/scenario"))
    elif args.command == "apply":
        show(call(args.url, "/_local/scenario", {"action": "apply", "name": args.name}))
    elif args.command == "reset":
        show(call(args.url, "/_local/scenario", {"action": "reset"}))
    else:
        call(args.url, "/_local/scenario", {"action": "refresh"})
        print("지표·로그를 지금 시각에 다시 넣었습니다")


if __name__ == "__main__":
    sys.exit(main())
