# Vigie 설치 마법사 — 단계 엔진 (`vigie_installer`)

Vigie를 배포·검증·정리하는 명령줄 도구입니다. `deploy.sh`를 그대로 쓰되, 배포 전후에 필요한 점검과
설정(할당량, SSM 비밀 값, GitHub 자동 배포, 정리)을 단계로 묶었습니다. 각 단계는 **이미 되어 있으면
건너뛰고**, 상태를 바꾸기 전에 실행할 명령을 보여 주고 확인을 받습니다.
Python 표준 라이브러리만 쓰므로 `pip install`이 필요 없습니다 (Python 3.10 이상).

```bash
installer/core/vigie-installer check                     # 사전 점검 (터미널용 출력)
installer/core/vigie-installer check --json              # JSON Lines 출력 (다른 도구에서 읽을 때)
installer/core/vigie-installer check --profile vigie-dev   # 기존 AWS CLI 프로필 사용
installer/core/vigie-installer setup                     # 할당량 요청, SSM 비밀 값 등록
installer/core/vigie-installer deploy --alarm-email me@example.com --admin-email admin@example.com
installer/core/vigie-installer verify                    # 배포 검증
installer/core/vigie-installer oidc --env dev --block-test   # GitHub Actions 자동 배포 설정
installer/core/vigie-installer teardown --env dev            # 정리 (되돌릴 수 없음)
```

| 명령 | 하는 일 | 바꾸는 것 |
|---|---|---|
| `check` | 도구·저장소·자격 증명·**권한**·리전 점검 | 없음 |
| `setup` | ① API Gateway 통합 타임아웃 할당량을 120000ms로 요청(자동 승인되는 최댓값) ② `/vigie/<env>/ANTHROPIC_API_KEY`·`SlackbotToken`·`SlackSigningSecret`을 SecureString으로 등록 | 할당량 요청, SSM 파라미터 |
| `deploy` | 사전 확인(필수 SSM 값 또는 루트 `.env`의 키, 할당량) 후 `./deploy.sh <env>` 실행, 진행 표시, 실패 원인 요약 | AWS 리소스 전체 |
| `verify` | 스택 상태, 인증 없는 API 호출 차단, `/health`, AccessDenied 로그, 프론트엔드, 대시보드 | 없음 |
| `oidc` | 배포 Role 스택(`vigie-github-oidc-<env>`), GitHub Environment(main 브랜치로 제한, prod는 본인 승인), 저장소 변수 등록. 선택: `--test-run`(시험 배포), `--block-test`(다른 브랜치 차단 확인) | IAM Role, GitHub 설정 |
| `teardown` | 한 환경의 스택·ECR·버킷(모든 버전)·로그 그룹·SSM 값·GitHub 변수와 Environment 삭제 | 전부 삭제 |

