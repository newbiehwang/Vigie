// 홈 대시보드 (GET /dashboard). 운영자가 홈에서 5초 안에 알고 싶은 것을 급한 차례로 담는다:
//   지금 문제(알람·오류) → 내가 할 일(승인 대기) → 돈(이번 달 비용) → 리소스 상태 → 최근 변경 → 치울 것
// 값은 서버가 주기적으로 모아 둔 것이다 (generatedAt). 비용 API(Cost Explorer)는 부를 때마다 요금이 나가므로
// 홈을 열 때마다 AWS를 부르지 않는다. 지금은 mock만 있다 (서버 집계는 아직 없다)

// 상태: 문제 · 주의 · 정상 · 데이터 없음 (감사 로그 7계층의 fail·warn·ok·info와 같은 색)
export type HealthStatus = 'fail' | 'warn' | 'ok' | 'none';

export type ResourceKind = 'Lambda' | 'EC2' | 'S3' | 'Logs' | 'Alarm';

export interface DashboardAlarm {
    name: string;
    metric: string; // 예: "5XXError > 5 (5분)"
    since: number; // 울리기 시작한 때 (초, epoch)
}

export interface DashboardResource {
    id: string; // 리소스 이름 (함수·인스턴스·버킷·로그 그룹)
    label?: string; // 사람이 붙인 이름 (EC2 Name 태그 등)
    kind: ResourceKind;
    status: HealthStatus;
    detail: string; // 상태의 까닭 한 줄 (예: "오류율 4.2% (24시간)")
    errors24h?: number;
    costMonth?: number; // 이번 달 비용 (USD)
    changedAt?: number; // 마지막 변경 (초)
}

export interface DashboardChange {
    at: number; // 초
    source: 'app' | 'cloudtrail'; // 이 앱에서 승인한 변경인가, CloudTrail에만 있는 변경인가
    actor: string;
    summary: string;
}

export interface DashboardFinding {
    kind: 'idle-ec2' | 'public-s3' | 'log-retention' | 'alarm-muted';
    status: Exclude<HealthStatus, 'ok' | 'none'>;
    title: string;
    detail: string;
    savingsMonthly?: number; // 치우면 줄어드는 월 비용 (USD)
    question: string; // 누르면 대화로 보낼 질문
}

export interface DashboardData {
    generatedAt: number; // 모은 때 (초)
    env: string; // dev · prod
    region: string;
    alarms: { total: number; firing: DashboardAlarm[] };
    errors: {
        total24h: number;
        previous24h: number; // 그 전 24시간 (비교)
        hourly: number[]; // 지난 24시간, 1시간마다 (오래된 것부터)
    };
    cost: {
        monthToDate: number; // 이번 달 1일부터 어제까지 (USD)
        lastMonthSamePeriod: number; // 지난달 같은 날짜까지
        forecast: number; // 이번 달 예상
        daily: { date: string; amount: number }[]; // YYYY-MM-DD, 이번 달 1일부터
        byService: { service: string; amount: number }[]; // 큰 것부터
    };
    approvals: { pending: number; soonestExpiresAt?: number };
    resources: DashboardResource[];
    changes: DashboardChange[]; // 최근 것부터
    findings: DashboardFinding[];
}
