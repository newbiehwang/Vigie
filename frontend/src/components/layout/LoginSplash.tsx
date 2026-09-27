// 로그인 전환 화면: Cognito 로그인 페이지로 넘어가기 직전과, 돌아와서 세션을 읽는 동안 보인다.
// 배경(파란·남색 번짐)은 Cognito 로그인 페이지의 배경과 같은 파일이라(assets/brand/login-background.svg,
// cloudformation/base.yaml의 PAGE_BACKGROUND) 앱 → Cognito → 앱으로 오가는 동안 바탕이 이어져 보인다.
//   나타날 때: 흰 카드(Vigie 로고)가 아래에서 살짝 떠오른다
//   떠날 때(is-leaving): 카드가 위로 살짝 사라진 뒤 Cognito 페이지가 열린다 (Cognito 페이지에는 효과를 넣을 수 없다)
import { useEffect, useState } from 'react';
import background from '@/assets/brand/login-background.svg';
import vigieLogo from '@/assets/brand/vigie-logo.svg';
import { login } from '@/auth/authClient';
import { getErrorText } from '@/utils/formatters';

const SHOW_MS = 450; // 카드가 떠오른 뒤 잠깐 머무는 시간
const LEAVE_MS = 220; // 사라지는 효과 (layout.css의 login-splash-out과 같게)

const reducedMotion = () =>
    typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

export function LoginSplash({ leaving = false, message }: { leaving?: boolean; message?: string }) {
    return (
        <main
            className={`login-splash${leaving ? ' is-leaving' : ''}`}
            style={{ backgroundImage: `url(${background})` }}
        >
            <section className="login-splash-card" role="status" aria-live="polite">
                <img className="login-splash-logo" src={vigieLogo} alt="Vigie" />
                <span className="login-splash-dots" aria-hidden="true">
                    <i />
                    <i />
                    <i />
                </span>
                <p className="login-splash-text">{message ?? '불러오는 중'}</p>
            </section>
        </main>
    );
}

// 안내 화면(LandingPage)에서 로그인을 누르면 전환 화면을 잠깐 보인 뒤 Cognito 로그인 페이지로 보낸다.
// 실패하면(설정이 없음 등) onError로 알리고, App은 안내 화면에 오류를 보인다.
// mock 모드에서는 페이지를 옮기지 않고 로그인된 것으로 바뀐다 (onSignedIn)
export function AutoLogin({ onSignedIn, onError }: { onSignedIn: () => Promise<void>; onError: (message: string) => void }) {
    const [leaving, setLeaving] = useState(false);

    useEffect(() => {
        const quick = reducedMotion();
        const leave = window.setTimeout(() => setLeaving(true), quick ? 0 : SHOW_MS);
        const go = window.setTimeout(
            async () => {
                try {
                    await login();
                    await onSignedIn();
                } catch (error) {
                    setLeaving(false);
                    onError(getErrorText(error));
                }
            },
            quick ? 0 : SHOW_MS + LEAVE_MS,
        );
        // 개발 모드(StrictMode)는 효과를 두 번 돌린다: 앞의 예약을 지워야 로그인 페이지로 두 번 가지 않는다
        return () => {
            window.clearTimeout(leave);
            window.clearTimeout(go);
        };
    }, [onSignedIn, onError]);

    return <LoginSplash leaving={leaving} message="로그인 화면으로 이동 중" />;
}
