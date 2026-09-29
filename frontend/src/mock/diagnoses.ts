// 목업 감사 로그의 진단 기록 (diagnoseService). 서버(mcp/lambda_mcp/diagnose.py)가 내는 것과 같은 모양이고,
// 데모 계정(Frothly, demo/frothly.ts)의 실제 CloudTrail 사건에 맞췄다:
//   - frothlywebcode: bstoll이 PutBucketAcl로 모든 사용자(AllUsers)에게 READ·WRITE를 준 뒤(-8014), 되돌리기 전(-4646)에 진단
//   - 웹 ALB: bstoll이 웹 서버 대상 세 대를 직접 종료한(-3332) 직후에 진단
//   - web_admin 키: 외부 IP 세 곳에서 권한을 더듬은 거부 4건(-21542~-20893) 뒤, bstoll이 키를 끄기(-20348) 전에 진단
// 만든 값: ALB 이름(frothly-web-alb)·대상 그룹 이름, 지표 값, 버전 관리 상태. 사건(누가·언제·무엇을)은 실제 기록이다
import type { Diagnosis, DiagnosisCheck, DiagnosisLayer, DiagnosisLayerId, DiagnosisStatus } from '@/types/audit';
import { DEMO_REGION, events } from './demo/frothly';

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
const reconRegion = () =>
    events(['ListBuckets', 'DescribeAccountAttributes']).find((e) => e.actor === 'web_admin')?.region ?? DEMO_REGION;

export const leakedKeyDiagnosis = (clock: (offset: number) => string) =>
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
                check('키 상태', 'ok', 'web_admin의 키 1개 활성', 'iam:ListAccessKeys'),
            ]),
            layer('L7', '데이터 접근 (비밀 값 · 버킷 공개 · 스냅샷 공유)', [
                check('데이터 접근', 'ok', '비밀 값 조회·버킷 공개·스냅샷 공유 흔적이 없습니다', 'cloudtrail:LookupEvents'),
                check('S3 객체 읽기', 'unknown', 'S3 GetObject는 데이터 이벤트라 관리 이벤트 조회로 보이지 않습니다 (데이터 이벤트 추적·S3 서버 액세스 로그로 확인)', 'cloudtrail 데이터 이벤트'),
            ]),
        ],
        24,
    );
