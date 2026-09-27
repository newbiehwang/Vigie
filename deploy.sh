#!/bin/bash
# Vigie - 통합 배포 스크립트 (백엔드 + 프론트엔드)

# 오류 발생 시 스크립트 중단
set -e

# 환경 변수 설정
ENV=${1:-dev}  # 기본값: dev

# 저장소 루트 .env (아래 '루트 .env' 참고)
ROOT_ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.env"

# .env에서 값 하나를 읽는다 ($1: 키). 셸로 source하지 않고 글자로만 읽는다.
# 규칙은 설치 도구(installer/core/vigie_installer/dotenv.py)·sync_anthropic_key와 같다: 앞뒤 공백과 감싼 따옴표를 벗기고,
# 같은 키가 여러 번 나오면 마지막 줄을 쓴다. 파일·키가 없거나 읽지 못하면 빈 글자.
# 값이 셸 변수로 들어오므로 비밀이 아닌 값에만 쓴다 (ANTHROPIC_API_KEY는 sync_anthropic_key가 따로 다룬다)
dotenv_get() {
    [ -f "$ROOT_ENV_FILE" ] || return 0
    python3 - "$ROOT_ENV_FILE" "$1" <<'PY'
import sys
path, wanted = sys.argv[1:3]
value = ""
try:
    with open(path, encoding="utf-8") as f:
        for line in f:
            name, sep, rest = line.strip().partition("=")
            if sep and name.strip() == wanted:
                value = rest.strip().strip('"').strip("'")
except (OSError, UnicodeDecodeError):
    value = ""
print(value)
PY
}

# 이메일은 명령 앞의 환경 변수가 먼저이고, 없으면 .env 값을 쓴다 (GitHub Actions는 .env 없이 저장소 변수로 넘긴다)
# CloudWatch 알람 수신 이메일 (선택, 예: ALARM_EMAIL=me@example.com ./deploy.sh dev 또는 .env의 ALARM_EMAIL=)
ALARM_EMAIL=${ALARM_EMAIL:-$(dotenv_get ALARM_EMAIL)}
# 관리자 계정 이메일 (선택). 이 이메일의 사용자를 admins·approvers 그룹에 넣는다. 없으면 만들고 초대 메일을 보낸다
# (예: ADMIN_EMAIL=admin@example.com ./deploy.sh dev 또는 .env의 ADMIN_EMAIL=). 일반 사용자는 스스로 가입한다
ADMIN_EMAIL=${ADMIN_EMAIL:-$(dotenv_get ADMIN_EMAIL)}
ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text)
# 리전 우선순위: AWS_REGION 환경 변수(CI의 OIDC 포함) → CLI 프로필 설정 → 기본값 서울(ap-northeast-2)
REGION=${AWS_REGION:-$(aws configure get region || true)}
REGION=${REGION:-ap-northeast-2}
export AWS_REGION=$REGION AWS_DEFAULT_REGION=$REGION
MCP_IMAGE_URI="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/vigie-mcp-$ENV:latest"

# ENV 값 검증
if [[ "$ENV" != "dev" && "$ENV" != "test" && "$ENV" != "prod" ]]; then
  echo "❌ 오류: ENV 값은 'dev', 'test', 'prod' 중 하나여야 합니다. 현재 값: '$ENV'"
  exit 1
fi