### 알아 둘 동작
- **권한도 점검합니다.** `sts get-caller-identity`는 정책이 하나도 없어도 성공하므로, 자격 증명만 보면 "통과"인데 다음 단계에서 모든 호출이 거부될 수 있습니다. `check`는 이후 단계가 읽는 API(CloudFormation·SSM·Service Quotas·S3)를 한 번씩 호출해 보고, 배포가 바꾸는 작업(스택·Lambda·IAM Role 생성 등 24개)은 IAM 정책 시뮬레이터(`simulate-principal-policy`)로 허용 여부만 묻습니다. 아무것도 만들지 않습니다. IAM Role은 템플릿이 쓰는 `vigie-*` 이름으로 물어서, `vigie-*`로 좁힌 정책도 통과합니다.
- **저장소 루트 `.env`의 Anthropic API 키를 씁니다.** `.env`에 `ANTHROPIC_API_KEY`를 적어 두면(`.env.example` 참고) `setup`은 키를 묻지 않고 그 값으로 등록합니다. SSM에 이미 있으면 덮어쓸지 묻지 않고 그대로 둡니다. 배포할 때 `deploy.sh`가 SSM 값과 `.env` 값을 비교해 다르면 `.env` 값으로 맞추기 때문입니다. `deploy`는 SSM에 키가 없어도 `.env`에 있으면 멈추지 않습니다(`deploy.sh`가 스택을 배포하기 전에 올립니다). 키 값은 출력에 나오지 않고 앞뒤 몇 글자와 길이만 보입니다. `.env`는 셸로 실행하지 않고 글자로만 읽으며, 읽는 규칙은 `deploy.sh`와 같습니다(`vigie_installer/dotenv.py`).
- **할당량이 먼저입니다.** `cloudformation/llm.yaml`이 통합 타임아웃을 120000ms로 설정하므로 할당량이 오르기 전에는 스택 생성이 실패합니다. `deploy`는 할당량이 부족하면 배포를 시작하지 않고 멈춥니다.
- **Slack 값은 비워 둘 수 있습니다.** 비워 두면 등록하지 않고 건너뜁니다. Signing Secret이 없으면 Slack 요청은 모두 거부됩니다.
- **이미 있는 SSM 값은 읽지 않습니다.** 이름과 형식만 확인하고(`describe-parameters`), 유지할지 덮어쓸지 묻습니다. 기본은 유지입니다.
- **oidc는 dev·prod만** 설정합니다 (`deploy.yml`에 두 환경의 작업만 있음). Role 변수(`AWS_DEPLOY_ROLE_ARN_<ENV>`)는 등록되는 순간부터 main push가 배포를 일으키므로 가장 마지막에 등록합니다.
- **OIDC 공급자는 계정에 하나이고 환경들이 함께 씁니다.** 공급자를 가진 스택에는 기존 ARN을 넘기지 않습니다(넘기면 CloudFormation이 공급자를 지움). teardown은 다른 환경이 쓰는 동안 공급자를 가진 OIDC 스택을 남깁니다.
- **teardown 안전장치:** prod는 `--allow-prod`가 필요합니다. 지울 대상을 먼저 모두 보여 주고, 환경 이름을 직접 입력하는 확인을 한 번만 받습니다(`--yes`로 건너뛸 수 없음). 확인한 뒤에는 단계마다 묻지 않고 끝까지 지우며, 무엇을 지우는지 한 줄씩 출력합니다. 실행할 명령 전체를 미리 보려면 `--dry-run`을 쓰세요. 다른 환경이 남아 있으면 공유 버킷 `vigie-cloudformation-<계정ID>`는 남깁니다.
- **취소:** `deploy` 도중 Ctrl+C(또는 SIGTERM)를 받으면 deploy.sh와 그 자식 프로세스 전체에 중단 신호를 보내고, 최대 60초 기다린 뒤 강제 종료합니다. 스택이 업데이트 도중 상태로 남을 수 있습니다.

## 실행기와 Python 선택

`vigie-installer`는 알맞은 Python을 골라 `python -m vigie_installer`를 실행하는 bash 스크립트입니다.
macOS 기본 `/usr/bin/python3`는 3.9라서 이 도구를 실행할 수 없고, Command Line Tools가 없으면
실행하는 순간 설치 창이 뜨기 때문에 찾는 순서를 이 스크립트 한곳에 정해 두었습니다.

1. `VIGIE_PYTHON` 환경 변수 (직접 지정. 3.10 미만이면 다른 후보로 넘어가지 않고 오류)
2. `/opt/homebrew/bin/python3` (Apple Silicon Homebrew)
3. `/usr/local/bin/python3` (Intel Homebrew, python.org 설치본)
4. `PATH`의 `python3`
5. `/usr/bin/python3` (macOS에서는 Command Line Tools가 있을 때만)

쓸 수 있는 Python이 없거나 `python -m vigie_installer`를 3.10 미만으로 직접 실행하면, 설치 방법을 담은 오류를 출력하고 종료 코드 3으로 끝납니다.

## 터미널 출력

```
사전 설정 (dev, ap-southeast-2)
  [완료] API Gateway 통합 타임아웃 할당량
  [오류] SSM 파라미터
         /vigie/dev/ANTHROPIC_API_KEY을(를) 저장하지 못했습니다
         An error occurred (AccessDeniedException) when calling the PutParameter operation: ...
✗ 사전 설정을 끝내지 못했습니다. 원인을 해결하고 다시 실행하면 이어서 진행합니다
```

