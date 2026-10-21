// 관문 그림: 변경이 지나는 길을 관문 셋(경계 › 유입 › 실행)으로 그린다. 도구 호출·변경 작업 행의 팝업창 맨 위에 보인다
// (질문·사용자 관리 행에는 관문이 없다).
//
//   관문                                         ● 주의 2  ● 정상 1
//   질문 "최근 오류 로그 보여줘"
//   [ 1 경계 ]›[ 2 유입 ]›[ 3 실행 ]›
//   ┆ 가정 · 판단  모델 안은 볼 수 없어 …  ┆  ┆ 전제 · 매개  모든 변경은 MCP 역할로만 … ┆
//   [2] 유입 Ingress                                                  주의
//   무엇을 읽었고, 의심 문구가 있었나. 그 뒤 변경을 요청했나
//   ┌ 판정: 찾은 것마다 한 줄 (점 색 = 그 줄의 판정) ┐  ┌ 근거 › › ┐  ┌ 흔적 › › ┐
//
// - 관문 셋은 판정이 실제로 갈리는 자리다 (services/llm/audit_trace.py 모듈 설명). 예전의 처리 단계 일곱 개 중
//   판단(모델 안)은 언제나 추론이고 매개(CloudTrail)는 언제나 앱 밖이라, 관문이 아니라 가정 · 전제로 아래에 둔다.
//   체류(의심 결과를 읽은 뒤의 요청)는 유입의 조건, 유출 · 효과는 같은 승인 관문의 앞뒤라 실행 하나로 묶었다
// - 조각은 셰브런(오른쪽이 뾰족해 흐르는 방향이 보인다). 이 기록의 관문(행의 locus)을 처음 고른 채로 연다
// - 조각의 색: 판정이 있으면 판정(정상 회색 · 주의 노랑 · 실패 빨강 · 참고 흰색), 없으면 흔적이 있는 관문만 옅은 파랑
// - 고른 조각은 살짝 떠오르고, 다른 관문을 고르면 나머지는 옅어진다. 고른 조각을 다시 누르면 이 기록의 관문으로 돌아온다
// - 판정 (변경 작업 행만, GET /audit?trace=): 팝업창을 열면 바로 받아 이 그림에 얹는다. 요청·승인·실행 행은 같은 판정
import { useEffect, useState, type CSSProperties } from 'react';
import { fetchTrace } from '@/api/audit';
import type { AuditGate, AuditRecord, AuditTrace, TraceBasis, TraceStatus, TraceStep } from '@/types/audit';
import { KST } from './timeWindow';
import { ACTION_EVENTS, GATES, gateOf, requesterOf, toolLabelOf } from './auditModel';

// ---------------------------------------------------------------- 판정
const STATUS_LABELS: Record<TraceStatus, string> = { ok: '정상', warn: '주의', fail: '실패', info: '참고' };
// 판정 배지 (공통 배지 components/badge.css): 실패 빨강 · 주의 노랑 · 정상 파랑 · 참고 옅은 점
const STATUS_BADGES: Record<TraceStatus, string> = { ok: 'is-ok', warn: 'is-warn', fail: 'is-fail', info: 'is-quiet is-empty' };
// 판정 개수의 차례 (나쁜 것부터)
const STATUS_ORDER: TraceStatus[] = ['fail', 'warn', 'ok', 'info'];

// 관문이 아닌 둘: 판정하지 않고 글로만 보인다 (서버가 판정을 주기 전에도, 도구 호출 행에서도 같은 글)
const ASSUMPTION = '모델 안은 볼 수 없어, 언제든 도구 결과 속 지시에 속을 수 있다고 가정합니다. 그래서 변경은 언제나 사람의 승인을 거칩니다';
const PREMISE = '모든 변경은 MCP 역할로만 나간다는 전제입니다. 앱 밖(IAM · CloudTrail)이라 판정하지 않고, 실행한 AWS API의 요청 ID로 대조합니다';

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

