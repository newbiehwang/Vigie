// 메시지 하나 (예전 components/ChatMessage.vue).
// 내 질문은 오른쪽에 글씨만, 답변은 말풍선 없이 본문으로.
// 답을 기다리는 동안에는 지금 단계만 한 줄로 보인다 ('로그 그룹 조회 중… (12초)', LiveLine).
// 답이 온 뒤에는 답변 아래 '사고 과정'을 펼치면 답을 만든 전체 과정(사고 요약·도구 호출)이 보인다 (ProgressTrace).
// AI가 AWS를 바꾸려 했으면 답변 아래에 승인 카드가 나온다 (inference.pendingActions, ApprovalCard).
// 답변 속 ![제목](artifact://…)은 결과물(차트·다이어그램)이다. inference.artifacts로 풀어 ArtifactView로 그린다.
import { memo, useEffect, useMemo, useRef, useState } from 'react';
import vigieMark from '@/assets/brand/vigie-mark.svg';
import type { PendingAction } from '@/types/actions';
import type { ChatMessageType } from '@/types/chat';
import { artifactsOf, splitArtifacts } from '@/utils/artifacts';
import { escapeHtml, parseMarkdown } from '@/utils/markdown';
import { fromProgressSteps, traceSteps } from '@/utils/toolTrace';
import { ApprovalCard } from './ApprovalCard';
import { ArtifactView } from './ArtifactView';
import { LiveLine, ProgressTrace } from './ProgressTrace';

const CARD_DELAY_MS = 350; // 답변 글이 다 나온 뒤 승인 카드가 나타나기까지

// 답변의 승인 요청 (저장된 메시지에서는 inference가 JSON 문자열이다)
const pendingActionsOf = (inference: unknown): PendingAction[] => {
    let data = inference;
    if (typeof data === 'string') {
        try {
            data = JSON.parse(data);
        } catch {
            return [];
        }
    }
    const actions = (data as { pendingActions?: unknown } | null)?.pendingActions;
    return Array.isArray(actions) ? (actions as PendingAction[]) : [];
};

function ChatMessageView({ message }: { message: ChatMessageType }) {
    const [showTrace, setShowTrace] = useState(false);
    const traceRef = useRef<HTMLDivElement>(null);
    // 펼친 사고 과정이 대화 목록 아래로 가려지지 않게, 펼쳐지고 나면(전환 240ms) 보이는 곳까지 스크롤한다
    useEffect(() => {
        if (!showTrace) return;
        const timer = window.setTimeout(
            () => traceRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }),
            260,
        );
        return () => window.clearTimeout(timer);
    }, [showTrace]);
    const isUser = message.sender === 'user';
    // 기다리는 중이면 진행 상황의 단계, 답이 왔으면 답변에 저장된 단계
    const steps = useMemo(
        () =>
            isUser ? [] : message.isTyping ? fromProgressSteps(message.progress?.steps) : traceSteps(message.inference),
        [isUser, message.isTyping, message.progress, message.inference],
    );
    // 타이핑 중이면 지금까지 보여 준 만큼만 (빈 글자일 때 전체가 잠깐 보이지 않게)
    const shownText = message.animationState === 'typing' ? (message.displayText ?? '') : message.text;
    // 글 조각은 마크다운으로, 결과물 조각은 컴포넌트로 (타이핑 중 참조가 덜 나왔으면 아직 글로 보인다)
    const segments = useMemo(
        () =>
            splitArtifacts(shownText || '').map((segment) =>
                segment.kind === 'text' ? { ...segment, html: parseMarkdown(escapeHtml(segment.text)) } : segment,
            ),
        [shownText],
    );
    const artifacts = useMemo(() => (isUser ? new Map() : artifactsOf(message.inference)), [isUser, message.inference]);
    const actions = useMemo(() => (isUser ? [] : pendingActionsOf(message.inference)), [isUser, message.inference]);
    // 승인 카드는 답변 글이 다 나온 뒤 잠깐 쉬었다가 나타난다 (글과 카드가 한꺼번에 튀어나오지 않게).
    // 방금 받은 답변(타이핑을 거친 메시지)만 효과를 주고, 예전 대화를 다시 열 때는 바로 보인다
    const typedNow = useRef(message.animationState === 'typing');
    if (message.animationState === 'typing') typedNow.current = true;
    const textDone = !isUser && !message.isTyping && message.animationState !== 'typing';
    const [cardsReady, setCardsReady] = useState(() => textDone && !typedNow.current);
    useEffect(() => {
        if (!textDone || cardsReady) return;
        const timer = window.setTimeout(() => setCardsReady(true), CARD_DELAY_MS);
        return () => window.clearTimeout(timer);
    }, [textDone, cardsReady]);
    // 실행 시간·사고 과정 줄도 글(과 카드)이 다 나온 뒤에
    const hasMeta = textDone && (actions.length === 0 || cardsReady) && (message.elapsed_time || steps.length > 0);
    const traceId = `trace-${message.id ?? message.timestamp}`;

    return (
        <div
            className={`message ${isUser ? 'user-message' : 'bot-message'}${
                message.animationState === 'appear' ? ' appear-animation' : ''
            }`}
        >
            {!isUser ? (
                <div className="message-avatar" aria-hidden="true">
                    <img src={vigieMark} alt="" />
                </div>
            ) : null}

            <div className="message-body">
                {message.isTyping ? (
                    <LiveLine steps={steps} phase={message.progress?.phase ?? 'thinking'} since={message.timestamp} />
                ) : null}

                {message.isTyping ? null : (
                    <div className="message-content markdown-content">
                        {segments.map((segment, index) =>
                            segment.kind === 'text' ? (
                                <div key={index} dangerouslySetInnerHTML={{ __html: segment.html }} />
                            ) : (
                                <ArtifactView key={index} artifact={artifacts.get(segment.ref)} title={segment.title} />
                            ),
                        )}
                    </div>
                )}

                {/* 답변을 다 보여 준 뒤에 승인 카드를 보여 준다 (타이핑 중에 버튼이 먼저 보이지 않게) */}
                {actions.length > 0 && cardsReady
                    ? actions.map((action) => (
                          <ApprovalCard key={action.actionId} action={action} entering={typedNow.current} />
                      ))
                    : null}

                {hasMeta ? (
                    <div className="query-metadata">
                        {message.elapsed_time ? <span className="elapsed-time">실행 시간 {message.elapsed_time}</span> : null}
                        {steps.length > 0 ? (
                            <button
                                type="button"
                                className="details-toggle"
                                aria-expanded={showTrace}
                                aria-controls={traceId}
                                onClick={() => setShowTrace((v) => !v)}
                            >
                                사고 과정 <span className={`details-caret${showTrace ? ' is-open' : ''}`} aria-hidden="true">▾</span>
                            </button>
                        ) : null}
                    </div>
                ) : null}

                {/* 펼치고 접을 때 높이가 부드럽게 바뀐다 (grid-template-rows 0fr ↔ 1fr, chat.css).
                    접힌 동안은 visibility: hidden이라 안의 버튼이 탭 순서·화면 읽기에 잡히지 않는다 */}
                {hasMeta && steps.length > 0 ? (
                    <div id={traceId} ref={traceRef} className={`trace-reveal${showTrace ? ' is-open' : ''}`}>
                        <div className="trace-reveal-inner">
                            <ProgressTrace steps={steps} />
                        </div>
                    </div>
                ) : null}
            </div>
        </div>
    );
}

// 타이핑 중인 답변 하나가 바뀔 때 다른 메시지는 다시 그리지 않는다
export const ChatMessage = memo(ChatMessageView);
