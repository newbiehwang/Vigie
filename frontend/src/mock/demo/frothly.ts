// 데모 데이터: 공개된 실제(가상 회사) AWS 운영 기록을 지금 시각에 맞춰 옮긴 것.
//   frothly.json ← scripts/demo_data/build_frothly.py가 만든다 (출처·뽑는 방법은 그 스크립트의 설명)
//   - Splunk BOTS v3 (CC0 1.0): 가상 회사 Frothly의 AWS 계정 기록 약 6시간 (CloudTrail·CloudWatch 지표·Config·리소스 목록)
//   - FinOps Foundation FOCUS 1.0 Sample Data (CC BY 4.0): 실제 AWS 청구를 익명화한 표. 서비스별 비중만 쓴다
// 데모는 이 계정에 Vigie(dev)를 설치해 쓰는 상황이다: Frothly의 리소스와 Vigie 자신의 리소스(vigie-*)가 함께 보인다.
//
// 기록에 없는 값은 이 파일에서 만든다. 만든 값은 '만든 값'이라고 적어 둔다:
//   - 알람 정의: CloudTrail 지표 필터 알람(CIS AWS Foundations 권고)과 ALB·RDS 알람. 알람이 울린 까닭은 실제 기록이다
//   - 비용 금액: 월 예상액(MONTHLY_FORECAST)을 정하고 FOCUS의 서비스 비중으로 나눈다. 일별 흔들림도 만든 값이다
//   - 지표·기록 밖의 시간(예: 3시간보다 앞의 Lambda 오류)은 0으로 둔다
import raw from './frothly.json';
import type {
    DashboardAlarm,
    DashboardChange,
    DashboardData,
    DashboardFinding,
    DashboardResource,
} from '@/types/dashboard';

export interface TrailEvent {
    at: number; // 기록의 끝으로부터 몇 초 전 (음수)
    name: string; // eventName
    source: string; // eventSource 앞부분 (s3, ec2, iam, signin …)
    actor: string;
    ip?: string;
    region?: string;
    agent?: string;
    target?: string;
    error?: string;
    mfa?: boolean; // ConsoleLogin
    publicPermissions?: string[]; // PutBucketAcl: 모든 사용자(AllUsers)에게 준 권한
    rules?: { protocol: string; port: number | string | null; from: string[] }[]; // 보안 그룹 규칙
    status?: string; // UpdateAccessKey
    keyUser?: string;
}

interface MetricSeries {
    unit: string;
    period: number;
    average: number[];
    maximum: number[];
    sum: number[];
}

interface FrothlyData {
    sources: { name: string; license: string; url: string; changes?: string }[];
    company: string;
    account: string;
    region: string;
    originalEnd: string;
    trail: TrailEvent[];
    trailReads: { total: number; top: [string, number][] };
    trailTotal: number;
    actors: { actor: string; total: number; writes: number; denied: number }[];
    metrics: Record<string, MetricSeries>;
    config: { rule: string; type: string; resource: string }[];
    instances: { id: string; type: string; state: string; name: string | null; zone: string; monitoring: boolean }[];
    securityGroups: { id: string; name: string | null; openToWorld: { protocol: string; port: string | null }[]; instances: string[] }[];
    eips: { ip: string; attached: boolean }[];
    buckets: { name: string; calls: number }[];
    users: string[];
    cost: { service: string; share: number }[];
}

export const FROTHLY = raw as unknown as FrothlyData; // JSON은 튜플·유니온을 모른다: 모양은 build_frothly.py가 정한다
export const DEMO_SOURCES = FROTHLY.sources;
export const DEMO_REGION = FROTHLY.region;

// ---------------------------------------------------------------- 시각
// 기록의 끝을 페이지를 연 때의 5분 전으로 옮긴다 (대시보드를 다시 읽어도 같은 시각)
export const END = Math.floor(Date.now() / 1000) - 5 * 60;
export const epochOf = (offset: number) => END + offset;

// 한국 시각 HH:MM
export const kstClock = (epoch: number) =>
    new Date(epoch * 1000).toLocaleTimeString('ko-KR', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hour12: false });

