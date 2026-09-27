// 변경 작업 승인 카드: AI가 AWS를 바꾸려 할 때 답변 아래에 나온다 (services/llm/approvals.py).
//
// 승인을 기다리는 동안
//   [승인 필요] 로그 보존 기간 변경                               남은 시간 9:41
//   대상   /aws/lambda/vigie-llm-dev
//   변경   30일(회색 취소선) → 14일(검정 굵게)
//   영향   지난 로그 일부가 지워질 수 있습니다
//   setLogRetention(log_group_name="/aws/lambda/vigie-llm-dev", retention_days=14)   ← 승인하면 실제로 실행될 호출
//                                                         [거절] [승인하고 실행]
// 앞서 읽은 도구 결과에 의심 문구가 있었으면 (체류 신호)
//   ┌ ⚠ 앞서 읽은 도구 결과에 지시문처럼 보이는 문구가 있었습니다 ┐   ← 카드 맨 위의 빨간 띠
//   │ • 로그 분석 (Insights) 결과 · 바로 다음 호출에서 이 변경을 요청 │
//   └ 사용자가 원한 변경인지 … 확인한 뒤 승인하세요.                ┘
//   … (위와 같은 본문)
//   ☐ 내가 요청한 변경이 맞습니다                          [거절] [그래도 승인]  ← 체크해야 눌린다 (빨간 버튼)
// 결정한 뒤: 한 줄로 접는다. 누르면 위 본문과 결과 문장이 펼쳐진다
//   (✓) 실행함  로그 보존 기간 변경 · 30일 → 14일                    [⧉ 요청 ID] ▾
//   (!) 실행하지 못함 …  → 실패 까닭은 접혀 있어도 줄 아래에 보인다
//
// - 대상·영향(target·warning)은 서버의 미리 보기가 따로 준다 (mcp/app.py의 _preview). 없는 예전 요청은 summary 문장을 그대로 보인다
// - 승인하면 서버가 실행하고 결과를 돌려준다. 그 뒤 '승인: …' 메시지와 함께 모델이 결과를 설명한다 (explainAction).
// - 거절하면 실행하지 않는다. 모델 설명은 부르지 않는다.
// - 답변에 저장된 상태는 요청 당시의 것이다. 대화를 다시 열면 서버에서 지금 상태를 다시 읽는다.
// - 승인 버튼 하나로 실행된다: 카드 자체가 무엇이 바뀌는지 보여 주는 확인 단계다.
//   의심 신호가 있으면 체크 하나를 더 거친다 (서버의 판단은 같다. 승인자가 한 번 더 생각하게 하는 화면의 장치다)
import { useEffect, useId, useState } from 'react';
import { actionErrorText, decideAction, getAction } from '@/api/actions';
import { DrawnMark, type DrawnMarkKind } from '@/components/DrawnMark';
import { useToast } from '@/components/Toast';
import { useChatStore } from '@/stores/chatStore';
import type { ActionStatus, PendingAction, TaintedBy } from '@/types/actions';
import { labelOf } from '@/utils/toolTrace';

// 펼쳤을 때 보이는 결과 문장
const STATUS_TEXT: Record<string, string> = {
    approved: '승인했습니다. 실행하는 중입니다…',
    executing: '실행하는 중입니다…',
    executed: '승인해 실행했습니다.',
    failed: '승인했지만 실행하지 못했습니다.',
    denied: '거절했습니다. 아무것도 바뀌지 않았습니다.',
    expired: '승인 시간(10분)이 지나 실행하지 않았습니다. 필요하면 다시 요청해 주세요.',
};

// 접힌 한 줄의 상태 낱말과 원 표시
const CLOSED_VIEW: Partial<Record<ActionStatus, { word: string; mark: DrawnMarkKind }>> = {
    approved: { word: '실행하는 중', mark: 'busy' },
    executing: { word: '실행하는 중', mark: 'busy' },
    executed: { word: '실행함', mark: 'check' },
    failed: { word: '실행하지 못함', mark: 'alert' },
    denied: { word: '거절함', mark: 'cross' },
    expired: { word: '시간 지남', mark: 'dash' },
};

// 남은 시간 (1초마다 다시 그린다). 0이 되면 만료로 본다
function useRemainingSeconds(expiresAt: number, active: boolean) {
    const [now, setNow] = useState(() => Date.now());
    useEffect(() => {
        if (!active) return;
        const timer = window.setInterval(() => setNow(Date.now()), 1000);
        return () => window.clearInterval(timer);
    }, [active]);
    return Math.max(0, Math.floor(expiresAt - now / 1000));
}

const clock = (seconds: number) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;

// 승인하면 실행될 호출 한 줄: 도구(인자=값, …). 값은 JSON 그대로 (문자열은 따옴표) — 가리거나 줄이지 않는다
const callText = (action: PendingAction) =>
    `${action.tool}(${Object.entries(action.args ?? {})
        .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
        .join(', ')})`;

// 작업 이름: '로그 보존 기간 변경 요청' → '로그 보존 기간 변경'
const toolName = (tool: string) => labelOf(tool).replace(/ 요청$/, '');

