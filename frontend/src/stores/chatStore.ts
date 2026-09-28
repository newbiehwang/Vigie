// 대화 목록과 지금 대화 (예전 stores/chatHistoryStore.ts를 옮겼다).
//
// API (services/chat-history, services/llm)
//   GET/POST/DELETE /sessions                  대화 목록 / 새 대화 / 전체 삭제
//   GET/PUT/DELETE  /sessions/{id}             대화 하나 / 제목 변경 / 삭제
//   GET/POST        /sessions/{id}/messages    메시지 목록 / 메시지 저장
//   POST            /llm1                      답변 만들기 (MCP 도구 호출 포함, 수십 초 걸릴 수 있다)
//   GET             /llm1/progress/{requestId} 답변을 만드는 동안의 진행 상황 (사고 요약·도구 호출)
//   POST            /llm1 {actionId}           승인한 변경 작업의 결과 설명 (질문은 서버가 저장된 기록으로 만든다)
// 사용자는 백엔드가 ID 토큰의 sub로 구분한다 (예전처럼 userId를 보내지 않는다).
//
// 질문 하나를 보내는 순서: 내 메시지 저장 → 화면에 '생각하는 중' → /llm1 → 답변 저장 → 타이핑하듯 보여 주기.
// /llm1은 답이 다 만들어진 뒤에 한 번만 응답하므로, 기다리는 동안 진행 상황을 1초마다 따로 가져와 보여 준다
import axios, { type CancelTokenSource } from 'axios';
import { create } from 'zustand';
import { LOCAL } from '@/auth/authClient';
import type { BotResponse, ChatMessageType, ChatSession } from '@/types/chat';

const newId = () => Date.now().toString(36) + Math.random().toString(36).substring(2);
const shortTitle = (text: string) => (text.length > 30 ? `${text.substring(0, 30)}...` : text);
const byUpdatedAt = (a: ChatSession, b: ChatSession) =>
    new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime();

const ERROR_ANSWER = '죄송합니다. 응답을 처리하는 중에 오류가 발생했습니다. 다시 시도해 주세요.';
const CANCEL_ANSWER = '요청이 취소되었습니다.';
const TYPING_MS_PER_CHAR = 10; // 답변을 타이핑하듯 보여 주는 속도
const TYPING_MAX_MS = 2000; // 긴 답변도 이 시간 안에 다 보여 준다
const PROGRESS_POLL_MS = 1000; // 답을 기다리는 동안 진행 상황을 가져오는 간격
const PROGRESS_MAX_FAILURES = 10; // 연달아 이만큼 실패하면 그만 묻는다 (진행 상황 API가 없는 예전 백엔드 등)

interface ChatState {
    sessions: ChatSession[]; // 목록에는 메시지 없이 요약만 둔다
    currentSession: ChatSession | null; // null이면 새 대화 (첫 질문을 보낼 때 만든다)
    // 대화 화면을 바꾼 횟수 (새 대화 · 다른 대화 열기). 화면이 이 값으로 전환 효과를 다시 튼다.
    // 세션 ID로 하지 않는 이유: 새 대화에 첫 질문을 보내면 세션이 만들어지며 ID가 생기는데, 그때는 효과를 틀면 안 된다
    viewNonce: number;
    loaded: boolean; // 목록을 한 번이라도 받았는지
    loading: boolean;
    error: string | null;
    waitingForResponse: boolean;
    cancelSource: CancelTokenSource | null;

    fetchSessions: () => Promise<void>;
    selectSession: (sessionId: string) => Promise<void>;
    newChat: () => void;
    sendMessage: (text: string) => Promise<void>;
    // 승인한 변경 작업의 결과를 모델이 설명하게 한다. note는 대화에 남길 내 메시지 (예: "승인: 보존 기간 30일 → 14일")
    explainAction: (actionId: string, note: string) => Promise<void>;
    cancelRequest: () => void;
    renameSession: (sessionId: string, title: string) => Promise<void>;
    deleteSession: (sessionId: string) => Promise<void>;
    deleteAllSessions: () => Promise<void>;
    setError: (message: string | null) => void;
}

