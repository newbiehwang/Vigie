// 층 다이어그램: 7계층을 고리 모양의 순환 다이어그램으로 그린다. 도구 호출·변경 작업 행의 팝업창 맨 위에 보인다
// (질문·사용자 관리 행에는 층이 없다).
//
//   7계층 위치
//        ╭ 효과 ╮                     [5] 유입 Ingress
//     매개      유출 ▸               비용 조회의 결과가 모델에게 들어갔습니다
//    경계   유입    체류              ┌ 다른 계층에 남긴 흔적 ───────────────┐
//     ▸[유입]    판단                │ 3 체류  의심 문구가 든 결과를 읽은 뒤 … │
//        ╰────╯                      └──────────────────────────────┘
//
// - 고리는 조각 7개다. 조각 끝이 뾰족해(셰브런) 따로 화살표 없이 도는 방향이 보인다.
//   차례는 서버의 판정(services/llm/audit_trace.py)이 따지는 차례다 (맨 위 1 효과부터 시계 방향)
// - 이 기록의 계층(행의 locus)은 따로 강조하지 않는다. 처음 고른 계층이고, 설명 칸에 이 기록의 근거가 붙을 뿐이다
// - 조각의 색: 판정이 있으면 판정, 없으면 흔적이 있는 계층만 옅은 주황. 기록으로 남지 않는 계층(판단: 모델 안,
//   매개: 앱 밖)은 점선 테두리. 색은 고르는 것과 상관없이 그대로다
// - 고른 계층(처음에는 이 기록의 계층): 조각이 그림자와 함께 살짝 떠오르고 바탕이 조금 짙어진다. 다른 계층을 고르면
//   나머지 조각은 은은하게 옅어진다. 마우스를 올린 조각도 바탕이 조금 짙어진다. 가운데 원판과 오른쪽 설명 칸도 고른 계층을 보인다
// - 조각을 누르면(키보드는 Enter·Space) 그 계층을 고르고, 고른 조각을 다시 누르면 이 기록의 계층으로 돌아온다
// - 설명 칸: 계층의 설명 → 근거(이 기록이 그 계층에 있는 까닭을 값의 흐름으로, 파란 상자) → 그 계층의 흔적(주황 상자).
//   이 기록의 계층을 고르고 있으면 다른 계층에 남긴 흔적도 모아 보인다. 제목 옆에는 영어 이름을 회색으로 (고리 안은 한글만)
// - 이 기록의 층: 도구 반복이 정한다 (등록부에 없으면 경계, 변경 도구면 유출, 나머지는 유입. 변경 작업은 요청이 유출, 결정·실행이 효과)
// - 흔적: 체류(승인 요청 행의 taintedBy), 유입(도구 행의 의심 문구), 매개(실행한 AWS API의 요청 ID)
// - 판정 (변경 작업 행만, GET /audit?trace=, services/llm/audit_trace.py의 역추적): 팝업창을 열면 바로 받아 이 그림에 얹는다
//     제목 줄   7계층 위치                      ● 주의 3  ● 정상 3  ● 참고 1   ← 판정 개수 (나쁜 것부터, 이름표 없이)
//               질문 "로그대로 보존 기간 줄여줘"                              ← 이 변경을 낳은 사용자의 질문
//     고리      조각의 바탕색 = 그 계층의 판정 (정상 초록 · 주의 노랑 · 실패 빨강 · 참고 회색).
//               이때 흔적은 설명 칸에서만 보인다 (주황이 '주의'와 섞이지 않게)
//     설명 칸   제목 옆에 판정 배지, 그 계층에서 찾은 것과 근거 기록(시각 · 무엇).
//               서버의 물음은 계층 정의와 겹쳐 보이지 않는다
//   작업의 사건은 요청·승인·실행 행이 모두 같은 판정을 보인다 (같은 작업 ID)
import { useEffect, useState, type CSSProperties } from 'react';
import { fetchTrace } from '@/api/audit';
import type { AuditRecord, AuditTrace, TraceLayer, TraceStatus, TraceStep } from '@/types/audit';
import { KST } from './timeWindow';
import { ACTION_EVENTS, LAYERS, requesterOf, toolLabelOf } from './auditModel';