// 글 복사: 클립보드 API를 먼저 쓰고, 막혀 있으면(권한·포커스·http 등) 숨긴 입력 칸을 골라 복사하는 옛 방식으로
async function copyText(text: string) {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch {
        const area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', '');
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        try {
            return document.execCommand('copy');
        } catch {
            return false;
        } finally {
            area.remove();
        }
    }
}

// 의심 결과와 이 요청 사이의 거리: 1이면 그 결과를 읽자마자 요청했다
const distanceText = (seen: TaintedBy) => (seen.callsAgo <= 1 ? '바로 다음 호출' : `${seen.callsAgo}번째 뒤 호출`);

export function ApprovalCard({ action: initial }: { action: PendingAction }) {
    const [action, setAction] = useState(initial);
    const [busy, setBusy] = useState<'approve' | 'deny' | null>(null);
    const [confirmed, setConfirmed] = useState(false); // 의심 신호가 있을 때 '내가 요청한 변경이 맞습니다'
    const [expanded, setExpanded] = useState(false); // 결정한 뒤 접힌 카드를 펼쳤는가
    const { show: showToast } = useToast(); // 결정하지 못하면 패널 위쪽 가운데의 알림으로 (components/Toast)
    const waiting = useChatStore((s) => s.waitingForResponse);
    const remaining = useRemainingSeconds(action.expiresAt, action.status === 'pending');
    const status = action.status === 'pending' && remaining === 0 ? 'expired' : action.status;
    const open = status === 'pending';
    const tainted = Boolean(action.taintedBy?.length);
    const moreId = useId();

    // 대화를 다시 열었을 때: 저장된 답변의 상태는 요청 당시 것이므로 지금 상태를 읽는다
    useEffect(() => {
        if (initial.status !== 'pending') return;
        let cancelled = false;
        getAction(initial.actionId)
            .then((latest) => !cancelled && setAction(latest))
            .catch(() => {}); // 못 읽으면 저장된 상태와 남은 시간으로 보여 준다
        return () => {
            cancelled = true;
        };
    }, [initial.actionId, initial.status]);

    const decide = async (decision: 'approve' | 'deny') => {
        setBusy(decision);
        try {
            const latest = await decideAction(action.actionId, decision);
            setAction(latest);
            // 승인해 실행했으면(실패 포함) 결과를 모델이 이어서 설명한다. 질문은 서버가 저장된 기록으로 만든다
            if (decision === 'approve' && (latest.status === 'executed' || latest.status === 'failed')) {
                useChatStore.getState().explainAction(latest.actionId, `승인: ${latest.summary}`);
            }
        } catch (err) {
            showToast('error', actionErrorText(err));
            // 이미 결정되었거나 만료된 요청이면 지금 상태로 바꿔 둔다
            getAction(action.actionId).then(setAction).catch(() => {});
        } finally {
            setBusy(null);
        }
    };

    // CloudTrail 요청 ID 복사 (앱의 승인 기록과 AWS의 변경 기록을 잇는 열쇠)
    const copyTrail = async (requestId: string) => {
        const copied = await copyText(requestId);
        if (copied) showToast('success', 'CloudTrail 요청 ID를 복사했습니다.');
        else showToast('error', '복사하지 못했습니다. 펼쳐서 요청 ID를 직접 복사해 주세요.');
    };

    if (!open) {
        const view = CLOSED_VIEW[status] ?? { word: status, mark: 'dash' as const };
        const change = action.before || action.after ? `${action.before ?? '-'} → ${action.after ?? '-'}` : null;
        const trail = status === 'executed' ? action.cloudtrail : undefined;
        return (
            <section className={`approval-card is-closed is-${status}`} aria-label="변경 작업 승인 요청">
                <div className="approval-row">
                    <button
                        type="button"
                        className="approval-row-toggle"
                        aria-expanded={expanded}
                        aria-controls={moreId}
                        title={action.summary}
                        onClick={() => setExpanded((prev) => !prev)}
                    >
                        <DrawnMark kind={view.mark} size={18} />
                        <span className="approval-row-status">{view.word}</span>
                        <span className="approval-row-text">
                            {toolName(action.tool)}
                            {change ? <span className="approval-row-change"> · {change}</span> : null}
                        </span>
                        <svg className="approval-row-caret" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
                            <path d="M3 4.5l3 3 3-3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                    </button>
                    {trail ? (
                        <button
                            type="button"
                            className="approval-copy"
                            onClick={() => copyTrail(trail.request_id)}
                            title={`CloudTrail 요청 ID 복사: ${trail.request_id}`}
                        >
                            <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                                <rect x="5.5" y="5.5" width="8" height="8" rx="1.6" fill="none" stroke="currentColor" strokeWidth="1.5" />
                                <path d="M10.5 3.4V3a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 3v5A1.5 1.5 0 0 0 4 9.5h.4" fill="none" stroke="currentColor" strokeWidth="1.5" />
                            </svg>
                            요청 ID
                        </button>
                    ) : null}
                </div>

                {/* 실패 까닭은 접혀 있어도 보인다 */}
                {status === 'failed' && action.result ? <p className="approval-result">{action.result}</p> : null}

                {expanded ? (
                    <div id={moreId} className="approval-more">
                        <ActionFacts action={action} />
                        <p className="approval-status" role="status">
                            {STATUS_TEXT[status] ?? status}
                        </p>
                        {trail ? (
                            // 보통 몇 분 뒤 CloudTrail에서 조회된다
                            <p className="approval-trail">
                                CloudTrail: {trail.event_name} · 요청 ID <code>{trail.request_id}</code>
                            </p>
                        ) : null}
                    </div>
                ) : null}
            </section>
        );
    }

    return (
        <section className={`approval-card is-pending${tainted ? ' is-tainted' : ''}`} aria-label="변경 작업 승인 요청">
            {tainted ? (
                // 체류 신호 (services/llm/approvals.py): 결정은 그대로이고, 승인자가 무엇을 의심할지 알려 준다
                <div className="approval-alert" role="note">
                    <svg className="approval-alert-icon" viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
                        <path d="M10 2.8L18 16.6H2L10 2.8z" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
                        <path d="M10 8v4M10 14.4v.1" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                    </svg>
                    <div className="approval-alert-body">
                        <p className="approval-alert-title">앞서 읽은 도구 결과에 지시문처럼 보이는 문구가 있었습니다</p>
                        <ul className="approval-alert-list">
                            {action.taintedBy!.map((seen) => (
                                <li key={seen.toolUseId} title={seen.kinds.join(', ')}>
                                    {labelOf(seen.tool)} 결과 · {distanceText(seen)}에서 이 변경을 요청
                                </li>
                            ))}
                        </ul>
                        <p className="approval-alert-hint">
                            사용자가 원한 변경인지, 로그·문서에 심긴 지시를 따른 것인지 확인한 뒤 승인하세요.
                        </p>
                    </div>
                </div>
            ) : null}

            <header className="approval-head">
                <span className="approval-badge">승인 필요</span>
                <span className="approval-tool" title={action.tool}>
                    {toolName(action.tool)}
                </span>
                <span className="approval-timer" aria-label={`남은 시간 ${clock(remaining)}`}>
                    남은 시간 {clock(remaining)}
                </span>
            </header>

            <ActionFacts action={action} />

            <div className="approval-actions">
                {tainted ? (
                    <label className="approval-confirm">
                        <input
                            type="checkbox"
                            checked={confirmed}
                            onChange={(event) => setConfirmed(event.target.checked)}
                            disabled={busy !== null}
                        />
                        내가 요청한 변경이 맞습니다
                    </label>
                ) : null}
                <button type="button" className="plan-reload-button" onClick={() => decide('deny')} disabled={busy !== null}>
                    {busy === 'deny' ? '거절하는 중…' : '거절'}
                </button>
                <button
                    type="button"
                    className={`plan-create-button${tainted ? ' approval-approve-risky' : ''}`}
                    onClick={() => decide('approve')}
                    // 다른 답을 기다리는 중에는 승인하지 않는다 (결과 설명이 그 답과 섞이지 않게).
                    // 의심 신호가 있으면 체크해야 눌린다
                    disabled={busy !== null || waiting || (tainted && !confirmed)}
                    title={tainted && !confirmed ? '먼저 내가 요청한 변경이 맞는지 확인해 체크하세요' : undefined}
                >
                    {busy === 'approve' ? '실행하는 중…' : tainted ? '그래도 승인' : '승인하고 실행'}
                </button>
            </div>
        </section>
    );
}

