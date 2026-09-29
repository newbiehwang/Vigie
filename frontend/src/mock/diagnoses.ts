// 목업 감사 로그의 진단 기록 (diagnoseService). 서버(mcp/lambda_mcp/diagnose.py)가 내는 것과 같은 모양이고,
// 데모 계정(Frothly, demo/frothly.ts)의 실제 CloudTrail 사건에 맞췄다:
//   - frothlywebcode: bstoll이 PutBucketAcl로 모든 사용자(AllUsers)에게 READ·WRITE를 준 뒤(-8014), 되돌리기 전(-4646)에 진단
//   - 웹 ALB: bstoll이 웹 서버 대상 세 대를 직접 종료한(-3332) 직후에 진단
//   - web_admin 키: 외부 IP 세 곳에서 권한을 더듬은 거부 4건(-21542~-20893) 뒤, bstoll이 키를 끄기(-20348) 전에 진단
// 장애 대응 흐름(대화 · 감사 로그, mock/api.ts의 '장애 대응')이 쓰는 뒤의 진단:
//   - 웹 ALB 복구 뒤: Auto Scaling이 새 대상을 띄운(-3271~-3024) 뒤. 대상은 정상이고, 최근 1시간의 5xx와 종료 기록이 남는다
//   - frothlywebcode 지금: ACL은 되돌려졌고(-4646), 퍼블릭 액세스 차단이 꺼져 있으면 주의 · 켜져 있으면 정상
//   - web_admin 키 지금: bstoll이 이미 끈 키. 거부된 시도(증상)는 24시간 안에 그대로 보인다
// 만든 값: ALB 이름(frothly-web-alb)·대상 그룹 이름, 지표 값, 버전 관리 상태. 사건(누가·언제·무엇을)은 실제 기록이다
import type { Diagnosis, DiagnosisCheck, DiagnosisLayer, DiagnosisLayerId, DiagnosisStatus } from '@/types/audit';
import { DEMO_REGION, epochOf, events } from './demo/frothly';

const NAMES: Record<DiagnosisLayerId, string> = {
    L1: 'AWS',
    L2: '리소스 변경 기록',
    L3: '네트워크 경로',
    L4: '로드 밸런서·게이트웨이',
    L5: '인스턴스·실행 환경',
    L6: '권한·한도',
    L7: '데이터·의존성',
};

const WEIGHT: Record<DiagnosisStatus, number> = { cause: 5, symptom: 4, warn: 3, ok: 2, unknown: 1, skip: 0 };

const check = (name: string, status: DiagnosisStatus, finding: string, evidence = ''): DiagnosisCheck => ({
    name,
    status,
    finding,
    evidence,
});

// 서버처럼: 층의 판정 = 가장 무거운 항목, 찾은 것 = 그 판정의 첫 항목
const layer = (id: DiagnosisLayerId, component: string, checks: DiagnosisCheck[]): DiagnosisLayer => {
    const status = checks.reduce<DiagnosisStatus>((top, c) => (WEIGHT[c.status] > WEIGHT[top] ? c.status : top), 'skip');
    return { id, name: NAMES[id], component, status, finding: checks.find((c) => c.status === status)?.finding ?? '', checks };
};

const HEALTH = check(
    'AWS Health 이벤트',
    'unknown',
    'AWS Health API는 Business Support 이상에서만 열려 조회하지 않았습니다. 콘솔의 계정별 Health Dashboard에서 이 리전·AZ의 이벤트를 확인하세요',
    'health:DescribeEvents',
);

const diagnosis = (
    service: string,
    serviceName: string,
    resource: string,
    summary: string,
    layers: DiagnosisLayer[],
    hours = 1, // 서버처럼 서비스마다 기본값 (자격 증명은 24시간)
): Diagnosis => ({
    service,
    serviceName,
    resource,
    hours,
    summary,
    causes: layers.filter((l) => l.status === 'cause').map((l) => l.id),
    layers,
});

// 서버의 _clock처럼 한국 시간 월-일 시:분. offset은 데모 기록의 끝(END)으로부터 몇 초 (음수면 전, 지금은 now())
const kstMinute = (epoch: number) => new Date((epoch + 9 * 3600) * 1000).toISOString().slice(5, 16).replace('T', ' ');
export const diagClock = (offset: number) => kstMinute(epochOf(offset));
export const diagNow = () => kstMinute(Math.floor(Date.now() / 1000));