// ---------------------------------------------------------------- 판정
const STATUS_LABELS: Record<TraceStatus, string> = { ok: '정상', warn: '주의', fail: '실패', info: '참고' };
// 판정 배지 (공통 배지 components/badge.css): 실패 옅은 빨강 · 주의 옅은 노랑 · 정상 글자와 회청색 점 · 참고 옅은 점
const STATUS_BADGES: Record<TraceStatus, string> = { ok: 'is-quiet', warn: 'is-warn', fail: 'is-fail', info: 'is-quiet is-empty' };

// 판정 개수의 차례 (나쁜 것부터)
const STATUS_ORDER: TraceStatus[] = ['fail', 'warn', 'ok', 'info'];

// 근거가 된 기록 한 줄: 시각(한국 시간 시:분:초)과 무엇
const clockOf = (record: AuditRecord) => new Date(Date.parse(record.at.split('#')[0]) + KST).toISOString().slice(11, 19);
const whatOf = (record: AuditRecord) => {
    if (record.kind === 'request') return `질문: ${record.question ?? ''}`;
    if (record.kind === 'action') return `${ACTION_EVENTS[record.event ?? '']?.label ?? record.event}: ${record.summary ?? record.tool ?? ''}`;
    return `${toolLabelOf(record.tool)} 호출`;
};

type TraceState = { status: 'none' } | { status: 'loading' } | { status: 'error'; message: string } | { status: 'ready'; trace: AuditTrace };

// 변경 작업 행이면 판정을 받는다 (팝업창을 열 때 한 번. 기록이 바뀌면 부르는 쪽이 새로 그린다)
function useTrace(record: AuditRecord): TraceState {
    const actionId = record.kind === 'action' ? record.actionId : undefined;
    const [state, setState] = useState<TraceState>(actionId ? { status: 'loading' } : { status: 'none' });
    useEffect(() => {
        if (!actionId) return;
        let cancelled = false;
        fetchTrace(actionId, record.day)
            .then((trace) => !cancelled && setState({ status: 'ready', trace }))
            .catch((error) => {
                if (cancelled) return;
                const message = (error as { response?: { data?: { error?: string } } })?.response?.data?.error;
                setState({ status: 'error', message: message ?? '판정을 불러오지 못했습니다' });
            });
        return () => {
            cancelled = true;
        };
    }, [actionId, record.day]);
    return state;
}

// 설명 칸의 판정: 그 계층에서 찾은 것(서버의 답)과 근거가 된 기록
function TraceAnswer({ step, trace }: { step: TraceStep; trace: AuditTrace }) {
    const records = [...trace.events, ...trace.rows];
    const cited = step.evidence
        .map((at) => records.find((record) => record.at === at))
        .filter((record): record is AuditRecord => Boolean(record))
        .sort((a, b) => (a.at < b.at ? -1 : 1));
    return (
        <div className={`audit-trace-answer-box is-${step.status}`}>
            {/* 물음은 계층 정의와 겹쳐 보이지 않는다: 그 계층에서 찾은 것(답)과 근거 기록만 */}
            <p className="audit-trace-answer-a">{step.answer}</p>
            {cited.length ? (
                <ul className="audit-trace-cites" aria-label="근거가 된 기록">
                    {cited.map((record) => (
                        <li key={`${record.userId}|${record.at}`}>
                            <time>{clockOf(record)}</time>
                            <span>{whatOf(record)}</span>
                        </li>
                    ))}
                </ul>
            ) : null}
        </div>
    );
}

// 근거: 이 기록이 그 계층에 있는 까닭을 기록의 값으로 보인다. 문장 대신 흐름(단계 → 단계 → 단계)으로
//   유입  [도구 비용 조회] › [결과 3,449자] › [받는 곳 모델]
//   유출  [모델이 부름 로그 보존 기간 변경] › [위험도 변경] › [처리 승인 요청]
//   효과  [결정 승인] › [결정한 사람 kim@…] › [다음 실행]
// tone: 끝 단계의 뜻 (good 바뀌지 않음·성공, bad 실패, change AWS가 바뀜)
interface EvidenceStep {
    label: string;
    value: string;
    sub?: string; // 작은 고정폭 글자 (도구 이름·이벤트 이름). 길면 말줄임
    hint?: string; // 마우스를 올리면 뜨는 전체 글 (말줄임한 값의 원문)
    tone?: 'good' | 'bad' | 'change';
}