// 무엇이 바뀌는가: 대상 · 변경 · 영향, 그리고 실제로 실행될 호출 한 줄 (기다리는 카드와 펼친 카드가 같이 쓴다)
function ActionFacts({ action }: { action: PendingAction }) {
    const hasChange = Boolean(action.before || action.after);
    return (
        <>
            {/* 대상이 따로 없는 예전 요청은 요약 문장을 그대로 */}
            {action.target ? null : <p className="approval-summary">{action.summary}</p>}
            {action.target || hasChange || action.warning ? (
                <dl className="approval-facts">
                    {action.target ? (
                        <div className="approval-fact">
                            <dt>대상</dt>
                            <dd>
                                <code className="approval-target">{action.target}</code>
                            </dd>
                        </div>
                    ) : null}
                    {hasChange ? (
                        <div className="approval-fact">
                            <dt>변경</dt>
                            <dd className="approval-change">
                                <del className="approval-before">{action.before ?? '-'}</del>
                                <span className="approval-arrow" aria-label="에서">
                                    →
                                </span>
                                <ins className="approval-after">{action.after ?? '-'}</ins>
                            </dd>
                        </div>
                    ) : null}
                    {action.warning ? (
                        <div className="approval-fact">
                            <dt>영향</dt>
                            <dd className="approval-warning">{action.warning}</dd>
                        </div>
                    ) : null}
                </dl>
            ) : null}
            <code className="approval-call" aria-label="승인하면 실행될 호출" title="승인하면 이 호출이 그대로 실행됩니다">
                {callText(action)}
            </code>
        </>
    );
}