// clock: 데모 기록의 끝으로부터 offset초 전의 한국 시간 (서버의 _clock처럼 월-일 시:분)
export const publicWebcodeDiagnosis = (clock: (offset: number) => string) => diagnosis(
    's3',
    'S3',
    'frothlywebcode',
    `원인 — L6 권한·한도: ACL이 모든 사람에게 열려 있습니다: AllUsers READ, AllUsers WRITE → 공개 노출 / 계기 L2 리소스 변경 기록: 관련 변경 1건: ${clock(-8014)} PutBucketAcl (bstoll → frothlywebcode)`,
    [
        layer('L1', 'S3 서비스', [HEALTH]),
        layer('L2', 'CloudTrail 쓰기 이벤트 (정책·ACL·차단)', [
            check('증상 직전의 변경', 'cause', `관련 변경 1건: ${clock(-8014)} PutBucketAcl (bstoll → frothlywebcode)`, 'cloudtrail:LookupEvents ReadOnly=false'),
        ]),
        layer('L3', 'VPC 엔드포인트', [
            check('해당 없음', 'skip', 'VPC 엔드포인트 정책은 이 절차가 보지 않습니다 (VPC 안에서만 403이면 엔드포인트 정책을 확인)'),
        ]),
        layer('L4', '앞단 (CloudFront 등)', [check('해당 없음', 'skip', 'S3 앞단(CloudFront 등)은 이 절차가 보지 않습니다')]),
        layer('L5', '관리형 (인스턴스 없음)', [check('해당 없음', 'skip', 'S3는 관리형 저장소라 인스턴스·실행 환경 층이 없습니다')]),
        layer('L6', '퍼블릭 액세스 차단 · 정책 · ACL · 요청 한도', [
            check('버킷 ACL', 'cause', 'ACL이 모든 사람에게 열려 있습니다: AllUsers READ, AllUsers WRITE → 공개 노출', 's3:GetBucketAcl'),
            check('요청 오류', 'unknown', '요청 지표가 없습니다 (버킷에 요청 지표를 켜야 403·503을 셀 수 있습니다)', 'AWS/S3 4xxErrors · 5xxErrors (FilterId EntireBucket)'),
        ]),
        layer('L7', '버전 관리 (복구 가능성)', [
            check('버전 관리', 'warn', '버전 관리가 꺼져 있어 지우거나 덮어쓴 객체를 되돌릴 수 없습니다', 's3:GetBucketVersioning'),
        ]),
    ],
);

const TARGETS = 'i-038ae43bc05053694, i-0920036c8ca91e501, i-0cc93bade2b3cba63';

