// 서비스 진단 층 그림의 모양과 설명 (AuditDiagnosis.tsx). 판정은 서버(mcp/lambda_mcp/diagnose.py)가 내고, 여기에는 그리는 법만 둔다.
//
// 서비스마다 요청이 지나가는 길이 다르다. 그래서 층 일곱 개를 한 줄로 늘어놓지 않고 그 서비스의 길 위에 놓는다
//   위 띠      L2 변경 (시간 축: 증상 직전에 무엇이 바뀌었나)
//   가운데 길  들어오는 쪽 → 그 서비스의 차례로 놓은 층들 → 나가는 쪽
//                ALB     사용자 → L3 입구 → L4 ALB → L5 대상 EC2 → L7 DB·외부 API
//                EC2     클라이언트 → L4 대상 그룹 → L3 보안 그룹·NACL → L5 인스턴스 → L7 의존성
//                Lambda  호출 → L4 API Gateway → L7 이벤트 소스 → L5 함수 실행 → L3 VPC 경로 → AWS API·DB
//                S3      요청자 → L4 앞단 → L3 VPC 엔드포인트 → L6 차단·정책·ACL → L5 (없음) → L7 버전 관리
//                RDS     애플리케이션 → L4 RDS Proxy → L3 보안 그룹·NACL → L5 DB 인스턴스 → L7 복제·디스크·백업
//                VPC     출발지 → L5 양쪽 인스턴스 → L3 보안 그룹·NACL·경로 → L6 NAT → 목적지
//                키 유출  유출된 키 → L3 쓴 곳 → L6 권한·지속성 → L5 컴퓨팅 남용 → L7 데이터 접근
//                비용     청구서 → L5 컴퓨팅 → L3 전송·NAT → L4 앞단 → L7 저장·로그
//   아래 띠    길에 없는 층 (보통 L6 권한·한도) · 맨 아래 L1 AWS 자체 (모든 것이 올라탄 바닥)
// 설명: 층마다 이 서비스에서 무엇을 묻고 무엇을 보는지 (런북의 진단 단계를 줄인 것)
import type { DiagnosisLayerId, DiagnosisStatus } from '@/types/audit';

export interface ServiceMap {
    entry: string; // 들어오는 쪽 (층이 아닌 글자)
    path: DiagnosisLayerId[]; // 길 위의 층 (그 서비스의 차례)
    exit?: string; // 나가는 쪽
    descriptions: Record<DiagnosisLayerId, string>;
}

// 판정의 이름과 배지 (공통 배지 components/badge.css). 원인 빨강 · 증상 노랑 · 정상 파랑 · 나머지 옅은 점
export const DIAGNOSIS_STATUS: Record<DiagnosisStatus, { label: string; badge: string; hint: string }> = {
    cause: { label: '원인', badge: 'is-fail', hint: '이 층에서 장애를 설명하는 것을 찾았습니다' },
    symptom: { label: '증상', badge: 'is-warn', hint: '이상이 보이지만 다른 층의 결과입니다' },
    warn: { label: '주의', badge: 'is-quiet', hint: '지금 장애의 원인은 아니지만 위험한 설정입니다' },
    ok: { label: '정상', badge: 'is-ok', hint: '확인한 항목이 모두 정상입니다' },
    unknown: { label: '확인 불가', badge: 'is-quiet is-empty', hint: '볼 수 없었습니다. 사람이 확인해야 합니다' },
    skip: { label: '해당 없음', badge: 'is-quiet is-empty', hint: '이 서비스에는 없는 층입니다' },
};

// 판정 개수의 차례 (무거운 것부터)
export const STATUS_ORDER: DiagnosisStatus[] = ['cause', 'symptom', 'warn', 'ok', 'unknown', 'skip'];

