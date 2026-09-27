// 안내 화면 (로그인하지 않았을 때): Vigie가 무엇인지 한눈에 보이고, 로그인 버튼 하나로 Cognito 로그인 페이지에 간다.
//   [Vigie 로고]
//   #Cloud Native #Serverless #MCP                ┌ 대화 미리 보기 ────────────┐
//   AWS 운영, 물어보면 답합니다                   │ 지난주 Lambda 오류 알려줘  │
//   (한 줄 소개)                                  │ ● 로그 조회 … 답변 표      │
//   [aws에서 로그인]  처음이라면 가입도 거기서     │ [승인 필요] 30일 → 14일    │
//                                                 └────────────────────────────┘
//   [자연어로 조회] [승인 뒤 실행] [감사 로그] [민감정보 가리기]
// - 바탕은 Cognito 로그인 페이지·전환 화면과 같은 번짐 이미지 (assets/brand/login-background.svg)
// - 로그인 버튼을 누르면 App이 전환 화면(LoginSplash의 AutoLogin)을 거쳐 Cognito로 보낸다
// - 대화 미리 보기는 그림일 뿐이다 (실제 답변이 아니다. 화면 읽기 프로그램에는 숨긴다)
// - 처음 그릴 때 글 → 버튼 → 미리 보기 → 기능 카드 차례로 떠오른다 (움직임 줄이기 설정이면 없음)
import type { CSSProperties, ReactNode } from 'react';
import background from '@/assets/brand/login-background.svg';
import vigieLogo from '@/assets/brand/vigie-logo.svg';
import vigieMark from '@/assets/brand/vigie-mark.svg';
import { AwsLogo } from './AwsLogo';

const enter = (order: number) => ({ '--enter': order }) as CSSProperties;

// 제목 위의 태그: 서버리스(Lambda·API Gateway·DynamoDB)로 만든 클라우드 네이티브 앱이고, AI가 MCP로 AWS 도구를 부른다
const TAGS = ['Cloud Native', 'Serverless', 'MCP'];

const FEATURES: { title: string; text: string; icon: ReactNode }[] = [
    {
        title: '자연어로 조회',
        text: 'CloudWatch·Cost Explorer·CloudTrail 등 AWS 공식 MCP 도구로 로그·지표·비용·변경 이력을 찾아 답합니다.',
        icon: <path d="M4 5h16v10H9l-5 4V5Z" />,
    },
    {
        title: '승인 뒤 실행',
        text: '로그 보존 기간·알람 알림·EC2 중지 같은 변경은 승인 요청이 되고, 사람이 승인해야 실행됩니다.',
        icon: <path d="M12 3 4 6v6c0 4.5 3.4 8.2 8 9 4.6-.8 8-4.5 8-9V6l-8-3Zm-3.5 9 2.5 2.5 4.5-5" />,
    },
    {
        title: '감사 로그',
        text: '누가 어떤 질문으로 어떤 도구를 불렀는지 남기고, 관리자는 기록을 찾아볼 수 있습니다.',
        icon: <path d="M6 3h9l3 3v15H6V3Zm3 7h6M9 14h6M9 18h4" />,
    },
    {
        title: '민감정보 가리기',
        text: '계정 ID·이메일·키 같은 값은 가린 채로 모델에 넘기고, 화면과 기록에도 가려서 남깁니다.',
        icon: <path d="M3 12s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6Zm6 0a3 3 0 0 0 6 0M4 20 20 4" />,
    },
];

function ChatPreview() {
    return (
        <div className="landing-preview" aria-hidden="true">
            <div className="landing-preview-bar">
                <i />
                <i />
                <i />
            </div>
            <div className="landing-preview-body">
                <p className="landing-bubble">지난주 Lambda 오류 알려줘</p>
                <div className="landing-answer">
                    <img className="landing-avatar" src={vigieMark} alt="" />
                    <div className="landing-answer-body">
                        <p className="landing-trace">
                            <b>●</b> 로그 그룹 조회 <span>/aws/lambda · 5</span>
                        </p>
                        <p>
                            지난 7일 동안 <strong>vigie-llm-dev</strong> 함수의 오류는 3건입니다.
                        </p>
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
                        <div className="landing-approval">
                            <span className="landing-approval-badge">승인 필요</span>
                            <span className="landing-approval-title">로그 보존 기간 변경</span>
                            <span className="landing-approval-change">
                                <del>30일</del> → <b>14일</b>
                            </span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}

export function LandingPage({ errorMessage, onLogin }: { errorMessage?: string; onLogin: () => void }) {
    return (
        <main className="landing" style={{ backgroundImage: `url(${background})` }}>
            <header className="landing-nav">
                <img className="landing-logo" src={vigieLogo} alt="Vigie" />
            </header>

            <section className="landing-hero">
                <div className="landing-copy">
                    <ul className="landing-tags landing-enter" style={enter(0)} aria-label="주제">
                        {TAGS.map((tag) => (
                            <li key={tag}>#{tag}</li>
                        ))}
                    </ul>
                    <h1 className="landing-title landing-enter" style={enter(1)}>
                        AWS 운영,
                        <br />
                        물어보면 답합니다
                    </h1>
                    <p className="landing-lead landing-enter" style={enter(2)}>
                        자연어로 물으면 Vigie가 AWS 계정의 로그·지표·비용·보안 이벤트를 조회해 답합니다. AWS를 바꾸는
                        작업은 사람이 승인해야 실행됩니다.
                    </p>

                    {errorMessage ? (
                        <div className="login-error-alert landing-enter" role="alert" style={enter(3)}>
                            <div className="login-error-icon" aria-hidden="true">
                                !
                            </div>
                            <div className="login-error-content">
                                <p className="login-error-title">로그인에 실패했습니다.</p>
                                <p className="login-error-text">{errorMessage}</p>
                            </div>
                        </div>
                    ) : null}

                    <div className="landing-actions landing-enter" style={enter(3)}>
                        {/* 로고가 'AWS' 글자를 대신한다. 버튼 이름은 aria-label로 */}
                        <button type="button" className="login-submit landing-cta" onClick={onLogin} aria-label="AWS에서 로그인">
                            <AwsLogo className="login-submit-logo" />
                            <span>에서 로그인</span>
                        </button>
                        <p className="landing-note">처음이라면 로그인 화면에서 바로 가입할 수 있습니다.</p>
                    </div>
                </div>

                <div className="landing-enter landing-preview-wrap" style={enter(4)}>
                    <ChatPreview />
                </div>
            </section>

            <ul className="landing-features">
                {FEATURES.map((feature, index) => (
                    <li key={feature.title} className="landing-feature landing-enter" style={enter(5 + index)}>
                        <svg className="landing-feature-icon" viewBox="0 0 24 24" aria-hidden="true">
                            {feature.icon}
                        </svg>
                        <h2>{feature.title}</h2>
                        <p>{feature.text}</p>
                    </li>
                ))}
            </ul>

        </main>
    );
}