export const webAlbDiagnosis = (clock: (offset: number) => string) => diagnosis(
    'alb',
    'Application Load Balancer',
    'frothly-web-alb',
    `원인 — L5 인스턴스·실행 환경: 인스턴스 3대 모두가 실행 중이 아닙니다: ${TARGETS} (WebServers) shutting-down (Client.UserInitiatedShutdown: User initiated shutdown) / 계기 L2 리소스 변경 기록: 관련 변경 1건: ${clock(-3332)} TerminateInstances (bstoll → i-038ae43bc05053694)`,
    [
        layer('L1', '리전·AZ · 대상 EC2 호스트', [
            check('시스템 상태 검사', 'ok', '인스턴스 3대의 시스템 상태 검사가 정상입니다', 'ec2:DescribeInstanceStatus SystemStatus'),
            HEALTH,
        ]),
        layer('L2', 'CloudTrail 쓰기 이벤트', [
            check('증상 직전의 변경', 'cause', `관련 변경 1건: ${clock(-3332)} TerminateInstances (bstoll → i-038ae43bc05053694)`, 'cloudtrail:LookupEvents ReadOnly=false'),
        ]),
        layer('L3', '서브넷 경로 · 보안 그룹 · NACL', [
            check('경로·보안 그룹·NACL', 'ok', '서브넷 경로, ALB·대상 보안 그룹, NACL이 리스너와 대상 포트를 허용합니다', 'ec2:DescribeRouteTables · DescribeSecurityGroups · DescribeNetworkAcls'),
        ]),
        layer('L4', 'ALB 리스너 · 대상 그룹 · 헬스체크', [
            check('대상 헬스', 'symptom', `대상 그룹 frothly-web-tg의 대상 3대가 모두 정상이 아닙니다 (Target.InvalidState 3). 모두 unhealthy면 ALB는 헬스와 무관하게 모든 대상으로 보냅니다 (fail-open)`, 'elbv2:DescribeTargetHealth'),
            check('ALB가 만든 5xx', 'symptom', 'ALB가 만든 5xx 212건 (502 64건 · 503 148건) / 요청 1,904건', 'AWS/ApplicationELB HTTPCode_ELB_5XX_Count'),
        ]),
        layer('L5', '대상 EC2', [
            check('인스턴스 상태', 'cause', `인스턴스 3대 모두가 실행 중이 아닙니다: ${TARGETS} (WebServers) shutting-down (Client.UserInitiatedShutdown: User initiated shutdown)`, 'ec2:DescribeInstances State·StateReason'),
        ]),
        layer('L6', '연결 한도', [check('연결 한도', 'ok', '거부한 연결이 없습니다', 'AWS/ApplicationELB RejectedConnectionCount')]),
        layer('L7', '대상 뒤 DB·외부 API', [
            check('대상 응답 시간', 'ok', '대상 응답 시간 최대 0.84초 (유휴 타임아웃 60초)', 'AWS/ApplicationELB TargetResponseTime Maximum'),
        ]),
    ],
);

// 유출된 web_admin 키: 쓰기는 없고 거부된 조회만 있다 (정찰). 배포 리전은 데모 계정의 리전(DEMO_REGION)이고,
// 거부된 호출이 간 리전은 기록에서 읽는다 (IAM처럼 전역 서비스는 서버 절차처럼 빼고 센다)
export const reconRegion = () =>
    events(['ListBuckets', 'DescribeAccountAttributes']).find((e) => e.actor === 'web_admin')?.region ?? DEMO_REGION;

// keyActive: 진단한 때 키가 켜져 있었나 (bstoll이 -20348에 끈 뒤면 false)
export const leakedKeyDiagnosis = (clock: (offset: number) => string, keyActive = true) =>
    diagnosis(
        'credential',
        '자격 증명 유출',
        'web_admin',
        `원인 층을 특정하지 못했습니다. 증상 — L3 네트워크 경로: 배포 리전(${DEMO_REGION}) 밖 1곳에서 호출했습니다: ${reconRegion()} (채굴은 여러 리전에 퍼뜨리는 경우가 많습니다) / L6 권한·한도: 권한 거부 4건: ListAccessKeys 1건, ListBuckets 1건, DescribeAccountAttributes 1건, GetUser 1건 (권한을 더듬어 본 흔적)`,
        [
            layer('L1', 'AWS (해당 없음)', [check('해당 없음', 'skip', 'AWS 쪽 장애가 아니라 자격 증명 사고입니다')]),
            layer('L2', '이 키가 바꾼 것 (쓰기 이벤트·기록 끄기)', [
                check('키가 바꾼 것', 'ok', '최근 24시간 동안 성공한 쓰기가 없습니다 (호출 4건)', 'cloudtrail:LookupEvents AccessKeyId'),
            ]),
            layer('L3', '쓴 곳 (IP · 도구 · 리전)', [
                check('쓴 리전', 'symptom', `배포 리전(${DEMO_REGION}) 밖 1곳에서 호출했습니다: ${reconRegion()} (채굴은 여러 리전에 퍼뜨리는 경우가 많습니다)`, 'cloudtrail awsRegion'),
                check('쓴 IP', 'symptom', 'IP 3곳에서 썼습니다: 139.198.18.205, 209.107.196.112, 82.102.18.111 …', 'cloudtrail sourceIPAddress'),
            ]),
            layer('L4', '앞단 (해당 없음)', [check('해당 없음', 'skip', '앞단이 없는 API 호출입니다')]),
            layer('L5', '인스턴스·함수 남용 (채굴 흔적 · GuardDuty)', [
                check('GuardDuty', 'unknown', 'GuardDuty를 조회하지 못했습니다 (AccessDeniedException)', 'guardduty:ListFindings'),
                check('인스턴스·함수 생성', 'ok', '인스턴스·함수·컨테이너를 만든 흔적이 없습니다', 'cloudtrail:LookupEvents'),
            ]),
            layer('L6', '지속성 확보 · 권한 더듬기 · 키 상태', [
                check('거부된 시도', 'symptom', `권한 거부 4건: ListAccessKeys 1건, ListBuckets 1건, DescribeAccountAttributes 1건, GetUser 1건 (권한을 더듬어 본 흔적, 처음 ${clock(-21542)})`, 'cloudtrail errorCode AccessDenied'),
                check('키 상태', 'ok', keyActive ? 'web_admin의 키 1개 활성' : `web_admin의 키 1개 비활성 (${clock(-20348)}에 bstoll이 끔)`, 'iam:ListAccessKeys'),
            ]),
            layer('L7', '데이터 접근 (비밀 값 · 버킷 공개 · 스냅샷 공유)', [
                check('데이터 접근', 'ok', '비밀 값 조회·버킷 공개·스냅샷 공유 흔적이 없습니다', 'cloudtrail:LookupEvents'),
                check('S3 객체 읽기', 'unknown', 'S3 GetObject는 데이터 이벤트라 관리 이벤트 조회로 보이지 않습니다 (데이터 이벤트 추적·S3 서버 액세스 로그로 확인)', 'cloudtrail 데이터 이벤트'),
            ]),
        ],
        24,
    );

