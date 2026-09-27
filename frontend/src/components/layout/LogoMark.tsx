import vigieLogo from '@/assets/brand/vigie-logo.svg';

// 서비스 로고: Vigie 워드마크 (src/assets/brand). AXPI는 회사 로고 이미지를, 예전 이름(WGA) 때는 로봇 아이콘 + 'MCP AIOps' 글자를 썼다.
// 크기는 놓인 자리가 정한다 (위쪽 내비게이션 .top-nav, 로그인 카드 .login-brand: layout.css)
export function LogoMark() {
    return (
        <div className="logo-mark">
            <img className="logo-wordmark-image" src={vigieLogo} alt="Vigie" />
        </div>
    );
}