# 관리자 이메일 검증: 잘못 적었으면 아무것도 바꾸기 전에 멈춘다
if [ -n "$ADMIN_EMAIL" ] && ! [[ "$ADMIN_EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; then
  echo "❌ 오류: ADMIN_EMAIL이 이메일 모양이 아닙니다. 현재 값: '$ADMIN_EMAIL'"
  exit 1
fi

echo "========================================"
echo "통합 배포 시작 - 환경: $ENV"
echo "AWS 계정 ID: $ACCOUNT_ID"
echo "AWS 리전: $REGION"
echo "========================================"

# 배포 버킷 이름
CLOUDFORMATION_BUCKET="vigie-cloudformation-$ACCOUNT_ID"
DEPLOYMENT_BUCKET="vigie-deployment-$ACCOUNT_ID-$ENV"
FRONTEND_BUCKET="vigie-frontend-$ACCOUNT_ID-$ENV"
OUTPUT_BUCKET_NAME="vigie-outputbucket-$ACCOUNT_ID-$ENV"
ATHENA_OUTPUT_BUCKET_NAME="vigie-athenaoutputbucket-$ACCOUNT_ID-$ENV"
GUARDDUTY_EXPORT_BUCKET_NAME="vigie-guarddutyexportbucket-$ACCOUNT_ID-$ENV"
DOCKER_BUILD_BUCKET_NAME="vigie-dockerbuildbucket-$ACCOUNT_ID-$ENV"
DIAGRAM_BUCKET_NAME="vigie-diagrambucket-$ACCOUNT_ID-$ENV"

# 스택 이름 설정
BASE_STACK_NAME="vigie-base-$ENV"
MAIN_STACK_NAME="vigie-$ENV"
FRONTEND_STACK_NAME="vigie-frontend-$ENV"
MCP_STACK_NAME="vigie-mcp-$ENV"

# 도메인 설정 (필요한 경우 수정)
DOMAIN_NAME=""  # 사용자 정의 도메인
CERTIFICATE_ARN=""  # 사용자 정의 도메인에 필요한 ACM 인증서 ARN
if [ "$ENV" = "dev" ] || [ "$ENV" = "test" ]; then
  DEVELOPER_MODE=true
else
  DEVELOPER_MODE=false
fi

# SSM 파라미터 경로 기본 prefix 설정
SSM_PATH_PREFIX="/vigie/$ENV"

# 코드 버전: Lambda zip의 S3 키와 MCP 이미지 태그에 붙인다.
# 키가 매번 같으면 CloudFormation이 변경을 감지하지 못해 새 코드가 반영되지 않으므로 배포마다 달라져야 한다.
if GIT_SHA=$(git rev-parse --short=12 HEAD 2>/dev/null); then
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        CODE_VERSION="${GIT_SHA}-dirty-$(date +%Y%m%d%H%M%S)"   # 커밋하지 않은 변경도 새 버전으로 배포
    else
        CODE_VERSION="$GIT_SHA"
    fi
else
    CODE_VERSION="build-$(date +%Y%m%d%H%M%S)"
fi
echo "코드 버전: $CODE_VERSION"

# 스택 업데이트 후 완료까지 대기한다. 변경 사항이 없으면 CloudFormation이 오류를 반환하는데,
# set -e 때문에 배포가 중단되지 않도록 정상으로 처리한다.
cfn_update() {
    local stack=$1; shift
    local out
    if out=$(aws cloudformation update-stack --stack-name "$stack" "$@" 2>&1); then
        echo "스택 업데이트 완료 대기 중: $stack"
        aws cloudformation wait stack-update-complete --stack-name "$stack"
    elif grep -q "No updates are to be performed" <<< "$out"; then
        echo "변경 사항 없음: $stack (업데이트 건너뜀)"
    else
        echo "$out" >&2
        return 1
    fi
}

# 인증 메일 Lambda(services/auth_messages)를 묶어 배포 버킷에 올리고, 올린 코드 버전을 AUTH_MESSAGES_VERSION에 둔다.
# base 스택이 이 버전의 코드로 Lambda를 만들어 Cognito에 연결한다 (base.yaml의 CodeVersion. 비어 있으면 Lambda를 만들지 않는다). 관리자 초대 메일보다 먼저
# 연결되어야 하므로 패키징 단계(뒤쪽)가 아니라 base 스택을 업데이트하기 직전에 올린다. 배포 버킷이 없으면(처음 만들 때) 건너뛴다
upload_auth_messages() {
    AUTH_MESSAGES_VERSION=""
    if ! aws s3 ls "s3://$DEPLOYMENT_BUCKET" > /dev/null 2>&1; then
        echo "배포 버킷이 아직 없어 인증 메일 Lambda는 다음 업데이트에서 올립니다."
        return 0
    fi
    rm -rf build/auth-messages && mkdir -p build/auth-messages
    (cd services/auth_messages && zip -qr "../../build/auth-messages/auth-messages.zip" . -x '__pycache__/*')
    aws s3 cp build/auth-messages/auth-messages.zip "s3://$DEPLOYMENT_BUCKET/auth-messages/auth-messages-lambda-$ENV-$CODE_VERSION.zip"
    AUTH_MESSAGES_VERSION="$CODE_VERSION"
}

# MCP Lambda를 한 번 불러 공식 MCP 서버를 미리 불러 둔다 (배포 직후 첫 질문이 콜드 스타트를 기다리지 않게).
# LLM Lambda가 질문마다 하는 것과 같은 순서로 부른다: initialize → tools/list(여기서 공식 서버를 불러온다) → 세션 삭제.
# - 효과는 배포 직후 잠깐이다. Lambda는 한동안 요청이 없으면 실행 환경을 정리하고, 동시에 온 요청은 새 환경에서 돈다.
# - 미리 깨우지 못해도 서비스에는 문제가 없으므로(첫 질문이 느릴 뿐) 실패해도 배포는 계속한다.
# 관리자 계정 ($1: User Pool ID, $2: 이메일). 이메일이 없으면 건너뛴다.
# - 사용자가 없으면 만든다: Cognito가 임시 비밀번호가 든 초대 메일을 보내고, 처음 로그인할 때 새 비밀번호를 정한다
# - 이미 있으면(스스로 가입한 경우 등) 그대로 두고 그룹만 넣는다. 어떤 경우에도 사용자를 지우지 않는다
# - admins(모든 사람의 감사 로그), approvers(AI가 요청한 변경 작업 승인) 두 그룹에 넣는다. 이미 들어 있으면 그대로다
# - CloudFormation으로 만들지 않는다: 이미 가입한 이메일이면 스택 전체가 실패하고, 이메일을 바꾸면 사용자를 지운다
# 실패해도 배포는 계속한다 (인프라는 이미 배포됐다). 대신 직접 실행할 명령을 알려 준다
ensure_admin_account() {
    local pool="$1" email="$2" error
    if [ -z "$email" ]; then
        echo "관리자 이메일(ADMIN_EMAIL)이 없어 관리자 계정은 건너뜁니다."
        echo "  변경 작업을 승인할 사람이 없습니다. ADMIN_EMAIL=<이메일>로 다시 배포하거나 직접 그룹에 넣으세요:"
        echo "  aws cognito-idp admin-add-user-to-group --user-pool-id $pool --username <이메일> --group-name approvers"
        return 0
    fi
    if error=$(aws cognito-idp admin-get-user --user-pool-id "$pool" --username "$email" 2>&1 >/dev/null); then
        echo "관리자 계정($email)이 이미 있습니다. 그룹만 확인합니다."
    elif [[ "$error" == *UserNotFoundException* ]]; then
        if ! error=$(aws cognito-idp admin-create-user --user-pool-id "$pool" --username "$email" \
                --user-attributes Name=email,Value="$email" Name=email_verified,Value=true \
                --desired-delivery-mediums EMAIL 2>&1 >/dev/null); then
            echo "⚠️ 관리자 계정($email)을 만들지 못했습니다 (배포는 계속합니다): $error" >&2
            return 0
        fi
        echo "✅ 관리자 계정($email)을 만들었습니다. 임시 비밀번호가 든 초대 메일을 확인하세요 (7일 동안 유효)."
    else
        echo "⚠️ 관리자 계정($email)을 확인하지 못했습니다 (배포는 계속합니다): $error" >&2
        return 0
    fi
    local group
    for group in admins approvers; do
        if ! error=$(aws cognito-idp admin-add-user-to-group --user-pool-id "$pool" --username "$email" \
                --group-name "$group" 2>&1 >/dev/null); then
            echo "⚠️ 관리자 계정($email)을 $group 그룹에 넣지 못했습니다 (배포는 계속합니다): $error" >&2
            return 0
        fi
    done
    echo "✅ 관리자 계정($email): admins·approvers 그룹"
}

warm_up_mcp() {
    local function_name="$1"
    local response session_id tool_count started
    response=$(mktemp)
    started=$(date +%s)

    # Function URL로 들어오는 요청과 같은 모양의 이벤트 ($1: JSON-RPC 메서드, $2: 세션 ID, $3: HTTP 메서드)
    mcp_event() {
        python3 - "$@" <<'PY'
import json, sys
method, session, http_method = sys.argv[1], sys.argv[2], sys.argv[3]
headers = {"content-type": "application/json"}
if session:
    headers["mcp-session-id"] = session
body = json.dumps({"jsonrpc": "2.0", "id": "warm-up", "method": method, "params": {}})
print(json.dumps({"httpMethod": http_method, "headers": headers, "body": body}))
PY
    }

    # 응답에서 값 하나를 꺼낸다 ($1: session | tools). 응답이 기대와 다르면 빈 값
    mcp_read() {
        python3 - "$1" "$response" <<'PY'
import json, sys
what, path = sys.argv[1], sys.argv[2]
try:
    response = json.load(open(path))
    if response.get("statusCode") != 200:
        raise ValueError(response)
    if what == "session":
        print(response["headers"]["MCP-Session-Id"])
    else:
        print(len(json.loads(response["body"])["result"]["tools"]))
except Exception:
    print("")
PY
    }

    mcp_invoke() {
        # 콜드 스타트에서 공식 서버를 불러오는 데 수십 초 걸릴 수 있어 응답 대기 시간을 넉넉히 둔다
        aws lambda invoke --function-name "$function_name" \
            --cli-binary-format raw-in-base64-out --cli-read-timeout 300 \
            --payload "$(mcp_event "$@")" "$response" > /dev/null
    }

    echo "MCP Lambda 미리 깨우는 중: $function_name"
    # 이미지가 바뀐 직후면 새 이미지로 바뀔 때까지 기다린다
    aws lambda wait function-updated-v2 --function-name "$function_name" || true

    if mcp_invoke initialize "" POST && session_id=$(mcp_read session) && [ -n "$session_id" ] \
        && mcp_invoke tools/list "$session_id" POST && tool_count=$(mcp_read tools) && [ -n "$tool_count" ]; then
        mcp_invoke "" "$session_id" DELETE || true  # 세션 기록은 남기지 않는다
        echo "✅ MCP Lambda 준비 완료: 도구 ${tool_count}개, $(( $(date +%s) - started ))초"
    else
        echo "⚠️ MCP Lambda를 미리 깨우지 못했습니다. 배포는 계속하며, 첫 질문이 조금 느릴 수 있습니다."
    fi
    rm -f "$response"
}

# ---------------------------------------------------------------- 루트 .env
# 저장소 루트의 .env 하나에 환경 값을 모은다 (.env.example 참고, git에는 올리지 않는다).
# - 직접 적는 값: ANTHROPIC_API_KEY. 배포할 때 SSM Parameter Store(SecureString)로 올린다 (Lambda는 SSM에서 읽는다)
# - deploy.sh가 채우는 값: 프론트엔드 빌드에 들어가는 API 주소·Cognito 설정 (frontend/vite.config.ts가 읽는다)
# - 직접 적는 값(선택): ALARM_EMAIL, ADMIN_EMAIL. 명령 앞의 환경 변수가 없을 때 쓴다 (맨 위 dotenv_get)
# .env는 셸로 source하지 않는다: 값 안의 글자가 명령으로 실행될 수 있고, 비밀 값이 셸 변수로 퍼진다.
# (ROOT_ENV_FILE은 이메일을 읽으려고 맨 위에서 정한다)

# .env의 KEY=VALUE 한 줄을 바꾸거나(없으면 끝에 더한다) 다른 줄은 그대로 둔다 ($1: 파일, $2: 키, $3: 값).
# 직접 적어 둔 ANTHROPIC_API_KEY와 주석을 지우지 않으려고 파일을 새로 쓰지 않고 줄 단위로 고친다
dotenv_set() {
    python3 - "$1" "$2" "$3" <<'PY'
import sys
path, key, value = sys.argv[1:4]
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()
line = f"{key}={value}"
for i, current in enumerate(lines):
    if current.split("=", 1)[0].strip() == key:
        lines[i] = line
        break
else:
    lines.append(line)
with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
PY
}

# .env의 ANTHROPIC_API_KEY를 SSM의 $SSM_PATH_PREFIX/ANTHROPIC_API_KEY(SecureString)로 올린다.
# - 키 값은 셸 변수·명령 인자에 올리지 않는다(ps로 보인다). python이 .env에서 읽어 권한 600 임시 파일에 요청을 쓰고,
#   aws CLI는 그 파일을 읽는다 (installer와 같은 방식: --cli-input-json file://)
# - SSM 값과 같으면 올리지 않는다 (배포마다 새 버전이 쌓이지 않게)
# - .env가 없거나 키가 비어 있으면(GitHub Actions 배포 등) SSM에 있는 값을 그대로 쓴다
sync_anthropic_key() {
    local name="$SSM_PATH_PREFIX/ANTHROPIC_API_KEY"
    local dir status
    if [ ! -f "$ROOT_ENV_FILE" ]; then
        echo "루트 .env가 없어 SSM의 Anthropic API 키를 그대로 씁니다."
        return 0
    fi
    dir=$(mktemp -d)
    chmod 700 "$dir"
    # 지금 SSM 값 (없으면 빈 파일). 화면에 찍지 않고 파일로만 받는다
    aws ssm get-parameter --name "$name" --with-decryption --output json > "$dir/current.json" 2>/dev/null || : > "$dir/current.json"
    status=0
    python3 - "$ROOT_ENV_FILE" "$name" "$dir" <<'PY' || status=$?
import json, os, sys
env_path, name, work = sys.argv[1:4]
value = ""
with open(env_path, encoding="utf-8") as f:
    for line in f:
        key, sep, rest = line.strip().partition("=")
        if sep and key.strip() == "ANTHROPIC_API_KEY":
            value = rest.strip().strip('"').strip("'")
if not value:
    sys.exit(3)  # .env에 키가 없다
try:
    with open(os.path.join(work, "current.json"), encoding="utf-8") as f:
        current = json.load(f)["Parameter"]
    if current["Value"] == value and current["Type"] == "SecureString":
        sys.exit(4)  # SSM 값과 같다
except (ValueError, KeyError):
    pass  # SSM에 아직 없다
request = os.path.join(work, "request.json")
fd = os.open(request, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump({"Name": name, "Value": value, "Type": "SecureString", "Overwrite": True}, f)
PY
    case $status in
        0)
            if aws ssm put-parameter --cli-input-json "file://$dir/request.json" > /dev/null; then
                echo "✅ .env의 Anthropic API 키를 SSM($name)에 올렸습니다."
            else
                rm -rf "$dir"
                echo "❌ Anthropic API 키를 SSM에 올리지 못했습니다." >&2
                return 1
            fi ;;
        3) echo "루트 .env에 ANTHROPIC_API_KEY가 없어 SSM의 값을 그대로 씁니다." ;;
        4) echo "Anthropic API 키: SSM 값과 같아 그대로 둡니다." ;;
        *) rm -rf "$dir"; echo "❌ 루트 .env를 읽지 못했습니다." >&2; return 1 ;;
    esac
    rm -rf "$dir"
}

