# 로컬 장애 재현

실제 AWS 없이 가짜 AWS(moto) 위에 Vigie의 MCP 서버와 API 서버를 띄웁니다. 장애 시나리오 43개 중 하나를 심은 뒤, Vigie 화면의 대화창에서 물어보며 진단을 재현합니다.
진단 채점 테스트(`tests/test_diagnose.py`)와 같은 환경·시나리오(`local/world.py`)를 씁니다.

```
화면 (npm run dev:local) ──▶ 로컬 API 서버 :8787 (local/api.py) ──▶ LLM·대화 기록·사용자 관리 Lambda 코드 그대로
                                                                     │ 도구 호출
Claude Code (직접 붙일 때) ─────────────────────────────▶ MCP 서버 :8765 (local/stack.py → mcp/app.py 그대로)
                                                                     │
python -m local.scenario ──▶ /_local/scenario ──▶ 정상 환경 + 장애    ▼ 같은 프로세스 안의 moto (가짜 AWS, 서울 리전)
```

## 실행

Python 3.12 이상에서 개발용 의존성을 설치한 뒤 띄웁니다.

```bash
pip install -r requirements-dev.txt
```

```bash
python -m local.stack --admin
```

MCP 서버(:8765)와 로컬 API 서버(:8787)가 함께 뜹니다.
`--admin`은 Claude Code를 `/mcp`로 직접 붙였을 때 도구 호출을 관리자 요청으로 보냅니다. 진단 도구 `diagnoseService`가 관리자 전용이라 이때 필요합니다.
화면에서 한 요청에는 영향이 없습니다. LLM 서버 코드가 `/mcp/server`로 부르고, 요청자의 권한을 보고 관리자 표시를 스스로 붙입니다.

다른 터미널에서 시나리오를 고릅니다.

```bash
python -m local.scenario list
```

```bash
python -m local.scenario apply alb-targets-stopped
```

```
시나리오   alb-targets-stopped
심은 장애  대상 두 대 중지 + StopInstances 기록
진단 대상  alb · web-alb
기대 판정  원인 L2 · L5 / 증상 L4
물어볼 말  web-alb에서 503이 나요. 원인 찾아 줘
```

`apply`는 환경을 통째로 다시 만들므로 앞 시나리오가 남지 않습니다. `reset`은 정상 환경으로 되돌립니다.

## Vigie 화면에서 대화하기

다른 터미널에서 화면을 띄웁니다. Cognito 없이 로컬 관리자로 바로 들어가고, 요청은 로컬 API 서버로 갑니다.

```bash
npm --prefix frontend run dev:local
```

- **사용자 바꾸기:** 주소에 `?local-role=member`(또는 `decider`, `admin`)를 붙여 엽니다. 그 탭에서는 다른 주소로 옮겨도 유지됩니다.
  로컬 사용자는 `admin@vigie.local`(관리자) · `decider@vigie.local`(결정자) · `member@vigie.local`(일반 사용자) 세 명입니다.
- **홈:** 시나리오를 심을 때마다 대시보드를 다시 모읍니다. '최근 변경'에는 시나리오가 심은 변경 기록이 보입니다.
- **대화:** 지금은 배포와 같은 Anthropic API 경로를 씁니다. 서버를 띄우는 셸에 `ANTHROPIC_API_KEY`가 있어야 하고, API 요금이 듭니다.
  키가 없으면 대화창에 안내 문구가 나옵니다. 이 맥에 로그인된 Claude Code를 모델로 쓰는 방법은 다음 단계에서 더합니다.
- **감사 로그:** 관리자가 물어 `diagnoseService`가 불리면, 그 행을 눌렀을 때 서비스의 진단 층 그림이 보입니다.
- **승인:** 로컬 환경은 dev처럼 관리자·결정자가 자기 요청도 승인할 수 있습니다. 승인하면 가짜 AWS의 자원이 실제로 바뀝니다.

## Claude Code로 대화해 보기

떠 있는 서버를 이번 세션에만 붙입니다 (Claude Code 설정 파일은 바꾸지 않습니다).

```bash
claude --mcp-config '{"mcpServers":{"vigie":{"type":"http","url":"http://127.0.0.1:8765/mcp"}}}'
```

그다음 `물어볼 말`처럼 물으면 모델이 `diagnoseService`를 부르고 판정을 설명합니다.
이렇게 직접 붙이면 Vigie의 승인 요청·감사 로그·가리기를 거치지 않습니다.

## 실제 AWS에 닿지 않게 하는 장치

- 모든 AWS 호출은 같은 프로세스 안의 moto가 받습니다. 별도 moto 서버를 두지 않아 AWS 요청이 네트워크로 나가지 않습니다.
- `AWS_`로 시작하는 환경 변수(프로필·엔드포인트·자격 증명)를 모두 지우고 가짜 자격 증명을 넣습니다. `~/.aws`의 설정·자격 증명 파일도 읽지 않습니다.
- moto가 모르는 AWS 주소로 나가려는 요청은 닫힌 로컬 프록시(127.0.0.1:9)로 보내 실패시킵니다. AWS 문서 검색만 예외입니다.
- 127.0.0.1에만 붙고, Host 머리글이 로컬 주소가 아니면 거절합니다. 시나리오 주소는 `application/json` 요청만 받습니다.
- 로컬 API 서버는 `X-Vigie-Local-Role` 머리글이 없는 요청을 거절합니다. 이 머리글을 붙이려면 브라우저가 사전 요청(CORS)을 보내야 하는데, 사전 요청은 `localhost`·`127.0.0.1` 화면 주소에만 허락합니다. 다른 사이트의 페이지가 로컬 서버를 부를 수 없습니다.

## 한계

- 진단은 최근 1시간의 지표를 봅니다. 서버가 10분마다 넣어 둔 지표·로그를 지금 시각에 다시 넣으므로, 오래 띄워 둬도 판정이 유지됩니다.
- CloudTrail 조회와 Cost Explorer 비용은 moto에 없어서, 시나리오가 심은 기록을 진단 절차가 읽도록 바꿔 끼웁니다.
- 변경 도구(인스턴스 시작, 퍼블릭 액세스 차단 등)는 여기서도 승인 없이는 실행되지 않습니다.
- 서버를 멈추면 가짜 AWS의 상태도 사라집니다. 대화 기록·감사 로그도 가짜 AWS의 테이블에 있으므로 함께 사라집니다(시나리오를 바꿀 때는 남습니다).
- 홈의 비용은 Cost Explorer가 moto에 없어서 $0으로 보입니다. 비용 급증 진단은 시나리오가 심은 비용을 읽습니다.
- Slack 봇과 사용자 초대 메일은 로컬에서 동작하지 않습니다.
