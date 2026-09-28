// 감사 로그 (GET /audit, services/llm/audit.py)와 같은 모양
import type { TaintedBy } from './actions';

export type AuditKind = 'tool' | 'request' | 'action' | 'admin';
export type AuditStatus = 'ok' | 'error';
// group_added·group_removed는 권한 세 단계 전의 기록이다 (지금은 role_changed)
export type AdminEvent = 'invited' | 'role_changed' | 'group_added' | 'group_removed' | 'disabled' | 'enabled';
export type AuditScope = 'mine' | 'all' | 'user';
// 층: 도구 반복 위의 자리 (services/llm/audit.py 모듈 설명). residence는 조회 조건으로만 쓴다 (taintedBy가 있는 승인 요청)
export type AuditLocus = 'interface' | 'ingress' | 'residence' | 'egress' | 'effect';

// 토큰 네 종류 (입력에는 캐시에서 읽은·캐시에 쓴 입력이 들어 있지 않다)
export interface TokenCounts {
    input: number;
    output: number;
    cacheWrite: number;
    cacheRead: number;
}

export interface AuditRecord {
    userId: string; // 웹은 Cognito sub, Slack은 'slack:<사용자 ID>'
    at: string; // 시각(UTC, 밀리초까지) + '#' + 도구 호출 ID 또는 'request#<질문 ID>'
    day: string; // YYYY-MM-DD (UTC)
    kind: AuditKind;
    locus?: Exclude<AuditLocus, 'residence'>; // 질문 행(요약)에는 없다
    status: AuditStatus;
    source?: 'web' | 'slack' | 'direct';
    email?: string;
    requestId?: string;
    sessionId?: string;
    ms?: number; // 걸린 시간
    error?: string;
    // 도구 호출 (kind: 'tool')
    tool?: string;
    toolUseId?: string;
    input?: Record<string, unknown> | string; // 길어서 잘린 입력은 글자로 온다
    resultChars?: number;
    diagnosis?: Diagnosis; // 진단 도구(diagnoseService)의 층별 판정. 팝업창이 처리 단계 대신 서비스 진단 층을 그린다
    // 지시문처럼 보이는 문구 (services/llm/injection.py). 도구 호출은 종류 목록, 질문은 그런 도구 결과의 수
    injectionSuspected?: string[] | number;
    // 질문 (kind: 'request')
    question?: string;
    model?: string;
    toolCount?: number;
    redacted?: Record<string, number>; // Claude로 보내기 전에 가린 값의 수 (종류별)
    // 사용자가 받은 답변: 목록에는 앞부분과 글자 수만 온다. 전체는 팝업창이 열 때 따로 받는다 (fetchAnswer)
    answerPreview?: string;
    answerChars?: number;
    // 쓴 토큰과 예상 비용 (services/llm/llm_cost.py). 단가표에 없는 모델이면 costMicroUsd·price가 없다
    tokens?: TokenCounts; // 네 종류의 합
    modelCalls?: TokenCounts[]; // 모델을 부를 때마다의 토큰
    costMicroUsd?: number; // 예상 비용 (마이크로달러 = 백만분의 1달러)
    price?: Record<keyof TokenCounts, string>; // 계산에 쓴 단가 (USD / 백만 토큰)
    // 변경 작업의 사건 (kind: 'action', services/llm/approvals.py)
    event?: 'requested' | 'approved' | 'denied' | 'executed' | 'failed' | AdminEvent;
    actionId?: string;
    summary?: string; // 예: "보존 기간 30일 → 14일"
    decidedBy?: string; // 승인·거절한 사람 (Cognito sub)
    result?: string; // 실행 결과
    awsRequestId?: string; // 실행한 AWS API의 요청 ID = CloudTrail 이벤트의 requestID
    cloudTrailEvent?: string; // 예: "logs.amazonaws.com:PutRetentionPolicy"
    taintedBy?: TaintedBy[]; // 승인 요청 행: 이 변경 전에 같은 질문에서 읽은 의심 결과 (체류층)
    // 사용자 관리의 사건 (kind: 'admin', services/llm/user_admin.py). event: invited·group_added·group_removed·disabled·enabled
    targetUser?: string; // 바꾼 사용자의 Cognito 사용자 이름
    targetEmail?: string;
    group?: string; // admins, approvers (예전 기록)
    fromRole?: string; // 권한 변경: 전 (member, decider, admin)
    toRole?: string; // 권한 변경: 후
}

