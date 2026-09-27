// 데모 답변: 질문에 맞춰, Frothly 계정의 실제 기록(frothly.ts)으로 Vigie가 답했을 모습을 만든다.
// - 질문의 낱말로 고른다 (DEMO_ANSWERS를 위에서부터 보고 처음 맞는 것). 맞는 것이 없으면 할 수 있는 질문을 안내한다
// - 도구 이름·입력은 실제 Vigie가 부르는 것과 같다 (mcp/lambda_mcp/risk.py). 처음부터 싣지 않는 도구는
//   도구 검색으로 찾은 뒤 부른다 (services/llm/tool_search.py의 ALWAYS_LOADED 밖)
// - 관리자 전용 도구(CloudTrail·IAM·네트워크·S3 보안 점검)가 있어야 하는 답은 adminOnly. 일반 사용자(?mock-role=member)는
//   실제 서버처럼 '관리자만 볼 수 있다'는 답을 받는다 (services/llm/tool_access.py)
// - 답변의 숫자·시각은 모두 frothly.json에서 계산한다. 시각은 지금에 맞춰 옮긴 한국 시각이다
// - 계정 ID는 Vigie처럼 끝 네 자리만 보인다 (MASKED_ACCOUNT)
import {
    FROTHLY,
    MASKED_ACCOUNT,
    agoText,
    asgLaunches,
    demoCost,
    demoAlarms,
    epochOf,
    events,
    idleInstance,
    kstClock,
    manualTerminations,
    metricAverage,
    metricMax,
    metricSum,
    peakOffset,
    round,
    when,
    LAMBDA_ERROR_KEYS,
} from './frothly';

export interface DemoTool {
    tool_name: string;
    input: Record<string, unknown>;
    status: 'ok' | 'error';
    error?: string;
}

export interface DemoEntry {
    answer: string;
    tools: DemoTool[];
    thinking: string[];
    search?: { query: string; found: string[] };
}

interface DemoAnswer {
    match: RegExp;
    adminOnly?: boolean;
    build: () => DemoEntry;
}