// 3분 전 · 2시간 10분 전
export const agoText = (epoch: number) => {
    const s = Math.max(0, Math.floor(Date.now() / 1000) - epoch);
    if (s < 60) return '방금';
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}분 전`;
    const h = Math.floor(m / 60);
    return m % 60 ? `${h}시간 ${m % 60}분 전` : `${h}시간 전`;
};

// 답변에 쓰는 시각 표기: "14:02 (1시간 12분 전)"
export const when = (offset: number) => `${kstClock(epochOf(offset))} (${agoText(epochOf(offset))})`;

// Vigie는 계정 ID를 끝 네 자리만 남기고 가린다 (services/llm/redaction.py). 답변도 그렇게 보인다
export const MASKED_ACCOUNT = `********${FROTHLY.account.slice(-4)}`;

// ---------------------------------------------------------------- 기록 고르기
export const events = (name: string | string[]) => {
    const names = Array.isArray(name) ? name : [name];
    return FROTHLY.trail.filter((event) => names.includes(event.name));
};

// 지표 한 줄: 5분마다, 마지막 점이 기록의 끝
export const metric = (key: string) => FROTHLY.metrics[key];
const sum = (values: number[]) => values.reduce((total, value) => total + value, 0);
const avg = (values: number[]) => (values.length ? sum(values) / values.length : 0);
export const round = (value: number, digits = 1) => Math.round(value * 10 ** digits) / 10 ** digits;
export const metricAverage = (key: string) => round(avg(metric(key).average));
export const metricMax = (key: string) => round(Math.max(...metric(key).maximum));
export const metricSum = (key: string) => Math.round(sum(metric(key).sum));
// 합이 가장 큰 5분 칸이 몇 초 전인가
export const peakOffset = (key: string) => {
    const values = metric(key).sum;
    const index = values.indexOf(Math.max(...values));
    return -(values.length - 1 - index) * metric(key).period;
};

// 오늘 Auto Scaling이 새로 띄운 인스턴스 수와, 사람이 직접 종료한 인스턴스
export const asgLaunches = () => events('RunInstances').filter((e) => e.actor === 'AWSServiceRoleForAutoScaling').length;
export const manualTerminations = () => events('TerminateInstances').filter((e) => e.actor !== 'AWSServiceRoleForAutoScaling');

// 사람이 읽을 변경 요약. 서버(dashboard_view.build_changes)와 같은 모양: 이벤트 이름 · 대상, 대상이 없으면 (서비스)
export const changeSummary = (event: TrailEvent) =>
    event.name + (event.target ? ` · ${event.target}` : ` (${event.source}.amazonaws.com)`);

// 조회가 아닌 기록 (콘솔 로그인·콘솔 출력 보기·거부된 호출은 변경이 아니다)
const isChange = (event: TrailEvent) =>
    !event.error && !['ConsoleLogin', 'GetConsoleOutput'].includes(event.name);

// ---------------------------------------------------------------- 알람 (정의는 만든 값, 울린 까닭은 실제 기록)
const firstAfter = (name: string, predicate: (e: TrailEvent) => boolean = () => true) =>
    events(name).find(predicate);

export const demoAlarms = (): { total: number; firing: DashboardAlarm[] } => {
    const sgChange = firstAfter('AuthorizeSecurityGroupIngress');
    const noMfa = [...events('ConsoleLogin')].reverse().find((e) => e.mfa === false);
    const firing: DashboardAlarm[] = [];
    if (sgChange)
        firing.push({ name: 'frothly-cis-security-group-changes', metric: 'SecurityGroupEventCount ≥ 1 (5분)', since: epochOf(sgChange.at) });
    if (noMfa)
        firing.push({ name: 'frothly-cis-console-signin-without-mfa', metric: 'ConsoleSigninWithoutMFA ≥ 1 (5분)', since: epochOf(noMfa.at) });
    // CIS 알람 9개 + ALB 정상 호스트·RDS CPU·Lambda 오류 + Auto Scaling의 목표 추적 알람 2개
    return { total: 14, firing };
};

// ---------------------------------------------------------------- Lambda 오류 (지난 24시간, 1시간마다)
// 지표는 기록의 끝 앞 3시간(5분 × 36)만 있다. 그 앞은 0으로 둔다
export const LAMBDA_ERROR_KEYS = { RDSAuditLogs: 'rdsAuditLogsErrors', VPCFlowLogs: 'vpcFlowLogsErrors' } as const;

export const demoErrors = () => {
    const hourly = Array.from({ length: 24 }, () => 0);
    for (const key of Object.values(LAMBDA_ERROR_KEYS)) {
        const values = metric(key).sum;
        values.forEach((value, index) => {
            const secondsAgo = (values.length - 1 - index) * metric(key).period + 5 * 60; // 끝이 5분 전
            const hourAgo = Math.floor(secondsAgo / 3600);
            if (hourAgo < 24) hourly[23 - hourAgo] += value;
        });
    }
    return { total24h: sum(hourly), previous24h: 0, hourly };
};

// ---------------------------------------------------------------- 비용 (금액은 만든 값, 서비스 비중은 FOCUS 샘플)
const MONTHLY_FORECAST = 1180; // 이번 달 예상 (USD). 이 계정 규모(t2 인스턴스 몇 대·RDS 1대·ALB·Marketplace 모니터링)에 맞춰 정했다

// FOCUS의 서비스 이름을 화면에 맞게 줄인다
const SERVICE_LABEL: Record<string, string> = {
    'Amazon Elastic Compute Cloud': 'EC2',
    Datadog: 'Datadog', // AWS Marketplace로 산 모니터링 구독
    AmazonCloudWatch: 'CloudWatch',
    'Amazon Relational Database Service': 'RDS',
    'Amazon Elastic File System': 'EFS',
    'AWS Config': 'Config',
    'Amazon Simple Storage Service': 'S3',
    'Amazon Elastic Container Service for Kubernetes': 'EKS',
    'Elastic Load Balancing': 'ELB',
};
export const serviceLabel = (service: string) => SERVICE_LABEL[service] ?? service;

// 날짜마다 같은 값이 나오는 가짜 난수 (0~1). 새로 고쳐도 차트가 흔들리지 않게
const wobble = (n: number) => {
    const x = Math.sin(n * 12.9898) * 43758.5453;
    return x - Math.floor(x);
};

export const demoCost = () => {
    const today = new Date();
    const year = today.getFullYear();
    const month = today.getMonth();
    const dayOfMonth = today.getDate();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const pad = (n: number) => String(n).padStart(2, '0');
    const perDay = MONTHLY_FORECAST / daysInMonth;
    // 1일부터 어제까지 (비용은 하루 늦게 확정된다). 주말이 조금 낮고, 날마다 ±6% 흔들린다
    const daily = Array.from({ length: Math.max(dayOfMonth - 1, 1) }, (_, i) => {
        const day = i + 1;
        const weekday = new Date(year, month, day).getDay();
        const weekend = weekday === 0 || weekday === 6 ? 0.9 : 1.04;
        return {
            date: `${year}-${pad(month + 1)}-${pad(day)}`,
            amount: round(perDay * weekend * (0.94 + wobble(day + month * 31) * 0.12), 2),
        };
    });
    const monthToDate = round(sum(daily.map((d) => d.amount)), 2);
    const byService = FROTHLY.cost.map((item) => ({
        service: serviceLabel(item.service),
        amount: round((monthToDate * item.share) / 100, 2),
    }));
    return {
        monthToDate,
        lastMonthSamePeriod: round(monthToDate / 1.064, 2),
        forecast: Math.round((monthToDate / daily.length) * daysInMonth),
        daily,
        byService,
    };
};

// ---------------------------------------------------------------- 리소스 · 변경 · 개선 권고
export const PUBLIC_BUCKET = 'frothlywebcode';
export const FORENSIC_INSTANCE = 'i-08e52f8b5a034012d';

// 데모 대화에서 승인해 바꾼 상태 (mock/api.ts의 mockResources가 들고 있다). 대시보드와 답이 이것을 따른다
export interface DemoState {
    forensicStopped: boolean; // 조사용 인스턴스를 중지했다 (setEc2InstanceState)
    webcodeBlocked: boolean; // frothlywebcode의 퍼블릭 액세스 차단을 켰다 (enableS3PublicAccessBlock)
}
export const INITIAL_DEMO_STATE: DemoState = { forensicStopped: false, webcodeBlocked: false };

export const idleInstance = () => FROTHLY.instances.find((i) => i.id === FORENSIC_INSTANCE)!;
export const webInstance = () => FROTHLY.instances.find((i) => i.name === 'WebServers')!;

export const demoResources = (state: DemoState = INITIAL_DEMO_STATE): DashboardResource[] => {
    const errors = (key: string) => metricSum(key);
    const rdsErrors = errors(LAMBDA_ERROR_KEYS.RDSAuditLogs);
    const vpcErrors = errors(LAMBDA_ERROR_KEYS.VPCFlowLogs);
    const web = webInstance();
    const idle = idleInstance();
    return [
        ...demoAlarms().firing.map((alarm) => ({
            id: alarm.name,
            kind: 'Alarm' as const,
            status: 'fail' as const,
            detail: `ALARM · ${alarm.metric}`,
        })),
        {
            id: 'RDSAuditLogs',
            kind: 'Lambda',
            status: rdsErrors ? 'fail' : 'ok',
            detail: `오류 ${rdsErrors}건 · ${kstClock(epochOf(peakOffset(LAMBDA_ERROR_KEYS.RDSAuditLogs)))} 전후에 몰림`,
            errors24h: rdsErrors,
        },
        { id: 'VPCFlowLogs', kind: 'Lambda', status: vpcErrors ? 'warn' : 'ok', detail: `오류 ${vpcErrors}건`, errors24h: vpcErrors },
        state.webcodeBlocked
            ? { id: PUBLIC_BUCKET, kind: 'S3', status: 'ok', detail: '퍼블릭 액세스 차단 모두 적용 · 대화에서 승인해 켬' }
            : {
                  id: PUBLIC_BUCKET,
                  kind: 'S3',
                  status: 'warn',
                  detail: '퍼블릭 액세스 차단 해제 · 오늘 공개 읽기·쓰기 ACL이 걸렸다 풀림',
              },
        // 중지한 인스턴스는 서버처럼 '데이터 없음'으로 보인다 (services/llm/dashboard_view.py의 _ec2_row)
        state.forensicStopped
            ? { id: idle.id, label: idle.name ?? undefined, kind: 'EC2', status: 'none', detail: '상태 stopped' }
            : {
                  id: idle.id,
                  label: idle.name ?? undefined,
                  kind: 'EC2',
                  status: 'warn',
                  detail: `${idle.type} · CPU 평균 ${metricAverage('forensicCpu')}% · 유휴`,
              },
        {
            // 이 그룹의 인스턴스는 오늘 여러 번 바뀌어 ID 대신 그룹 이름으로 보인다
            id: 'WebServers',
            kind: 'EC2',
            status: 'ok',
            detail: `Auto Scaling 그룹 · ${web.type} · 상태 검사 통과 · CPU 평균 ${metricAverage('asgCpu')}% · 오늘 ${asgLaunches()}번 교체`,
        },
        { id: 'vigie-llm-dev', kind: 'Lambda', status: 'ok', detail: '오류 없음', errors24h: 0 },
        { id: 'vigie-mcp-dev', kind: 'Lambda', status: 'ok', detail: '오류 없음', errors24h: 0 },
        { id: 'frothlyweblogs', kind: 'S3', status: 'ok', detail: '퍼블릭 액세스 차단 모두 적용' },
        { id: 'frothlyinvestigations', kind: 'S3', status: 'ok', detail: '퍼블릭 액세스 차단 모두 적용' },
        { id: `cloudtrail-${FROTHLY.account}`, kind: 'S3', status: 'ok', detail: '퍼블릭 액세스 차단 모두 적용' },
        { id: 'vigie-slackbot-dev', kind: 'Lambda', status: 'none', detail: '호출 없음', errors24h: 0 },
    ];
};

export const demoChanges = (): DashboardChange[] =>
    FROTHLY.trail
        .filter(isChange)
        .map((event) => ({ at: epochOf(event.at), source: 'cloudtrail' as const, actor: event.actor, summary: changeSummary(event) }))
        .reverse();

export const demoFindings = (state: DemoState = INITIAL_DEMO_STATE): DashboardFinding[] => {
    const idle = idleInstance();
    const findings: DashboardFinding[] = [];
    if (!state.webcodeBlocked)
        findings.push({ kind: 'public-s3', status: 'fail', title: '퍼블릭 액세스 차단 미적용 S3 버킷 1개', detail: PUBLIC_BUCKET });
    if (!state.forensicStopped)
        findings.push({
            kind: 'idle-ec2',
            status: 'warn',
            title: '유휴 EC2 인스턴스 1대',
            detail: `${idle.name} · 최근 CPU 평균 ${metricAverage('forensicCpu')}%`,
        });
    return findings;
};

// 대시보드 중 데모 데이터로 채우는 부분 (승인 대기·이 앱의 변경·Vigie 알람 상태는 mock/api.ts가 더한다)
export const demoDashboard = (): Pick<DashboardData, 'region' | 'alarms' | 'errors' | 'cost'> => ({
    region: DEMO_REGION,
    alarms: demoAlarms(),
    errors: demoErrors(),
    cost: demoCost(),
});