const ALB: ServiceMap = {
    entry: '사용자',
    path: ['L3', 'L4', 'L5', 'L7'],
    descriptions: {
        L1: 'AWS 쪽 장애인가. 대상 EC2의 시스템 상태 검사(호스트·네트워크)와 예정 이벤트를 봅니다. AWS Health는 지원 플랜에 묶여 있어 사람이 확인합니다.',
        L2: '증상 직전에 무엇이 바뀌었나. ALB·대상 그룹·보안 그룹·대상 인스턴스와 관련된 CloudTrail 쓰기 이벤트입니다. 다른 층에 이상이 있을 때만 계기로 봅니다.',
        L3: '요청이 ALB와 대상까지 가나. 인터넷용이면 서브넷의 인터넷 게이트웨이 경로, ALB 보안 그룹의 리스너 포트, 대상 보안 그룹이 ALB에서 오는 포트를 여는지, 양쪽 서브넷의 NACL을 봅니다.',
        L4: 'ALB가 뒤에 닿나. 리스너·대상 그룹·등록된 대상, 대상 헬스와 사유 코드(Elb.*는 ALB 쪽, Target.*는 대상 쪽), ALB가 만든 5xx를 봅니다. 대상이 모두 unhealthy면 ALB는 헬스와 무관하게 모든 대상으로 보냅니다(fail-open).',
        L5: '대상이 실행되나. 인스턴스 상태, OS 상태 검사, CPU와 CPU 크레딧, 헬스체크 사유(Target.Timeout 등), 대상이 만든 5xx를 봅니다.',
        L6: '막혔나. 연결 한도에 닿아 거부한 연결(RejectedConnectionCount)을 봅니다.',
        L7: '대상 뒤가 느린가. 대상 응답 시간이 유휴 타임아웃에 닿으면 DB·외부 API 지연을 의심합니다.',
    },
};

const EC2: ServiceMap = {
    entry: '클라이언트',
    path: ['L4', 'L3', 'L5', 'L7'],
    descriptions: {
        L1: 'AWS 쪽 장애인가. 시스템 상태 검사(호스트 하드웨어·네트워크)와 AWS가 예정한 이벤트(퇴역·재부팅)를 봅니다.',
        L2: '증상 직전에 무엇이 바뀌었나. 인스턴스·보안 그룹·Auto Scaling 그룹과 관련된 CloudTrail 쓰기 이벤트입니다.',
        L3: '요청이 인스턴스까지 가나. 보안 그룹이 연 포트를 서브넷 NACL이 막지 않는지, 공인 IP가 있으면 인터넷 게이트웨이 경로가 있는지 봅니다.',
        L4: '로드 밸런서 뒤에 있다면, 그 대상 그룹에서 이 인스턴스가 healthy인지와 사유 코드를 봅니다.',
        L5: '인스턴스가 실행되나. 상태와 멈춘 이유, OS 상태 검사, CPU, 버스트 인스턴스의 CPU 크레딧, Auto Scaling 헬스체크 유예 시간을 봅니다.',
        L6: 'Auto Scaling 그룹이 새 인스턴스를 띄우지 못하나. 실패한 활동(용량 부족·vCPU 한도·시작 템플릿 오류)을 봅니다.',
        L7: '인스턴스 안 애플리케이션의 의존성(DB·외부 API)은 애플리케이션 로그와 다른 서비스의 진단으로 봅니다.',
    },
};

const LAMBDA: ServiceMap = {
    entry: '호출',
    path: ['L4', 'L7', 'L5', 'L3'],
    exit: 'AWS API·DB',
    descriptions: {
        L1: 'AWS 쪽 장애인가. Lambda 서비스 장애는 AWS Health로 봐야 해서 사람이 확인합니다.',
        L2: '증상 직전에 무엇이 바뀌었나. 함수 코드·설정·동시성 변경과 실행 역할에 관련된 CloudTrail 쓰기 이벤트입니다.',
        L3: '함수가 밖으로 나가나. VPC에 붙은 함수는 NAT 경로가 없으면 인터넷과 엔드포인트 없는 AWS API를 부르지 못하고 시간 초과로 끝납니다.',
        L4: 'API Gateway·함수 URL 앞단입니다. 이 절차는 아직 보지 않습니다. API Gateway 뒤라면 함수의 스로틀이 클라이언트에 500으로 보입니다.',
        L5: '함수가 제대로 끝나나. 함수 상태와 마지막 배포, 오류 수, 실행 시간이 제한에 닿았는지, 로그의 시간 초과·메모리 부족 줄을 봅니다.',
        L6: '막혔나. 예약 동시성(0이면 모든 호출이 스로틀), 스로틀 수, 로그의 권한 거부(AccessDenied)를 봅니다.',
        L7: '이벤트가 들어오고 나가나. 이벤트 소스 매핑의 상태와 마지막 처리 결과, 스트림 처리 지연(IteratorAge), 버린 비동기 이벤트를 봅니다.',
    },
};