// ---------------------------------------------------------------- 장애 대응 흐름의 뒤 진단 (mock/api.ts '장애 대응')

// 웹 ALB, 대상이 교체되어 복구된 뒤. 대상 2대가 정상이고(정상 호스트 지표도 2대), 최근 1시간의 5xx(증상)와
// 그 직전의 종료 기록(계기)이 남는다. 서버처럼 고장 난 층을 못 찾고 L2가 원인이면 요약은 '증상 직전의 변경'
export const webAlbRecoveredDiagnosis = (clock: (offset: number) => string, errors: { total: number; e502: number; e503: number; requests: number }) => {
    const changes = `관련 변경 2건: ${clock(-3332)} TerminateInstances (bstoll → i-038ae43bc05053694), ${clock(-6296)} TerminateInstances (bstoll → i-0003b600f157dbc49)`;
    const fiveXX = `ALB가 만든 5xx ${errors.total}건 (502 ${errors.e502}건 · 503 ${errors.e503}건) / 요청 ${errors.requests.toLocaleString()}건`;
    return diagnosis(
        'alb',
        'Application Load Balancer',
        'frothly-web-alb',
        `원인 — 증상 직전의 변경(L2): ${changes} / 증상 — L4 로드 밸런서·게이트웨이: ${fiveXX}`,
        [
            layer('L1', '리전·AZ · 대상 EC2 호스트', [
                check('시스템 상태 검사', 'ok', '인스턴스 2대의 시스템 상태 검사가 정상입니다', 'ec2:DescribeInstanceStatus SystemStatus'),
                HEALTH,
            ]),
            layer('L2', 'CloudTrail 쓰기 이벤트', [check('증상 직전의 변경', 'cause', changes, 'cloudtrail:LookupEvents ReadOnly=false')]),
            layer('L3', '서브넷 경로 · 보안 그룹 · NACL', [
                check('경로·보안 그룹·NACL', 'ok', '서브넷 경로, ALB·대상 보안 그룹, NACL이 리스너와 대상 포트를 허용합니다', 'ec2:DescribeRouteTables · DescribeSecurityGroups · DescribeNetworkAcls'),
            ]),
            layer('L4', 'ALB 리스너 · 대상 그룹 · 헬스체크', [
                check('ALB가 만든 5xx', 'symptom', fiveXX, 'AWS/ApplicationELB HTTPCode_ELB_5XX_Count'),
                check('대상 헬스', 'ok', '대상 그룹 frothly-web-tg의 대상 2대가 모두 healthy입니다', 'elbv2:DescribeTargetHealth'),
            ]),
            layer('L5', '대상 EC2', [
                check('인스턴스 상태', 'ok', '대상 2대 모두 실행 중입니다 (Auto Scaling이 새로 띄운 인스턴스)', 'ec2:DescribeInstances State'),
                check('CPU', 'ok', 'CPU 최대 2.4% (과부하 기준 90%)', 'AWS/EC2 CPUUtilization Maximum'),
            ]),
            layer('L6', '연결 한도', [check('연결 한도', 'ok', '거부한 연결이 없습니다', 'AWS/ApplicationELB RejectedConnectionCount')]),
            layer('L7', '대상 뒤 DB·외부 API', [
                check('대상 응답 시간', 'ok', '대상 응답 시간 최대 0.62초 (유휴 타임아웃 60초)', 'AWS/ApplicationELB TargetResponseTime Maximum'),
            ]),
        ],
    );
};