# Anthropic API 키: .env에 적은 값을 SSM으로 올린다 (Lambda가 SSM에서 읽는다)
sync_anthropic_key

#################################################
# 1. CloudFormation 버킷 확인 및 템플릿 업로드
#################################################
echo "CloudFormation 템플릿을 저장할 $CLOUDFORMATION_BUCKET 버킷 확인 중..."
if ! aws s3 ls "s3://$CLOUDFORMATION_BUCKET" 2>&1 > /dev/null; then
    echo "$CLOUDFORMATION_BUCKET 버킷이 존재하지 않습니다. 생성합니다..."
    aws s3 mb "s3://$CLOUDFORMATION_BUCKET" --region $REGION
    aws s3api put-bucket-versioning --bucket "$CLOUDFORMATION_BUCKET" --versioning-configuration Status=Enabled
    echo "$CLOUDFORMATION_BUCKET 버킷 생성 완료"
else
    echo "$CLOUDFORMATION_BUCKET 버킷이 이미 존재합니다"
fi

echo "====== 1. CloudFormation 템플릿 업로드 ======"
# 배포 버킷이 존재하지 않을 수 있으므로 base 스택을 먼저 배포
echo "CloudFormation 템플릿 업로드 중..."
aws s3 cp cloudformation/base.yaml "s3://$CLOUDFORMATION_BUCKET/base.yaml"
aws s3 cp cloudformation/llm.yaml "s3://$CLOUDFORMATION_BUCKET/llm.yaml"
aws s3 cp cloudformation/main.yaml "s3://$CLOUDFORMATION_BUCKET/main.yaml"
aws s3 cp cloudformation/frontend.yaml "s3://$CLOUDFORMATION_BUCKET/frontend.yaml"
aws s3 cp cloudformation/logs.yaml "s3://$CLOUDFORMATION_BUCKET/logs.yaml"
aws s3 cp cloudformation/slackbot.yaml "s3://$CLOUDFORMATION_BUCKET/slackbot.yaml"
aws s3 cp cloudformation/mcp.yaml "s3://$CLOUDFORMATION_BUCKET/mcp.yaml"
aws s3 cp cloudformation/chat-history.yaml "s3://$CLOUDFORMATION_BUCKET/chat-history.yaml"
aws s3 cp cloudformation/monitoring.yaml "s3://$CLOUDFORMATION_BUCKET/monitoring.yaml"

echo "CloudFormation 템플릿 업로드 완료"

#################################################
# 2. 기본 스택 배포
#################################################
echo "====== 2. 기본 스택 배포 시작 ======"

# 기본 스택 배포 전 S3 버킷 존재 여부 확인
if aws s3 ls "s3://$DEPLOYMENT_BUCKET" > /dev/null 2>&1; then
    echo "배포 버킷($DEPLOYMENT_BUCKET)이 이미 존재합니다. 이 버킷을 재사용합니다."
    BUCKET_EXISTS="true"
else
    echo "배포 버킷($DEPLOYMENT_BUCKET)이 존재하지 않습니다. 새로 생성합니다."
    BUCKET_EXISTS="false"
fi

# OutputBucket 존재 여부 확인
if aws s3 ls "s3://$OUTPUT_BUCKET_NAME" > /dev/null 2>&1; then
    echo "출력 버킷($OUTPUT_BUCKET_NAME)이 이미 존재합니다. 이 버킷을 재사용합니다."
    OUTPUT_BUCKET_EXISTS="true"
else
    echo "출력 버킷($OUTPUT_BUCKET_NAME)이 존재하지 않습니다. 새로 생성합니다."
    OUTPUT_BUCKET_EXISTS="false"
fi

# AthenaOutputBucket 존재 여부 확인
if aws s3 ls "s3://$ATHENA_OUTPUT_BUCKET_NAME" > /dev/null 2>&1; then
    echo "출력 버킷($ATHENA_OUTPUT_BUCKET_NAME)이 이미 존재합니다. 이 버킷을 재사용합니다."
    ATHENA_OUTPUT_BUCKET_EXISTS="true"
else
    echo "출력 버킷($ATHENA_OUTPUT_BUCKET_NAME)이 존재하지 않습니다. 새로 생성합니다."
    ATHENA_OUTPUT_BUCKET_EXISTS="false"