const S3: ServiceMap = {
    entry: '요청자',
    path: ['L4', 'L3', 'L6', 'L5', 'L7'],
    descriptions: {
        L1: 'AWS 쪽 장애인가. S3 서비스 장애는 AWS Health로 봐야 해서 사람이 확인합니다.',
        L2: '증상 직전에 무엇이 바뀌었나. 버킷 정책·ACL·퍼블릭 액세스 차단 설정과 관련된 CloudTrail 쓰기 이벤트입니다.',
        L3: 'VPC 엔드포인트 정책입니다. 이 절차는 보지 않습니다. VPC 안에서만 403이면 엔드포인트 정책을 확인합니다.',
        L4: 'CloudFront 같은 앞단입니다. 이 절차는 보지 않습니다.',
        L5: 'S3는 관리형 저장소라 컴퓨팅 층이 없습니다.',
        L6: '누가 읽을 수 있나. 계정·버킷의 퍼블릭 액세스 차단, 조건 없이 모두에게 허용하거나 거부하는 정책 문장, 모든 사람에게 준 ACL, 4xx·5xx 요청 오류를 봅니다.',
        L7: '잘못 지우거나 덮어써도 되돌릴 수 있나. 버전 관리를 봅니다.',
    },
};

const RDS: ServiceMap = {
    entry: '애플리케이션',
    path: ['L4', 'L3', 'L5', 'L7'],
    descriptions: {
        L1: 'AWS 쪽 장애인가. RDS가 알린 이벤트 중 장애(failure), 장애 조치(failover)·복구·유지 관리를 봅니다. 장애 조치 동안에는 연결이 끊기고 DNS가 새 인스턴스를 가리킵니다.',
        L2: '증상 직전에 무엇이 바뀌었나. DB 인스턴스·파라미터 그룹·보안 그룹과 관련된 CloudTrail 쓰기 이벤트입니다.',
        L3: '애플리케이션이 DB 포트까지 오나. DB 보안 그룹이 포트를 여는지, DB 서브넷의 NACL이 들어오는 포트와 돌아가는 임시 포트를 막지 않는지 봅니다. 퍼블릭 접근이 켜진 채 인터넷에 열려 있으면 주의입니다.',
        L4: 'RDS Proxy 같은 앞단입니다. 이 절차는 보지 않습니다.',
        L5: 'DB가 제대로 돌고 있나. 인스턴스 상태(멈춤·파라미터 불일치·KMS 키 문제), CPU, 여유 메모리, gp2 버스트 크레딧과 버스트 인스턴스의 CPU 크레딧을 봅니다.',
        L6: '한도에 닿았나. 여유 저장 공간이 할당의 10% 아래인지(가득 차면 쓰기가 멈춤)와 연결 수를 봅니다.',
        L7: '데이터가 늦거나 잃을 수 있나. 읽기 복제본의 복제 지연, 디스크 읽기·쓰기 지연, 자동 백업과 Multi-AZ를 봅니다.',
    },
};

