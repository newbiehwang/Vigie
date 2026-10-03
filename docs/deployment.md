# Vigie 배포와 운영

처음 배포, 재배포, 운영 중 알람과 대시보드, GitHub Actions 배포 파이프라인을 적은 문서입니다. 기능은 [features.md](features.md), 개발과 테스트는 [development.md](development.md)에 있습니다.

## CloudFormation 스택 구성
| 스택 (템플릿) | 스택 이름 | 역할 |
|---|---|---|
| base (`base.yaml`) | `vigie-base-{env}` | API Gateway RestApi, Cognito User/Identity Pool, DynamoDB 테이블 4개, S3 버킷 7개, SSM Parameter |
| frontend (`frontend.yaml`) | `vigie-frontend-{env}` | Amplify App/Branch, 프론트엔드 버킷 정책 |
| mcp (`mcp.yaml`) | `vigie-mcp-{env}` | MCP 이미지용 ECR 리포지토리, CodeBuild 프로젝트 |
| main (`main.yaml`) | `vigie-{env}` | 아래 5개 Nested Stack과 API Gateway 최종 Deployment |
| └ llm (`llm.yaml`) | Nested | LLM Lambda, MCP Lambda(Container Image, Function URL), 사용자 관리 Lambda(`/users`), `/llm1`, `/llm1/progress/{requestId}`, `/llm2`, `/audit`, `/actions/{actionId}` 등, 답변 진행 상황 테이블, 감사 로그 테이블·로그 그룹, 변경 작업 승인 테이블 |
| └ logs (`logs.yaml`) | Nested | Athena 유틸리티 Lambda, `/execute-query`, `/create-table` |
| └ slackbot (`slackbot.yaml`) | Nested | Slack 봇 Lambda, `/login`, `/callback`, `/events`, `/slack-interactions` |
| └ chat-history (`chat-history.yaml`) | Nested | 대화 기록 Lambda, `/sessions/*` |
| └ monitoring (`monitoring.yaml`) | Nested | CloudWatch 알람 21개, SNS 알림 토픽, 서비스 대시보드, Logs Insights 저장 쿼리 |

**의존 관계**: base가 API Gateway·Cognito·DynamoDB·S3를 만들고, 나머지 스택은 base의 RestApi ID와 루트 리소스 ID를 파라미터로 받아 리소스와 메서드를 추가합니다. llm 스택은 mcp 스택이 ECR에 올린 이미지를 사용합니다.

**배포 순서** (`deploy.sh`):
1. 템플릿을 S3에 업로드
2. base 스택 배포
3. frontend 스택 배포 → Amplify 도메인을 base 스택에 반영(콜백 URL 갱신)
4. Lambda Layer·함수 코드 패키징 및 S3 업로드
5. mcp 스택 배포 → CodeBuild로 MCP 이미지 빌드 후 ECR에 푸시
6. main 스택 배포 (llm, logs, slackbot, chat-history, monitoring + API Deployment) → API 스테이지 재배포, X-Ray 추적 활성화
7. 프론트엔드 환경 변수 설정, 빌드, Amplify 배포
8. MCP Function URL을 base 스택 SSM 파라미터에 반영

## 설치 및 배포

배포하는 방법은 두 가지입니다. 결과는 같고, 둘 다 `deploy.sh`로 배포합니다.

