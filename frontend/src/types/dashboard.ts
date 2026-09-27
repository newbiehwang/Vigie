// 홈 대시보드 (GET /dashboard). 운영자가 홈에서 5초 안에 알고 싶은 것을 급한 차례로 담는다:
//   지금 문제(알람·오류) → 내가 할 일(승인 대기) → 돈(이번 달 비용) → 리소스 상태 → 최근 변경 → 개선 권고
// 값은 수집 Lambda(services/dashboard)가 구역마다 모아 둔 것이고, 서버(services/llm/dashboard_view.py)가 합쳐 준다.
// 홈을 열 때마다 AWS를 부르지 않는다 (비용 API는 부를 때마다 요금이 나간다). 구역마다 모으는 때가 다르다:
//   alarms·resources·changes: AWS 이벤트로 바로(몇 초~몇 분) + 5분마다 · errors: 5분마다 · usage: 1시간마다 · cost: 하루 1번
// 한 번도 모으지 못한 구역은 null이다 (alarms·errors·cost). sections에 구역마다 모은 때·성공 여부가 있다

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
    costMonth?: number; // 이번 달 비용 (USD). 실제 서버는 주지 않는다 (리소스 단위 비용은 유료 설정이 필요하다)
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
}

export type SectionName = 'alarms' | 'resources' | 'errors' | 'changes' | 'usage' | 'cost';

// 구역 하나의 모은 상태. ok가 false면 지난번 성공 값(lastSuccessAt)을 그대로 보인다
export interface SectionState {
    ok: boolean;
    error?: string | null;
    collectedAt?: number | null; // 마지막으로 모은(성공·실패) 때
    lastSuccessAt?: number | null;
}

export interface DashboardData {
    generatedAt: number | null; // 가장 최근에 모은 때 (초). 아무것도 모으지 않았으면 null
    sections: Partial<Record<SectionName, SectionState>>;
    env: string; // dev · prod
    region: string;
    alarms: { total: number; firing: DashboardAlarm[] } | null;
    errors: {
        total24h: number;
        previous24h: number; // 그 전 24시간 (비교)
        hourly: number[]; // 지난 24시간, 1시간마다 (오래된 것부터)
    } | null;
    cost: {
        monthToDate: number; // 이번 달 1일부터 어제까지 (USD)
        lastMonthSamePeriod: number; // 지난달 같은 날짜까지
        forecast: number; // 이번 달 예상
        daily: { date: string; amount: number }[]; // YYYY-MM-DD, 이번 달 1일부터
        byService: { service: string; amount: number }[]; // 큰 것부터
    } | null;
    approvals: { pending: number; soonestExpiresAt?: number; unavailable?: boolean };
    resources: DashboardResource[]; // 표의 줄: 문제·주의 먼저, 최대 40줄
    resourceCounts?: Record<HealthStatus, number>; // 상태별 개수 (잘리기 전 모든 리소스)
    resourceTotal?: number;
    // 최근 것부터. 관리자(admins 그룹)가 아니면 null: 누가 무엇을 바꿨나(CloudTrail·감사 로그)는 관리자만 본다.
    // 대화의 CloudTrail 조회가 관리자 전용인 것과 같은 범위 (services/llm/dashboard_view.py). 화면은 카드를 그리지 않는다
    changes: DashboardChange[] | null; // 최근 10개까지 (카드 높이가 고정이라 한 번에 그만큼만 보인다)
    changesTotal?: number | null; // 24시간 안의 전체 변경 수 (10개보다 많으면 카드가 'N건 중 최근 10건'을 보인다)
    findings: DashboardFinding[];
}
