// 자주 묻는 질문 예시 (홈 화면과 빈 대화 화면에서 쓴다). 분류마다 두 개씩
// adminOnly: 관리자 전용 도구(CloudTrail·IAM 등, mcp/lambda_mcp/risk.py의 ADMIN_ONLY)가 있어야 답할 수 있는 질문.
// 관리자가 아니면 보이지 않는다 (눌러도 '관리자만 볼 수 있다'는 답만 받으므로). 실제로 막는 곳은 서버다
import { isAdmin, type AuthUser } from '@/auth/authClient';

export interface ExampleQuestion {
    category: string;
    question: string;
    adminOnly?: boolean;
}

export const EXAMPLE_QUESTIONS: ExampleQuestion[] = [
    { category: '리소스 모니터링', question: '지난 24시간 동안 CPU 사용률이 가장 높았던 EC2 인스턴스는 무엇인가요?' },
    { category: '리소스 모니터링', question: '이번 달 메모리 사용량이 가장 많은 Lambda 함수 Top 3를 알려주세요.' },
    { category: '보안 감사', question: '최근 7일간 발생한 보안 이벤트를 심각도 순으로 정리해주세요.', adminOnly: true },
    { category: '보안 감사', question: '어제 루트 계정으로 로그인한 기록이 있나요?', adminOnly: true },
    { category: '비용 관리', question: '지난 달 대비 이번 달 비용이 가장 많이 증가한 서비스 3가지를 알려주세요.' },
    { category: '비용 관리', question: '비용 최적화를 위해 삭제 가능한 미사용 리소스가 있나요?' },
    { category: '권한 관리', question: '지난 일주일 간 IAM 권한이 변경된 사용자 목록을 보여주세요.', adminOnly: true },
    { category: '권한 관리', question: '현재 인프라에서 최소 권한 원칙에 위배되는 IAM 정책이 있나요?', adminOnly: true },
];

// 이 사용자에게 보일 예시 (관리자가 아니면 관리자 전용 질문을 뺀다)
export const examplesFor = (user: AuthUser | null) =>
    isAdmin(user) ? EXAMPLE_QUESTIONS : EXAMPLE_QUESTIONS.filter((example) => !example.adminOnly);