function evidenceOf(record: AuditRecord): EvidenceStep[] {
    const tool = toolLabelOf(record.tool);
    if (record.kind === 'tool') {
        if (record.locus === 'interface')
            return [
                { label: '모델이 부름', value: tool, sub: record.tool },
                { label: '위험도 등록부', value: '없음', tone: 'bad' },
                { label: '처리', value: '변경 도구로 다룸 · 승인 요청' },
            ];
        if (record.locus === 'egress')
            return [
                { label: '모델이 부름', value: tool, sub: record.tool },
                { label: '위험도', value: '변경' },
                { label: '처리', value: '실행하지 않고 승인 요청', tone: 'good' },
            ];
        return [
            { label: '도구', value: tool, sub: record.tool },
            record.status === 'error'
                ? { label: '결과', value: '오류', tone: 'bad' }
                : {
                      label: '결과',
                      value: record.resultChars !== undefined ? `${record.resultChars.toLocaleString()}자` : '받음',
                  },
            { label: '받는 곳', value: '모델' },
        ];
    }
    const who = requesterOf(record);
    switch (record.event) {
        case 'requested':
            return [
                { label: '변경 도구', value: tool, sub: record.tool },
                { label: '상태', value: '승인 대기' },
                { label: 'AWS', value: '바뀌지 않음', tone: 'good' },
            ];
        case 'approved':
            return [
                { label: '결정', value: '승인' },
                { label: '결정한 사람', value: who },
                { label: '다음', value: '실행' },
            ];
        case 'denied':
            return [
                { label: '결정', value: '거절' },
                { label: '결정한 사람', value: who },
                { label: 'AWS', value: '바뀌지 않음', tone: 'good' },
            ];
        case 'executed':
            return [
                { label: '실행', value: tool, sub: record.tool },
                { label: '결과', value: '성공' }, // CloudTrail 이벤트는 매개의 흔적에서 보인다
                { label: 'AWS', value: '바뀜', tone: 'change' },
            ];
        case 'failed':
            return [
                { label: '실행', value: tool, sub: record.tool },
                { label: '결과', value: '실패', tone: 'bad' },
                { label: 'AWS', value: '확인 필요' },
            ];
        default:
            return [];
    }
}

// 값의 흐름 (단계 사이에 ›). 근거는 파란 상자, 흔적은 주황 상자. 흐름이 여럿이면 한 상자 안에 줄로 쌓는다
function Evidence({ title, flows, variant }: { title: string; flows: EvidenceStep[][]; variant: 'here' | 'mark' }) {
    const shown = flows.filter((steps) => steps.length);
    if (!shown.length) return null;
    return (
        <div className={`audit-evidence is-${variant}`}>
            <span className="audit-evidence-title">{title}</span>
            {shown.map((steps, flowIndex) => (
            <ol key={flowIndex} className="audit-evidence-flow">
                {steps.map((step, index) => (
                    <li key={step.label} className={step.tone ? `is-${step.tone}` : undefined}>
                        {index > 0 ? (
                            <svg className="audit-evidence-arrow" viewBox="0 0 8 12" width="8" height="12" aria-hidden="true">
                                <path d="M1.5 1.5L6 6l-4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
                            </svg>
                        ) : null}
                        <span className="audit-evidence-step" title={step.hint}>
                            <span className="audit-evidence-label">{step.label}</span>
                            <span className="audit-evidence-value">{step.value}</span>
                            {step.sub ? <code className="audit-evidence-sub">{step.sub}</code> : null}
                        </span>
                    </li>
                ))}
            </ol>
            ))}
        </div>
    );
}

