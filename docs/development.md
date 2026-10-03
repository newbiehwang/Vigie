# Vigie 개발과 테스트

로컬에서 화면을 띄우고, AWS 없이 장애를 재현하고, 테스트와 CI를 돌리는 방법입니다. 기능은 [features.md](features.md), 배포는 [deployment.md](deployment.md)에 있습니다.

## 프로젝트 구조

```
Vigie/
├─cloudformation     CloudFormation 템플릿 (base, frontend, mcp, main과 중첩 스택)
├─frontend           React 웹 (src/features: chat, audit, users, home / src/mock: 데모와 목업)
├─services
│ ├─llm              LLM Lambda: 관문(가리기, 위험도, 승인, 감사, 역추적), 사용자 관리
│ ├─chat-history     대화 기록 Lambda
│ ├─dashboard        홈 대시보드 수집 Lambda
│ ├─slackbot         Slack 봇 Lambda
│ ├─auth_messages    Cognito 인증 메일 Lambda
│ └─db               Athena 유틸리티 Lambda (캡스톤 때 만든 것)
├─mcp                MCP Lambda: 공식 MCP 서버 연결, 직접 만든 도구, 진단 절차(lambda_mcp/diagnose.py)
├─layers/common      Lambda 공통 레이어
├─local              가짜 AWS(moto) 위의 로컬 장애 재현 서버
├─installer/core     배포 도구 CLI (점검, 설정, 배포, 검증, 정리)
├─tests              단위 테스트 (moto)
├─docs               기능, 배포, 개발 문서와 위협 모델
└─deploy.sh          통합 배포 스크립트
```

## 개발 환경 설정
```bash
# 프론트엔드 개발 서버 (배포된 환경의 로그인·API 사용, deploy.sh가 값을 채운 루트 .env 필요)
# 로그인 뒤 돌아올 주소로 http://localhost:5173/redirect만 등록되어 있으므로 포트 5173으로 띄운다
cd frontend && npm install && npm run dev

# 프론트엔드만 (AWS 없이): 로그인을 건너뛰고 가짜 API로 응답 → 화면만 고칠 때
cd frontend && npm install && npm run dev:mock

# 백엔드 테스트·정적 분석 (Python 3.12)
pip install -r requirements-dev.txt
ruff check .
pytest
```