fi

# GuardDutyExportBucket 존재 여부 확인
if aws s3 ls "s3://$GUARDDUTY_EXPORT_BUCKET_NAME" > /dev/null 2>&1; then
    echo "출력 버킷($GUARDDUTY_EXPORT_BUCKET_NAME)이 이미 존재합니다. 이 버킷을 재사용합니다."
    GUARDDUTY_EXPORT_BUCKET_EXISTS="true"
else
    echo "출력 버킷($GUARDDUTY_EXPORT_BUCKET_NAME)이 존재하지 않습니다. 새로 생성합니다."
    GUARDDUTY_EXPORT_BUCKET_EXISTS="false"
fi

# DockerBuildBucket 존재 여부 확인
if aws s3 ls "s3://$DOCKER_BUILD_BUCKET_NAME" > /dev/null 2>&1; then
    echo "출력 버킷($DOCKER_BUILD_BUCKET_NAME)이 이미 존재합니다. 이 버킷을 재사용합니다."
    DOCKER_BUILD_BUCKET_EXISTS="true"
else
    echo "출력 버킷($DOCKER_BUILD_BUCKET_NAME)이 존재하지 않습니다. 새로 생성합니다."
    DOCKER_BUILD_BUCKET_EXISTS="false"
fi

# 프론트엔드 버킷 존재 여부 확인
if aws s3 ls "s3://$FRONTEND_BUCKET" > /dev/null 2>&1; then
    echo "출력 버킷($FRONTEND_BUCKET)이 이미 존재합니다. 이 버킷을 재사용합니다."
    FRONTEND_BUCKET_EXISTS="true"
else
    echo "출력 버킷($FRONTEND_BUCKET)이 존재하지 않습니다. 새로 생성합니다."
    FRONTEND_BUCKET_EXISTS="false"
fi

# DiagramBucket 존재 여부 확인
if aws s3 ls "s3://$DIAGRAM_BUCKET_NAME" > /dev/null 2>&1; then
    echo "다이어그램 버킷($DIAGRAM_BUCKET_NAME)이 이미 존재합니다. 이 버킷을 재사용합니다."
    DIAGRAM_BUCKET_EXISTS="true"
else
    echo "다이어그램 버킷($DIAGRAM_BUCKET_NAME)이 존재하지 않습니다. 새로 생성합니다."
    DIAGRAM_BUCKET_EXISTS="false"
fi

# 기본 스택 배포
echo "기본 인프라 스택 배포 중: $BASE_STACK_NAME..."

if aws cloudformation describe-stacks --stack-name "$BASE_STACK_NAME" > /dev/null 2>&1; then
    # 스택이 존재하면 업데이트
    echo "기존 스택 업데이트 중: $BASE_STACK_NAME"
    upload_auth_messages
    # 도메인·MCP URL은 이후 단계에서 실제 값으로 갱신되므로 여기서는 기존 값을 유지한다
    # (placeholder를 넣으면 배포 도중 Cognito 콜백 URL이 잠시 잘못된 값이 된다)
    cfn_update $BASE_STACK_NAME \
        --template-url "https://$CLOUDFORMATION_BUCKET.s3.$REGION.amazonaws.com/base.yaml" \
        --parameters ParameterKey=Environment,ParameterValue=$ENV \
                    ParameterKey=BucketExists,ParameterValue=$BUCKET_EXISTS \
                    ParameterKey=OutputBucketExists,ParameterValue=$OUTPUT_BUCKET_EXISTS \
                    ParameterKey=AthenaOutputBucketExists,ParameterValue=$ATHENA_OUTPUT_BUCKET_EXISTS \
                    ParameterKey=GuardDutyExportBucketExists,ParameterValue=$GUARDDUTY_EXPORT_BUCKET_EXISTS \
                    ParameterKey=DockerBuildBucketExists,ParameterValue=$DOCKER_BUILD_BUCKET_EXISTS \
                    ParameterKey=FrontendBucketExists,ParameterValue=$FRONTEND_BUCKET_EXISTS \
                    ParameterKey=DiagramBucketExists,ParameterValue=$DIAGRAM_BUCKET_EXISTS \
                    ParameterKey=FrontendRedirectDomain,UsePreviousValue=true \
                    ParameterKey=CallbackDomain,UsePreviousValue=true \
                    ParameterKey=McpFunctionUrl,UsePreviousValue=true \
                    ParameterKey=CodeVersion,ParameterValue=$AUTH_MESSAGES_VERSION \
        --tags Key=Project,Value=Vigie Key=Environment,Value=$ENV \
        --capabilities CAPABILITY_NAMED_IAM
else
    # 스택이 존재하지 않으면 생성
    echo "새 스택 생성 중: $BASE_STACK_NAME"
    aws cloudformation create-stack \
        --stack-name $BASE_STACK_NAME \
        --template-url "https://$CLOUDFORMATION_BUCKET.s3.$REGION.amazonaws.com/base.yaml" \
        --parameters ParameterKey=Environment,ParameterValue=$ENV \
                    ParameterKey=BucketExists,ParameterValue=$BUCKET_EXISTS \
                    ParameterKey=OutputBucketExists,ParameterValue=$OUTPUT_BUCKET_EXISTS \
                    ParameterKey=AthenaOutputBucketExists,ParameterValue=$ATHENA_OUTPUT_BUCKET_EXISTS \
                    ParameterKey=GuardDutyExportBucketExists,ParameterValue=$GUARDDUTY_EXPORT_BUCKET_EXISTS \
                    ParameterKey=DockerBuildBucketExists,ParameterValue=$DOCKER_BUILD_BUCKET_EXISTS \
                    ParameterKey=FrontendBucketExists,ParameterValue=$FRONTEND_BUCKET_EXISTS \
                    ParameterKey=DiagramBucketExists,ParameterValue=$DIAGRAM_BUCKET_EXISTS \
                    ParameterKey=FrontendRedirectDomain,ParameterValue=placeholder.example.com \
                    ParameterKey=CallbackDomain,ParameterValue=placeholder.example.com \
        --tags Key=Project,Value=Vigie Key=Environment,Value=$ENV \
        --capabilities CAPABILITY_NAMED_IAM

    # 스택 생성 완료 대기
    echo "스택 생성 완료 대기 중: $BASE_STACK_NAME"
    aws cloudformation wait stack-create-complete --stack-name $BASE_STACK_NAME
fi

echo "기본 인프라 스택 배포 완료: $BASE_STACK_NAME"

# 배포 버킷이 이제 존재해야 함
echo "배포 버킷이 존재하는지 확인 중: $DEPLOYMENT_BUCKET"
if ! aws s3 ls "s3://$DEPLOYMENT_BUCKET" > /dev/null 2>&1; then
    echo "배포 버킷이 없습니다. 기본 스택이 올바르게 생성되었는지 확인하세요."
    exit 1
fi

#################################################
# 3. 프론트엔드 인프라 배포
#################################################
# 프론트엔드 스택 배포
echo "====== 3. 프론트엔드 인프라 배포 ======"
echo "프론트엔드 인프라 스택 배포 중: $FRONTEND_STACK_NAME..."

# API Gateway 엔드포인트 URL 설정
API_GATEWAY_ID=$(aws cloudformation describe-stacks --stack-name $BASE_STACK_NAME --query "Stacks[0].Outputs[?OutputKey=='ApiGatewayId'].OutputValue" --output text)
API_ENDPOINT="https://${API_GATEWAY_ID}.execute-api.${REGION}.amazonaws.com/${ENV}"
echo "API 엔드포인트: $API_ENDPOINT"

# 도메인 설정에 대한 정보 표시
if [ -n "$DOMAIN_NAME" ] && [ -n "$CERTIFICATE_ARN" ]; then
    echo "사용자 정의 도메인: $DOMAIN_NAME (인증서: $CERTIFICATE_ARN)"
fi

