// 대화 화면 (예전 views/EnhancedChatbotPage.vue).
// AXPI 패널 하나를 대화가 다 쓴다. 패널 머리 작은 제목 자리에 대화 제목, 오른쪽에 '대화 목록'(팝업) · '+ 새 대화'.
import { useEffect, useRef, useState } from 'react';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { ToastHost, useToast } from '@/components/Toast';
import { useAuthStore } from '@/auth/authStore';
import { examplesFor } from '@/features/home/examples';
import { useChatStore } from '@/stores/chatStore';
import { ChatMessage } from './ChatMessage';
import { Composer } from './Composer';
import { SessionListModal } from './SessionListModal';
import './chat.css';

const NEAR_BOTTOM = 120; // 이만큼 안쪽까지 내려와 있으면 새 글이 올 때 따라 내려간다
const AFTER_PRESS_MS = 800; // 대화 안을 누른 뒤 이 시간 동안 늘어난 높이는 따라가지 않는다 (사고 과정을 펼친 것처럼 사용자가 연 것)

export function ChatPage() {
    // 빈 대화의 예시 질문: 분류마다 첫 질문. 관리자가 아니면 관리자 전용 질문을 뺀다 (examples.ts)
    const user = useAuthStore((s) => s.user);
    const currentSession = useChatStore((s) => s.currentSession);
    const waiting = useChatStore((s) => s.waitingForResponse);
    const error = useChatStore((s) => s.error);
    const loading = useChatStore((s) => s.loading);
    const viewNonce = useChatStore((s) => s.viewNonce);
    const [isListOpen, setIsListOpen] = useState(false);
    const [pendingSessionId, setPendingSessionId] = useState<string | null>(null);
    const messagesRef = useRef<HTMLDivElement>(null);
    const flowRef = useRef<HTMLDivElement>(null);
    const stickToBottom = useRef(true);
    const pressedAt = useRef(0);
    const messages = currentSession?.messages ?? [];
    const { show: showToast } = useToast();

    // 대화 오류(보내기·불러오기 실패)는 패널 위쪽 가운데의 알림으로 띄우고 스토어에서는 지운다 (components/Toast)
    useEffect(() => {
        if (!error) return;
        showToast('error', error);
        useChatStore.getState().setError(null);
    }, [error, showToast]);

    useEffect(() => {
        if (!useChatStore.getState().loaded) useChatStore.getState().fetchSessions().catch(() => {});
    }, []);

    // 새 메시지나 타이핑으로 글이 늘면 맨 아래로. 사용자가 위로 올려 읽는 중이면 그대로 둔다
    useEffect(() => {
        const box = messagesRef.current;
        if (box && stickToBottom.current) box.scrollTop = box.scrollHeight;
    }, [messages]);

    // 메시지가 바뀌지 않아도 대화의 높이가 늘면 따라 내려간다: 답변 글이 다 나온 뒤 잠깐 쉬었다 나타나는 승인 카드,
    // 늦게 그려지는 차트, 그 뒤에 붙는 실행 시간 줄. 맨 아래 가까이 있을 때만 따라가고, 방금 대화 안을 누른 뒤의
    // 변화(사고 과정 펼치기 등, 그쪽이 알아서 보이는 곳까지 스크롤한다)는 따라가지 않는다
    useEffect(() => {
        const box = messagesRef.current;
        const flow = flowRef.current;
        if (!box || !flow) return;
        const observer = new ResizeObserver(() => {
            if (!stickToBottom.current || Date.now() - pressedAt.current < AFTER_PRESS_MS) return;
            box.scrollTop = box.scrollHeight;
        });
        observer.observe(flow);
        return () => observer.disconnect();
    }, [viewNonce, messages.length > 0]);

    // 다른 대화를 열거나 새 대화를 시작하면 맨 아래부터 보여 준다
    useEffect(() => {
        stickToBottom.current = true;
    }, [viewNonce]);

    const openSession = (sessionId: string) => {
        if (sessionId === currentSession?.sessionId) return;
        // 답변을 기다리는 중에 옮기면 지금 질문을 취소하게 되므로 먼저 묻는다
        if (waiting) {
            setPendingSessionId(sessionId);
            return;
        }
        useChatStore.getState().selectSession(sessionId).catch(() => {});
    };

    const newChat = () => useChatStore.getState().newChat();

    const send = (text: string) => {
        stickToBottom.current = true;
        useChatStore.getState().sendMessage(text);
    };

    return (
        <section className="plan-panel chat-panel" aria-label="대화">
            <div className="plan-panel-header chat-header">
                {/* key가 바뀌면(새 대화·다른 대화) 새로 그려지며 전환 효과(chat-stage)가 다시 나온다 */}
                <h1 key={viewNonce} className="plan-panel-eyebrow chat-title chat-stage" title={currentSession?.title}>
                    {currentSession?.title ?? '새 대화'}
                </h1>
                <div className="chat-header-actions">
                    <button
                        className="plan-reload-button"
                        type="button"
                        aria-haspopup="dialog"
                        onClick={() => setIsListOpen(true)}
                    >
                        대화 목록
                    </button>
                    <button className="plan-create-button" type="button" disabled={waiting} onClick={newChat}>
                        + 새 대화
                    </button>
                </div>
            </div>

            <ToastHost />

            <div className="chat-body">
                <div className="chat-conversation">
                    <div
                        key={viewNonce}
                        className="chat-messages chat-stage"
                        ref={messagesRef}
                        onScroll={(event) => {
                            const box = event.currentTarget;
                            stickToBottom.current = box.scrollHeight - box.scrollTop - box.clientHeight < NEAR_BOTTOM;
                        }}
                        onPointerDown={() => (pressedAt.current = Date.now())}
                        onKeyDown={() => (pressedAt.current = Date.now())}
                    >
                        {loading && messages.length === 0 ? (
                            <div className="chat-empty">
                                <div className="plan-inline-spinner" />
                            </div>
                        ) : messages.length > 0 ? (
                            // 높이를 재는 틀 (위의 ResizeObserver)
                            <div ref={flowRef}>
                                {messages.map((message) => (
                                    <ChatMessage key={message.id} message={message} />
                                ))}
                            </div>
                        ) : (
                            <div className="chat-empty">
                                <p className="chat-empty-title">무엇이 궁금하세요?</p>
                                <p className="chat-empty-text">질문을 입력하거나 아래 예시를 눌러 보세요.</p>
                                <div className="chat-examples">
                                    {examplesFor(user).filter((_, i) => i % 2 === 0).map(({ question }) => (
                                        <button
                                            key={question}
                                            type="button"
                                            className="chat-example"
                                            disabled={waiting}
                                            onClick={() => send(question)}
                                        >
                                            {question}
                                        </button>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>
                    <Composer variant="chat" placeholder="질문을 입력하세요..." onSend={send} />
                </div>
            </div>

            {isListOpen ? (
                <SessionListModal onClose={() => setIsListOpen(false)} onSelect={openSession} onNewChat={newChat} />
            ) : null}

            {pendingSessionId ? (
                <ConfirmDialog
                    label="대화 전환 확인"
                    message="답변을 만드는 중입니다. 다른 대화로 옮기면 지금 질문은 취소됩니다."
                    confirmText="옮기기"
                    tone="primary"
                    onConfirm={() => {
                        const target = pendingSessionId;
                        setPendingSessionId(null);
                        useChatStore.getState().cancelRequest();
                        useChatStore.getState().selectSession(target).catch(() => {});
                    }}
                    onCancel={() => setPendingSessionId(null)}
                />
            ) : null}
        </section>
    );
}
