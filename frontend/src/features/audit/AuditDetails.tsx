// 감사 기록 한 건을 자세히 보이는 조각들: 항목 표(Details, 맨 위에 7계층 위치와 역추적), 결과 배지, 종류 이름, 표시 배지.
// 목록 행(AuditPage)과 기록 팝업창(AuditDetailModal)이 같이 쓴다
import { Fragment, type ReactNode } from 'react';
import type { AuditRecord } from '@/types/audit';
import { labelOf } from '@/utils/toolTrace';
import { AuditLayers } from './AuditLayers';
import {
    ACTION_EVENTS,
    ADMIN_EVENTS,
    GROUP_NAMES,
    kindTextOf,
    REDACTED_LABELS,
    redactedTotal,
    roleText,
    TOKEN_LABELS,
    secondsOf,
    suspiciousOf,
    tokenTotal,
    tokensText,
    usdText,
} from './auditModel';

export function Details({ record }: { record: AuditRecord }) {
    const rows: [string, ReactNode][] = [];
    if (record.kind === 'tool') {
        rows.push(['도구 이름', <code key="tool">{record.tool}</code>]);
        rows.push([
            '입력',
            <pre key="input" className="audit-code">
                {typeof record.input === 'string' ? record.input : JSON.stringify(record.input ?? {}, null, 2)}
            </pre>,
        ]);
        if (record.resultChars !== undefined) rows.push(['결과 크기', `${record.resultChars.toLocaleString()}자`]);
        if (Array.isArray(record.injectionSuspected) && record.injectionSuspected.length)
            rows.push(['의심 문구', `지시문처럼 보이는 문구 (${record.injectionSuspected.join(', ')}). 데이터로만 다뤘습니다`]);
    } else if (record.kind === 'action') {
        rows.push(['사건', ACTION_EVENTS[record.event ?? '']?.label ?? record.event ?? '']);
        rows.push(['변경 내용', record.summary ?? '']);
        rows.push(['도구 이름', <code key="tool">{record.tool}</code>]);
        rows.push([
            '실행될 값',
            <pre key="input" className="audit-code">
                {typeof record.input === 'string' ? record.input : JSON.stringify(record.input ?? {}, null, 2)}
            </pre>,
        ]);
        if (record.taintedBy?.length)
            rows.push([
                '먼저 읽은 의심 결과',
                <ul key="tainted" className="audit-tainted">
                    {record.taintedBy.map((seen) => (
                        <li key={seen.toolUseId}>
                            {labelOf(seen.tool)} 결과 ·{' '}
                            {seen.callsAgo <= 1 ? '바로 다음 호출' : `${seen.callsAgo}번째 뒤 호출`}에서 요청 ·{' '}
                            <span className="audit-muted">{seen.kinds.join(', ')}</span> ·{' '}
                            <code>{seen.toolUseId}</code>
                        </li>
                    ))}
                </ul>,
            ]);
        if (record.decidedBy) rows.push(['결정한 사람', <code key="by">{record.decidedBy}</code>]);
        if (record.result) rows.push(['실행 결과', record.result]);
        if (record.awsRequestId)
            rows.push([
                'CloudTrail',
                <span key="trail">
                    {record.cloudTrailEvent} · 요청 ID <code>{record.awsRequestId}</code>
                    <span className="audit-muted"> (CloudTrail 이벤트의 requestID와 같습니다)</span>
                </span>,
            ]);
        if (record.actionId) rows.push(['작업 ID', <code key="action">{record.actionId}</code>]);
    } else if (record.kind === 'admin') {
        rows.push(['사건', ADMIN_EVENTS[record.event ?? ''] ?? record.event ?? '']);
        rows.push(['대상', record.targetEmail ?? '']);
        if (record.targetUser) rows.push(['대상 사용자 이름', <code key="target">{record.targetUser}</code>]);
        if (record.toRole) rows.push(['권한', `${roleText(record.fromRole)} → ${roleText(record.toRole)}`]);
        if (record.group) rows.push(['그룹', `${GROUP_NAMES[record.group] ?? record.group} (${record.group})`]);
        if (record.decidedBy) rows.push(['바꾼 사람', <code key="by">{record.decidedBy}</code>]);
        if (record.status === 'error')
            rows.push(['결과', '바꾸지 못했습니다 (바로 앞의 같은 사건 행은 시도를 기록한 것입니다)']);
    } else {
        // 질문과 답변은 표 위에 대화창 모양으로 보인다 (AuditConversation)
        if (record.model) rows.push(['모델', <code key="model">{record.model}</code>]);
        // 쓴 토큰과 예상 비용 (services/llm/llm_cost.py). 토큰을 남기기 전의 질문에는 없다
        if (record.tokens) {
            rows.push([
                '토큰',
                <span key="tokens">
                    {tokensText(record.tokens)}{' '}
                    <span className="audit-muted">(합계 {tokenTotal(record.tokens).toLocaleString()})</span>
                </span>,
            ]);
            if (record.modelCalls?.length)
                rows.push([
                    '모델 호출',
                    <div key="calls">
                        {record.modelCalls.length}번
                        <ol className="audit-model-calls">
                            {record.modelCalls.map((call, index) => (
                                <li key={index}>{tokensText(call)}</li>
                            ))}
                        </ol>
                    </div>,
                ]);
            const price = record.price;
            rows.push([
                '예상 비용',
                record.costMicroUsd !== undefined ? (
                    <span key="cost">
                        {usdText(record.costMicroUsd)}
                        {price ? (
                            <span className="audit-muted">
                                {' '}
                                (단가, 백만 토큰당:{' '}
                                {TOKEN_LABELS.map(([kind, label]) => `${label} $${price[kind]}`).join(' · ')})
                            </span>
                        ) : null}
                    </span>
                ) : (
                    <span key="cost" className="audit-muted">
                        단가표에 없는 모델이라 계산하지 않았습니다
                    </span>
                ),
            ]);
        }
        rows.push(['도구 호출', `${record.toolCount ?? 0}번`]);
        if (record.injectionSuspected) rows.push(['의심 문구가 든 도구 결과', `${record.injectionSuspected}건`]);
        const redacted = Object.entries(record.redacted ?? {});
        rows.push([
            'Claude로 보내기 전에 가린 값',
            redacted.length
                ? redacted.map(([kind, count]) => `${REDACTED_LABELS[kind] ?? kind} ${count}개`).join(', ')
                : '없음',
        ]);
    }
    if (record.error) rows.push(['오류', <span key="error" className="audit-error-text">{record.error}</span>]);
    if (record.ms !== undefined) rows.push(['걸린 시간', secondsOf(record.ms)]);
    rows.push(['요청자 ID', <code key="user">{record.userId}</code>]);
    if (record.requestId) rows.push(['질문 ID', <code key="request">{record.requestId}</code>]);
    if (record.sessionId) rows.push(['대화 ID', <code key="session">{record.sessionId}</code>]);
    if (record.toolUseId) rows.push(['도구 호출 ID', <code key="use">{record.toolUseId}</code>]);

    return (
        <>
            {/* 층: 7계층에서 이 기록의 자리를 맨 위에 (도구 호출·변경 작업. 질문·사용자 관리 행에는 층이 없다) */}
            {record.locus ? <AuditLayers record={record} /> : null}
            <dl className="audit-details">
                {rows.map(([name, value]) => (
                    <Fragment key={name}>
                        <dt>{name}</dt>
                        <dd>{value}</dd>
                    </Fragment>
                ))}
            </dl>
        </>
    );
}

