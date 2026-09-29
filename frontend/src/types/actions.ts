// 변경 작업 승인 요청 (services/llm/approvals.py의 public_view와 같은 모양).
// 답변의 inference.pendingActions와 GET·POST /actions/{actionId}의 응답이 이 모양이다

// 이 변경을 요청하기 전에 같은 질문에서 모델이 읽은 도구 결과 중 지시문처럼 보이는 문구가 있던 것 (체류 신호).
// 판단은 바꾸지 않는다 (변경은 원래 모두 승인이 필요하다). 승인자에게 요청의 출처를 의심하라고 알린다
export interface TaintedBy {
    toolUseId: string;
    tool: string; // 그 결과를 돌려준 도구 (get_logs_insight_query_results 등)
    kinds: string[]; // 찾은 문구의 종류 (services/llm/injection.py)
    callsAgo: number; // 그 결과 뒤로 몇 번째 도구 호출에서 이 변경을 요청했는가 (1이면 바로 다음)
    // 요청 값 중 이 결과의 글 안에 그대로 있고 사용자의 질문에는 없던 인자 (값이 지시문에서 왔다). 없으면 뺀다
    matchedArgs?: string[];
}

export type ActionStatus = 'pending' | 'approved' | 'denied' | 'executing' | 'executed' | 'failed' | 'expired';

export interface PendingAction {
    actionId: string;
    tool: string; // 변경 도구 이름 (setLogRetention 등)
    args: Record<string, unknown>; // 승인하면 실제로 실행될 인자 (가리지 않는다)
    summary: string; // 예: "/aws/lambda/vigie-llm-dev 로그 보존 기간 30일 → 14일 (지난 로그 일부가 지워질 수 있습니다)"
    // 카드에 따로 보일 대상 리소스와 이 변경의 영향 (mcp/app.py의 _preview). 예전 승인 요청에는 없다 → summary를 보인다
    target?: string; // 예: "/aws/lambda/vigie-llm-dev"
    warning?: string; // 예: "지난 로그 일부가 지워질 수 있습니다"
    before?: string;
    after?: string;
    status: ActionStatus;
    requesterId?: string;
    createdAt: number; // 초 (epoch)
    expiresAt: number; // 초 (epoch). 이때까지 승인하지 않으면 만료된다 (10분)
    decidedBy?: string;
    decidedAt?: number;
    result?: string; // 실행 결과 (MCP 도구가 돌려준 글)
    // 이 변경이 CloudTrail에 남긴 이벤트를 찾을 단서. CloudTrail 이벤트의 requestID가 request_id와 같다
    cloudtrail?: { event_source: string; event_name: string; request_id: string };
    taintedBy?: TaintedBy[];
}