const VPC: ServiceMap = {
    entry: '출발지',
    path: ['L5', 'L3', 'L6'],
    exit: '목적지',
    descriptions: {
        L1: 'AWS 쪽 장애인가. 양쪽 인스턴스의 시스템 상태 검사를 봅니다.',
        L2: '증상 직전에 무엇이 바뀌었나. 양쪽 인스턴스·보안 그룹·서브넷·NAT와 관련된 CloudTrail 쓰기 이벤트입니다.',
        L3: '패킷이 가고 돌아오나. 출발지 보안 그룹의 나가는 규칙과 목적지 보안 그룹의 들어오는 규칙, 서브넷 경계를 넘을 때의 NACL(상태를 기억하지 않아 응답 방향의 임시 포트도 열어야 함), 라우팅 테이블에서 접두사가 가장 긴 경로(없음·blackhole·공인 IP 없는 인터넷 게이트웨이)를 봅니다. 흐름 로그가 있는지도 봅니다.',
        L4: '로드 밸런서를 거치지 않는 인스턴스 사이의 직접 연결을 봅니다.',
        L5: '양쪽 인스턴스가 실행 중인가를 봅니다.',
        L6: 'NAT 게이트웨이를 거친다면 그 상태와, 같은 목적지로 동시 연결 55,000개 한도에 닿아 포트를 할당하지 못했는지(ErrorPortAllocation)를 봅니다.',
        L7: 'DNS 해석은 흐름 로그에 남지 않습니다. 이름으로 연결이 안 되면 Route 53 Resolver 쿼리 로그로 봅니다.',
    },
};

const CREDENTIAL: ServiceMap = {
    entry: '유출된 키',
    path: ['L3', 'L6', 'L5', 'L7'],
    descriptions: {
        L1: '자격 증명 사고는 AWS 쪽 장애가 아니라 해당 없음입니다.',
        L2: '이 키로 무엇을 바꿨나. 이 키가 부른 쓰기 API(CloudTrail)와 감사 기록·탐지를 끄려 한 흔적(StopLogging, DeleteDetector 등)을 봅니다.',
        L3: '어디서 썼나. 호출한 IP, 도구(userAgent), 배포 리전 밖의 리전을 봅니다. 채굴은 여러 리전에 퍼뜨리는 경우가 많습니다.',
        L4: '앞단이 없는 API 호출이라 해당 없음입니다.',
        L5: '컴퓨팅을 남용했나. 인스턴스·스팟·함수·컨테이너를 만든 흔적과 GuardDuty 탐지를 봅니다.',
        L6: '권한을 넓히거나 더듬었나. 새 사용자·키·정책을 만든 지속성 확보, 권한 거부가 잇따른 탐색, 키 상태와 권한 범위를 봅니다. 활성인 키는 지우지 말고 먼저 비활성화합니다.',
        L7: '데이터에 손댔나. 비밀 값 조회, 버킷 정책·ACL 공개, 스냅샷 공유를 봅니다. S3 객체 읽기는 데이터 이벤트라 관리 이벤트로는 보이지 않습니다.',
    },
};

const COST: ServiceMap = {
    entry: '청구서',
    path: ['L5', 'L3', 'L4', 'L7'],
    descriptions: {
        L1: 'AWS 쪽 가격 변경·청구 오류는 이 절차가 보지 않습니다.',
        L2: '비용을 늘리는 변경이 있었나. 인스턴스 시작, NAT·볼륨·엔드포인트 생성, 로그 구독 같은 계정 전체의 쓰기 이벤트를 봅니다. 급증이 있을 때만 계기로 봅니다.',
        L3: '데이터 전송·NAT 처리·VPC 엔드포인트·공인 IPv4 요금이 최근 3일 동안 그 전 14일 평균보다 크게 늘었나를 봅니다.',
        L4: '로드 밸런서(LCU)·API Gateway·CloudFront 요금이 크게 늘었나를 봅니다.',
        L5: 'EC2·Lambda·RDS·컨테이너 같은 컴퓨팅 요금이 크게 늘었나를 봅니다. 하루 평균이 1.5배 이상이면서 5달러 이상 늘면 급증입니다.',
        L6: '예산을 넘었거나 넘을 것으로 예측되는지, Cost Anomaly Detection 감시가 있는지, 계정 전체의 하루 평균이 어떻게 바뀌었는지를 봅니다.',
        L7: 'S3·EBS 저장, CloudWatch 로그 수집, DynamoDB·S3 요청 요금이 크게 늘었나를 봅니다.',
    },
};