// 흔적: 이 기록이 다른 계층에 남긴 표시. 근거처럼 값의 흐름으로 (한 계층에 흐름이 여럿일 수 있다: 체류의 의심 결과가 여럿)
//   체류  [읽은 결과 로그 분석] › [의심 문구 3종] › [변경 요청 바로 다음 호출]
//   유입  [도구 결과 로그 분석] › [의심 문구 3종] › [처리 데이터로만 다룸]
//   매개  [AWS API PutRetentionPolicy] › [요청 ID …] › [대조 CloudTrail 이벤트]
type Marks = Partial<Record<TraceLayer, EvidenceStep[][]>>;

// 의심 문구: 종류 수와 첫 종류(외 N), 전체는 마우스를 올리면
const kindsStep = (kinds: string[]): EvidenceStep => ({
    label: '의심 문구',
    value: `${kinds.length}종`,
    sub: kinds.length > 1 ? `${kinds[0]} 외 ${kinds.length - 1}` : kinds[0],
    hint: kinds.join(', '),
    tone: 'bad',
});

function marksOf(record: AuditRecord): Marks {
    const marks: Marks = {};
    if (record.taintedBy?.length)
        marks.residence = record.taintedBy.map((seen) => [
            { label: '읽은 결과', value: toolLabelOf(seen.tool), sub: seen.toolUseId },
            kindsStep(seen.kinds),
            { label: '변경 요청', value: seen.callsAgo <= 1 ? '바로 다음 호출' : `${seen.callsAgo}번째 뒤 호출` },
        ]);
    if (Array.isArray(record.injectionSuspected) && record.injectionSuspected.length)
        marks.ingress = [
            [
                { label: '도구 결과', value: toolLabelOf(record.tool), sub: record.tool },
                kindsStep(record.injectionSuspected),
                { label: '처리', value: '데이터로만 다룸', tone: 'good' },
            ],
        ];
    if (record.awsRequestId)
        marks.mediation = [
            [
                { label: 'AWS API', value: record.cloudTrailEvent?.split(':').pop() ?? '—', sub: record.cloudTrailEvent?.split(':')[0] },
                { label: '요청 ID', value: `${record.awsRequestId.slice(0, 8)}…`, hint: record.awsRequestId },
                { label: '대조', value: 'CloudTrail 이벤트의 requestID' },
            ],
        ];
    return marks;
}

// ---------------------------------------------------------------- 고리의 모양
const SIZE = 240;
const C = SIZE / 2; // 가운데
const R_OUT = 112; // 바깥 반지름
const R_IN = 72; // 안쪽 반지름
const R_MID = (R_OUT + R_IN) / 2;
const SPAN = 360 / LAYERS.length; // 조각 하나의 각 (도)
const GAP = 2.2; // 조각 사이 틈 (도)
const TIP = 5; // 셰브런의 뾰족한 끝이 나아가는 각 (도)
const R_HUB = R_IN - 9; // 가운데 원판의 반지름
const POP = 4; // 고른 조각을 바깥으로 띄우는 거리

const rad = (deg: number) => (deg * Math.PI) / 180;
const at = (r: number, deg: number) => `${(C + r * Math.cos(rad(deg))).toFixed(2)} ${(C + r * Math.sin(rad(deg))).toFixed(2)}`;
const PAD = POP + 8; // 그림 둘레의 여백 (떠오른 조각과 그림자가 잘리지 않게)

// 조각 i의 가운데 각: 맨 위(-90°)에서 시계 방향
const midOf = (index: number) => -90 + index * SPAN;

// 셰브런 조각: 바깥 호 → 앞쪽 뾰족한 끝 → 안쪽 호 → 뒤쪽 오목한 홈
function segmentPath(index: number) {
    const a0 = midOf(index) - SPAN / 2 + GAP / 2;
    const a1 = a0 + SPAN - GAP;
    return [
        `M ${at(R_OUT, a0)}`,
        `A ${R_OUT} ${R_OUT} 0 0 1 ${at(R_OUT, a1)}`,
        `L ${at(R_MID, a1 + TIP)}`,
        `L ${at(R_IN, a1)}`,
        `A ${R_IN} ${R_IN} 0 0 0 ${at(R_IN, a0)}`,
        `L ${at(R_MID, a0 + TIP)}`,
        'Z',
    ].join(' ');
}

// 조각 안 글자의 자리 (셰브런 끝만큼 앞으로 치우친 가운데)
const labelAt = (index: number) => {
    const deg = midOf(index) + TIP / 2;
    return { x: C + R_MID * Math.cos(rad(deg)), y: C + R_MID * Math.sin(rad(deg)) };
};

