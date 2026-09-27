// Cognito 로그인: 로그인 버튼을 누르면 Cognito 로그인 페이지(Hosted UI)로 가서 로그인하고 앱으로 돌아온다.
//
//   [로그인] → https://<도메인>.auth.<리전>.amazoncognito.com/login (아이디·비밀번호, 가입, 비밀번호 찾기)
//          → <앱 주소>/redirect?code=... → Amplify가 code를 토큰으로 바꾸고 저장 → Hub 'signInWithRedirect'
//
// 비밀번호는 Cognito 페이지만 받는다. 앱은 토큰만 다룬다 (OAuth 2.0 Authorization Code + PKCE).
// 돌아올 주소는 지금 열린 앱 주소 + /redirect로 정한다. User Pool Client의 CallbackURLs에 배포 주소와
// http://localhost:5173/redirect가 모두 등록되어 있어(cloudformation/base.yaml) 배포·로컬 어디서나 맞다.
//
// mock 모드(npm run dev:mock)에서는 Cognito 없이 로그인 버튼을 누르면 바로 로그인된다.
import { Amplify } from 'aws-amplify';
import { fetchAuthSession, signInWithRedirect, signOut } from 'aws-amplify/auth';
// /redirect로 돌아왔을 때 code를 토큰으로 바꾸는 처리를 켠다 (Amplify.configure 때 동작)
import 'aws-amplify/auth/enable-oauth-listener';
import { Hub } from 'aws-amplify/utils';

export interface AuthUser {
    email: string;
    displayName: string; // 이름이 없으면 이메일 앞부분
    groups: string[]; // Cognito 그룹 (ID 토큰의 cognito:groups). admins, approvers
}

// 관리자(admins 그룹)인가: 감사 로그·사용자 관리 탭을 보인다.
// 화면에서 숨기는 것은 편의일 뿐이다. 실제로 막는 곳은 서버다 (GET /audit은 관리자가 아니면 403, services/llm/audit.py)
export const ADMIN_GROUP = 'admins';
export const isAdmin = (user: AuthUser | null) => !!user?.groups.includes(ADMIN_GROUP);

// 권한 세 단계 (services/llm/user_admin.py의 role_of와 같은 규칙): admins면 관리자, approvers면 결정자, 없으면 일반 사용자
export type Role = 'member' | 'decider' | 'admin';
export const ROLE_LABELS: Record<Role, string> = { member: '일반 사용자', decider: '결정자', admin: '관리자' };
export const roleOf = (groups: string[]): Role =>
    groups.includes(ADMIN_GROUP) ? 'admin' : groups.includes('approvers') ? 'decider' : 'member';

// cognito:groups는 보통 문자열 배열이지만, 문자열 하나로 오는 경우도 받는다 (서버의 audit.groups_of와 같은 규칙)
function groupsOf(value: unknown): string[] {
    if (Array.isArray(value)) return value.map(String);
    if (typeof value === 'string') return value.replace(/^\[|\]$/g, '').split(/[\s,]+/).filter(Boolean);
    return [];
}

const MOCK = import.meta.env.MODE === 'mock';
// mock 사용자는 관리자다. 주소에 ?mock-role=member를 붙여 열면 일반 사용자로 화면을 확인한다 (mock 모드에서만)
const MOCK_MEMBER = MOCK && new URLSearchParams(window.location.search).get('mock-role') === 'member';
const MOCK_USER: AuthUser = {
    email: 'demo@example.com',
    displayName: 'Demo',
    groups: MOCK_MEMBER ? [] : [ADMIN_GROUP, 'approvers'],
};
export const mockIsAdmin = () => isAdmin(MOCK_USER); // mock API가 GET /audit에 403을 흉내 낼 때 쓴다
let mockSignedIn = true; // mock 모드는 로그인된 상태로 시작한다 (화면만 고칠 때 바로 보이게)

const USER_POOL_ID = import.meta.env.USER_POOL_ID;
const CLIENT_ID = import.meta.env.COGNITO_CLIENT_ID;
const COGNITO_DOMAIN = import.meta.env.COGNITO_DOMAIN; // 도메인 앞부분 (deploy.sh가 루트 .env에 적는다)
const REGION = import.meta.env.AWS_REGION;
const REDIRECT_PATH = '/redirect';
const configured = !!(USER_POOL_ID && CLIENT_ID && COGNITO_DOMAIN && REGION);

export function configureAuth() {
    if (MOCK || !configured) return;
    listenLoginResult(); // configure보다 먼저: code 교환 결과를 놓치지 않게
    const redirect = `${window.location.origin}${REDIRECT_PATH}`;
    Amplify.configure({
        Auth: {
            Cognito: {
                userPoolId: USER_POOL_ID!,
                userPoolClientId: CLIENT_ID!,
                loginWith: {
                    oauth: {
                        domain: `${COGNITO_DOMAIN}.auth.${REGION}.amazoncognito.com`,
                        // User Pool Client의 AllowedOAuthScopes와 같게 (cloudformation/base.yaml)
                        scopes: ['openid', 'email', 'profile'],
                        redirectSignIn: [redirect],
                        redirectSignOut: [redirect],
                        responseType: 'code',
                    },
                },
            },
        },
    });
}

