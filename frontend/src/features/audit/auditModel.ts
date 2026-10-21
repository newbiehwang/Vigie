// 감사 기록을 화면에 보이기 위한 이름표·요약과, 왼쪽 거르기 목록(facet)의 계산.
// 화면(React)과 떨어진 순수 함수만 둔다. AuditPage·AuditDetails·FacetSidebar·AuditDetailModal이 같이 쓴다.
import { ROLE_LABELS, type Role } from '@/auth/authClient';
import type { AuditGate, AuditKind, AuditRecord, TokenCounts } from '@/types/audit';
import { labelOf, summarize } from '@/utils/toolTrace';

// ---------------------------------------------------------------- 이름표

export const KIND_LABELS: Record<AuditKind, string> = {
    tool: '도구 호출',
    request: '질문',
    action: '변경 작업',
    admin: '사용자 관리',
};

// 사용자 관리의 사건 (services/llm/user_admin.py)
export const ADMIN_EVENTS: Record<string, string> = {
    invited: '초대',
    role_changed: '권한 변경',
    group_added: '그룹 추가',
    group_removed: '그룹 제외',
    disabled: '정지',
    enabled: '정지 해제',
};
export const GROUP_NAMES: Record<string, string> = { admins: '관리자', approvers: '결정자' };
export const roleText = (role?: string) => (role ? ROLE_LABELS[role as Role] ?? role : '');
const adminSummaryOf = (record: AuditRecord) =>
    [
        record.targetEmail ?? record.targetUser,
        ADMIN_EVENTS[record.event ?? ''] ?? record.event,
        record.toRole ? `${roleText(record.fromRole)} → ${roleText(record.toRole)}` : null,
        record.group ? GROUP_NAMES[record.group] ?? record.group : null,
    ]
        .filter(Boolean)
        .join(' · ');

// 관문 (services/llm/audit.py · audit_trace.py 모듈 설명): 변경이 지나는 길에서 판정이 실제로 갈리는 자리 셋.
// 모델의 판단(모델 안)은 언제나 속을 수 있다고 가정하고, 모든 변경이 MCP 역할로만 나간다는 것(매개)은 앱 밖의 전제다.
// 행에 적는 자리(locus)는 넷이다: 실행 관문은 승인 관문의 앞(egress, 승인 요청)과 뒤(effect, 결정·실행)로 남는다
export const GATES: { value: AuditGate; label: string; en: string; description: string }[] = [
    { value: 'interface', label: '경계', en: 'Interface', description: '위험도 등록부에 있는 도구만 불렀는가. 없는 도구는 변경 도구로 다루고 승인을 거친다' },
    { value: 'ingress', label: '유입', en: 'Ingress', description: '무엇을 읽었고, 의심 문구가 있었나. 그 뒤 변경을 요청했나. 의심 문구는 패턴으로 찾아 놓칠 수 있다' },
    { value: 'execution', label: '실행', en: 'Execution', description: '승인 요청을 거쳐 사람이 결정했고, AWS가 실제로 바뀌었는가. 사용자가 묻지 않은 감시 장치 약화인가' },
];
export const gateOf = (locus?: string): AuditGate | undefined =>
    locus === 'interface' || locus === 'ingress' ? locus : locus === 'egress' || locus === 'effect' ? 'execution' : undefined;
export const gateLabel = (locus?: string) => GATES.find((gate) => gate.value === gateOf(locus))?.label;

// 변경 작업의 사건 → 결과 열에 보일 이름과 모양
export const ACTION_EVENTS: Record<string, { label: string; className: string }> = {
    // className: 공통 배지(components/badge.css)의 변형. 승인·실행은 그래프의 '성공'과 같은 파랑, 실패는 빨강,
    // 승인 요청은 결과가 아직 없어 회청색, 거절은 옅은 점
    requested: { label: '승인 요청', className: 'is-quiet' },
    approved: { label: '승인', className: 'is-ok' },
    denied: { label: '거절', className: 'is-quiet is-empty' },
    executed: { label: '실행', className: 'is-ok' },
    failed: { label: '실패', className: 'is-fail' },
};

// 가린 값의 종류 → 화면에 보일 이름 (services/llm/redaction.py)
export const REDACTED_LABELS: Record<string, string> = {
    aws_access_key: 'AWS 액세스 키', // 예전 기록: 키 ID를 비밀 값으로 지우던 때
    aws_access_key_id: 'AWS 액세스 키 ID',
    aws_secret_key: 'AWS 비밀 키',
    aws_session_token: 'AWS 세션 토큰',
    anthropic_api_key: 'Anthropic 키',
    slack_token: 'Slack 토큰',
    slack_webhook: 'Slack 웹훅',
    github_token: 'GitHub 토큰',
    jwt: '로그인 토큰(JWT)',
    private_key: '개인 키',
    url_password: '접속 주소 비밀번호',
    account_id: '계정 ID',
    email: '이메일',
};