- 하위 단계는 한 줄로 끝나고, 결과가 점검 항목처럼 왼쪽에 붙습니다: `[완료]` · `[예정]`(dry-run에서 바꿀 일) · `[건너뜀]` · `[오류]`
- 이미 되어 있는 단계도 `[완료]`입니다. 할 말이 있을 때만 아랫줄에 씁니다
- 오류는 `[오류]` 아랫줄에 무엇이 실패했는지, 그 아랫줄에 **명령이 낸 오류 원문**, 그 아래 해결 안내(`→`)를 씁니다
- 도중에 질문이나 로그가 나오는 단계는 제목을 먼저 쓰고, 끝날 때 결과 줄을 다시 씁니다
- `--dry-run`은 명령 제목 옆에 한 번만 알립니다
- 마지막 줄: `✓` 성공 · `·` 건너뜀 · `✗` 실패

## 공통 옵션

| 옵션 | 설명 |
|---|---|
| `--json` | 이벤트를 JSON Lines로 출력 (다른 프로그램이 읽을 때) |
| `--dry-run` | 상태 확인만 하고, 변경 명령은 보여 주기만 함 |
| `--yes` | 변경 작업을 묻지 않고 승인 (되돌릴 수 없는 삭제에는 적용되지 않음) |
| `--env dev\|test\|prod` | 배포 환경 (기본 `dev`) |
| `--region` | AWS 리전. 없으면 `AWS_REGION` → CLI 프로필의 region → `ap-northeast-2` (deploy.sh와 같은 순서) |
| `--profile` | AWS CLI 프로필. 지정하면 환경 변수의 `AWS_ACCESS_KEY_ID` 등은 자식 명령에 넘기지 않음 |
| `--repo` | 저장소 경로. 없으면 현재 폴더부터 상위로 `deploy.sh`와 `cloudformation/`을 찾음 |
| `--alarm-email` | (`deploy`·`oidc`) CloudWatch 알람을 받을 이메일. 없으면 저장소 루트 `.env`의 `ALARM_EMAIL`. `oidc`에서는 저장소 변수 `ALARM_EMAIL`로 등록 |
| `--admin-email` | (`deploy`·`oidc`) 관리자 계정 이메일. 배포할 때 이 사용자를 `admins`·`approvers` 그룹에 넣고, 없으면 만들어 초대 메일을 보낸다. 없으면 저장소 루트 `.env`의 `ADMIN_EMAIL`. `oidc`에서는 저장소 변수 `ADMIN_EMAIL`로 등록 |
| `--github-repo` | (`oidc`·`teardown`) GitHub 저장소 owner/repo. 없으면 저장소 폴더의 git remote로 알아냄 |
| `--test-run`, `--block-test` | (`oidc`) 시험 배포 실행 / main 외 브랜치 배포가 막히는지 확인 |
| `--allow-prod` | (`teardown`) prod 삭제 허용 |

## 다른 프로그램과 주고받는 형식 (`--json`)

**CLI → 호출한 프로그램 (stdout, 한 줄에 JSON 하나)**

```json
{"type": "step_started", "step": "check", "title": "사전 점검"}
{"type": "check", "id": "aws_cli", "title": "AWS CLI", "status": "ok", "detail": "2.17.0"}
{"type": "check", "id": "node", "title": "Node.js", "status": "fail", "detail": "16.20.2 (필요: 18.0.0 이상)", "hint": "brew upgrade node"}
{"type": "confirm_required", "id": "put_ssm", "command": "aws ssm put-parameter ...", "reason": "SSM에 값을 저장합니다"}
{"type": "input_required", "id": "secret_ANTHROPIC_API_KEY", "prompt": "Anthropic API 키", "secret": true}
{"type": "choice_required", "id": "existing_SlackbotToken", "prompt": "/vigie/dev/SlackbotToken이(가) 이미 있습니다", "options": [{"id": "keep", "label": "기존 값 유지"}, {"id": "overwrite", "label": "새 값으로 덮어쓰기"}], "default": "keep"}
{"type": "progress", "step": "deploy", "phase": "3/6", "label": "Layer 및 Lambda 함수 패키징"}
{"type": "dry_run", "id": "put_ssm", "command": "aws ssm put-parameter ...", "reason": "SSM에 값을 저장합니다"}
{"type": "log", "stream": "stdout", "line": "..."}
{"type": "step_finished", "step": "check", "status": "ok", "summary": "통과 14개 · 주의 1개 · 실패 0개"}
{"type": "error", "step": "setup", "message": "할당량을 조회하지 못했습니다", "raw": "An error occurred (AccessDeniedException) ...", "hint": "..."}
```

