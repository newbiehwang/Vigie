// 안내 화면 오른쪽의 대화 예시 (그림일 뿐 실제 답변이 아니다. 화면 읽기 프로그램에는 숨긴다).
// 예시 세 개를 차례로 되풀이한다. 한 예시는
//   질문이 떠오름 → 입력 중(점 세 개) → 도구 호출 줄 → 답변 → 표·승인 카드 → 3초 머묾 → 스르륵 사라짐 → 다음 예시
// 움직임 줄이기 설정이면 첫 예시를 다 보인 채로 멈춘다.
import { type ReactNode, useEffect, useState } from 'react';
import vigieMark from '@/assets/brand/vigie-mark.svg';

type Example = {
    question: string;
    tool: string; // 도구 호출 줄 (어떤 도구를 불렀나)
    detail: string;
    answer: ReactNode;
    extra: ReactNode; // 표 또는 승인 카드
};

const EXAMPLES: Example[] = [
    {
        question: '지난주 Lambda 오류 알려줘',
        tool: '로그 그룹 조회',
        detail: '/aws/lambda · 5',
        answer: (
            <>
                지난 7일 동안 <strong>vigie-llm-dev</strong> 함수의 오류는 3건입니다.
            </>
        ),
        extra: (
            <table className="landing-table">
                <tbody>
                    <tr>
                        <td>9월 22일</td>
                        <td>2</td>
                        <td>API 시간 초과</td>
                    </tr>
                    <tr>
                        <td>9월 24일</td>
                        <td>1</td>
                        <td>잘못된 세션 ID</td>
                    </tr>
                </tbody>
            </table>
        ),
    },
    {
        question: '이번 달 비용이 가장 많이 늘어난 서비스는?',
        tool: '비용 조회',
        detail: 'Cost Explorer · 서비스별',
        answer: (
            <>
                지난달 같은 기간보다 <strong>EC2</strong> 비용이 가장 많이 늘었습니다.
            </>
        ),
        extra: (
            <table className="landing-table">
                <tbody>
                    <tr>
                        <td>EC2</td>
                        <td>$109.63</td>
                        <td className="is-up">+18%</td>
                    </tr>
                    <tr>
                        <td>CloudWatch</td>
                        <td>$56.85</td>
                        <td className="is-up">+9%</td>
                    </tr>
                    <tr>
                        <td>Lambda</td>
                        <td>$138.06</td>
                        <td className="is-up">+4%</td>
                    </tr>
                </tbody>
            </table>
        ),
    },
    {
        question: 'vigie-llm-dev 로그 보존 기간을 14일로 줄여줘',
        tool: '변경 요청 준비',
        detail: 'setLogRetention',
        answer: '바꾸려면 승인이 필요합니다. 아래 승인 요청을 확인해 주세요.',
        extra: (
            <div className="landing-approval">
                <span className="landing-approval-badge">승인 필요</span>
                <span className="landing-approval-title">로그 보존 기간 변경</span>
                <span className="landing-approval-change">
                    <del>30일</del> → <b>14일</b>
                </span>
            </div>
        ),
    },
];

// 단계가 시작되는 때(ms): 0 질문, 1 입력 중, 2 도구 호출, 3 답변, 4 표·카드
const STEP_AT = [0, 700, 1600, 1950, 2350];
const LAST_STEP = STEP_AT.length - 1;
const HOLD_MS = 3000; // 다 보인 뒤 머무는 시간
const LEAVE_MS = 500; // 사라지는 효과 (layout.css의 .landing-demo transition과 같게)

const reducedMotion = () =>
    typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

export function LandingChatDemo() {
    const [index, setIndex] = useState(0);
    const [step, setStep] = useState(() => (reducedMotion() ? LAST_STEP : 0));
    const [leaving, setLeaving] = useState(false);

    // 예시가 바뀔 때마다 단계를 처음부터 예약한다. 화면을 떠나면(언마운트) 예약을 모두 지운다
    useEffect(() => {
        if (reducedMotion()) return;
        const timers = STEP_AT.map((at, n) => window.setTimeout(() => setStep(n), at));
        const end = STEP_AT[LAST_STEP] + HOLD_MS;
        timers.push(window.setTimeout(() => setLeaving(true), end));
        timers.push(
            window.setTimeout(() => {
                setLeaving(false);
                setStep(0);
                setIndex((current) => (current + 1) % EXAMPLES.length);
            }, end + LEAVE_MS),
        );
        return () => timers.forEach((timer) => window.clearTimeout(timer));
    }, [index]);

    const example = EXAMPLES[index];
    return (
        <div className="landing-preview" aria-hidden="true">
            <div className="landing-preview-bar">
                <i />
                <i />
                <i />
            </div>
            <div className="landing-preview-body">
                {/* key가 바뀌면 새로 그려져 나타나는 효과가 처음부터 다시 돈다 */}
                <div key={index} className={`landing-demo${leaving ? ' is-leaving' : ''}`}>
                    <p className="landing-bubble landing-demo-in">{example.question}</p>
                    {step >= 1 ? (
                        <div className="landing-answer landing-demo-in">
                            <img className="landing-avatar" src={vigieMark} alt="" />
                            <div className="landing-answer-body">
                                {step === 1 ? (
                                    <span className="landing-typing">
                                        <i />
                                        <i />
                                        <i />
                                    </span>
                                ) : (
                                    <p className="landing-trace landing-demo-in">
                                        <b>●</b> {example.tool} <span>{example.detail}</span>
                                    </p>
                                )}
                                {step >= 3 ? <p className="landing-demo-in">{example.answer}</p> : null}
                                {step >= 4 ? <div className="landing-demo-in">{example.extra}</div> : null}
                            </div>
                        </div>
                    ) : null}
                </div>
            </div>
        </div>
    );
}