// ---------------------------------------------------------------- 기록 한 건에서 읽는 값

export const timeOf = (record: AuditRecord) => record.at.split('#')[0];
export const keyOf = (record: AuditRecord) => `${record.userId}|${record.at}`;
export const secondsOf = (ms?: number) => (typeof ms === 'number' ? `${(ms / 1000).toFixed(1)}초` : '');

// 토큰 네 종류: 입력 1,000 · 출력 500 · 캐시 쓰기 2,000 · 캐시 읽기 10,000 (0인 캐시는 뺀다)
export const TOKEN_LABELS: [keyof TokenCounts, string][] = [
    ['input', '입력'],
    ['output', '출력'],
    ['cacheWrite', '캐시 쓰기'],
    ['cacheRead', '캐시 읽기'],
];
export const tokensText = (tokens: TokenCounts) =>
    TOKEN_LABELS.filter(([kind]) => kind === 'input' || kind === 'output' || tokens[kind])
        .map(([kind, label]) => `${label} ${(tokens[kind] ?? 0).toLocaleString()}`)
        .join(' · ');
export const tokenTotal = (tokens: TokenCounts) => TOKEN_LABELS.reduce((sum, [kind]) => sum + (tokens[kind] ?? 0), 0);

// 예상 비용 (마이크로달러 → USD 글자). 1달러 미만은 센트 아래까지 보이게 소수 넷째 자리까지.
// 반올림은 정수로 먼저 한다: 0.01695.toFixed(4)는 부동소수점 때문에 0.0169가 된다
export const usdText = (micro: number) => {
    if (micro > 0 && micro < 50) return '$0.0001 미만';
    return micro >= 999_950 // 넷째 자리에서 반올림하면 1달러가 되는 값부터
        ? `$${(Math.round(micro / 10_000) / 100).toFixed(2)}`
        : `$${(Math.round(micro / 100) / 10_000).toFixed(4)}`;
};
export const suspiciousOf = (record: AuditRecord) =>
    Array.isArray(record.injectionSuspected) ? record.injectionSuspected.length > 0 : !!record.injectionSuspected;
export const redactedTotal = (record: AuditRecord) =>
    Object.values(record.redacted ?? {}).reduce((sum, count) => sum + count, 0);

export const requesterOf = (record: AuditRecord) => {
    if (record.email) return record.email;
    if (record.userId.startsWith('slack:')) return `Slack ${record.userId.slice('slack:'.length)}`;
    return record.userId;
};

// 변경 도구 이름표는 '… 요청'으로 끝난다 (도구 호출 목록에서 요청했다는 뜻). 행동 이름으로 쓸 때는 뗀다
export const toolLabelOf = (tool?: string) => labelOf(tool ?? '').replace(/ 요청$/, '');

export const summaryOf = (record: AuditRecord) => {
    if (record.kind === 'request') return record.question ?? '';
    if (record.kind === 'action') return record.summary ?? '';
    if (record.kind === 'admin') return adminSummaryOf(record);
    if (typeof record.input === 'string') return record.input;
    return summarize(record.input);
};

// ---------------------------------------------------------------- 왼쪽 거르기 목록 (Datadog Audit Trail의 facet)
//
// 거르기 하나(facet)는 기록 한 건에서 값 몇 개를 읽는다 (표시처럼 여러 개일 수 있다).
// - 같은 거르기 안에서 고른 값들은 '또는', 서로 다른 거르기끼리는 '그리고'로 잇는다. 아무것도 고르지 않으면 거르지 않는다
// - 값 옆의 건수는 '다른 거르기는 모두 적용하고 이 거르기만 뺀' 기록에서 센다.
//   그래서 결과에서 '실패'를 골라도 결과 목록에는 성공의 건수가 그대로 보이고, 요청자 목록은 실패한 기록의 요청자만 센다

export type FacetId = 'kind' | 'result' | 'locus' | 'requester' | 'tool' | 'source' | 'flag';
export type Selection = Partial<Record<FacetId, string[]>>; // 거르기마다 고른 값 (비었거나 없으면 거르지 않는다)

export interface FacetDef {
    id: FacetId;
    label: string;
    valuesOf: (record: AuditRecord) => string[];
    order?: string[]; // 값을 이 순서로 보인다 (없으면 건수가 많은 순)
    // 값 → 화면에 보일 이름. 요청자처럼 기록에서 이름을 읽어야 하는 것은 목록을 만들 때 채운다 (FacetValue.label)
    labelOf?: (value: string) => string;
}