if aws cloudformation describe-stacks --stack-name $FRONTEND_STACK_NAME > /dev/null 2>&1; then
    # 스택이 존재하면 업데이트
    echo "기존 프론트엔드 스택 업데이트 중: $FRONTEND_STACK_NAME"
    cfn_update $FRONTEND_STACK_NAME \
        --template-url "https://$CLOUDFORMATION_BUCKET.s3.$REGION.amazonaws.com/frontend.yaml" \
        --parameters \
            ParameterKey=Environment,ParameterValue=$ENV \
            ParameterKey=DomainName,ParameterValue=$DOMAIN_NAME \
            ParameterKey=CertificateARN,ParameterValue=$CERTIFICATE_ARN \
            ParameterKey=ApiEndpoint,ParameterValue=$API_ENDPOINT \
            ParameterKey=FrontendBucketName,ParameterValue=$FRONTEND_BUCKET \
        --tags Key=Project,Value=Vigie Key=Environment,Value=$ENV \
        --capabilities CAPABILITY_NAMED_IAM
else
    # 스택이 존재하지 않으면 생성
    echo "새 프론트엔드 스택 생성 중: $FRONTEND_STACK_NAME"
    aws cloudformation create-stack \
        --stack-name $FRONTEND_STACK_NAME \
        --template-url "https://$CLOUDFORMATION_BUCKET.s3.$REGION.amazonaws.com/frontend.yaml" \
        --parameters \
            ParameterKey=Environment,ParameterValue=$ENV \
            ParameterKey=DomainName,ParameterValue=$DOMAIN_NAME \
            ParameterKey=CertificateARN,ParameterValue=$CERTIFICATE_ARN \
            ParameterKey=ApiEndpoint,ParameterValue=$API_ENDPOINT \
            ParameterKey=FrontendBucketName,ParameterValue=$FRONTEND_BUCKET \
        --tags Key=Project,Value=Vigie Key=Environment,Value=$ENV \
        --capabilities CAPABILITY_NAMED_IAM

    # 스택 생성 완료 대기
    echo "프론트엔드 스택 생성 완료 대기 중: $FRONTEND_STACK_NAME"
    aws cloudformation wait stack-create-complete --stack-name $FRONTEND_STACK_NAME
fi

echo "프론트엔드 인프라 스택 배포 완료: $FRONTEND_STACK_NAME"

# 프론트엔드 배포 정보 가져오기
AMPLIFY_APP_ID=$(aws cloudformation describe-stacks --stack-name $FRONTEND_STACK_NAME --query "Stacks[0].Outputs[?OutputKey=='AmplifyAppId'].OutputValue" --output text)
FRONTEND_URL=$(aws cloudformation describe-stacks --stack-name $FRONTEND_STACK_NAME --query "Stacks[0].Outputs[?OutputKey=='AmplifyAppDefaultDomain'].OutputValue" --output text)

echo "프론트엔드 배포 정보:"
echo "Amplify App ID: $AMPLIFY_APP_ID"
echo "Amplify 기본 도메인: https://$FRONTEND_URL"

# FrontendRedirectDomain 정보를 SSM에 업데이트
echo "FrontendRedirectDomain 값을 SSM에 업데이트 중..."
aws ssm put-parameter \
    --name "$SSM_PATH_PREFIX/FrontendRedirectDomain" \
    --value "$FRONTEND_URL" \
    --type "String" \
    --overwrite

echo "기본 스택에서 출력값 가져오는 중..."
API_GATEWAY_ID=$(aws cloudformation describe-stacks --stack-name $BASE_STACK_NAME --query "Stacks[0].Outputs[?OutputKey=='ApiGatewayId'].OutputValue" --output text)
CALLBACK_DOMAIN="${API_GATEWAY_ID}.execute-api.${REGION}.amazonaws.com/${ENV}/callback"
echo "Callback Domain: ${CALLBACK_DOMAIN}"
# SSM 파라미터 변경 후 base 스택 업데이트 (SSM 파라미터가 CloudFormation에 의해 생성되기 때문)
echo "FrontendRedirectDomain 및 Callback URL 업데이트를 위해 base 스택 업데이트 중..."
upload_auth_messages
cfn_update $BASE_STACK_NAME \
    --template-url "https://$CLOUDFORMATION_BUCKET.s3.$REGION.amazonaws.com/base.yaml" \
    --parameters ParameterKey=Environment,ParameterValue=$ENV \
                ParameterKey=BucketExists,ParameterValue=true \
                ParameterKey=OutputBucketExists,ParameterValue=true \
                ParameterKey=AthenaOutputBucketExists,ParameterValue=true \
                ParameterKey=GuardDutyExportBucketExists,ParameterValue=true \
                ParameterKey=DockerBuildBucketExists,ParameterValue=true \
                ParameterKey=FrontendBucketExists,ParameterValue=true \
                ParameterKey=DiagramBucketExists,ParameterValue=true \
                ParameterKey=FrontendRedirectDomain,ParameterValue=$FRONTEND_URL \
                ParameterKey=CallbackDomain,ParameterValue=$CALLBACK_DOMAIN \
                ParameterKey=McpFunctionUrl,UsePreviousValue=true \
                ParameterKey=CodeVersion,ParameterValue=$AUTH_MESSAGES_VERSION \
    --tags Key=Project,Value=Vigie Key=Environment,Value=$ENV \
    --capabilities CAPABILITY_NAMED_IAM
echo "FrontendRedirectDomain 업데이트 완료"

echo "====== Layer 및 Lambda 함수 패키징 ======"

# 빌드 디렉토리 정리 (이전 빌드 파일 제거)
echo "빌드 디렉토리 정리 중..."
rm -rf build
mkdir -p build

