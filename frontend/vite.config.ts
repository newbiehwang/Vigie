// vite.config.ts
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

// 환경 값은 저장소 루트의 .env 하나에 모은다 (.env.example 참고). deploy.sh가 배포할 때 아래 값을 채운다.
// 같은 파일에 ANTHROPIC_API_KEY 같은 비밀 값도 있으므로, 브라우저 번들에 넣는 값을 정해 두고 그것만 넣는다.
const ENV_DIR = path.resolve(__dirname, '..');

export default defineConfig(({ mode }) => {
    // VITE_ 접두사가 없는 값(AWS_REGION 등)도 읽도록 ''. 읽기만 하고 번들에는 아래 define의 값만 들어간다
    // mock 모드(데모)는 Cognito를 쓰지 않으므로 배포 환경의 값을 번들에 넣지 않는다 (공개 데모 사이트에 남지 않게)
    const env = mode === 'mock' ? ({} as Record<string, string>) : loadEnv(mode, ENV_DIR, '');

    return {
        plugins: [react()],
        // import.meta.env.VITE_*(VITE_API_DEST)도 루트 .env에서 읽는다.
        // envPrefix는 기본값(VITE_)을 그대로 둔다: 바꾸면 그 접두사로 시작하는 값이 모두 번들에 들어간다
        envDir: ENV_DIR,
        resolve: {
            alias: {
                '@': path.resolve(__dirname, './src'),
            },
        },
        server: {
            port: 5173,
        },
        define: {
            // 브라우저 번들에 넣을 값만 골라 넣는다. 비밀 값(ANTHROPIC_API_KEY, AWS 키 등)은 넣지 않는다:
            // loadEnv(..., '')는 .env의 모든 값과 셸 환경 변수까지 읽으므로 배포자의 AWS 자격 증명도 들어 있을 수 있다.
            'import.meta.env.AWS_REGION': JSON.stringify(env.AWS_REGION),
            'import.meta.env.USER_POOL_ID': JSON.stringify(env.USER_POOL_ID),
            'import.meta.env.COGNITO_CLIENT_ID': JSON.stringify(env.COGNITO_CLIENT_ID),
            'import.meta.env.COGNITO_DOMAIN': JSON.stringify(env.COGNITO_DOMAIN),
        },
    };
});
