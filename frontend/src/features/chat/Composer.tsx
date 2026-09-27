// 질문 입력칸 (홈과 대화 화면에서 함께 쓴다).
// Enter로 보내고 Shift+Enter로 줄을 바꾼다. 답변을 기다리는 중에는 보내기 버튼이 취소 버튼이 되고 Esc로도 취소한다.
//
// suggestions를 주면(홈), 입력칸을 누를 때 입력칸 바로 아래에 예시 질문이 떠서 펼쳐진다.
// 입력칸과 예시 목록 밖으로 포커스가 나가면 접히는 효과를 보여 준 뒤 사라진다.
//
// 홈(variant="home")은 FinGate-X 첫 화면의 입력창 모양이다: 2줄 입력칸, 아래 줄 왼쪽에 키 안내
// (빈칸이면 'Tab 예시 넣기', 글이 있으면 'Enter 보내기'), 오른쪽에 보내기. 모델은 서버가 최신 Sonnet으로 정하므로 고르는 칸이 없다.
import { type FocusEvent, type KeyboardEvent, useEffect, useRef, useState } from 'react';
import { useChatStore } from '@/stores/chatStore';

const MAX_HEIGHT = 150; // 입력칸이 늘어나는 최대 높이
const CLOSE_MS = 160; // 예시 목록이 접히는 시간 (CSS의 composer-suggest-out과 같게)
const SUGGEST_MAX = 360; // 예시 목록의 최대 높이
const SUGGEST_MIN = 160; // 카드 아래 공간이 좁아도 이만큼은 보여 주고 목록 안에서 스크롤한다
const SUGGEST_GAP = 6; // 입력칸과 예시 목록 사이 (CSS의 .composer-suggestions top과 같게)

export interface Suggestion {
    category: string;
    question: string;
}