`dev:mock`은 `frontend/src/mock/api.ts`가 axios 요청을 가로채 백엔드와 같은 모양으로 응답합니다. mock 모드는 곧 데모라서(아래 '데모 사이트') 안내 화면에서 시작하고, '체험하기'를 누르면 로그인 없이 바로 들어갑니다(그 탭에서는 새로 고쳐도 들어간 채, 로그아웃하면 안내 화면). 화면만 고칠 때는 주소에 `?mock-auth=signed-in`을 붙이면 바로 들어갑니다. 처음에는 예시 대화가 하나 있고(관리자에게는 끝난 장애 대응 대화 하나가 더: 웹 ALB 진단 → "종료된 거 다시 켜 줘" → 다시 진단), 질문은 낱말에 맞춰 데모 데이터로 답합니다(`frontend/src/mock/demo/answers.ts`, 맞는 답이 없으면 할 수 있는 질문을 안내). 같은 대화의 앞 질문들로 주제를 이어 가서 "자세히", "왜?", "두 번째 거", "누가 했어?", "어떻게 대응해야 해?", "사고 보고서로 정리해 줘", "그거 멈춰 줘", "막아 줘", "차트로 그려 줘"처럼 이어 묻는 말을 알아듣고, 홈 화면 '지금 확인할 것'의 물어보기("<알람 이름> 알람 왜 울렸어?", "<리소스 이름> 상태 점검해줘")에는 그 알람이 울린 까닭과 그 리소스의 점검 결과로 답하며, 무엇을 가리키는지 모르면 짐작으로 바꾸지 않고 되묻습니다. 실제 Vigie가 거절하거나 할 수 없는 요청(승인 건너뛰기, 비밀 값, 삭제·종료, 공개·포트 개방처럼 넓히는 변경, IAM·보안 그룹·AWS Config처럼 도구가 없는 일, AWS와 관계없는 질문)에는 그 까닭과 사람이 직접 할 방법을 답하고, 로그를 읽다 도구가 실패하면 지표로 좁혀 답합니다("Lambda 오류 원인은?"). 변경을 부탁하면 승인 카드가 뜨고, 승인하면 홈 대시보드와 이후 답에 반영됩니다: 조사용 인스턴스 중지·시작("Bud's Forensic AMI 멈춰 줘"), `frothlywebcode` 퍼블릭 액세스 차단 켜기("퍼블릭 액세스 차단 켜 줘"), Vigie 로그 보존 기간("보존 기간 14일로 줄여 줘", 묻기만 하면 지금 값을 답함), Vigie 알람 알림 끄기("vigie-dev-api-5xx 알람 꺼 줘", Frothly 보안 알람은 끄지 않기를 권함). 이미 그 상태면 바꿀 것이 없다고 답합니다. 장애를 물으면 서비스 진단(`diagnoseService`, 관리자 전용)을 부른 것처럼 지금 상태로 진단합니다: 웹 ALB 5xx("frothly-web-alb에서 503이 나요", 이미 복구됨 · 원인 L2 종료 기록 · 증상 L4), 버킷 공개("frothlywebcode가 공개된 것 같아요", 차단 꺼짐 L6 주의 · 오늘 56분 공개됐던 기록), 키 유출("web_admin 키가 GitHub에 올라갔대요", 이미 꺼진 키). "차단 다시 켜 줘"로 승인한 뒤 "다시 진단해 줘"라고 하면 L6이 정상으로 바뀌고, 진단한 웹 서버를 "다시 켜 줘"라고 하면 종료된 인스턴스는 켤 수 없다고 안내합니다. 대화 기록은 메모리에만 있어 새로 고치면 처음으로 돌아갑니다. mock 사용자는 관리자이고(사용자 관리 탭에 예시 사용자 5명), 주소에 `?mock-role=member`를 붙여 열면 일반 사용자 화면(감사 로그·사용자 관리 탭 없음)을 봅니다. 화면을 고칠 때 쓰는 예시도 있습니다: '차트 예시'(차트 4종), '갤러리'(차트 15종), '인젝션'(로그에 심긴 지시문). 질문에 '천천히'를 넣으면 단계마다 여섯 배 느린 답변으로, 기다리는 동안의 한 줄이 바뀌어 가는 모습을 봅니다. 질문에 '로그대로'를 넣으면 로그에 심긴 지시를 따른 변경 요청(승인 카드의 의심 경고, 감사 로그의 체류)을 보고, 승인한 뒤 감사 로그에서 그 작업의 기록을 열면 처리 단계 다이어그램에서 판정을 봅니다. 감사 기록은 실제 사용처럼 만듭니다(평일 업무 시간에 많고 밤·주말에 드묾, 도구가 많이 실패하는 장애 구간 세 번, 기록이 없는 하루, 30일에 약 1,000건). 판정과 처리 단계를 바로 확인하도록 판정이 서로 다른 변경 작업 여섯 가지와 사용자 관리 기록 세 건(초대·권한 변경·정지)도 기본 기록에 들어 있습니다. 변경 작업은 데모의 Frothly 사고 뒤에 Vigie로 한 일로, 그 기록의 시간 축 위에 있습니다(괄호 안은 검색어): 공개 사고 뒤 웹 로그 버킷에 예방으로 퍼블릭 액세스 차단 켜기(정상 승인·실행, `frothlyweblogs 퍼블릭`), 조사용 인스턴스 중지를 결정자가 거절(`Forensic`), 이미 종료된 웹 서버 중지 실패(`IncorrectInstanceState`), 공격자가 웹 요청에 심어 웹 서버 로그에 남은 지시를 읽고 Vigie 자신의 로그 보존 기간을 1일로 줄인 변경(의심 뒤 요청 → 승인·실행, `"의심 뒤 요청"`), 등록부에 없는 `put_bucket_acl` 호출(`미등록 도구`), 승인 기록 없이 실행된 어긋난 기록(`frothlyinvestigations`). 서비스 진단 기록도 있습니다: 버킷 공개 진단 → bstoll이 ACL을 되돌린 뒤 다시 진단(`frothlywebcode`, 한 대화. 차단은 켜지 않은 채 끝나 목업의 지금 상태로 이어짐), 웹 ALB 대응(끝난 대화와 같은 질문들), 유출된 키 진단(`web_admin`). 주소에 `?mock-audit=many`를 붙이면 약 2,700건을 만들어 긴 목록과 2,000건 한도를 확인합니다. 배포용 빌드에는 들어가지 않습니다.