export const SERVICE_MAPS: Record<string, ServiceMap> = {
    alb: ALB,
    ec2: EC2,
    lambda: LAMBDA,
    s3: S3,
    rds: RDS,
    vpc: VPC,
    credential: CREDENTIAL,
    cost: COST,
};

// 모르는 서비스(나중에 더한 서비스를 옛 화면이 받을 때): 층 차례대로 한 줄
export const FALLBACK_MAP: ServiceMap = {
    entry: '요청',
    path: ['L3', 'L4', 'L5', 'L7'],
    descriptions: { L1: '', L2: '', L3: '', L4: '', L5: '', L6: '', L7: '' },
};

// 띠: 위는 L2, 아래는 길에 없는 층(L2·L1 말고)과 맨 아래 L1
export const bandsOf = (map: ServiceMap) => {
    const below = (['L6', 'L3', 'L4', 'L5', 'L7'] as DiagnosisLayerId[]).filter((id) => !map.path.includes(id));
    return { top: 'L2' as DiagnosisLayerId, below: [...below, 'L1' as DiagnosisLayerId] };
};

// ---------------------------------------------------------------- 띠의 판정 한 줄
// 이상이 있는 띠(L2 계기 등)에는 판정 글을 한 줄로 줄여 적는다. 전체 글과 근거는 설명 칸에.
//   관련 변경 2건: 09-29 15:16 StopInstances (alice → i-0a1b…), 09-29 15:16 StopInstances (…)
//   → 관련 변경 2건 — 09-29 15:16 StopInstances (alice → i-0a1b…) 외 1건
const BRIEF_ITEMS_MAX = 60; // 콜론 뒤 목록에서 보일 글자 수 (넘으면 '외 N건')
const BRIEF_MAX = 90; // 한 줄 전체의 최대 글자 수 (목록이 없는 긴 판정 글은 말줄임표로)

// 띠에 판정 한 줄을 적는 판정 (확인 불가는 이상이 아니라 볼 수 없었다는 뜻이라 배지만으로 충분하다)
export const FINDING_STATUSES: DiagnosisStatus[] = ['cause', 'symptom', 'warn'];

const clip = (text: string) => (text.length > BRIEF_MAX ? `${text.slice(0, BRIEF_MAX - 1).trimEnd()}…` : text);

// 괄호 밖의 구분자로만 나눈다 ("(Client.UserInitiatedShutdown: …)"이나 "(처음 …, 마지막 …)" 안은 나누지 않는다)
function splitTop(text: string, separator: string): string[] {
    const parts: string[] = [];
    let depth = 0;
    let start = 0;
    for (let i = 0; i < text.length; i++) {
        const ch = text[i];
        if (ch === '(' || ch === '[') depth++;
        else if ((ch === ')' || ch === ']') && depth > 0) depth--;
        else if (depth === 0 && text.startsWith(separator, i)) {
            parts.push(text.slice(start, i));
            start = i + separator.length;
            i += separator.length - 1;
        }
    }
    parts.push(text.slice(start));
    return parts.map((part) => part.trim()).filter(Boolean);
}

// 판정 글 하나를 한 줄로: "앞말: 항목1, 항목2, 항목3 → 덧붙임" → "앞말 — 항목1, 항목2 외 1건"
export function briefOf(finding: string): string {
    const [head, ...rest] = splitTop(finding, ': ');
    if (!rest.length) return clip(head ?? '');
    const listed = splitTop(rest.join(': '), ' → ')[0] ?? ''; // 목록 뒤의 권고("→ …")는 설명 칸에만
    const items = splitTop(listed, ', ');
    const shown: string[] = [];
    for (const item of items) {
        if (shown.length && [...shown, item].join(', ').length > BRIEF_ITEMS_MAX) break;
        shown.push(item);
    }
    const more = items.length - shown.length;
    return clip(`${head} — ${shown.join(', ')}${more > 0 ? ` 외 ${more}건` : ''}`);
}

// 길에 없는 층 중 조용한 것(정상 · 해당 없음)은 띠 대신 '그 밖의 층' 한 줄의 작은 칩으로 모은다
export const QUIET_STATUSES: DiagnosisStatus[] = ['ok', 'skip'];
