// 로그인 화면 (AXPI LoginPage.tsx의 카드 모양). '(AWS 로고)에서 로그인' 버튼 하나만 있다.
// 누르면 AWS(Cognito) 로그인 페이지로 간다. 가입·이메일 인증·비밀번호 찾기도 그 페이지에서 한다 (auth/authClient.ts).
import { useState } from 'react';
import { login } from '@/auth/authClient';
import { getErrorText } from '@/utils/formatters';
import { AwsLogo } from './AwsLogo';
import { LogoMark } from './LogoMark';

export function LoginPage({
    errorMessage: initialError = '',
    onSignedIn,
}: {
    errorMessage?: string; // Cognito 로그인 페이지에서 돌아왔는데 실패한 경우 (App이 넘겨준다)
    onSignedIn: () => Promise<void>; // mock 모드처럼 페이지를 옮기지 않고 로그인된 경우
}) {
    const [isRedirecting, setIsRedirecting] = useState(false);
    const [errorMessage, setErrorMessage] = useState(initialError);

    const handleLogin = async () => {
        setErrorMessage('');
        setIsRedirecting(true);
        try {
            await login();
            // 실제 환경에서는 여기서 Cognito 페이지로 넘어가므로 아래는 mock 모드에서만 실행된다
            await onSignedIn();
            setIsRedirecting(false);
        } catch (error) {
            setErrorMessage(getErrorText(error));
            setIsRedirecting(false);
        }
    };

    return (
        <main className="login-shell">
            <section className="login-card" aria-label="로그인">
                <div className="login-brand">
                    <LogoMark />
                    <div className="login-brand-text">
                        <h1 className="login-title">로그인</h1>
                        <p className="login-description">AWS 인증이 필요합니다.</p>
                    </div>
                </div>

                <div className="login-form">
                    {errorMessage ? (
                        <div className="login-error-alert" role="alert" aria-live="polite">
                            <div className="login-error-icon" aria-hidden="true">
                                !
                            </div>
                            <div className="login-error-content">
                                <p className="login-error-title">로그인에 실패했습니다.</p>
                                <p className="login-error-text">{errorMessage}</p>
                            </div>
                        </div>
                    ) : null}

                    {/* 로고가 'AWS' 글자를 대신해 '(AWS 로고)에서 로그인'으로 읽힌다.
                        로고는 화면 읽기 프로그램에 보이지 않으므로(aria-hidden) 버튼 이름은 aria-label로 준다 */}
                    <button
                        className="login-submit"
                        type="button"
                        disabled={isRedirecting}
                        onClick={handleLogin}
                        aria-label={isRedirecting ? '로그인 페이지로 이동 중' : 'AWS에서 로그인'}
                    >
                        <AwsLogo className="login-submit-logo" />
                        <span>{isRedirecting ? '로그인 페이지로 이동 중...' : '에서 로그인'}</span>
                    </button>
                </div>
            </section>
        </main>
    );
}