// 진단 (mcp/lambda_mcp/diagnose.py → services/llm/audit.py의 diagnosis_of): 자원 하나를 정해진 절차로 확인한 층별 판정
export type DiagnosisLayerId = 'L1' | 'L2' | 'L3' | 'L4' | 'L5' | 'L6' | 'L7';
// 원인 · 증상(다른 층의 결과) · 주의(위험한 설정) · 정상 · 확인 불가 · 해당 없음
export type DiagnosisStatus = 'cause' | 'symptom' | 'warn' | 'ok' | 'unknown' | 'skip';

export interface DiagnosisCheck {
    name: string;
    status: DiagnosisStatus;
    finding: string;
    evidence: string; // 본 API·지표 이름
}

export interface DiagnosisLayer {
    id: DiagnosisLayerId;
    name: string; // 층의 이름 (예: 컴퓨팅)
    component: string; // 이 서비스에서 그 층이 가리키는 부품 (예: 대상 EC2)
    status: DiagnosisStatus; // 항목 중 가장 무거운 판정
    finding: string; // 층의 판정을 정한 항목의 찾은 것
    checks: DiagnosisCheck[];
}

export interface Diagnosis {
    service: string; // alb, ec2, lambda, s3
    serviceName: string;
    resource: string;
    target?: string; // VPC 연결의 목적지 (IP:포트)
    hours: number; // 본 시간 (지표·로그·변경). 비용은 최근 3일(72)
    summary: string;
    causes: DiagnosisLayerId[];
    layers: DiagnosisLayer[];
}

export interface AuditPage {
    items: AuditRecord[];
    cursor: string | null; // 더 있으면 다음 조회에 넘긴다
    scope: AuditScope;
    isAdmin: boolean; // 항상 true (감사 로그는 admins 그룹만 조회할 수 있다. 화면은 토큰의 그룹으로 판단한다)
    from: string;
    to: string;
}

// 역추적 (GET /audit?trace=<actionId>, services/llm/audit_trace.py): 변경 작업 하나를 층 하나씩 아래에서 위로 묻는다
export type TraceLayer = 'effect' | 'egress' | 'residence' | 'deliberation' | 'ingress' | 'interface' | 'mediation';
export type TraceStatus = 'ok' | 'warn' | 'fail' | 'info';

export interface TraceStep {
    layer: TraceLayer;
    question: string;
    status: TraceStatus;
    answer: string;
    evidence: string[]; // 근거가 된 행의 at (events·rows 안에 있다)
}

export interface AuditTrace {
    actionId: string;
    steps: TraceStep[]; // 효과 → 유출 → 체류 → 판단 → 유입 → 경계 → 매개
    verdict: string;
    question?: string | null; // 이 변경을 낳은 사용자의 질문
    events: AuditRecord[]; // 작업의 사건 (요청자·승인자)
    rows: AuditRecord[]; // 같은 질문의 질문·도구 행
}

export interface AuditQuery {
    from?: string;
    to?: string;
    scope?: 'mine' | 'all';
    user?: string;
    tool?: string;
    status?: AuditStatus;
    kind?: AuditKind;
    locus?: AuditLocus;
    limit?: number;
    cursor?: string;
}

// 질문 하나의 답변 전체 (GET /audit?answer=<질문 행의 at>&user=<요청자>)
export interface AuditAnswer {
    answer: string;
    answerChars?: number; // 잘리기 전의 전체 길이 (서버의 안전 상한을 넘으면 answer보다 길다)
}
