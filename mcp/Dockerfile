# 공식 AWS Lambda Python 기본 이미지를 사용합니다
FROM public.ecr.aws/lambda/python:3.12

# 컨테이너 내 작업 디렉토리 설정
WORKDIR ${LAMBDA_TASK_ROOT}

# 실행할 때 필요한 시스템 패키지만 설치합니다.
# 이미지가 작을수록 콜드 스타트가 빠릅니다: Lambda는 이미지의 파일을 처음 읽을 때 받아 오기 때문입니다.
# - graphviz: 다이어그램 도구(diagrams 패키지)가 그림을 그릴 때 부르는 dot 프로그램
#   (파이썬 쪽 graphviz 패키지는 dot을 부르기만 하는 순수 파이썬이라 graphviz-devel·컴파일러가 필요 없다)
# - google-noto-sans-cjk-ttc-fonts, fontconfig: 다이어그램·차트의 한글 글꼴
#   (diagram_utils.py는 'Noto Sans CJK KR'을 먼저 찾는다. 세리프 글꼴은 그다음 후보라 넣지 않는다)
# 예전에 함께 설치하던 gcc·gcc-c++·make·pkgconfig·graphviz-devel은 빌드 도구라 뺐습니다.
# requirements.txt의 패키지는 모두 미리 빌드된 wheel로 설치되어 컴파일할 것이 없습니다.
# dnf update도 하지 않습니다: 기본 이미지를 AWS가 보안 업데이트와 함께 자주 새로 올리고,
# 업데이트하면 바뀐 패키지만큼 이미지가 커집니다.
RUN dnf install -y \
        graphviz \
        google-noto-sans-cjk-ttc-fonts \
        fontconfig \
    && fc-cache -f \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# Docker 캐시를 활용하기 위해 먼저 요구 사항을 복사합니다
COPY requirements.txt .

# Python 종속성 설치
RUN pip install --no-cache-dir -r requirements.txt

# lambda_mcp 모듈 폴더 복사
COPY lambda_mcp/ lambda_mcp/

# 공식 MCP 도구 목록 스냅샷 (lambda_mcp/official.py 모듈 설명).
# 이미지에 설치된 공식 서버를 한 번 불러와 도구 정의를 lambda_mcp/official_tools.json에 적어 둔다.
# 그러면 tools/list가 공식 서버 7개를 불러오지 않고 이 파일을 읽어, 차가운 MCP Lambda의 첫 질문이 10초 넘게 기다리지 않는다.
# 만들지 못해도 빌드는 계속한다 (파일이 없으면 실행할 때 예전처럼 모두 불러와 모은다).
# 리전은 도구 정의에 쓰이지 않는다 (읽을 때 Lambda의 리전으로 채운다). 서버가 import할 때 boto3가 리전을 찾으므로
# 빌드하는 곳(CodeBuild)의 리전을 받아 둔다 (cloudformation/mcp.yaml의 --build-arg). 실행 환경 변수로는 남지 않는다
ARG AWS_REGION
RUN AWS_DEFAULT_REGION=$AWS_REGION python -m lambda_mcp.official --write-snapshot

# 애플리케이션 코드의 나머지를 복사합니다
COPY app.py .

# 핸들러 함수 실행 명령 설정
CMD ["app.lambda_handler"]
