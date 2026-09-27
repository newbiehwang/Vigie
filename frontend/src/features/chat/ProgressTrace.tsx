// 답변을 만드는 과정 (Claude Code가 작업 과정을 보여 주는 모양을 따랐다).
//
// 답을 기다리는 동안 (LiveLine): 지금 하는 일을 가벼운 말 한 줄로. 도구 이름 대신 종류만 말하고('도구 사용 중',
// '계산 중'), 같은 단계가 이어지면 3초마다 다음 말로 넘어간다. 바뀔 때마다 새 글자가 아래에서 올라온다
//   ✶ 데이터 살펴보는 중… (12초)
//
// 답이 온 뒤 (ProgressTrace): 답변 아래 '사고 과정'을 펼치면 전체 과정이 보인다
//   ▸ ✻ 생각  로그 그룹부터 찾아야 한다…          ← 사고 요약. 첫 줄만 보이고 누르면 펼쳐진다
//   ● 로그 그룹 조회  /aws/lambda · 5
//     ⎿ 1.2초                                      ← 도구 결과 (실패면 이유)
//   ● 도구 찾기  lookup_events
//     ⎿ CloudTrail 이벤트 조회                     ← 도구 검색: 모델이 필요한 도구를 찾아 불러왔다
//
// 기다리는 동안에는 진행 상황(GET /llm1/progress)으로, 답이 온 뒤에는 답변의 inference.steps로 단계를 읽는다.
import { useEffect, useState } from 'react';
import { activityOf, type TraceStep } from '@/utils/toolTrace';

// Claude Code의 작업 중 표시와 같은 글자들을 차례로 돌린다
const SPINNER_FRAMES = ['·', '✢', '✳', '✶', '✻', '✽', '✻', '✶', '✳', '✢'];
const SPINNER_MS = 120;

const prefersReducedMotion = () =>
    typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

function Spinner() {
    const [frame, setFrame] = useState(0);
    useEffect(() => {
        if (prefersReducedMotion()) return; // 움직임을 줄이는 설정이면 멈춘 별표 하나
        const timer = window.setInterval(() => setFrame((f) => (f + 1) % SPINNER_FRAMES.length), SPINNER_MS);
        return () => window.clearInterval(timer);
    }, []);
    return (
        <span className="trace-spinner" aria-hidden="true">
            {prefersReducedMotion() ? '✻' : SPINNER_FRAMES[frame]}
        </span>
    );
}

// 지난 시간 (1초마다 다시 그린다)
function useElapsedSeconds(since: string) {
    const started = new Date(since).getTime();
    const [now, setNow] = useState(() => Date.now());
    useEffect(() => {
        const timer = window.setInterval(() => setNow(Date.now()), 1000);
        return () => window.clearInterval(timer);
    }, []);
    return Math.max(0, Math.floor((now - started) / 1000));
}

// 지금 단계의 이름 (세부 내용 없이). 도구를 실행 중이면 그 도구, 아니면 모델이 생각하는 중이다
// 지금 하는 일 (단계). 도구를 실행 중이면 그 도구의 종류, 아니면 모델이 생각하는 때에 따라 나눈다
type Stage = 'start' | 'search' | 'lookup' | 'cost' | 'draw' | 'change' | 'after';

// 단계마다 돌아가며 보일 말 (첫 말부터 차례로, 끝에 이르면 마지막 말에 머문다). 도구 이름은 쓰지 않는다
const STAGE_WORDS: Record<Stage, string[]> = {
    // 도구를 쓰기 전: 첫 응답이 올 때까지 한 가지 말만 둔다. 예전에는 3초마다 '질문 살펴보는 중 → 방법 고르는 중'으로
    // 바뀌어 서버가 그 단계까지 간 것처럼 보였지만, 실제로는 시간만 지난 것이었다 (서버는 첫 응답 전까지 단계가 없다)
    start: ['생각하는 중'],
    search: ['도구 찾는 중', '알맞은 도구 고르는 중'], // 도구 검색 뒤 (services/llm/tool_search.py)
    lookup: ['도구 사용 중', '데이터 살펴보는 중', '기록 확인하는 중'], // 로그·지표·리소스·문서·CloudTrail 조회
    cost: ['계산 중', '숫자 맞춰 보는 중'], // 비용·가격
    draw: ['그리는 중', '모양 다듬는 중'], // 차트·다이어그램
    change: ['확인 준비 중', '바뀔 내용 살피는 중'], // 변경 도구 (실행하지 않고 승인 요청을 만든다)
    after: ['결과 정리하는 중', '답변 쓰는 중', '다듬는 중'], // 도구를 쓴 뒤 다시 생각
};
const WORD_MS = 3000; // 같은 단계에서 다음 말로 넘어가는 간격

// 화면 읽기 프로그램에는 돌아가는 말 대신 단계가 바뀔 때만 알린다 (3초마다 읽으면 시끄럽다)
const STAGE_ANNOUNCE: Record<Stage, string> = {
    start: '생각하는 중',
    search: '도구를 찾는 중',
    lookup: '도구를 사용하는 중',
    cost: '계산하는 중',
    draw: '그리는 중',
    change: '변경 내용을 확인하는 중',
    after: '답변을 쓰는 중',
};

function currentStage(steps: TraceStep[], phase: string): Stage {
    const running = [...steps].reverse().find((step) => step.kind === 'tool' && step.status === 'running');
    if (phase === 'tool' && running?.kind === 'tool') return activityOf(running.name);
    const last = [...steps].reverse().find((step) => step.kind === 'tool');
    if (!last) return 'start';
    // 도구 검색은 모델 요청 안에서 일어나 '실행 중'으로 오지 않는다. 검색 직후면 찾은 도구를 고르는 중이다
    return last.kind === 'tool' && last.name === 'tool_search' ? 'search' : 'after';
}