// 결과: 변경 작업은 사건(승인 요청·승인·거절·실행·실행 실패), 나머지는 성공·실패
const RESULT_LABELS: Record<string, string> = {
    ok: '성공',
    error: '실패',
    requested: '승인 요청',
    approved: '승인',
    denied: '거절',
    executed: '실행',
    failed: '실행 실패',
};
const resultOf = (record: AuditRecord) =>
    record.kind === 'action' && record.event && record.event in ACTION_EVENTS ? record.event : record.status;

// 목록의 '도구' 칸 글자 (KindLabel과 같다. 정렬에도 쓴다)
export const kindTextOf = (record: AuditRecord) =>
    record.kind === 'request'
        ? '질문'
        : record.kind === 'action'
          ? toolLabelOf(record.tool)
          : record.kind === 'admin'
            ? '사용자 관리'
            : labelOf(record.tool ?? '');

// 결과로 정렬할 때의 차례: 거르기 목록과 같은 차례 (성공 · 실패 · 승인 요청 · 승인 · 거절 · 실행 · 실행 실패)
export const resultRankOf = (record: AuditRecord) => {
    const index = Object.keys(RESULT_LABELS).indexOf(resultOf(record) ?? '');
    return index < 0 ? Number.MAX_SAFE_INTEGER : index;
};

const SOURCE_LABELS: Record<string, string> = { web: '웹', slack: 'Slack', direct: '직접 호출' };

const FLAG_LABELS: Record<string, string> = {
    suspicious: '의심 문구',
    tainted: '의심 뒤 요청',
    unregistered: '미등록 도구',
    redacted: '가림',
};
export const flagsOf = (record: AuditRecord) => {
    const flags: string[] = [];
    if (suspiciousOf(record)) flags.push('suspicious');
    if (record.taintedBy?.length) flags.push('tainted');
    if (record.locus === 'interface') flags.push('unregistered');
    if (redactedTotal(record)) flags.push('redacted');
    return flags;
};

export const FACETS: FacetDef[] = [
    {
        id: 'kind',
        label: '종류',
        valuesOf: (record) => [record.kind],
        order: Object.keys(KIND_LABELS),
        labelOf: (value) => KIND_LABELS[value as AuditKind] ?? value,
    },
    {
        id: 'result',
        label: '결과',
        valuesOf: (record) => [resultOf(record)],
        order: Object.keys(RESULT_LABELS),
        labelOf: (value) => RESULT_LABELS[value] ?? value,
    },
    {
        id: 'locus',
        label: '관문',
        // 의심 결과를 읽은 뒤의 요청은 '표시'의 '의심 뒤 요청'으로 거른다
        valuesOf: (record) => {
            const gate = gateOf(record.locus);
            return gate ? [gate] : [];
        },
        order: GATES.map((gate) => gate.value),
        labelOf: (value) => GATES.find((gate) => gate.value === value)?.label ?? value,
    },
    { id: 'requester', label: '요청자', valuesOf: (record) => [record.userId] },
    {
        id: 'tool',
        label: '도구',
        valuesOf: (record) => (record.tool ? [record.tool] : []),
        labelOf: (value) => toolLabelOf(value),
    },
    {
        id: 'source',
        label: '출처',
        valuesOf: (record) => [record.source ?? 'web'],
        order: Object.keys(SOURCE_LABELS),
        labelOf: (value) => SOURCE_LABELS[value] ?? value,
    },
    {
        id: 'flag',
        label: '표시',
        valuesOf: flagsOf,
        order: Object.keys(FLAG_LABELS),
        labelOf: (value) => FLAG_LABELS[value] ?? value,
    },
];

const facetById = Object.fromEntries(FACETS.map((facet) => [facet.id, facet])) as Record<FacetId, FacetDef>;

// 기록이 고른 조건에 맞는가. except의 거르기는 보지 않는다 (그 거르기의 건수를 셀 때)
export function matches(record: AuditRecord, selection: Selection, except?: FacetId): boolean {
    for (const [id, chosen] of Object.entries(selection) as [FacetId, string[] | undefined][]) {
        if (id === except || !chosen?.length) continue;
        const values = facetById[id].valuesOf(record);
        if (!values.some((value) => chosen.includes(value))) return false;
    }
    return true;
}

export interface FacetValue {
    value: string;
    label: string;
    count: number;
    selected: boolean;
}

