/// <reference types="vite/client" />

// vite.config.ts가 넣어 주는 값 (저장소 루트의 .env에서 읽는다. deploy.sh가 배포할 때 채운다)
interface ImportMetaEnv {
    readonly AWS_REGION?: string;
    readonly USER_POOL_ID?: string;
    readonly COGNITO_CLIENT_ID?: string;
    readonly COGNITO_DOMAIN?: string; // Cognito 도메인 앞부분 (vigie-auth-<env>-<계정ID>)
    readonly VITE_API_DEST?: string;
}