### 로컬에서 장애 재현 (AWS 없이, 대화로)
가짜 AWS(moto) 위에 Vigie의 MCP 서버·API 서버를 띄우고, 장애 시나리오 44개(진단 채점 테스트와 같은 것) 중 하나를 심은 뒤 Vigie 대화창에서 물어봅니다. 실제 AWS에는 요청이 나가지 않습니다. 대화의 모델로 이 맥에 로그인된 Claude Code를 쓰고(API 키 없이), 도구 호출은 중계 MCP를 거쳐 배포와 같은 승인 요청·가리기·감사 로그·진행 상황을 남깁니다. 자세한 것은 [`local/README.md`](../local/README.md)에 있습니다.

```bash
python -m local.stack --scenario alb-targets-stopped   # MCP :8765 · API :8787
npm --prefix frontend run dev:local                     # 화면 (로컬 관리자로 바로 들어간다)
python -m local.scenario list                           # 다른 시나리오: apply <이름> · reset
```

### 데모 사이트 (정적 배포)
AWS와 Claude를 부르지 않는 시연용 사이트입니다. mock 모드를 정적 파일로 빌드하므로 백엔드·로그인·비용 없이 링크로 공유할 수 있습니다.

```bash
cd frontend && npm run build:demo   # frontend/dist-demo/ (Cognito 설정 값은 번들에 넣지 않음)
```

`dist-demo/`를 정적 호스팅(Amplify Hosting의 수동 배포, GitHub Pages, S3 정적 웹 사이트 등)에 올립니다. 화면 주소(`/chat` 등)를 새로 고쳐도 열리도록 `index.html`을 `404.html`로도 복사해 둡니다(GitHub Pages용). 다른 호스팅은 없는 경로를 `index.html`로 돌려주는 규칙을 둡니다.

**Vercel에 올리기** (`frontend/vercel.json`에 설정이 있습니다)
1. Vercel에서 이 GitHub 저장소를 가져오고(Import), **Root Directory**를 `frontend`로 정합니다. 나머지(빌드 명령 `npm run build:demo`, 결과 폴더 `dist-demo`)는 `vercel.json`이 정합니다.
2. 환경 변수는 넣지 않습니다. 데모는 백엔드·Cognito·Claude를 쓰지 않습니다.
3. 이후 main에 머지하면 자동으로 다시 배포되고, PR마다 미리보기 주소가 생깁니다.

`vercel.json`은 없는 경로(`/chat`, `/audit` 등)를 `index.html`로 돌려 새로 고쳐도 화면이 열리게 하고, 이름에 해시가 붙은 `assets/` 파일은 오래 캐시합니다. AWS 계정과 따로 두므로, AWS 무료 플랜이 끝나도 데모 링크는 그대로입니다.

데모의 홈 대시보드와 답변은 공개된 실제(가상 회사) AWS 운영 기록으로 만듭니다. 시각은 지금에 맞춰 옮깁니다.