- `check.status`: `ok` 통과 / `warn` 진행 가능하지만 확인 필요 / `fail` 진행 불가 / `info` 정보
- `check.url`: 있으면 사람이 열어 볼 주소 (프론트엔드, CloudWatch 대시보드 등)
- `check.raw`·`error.raw`: 실패한 명령이 낸 **오류 원문** (AWS CLI·gh·CloudFormation이 남긴 그대로). `detail`·`message`는 무엇이 실패했는지 붙인 설명입니다
- `progress.phase`: deploy.sh의 번호 단계 `N/6`. 번호 없는 구분 줄(예: Layer 패키징)은 직전 번호를 유지하고 `label`만 바뀝니다
- `step_finished.status`: `ok`(끝냄. 이미 되어 있던 경우 포함) / `skipped`(하지 않음. 사용자가 거절한 경우 등) / `failed`
- `step_finished.summary`: 할 말이 없으면 빈 문자열입니다 (이미 되어 있는 단계 등)
- `log.stream`: `stdout`·`stderr`(실행한 명령의 출력) / `info`(설치 마법사의 안내)
- 사용자가 입력한 비밀 값은 어떤 이벤트에도 그대로 나오지 않고 `***`로 가려집니다.

**호출한 프로그램 → CLI (stdin, 한 줄에 JSON 하나)**

```json
{"type": "confirm_response", "id": "put_ssm", "approved": true}
{"type": "secret_response", "id": "secret_ANTHROPIC_API_KEY", "value": "..."}
{"type": "choice_response", "id": "existing_SlackbotToken", "choice": "keep"}
{"type": "text_response", "id": "confirm_env", "value": "dev"}
```

질문(`confirm_required`·`input_required`·`choice_required`)은 하나씩 순서대로 나오므로 받은 순서대로 한 줄씩 답합니다. 여러 명령을 한 번에 승인받거나 보여 줄 때(`confirm_required`·`dry_run`) `command`에는 명령이 줄바꿈으로 이어져 있습니다.

응답이 없거나(stdin 닫힘) 형식이 틀리거나 `id`가 다르거나 `approved`가 정확히 `true`가 아니면 **거절**로 처리합니다.
선택(`choice_response`)은 응답이 없거나 선택지에 없는 값이면 `default`를 씁니다. 기본값은 항상 아무것도 바꾸지 않는 쪽입니다.

## 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 성공 |
| 1 | 단계 실패 (자세한 내용은 `check`·`error` 이벤트) |
| 2 | 명령줄 사용법 오류 |
| 3 | 쓸 수 있는 Python(3.10 이상)이 없음 |
| 130 | Ctrl+C로 중단 |

## 테스트

AWS·GitHub 없이 가짜 `aws`·`gh` 등으로 테스트합니다 (`tests/installer/`).

```bash
pytest tests/installer
```

## 앞으로

- **테스트 모드:** 지금은 가짜 `aws`·`gh` 실행 파일로 테스트합니다. 실제 `aws` CLI를 로컬 목(moto)에 붙여 전 과정을 돌리는 모드를 검토 중입니다.
- **Terraform:** `deploy` 단계는 `deploy.sh`를 실행하는 일만 `steps/deploy.py`에 모아 두었습니다. 나중에 `terraform plan/apply`로 바꿔도 나머지 단계(점검·비밀 값·검증·GitHub 자동 배포·정리)는 그대로 쓸 수 있습니다.