export function Composer({
    variant,
    placeholder,
    onSend,
    suggestions,
    compact = false,
}: {
    variant: 'home' | 'chat';
    placeholder: string;
    onSend: (text: string) => void;
    suggestions?: Suggestion[];
    compact?: boolean; // 홈 대시보드 위의 한 줄 입력칸 (입력칸 1줄, 키 안내는 오른쪽)
}) {
    const [text, setText] = useState('');
    // 예시 목록: 'closed' → (포커스) 'open' → (포커스가 나감) 'closing' → CLOSE_MS 뒤 'closed'
    const [suggest, setSuggest] = useState<'closed' | 'open' | 'closing'>('closed');
    const [suggestMaxHeight, setSuggestMaxHeight] = useState(SUGGEST_MAX);
    const closeTimer = useRef<number>();
    const wrapRef = useRef<HTMLDivElement>(null);
    const inputRef = useRef<HTMLTextAreaElement>(null);
    const waiting = useChatStore((s) => s.waitingForResponse);
    const busy = variant === 'chat' && waiting;
    const hasSuggestions = !!suggestions?.length;

    // 글이 늘어나면 입력칸도 함께 늘린다
    useEffect(() => {
        const input = inputRef.current;
        if (!input) return;
        input.style.height = 'auto';
        input.style.height = `${Math.min(input.scrollHeight, MAX_HEIGHT)}px`;
    }, [text]);

    useEffect(() => () => window.clearTimeout(closeTimer.current), []);

    const openSuggestions = () => {
        if (!hasSuggestions) return;
        window.clearTimeout(closeTimer.current); // 접히는 중에 다시 누르면 그대로 다시 편다
        // 흰 카드는 넘치는 부분을 잘라 낸다. 입력칸 아래에서 카드 바닥까지 남은 높이 안에서 펼친다
        const wrap = wrapRef.current;
        const card = wrap?.closest('.plan-panel');
        if (wrap && card) {
            const space = card.getBoundingClientRect().bottom - wrap.getBoundingClientRect().bottom - SUGGEST_GAP - 16;
            setSuggestMaxHeight(Math.max(SUGGEST_MIN, Math.min(SUGGEST_MAX, space)));
        }
        setSuggest('open');
    };

    const closeSuggestions = () => {
        setSuggest((state) => (state === 'closed' ? state : 'closing'));
        window.clearTimeout(closeTimer.current);
        closeTimer.current = window.setTimeout(() => setSuggest('closed'), CLOSE_MS);
    };

    // 포커스가 입력칸에서 예시 목록(또는 모델 선택)으로 옮겨 가는 것은 '나감'이 아니다 (Tab으로 예시를 고를 수 있게)
    const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) closeSuggestions();
    };

    const submit = (value: string) => {
        if (!value.trim() || busy) return;
        onSend(value.trim());
        setText('');
    };

    const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
        // 한글을 조합하는 중의 Enter는 글자를 확정하는 Enter다. 이때 보내면 마지막 글자가 두 번 들어간다
        if (event.nativeEvent.isComposing) return;
        // Tab: 빈칸이면 첫 예시 질문을 채운다 (placeholder에 보이는 문장). 글이 있으면 보통의 Tab(포커스 이동)
        if (event.key === 'Tab' && !event.shiftKey && !text.trim() && hasSuggestions) {
            event.preventDefault();
            setText(suggestions![0].question);
            return;
        }
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            submit(text);
        } else if (event.key === 'Escape') {
            if (busy) useChatStore.getState().cancelRequest();
            else if (suggest === 'open') closeSuggestions();
        }
    };

    return (
        <div ref={wrapRef} className={`composer-wrap composer-wrap--${variant}`} onBlur={handleBlur}>
            <div
                className={`composer composer--${variant}${compact ? ' is-compact' : ''}`}
            >
                <textarea
                    ref={inputRef}
                    className="composer-input"
                    rows={variant === 'home' && !compact ? 2 : 1}
                    placeholder={placeholder}
                    value={text}
                    onChange={(event) => setText(event.target.value)}
                    onKeyDown={handleKeyDown}
                    onFocus={openSuggestions}
                    aria-label="질문"
                />
                <div className="composer-actions">
                    {variant === 'home' ? (
                        <p className="composer-hint" aria-hidden="true">
                            {text.trim() === '' && hasSuggestions ? (
                                <>
                                    <kbd>Tab</kbd> 예시 넣기
                                </>
                            ) : (
                                <>
                                    <kbd>Enter</kbd> 보내기
                                </>
                            )}
                        </p>
                    ) : null}
                    {busy ? (
                        <button
                            className="plan-create-button composer-send is-cancel"
                            type="button"
                            aria-label="답변 취소"
                            title="답변 취소 (Esc)"
                            onClick={() => useChatStore.getState().cancelRequest()}
                        >
                            <svg viewBox="0 0 24 24" aria-hidden="true">
                                <rect x="7" y="7" width="10" height="10" rx="1.5" />
                            </svg>
                        </button>
                    ) : (
                        <button
                            className="plan-create-button composer-send"
                            type="button"
                            aria-label="보내기"
                            title="보내기 (Enter)"
                            disabled={!text.trim()}
                            onClick={() => submit(text)}
                        >
                            <svg viewBox="0 0 24 24" aria-hidden="true">
                                <path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7Z" />
                            </svg>
                        </button>
                    )}
                </div>
            </div>

            {hasSuggestions && suggest !== 'closed' ? (
                <div
                    className={`composer-suggestions${suggest === 'closing' ? ' is-closing' : ''}`}
                    style={{ maxHeight: suggestMaxHeight }}
                >
                    <p className="composer-suggestions-title">예시 질문</p>
                    <ul>
                        {suggestions!.map(({ category, question }) => (
                            <li key={question}>
                                <button
                                    type="button"
                                    className="composer-suggestion"
                                    // 누르는 순간 입력칸 포커스가 빠져 목록이 접히기 시작하지 않도록 포커스를 옮기지 않는다
                                    onMouseDown={(event) => event.preventDefault()}
                                    onClick={() => {
                                        submit(question);
                                        inputRef.current?.blur();
                                    }}
                                >
                                    <span className="plan-status-badge plan-status-active">{category}</span>
                                    <span className="composer-suggestion-text">{question}</span>
                                </button>
                            </li>
                        ))}
                    </ul>
                </div>
            ) : null}
        </div>
    );
}