// 결과 열: 변경 작업은 사건(승인 요청·승인·거절·실행·실패), 나머지는 성공·실패
export function ResultBadge({ record }: { record: AuditRecord }) {
    const event = record.kind === 'action' ? ACTION_EVENTS[record.event ?? ''] : undefined;
    if (event) return <span className={`badge ${event.className}`}>{event.label}</span>;
    const failed = record.status === 'error';
    return <span className={`badge ${failed ? 'is-fail' : 'is-quiet'}`}>{failed ? '실패' : '성공'}</span>;
}

// 도구 칸: 질문·변경 작업·사용자 관리는 종류를, 도구 호출은 도구 이름을 보인다 (목록 행과 기록 팝업창 제목이 같이 쓴다).
// 종류마다 색을 달리하지 않는다: 목록·팝업창 모두 둘레 글자색(검정)을 따른다
export function KindLabel({ record }: { record: AuditRecord }) {
    return <>{kindTextOf(record)}</>;
}

// 표시 칸: Slack · 의심 문구 · 의심 뒤 요청 · 미등록 도구 · 가림 (목록 행과 기록 팝업창 머리가 같이 쓴다)
export function Flags({ record }: { record: AuditRecord }) {
    const redacted = redactedTotal(record);
    return (
        <>
            {record.source === 'slack' ? <span className="audit-flag">Slack</span> : null}
            {suspiciousOf(record) ? (
                <span className="audit-flag is-suspicious" title="도구 결과에 지시문처럼 보이는 문구가 있었습니다">
                    의심 문구
                </span>
            ) : null}
            {record.taintedBy?.length ? (
                <span
                    className="audit-flag is-suspicious"
                    title="의심 문구가 든 도구 결과를 읽은 뒤 같은 질문에서 요청한 변경입니다 (체류)"
                >
                    의심 뒤 요청
                </span>
            ) : null}
            {record.locus === 'interface' ? (
                <span className="audit-flag is-suspicious" title="위험도 등록부에 없는 도구입니다 (경계)">
                    미등록 도구
                </span>
            ) : null}
            {redacted ? (
                <span className="audit-flag" title="Claude로 보내기 전에 가린 값의 수">
                    가림 {redacted}
                </span>
            ) : null}
        </>
    );
}