// /llm1 응답을 화면에 보여 줄 답변으로 바꾼다 (응답 모양이 여러 가지인 예전 백엔드도 받아 준다)
function toBotResponse(data: any): BotResponse {
    if (!data) return { text: '죄송합니다. 유효한 응답 데이터를 받지 못했습니다.' };
    if (data.inference || data.query_string) {
        return {
            text: data.answer || '쿼리 결과 없음',
            query_string: data.query_string,
            query_result: data.query_result || [],
            elapsed_time: data.elapsed_time,
            inference: data.inference,
        };
    }
    if (Array.isArray(data.answer)) {
        const items = [...data.answer].sort((a, b) => a.rank_order - b.rank_order);
        return {
            text: items.map((item) => `${item.context}\n${item.title}\n${item.url}`).join('\n\n'),
            elapsed_time: data.elapsed_time,
        };
    }
    return {
        text: typeof data.answer === 'string' ? data.answer : JSON.stringify(data.answer),
        elapsed_time: data.elapsed_time,
    };
}

export const useChatStore = create<ChatState>((set, get) => {
    let inflight: Promise<void> | null = null; // 받고 있는 대화 목록 요청

    // 지금 보고 있는 대화가 sessionId일 때만 메시지를 바꾼다.
    // 답변을 기다리는 중에 다른 대화로 옮겨 가면, 늦게 온 답변이 엉뚱한 대화에 붙지 않게 한다
    const updateMessages = (sessionId: string, fn: (messages: ChatMessageType[]) => ChatMessageType[]) =>
        set((state) =>
            state.currentSession?.sessionId === sessionId
                ? { currentSession: { ...state.currentSession, messages: fn(state.currentSession.messages) } }
                : {},
        );

    const touchSession = (sessionId: string, patch: Partial<ChatSession>) =>
        set((state) => ({
            sessions: state.sessions.map((s) => (s.sessionId === sessionId ? { ...s, ...patch } : s)).sort(byUpdatedAt),
            currentSession:
                state.currentSession?.sessionId === sessionId ? { ...state.currentSession, ...patch } : state.currentSession,
        }));

    const saveMessage = async (sessionId: string, message: Record<string, unknown>, cancel?: CancelTokenSource) =>
        (await axios.post(`/sessions/${sessionId}/messages`, message, { cancelToken: cancel?.token }))
            .data as ChatMessageType;

    // 답을 기다리는 동안 진행 상황을 1초마다 가져와 기다리는 메시지에 넣는다. 멈출 때 쓸 타이머 번호를 돌려준다.
    // - 아직 첫 기록 전(404)이거나 잠깐 실패하면 다음 차례에 다시 묻는다 (진행 상황은 보조 정보다).
    //   연달아 PROGRESS_MAX_FAILURES번 실패하면 그만 묻고 '생각하는 중'만 보여 준다
    // - 앞 요청이 끝나지 않았으면 이번 차례는 건너뛴다 (느린 응답이 쌓이지 않게)
    // - 답이 먼저 와서 기다리는 메시지가 없어졌으면 늦게 온 진행 상황은 버린다 (updateMessages가 id로 찾는다)
    const watchProgress = (sessionId: string, loadingId: string, requestId: string, cancel: CancelTokenSource) => {
        let busy = false;
        let failures = 0;
        const timer = window.setInterval(async () => {
            if (busy) return;
            busy = true;
            try {
                const { data } = await axios.get(`/llm1/progress/${requestId}`, { cancelToken: cancel.token });
                updateMessages(sessionId, (messages) =>
                    messages.map((m) =>
                        m.id === loadingId && m.isTyping
                            ? { ...m, progress: { phase: data.phase ?? 'thinking', steps: data.steps ?? [] } }
                            : m,
                    ),
                );
                failures = 0;
            } catch {
                failures += 1;
                if (failures >= PROGRESS_MAX_FAILURES) window.clearInterval(timer);
            } finally {
                busy = false;
            }
        }, PROGRESS_POLL_MS);
        return timer;
    };

    // 답변을 한 글자씩 늘려 가며 보여 준다. 글자마다 그리지 않고 시간에 맞춰 여러 글자씩 늘린다
    const typeOut = (sessionId: string, messageId: string, fullText: string) => {
        const total = Math.min(fullText.length * TYPING_MS_PER_CHAR, TYPING_MAX_MS);
        const started = performance.now();
        const setMessage = (patch: Partial<ChatMessageType>) =>
            updateMessages(sessionId, (messages) => messages.map((m) => (m.id === messageId ? { ...m, ...patch } : m)));

        const tick = () => {
            const shown = total ? Math.ceil((fullText.length * (performance.now() - started)) / total) : fullText.length;
            if (shown >= fullText.length) {
                setMessage({ displayText: fullText, animationState: 'complete' });
                return;
            }
            setMessage({ displayText: fullText.slice(0, shown) });
            setTimeout(tick, 30);
        };
        tick();
    };

    // 질문 하나를 보내고 답을 받는다. actionId가 있으면 승인한 변경 작업의 결과 설명을 받는다
    // (text는 대화에 남길 내 메시지이고, 모델에 보낼 질문은 서버가 저장된 기록으로 만든다)
    const ask = async (text: string, actionId?: string) => {
        const question = text.trim();
        if (!question || get().waitingForResponse) return;
        // 취소 버튼은 보내자마자 보이므로, 취소 신호도 처음부터 만들어 모든 요청에 건다
        const cancelSource = axios.CancelToken.source();
        set({ waitingForResponse: true, error: null, cancelSource });

        let sessionId = '';
        const loadingId = newId();
        // 이 질문의 진행 상황을 찾을 열쇠. /llm1과 진행 상황 조회에 같은 값을 보낸다
        const requestId = crypto.randomUUID();
        let progressTimer: number | undefined;
        try {
            // 새 대화면 첫 질문을 제목으로 만든다
            let session = get().currentSession;
            if (!session) {
                const created = (
                    await axios.post('/sessions', { title: shortTitle(question) }, { cancelToken: cancelSource.token })
                ).data;
                session = { ...created, messages: [] } as ChatSession;
                set((state) => ({ currentSession: session, sessions: [session!, ...state.sessions] }));
            }
            sessionId = session.sessionId;
            const isFirstMessage = session.messages.length === 0;

            const userMessage = await saveMessage(sessionId, { sender: 'user', text: question }, cancelSource);
            updateMessages(sessionId, (messages) => [
                ...messages,
                { ...userMessage, animationState: 'appear' },
                {
                    id: loadingId,
                    sender: 'assistant',
                    text: '...',
                    timestamp: new Date().toISOString(), // 화면의 '(12초)'는 이 시각부터 센다
                    isTyping: true,
                    progress: { phase: 'thinking', steps: [] },
                },
            ]);
            progressTimer = watchProgress(sessionId, loadingId, requestId, cancelSource);
            // 예전에 만든 빈 대화('새 대화')에 처음 질문하면 제목을 질문으로 바꾼다
            if (isFirstMessage && session.title !== shortTitle(question)) {
                await axios.put(`/sessions/${sessionId}`, { title: shortTitle(question) });
                touchSession(sessionId, { title: shortTitle(question) });
            }

            const response = await axios.post(
                '/llm1',
                {
                    text: question,
                    sessionId,
                    // 대화 컨텍스트는 항상 기억한다: 백엔드가 이 세션의 이전 대화를 함께 모델에 보낸다.
                    // 백엔드는 값이 없으면 false로 보므로 반드시 true를 보낸다
                    isCached: true,
                    requestId,
                    ...(actionId && { actionId }),
                },
                { cancelToken: cancelSource.token },
            );
            const answer = toBotResponse(response.data);

            const saved = await saveMessage(sessionId, {
                sender: 'assistant',
                text: answer.text,
                ...(answer.query_string && { query_string: answer.query_string }),
                ...(answer.query_result?.length && { query_result: JSON.stringify(answer.query_result) }),
                ...(answer.elapsed_time && { elapsed_time: answer.elapsed_time }),
                ...(answer.inference && { inference: JSON.stringify(answer.inference) }),
            });
            // 저장한 메시지에는 JSON 문자열로 들어가므로, 화면에는 받은 그대로의 값을 쓴다
            const botMessage: ChatMessageType = {
                ...saved,
                ...answer,
                displayText: '',
                animationState: 'typing',
            };
            updateMessages(sessionId, (messages) => [...messages.filter((m) => m.id !== loadingId), botMessage]);
            touchSession(sessionId, { updatedAt: new Date().toISOString() });
            typeOut(sessionId, botMessage.id, answer.text);
        } catch (error) {
            const cancelled = axios.isCancel(error);
            // 로컬 장애 재현(npm run dev:local)에서는 서버의 안내를 그대로 보인다 (예: 모델이 연결되지 않았다)
            const localNotice = LOCAL
                ? (error as { response?: { data?: { error?: string } } })?.response?.data?.error
                : undefined;
            const notice: ChatMessageType = {
                id: newId(),
                sender: 'assistant',
                text: cancelled ? CANCEL_ANSWER : (localNotice ?? ERROR_ANSWER),
                timestamp: new Date().toISOString(),
                animationState: 'appear',
            };
            if (sessionId) {
                updateMessages(sessionId, (messages) => [...messages.filter((m) => m.id !== loadingId), notice]);
                // 다시 열었을 때도 무슨 일이 있었는지 보이도록 안내 문구도 저장한다
                saveMessage(sessionId, { sender: 'assistant', text: notice.text }).catch(() => {});
            }
            if (!cancelled) set({ error: '메시지를 전송하는 중 오류가 발생했습니다.' });
        } finally {
            window.clearInterval(progressTimer);
            set({ waitingForResponse: false, cancelSource: null });
        }
    };

    return {
        sessions: [],
        currentSession: null,
        viewNonce: 0,
        loaded: false,
        loading: false,
        error: null,
        waitingForResponse: false,
        cancelSource: null,

        fetchSessions: () => {
            // 홈과 대화 화면이 거의 동시에 부를 수 있다. 받는 중이면 같은 요청을 기다린다
            if (inflight) return inflight;
            inflight = (async () => {
                set({ loading: true, error: null });
                try {
                    const response = await axios.get('/sessions');
                    const fetched: ChatSession[] = response.data.sessions || [];
                    // 목록을 받는 사이에 홈에서 보낸 질문으로 새 대화가 생겼을 수 있다. 그 대화도 목록에 남긴다
                    set((state) => {
                        const ids = new Set(fetched.map((session) => session.sessionId));
                        const created = state.sessions.filter((session) => !ids.has(session.sessionId));
                        return { sessions: [...created, ...fetched].sort(byUpdatedAt), loaded: true };
                    });
                    // 처음 열었을 때는 가장 최근 대화를 연다. 새 대화에 질문하는 중이면 그대로 둔다
                    const { currentSession, waitingForResponse, sessions } = get();
                    if (!currentSession && !waitingForResponse && sessions.length > 0)
                        await get().selectSession(sessions[0].sessionId);
                } catch (error) {
                    set({ error: '대화 목록을 불러오지 못했습니다.' });
                    throw error;
                } finally {
                    set({ loading: false });
                    inflight = null;
                }
            })();
            return inflight;
        },

        selectSession: async (sessionId) => {
            set({ loading: true, error: null });
            try {
                const [session, messages] = await Promise.all([
                    axios.get(`/sessions/${sessionId}`),
                    axios.get(`/sessions/${sessionId}/messages`),
                ]);
                set((state) => ({
                    currentSession: { ...session.data, messages: messages.data.messages || [] },
                    viewNonce: state.viewNonce + 1,
                }));
            } catch (error) {
                set({ error: '대화를 불러오지 못했습니다.' });
                throw error;
            } finally {
                set({ loading: false });
            }
        },

        newChat: () => set((state) => ({ currentSession: null, error: null, viewNonce: state.viewNonce + 1 })),

        sendMessage: (text) => ask(text),

        explainAction: (actionId, note) => ask(note, actionId),

        cancelRequest: () => {
            get().cancelSource?.cancel('사용자가 요청을 취소했습니다.');
        },

        renameSession: async (sessionId, title) => {
            await axios.put(`/sessions/${sessionId}`, { title });
            set((state) => ({
                sessions: state.sessions.map((s) => (s.sessionId === sessionId ? { ...s, title } : s)),
                currentSession:
                    state.currentSession?.sessionId === sessionId ? { ...state.currentSession, title } : state.currentSession,
            }));
        },

        deleteSession: async (sessionId) => {
            await axios.delete(`/sessions/${sessionId}`);
            const sessions = get().sessions.filter((s) => s.sessionId !== sessionId);
            set({ sessions });
            if (get().currentSession?.sessionId === sessionId) {
                set({ currentSession: null });
                if (sessions.length > 0) await get().selectSession(sessions[0].sessionId);
            }
        },

        deleteAllSessions: async () => {
            await axios.delete('/sessions');
            set({ sessions: [], currentSession: null });
        },

        setError: (message) => set({ error: message }),
    };
});