// 거르기 하나의 값 목록과 건수. 고른 값은 건수가 0이어도 남긴다 (다시 눌러 풀 수 있게)
export function facetValues(records: AuditRecord[], selection: Selection, facet: FacetDef): FacetValue[] {
    const counts = new Map<string, number>();
    const names = new Map<string, string>(); // 요청자: userId → 이메일·Slack 이름
    for (const record of records) {
        if (!matches(record, selection, facet.id)) continue;
        for (const value of facet.valuesOf(record)) {
            counts.set(value, (counts.get(value) ?? 0) + 1);
            if (facet.id === 'requester' && !names.has(value)) names.set(value, requesterOf(record));
        }
    }
    // 이름표에 쓸 요청자 이름은 거르기와 상관없이 모든 기록에서 찾는다 (고른 요청자의 건수가 0이 되어도 이름이 보이게)
    if (facet.id === 'requester')
        for (const record of records) if (!names.has(record.userId)) names.set(record.userId, requesterOf(record));

    const chosen = selection[facet.id] ?? [];
    for (const value of chosen) if (!counts.has(value)) counts.set(value, 0);
    const label = (value: string) => facet.labelOf?.(value) ?? names.get(value) ?? value;
    const list = [...counts].map(([value, count]) => ({ value, label: label(value), count, selected: chosen.includes(value) }));
    if (facet.order) {
        const rank = (value: string) => {
            const index = facet.order!.indexOf(value);
            return index < 0 ? facet.order!.length : index;
        };
        return list.sort((a, b) => rank(a.value) - rank(b.value) || b.count - a.count);
    }
    return list.sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

// 값 하나를 고르거나 푼다
export function toggleValue(selection: Selection, id: FacetId, value: string): Selection {
    const chosen = selection[id] ?? [];
    const next = chosen.includes(value) ? chosen.filter((v) => v !== value) : [...chosen, value];
    return { ...selection, [id]: next };
}

export const activeCount = (selection: Selection) =>
    Object.values(selection).reduce((sum, chosen) => sum + (chosen?.length ?? 0), 0);

// ---------------------------------------------------------------- 막대그래프 그룹 기준 (AuditHistogram)

export type GroupBy = 'result' | 'kind' | 'requester' | 'tool';
export const GROUP_OPTIONS: { value: GroupBy; label: string }[] = [
    { value: 'result', label: '결과' },
    { value: 'kind', label: '종류' },
    { value: 'requester', label: '요청자' },
    { value: 'tool', label: '도구' },
];

// ---------------------------------------------------------------- 검색창
//
// 낱말을 띄어 쓰면 모두 들어 있는 기록만(그리고), "따옴표"는 한 덩어리, 앞에 -를 붙이면 그 낱말이 없는 기록만.
// 대소문자는 가리지 않는다. 찾는 곳: 요청자(이메일·ID), 도구(이름·이름표), 요약, 질문, 입력값(리소스 ID 등),
// 오류·실행 결과, 결과·종류 이름, 각종 ID(질문·대화·도구 호출·작업·AWS 요청)
//   예: kim i-0428 실패      "보존 기간" -slack      6e8b2fdf-3c15 (질문 ID로 같은 질문의 기록)

export interface ParsedQuery {
    include: string[];
    exclude: string[];
}

export function parseQuery(text: string): ParsedQuery {
    const include: string[] = [];
    const exclude: string[] = [];
    for (const match of text.matchAll(/(-?)(?:"([^"]*)"|(\S+))/g)) {
        const word = (match[2] ?? match[3] ?? '').trim().toLowerCase();
        if (!word || word === '-') continue;
        (match[1] ? exclude : include).push(word);
    }
    return { include, exclude };
}

const searchCache = new WeakMap<AuditRecord, string>(); // 기록마다 한 번만 만든다 (2,000건을 글자마다 다시 만들지 않게)
const searchTextOf = (record: AuditRecord) => {
    let text = searchCache.get(record);
    if (text === undefined) {
        const input = typeof record.input === 'string' ? record.input : JSON.stringify(record.input ?? '');
        text = [
            requesterOf(record),
            record.userId,
            record.tool,
            record.tool ? labelOf(record.tool) : '',
            KIND_LABELS[record.kind],
            RESULT_LABELS[resultOf(record)],
            summaryOf(record),
            record.question,
            input,
            record.error,
            record.result,
            record.requestId,
            record.sessionId,
            record.toolUseId,
            record.actionId,
            record.awsRequestId,
            record.cloudTrailEvent,
            record.targetEmail,
            record.targetUser,
            // 표시 배지 (의심 문구 · 의심 뒤 요청 · 미등록 도구 · 가림)와 관문 이름: 배지·관문으로도 찾게
            ...flagsOf(record).map((flag) => FLAG_LABELS[flag] ?? flag),
            gateLabel(record.locus),
        ]
            .filter(Boolean)
            .join('\n')
            .toLowerCase();
        searchCache.set(record, text);
    }
    return text;
};

export function matchesQuery(record: AuditRecord, query: ParsedQuery): boolean {
    if (!query.include.length && !query.exclude.length) return true;
    const text = searchTextOf(record);
    return query.include.every((word) => text.includes(word)) && !query.exclude.some((word) => text.includes(word));
}