# Common 레이어 패키징 및 업로드
echo "Common 레이어 패키징 중..."
mkdir -p build/layers/python/common
mkdir -p build/layers/python/lib/python3.12/site-packages
cp -r layers/common/* build/layers/python/common/

echo "Common 레이어 의존성 설치 중..."

# pip 명령이 환경마다 다를 수 있으므로 동적으로 결정
if command -v pip &> /dev/null; then
  PIP_CMD="pip"
elif command -v pip3 &> /dev/null; then
  PIP_CMD="pip3"
elif command -v python3 &> /dev/null; then
  PIP_CMD="python3 -m pip"
else
  echo "❌ pip(또는 python3)가 설치되어 있지 않습니다. Python/pip 을 먼저 설치해 주세요."
  exit 1
fi

$PIP_CMD install -r layers/common/requirements.txt -t build/layers/python/lib/python3.12/site-packages/

$PIP_CMD install --platform manylinux2014_x86_64 --only-binary=:all: \
    --target build/layers/python/lib/python3.12/site-packages/ \
    --implementation cp --python-version 3.12 --abi cp312 \
    pydantic pydantic-core

cd build/layers
echo "Common 레이어 압축 중..."
zip -r common-layer-$ENV.zip python
cd ../..

# Layer 구조 확인
echo "Layer 구조 확인:"
unzip -l build/layers/common-layer-$ENV.zip | head -n 20

# Layer 압축 파일 내용 검증
if unzip -l build/layers/common-layer-$ENV.zip | grep -q "python/common/config.py"; then
  echo "Layer 구조가 올바릅니다."
else
  echo "경고: Layer 구조가 올바르지 않을 수 있습니다!"
  unzip -l build/layers/common-layer-$ENV.zip
fi

echo "Common 레이어 업로드 중..."
aws s3 cp build/layers/common-layer-$ENV.zip "s3://$DEPLOYMENT_BUCKET/layers/common-layer-$ENV-$CODE_VERSION.zip"
# Athena Utility Lambda 패키징 및 업로드
if [ -d "services/db" ]; then
    echo "Athena Utility Lambda 패키징 중..."
    mkdir -p build/db
    cp -r services/db/* build/db/
    cd build/db
    echo "Athena Utility Lambda 압축 중..."
    zip -r athena-utility-lambda-$ENV.zip *
    cd ../..

    echo "Athena Utility Lambda 업로드 중..."
    aws s3 cp build/db/athena-utility-lambda-$ENV.zip "s3://$DEPLOYMENT_BUCKET/db/athena-utility-lambda-$ENV-$CODE_VERSION.zip"
fi

if [ -d "services/llm" ]; then
    echo "LLM Lambda 패키징 중..."
    mkdir -p build/llm
    cp -r services/llm/* build/llm/
    cd build/llm
    echo "LLM Lambda 압축 중..."
    zip -r llm-lambda-$ENV.zip *
    cd ../..

    echo "LLM 업로드 중..."
    aws s3 cp build/llm/llm-lambda-$ENV.zip "s3://$DEPLOYMENT_BUCKET/llm/llm-lambda-$ENV-$CODE_VERSION.zip"
fi

# 홈 대시보드 수집 Lambda (services/dashboard). 공통 코드(common/dashboard_store.py)는 레이어에 있다
if [ -d "services/dashboard" ]; then
    echo "대시보드 수집 Lambda 패키징 중..."
    mkdir -p build/dashboard
    cp -r services/dashboard/* build/dashboard/
    cd build/dashboard
    zip -r dashboard-lambda-$ENV.zip *
    cd ../..

    echo "대시보드 수집 Lambda 업로드 중..."
    aws s3 cp build/dashboard/dashboard-lambda-$ENV.zip "s3://$DEPLOYMENT_BUCKET/dashboard/dashboard-lambda-$ENV-$CODE_VERSION.zip"
fi

# Slackbot Lambda 패키징 및 업로드 (존재하는 경우)
if [ -d "services/slackbot" ]; then
    echo "Slackbot Lambda 패키징 중..."
    mkdir -p build/slackbot
    cp -r services/slackbot/* build/slackbot/
    cd build/slackbot
    echo "Slackbot Lambda 압축 중..."
    zip -r slackbot-lambda-$ENV.zip *
    cd ../..

    echo "Slackbot 업로드 중..."
    aws s3 cp build/slackbot/slackbot-lambda-$ENV.zip "s3://$DEPLOYMENT_BUCKET/slackbot/slackbot-lambda-$ENV-$CODE_VERSION.zip"
fi

# Chat History Lambda 패키징 및 업로드
if [ -d "services/chat-history" ]; then
    echo "Chat History Lambda 패키징 중..."
    mkdir -p build/chat-history
    cp -r services/chat-history/* build/chat-history/
    cd build/chat-history
    echo "Chat History Lambda 압축 중..."
    zip -r chat-history-lambda-$ENV.zip *
    cd ../..

    echo "Chat History Lambda 업로드 중..."
    aws s3 cp build/chat-history/chat-history-lambda-$ENV.zip "s3://$DEPLOYMENT_BUCKET/chat-history/chat-history-lambda-$ENV-$CODE_VERSION.zip"
fi

# MCP 패키징 및 업로드 (존재하는 경우)
if [ -d "mcp" ]; then
    echo "MCP 패키징 중..."
    mkdir -p build/mcp
    cp -r mcp/* build/mcp/
    cd build/mcp
    echo " 압축 중..."
    zip -r docker-build-$ENV.zip *
    cd ../..

    echo "MCP 업로드 중..."
    aws s3 cp build/mcp/docker-build-$ENV.zip "s3://$DOCKER_BUILD_BUCKET_NAME/docker-build-$ENV.zip"
fi

DOCKER_BUILD_BUCKET=$(aws ssm get-parameter --name "${SSM_PATH_PREFIX}/DockerBuildBucketName" --query "Parameter.Value" --output text --region ${REGION})
echo "도커 빌드 버킷: $DOCKER_BUILD_BUCKET"

# MCP 스택 배포 부분 수정
if aws cloudformation describe-stacks --stack-name $MCP_STACK_NAME > /dev/null 2>&1; then
    # 스택이 존재하면 업데이트
    echo "기존 스택 업데이트 중: $MCP_STACK_NAME"
    cfn_update $MCP_STACK_NAME \
        --template-url "https://$CLOUDFORMATION_BUCKET.s3.$REGION.amazonaws.com/mcp.yaml" \
        --parameters \
            ParameterKey=Environment,ParameterValue=$ENV \
            ParameterKey=DockerBuildBucketName,ParameterValue="$DOCKER_BUILD_BUCKET" \
        --tags Key=Project,Value=Vigie Key=Environment,Value=$ENV \
        --capabilities CAPABILITY_NAMED_IAM
else
    # 스택이 존재하지 않으면 생성
    echo "새 스택 생성 중: $MCP_STACK_NAME"
    aws cloudformation create-stack \
        --stack-name $MCP_STACK_NAME \
        --template-url "https://$CLOUDFORMATION_BUCKET.s3.$REGION.amazonaws.com/mcp.yaml" \
        --parameters \
            ParameterKey=Environment,ParameterValue=$ENV \
            ParameterKey=DockerBuildBucketName,ParameterValue="$DOCKER_BUILD_BUCKET" \
        --tags Key=Project,Value=Vigie Key=Environment,Value=$ENV \
        --capabilities CAPABILITY_NAMED_IAM
    # 스택 생성 완료 대기
    echo "MCP 스택 생성 완료 대기 중: $MCP_STACK_NAME"
    aws cloudformation wait stack-create-complete --stack-name $MCP_STACK_NAME
fi

# MCP docker build 시작
echo "MCP 배포 시작"
BUILD_ID=$(aws codebuild start-build \
  --project-name vigie-docker-build-$ENV \
  --environment-variables-override name=MCP_ECR_IMAGE_TAG,value=$CODE_VERSION,type=PLAINTEXT \
  --query 'build.id' \
  --output text)

echo "빌드 ID: $BUILD_ID"

# 빌드 완료 대기
echo "MCP 빌드 완료 대기 중..."
while true; do
  BUILD_STATUS=$(aws codebuild batch-get-builds --ids $BUILD_ID --query 'builds[0].buildStatus' --output text)
  echo "현재 빌드 상태: $BUILD_STATUS"

  if [ "$BUILD_STATUS" == "SUCCEEDED" ]; then
    echo "✅ MCP 빌드 성공!"
    break
  elif [ "$BUILD_STATUS" == "FAILED" ] || [ "$BUILD_STATUS" == "FAULT" ] || [ "$BUILD_STATUS" == "STOPPED" ] || [ "$BUILD_STATUS" == "TIMED_OUT" ]; then
    echo "❌ MCP 빌드 실패: $BUILD_STATUS"
    exit 1
  fi

  echo "계속 대기 중... (30초마다 상태 확인)"
  sleep 30
done

# 빌드 완료 후 ECR 이미지 URI 가져오기
ECR_REPOSITORY="vigie-mcp-$ENV"
ECR_IMAGE_TAG="$CODE_VERSION"
MCP_IMAGE_URI="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$ECR_REPOSITORY:$ECR_IMAGE_TAG"
echo "MCP 이미지 URI: $MCP_IMAGE_URI"


# 기본 스택에서 출력값 가져오기
echo "기본 스택에서 출력값 가져오는 중..."
API_GATEWAY_ID=$(aws cloudformation describe-stacks --stack-name $BASE_STACK_NAME --query "Stacks[0].Outputs[?OutputKey=='ApiGatewayId'].OutputValue" --output text)
API_GATEWAY_ROOT_RESOURCE_ID=$(aws cloudformation describe-stacks --stack-name $BASE_STACK_NAME --query "Stacks[0].Outputs[?OutputKey=='ApiGatewayRootResourceId'].OutputValue" --output text)
FRONTEND_REDIRECT_DOMAIN=$(aws cloudformation describe-stacks --stack-name $BASE_STACK_NAME --query "Stacks[0].Outputs[?OutputKey=='FrontendRedirectDomain'].OutputValue" --output text)

echo "가져온 파라미터 확인:"
echo "API_GATEWAY_ID: $API_GATEWAY_ID"
echo "API_GATEWAY_ROOT_RESOURCE_ID: $API_GATEWAY_ROOT_RESOURCE_ID"
echo "FRONTEND_REDIRECT_DOMAIN: $FRONTEND_REDIRECT_DOMAIN"

# 메인 스택 배포 부분 수정
if aws cloudformation describe-stacks --stack-name $MAIN_STACK_NAME > /dev/null 2>&1; then
    # 스택이 존재하면 업데이트
    echo "기존 스택 업데이트 중: $MAIN_STACK_NAME"
    cfn_update $MAIN_STACK_NAME \
        --template-url "https://$CLOUDFORMATION_BUCKET.s3.$REGION.amazonaws.com/main.yaml" \
        --parameters \
            ParameterKey=Environment,ParameterValue=$ENV \
            ParameterKey=DeveloperMode,ParameterValue=$DEVELOPER_MODE \
            ParameterKey=UserPoolId,ParameterValue="$SSM_PATH_PREFIX/UserPoolId" \
            ParameterKey=UserPoolClientId,ParameterValue="$SSM_PATH_PREFIX/UserPoolClientId" \
            ParameterKey=UserPoolDomain,ParameterValue="$SSM_PATH_PREFIX/UserPoolDomain" \
            ParameterKey=IdentityPoolId,ParameterValue="$SSM_PATH_PREFIX/IdentityPoolId" \
            ParameterKey=OutputBucketName,ParameterValue="$SSM_PATH_PREFIX/OutputBucketName" \
            ParameterKey=ApiGatewayIdParameter,ParameterValue="$SSM_PATH_PREFIX/ApiGatewayId" \
            ParameterKey=ApiGatewayRootResourceIdParameter,ParameterValue="$SSM_PATH_PREFIX/ApiGatewayRootResourceId" \
            ParameterKey=FrontendRedirectDomainParameter,ParameterValue="$SSM_PATH_PREFIX/FrontendRedirectDomain" \
            ParameterKey=AthenaOutputBucketParameter,ParameterValue="$SSM_PATH_PREFIX/AthenaOutputBucketName" \
            ParameterKey=KnowledgeBaseIdParameter,ParameterValue="$SSM_PATH_PREFIX/KnowledgeBaseId" \
            ParameterKey=CognitoAuthorizerIdParameter,ParameterValue="$SSM_PATH_PREFIX/CognitoAuthorizerId" \
            ParameterKey=McpImageUri,ParameterValue=$MCP_IMAGE_URI \
            ParameterKey=CodeVersion,ParameterValue=$CODE_VERSION \
            ParameterKey=AlarmEmail,ParameterValue="$ALARM_EMAIL" \
        --tags Key=Project,Value=Vigie Key=Environment,Value=$ENV \
        --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND
else
    # 스택이 존재하지 않으면 생성
    echo "새 스택 생성 중: $MAIN_STACK_NAME"
    aws cloudformation create-stack \
        --stack-name $MAIN_STACK_NAME \
        --template-url "https://$CLOUDFORMATION_BUCKET.s3.$REGION.amazonaws.com/main.yaml" \
        --parameters \
            ParameterKey=Environment,ParameterValue=$ENV \
            ParameterKey=DeveloperMode,ParameterValue=$DEVELOPER_MODE \
            ParameterKey=UserPoolId,ParameterValue="$SSM_PATH_PREFIX/UserPoolId" \
            ParameterKey=UserPoolClientId,ParameterValue="$SSM_PATH_PREFIX/UserPoolClientId" \
            ParameterKey=UserPoolDomain,ParameterValue="$SSM_PATH_PREFIX/UserPoolDomain" \
            ParameterKey=IdentityPoolId,ParameterValue="$SSM_PATH_PREFIX/IdentityPoolId" \
            ParameterKey=OutputBucketName,ParameterValue="$SSM_PATH_PREFIX/OutputBucketName" \
            ParameterKey=ApiGatewayIdParameter,ParameterValue="$SSM_PATH_PREFIX/ApiGatewayId" \
            ParameterKey=ApiGatewayRootResourceIdParameter,ParameterValue="$SSM_PATH_PREFIX/ApiGatewayRootResourceId" \
            ParameterKey=FrontendRedirectDomainParameter,ParameterValue="$SSM_PATH_PREFIX/FrontendRedirectDomain" \
            ParameterKey=AthenaOutputBucketParameter,ParameterValue="$SSM_PATH_PREFIX/AthenaOutputBucketName" \
            ParameterKey=KnowledgeBaseIdParameter,ParameterValue="$SSM_PATH_PREFIX/KnowledgeBaseId" \
            ParameterKey=CognitoAuthorizerIdParameter,ParameterValue="$SSM_PATH_PREFIX/CognitoAuthorizerId" \
            ParameterKey=McpImageUri,ParameterValue=$MCP_IMAGE_URI \
            ParameterKey=CodeVersion,ParameterValue=$CODE_VERSION \
            ParameterKey=AlarmEmail,ParameterValue="$ALARM_EMAIL" \
        --tags Key=Project,Value=Vigie Key=Environment,Value=$ENV \
        --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND
fi

echo "메인 스택 생성 대기 중..."
while true; do
    STATUS=$(aws cloudformation describe-stacks \
        --stack-name $MAIN_STACK_NAME \
        --query "Stacks[0].StackStatus" \
        --output text)

    echo "현재 상태: $STATUS"

    if [[ "$STATUS" == "CREATE_COMPLETE" || "$STATUS" == "UPDATE_COMPLETE" ]]; then
        echo "✅ 스택 생성 완료"
        break
    elif [[ "$STATUS" == *"FAILED"* || "$STATUS" == "ROLLBACK_COMPLETE" ]]; then
        echo "❌ 오류 발생: $STATUS"
        exit 1
    else
        sleep 10
    fi
done

echo "메인 스택 배포 완료: $MAIN_STACK_NAME"

# API Gateway URL 확인 (CloudFormation Output 대신 SSM 기반으로 구성)
API_GATEWAY_ID=$(aws ssm get-parameter --name "$SSM_PATH_PREFIX/ApiGatewayId" --query "Parameter.Value" --output text)

# main 스택의 Deployment 리소스는 스택 업데이트 시 다시 생성되지 않으므로,
# 메서드 변경(인증 방식 등)이 스테이지에 반영되도록 매 배포마다 새 Deployment를 만든다
echo "API Gateway 스테이지 재배포 중: $ENV"
aws apigateway create-deployment \
    --rest-api-id "$API_GATEWAY_ID" \
    --stage-name "$ENV" \
    --description "deploy.sh $(date -u +%Y-%m-%dT%H:%M:%SZ)" > /dev/null

# 스테이지 X-Ray 추적 활성화 (Deployment가 만든 스테이지라 템플릿 대신 여기서 설정)
aws apigateway update-stage \
    --rest-api-id "$API_GATEWAY_ID" \
    --stage-name "$ENV" \
    --patch-operations op=replace,path=/tracingEnabled,value=true > /dev/null
API_URL="https://${API_GATEWAY_ID}.execute-api.${REGION}.amazonaws.com/${ENV}"
echo "API Gateway URL: $API_URL"

#################################################
# 5. 환경 변수 설정
#################################################
echo "====== 4. 환경 변수 설정 ======"
# SSM 파라미터에서 값을 가져옴 (API URL 제외)
echo "[INFO] SSM에서 구성 값을 가져옵니다."
USER_POOL_ID=$(aws ssm get-parameter --name "$SSM_PATH_PREFIX/UserPoolId" --query "Parameter.Value" --output text)
USER_POOL_CLIENT_ID=$(aws ssm get-parameter --name "$SSM_PATH_PREFIX/UserPoolClientId" --query "Parameter.Value" --output text)
USER_POOL_DOMAIN=$(aws ssm get-parameter --name "$SSM_PATH_PREFIX/UserPoolDomain" --query "Parameter.Value" --output text)

# 프론트엔드 빌드에 들어가는 값을 루트 .env에 채운다 (frontend/vite.config.ts가 읽는다).
# 직접 적어 둔 ANTHROPIC_API_KEY 등 다른 줄은 그대로 둔다. .env가 없으면 .env.example로 만든다
if [ ! -f "$ROOT_ENV_FILE" ]; then
    cp "$(dirname "$ROOT_ENV_FILE")/.env.example" "$ROOT_ENV_FILE"
    chmod 600 "$ROOT_ENV_FILE"
fi
echo "환경 파일 업데이트 중: $ROOT_ENV_FILE (프론트엔드 값)"
dotenv_set "$ROOT_ENV_FILE" VITE_API_DEST "$API_URL"
dotenv_set "$ROOT_ENV_FILE" AWS_REGION "$REGION"
dotenv_set "$ROOT_ENV_FILE" USER_POOL_ID "$USER_POOL_ID"
dotenv_set "$ROOT_ENV_FILE" COGNITO_CLIENT_ID "$USER_POOL_CLIENT_ID"
# Cognito 도메인은 앞부분만 (https://<앞부분>.auth.<리전>.amazoncognito.com)
dotenv_set "$ROOT_ENV_FILE" COGNITO_DOMAIN "$(echo "$USER_POOL_DOMAIN" | sed -E 's#https://([^.]*)\..*#\1#')"
echo "환경 파일($ROOT_ENV_FILE)이 업데이트되었습니다."

# 관리자 계정: User Pool이 생긴 뒤에 만든다 (ADMIN_EMAIL이 있을 때만)
ensure_admin_account "$USER_POOL_ID" "$ADMIN_EMAIL"

#################################################
# 6. 프론트엔드 빌드 및 배포
#################################################
echo "====== 5. 프론트엔드 빌드 및 배포 ======"

# frontend 디렉토리로 이동
cd frontend

# 빌드 디렉토리 설정
BUILD_DIR="dist"

echo "의존성 설치 중..."
npm install

echo "프로젝트 빌드 중..."
npm run build

if [ ! -d "$BUILD_DIR" ]; then
    echo "빌드 디렉토리($BUILD_DIR)가 존재하지 않습니다. 빌드가 실패했습니다."
    exit 1
fi

# Amplify 업로드 대상 버킷 경로 (계정 ID 포함)
echo "Amplify에 S3 업로드 중... ($FRONTEND_BUCKET)"

# dist 폴더를 프론트엔드 버킷 루트(또는 해당 prefix)와 동기화
aws s3 sync dist s3://$FRONTEND_BUCKET --delete
  # Amplify에 S3 업로드 이후 수동 배포 트리거
  echo "Amplify 수동 배포 트리거 중..."
  TIMESTAMP=$(date +%s)
  S3_DEPLOY_PATH="s3://$FRONTEND_BUCKET/amplify-upload-$TIMESTAMP/"

  # 업로드한 dist 디렉토리를 복사 (원본 경로를 분리된 위치로 저장)
  aws s3 cp --recursive dist "$S3_DEPLOY_PATH"

  # Amplify 수동 배포 시작
  aws amplify start-deployment \
    --app-id "$AMPLIFY_APP_ID" \
    --branch-name "$ENV" \
    --source-url "$S3_DEPLOY_PATH" \
    --source-url-type BUCKET_PREFIX

  echo "Amplify 배포 요청 완료"
echo "Amplify 배포 완료"

# 기존 디렉토리로 돌아감
cd ..

McpFunctionUrl=$(aws lambda get-function-url-config \
  --function-name vigie-mcp-$ENV \
  --query "FunctionUrl" \
  --output text)
echo "McpFunctionUrl: $McpFunctionUrl"
echo "McpFunctionUrl 업데이트를 위해 base 스택 업데이트 중..."
cfn_update $BASE_STACK_NAME \
    --template-url "https://$CLOUDFORMATION_BUCKET.s3.$REGION.amazonaws.com/base.yaml" \
    --parameters ParameterKey=Environment,ParameterValue=$ENV \
                ParameterKey=BucketExists,ParameterValue=true \
                ParameterKey=OutputBucketExists,ParameterValue=true \
                ParameterKey=AthenaOutputBucketExists,ParameterValue=true \
                ParameterKey=GuardDutyExportBucketExists,ParameterValue=true \
                ParameterKey=DockerBuildBucketExists,ParameterValue=true \
                ParameterKey=FrontendBucketExists,ParameterValue=true \
                ParameterKey=DiagramBucketExists,ParameterValue=true \
                ParameterKey=FrontendRedirectDomain,ParameterValue=$FRONTEND_URL \
                ParameterKey=CallbackDomain,ParameterValue=$CALLBACK_DOMAIN \
                ParameterKey=McpFunctionUrl,ParameterValue=$McpFunctionUrl \
                ParameterKey=CodeVersion,ParameterValue=$AUTH_MESSAGES_VERSION \
    --tags Key=Project,Value=Vigie Key=Environment,Value=$ENV \
    --capabilities CAPABILITY_NAMED_IAM

# 배포 직후 첫 질문이 MCP Lambda의 콜드 스타트를 기다리지 않도록 한 번 깨워 둔다 (위 warm_up_mcp)
warm_up_mcp "vigie-mcp-$ENV"

#################################################
# 7. 배포 완료 요약
#################################################
echo "====== 6. 배포 완료 요약 ======"
echo "환경: $ENV"

if [ "$SKIP_BACKEND" != "true" ]; then
    echo "백엔드 배포 상태: 성공"
    echo "API URL: $API_URL"
else
    echo "백엔드 배포 상태: 스킵됨"
fi

if [ "$SKIP_FRONTEND" != "true" ]; then
    echo "프론트엔드 배포 상태: 성공"
    echo "프론트엔드 URL: $ENV.$FRONTEND_URL"
else
    echo "프론트엔드 배포 상태: 스킵됨"
fi
# Athena 테이블 레지스트리 초기값 (CloudTrail·GuardDuty 로그를 저장하는 버킷은 계정마다 다르므로 환경 변수로 지정)
CLOUDTRAIL_LOG_BUCKET=${CLOUDTRAIL_LOG_BUCKET:-vigie-cloudtrail-$ACCOUNT_ID}
GUARDDUTY_LOG_BUCKET=${GUARDDUTY_LOG_BUCKET:-vigie-guardduty-logs-$ACCOUNT_ID}
REGISTRY_UPDATED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

aws dynamodb put-item --table-name "AthenaTableRegistry-$ENV" --item '{
  "log_type": {"S": "cloudtrail"},
  "table_name": {"S": "cloudtrail_logs"},
  "s3_path": {"S": "s3://'"$CLOUDTRAIL_LOG_BUCKET"'/AWSLogs/'"$ACCOUNT_ID"'/CloudTrail/'"$REGION"'/"},
  "updated_at": {"S": "'"$REGISTRY_UPDATED_AT"'"}
}'

aws dynamodb put-item --table-name "AthenaTableRegistry-$ENV" --item '{
  "log_type": {"S": "guardduty"},
  "table_name": {"S": "guardduty_logs"},
  "s3_path": {"S": "s3://'"$GUARDDUTY_LOG_BUCKET"'/guardduty-logs/"},
  "updated_at": {"S": "'"$REGISTRY_UPDATED_AT"'"}
}'
# SSM 파라미터 요약 출력
echo "====== SSM 파라미터 요약 ======"
aws ssm get-parameters-by-path \
    --path "$SSM_PATH_PREFIX" \
    --query "Parameters[*].[Name,Value]" \
    --output table

echo "====== 배포 완료! ======"
echo "배포 시간: $(date)"
echo "감사합니다!"