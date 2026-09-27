// 안내 화면 (로그인하지 않았을 때): Vigie가 무엇인지 한눈에 보이고, 로그인 버튼 하나로 Cognito 로그인 페이지에 간다.
//   [Vigie 로고]
//   #Cloud Native #Serverless #MCP                ┌ 대화 미리 보기 ────────────┐
//   AWS 운영, 물어보면 답합니다                   │ 지난주 Lambda 오류 알려줘  │
//   (한 줄 소개)                                  │ ● 로그 조회 … 답변 표      │
//   [시작하기]                                     │ [승인 필요] 30일 → 14일    │
//                                                 └────────────────────────────┘
//   [자연어로 조회] [승인 뒤 실행] [감사 로그] [민감정보 가리기]
// - 바탕은 Cognito 로그인 페이지·전환 화면과 같은 번짐 이미지 (assets/brand/login-background.svg)
// - 로그인 버튼을 누르면 App이 전환 화면(LoginSplash의 AutoLogin)을 거쳐 Cognito로 보낸다
// - 오른쪽 대화 예시는 질문과 답변을 되풀이하는 그림이다 (LandingChatDemo.tsx)
// - 처음 그릴 때 왼쪽 글만 태그·제목 → 소개 → 버튼 차례로 서서히 나타난다. 기능 카드는 움직이지 않는다
//   (움직임 줄이기 설정이면 효과 없음)
import type { CSSProperties, ReactNode } from 'react';
import background from '@/assets/brand/login-background.svg';
import vigieLogo from '@/assets/brand/vigie-logo.svg';
import { LandingChatDemo } from './LandingChatDemo';

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

export function LandingPage({ errorMessage, onLogin }: { errorMessage?: string; onLogin: () => void }) {
    return (
        <main className="landing" style={{ backgroundImage: `url(${background})` }}>
            <header className="landing-nav">
                <img className="landing-logo" src={vigieLogo} alt="Vigie" />
            </header>

            <section className="landing-hero">
                <div className="landing-copy">
                    {/* 태그와 제목은 함께, 그다음 소개, 그다음 버튼 차례로 서서히 나타난다 */}
                    <div className="landing-heading landing-enter" style={enter(0)}>
                        <ul className="landing-tags" aria-label="주제">
                            {TAGS.map((tag) => (
                                <li key={tag}>#{tag}</li>
                            ))}
                        </ul>
                        <h1 className="landing-title">
                            AWS 운영,
                            <br />
                            물어보면 답합니다
                        </h1>
                    </div>
                    <p className="landing-lead landing-enter" style={enter(1)}>
                        자연어로 물으면 Vigie가 AWS 계정의 로그·지표·비용·보안 이벤트를 조회해 답합니다. AWS를 바꾸는
                        작업은 사람이 승인해야 실행됩니다.
                    </p>

                    {errorMessage ? (
                        <div className="login-error-alert landing-enter" role="alert" style={enter(2)}>
                            <div className="login-error-icon" aria-hidden="true">
                                !
                            </div>
                            <div className="login-error-content">
                                <p className="login-error-title">로그인에 실패했습니다.</p>
                                <p className="login-error-text">{errorMessage}</p>
                            </div>
                        </div>
                    ) : null}

                    <div className="landing-actions landing-enter" style={enter(2)}>
                        {/* AWS 계정이 아니라 Vigie 계정(Cognito)으로 로그인·가입하므로 'AWS에서 로그인'이 아니라 '시작하기' */}
                        <button type="button" className="login-submit landing-cta" onClick={onLogin}>
                            시작하기
                        </button>
                    </div>
                </div>

                <div className="landing-preview-wrap">
                    <LandingChatDemo />
                </div>
            </section>

            <ul className="landing-features">
                {FEATURES.map((feature) => (
                    <li key={feature.title} className="landing-feature">
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