const ok = (tool_name: string, input: Record<string, unknown> = {}): DemoTool => ({ tool_name, input, status: 'ok' });
const lines = (...parts: (string | false | undefined)[]) => parts.filter((p) => p !== false && p !== undefined).join('\n');
const usd = (n: number) => `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

// ---------------------------------------------------------------- 기록에서 뽑은 사실
const publicAcl = () => events('PutBucketAcl').find((e) => e.publicPermissions?.length);
const aclRestored = () => events('PutBucketAcl').find((e) => !e.publicPermissions?.length && e.at > (publicAcl()?.at ?? 0));
const memcachedOpen = () => events('AuthorizeSecurityGroupIngress')[0];
const memcachedClosed = () => events('RevokeSecurityGroupIngress')[0];
const recon = () => FROTHLY.trail.filter((e) => e.actor === 'web_admin' && e.error);
const keyDisabled = () => events('UpdateAccessKey')[0];
const logins = () => events('ConsoleLogin');
const consoleOutputs = () => events('GetConsoleOutput');
const sshOpenGroups = () =>
    FROTHLY.securityGroups.filter((g) => g.openToWorld.some((r) => r.port === '22' || r.protocol === '-1'));
const minutesBetween = (from: number, to: number) => Math.round((to - from) / 60);
const unique = <T,>(values: T[]) => [...new Set(values)];

// ---------------------------------------------------------------- 답변
const DEMO_ANSWERS: DemoAnswer[] = [
    {
        // 보안 감사: 루트 로그인
        match: /루트|root/i,
        adminOnly: true,
        build: () => {
            const all = logins();
            const ips = unique(all.map((e) => e.ip));
            return {
                search: { query: 'lookup_events', found: ['lookup_events'] },
                tools: [ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'EventName', AttributeValue: 'ConsoleLogin' }] })],
                thinking: [
                    '루트 계정 로그인은 CloudTrail의 ConsoleLogin 이벤트에서 userIdentity.type이 Root인 것을 찾으면 된다. 로그인 이벤트를 모두 가져와 주체별로 나눈다.',
                    `루트 로그인은 없다. 대신 IAM 사용자 bstoll이 MFA 없이 ${all.length}번 로그인했다. 이것도 함께 알린다.`,
                ],
                answer: lines(
                    '**루트 계정으로 로그인한 기록은 없습니다.**',
                    '',
                    `같은 기간의 콘솔 로그인은 모두 IAM 사용자 \`bstoll\`의 것이고, **${all.length}번 모두 MFA 없이** 로그인했습니다.`,
                    '',
                    '| 시각 (KST) | 사용자 | 출발 IP | MFA | 결과 |',
                    '|:--|:--|:--|:--|:--|',
                    ...all.map((e) => `| ${when(e.at)} | ${e.actor} | ${e.ip} | 사용 안 함 | 성공 |`),
                    '',
                    `- 출발 IP가 ${ips.length}곳(${ips.join(', ')})입니다. 평소 쓰는 곳인지 확인해 보세요.`,
                    '- 콘솔 사용자에게 MFA를 강제하려면 IAM 정책에 `aws:MultiFactorAuthPresent` 조건을 거는 방법이 있습니다.',
                    `- 계정: ${MASKED_ACCOUNT} · 출처: CloudTrail 관리 이벤트`,
                ),
            };
        },
    },
    {
        // 보안 감사: 최근 보안 이벤트 (심각도 순)
        match: /보안\s*이벤트|보안\s*사고|심각도|위협|침해|이상\s*징후/,
        adminOnly: true,
        build: () => {
            const acl = publicAcl()!;
            const restored = aclRestored();
            const open = memcachedOpen();
            const closed = memcachedClosed();
            const probes = recon();
            const disabled = keyDisabled();
            const noMfa = logins().filter((e) => e.mfa === false);
            const outputs = consoleOutputs();
            const ssh = sshOpenGroups();
            return {
                search: {
                    query: 'lookup_events|checkS3BucketSecurity|get_vpc_network',
                    found: ['lookup_events', 'checkS3BucketSecurity', 'get_vpc_network'],
                },
                tools: [
                    ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'ReadOnly', AttributeValue: 'false' }], max_results: 200 }),
                    ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'EventName', AttributeValue: 'ConsoleLogin' }] }),
                    ok('checkS3BucketSecurity', {}),
                    ok('get_vpc_network', { vpc_id: 'vpc-05cb3dfad78e47394', region: FROTHLY.region }),
                ],
                thinking: [
                    '보안 이벤트를 모아 심각도로 나눠야 한다. CloudTrail에서 변경 호출과 로그인·거부된 호출을 가져오고, S3 버킷 공개 여부와 보안 그룹 개방 상태를 함께 본다.',
                    '공개 쓰기가 열린 버킷과 전 세계에 열린 memcached 포트가 가장 위험하다. 둘 다 지금은 닫혔지만 열려 있던 시간을 적는다. 그다음 탈취가 의심되는 키의 권한 탐색, MFA 없는 로그인 순으로 정리한다.',
                ],
                answer: lines(
                    `최근 기록에서 보안 이벤트 **6건**을 찾았습니다. 심각도 순입니다 (계정 ${MASKED_ACCOUNT}, 리전 ${FROTHLY.region}).`,
                    '',
                    '| 심각도 | 무엇 | 대상 | 언제 · 누가 | 지금 |',
                    '|:--|:--|:--|:--|:--|',
                    `| 🔴 심각 | 버킷에 **모든 사용자 읽기·쓰기** ACL (\`PutBucketAcl\`) | \`frothlywebcode\` | ${when(acl.at)} · ${acl.actor} | ${restored ? `${minutesBetween(acl.at, restored.at)}분 뒤 원복` : '공개 중'} |`,
                    `| 🔴 심각 | UDP 11211(memcached)을 0.0.0.0/0 · ::/0에 개방 | \`${open.target}\` (웹 서버용) | ${when(open.at)} · ${open.actor} | ${closed ? `${minutesBetween(open.at, closed.at)}분 뒤 닫힘` : '열림'} |`,
                    `| 🟠 높음 | \`web_admin\` 액세스 키로 권한 탐색 (거부 ${probes.length}건, IP ${unique(probes.map((e) => e.ip)).length}곳) | IAM·S3·EC2 | ${kstClock(epochOf(probes[0].at))}~${kstClock(epochOf(probes[probes.length - 1].at))} | ${disabled ? `${kstClock(epochOf(disabled.at))}에 ${disabled.actor}가 키 비활성화` : '키 활성'} |`,
                    `| 🟡 중간 | MFA 없는 콘솔 로그인 ${noMfa.length}회 | \`bstoll\` | 마지막 ${when(noMfa[noMfa.length - 1].at)} | MFA 미설정 |`,
                    `| 🟡 중간 | SSH(22)나 모든 포트를 전 세계에 연 보안 그룹 ${ssh.length}개 | \`production-FrothlyWebPubSecGroup\` 외 | 설정 상태 | 열림 |`,
                    `| 🔵 낮음 | 인스턴스 콘솔 출력 조회 ${outputs.length}회 (\`GetConsoleOutput\`) | 웹 서버 인스턴스 ${unique(outputs.map((e) => e.target)).length}대 | ${kstClock(epochOf(outputs[0].at))}~ · bstoll | 조사 활동으로 보임 |`,
                    '',
                    '**먼저 할 일**',
                    '1. `frothlywebcode`가 공개로 열려 있던 동안 누가 파일을 올리거나 바꿨는지 S3 서버 액세스 로그로 확인하세요. 공개 **쓰기**였으므로 웹 코드가 바뀌었을 수 있습니다.',
                    '2. `web_admin` 키는 비활성화만 되어 있습니다. 쓰지 않는다면 삭제하고, 키가 어디서 새었는지 확인하세요.',
                    '3. 보안 그룹의 22번 포트를 사무실 IP나 Session Manager로 좁히세요.',
                    '',
                    '버킷 퍼블릭 액세스 차단은 대화에서 요청하면 승인 뒤 켤 수 있습니다 (`enableS3PublicAccessBlock`).',
                ),
            };
        },
    },
    {
        // 권한 관리: IAM 권한이 바뀐 사용자
        match: /IAM.*(변경|바뀐|바꾼)|권한.*(변경|바뀐|바꾼)/,
        adminOnly: true,
        build: () => {
            const disabled = keyDisabled();
            return {
                search: { query: 'lookup_events|list_users', found: ['lookup_events', 'list_users'] },
                tools: [
                    ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'EventSource', AttributeValue: 'iam.amazonaws.com' }] }),
                    ok('list_users', {}),
                ],
                thinking: [
                    'IAM 변경은 CloudTrail에서 이벤트 출처가 iam.amazonaws.com인 변경 호출을 보면 된다. 사용자 목록도 함께 가져와 누가 있는지 본다.',
                    '정책·그룹을 바꾼 기록은 없고, 액세스 키를 비활성화한 기록 하나가 있다.',
                ],
                answer: lines(
                    '**정책을 붙이거나 떼고, 그룹을 바꾼 기록은 없습니다.** IAM에서 바뀐 것은 액세스 키 하나입니다.',
                    '',
                    '| 시각 (KST) | 누가 | 무엇 | 대상 |',
                    '|:--|:--|:--|:--|',
                    `| ${when(disabled.at)} | ${disabled.actor} | 액세스 키 **비활성화** (\`UpdateAccessKey\`) | \`${disabled.keyUser}\`의 키 |`,
                    '',
                    `이 키는 비활성화되기 직전에 외부 IP ${unique(recon().map((e) => e.ip)).length}곳에서 권한을 떠보다 거부된 기록(${recon().length}건)이 있습니다. 탈취를 의심해 막은 것으로 보입니다.`,
                    '',
                    `IAM 사용자는 ${FROTHLY.users.length}명입니다: ${FROTHLY.users.map((u) => `\`${u}\``).join(', ')}.`,
                ),
            };
        },
    },
    {
        // 권한 관리: 최소 권한 원칙
        match: /최소\s*권한|과도한\s*권한|권한이\s*너무|위배/,
        adminOnly: true,
        build: () => {
            const actor = (name: string) => FROTHLY.actors.find((a) => a.actor === name);
            const splunk = actor('splunk_access')!;
            const bud = actor('bstoll')!;
            const web = actor('web_admin')!;
            const idle = FROTHLY.users.filter((u) => !actor(u));
            return {
                search: {
                    query: 'list_users|list_user_policies|lookup_events',
                    found: ['list_users', 'list_user_policies', 'lookup_events'],
                },
                tools: [
                    ok('list_users', {}),
                    ok('list_user_policies', { user_name: 'splunk_access' }),
                    ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'Username', AttributeValue: 'splunk_access' }] }),
                ],
                thinking: [
                    '최소 권한을 보려면 사용자마다 실제로 쓰는 호출과 가진 권한을 견줘야 한다. 사용자 목록과 인라인 정책을 보고, CloudTrail에서 사용자별 호출을 센다.',
                    '읽기만 하는 연동 계정, MFA 없이 변경을 하는 콘솔 사용자, 거부만 당한 키, 호출이 없는 사용자로 나뉜다.',
                ],
                answer: lines(
                    'CloudTrail에 남은 **실제 사용**과 견줘 보면 손볼 곳이 네 가지입니다.',
                    '',
                    '| 사용자 | 실제 사용 | 문제 | 권장 |',
                    '|:--|:--|:--|:--|',
                    `| \`splunk_access\` | 호출 ${splunk.total.toLocaleString()}건, **모두 조회** | 조회만 하는 로그 연동 계정 | \`ReadOnlyAccess\`나 \`SecurityAudit\` 범위로 좁히기 |`,
                    `| \`bstoll\` | 호출 ${bud.total}건, 변경 ${bud.writes}건 (버킷 ACL·보안 그룹·인스턴스 종료) | MFA 없이 콘솔에서 변경 | 변경 권한에 \`aws:MultiFactorAuthPresent\` 조건 |`,
                    `| \`web_admin\` | 호출 ${web.total}건, **모두 거부** | 쓰지 않는 키가 외부에서 쓰임 | 사용자·키 삭제 |`,
                    `| ${idle.map((u) => `\`${u}\``).join(', ')} | 이 기간 호출 없음 | 쓰지 않는 권한일 수 있음 | IAM Access Analyzer의 미사용 권한으로 확인 |`,
                    '',
                    '정책 문서 자체에서 `"Action": "*"` 같은 넓은 권한을 찾으려면 사용자별로 `get_user_policy`로 이어서 볼 수 있습니다.',
                ),
            };
        },
    },
    {
        // 리소스 모니터링: EC2 CPU
        match: /CPU|cpu|사용률/,
        build: () => {
            const idle = idleInstance();
            return {
                search: { query: 'getEc2CpuRanking', found: ['getEc2CpuRanking'] },
                tools: [ok('getEc2CpuRanking', { hours: 24 })],
                thinking: [
                    'EC2 인스턴스의 CPU 순위는 getEc2CpuRanking 한 번으로 가져온다 (지표 조회 한 번).',
                    '둘 다 한 자릿수로 낮다. 웹 서버 그룹은 인스턴스가 여러 번 바뀌어 그룹 단위로 본다.',
                ],
                answer: lines(
                    `CPU 사용률이 가장 높았던 인스턴스는 **${idle.name}** (\`${idle.id}\`)입니다. 다만 둘 다 한 자릿수로 한가합니다.`,
                    '',
                    '| 순위 | 인스턴스 | 유형 | 평균 | 최대 |',
                    '|--:|:--|:--|--:|--:|',
                    `| 1 | ${idle.name} (\`${idle.id}\`) | ${idle.type} | ${metricAverage('forensicCpu')}% | ${metricMax('forensicCpu')}% |`,
                    `| 2 | WebServers (Auto Scaling 그룹) | t2.medium | ${metricAverage('asgCpu')}% | ${metricMax('asgCpu')}% |`,
                    '',
                    `- \`${idle.name}\`는 평균 ${metricAverage('forensicCpu')}%로 거의 쓰지 않습니다. 조사용으로 띄운 인스턴스라면 끝난 뒤 멈춰 비용을 줄일 수 있습니다.`,
                    `- WebServers 그룹은 오늘 인스턴스가 ${asgLaunches()}번 새로 떴습니다. 사람이 직접 종료한 것이 ${manualTerminations().length}번이고, 나머지는 Auto Scaling이 교체한 것입니다.`,
                ),
            };
        },
    },
    {
        // 리소스 모니터링: Lambda 메모리
        match: /메모리/,
        build: () => ({
            search: { query: 'get_metric_data', found: ['get_metric_data'] },
            tools: [
                ok('get_metric_metadata', { namespace: 'AWS/Lambda' }),
                ok('get_metric_data', { namespace: 'AWS/Lambda', metric_name: 'Duration', statistic: 'Average', group_by: 'FunctionName' }),
            ],
            thinking: [
                'Lambda 메모리 사용량은 기본 지표에 없다. Lambda Insights가 켜져 있는지 지표 목록부터 본다.',
                'Lambda Insights 지표가 없다. 메모리 대신 실행 시간과 호출 수를 보여 주고, 메모리를 보는 방법을 안내한다.',
            ],
            answer: lines(
                '**메모리 사용량은 이 계정에서 볼 수 없습니다.** Lambda의 기본 지표에는 메모리가 없고, 메모리 지표를 주는 Lambda Insights가 꺼져 있습니다.',
                '',
                '대신 실행 시간 순으로 보면 다음과 같습니다 (최근 3시간).',
                '',
                '| 함수 | 평균 실행 시간 | 최대 | 호출 | 오류 |',
                '|:--|--:|--:|--:|--:|',
                `| RDSAuditLogs | ${Math.round(metricAverage('rdsAuditLogsDuration'))}ms | ${Math.round(metricMax('rdsAuditLogsDuration'))}ms | ${metricSum('rdsAuditLogsInvocations')} | ${metricSum(LAMBDA_ERROR_KEYS.RDSAuditLogs)} |`,
                `| VPCFlowLogs | ${Math.round(metricAverage('vpcFlowLogsDuration'))}ms | ${Math.round(metricMax('vpcFlowLogsDuration'))}ms | ${metricSum('vpcFlowLogsInvocations')} | ${metricSum(LAMBDA_ERROR_KEYS.VPCFlowLogs)} |`,
                '',
                '메모리는 함수 로그의 `REPORT` 줄에 남습니다. 다음 Logs Insights 쿼리로 함수별 최대 메모리를 볼 수 있습니다.',
                '',
                '```',
                'filter @type = "REPORT"',
                '| stats max(@maxMemoryUsed / 1000 / 1000) as maxMemoryMB, max(@memorySize / 1000 / 1000) as configuredMB by @log',
                '| sort maxMemoryMB desc',
                '```',
            ),
        }),
    },
    {
        // 비용 관리
        match: /비용|요금|청구|얼마/,
        build: () => {
            const cost = demoCost();
            // 서비스별 전월 같은 기간 대비 (만든 값: 전체 증가율을 서비스마다 나눠 둔 것)
            const growth: Record<string, number> = { EC2: 1.083, CloudWatch: 1.152, Config: 1.224, S3: 1.041, ELB: 1.02 };
            const rows = cost.byService
                .map((s) => {
                    const last = s.amount / (growth[s.service] ?? 1);
                    return { ...s, last, delta: s.amount - last };
                })
                .sort((a, b) => b.delta - a.delta)
                .slice(0, 3);
            return {
                tools: [ok('cost-explorer', { operation: 'getCostAndUsage', granularity: 'MONTHLY', group_by: 'SERVICE' })],
                thinking: [
                    '이번 달과 지난달 같은 기간의 비용을 서비스별로 가져와 차이를 본다.',
                    'EC2·CloudWatch·Config가 늘었다. EC2는 오늘 Auto Scaling 교체와 조사용 인스턴스가 있었고, Config는 규칙 평가가 많다.',
                ],
                answer: lines(
                    `이번 달 비용은 지금까지 **${usd(cost.monthToDate)}**이고, 지난달 같은 기간보다 ${round((cost.monthToDate / cost.lastMonthSamePeriod - 1) * 100)}% 많습니다. 가장 많이 늘어난 서비스는 다음 세 가지입니다.`,
                    '',
                    '| 서비스 | 지난달 같은 기간 | 이번 달 | 증가 |',
                    '|:--|--:|--:|--:|',
                    ...rows.map((r) => `| ${r.service} | ${usd(r.last)} | ${usd(r.amount)} | +${usd(r.delta)} (${round((r.amount / r.last - 1) * 100)}%) |`),
                    '',
                    `- **EC2**: 웹 서버 그룹이 오늘만 ${asgLaunches()}번 인스턴스를 새로 띄웠고, 조사용 \`${idleInstance().name}\`이 계속 켜져 있습니다.`,
                    '- **CloudWatch**: 로그 수집 Lambda가 늘어난 로그를 올리고 있습니다. 보존 기간이 없는 로그 그룹이 있는지 확인하세요.',
                    '- **Config**: 규칙 평가 호출이 많습니다(오늘 CloudTrail 조회 호출 중 가장 많음). 평가 주기를 늘리면 줄어듭니다.',
                ),
            };
        },
    },
    {
        // 비용 관리: 쓰지 않는 리소스
        match: /미사용|안\s*쓰는|쓰지\s*않는|삭제\s*가능|낭비|유휴/,
        build: () => {
            const idle = idleInstance();
            const eip = FROTHLY.eips.find((e) => !e.attached);
            const eipMonthly = 0.005 * 730; // 연결 안 된 탄력적 IP: 시간당 $0.005
            const idleMonthly = 0.0138 * 730; // t2.micro 온디맨드 (us-west-1)
            return {
                search: { query: 'findEc2Waste', found: ['findEc2Waste'] },
                tools: [ok('findEc2Waste', {})],
                thinking: [
                    '연결되지 않은 EBS 볼륨, 오래 쓰지 않은 인스턴스, 연결되지 않은 탄력적 IP를 한 번에 찾는다.',
                    '연결 안 된 볼륨은 없다. 탄력적 IP 하나와 거의 쓰지 않는 인스턴스 하나가 있다.',
                ],
                answer: lines(
                    `정리할 수 있는 리소스가 **2개** 있습니다. 모두 치우면 한 달에 약 ${usd(eipMonthly + idleMonthly)}를 아낍니다.`,
                    '',
                    '| 리소스 | 상태 | 월 비용 (예상) |',
                    '|:--|:--|--:|',
                    eip ? `| 탄력적 IP \`${eip.ip}\` | 어느 인스턴스에도 연결되지 않음 | ${usd(eipMonthly)} |` : undefined,
                    `| ${idle.name} (\`${idle.id}\`, ${idle.type}) | CPU 평균 ${metricAverage('forensicCpu')}% · 조사용으로 띄운 뒤 켜져 있음 | ${usd(idleMonthly)} |`,
                    '',
                    '- 연결되지 않은 EBS 볼륨은 없습니다 (종료된 웹 서버의 볼륨은 함께 지워지게 설정되어 있습니다).',
                    '- 인스턴스는 조사가 끝났는지 확인한 뒤 멈추세요. 대화에서 "멈춰 줘"라고 하면 승인 요청을 만듭니다.',
                ),
            };
        },
    },
    {
        // 알람
        match: /알람|경보/,
        build: () => {
            const alarms = demoAlarms();
            const sg = memcachedOpen();
            return {
                tools: [ok('get_active_alarms', { max_items: 50 })],
                thinking: [
                    '지금 울리는 알람을 가져온다.',
                    '둘 다 CloudTrail 지표 필터로 만든 보안 알람이다. 무엇 때문에 울렸는지 기록과 이어서 설명한다.',
                ],
                answer: lines(
                    `알람 ${alarms.total}개 중 **${alarms.firing.length}개가 울리고 있습니다.** 둘 다 CloudTrail 기록으로 울리는 보안 알람입니다.`,
                    '',
                    '| 알람 | 조건 | 울린 때 | 까닭 |',
                    '|:--|:--|:--|:--|',
                    `| \`${alarms.firing[0].name}\` | ${alarms.firing[0].metric} | ${kstClock(alarms.firing[0].since)} (${agoText(alarms.firing[0].since)}) | bstoll이 \`${sg.target}\`에 UDP 11211을 전 세계에 열었다가 닫음 |`,
                    `| \`${alarms.firing[1].name}\` | ${alarms.firing[1].metric} | ${kstClock(alarms.firing[1].since)} (${agoText(alarms.firing[1].since)}) | bstoll이 MFA 없이 콘솔 로그인 |`,
                    '',
                    '포트는 이미 닫혔지만 알람은 새 데이터가 들어올 때까지 ALARM에 머뭅니다. 확인이 끝났다면 콘솔에서 상태를 되돌리거나 다음 평가를 기다리면 됩니다.',
                ),
            };
        },
    },
    {
        // 오류
        match: /오류|에러|실패|장애|error/i,
        build: () => {
            const rds = metricSum(LAMBDA_ERROR_KEYS.RDSAuditLogs);
            const vpc = metricSum(LAMBDA_ERROR_KEYS.VPCFlowLogs);
            const peak = epochOf(peakOffset(LAMBDA_ERROR_KEYS.RDSAuditLogs));
            return {
                tools: [
                    ok('describe_log_groups', { log_group_name_prefix: '/aws/lambda/' }),
                    ok('get_metric_data', { namespace: 'AWS/Lambda', metric_name: 'Errors', statistic: 'Sum', group_by: 'FunctionName' }),
                ],
                thinking: [
                    'Lambda 오류는 함수별 Errors 지표의 합으로 본다. 로그 그룹 이름도 함께 확인한다.',
                    '두 로그 수집 함수가 같은 시각에 함께 실패했다. 공통 원인을 먼저 보라고 안내한다.',
                ],
                answer: lines(
                    `최근 3시간 동안 Lambda 오류는 **${rds + vpc}건**이고, 모두 로그를 옮기는 함수 두 개에서 났습니다.`,
                    '',
                    '| 함수 | 오류 | 몰린 때 |',
                    '|:--|--:|:--|',
                    `| RDSAuditLogs | ${rds} | ${kstClock(peak)} 전후 15분 |`,
                    `| VPCFlowLogs | ${vpc} | 같은 시각 |`,
                    '',
                    '두 함수가 **같은 시각에 함께** 실패했으므로 함수 코드보다 공통으로 쓰는 것(로그를 보내는 대상, 실행 역할의 권한, 네트워크)을 먼저 보세요. 그 뒤로는 오류가 없습니다.',
                    '',
                    '```',
                    'filter @message like /ERROR|Task timed out/',
                    '| stats count() by bin(5m), @log',
                    '```',
                ),
            };
        },
    },
    {
        // S3 공개 버킷
        match: /버킷|S3|s3|공개/,
        adminOnly: true,
        build: () => {
            const acl = publicAcl()!;
            const restored = aclRestored();
            return {
                search: { query: 'listS3Buckets|checkS3BucketSecurity', found: ['listS3Buckets', 'checkS3BucketSecurity'] },
                tools: [ok('listS3Buckets', {}), ok('checkS3BucketSecurity', {})],
                thinking: [
                    '버킷 목록을 보고, 버킷마다 퍼블릭 액세스 차단·정책·ACL을 점검한다.',
                    'frothlywebcode가 오늘 공개 ACL이 걸렸다 풀렸고, 퍼블릭 액세스 차단은 여전히 꺼져 있다.',
                ],
                answer: lines(
                    `지금 공개된 버킷은 없습니다. 하지만 **\`frothlywebcode\`는 오늘 ${restored ? minutesBetween(acl.at, restored.at) : '?'}분 동안 누구나 읽고 쓸 수 있었고**, 퍼블릭 액세스 차단이 아직 꺼져 있습니다.`,
                    '',
                    `- ${when(acl.at)}: ${acl.actor}가 ACL로 모든 사용자에게 ${acl.publicPermissions?.join('·')} 권한을 줌`,
                    restored ? `- ${when(restored.at)}: ${restored.actor}가 공개 권한을 뺌` : undefined,
                    '- 암호화·버전 관리·SSL 강제도 꺼져 있습니다 (AWS Config 규칙 위반).',
                    '',
                    '다시 공개되지 않게 퍼블릭 액세스 차단을 켜 두는 것을 권합니다. "frothlywebcode 퍼블릭 액세스 차단 켜 줘"라고 하면 승인 요청을 만듭니다.',
                ),
            };
        },
    },
    {
        // 인사·소개
        match: /^\s*(안녕|하이|hi|hello)|누구|소개|뭘\s*할\s*수|무엇을\s*할\s*수/i,
        build: () => ({
            tools: [],
            thinking: ['인사다. 도구 없이 무엇을 할 수 있는지 소개한다.'],
            answer: lines(
                '안녕하세요, AWS 클라우드 운영을 돕는 **Vigie(비지)**입니다.',
                '',
                '지금은 **데모**입니다. 가상 회사 Frothly의 실제 AWS 운영 기록(공개 데이터셋)으로 답합니다. 이런 것을 물어보세요.',
                '',
                '- 최근 보안 이벤트를 심각도 순으로 정리해 줘',
                '- CPU 사용률이 가장 높은 EC2 인스턴스는?',
                '- 이번 달 비용이 가장 많이 늘어난 서비스는?',
                '- 지금 울리는 알람 알려줘',
            ),
        }),
    },
];