// 보일 단계. 도구 단계로는 바로 넘어가고, 생각 단계(start·search·after)로는 SETTLE_MS 넘게 이어질 때만 넘어간다.
// 도구를 잇달아 부를 때 사이사이 모델이 잠깐 생각하는 틈마다 말이 번갈아 깜빡이지 않게 한다
const SETTLE_MS = 1200;
const THINKING_STAGES = new Set<Stage>(['start', 'search', 'after']);

function useSettledStage(stage: Stage): Stage {
    const [shown, setShown] = useState(stage);
    useEffect(() => {
        if (stage === shown) return;
        if (!THINKING_STAGES.has(stage)) {
            setShown(stage);
            return;
        }
        const timer = window.setTimeout(() => setShown(stage), SETTLE_MS);
        return () => window.clearTimeout(timer);
    }, [stage, shown]);
    return shown;
}

// 같은 단계가 이어진 시간에 따라 몇 번째 말을 보일지 (단계가 바뀌면 처음부터)
function useStageWord(stage: Stage): string {
    const [started, setStarted] = useState(() => ({ stage, at: Date.now() }));
    const [now, setNow] = useState(() => Date.now());
    if (started.stage !== stage) setStarted({ stage, at: Date.now() }); // 그리는 중에 단계가 바뀌었다
    useEffect(() => {
        const timer = window.setInterval(() => setNow(Date.now()), 500);
        return () => window.clearInterval(timer);
    }, []);
    const words = STAGE_WORDS[stage];
    const index = Math.min(Math.floor(Math.max(0, now - started.at) / WORD_MS), words.length - 1);
    return words[index];
}

// 답을 기다리는 동안의 한 줄: 도는 별표 · 지금 하는 일 · 지난 시간.
// 말이 바뀌면 key가 바뀌어 새로 그려지며, 아래에서 올라오는 전환 효과(trace-line-in)가 난다
export function LiveLine({ steps, phase, since }: { steps: TraceStep[]; phase: string; since: string }) {
    const seconds = useElapsedSeconds(since);
    const stage = useSettledStage(currentStage(steps, phase));
    const word = useStageWord(stage);
    return (
        <div className="trace trace-live">
            <Spinner />
            <span key={word} className="trace-verb trace-line-in" aria-hidden="true">
                {word}…
            </span>
            <span className="trace-time" aria-hidden="true">
                ({seconds}초)
            </span>
            <span className="sr-only" role="status" aria-live="polite">
                {STAGE_ANNOUNCE[stage]}
            </span>
        </div>
    );
}

function ThinkingStep({ step }: { step: Extract<TraceStep, { kind: 'thinking' }> }) {
    const [open, setOpen] = useState(false);
    return (
        <li className={`trace-step trace-thinking${open ? ' is-open' : ''}`}>
            <button type="button" className="trace-thinking-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
                <span className="trace-caret" aria-hidden="true">
                    ▸
                </span>
                <span className="trace-thinking-mark" aria-hidden="true">
                    ✻
                </span>
                <span className="trace-thinking-title">생각</span>
                {open ? null : <span className="trace-thinking-preview">{step.preview}</span>}
            </button>
            {open ? <p className="trace-thinking-text">{step.text}</p> : null}
        </li>
    );
}

function ToolStepView({ step }: { step: Extract<TraceStep, { kind: 'tool' }> }) {
    // 실패는 색만이 아니라 글자('실패')로도 알린다
    const result =
        step.status === 'error'
            ? `실패${step.error ? `: ${step.error}` : ''}`
            : step.status === 'ok'
              ? (step.result ?? step.seconds)
              : null;
    return (
        <li className={`trace-step trace-tool is-${step.status}`} title={step.name}>
            <span className="trace-tool-line">
                <span className="trace-bullet" aria-hidden="true">
                    ●
                </span>
                <span className="trace-tool-label">{step.label}</span>
                {step.detail ? <span className="trace-tool-detail">{step.detail}</span> : null}
                {step.suspicious ? (
                    // 도구 결과(로그·문서 등 제3자가 쓴 글)에 모델에게 하는 지시처럼 보이는 문구가 있었다.
                    // 모델에는 데이터로만 다루라는 경고와 함께 넘겼고, 변경은 사람이 승인해야 실행된다
                    <span
                        className="trace-suspicious"
                        title="도구 결과에 지시문처럼 보이는 문구가 있어 데이터로만 다뤘습니다"
                    >
                        의심 문구
                    </span>
                ) : null}
                {step.status === 'running' ? <span className="sr-only">실행 중</span> : null}
            </span>
            {result ? (
                <span className="trace-tool-result">
                    <span aria-hidden="true">⎿ </span>
                    {result}
                </span>
            ) : null}
        </li>
    );
}

// 답변을 만든 전체 과정 (답변 아래 '사고 과정'을 펼치면)
export function ProgressTrace({ steps }: { steps: TraceStep[] }) {
    if (steps.length === 0) return null;
    return (
        <div className="trace">
            <ol className="trace-steps" aria-label="답변을 만든 과정">
                {steps.map((step, index) =>
                    step.kind === 'thinking' ? (
                        <ThinkingStep key={index} step={step} />
                    ) : (
                        <ToolStepView key={index} step={step} />
                    ),
                )}
            </ol>
        </div>
    );
}