| 방법 | 언제 쓰나 |
|---|---|
| [배포 도구 `vigie-installer`](#배포-도구-vigie-installer) | 처음 배포할 때, 그리고 평소 운영에. 배포 전후의 점검·설정까지 함께 합니다 |
| [아래의 명령줄 절차](#사전-요구사항) | 이미 환경을 아는 경우, 직접 단계를 고를 때 |

### 배포 도구 `vigie-installer`

`deploy.sh`만으로는 부족한 부분 — 배포 전 할당량·비밀 값 점검, 배포 후 검증, GitHub 자동 배포 설정, 환경 정리 — 을 단계로 묶은 명령줄 도구입니다. Python 표준 라이브러리만 쓰므로 설치할 것이 없습니다(Python 3.10 이상).

```bash
installer/core/vigie-installer check --env dev      # 도구·자격 증명·권한·리전 점검 (아무것도 바꾸지 않음)
installer/core/vigie-installer setup --env dev      # 할당량 요청, SSM 비밀 값 등록
installer/core/vigie-installer deploy --env dev     # 사전 확인 후 deploy.sh 실행
installer/core/vigie-installer verify --env dev     # 스택·API 인증·로그·프론트엔드 확인
installer/core/vigie-installer oidc --env dev       # GitHub Actions 자동 배포 설정
installer/core/vigie-installer teardown --env dev   # 환경 삭제 (되돌릴 수 없음)
```

- **각 단계는 이미 되어 있으면 건너뜁니다.** 중간에 실패해도 다시 실행하면 이어서 진행합니다.
- **바꾸기 전에 보여 주고 묻습니다.** 상태를 바꾸는 명령은 실행 전에 그대로 보여 주고 확인을 받습니다. `--dry-run`을 붙이면 아무것도 바꾸지 않고 무엇을 할지만 보여 줍니다.
- **할당량이 먼저입니다.** `llm.yaml`이 통합 타임아웃을 120000ms로 고정하므로, 할당량이 오르기 전에 배포하면 스택 생성이 실패합니다. `deploy`는 할당량이 부족하면 시작하지 않습니다.
- **비밀 값:** 명령 인자로 넘기지 않고(`ps`에 보이지 않도록) 입력받아 SSM에 SecureString으로 올리며, 화면·로그에서는 `***`로 가립니다.
- **정리:** 지울 대상을 먼저 보여 주고 환경 이름을 직접 입력해야 진행합니다. 다른 환경과 함께 쓰는 리소스(GitHub OIDC 공급자, 템플릿 버킷)는 남깁니다.

명령별 동작과 이벤트 형식: [installer/core/README.md](../installer/core/README.md)

### 사전 요구사항
- AWS CLI 설정 및 적절한 권한
- 배포 리전: 기본값은 서울(`ap-northeast-2`)입니다. `AWS_REGION` 환경 변수 → CLI 프로필의 리전 → 서울 순서로 정해지며, 코드에 특정 리전을 고정하지 않습니다.
- Service Quotas -> API Gateway -> Maximum integration timeout in milliseconds -> 120000ms로 변경 요청(자동 승인. 120000ms를 넘는 값은 추가 승인이 필요)
- Node.js 18+ 
- Python 3.12+

### 환경 변수 설정
```bash
# AWS CLI 설정
aws configure
```

### 1단계: 기본 설정
```bash
# 프로젝트 클론
git clone https://github.com/newbiehwang/Vigie.git
cd Vigie

# 환경 값: 루트 .env에 Anthropic API 키를 적는다 (.env는 git에 올라가지 않는다)
cp .env.example .env
# .env를 열어 ANTHROPIC_API_KEY=sk-ant-... 를 적는다.
# deploy.sh가 배포할 때 SSM의 /vigie/<env>/ANTHROPIC_API_KEY(SecureString)로 올린다

# Slack 봇을 쓰는 경우 SSM 파라미터 설정
aws ssm put-parameter --name "/vigie/${Environment}/SlackbotToken" --value "your-slack-token" --type "SecureString"
aws ssm put-parameter --name "/vigie/${Environment}/SlackSigningSecret" --value "your-slack-signing-secret" --type "SecureString"

# .env 없이 배포하는 경우(GitHub Actions만 쓰는 경우 등) Anthropic API 키를 SSM에 직접 등록
aws ssm put-parameter --name "/vigie/${Environment}/ANTHROPIC_API_KEY" --value "your-anthropic-key" --type "SecureString"
```

루트 `.env`(예시는 `.env.example`)에는 두 종류의 값이 들어갑니다.

| 값 | 누가 채우나 | 쓰는 곳 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 직접 적는다 | deploy.sh가 SSM(SecureString)으로 올리고 LLM Lambda가 SSM에서 읽는다. 비워 두면 SSM에 있는 값을 그대로 쓴다(GitHub Actions 배포 등) |
| `VITE_API_DEST`, `AWS_REGION`, `USER_POOL_ID`, `COGNITO_CLIENT_ID`, `COGNITO_DOMAIN` | deploy.sh가 배포할 때 채운다 | 프론트엔드 빌드와 로컬 개발 서버 (`frontend/vite.config.ts`) |

deploy.sh는 키 값을 명령 인자에 넣지 않고 권한 600 임시 파일로 SSM에 올리며, SSM 값과 같으면 올리지 않습니다. 프론트엔드 번들에는 `vite.config.ts`가 고른 값만 들어가고 `ANTHROPIC_API_KEY`는 들어가지 않습니다.

### 2단계: 통합 배포
```bash
# 개발 환경 배포
./deploy.sh dev

# 프로덕션 환경 배포
./deploy.sh prod

# 알람을 이메일로 받으려면 (구독 확인 메일의 링크를 눌러야 활성화됨)
ALARM_EMAIL=you@example.com ./deploy.sh dev

# 관리자 계정을 함께 만들려면 (admins·approvers 그룹. 없으면 만들고 임시 비밀번호가 든 초대 메일을 보냄)
ADMIN_EMAIL=admin@example.com ./deploy.sh dev
```

`ADMIN_EMAIL`·`ALARM_EMAIL`은 저장소 루트 `.env`에 적어 두어도 됩니다(`.env.example` 참고). 명령 앞에 붙인 값이 있으면 그것을 먼저 씁니다.

재배포 동작:
- **코드 버전**: Lambda zip의 S3 키와 MCP 이미지 태그에 git 커밋 SHA를 붙여, 코드를 바꾸면 CloudFormation이 변경을 감지해 새 코드를 배포합니다. 커밋하지 않은 변경이 있으면 `-dirty-<시각>`이 붙습니다.
- **데이터 보존**: 버킷 내용을 지우지 않습니다. 모든 버킷이 `DeletionPolicy: Retain`이라 내용물이 있어도 스택 업데이트·롤백에 영향이 없습니다. 배포 버킷의 오래된 빌드 산출물은 수명 주기 규칙(90일)으로 정리됩니다.
- **변경 없는 스택**: 바뀐 것이 없는 스택은 오류 없이 건너뜁니다.

모든 스택에 `Project=Vigie`, `Environment={env}` 태그가 붙어 하위 리소스까지 전파됩니다. Cost Explorer에서 두 태그를 비용 할당 태그로 활성화하면 프로젝트·환경별 비용을 볼 수 있습니다.

### 3단계: 배포 확인
배포 완료 후 다음 정보가 출력됩니다:
- **프론트엔드 URL**: `https://ENVIRONMENT.xxxxxxxxxxxxx.amplifyapp.com`
- **API Gateway URL**: `https://xxxxxxxxxx.execute-api.AWSREGION.amazonaws.com/ENVIRONMENT`
- **MCP Function URL**: `https://xxxxxxxxxx.lambda-url.AWSREGION.on.aws/`

추가로, SSM Parameter 정보도 제공됩니다.

### 4단계: 관리자 계정
일반 사용자는 로그인 페이지에서 스스로 가입하고, 어느 그룹에도 속하지 않습니다(질문·조회만). 관리자는 배포할 때 `ADMIN_EMAIL`(명령 앞에 붙이거나 `.env`에 적음)로 정합니다.

| 계정 | 만드는 방법 | 할 수 있는 것 |
|:--|:--|:--|
| 일반 사용자 | 로그인 페이지에서 스스로 가입 | 질문·조회(관리자 전용 도구 제외, 10절), 자기 변경 요청 거절 |
| 결정자 | 관리자가 사용자 관리 탭에서 지정 | 위에 더해 변경 작업 승인(`approvers`) |
| 관리자 (`ADMIN_EMAIL`) | `deploy.sh`가 만들거나(초대 메일, 임시 비밀번호 7일) 이미 가입한 계정에 권한을 더함. 다른 관리자는 사용자 관리 탭에서 지정 | 위에 더해 감사 로그·사용자 관리 탭, CloudTrail·IAM·네트워크·S3 보안 조회(`admins`) |

- `deploy.sh`는 User Pool이 생긴 뒤 그 이메일의 사용자가 있는지 보고, 없으면 만들고, 두 그룹에 넣습니다. 사용자를 지우지는 않습니다. 다시 배포해도 그대로이고(이미 들어 있으면 넘어감), 관리자를 바꾸려면 새 이메일로 배포한 뒤 예전 계정은 콘솔에서 그룹을 빼거나 지웁니다.
- CloudFormation으로 만들지 않은 이유: 이미 가입한 이메일이면 스택 전체가 실패하고, 이메일을 바꾸면 CloudFormation이 예전 사용자를 지웁니다.
- 실패해도(권한 부족 등) 배포는 계속하고, 직접 실행할 명령을 알려 줍니다. 결정자·관리자를 더 두려면 관리자로 로그인해 사용자 관리 탭에서 정합니다(명령으로 넣어도 됩니다). User Pool ID는 SSM 파라미터 `/vigie/<env>/UserPoolId`에 있습니다.

```bash
aws cognito-idp admin-add-user-to-group --user-pool-id <UserPoolId> --username <이메일> --group-name approvers
```

## 운영 및 모니터링

### 알람 (`monitoring.yaml`)
모든 알람은 SNS 토픽 `vigie-alarms-{env}`로 발생·해소 알림을 보냅니다.

| 대상 | 지표 | 조건 | 의도 |
|---|---|---|---|
| Lambda 5개 | Errors | 5분 합계 1건 이상 | 함수 오류 즉시 감지 |
| Lambda 5개 | Throttles | 5분 합계 1건 이상 | 동시성 한도 도달 감지 |
| Lambda 5개 | Duration p95 | 함수 Timeout의 80% 초과, 2회 연속 | 타임아웃 임박 감지 (LLM·MCP 144초, Slack 봇 12초 등) |
| API Gateway | 5XXError | 5분 합계 5건 이상 | 백엔드 장애 감지 |
| API Gateway | Latency p95 | 100초 초과, 2회 연속 | 통합 타임아웃(120초) 임박 감지 |
| DynamoDB 4개 | Read + Write ThrottleEvents | 5분 합계 1건 이상 | 프로비저닝 용량(5 RCU/WCU) 부족 감지 |

Lambda 알람은 `Fn::ForEach`(AWS::LanguageExtensions)로 함수 목록과 임계값 매핑만 두고 한 번에 정의했습니다.

### 대시보드와 추적
- **대시보드** `vigie-{env}-service`: API 요청 수·오류·응답 시간, Lambda 호출·오류·실행 시간 p95, DynamoDB 스로틀·소비 용량. 챗봇의 대시보드 조회 도구로도 확인할 수 있습니다.
- **X-Ray**: 모든 Lambda와 API Gateway 스테이지에서 Active 추적을 켜서 API → Lambda → MCP 호출 구간별 지연을 볼 수 있습니다.
- **구조화 로그**: Lambda 로그 형식을 JSON으로 설정했습니다. 플랫폼 `REPORT` 레코드의 `initDurationMs`, `durationMs`, `maxMemoryUsedMB`로 콜드 스타트와 메모리 사용률을 집계합니다.
- **저장 쿼리**: CloudWatch Logs Insights의 `vigie-{env}/lambda-performance`(함수별 콜드 스타트 비율, p50/p95, 최대 메모리)와 `vigie-{env}/cold-start-vs-warm`(콜드/웜 응답 시간 비교).

### 데이터 보호 정책
| 리소스 | 설정 | 이유 |
|---|---|---|
| S3 버킷 7개 | `DeletionPolicy: Retain`, 퍼블릭 액세스 차단 | 스택을 지워도 로그·산출물을 보존하고, 재배포 시 `deploy.sh`가 기존 버킷을 재사용 |
| DynamoDB 테이블 4개 | Point-in-Time Recovery, prod에서 삭제 방지 | 최근 35일 내 임의 시점 복구. 테이블 이름이 고정이라 Retain 대신 삭제 방지로 prod 데이터를 보호 |
| SSM 파라미터 | `DeletionPolicy: Delete` | 스택 출력값에서 파생되는 설정이라 재배포 시 다시 생성됨 |

## 설정 가이드

### Slack 봇 설정
1. Slack 앱 생성 및 봇 토큰 발급
2. SSM Parameter Store에 봇 토큰(`SlackbotToken`)과 Signing Secret(`SlackSigningSecret`) 저장
   - Signing Secret은 Slack 앱의 Basic Information → App Credentials에서 확인합니다.
   - Slack 요청은 이 값으로 서명을 검증하며, 설정되지 않으면 모든 Slack 요청을 거부합니다.
3. Slack 앱에 다음 기능 추가:
   - Interactive Components (로그인 버튼을 누르면 Slack이 보내는 알림을 받습니다)
   - Bot Token Scopes: `chat:write`, `im:write`

## 배포 파이프라인 (`.github/workflows/deploy.yml`)
`main`에 머지되면 dev에 자동 배포하고, prod는 GitHub Environment 승인 후 배포합니다. AWS 인증은 GitHub OIDC로 받은 단기 자격 증명만 사용하며 Access Key를 저장하지 않습니다.

```
main 머지 ──▶ dev 배포 (OIDC Role: vigie-github-deploy-dev) ──▶ 승인 대기 ──▶ prod 배포 (vigie-github-deploy-prod)
```

**처음 한 번 설정** (저장소 변수를 등록하기 전에는 배포 작업이 실행되지 않습니다)
1. 환경별로 OIDC Role 스택을 관리자 권한으로 배포합니다. 계정에 GitHub OIDC 공급자가 이미 있으면 `ExistingOidcProviderArn`에 그 ARN을 넘깁니다.
   ```bash
   aws cloudformation deploy --stack-name vigie-github-oidc-dev \
     --template-file cloudformation/github-oidc.yaml \
     --parameter-overrides Environment=dev \
     --capabilities CAPABILITY_NAMED_IAM
   ```
2. GitHub 저장소 Settings → Environments에서 `dev`, `prod`를 만들고 다음을 설정합니다.
   - **두 Environment 모두** Deployment branches and tags를 `main`만 허용하도록 제한합니다.
     수동 실행(`workflow_dispatch`)은 브랜치를 고를 수 있어서, 제한하지 않으면 리뷰받지 않은 브랜치의 코드도 `environment:dev`로 실행되어 OIDC 신뢰 정책을 통과합니다. Environment 보호 규칙은 워크플로 파일 내용과 관계없이 GitHub가 강제하므로, `main`이 아닌 브랜치에서는 배포 작업이 시작되지 않고 OIDC 토큰도 발급되지 않습니다.
   - `prod`에는 Required reviewers를 지정합니다.
3. Settings → Variables → Actions에 스택 출력값 `DeployRoleArn`을 등록합니다.

| 변수 | 값 |
|---|---|
| `AWS_DEPLOY_ROLE_ARN_DEV` | dev OIDC 스택의 `DeployRoleArn` |
| `AWS_DEPLOY_ROLE_ARN_PROD` | prod OIDC 스택의 `DeployRoleArn` |
| `AWS_REGION` | 배포 리전 (선택, 기본 `ap-northeast-2` 서울) |
| `ALARM_EMAIL` | CloudWatch 알람 수신 이메일 (선택) |
| `ADMIN_EMAIL` | 관리자 계정 이메일 (선택, `admins`·`approvers` 그룹) |

**배포 Role 권한 범위**: `PowerUserAccess`(IAM 제외 전 서비스) + `vigie-*` Role에 한정한 IAM 관리 권한입니다. 관리형 정책은 템플릿에서 쓰는 목록만 연결할 수 있고, 배포 Role 자신은 수정할 수 없습니다. Role 신뢰 정책은 이 저장소의 해당 GitHub Environment에서 실행된 작업만 허용하고(`sub` 조건), Environment의 브랜치 제한과 승인 규칙이 그 작업을 실행할 수 있는 코드와 사람을 제한합니다.