export function AuditLayers({ record }: { record: AuditRecord }) {
    const hereIndex = LAYERS.findIndex((layer) => layer.id === record.locus);
    const here = LAYERS[hereIndex];
    // 고른 계층: 누른 조각, 누르기 전에는 이 기록의 계층
    const [picked, setPicked] = useState<TraceLayer | null>(null);
    const marks = marksOf(record);
    const selectedIndex = picked ? LAYERS.findIndex((layer) => layer.id === picked) : hereIndex;
    const selected = LAYERS[selectedIndex];
    const selectedIsHere = selectedIndex === hereIndex;
    const otherMarks = LAYERS.filter((layer) => marks[layer.id] && layer.id !== here?.id);
    // 판정 (변경 작업 행): 계층마다 정상·주의·실패·참고
    const traceState = useTrace(record);
    const trace = traceState.status === 'ready' ? traceState.trace : null;
    const stepOf = (id: TraceLayer) => trace?.steps.find((step) => step.layer === id);
    const selectedStep = selected ? stepOf(selected.id) : undefined;
    // 판정 개수 (나쁜 것부터, 없는 판정은 뺀다)
    const counts = trace
        ? STATUS_ORDER.map((status) => ({ status, n: trace.steps.filter((step) => step.status === status).length })).filter(
              (count) => count.n,
          )
        : [];
    // 조각의 색: 판정이 있으면 판정, 없으면 흔적(주황)만. 이 기록의 계층을 따로 강조하지 않는다
    // (처음 고른 계층일 뿐이다). 판정이 있으면 흔적은 설명 칸에서만 보인다 (주황이 '주의'와 섞이지 않게)
    const toneOf = (index: number) => (index !== hereIndex && !trace && marks[LAYERS[index].id] ? 'is-marked' : '');

    return (
        <section className="audit-layers" aria-labelledby="audit-layers-title">
            <div className="audit-layers-head">
                <h4 id="audit-layers-title" className="audit-layers-title">
                    7계층 위치
                </h4>
                {/* 판정 개수 (변경 작업 행): ● 주의 3  ● 정상 3  ● 참고 1 */}
                {counts.length ? (
                    <ul className="audit-counts" aria-label="판정 개수">
                        {counts.map(({ status, n }) => (
                            <li key={status} className={`is-${status}`}>
                                <span className="audit-counts-dot" aria-hidden="true" />
                                {STATUS_LABELS[status]} <strong>{n}</strong>
                            </li>
                        ))}
                    </ul>
                ) : traceState.status === 'loading' ? (
                    <span className="audit-counts-note" role="status">
                        <span className="plan-inline-spinner" aria-hidden="true" />
                        판정을 불러오는 중…
                    </span>
                ) : traceState.status === 'error' ? (
                    <span className="audit-counts-note is-error" role="alert">
                        {traceState.message}
                    </span>
                ) : null}
            </div>
            {trace?.question ? <p className="audit-layers-question">질문 “{trace.question}”</p> : null}

            <div className="audit-cycle">
                <div className="audit-cycle-figure">
                <svg
                    className={`audit-cycle-svg${picked ? ' has-pick' : ''}`}
                    viewBox={`${-PAD} ${-PAD} ${SIZE + PAD * 2} ${SIZE + PAD * 2}`}
                    role="group"
                    aria-label="7계층. 계층을 누르면 설명이 나옵니다"
                >
                    {LAYERS.map((layer, index) => {
                        const tone = toneOf(index);
                        const offRecord = layer.recorded === 'none' || layer.recorded === 'outside';
                        const isSelected = index === selectedIndex;
                        const mid = rad(midOf(index));
                        const label = labelAt(index);
                        const step = stepOf(layer.id);
                        const state = [
                            index === hereIndex ? '이 기록의 계층' : tone === 'is-marked' ? '흔적' : offRecord ? '기록으로 남지 않음' : '',
                            step ? `판정 ${STATUS_LABELS[step.status]}` : '',
                        ]
                            .filter(Boolean)
                            .join(', ');
                        // 누르면 그 계층을 고르고, 고른 조각을 다시 누르면 이 기록의 계층으로 돌아온다
                        const choose = () => setPicked(isSelected || index === hereIndex ? null : layer.id);
                        return (
                            <g
                                key={layer.id}
                                className={`audit-cycle-seg ${tone}${offRecord ? ' is-off-record' : ''}${
                                    step ? ` has-status is-${step.status}` : ''
                                }${isSelected ? ' is-selected' : ''}`}
                                // 고른 조각은 가운데에서 바깥으로 밀어낸다 (--dx·--dy: CSS가 transform으로)
                                style={
                                    {
                                        '--dx': `${(POP * Math.cos(mid)).toFixed(2)}px`,
                                        '--dy': `${(POP * Math.sin(mid)).toFixed(2)}px`,
                                        animationDelay: `${index * 45}ms`,
                                    } as CSSProperties
                                }
                                tabIndex={0}
                                role="button"
                                aria-pressed={isSelected}
                                aria-label={`${index + 1} ${layer.label} (${layer.en})${state ? `, ${state}` : ''}`}
                                onClick={choose}
                                onKeyDown={(event) => {
                                    if (event.key !== 'Enter' && event.key !== ' ') return;
                                    event.preventDefault(); // Space로 본문이 스크롤되지 않게
                                    choose();
                                }}
                            >
                                <path className="audit-cycle-shape" d={segmentPath(index)} />
                                <text className="audit-cycle-no" x={label.x} y={label.y - 7} textAnchor="middle">
                                    {index + 1}
                                </text>
                                <text className="audit-cycle-label" x={label.x} y={label.y + 9} textAnchor="middle">
                                    {layer.label}
                                </text>
                            </g>
                        );
                    })}
                    {/* 가운데: 옅은 원판 위에 고른 계층의 이름. 고르면 바뀌며 살짝 떠오른다 */}
                    <circle className="audit-cycle-hub" cx={C} cy={C} r={R_HUB} aria-hidden="true" />
                    {selected ? (
                        <g key={selected.id} className="audit-cycle-center" aria-hidden="true">
                            <text x={C} y={C + 6.5} textAnchor="middle" className="audit-cycle-name">
                                {selected.label}
                            </text>
                        </g>
                    ) : null}
                </svg>
                </div>

                {/* 설명 칸 (aria-live: 다른 계층을 고르면 화면 읽기 프로그램이 새 설명을 읽는다) */}
                {selected ? (
                    <div className="audit-cycle-panel" aria-live="polite" key={selected.id}>
                        <div className="audit-cycle-panel-head">
                            <span className={`audit-cycle-panel-no ${toneOf(selectedIndex)}`}>{selectedIndex + 1}</span>
                            <strong>{selected.label}</strong>
                            <span className="audit-cycle-en">{selected.en}</span>
                            {selectedStep ? (
                                <span className={`badge audit-trace-badge ${STATUS_BADGES[selectedStep.status]}`}>{STATUS_LABELS[selectedStep.status]}</span>
                            ) : null}
                        </div>
                        <p className="audit-cycle-panel-text">{selected.description}</p>
                        {/* 판정: 이 계층에서 찾은 것과 근거 기록 (변경 작업 행) */}
                        {trace && selectedStep ? <TraceAnswer step={selectedStep} trace={trace} /> : null}
                        {selectedIsHere ? <Evidence title="근거" flows={[evidenceOf(record)]} variant="here" /> : null}
                        {/* 이 계층의 흔적 */}
                        {marks[selected.id] ? <Evidence title="흔적" flows={marks[selected.id]!} variant="mark" /> : null}
                        {/* 이 기록의 계층을 보고 있으면 다른 계층에 남긴 흔적도 (계층마다 한 상자) */}
                        {selectedIsHere
                            ? otherMarks.map((layer) => (
                                  <Evidence
                                      key={layer.id}
                                      title={`흔적 · ${LAYERS.indexOf(layer) + 1} ${layer.label}`}
                                      flows={marks[layer.id]!}
                                      variant="mark"
                                  />
                              ))
                            : null}
                    </div>
                ) : null}
            </div>
        </section>
    );
}