// 판정의 근거 (services/llm/audit_trace.py 모듈 설명): 기록은 코드가 확정하는 사실, 탐지는 문구 패턴이라 놓칠 수 있다
const BASIS_LABELS: Record<TraceBasis, string> = { record: '기록', detection: '탐지' };
const BASIS_HINTS: Record<TraceBasis, string> = {
    record: '코드가 확정하는 사실 (승인 · 실행 · 요청 기록, 도구 등록부)',
    detection: '도구 결과의 문구 패턴 탐지. 패턴에 없는 표현은 놓칠 수 있습니다',
};

// 설명 칸의 판정: 그 관문에서 찾은 것(줄마다 판정 점), 판정의 근거, 근거가 된 기록
//   탐지를 근거로 한 '정상'은 '탐지된 것이 없다'는 뜻이라 한 줄을 덧붙인다
function TraceAnswer({ step, trace }: { step: TraceStep; trace: AuditTrace }) {
    const records = [...trace.events, ...trace.rows];
    const cited = step.evidence
        .map((at) => records.find((record) => record.at === at))
        .filter((record): record is AuditRecord => Boolean(record))
        .sort((a, b) => (a.at < b.at ? -1 : 1));
    return (
        <div className={`audit-trace-answer-box is-${step.status}`}>
            {step.findings?.length ? (
                <ul className="audit-trace-findings">
                    {step.findings.map((finding) => (
                        <li key={finding.text} className={`is-${finding.status}`}>
                            <span className="audit-trace-finding-dot" aria-label={STATUS_LABELS[finding.status]} />
                            <span>{finding.text}</span>
                        </li>
                    ))}
                </ul>
            ) : (
                <p className="audit-trace-answer-a">{step.answer}</p>
            )}
            {step.basis?.length ? (
                <p className="audit-trace-basis">
                    <span className="audit-trace-basis-title">근거</span>
                    {step.basis.map((basis) => (
                        <span key={basis} className={`audit-trace-basis-chip is-${basis}`} title={BASIS_HINTS[basis]}>
                            {BASIS_LABELS[basis]}
                        </span>
                    ))}
                    {step.status === 'ok' && step.basis.includes('detection') ? (
                        <span className="audit-trace-basis-note">탐지가 놓친 표현은 여기에 드러나지 않습니다</span>
                    ) : null}
                </p>
            ) : null}
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

// 근거: 이 기록이 그 관문에 있는 까닭을 기록의 값으로 보인다. 문장 대신 흐름(단계 › 단계 › 단계)으로
//   유입  [도구 비용 조회] › [결과 3,449자] › [받는 곳 모델]
//   실행  [모델이 부름 로그 보존 기간 변경] › [위험도 변경] › [처리 승인 요청]
//         [결정 승인] › [결정한 사람 kim@…] › [다음 실행]
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
                { label: '결과', value: '성공' }, // CloudTrail 이벤트는 아래 전제(매개)에서 보인다
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

// 값의 흐름 (단계 사이에 ›). 근거는 파란 상자, 흔적은 흰 상자. 흐름이 여럿이면 한 상자 안에 줄로 쌓는다
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

// 의심 문구: 종류 수와 첫 종류(외 N), 전체는 마우스를 올리면
const kindsStep = (kinds: string[]): EvidenceStep => ({
    label: '의심 문구',
    value: `${kinds.length}종`,
    sub: kinds.length > 1 ? `${kinds[0]} 외 ${kinds.length - 1}` : kinds[0],
    hint: kinds.join(', '),
    tone: 'bad',
});

// 흔적: 이 기록이 유입 관문에 남긴 표시 (도구 결과의 의심 문구, 승인 요청 행의 '의심 뒤 요청')
function ingressMarksOf(record: AuditRecord): EvidenceStep[][] {
    const flows: EvidenceStep[][] = [];
    if (Array.isArray(record.injectionSuspected) && record.injectionSuspected.length)
        flows.push([
            { label: '도구 결과', value: toolLabelOf(record.tool), sub: record.tool },
            kindsStep(record.injectionSuspected),
            { label: '처리', value: '데이터로만 다룸', tone: 'good' },
        ]);
    for (const seen of record.taintedBy ?? [])
        flows.push([
            { label: '읽은 결과', value: toolLabelOf(seen.tool), sub: seen.toolUseId },
            kindsStep(seen.kinds),
            { label: '변경 요청', value: seen.callsAgo <= 1 ? '바로 다음 호출' : `${seen.callsAgo}번째 뒤 호출` },
        ]);
    return flows;
}

// 전제(매개)의 단서: 실행한 AWS API의 요청 ID = CloudTrail 이벤트의 requestID
const premiseFlowOf = (record: AuditRecord): EvidenceStep[] | null =>
    record.awsRequestId
        ? [
              { label: 'AWS API', value: record.cloudTrailEvent?.split(':').pop() ?? '—', sub: record.cloudTrailEvent?.split(':')[0] },
              { label: '요청 ID', value: `${record.awsRequestId.slice(0, 8)}…`, hint: record.awsRequestId },
              { label: '대조', value: 'CloudTrail 이벤트의 requestID' },
          ]
        : null;

// ---------------------------------------------------------------- 셰브런의 모양
const W = 600; // 그림 폭 (viewBox)
const H = 56; // 조각 높이
const TIP = 16; // 셰브런의 뾰족한 끝이 나아가는 거리
const GAP = 6; // 조각 사이 틈
const POP = 3; // 고른 조각을 위로 띄우는 거리
const PAD = POP + 6; // 그림 둘레의 여백 (떠오른 조각과 그림자가 잘리지 않게)
const SEG = (W - TIP - GAP * (GATES.length - 1)) / GATES.length; // 조각 하나의 몸통 폭

// 조각 i: 왼쪽은 오목한 홈(첫 조각은 반듯하게), 오른쪽은 뾰족한 끝
function segmentPath(index: number) {
    const x0 = index * (SEG + GAP);
    const x1 = x0 + SEG;
    const notch = index === 0 ? 0 : TIP;
    return `M ${x0} 0 H ${x1} L ${x1 + TIP} ${H / 2} L ${x1} ${H} H ${x0} L ${x0 + notch} ${H / 2} Z`;
}
const labelX = (index: number) => index * (SEG + GAP) + (SEG + TIP + (index === 0 ? 0 : TIP)) / 2;

export function AuditGates({ record }: { record: AuditRecord }) {
    const here = gateOf(record.locus);
    const hereIndex = GATES.findIndex((gate) => gate.value === here);
    // 고른 관문: 누른 조각, 누르기 전에는 이 기록의 관문
    const [picked, setPicked] = useState<AuditGate | null>(null);
    const selectedIndex = picked ? GATES.findIndex((gate) => gate.value === picked) : hereIndex;
    const selected = GATES[selectedIndex];
    const marks = ingressMarksOf(record);
    const premiseFlow = premiseFlowOf(record);
    // 판정 (변경 작업 행): 관문마다 정상·주의·실패·참고
    const traceState = useTrace(record);
    const trace = traceState.status === 'ready' ? traceState.trace : null;
    const stepOf = (gate: AuditGate) => trace?.steps.find((step) => step.gate === gate);
    const selectedStep = selected ? stepOf(selected.value) : undefined;
    const counts = trace
        ? STATUS_ORDER.map((status) => ({ status, n: trace.steps.filter((step) => step.status === status).length })).filter(
              (count) => count.n,
          )
        : [];
    // 조각의 색: 판정이 있으면 판정, 없으면 흔적(옅은 파랑)만. 이 기록의 관문을 따로 강조하지 않는다 (처음 고른 관문일 뿐)
    const markedIndex = GATES.findIndex((gate) => gate.value === 'ingress');
    const toneOf = (index: number) => (index !== hereIndex && !trace && index === markedIndex && marks.length ? 'is-marked' : '');

    return (
        <section className="audit-layers" aria-labelledby="audit-layers-title">
            <div className="audit-layers-head">
                <h4 id="audit-layers-title" className="audit-layers-title">
                    관문
                </h4>
                {/* 판정 개수 (변경 작업 행): ● 주의 2  ● 정상 1 */}
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

            <div className="audit-gates">
                <svg
                    className={`audit-cycle-svg audit-gates-svg${picked ? ' has-pick' : ''}`}
                    viewBox={`${-PAD} ${-PAD} ${W + PAD * 2} ${H + PAD * 2}`}
                    role="group"
                    aria-label="관문. 관문을 누르면 설명이 나옵니다"
                >
                    {GATES.map((gate, index) => {
                        const tone = toneOf(index);
                        const isSelected = index === selectedIndex;
                        const step = stepOf(gate.value);
                        const state = [
                            index === hereIndex ? '이 기록의 관문' : tone ? '흔적' : '',
                            step ? `판정 ${STATUS_LABELS[step.status]}` : '',
                        ]
                            .filter(Boolean)
                            .join(', ');
                        // 누르면 그 관문을 고르고, 고른 조각을 다시 누르면 이 기록의 관문으로 돌아온다
                        const choose = () => setPicked(isSelected || index === hereIndex ? null : gate.value);
                        return (
                            <g
                                key={gate.value}
                                className={`audit-cycle-seg ${tone}${step ? ` has-status is-${step.status}` : ''}${
                                    isSelected ? ' is-selected' : ''
                                }`}
                                style={{ '--dx': '0px', '--dy': `${-POP}px`, animationDelay: `${index * 60}ms` } as CSSProperties}
                                tabIndex={0}
                                role="button"
                                aria-pressed={isSelected}
                                aria-label={`${index + 1} ${gate.label} (${gate.en})${state ? `, ${state}` : ''}`}
                                onClick={choose}
                                onKeyDown={(event) => {
                                    if (event.key !== 'Enter' && event.key !== ' ') return;
                                    event.preventDefault(); // Space로 본문이 스크롤되지 않게
                                    choose();
                                }}
                            >
                                <path className="audit-cycle-shape" d={segmentPath(index)} />
                                <text className="audit-cycle-no" x={labelX(index)} y={H / 2 - 6} textAnchor="middle">
                                    {index + 1}
                                </text>
                                <text className="audit-cycle-label" x={labelX(index)} y={H / 2 + 11} textAnchor="middle">
                                    {gate.label}
                                </text>
                            </g>
                        );
                    })}
                </svg>

                {/* 관문이 아닌 둘: 판단은 가정, 매개는 전제 (점선 상자) */}
                <div className="audit-gate-notes">
                    <p className="audit-gate-note">
                        <span className="audit-gate-note-tag">가정 · 판단</span>
                        {trace?.assumption ?? ASSUMPTION}
                    </p>
                    <div className="audit-gate-note">
                        <span className="audit-gate-note-tag">전제 · 매개</span>
                        <span>{trace?.premise?.text ?? PREMISE}</span>
                        {premiseFlow ? <Evidence title="대조할 단서" flows={[premiseFlow]} variant="mark" /> : null}
                    </div>
                </div>

                {/* 설명 칸 (aria-live: 다른 관문을 고르면 화면 읽기 프로그램이 새 설명을 읽는다) */}
                {selected ? (
                    <div className="audit-cycle-panel" aria-live="polite" key={selected.value}>
                        <div className="audit-cycle-panel-head">
                            <span className={`audit-cycle-panel-no ${toneOf(selectedIndex)}`}>{selectedIndex + 1}</span>
                            <strong>{selected.label}</strong>
                            <span className="audit-cycle-en">{selected.en}</span>
                            {selectedStep ? (
                                <span className={`badge audit-trace-badge ${STATUS_BADGES[selectedStep.status]}`}>{STATUS_LABELS[selectedStep.status]}</span>
                            ) : null}
                        </div>
                        <p className="audit-cycle-panel-text">{selected.description}</p>
                        {trace && selectedStep ? <TraceAnswer step={selectedStep} trace={trace} /> : null}
                        {selectedIndex === hereIndex ? <Evidence title="근거" flows={[evidenceOf(record)]} variant="here" /> : null}
                        {selected.value === 'ingress' && marks.length ? <Evidence title="흔적" flows={marks} variant="mark" /> : null}
                    </div>
                ) : null}
            </div>
        </section>
    );
}