// frothlywebcode, ACL을 되돌린 뒤. 공개 ACL·정책이 없으니 원인은 없고, 퍼블릭 액세스 차단이 꺼져 있으면 L6 주의,
// 켜져 있으면 L6 정상(서버처럼 '요청 오류'가 확인 불가뿐이면 '공개·권한' 정상을 더한다). 버전 관리(L7)는 계속 주의.
// changes: 보는 시간(최근 1시간 + 앞 1시간) 안의 관련 변경 (새것부터, 서버의 '시각 이벤트 (누가 → 무엇)')
export const webcodeDiagnosis = (blocked: boolean, changes: string[]) => {
    const listed = changes.slice(0, 3).join(', ') + (changes.length > 3 ? ` 외 ${changes.length - 3}건` : '');
    return diagnosis('s3', 'S3', 'frothlywebcode', '이 절차가 보는 범위에서는 이상이 없습니다', [
        layer('L1', 'S3 서비스', [HEALTH]),
        layer('L2', 'CloudTrail 쓰기 이벤트 (정책·ACL·차단)', [
            changes.length
                ? check('관련 변경', 'ok', `변경 ${changes.length}건이 있었지만 다른 층에 이상이 없습니다: ${listed}`, 'cloudtrail:LookupEvents ReadOnly=false')
                : check('CloudTrail 쓰기 이벤트', 'ok', '최근 2시간 동안 관련 쓰기 이벤트가 없습니다 (CloudTrail은 보통 5분쯤 늦게 보입니다)', 'cloudtrail:LookupEvents ReadOnly=false'),
        ]),
        layer('L3', 'VPC 엔드포인트', [
            check('해당 없음', 'skip', 'VPC 엔드포인트 정책은 이 절차가 보지 않습니다 (VPC 안에서만 403이면 엔드포인트 정책을 확인)'),
        ]),
        layer('L4', '앞단 (CloudFront 등)', [check('해당 없음', 'skip', 'S3 앞단(CloudFront 등)은 이 절차가 보지 않습니다')]),
        layer('L5', '관리형 (인스턴스 없음)', [check('해당 없음', 'skip', 'S3는 관리형 저장소라 인스턴스·실행 환경 층이 없습니다')]),
        layer('L6', '퍼블릭 액세스 차단 · 정책 · ACL · 요청 한도', [
            ...(blocked
                ? []
                : [check('퍼블릭 액세스 차단', 'warn', '계정·버킷 어느 쪽에서도 켜지 않은 항목: BlockPublicAcls, IgnorePublicAcls, BlockPublicPolicy, RestrictPublicBuckets', 's3control:GetPublicAccessBlock · s3:GetPublicAccessBlock')]),
            check('요청 오류', 'unknown', '요청 지표가 없습니다 (버킷에 요청 지표를 켜야 403·503을 셀 수 있습니다)', 'AWS/S3 4xxErrors · 5xxErrors (FilterId EntireBucket)'),
            ...(blocked
                ? [check('공개·권한', 'ok', '퍼블릭 액세스 차단이 모두 켜져 있고 공개 정책·ACL이 없습니다', 's3:GetPublicAccessBlock · GetBucketPolicy · GetBucketAcl')]
                : []),
        ]),
        layer('L7', '버전 관리 (복구 가능성)', [
            check('버전 관리', 'warn', '버전 관리가 꺼져 있어 지우거나 덮어쓴 객체를 되돌릴 수 없습니다', 's3:GetBucketVersioning'),
        ]),
    ]);
};