// 맞는 답이 없을 때: 데모에서 할 수 있는 질문을 안내한다
const fallback = (): DemoEntry => ({
    tools: [],
    thinking: ['데모 데이터로 답할 수 있는 질문이 아니다. 답할 수 있는 질문을 안내한다.'],
    answer: lines(
        '이 데모는 가상 회사 Frothly의 실제 AWS 운영 기록 약 6시간(공개 데이터셋)으로 답합니다. 그 기록으로는 이 질문에 답하기 어렵습니다.',
        '',
        '이런 질문은 답할 수 있습니다.',
        '',
        '- 보안: "최근 보안 이벤트를 심각도 순으로", "루트 계정 로그인 기록 있어?", "최소 권한에 어긋나는 사용자"',
        '- 운영: "CPU가 가장 높은 EC2", "Lambda 오류", "지금 울리는 알람"',
        '- 비용: "비용이 가장 많이 늘어난 서비스", "쓰지 않는 리소스"',
    ),
});

// 일반 사용자가 관리자 전용 도구가 필요한 것을 물었을 때 (services/llm/tool_access.py의 안내와 같은 답)
const adminOnlyRefusal = (): DemoEntry => ({
    tools: [],
    thinking: ['CloudTrail·IAM·S3 보안 점검이 필요한 질문이다. 이 사용자는 관리자가 아니라 그 도구를 쓸 수 없다.'],
    answer:
        '이 내용은 **관리자(admins 그룹)만 볼 수 있습니다.** CloudTrail·IAM·네트워크·S3 보안 점검 조회는 관리자 전용이라, 필요하면 관리자에게 요청해 주세요.\n\n로그·지표·비용·EC2 상태는 지금 권한으로도 물어볼 수 있습니다.',
});

// 질문에 맞는 데모 답 (admin: 요청자가 관리자인가)
export const demoEntryFor = (text: string, admin: boolean): DemoEntry => {
    const found = DEMO_ANSWERS.find((entry) => entry.match.test(text));
    if (!found) return fallback();
    if (found.adminOnly && !admin) return adminOnlyRefusal();
    return found.build();
};

// 처음 열었을 때 보이는 예시 대화의 질문. 일반 사용자에게는 관리자 전용 도구가 필요 없는 질문
export const demoFirstQuestion = (admin: boolean) =>
    admin ? '최근 보안 이벤트를 심각도 순으로 정리해줘' : 'CPU 사용률이 가장 높은 EC2 인스턴스는 무엇인가요?';