| 데이터 | 라이선스 | 쓰는 곳 |
|:--|:--|:--|
| [Splunk Boss of the SOC (BOTS) v3](https://github.com/splunk/botsv3): 가상 회사 Frothly의 AWS 계정 기록 약 6시간 | CC0 1.0 | CloudTrail(변경·로그인·거부된 호출), CloudWatch 지표(EC2·RDS·ALB·Lambda), AWS Config 규칙 위반, 보안 그룹·탄력적 IP |
| [FinOps Foundation FOCUS 1.0 Sample Data](https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data): 실제 AWS 청구를 익명화한 표 | CC BY 4.0 | 서비스별 비용 비중만 (금액은 월 예상액을 정해 나눔) |

- 기록에 없는 값(알람 정의, 비용 금액과 전월 대비 증감, 지표가 없는 시간)은 `frontend/src/mock/demo/frothly.ts`에서 만들고 그 파일에 적어 두었습니다.
- 데이터는 `scripts/demo_data/build_frothly.py`가 뽑아 `frontend/src/mock/demo/frothly.json`(약 40KB)으로 둡니다. BOTS v3는 Splunk에 미리 색인된 형태(335MB)로만 배포되어, Splunk 없이 색인의 원본 저널에서 JSON 이벤트를 읽습니다. 다시 뽑는 방법은 스크립트 맨 위에 있습니다(원본 파일은 저장소에 넣지 않습니다).
- 데모의 안내 화면 맨 아래에 두 출처를 적습니다.

## 테스트
`tests/`의 단위 테스트는 [moto](https://github.com/getmoto/moto)로 DynamoDB, CloudWatch Logs, CloudWatch, Cost Explorer를 모킹해 AWS 계정 없이 실행됩니다.

| 파일 | 검증 내용 |
|---|---|
| `test_chat_history.py` | 토큰 `sub` 기반 사용자 식별, 다른 사용자의 세션 조회·수정·삭제 차단 |
| `test_llm_service.py` | 웹 요청의 Slack 전용 필드 제거, 세션 히스토리 소유자 확인, CORS 허용 목록 |
| `test_mcp_client.py` | MCP Function URL 호출 시 SigV4 서명 |
| `test_slack_security.py` | Slack 요청 서명(위조·변조·재전송), Cognito ID 토큰(aud·iss·만료·서명) 검증 |
| `test_mcp_tools.py` | MCP 도구: 공식 서버 도구가 목록에 합쳐지는지(`$ref` 없이), 공식 CloudWatch·Cost Explorer·문서 검색 호출, 도구 오류를 `isError` 결과로 돌려주는지, 대시보드 도구와 세션 저장소 |
| `test_latest_model.py` | 요청할 때 최신 Sonnet 자동 선택(출시일 비교, 새 Sonnet으로 저절로 넘어감), 목록 캐시·오류 시 마지막 목록·한 번도 못 받았을 때의 예비 모델, 모델별 사고 설정, 요청의 modelId 무시, 화면·Slack에 모델 선택이 없음 |

## CI (`.github/workflows/ci.yml`)
PR과 `main` 푸시마다 세 작업이 병렬로 실행됩니다. AWS 자격 증명은 사용하지 않습니다.

| 작업 | 내용 |
|---|---|
| Python | `ruff`(문법 오류·정의되지 않은 이름), `pytest` |
| IaC | `cfn-lint`(오류 시 실패), `checkov` 보안 스캔, `deploy.sh` 문법 검사 |
| 프론트엔드 | `tsc` 타입 검사, `vite build` |

`checkov`는 도입 시점의 기존 결과를 `cloudformation/.checkov.baseline`에 기준선으로 저장하고, **새로 생기는 보안 문제만** 실패로 처리합니다. 기준선의 항목은 하나씩 해결하면서 기준선을 다시 만듭니다.