// Cognito 로그인 페이지에서 돌아온 직후인가 (Amplify가 code를 토큰으로 바꾸는 중)
export function isReturningFromLogin(): boolean {
    if (MOCK || window.location.pathname !== REDIRECT_PATH) return false;
    const params = new URLSearchParams(window.location.search);
    return params.has('code') || params.has('error');
}

export type LoginResult = { ok: true } | { ok: false; message: string };

// 돌아온 뒤의 결과는 한 번만 알린다.
// Amplify는 configure 직후부터 code를 토큰으로 바꾸므로, 화면(App)이 결과를 들을 준비가 되기 전에 끝날 수도 있다.
// 그래서 듣는 쪽이 없으면 결과를 받아 두었다가 처음 듣는 쪽에 한 번 넘기고 지운다.
// 받아 둔 결과를 지우지 않으면, 나중에 다시 듣기 시작하는 쪽(예: 화면을 다시 그리며 구독을 새로 한 App)이
// 이미 끝난 로그인 결과를 또 받아 홈으로 다시 보내 버린다
let pendingResult: LoginResult | null = null;
const loginListeners = new Set<(result: LoginResult) => void>();

function publishLoginResult(result: LoginResult) {
    if (loginListeners.size === 0) {
        pendingResult = result;
        return;
    }
    loginListeners.forEach((listener) => listener(result));
}

function listenLoginResult() {
    Hub.listen('auth', ({ payload }) => {
        if (payload.event === 'signInWithRedirect') publishLoginResult({ ok: true });
        else if (payload.event === 'signInWithRedirect_failure') {
            const error = (payload.data as { error?: { message?: string } } | undefined)?.error;
            publishLoginResult({ ok: false, message: error?.message || 'Cognito 로그인을 끝내지 못했습니다.' });
        }
    });
}

// 결과를 알려 준다 (받아 둔 결과가 있으면 바로, 한 번만). 반환값은 구독 해제 함수
export function onLoginResult(handler: (result: LoginResult) => void) {
    loginListeners.add(handler);
    if (pendingResult) {
        const result = pendingResult;
        pendingResult = null;
        handler(result);
    }
    return () => {
        loginListeners.delete(handler);
    };
}

// 사용자 정보는 ID 토큰에서 읽는다. (fetchUserAttributes는 aws.cognito.signin.user.admin 범위가 필요한데
// Hosted UI로 받은 토큰에는 openid·email·profile만 있다)
// 그룹은 토큰을 받은 때의 것이다: 그룹을 바꿔도 ID 토큰이 갱신될 때까지(최대 1시간) 화면도 서버의 확인(토큰의 그룹)도
// 예전 그룹을 따른다. 바로 적용하려면 다시 로그인한다
export async function currentUser(): Promise<AuthUser | null> {
    if (MOCK) return mockSignedIn ? MOCK_USER : null;
    if (!configured) return null;
    try {
        const claims = (await fetchAuthSession()).tokens?.idToken?.payload;
        if (!claims) return null;
        const email = String(claims.email ?? '');
        const name = typeof claims.name === 'string' ? claims.name : '';
        return { email, displayName: name || email.split('@')[0], groups: groupsOf(claims['cognito:groups']) };
    } catch {
        return null; // 로그인하지 않았거나 세션이 만료됨
    }
}

// API 요청에 붙일 ID 토큰 (API Gateway의 Cognito Authorizer가 확인한다). 만료되면 Amplify가 갱신한다
export async function idToken(): Promise<string | null> {
    if (MOCK || !configured) return null;
    try {
        const session = await fetchAuthSession();
        return session.tokens?.idToken?.toString() ?? null;
    } catch {
        return null;
    }
}

// Cognito 로그인 페이지로 간다. 돌아오면 onLoginResult로 결과가 온다
export async function login(): Promise<void> {
    if (MOCK) {
        mockSignedIn = true;
        return;
    }
    if (!configured) {
        throw new Error(
            'Cognito 설정(USER_POOL_ID, COGNITO_CLIENT_ID, COGNITO_DOMAIN, AWS_REGION)이 없습니다. 저장소 루트의 .env에 값이 있는지 확인하세요 (deploy.sh가 배포할 때 채웁니다).',
        );
    }
    try {
        // lang=ko: Cognito 로그인 화면(managed login)을 한국어로 연다. Cognito가 쿠키에 기억해 다음 화면(가입·비밀번호 찾기)도 한국어다
        await signInWithRedirect({ options: { lang: 'ko' } });
    } catch (error) {
        // 다른 탭에서 이미 로그인했으면 그대로 쓴다 (앱이 세션을 다시 읽는다)
        if ((error as { name?: string })?.name === 'UserAlreadyAuthenticatedException') return;
        throw error;
    }
}

// 앱의 토큰을 지우고 Cognito 로그인 페이지의 세션도 끝낸다(Cognito /logout을 거쳐 앱으로 돌아온다).
// Cognito 쪽 세션을 남기면 다음 로그인 버튼이 비밀번호를 묻지 않고 바로 같은 계정으로 들어온다
export async function logout(): Promise<void> {
    if (MOCK) {
        mockSignedIn = false;
        return;
    }
    await signOut();
}
