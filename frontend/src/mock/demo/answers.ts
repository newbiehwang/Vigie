// 데모 답변: 질문에 맞춰, Frothly 계정의 실제 기록(frothly.ts)으로 Vigie가 답했을 모습을 만든다.
//
//   질문 ─▶ ① 가드 (GUARDS): 승인 건너뛰기·비밀 값·삭제·권한 넓히기·범위 밖 질문처럼 실제 Vigie가 거절하거나
//          │                   할 수 없다고 답하는 요청. 주제보다 먼저 본다 ("web_admin 키 삭제해줘"는 키 설명이 아니라 거절)
//          ├▶ ② 이어 묻기 (FOLLOW_UPS): 앞 질문의 주제에 딸린 물음 ("두 번째 거 자세히", "누가 했어?", "어떻게 막아?")
//          ├▶ ③ 주제 (DEMO_ANSWERS): 질문의 낱말로 고른다 (위에서부터 보고 처음 맞는 것)
//          ├▶ ④ 앞 주제 더 보기 (MORE): "자세히", "왜?"처럼 주제 낱말 없이 더 묻는 말
//          ├▶ ⑤ 되묻기: "고쳐 줘"처럼 무엇을 말하는지 모를 때 지금 손볼 것을 보이고 고르게 한다
//          └▶ ⑥ 안내 (fallback): 데모 기록으로 답할 수 없는 질문
//   앞 질문의 주제는 이 대화의 이전 질문들을 처음부터 같은 규칙으로 다시 따라가서 정한다 (topicAfter).
//   실제 Vigie는 이전 대화를 모델에 함께 보내므로(isCached) "그거"가 무엇인지 안다. 목업은 그 모습을 흉내 낸다
//
// - 도구 이름·입력은 실제 Vigie가 부르는 것과 같다 (mcp/lambda_mcp/risk.py). 처음부터 싣지 않는 도구는
//   도구 검색으로 찾은 뒤 부른다 (services/llm/tool_search.py의 ALWAYS_LOADED 밖)
// - 관리자 전용 도구(CloudTrail·IAM·네트워크·S3 보안 점검)가 있어야 하는 답은 adminOnly. 일반 사용자(?mock-role=member)는
//   실제 서버처럼 '관리자만 볼 수 있다'는 답을 받는다 (services/llm/tool_access.py)
// - Vigie에 없는 도구(AWS Config·IAM 변경·보안 그룹 변경·삭제)가 필요한 일은 할 수 없다고 말하고, 사람이 직접 할 방법을 준다
// - 답변의 숫자·시각은 모두 frothly.json에서 계산한다. 시각은 지금에 맞춰 옮긴 한국 시각이다
// - 계정 ID는 Vigie처럼 끝 네 자리만 보인다 (MASKED_ACCOUNT)
import type { Artifact } from '@/types/artifacts';
import {
    FROTHLY,
    MASKED_ACCOUNT,
    PUBLIC_BUCKET,
    FORENSIC_INSTANCE,
    agoText,
    asgLaunches,
    demoCost,
    demoAlarms,
    epochOf,
    events,
    idleInstance,
    kstClock,
    manualTerminations,
    metric,
    metricAverage,
    metricMax,
    metricSum,
    peakOffset,
    round,
    when,
    LAMBDA_ERROR_KEYS,
    INITIAL_DEMO_STATE,
    type DemoState,
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
    artifacts?: Artifact[]; // 차트 (답변에는 ![제목](artifact://…) 참조만)
}

// 답을 만들 때 아는 것: 대화에서 승인해 바꾼 상태, 요청자가 관리자인가, 질문
interface DemoContext {
    state: DemoState;
    admin: boolean;
    text: string;
}

// 주제: 이어 묻는 말("그거", "두 번째")이 가리키는 앞 답변
export type Topic =
    | 'login'
    | 'timeline'
    | 'postmortem'
    | 'remediation'
    | 'security'
    | 'iam'
    | 'leastPrivilege'
    | 'actor'
    | 'ip'
    | 'network'
    | 'cpu'
    | 'asg'
    | 'health'
    | 'rds'
    | 'memory'
    | 'waste'
    | 'forecast'
    | 'cost'
    | 'alarms'
    | 'config'
    | 'errors'
    | 's3'
    | 'briefing'
    | 'greeting'
    // 이어 묻기로 주로 가는 주제 (직접 물어도 된다)
    | 'who'
    | 's3Objects'
    | 'costBreakdown'
    | 'errorLogs'
    | 'eip';

interface DemoAnswer {
    id: Topic;
    match: RegExp;
    adminOnly?: boolean;
    build: (ctx: DemoContext) => DemoEntry;
}

const ok = (tool_name: string, input: Record<string, unknown> = {}): DemoTool => ({ tool_name, input, status: 'ok' });
const failed = (tool_name: string, input: Record<string, unknown>, error: string): DemoTool => ({
    tool_name,
    input,
    status: 'error',
    error,
});
const lines = (...parts: (string | false | undefined)[]) => parts.filter((p) => p !== false && p !== undefined).join('\n');
const usd = (n: number) => `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const kst = (offset: number) => kstClock(epochOf(offset)); // 기록의 시각(끝으로부터 몇 초 전) → 한국 시각 HH:MM

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
const byActor = (actor: string) => FROTHLY.trail.filter((e) => e.actor === actor);
const actorStats = (actor: string) => FROTHLY.actors.find((a) => a.actor === actor);
// 사람이 직접 종료한 웹 서버 (한 번에 여러 대를 쉼표로 적는다)
const terminatedByHand = () => manualTerminations().flatMap((e) => (e.target ?? '').split(',').map((id) => id.trim()).filter(Boolean));
// 5분 칸의 시각 (지표의 i번째 점)
const slotClock = (key: string, index: number) => kst(-(metric(key).sum.length - 1 - index) * metric(key).period);
const lowestHealthy = () => {
    const values = metric('healthyHosts').average;
    const index = values.indexOf(Math.min(...values));
    return { value: values[index], at: slotClock('healthyHosts', index) };
};
const busiestSlot = () => {
    const values = metric('albRequests').sum;
    const index = values.indexOf(Math.max(...values));
    return { value: values[index], at: slotClock('albRequests', index) };
};
// 사용자가 이어서 물어볼 만한 것 (답 끝에 한 줄로)
const nextHint = (...questions: string[]) => `이어서 물어볼 수 있어요: ${questions.map((q) => `"${q}"`).join(' · ')}`;

// ---------------------------------------------------------------- 사고를 깊게 보는 답 (관리자: CloudTrail·IAM·네트워크)
const agentName = (agent?: string) => (agent ?? '').replace(/^\[/, '').split(' ')[0] || '알 수 없음'; // "Boto3/1.6.3 Python/…" → "Boto3/1.6.3"
const shortInstance = (id: string) =>
    FROTHLY.instances.find((i) => i.id === id)?.name ?? (id.startsWith('i-') ? '웹 서버(지금은 없음)' : id);

// 사고 경위: 이 계정에서 사람이 한 일을 시간 순으로 (서비스 역할의 자동 교체는 한 줄로 줄인다)
const timelineEntry = (): DemoEntry => {
    const probes = recon();
    const rows: [number, string, string][] = [
        [
            probes[0].at,
            '`web_admin` 키 (외부 IP ' + unique(probes.map((e) => e.ip)).length + '곳)',
            `IAM·S3·EC2 조회 ${probes.length}번 시도, **모두 거부** (${kst(probes[0].at)}~${kst(probes[probes.length - 1].at)}, 도구 ${unique(probes.map((e) => agentName(e.agent))).join(' · ')})`,
        ],
    ];
    const outputs = consoleOutputs();
    for (const e of byActor('bstoll')) {
        const text = (() => {
            switch (e.name) {
                case 'ConsoleLogin':
                    return `콘솔 로그인 · MFA 없음 · ${e.ip}`;
                case 'UpdateAccessKey':
                    return `\`${e.keyUser}\` 액세스 키 **비활성화**`;
                case 'PutBucketAcl':
                    return e.publicPermissions?.length
                        ? `\`${e.target}\`에 **모든 사용자 ${e.publicPermissions.join('·')}** ACL`
                        : `\`${e.target}\`의 공개 권한 제거`;
                case 'GetConsoleOutput':
                    return e === outputs[0]
                        ? `웹 서버 ${unique(outputs.map((o) => o.target)).length}대의 콘솔 출력 ${outputs.length}번 조회 (${kst(outputs[0].at)}~${kst(outputs[outputs.length - 1].at)})`
                        : undefined;
                case 'TerminateInstances':
                    return `웹 서버 ${(e.target ?? '').split(',').length}대 **직접 종료** → Auto Scaling이 새로 띄움`;
                case 'AuthorizeSecurityGroupIngress':
                    return `\`${e.target}\`에 **UDP 11211을 0.0.0.0/0 · ::/0에 개방**`;
                case 'RevokeSecurityGroupIngress':
                    return `UDP 11211 개방 규칙 제거`;
                default:
                    return undefined; // 규칙 설명 바꾸기 같은 곁가지
            }
        })();
        if (text) rows.push([e.at, 'bstoll', text]);
    }
    rows.sort((a, b) => a[0] - b[0]);
    const acl = publicAcl()!;
    const restored = aclRestored();
    const open = memcachedOpen();
    const closed = memcachedClosed();
    return {
        search: { query: 'lookup_events', found: ['lookup_events'] },
        tools: [
            ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'ReadOnly', AttributeValue: 'false' }], max_results: 200 }),
            ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'Username', AttributeValue: 'web_admin' }] }),
            ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'EventName', AttributeValue: 'ConsoleLogin' }] }),
        ],
        thinking: [
            '사고 경위를 시간 순으로 세우려면 변경 호출, 탈취가 의심되는 키의 호출, 콘솔 로그인을 모두 모아야 한다. CloudTrail에서 세 가지로 나눠 가져온다.',
            'Auto Scaling의 자동 교체는 사람의 행동이 아니라 한 줄로 줄인다. 기록으로 확인되는 것과 확인되지 않는 것을 나눠 적는다.',
        ],
        answer: lines(
            `CloudTrail에 남은 사람의 행동을 시간 순으로 세웠습니다 (한국 시각, 계정 ${MASKED_ACCOUNT}).`,
            '',
            '| 시각 | 누가 | 무엇 |',
            '|:--|:--|:--|',
            ...rows.map(([at, who, text]) => `| ${kst(at)} | ${who} | ${text} |`),
            '',
            '**기록으로 확인되는 것**',
            `- 쓰지 않던 \`web_admin\` 키가 외부 IP 여러 곳에서 서로 다른 도구로 쓰였습니다. 키가 샌 것으로 보이고, 권한이 없어 모두 거부됐습니다.`,
            `- \`${PUBLIC_BUCKET}\`는 **${restored ? minutesBetween(acl.at, restored.at) : '?'}분**, memcached 포트는 **${closed ? minutesBetween(open.at, closed.at) : '?'}분** 동안 열려 있었습니다. 두 변경 모두 MFA 없이 로그인한 \`bstoll\` 계정이 했습니다.`,
            '',
            '**기록만으로는 알 수 없는 것**',
            '- `bstoll`의 변경이 본인이 의도한 것인지, 계정이 도용된 것인지',
            `- 공개였던 동안 \`${PUBLIC_BUCKET}\`의 파일이 바뀌었는지 (CloudTrail 조회는 관리 이벤트만 보여 줍니다)`,
            '',
            nextHint('누가 했어?', '어떻게 대응해야 해?', '사고 보고서로 정리해 줘'),
        ),
    };
};

// 누가 했나: 변경은 모두 한 계정. 본인인지 도용인지는 기록으로 판단하지 않는다
const whoEntry = (): DemoEntry => {
    const mine = byActor('bstoll');
    const count = (name: string) => mine.filter((e) => e.name === name).length;
    const ips = unique(mine.map((e) => e.ip).filter(Boolean));
    const firstIp = logins()[0].ip;
    const probes = recon();
    return {
        search: { query: 'lookup_events', found: ['lookup_events'] },
        tools: [
            ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'Username', AttributeValue: 'bstoll' }] }),
            ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'Username', AttributeValue: 'web_admin' }] }),
        ],
        thinking: [
            '누가 했는지는 CloudTrail의 userIdentity로 본다. 변경한 사용자와 거부된 호출의 주체를 나눠 가져온다.',
            '변경은 모두 bstoll 한 계정이다. 다만 본인의 행동인지 계정 도용인지는 기록만으로 단정할 수 없다. 판단에 필요한 사실과 확인할 것을 적는다.',
        ],
        answer: lines(
            '**이 기간의 변경은 모두 IAM 사용자 `bstoll` 계정이 했습니다.** 다만 본인이 한 것인지, 계정이 도용된 것인지는 기록만으로 단정할 수 없습니다.',
            '',
            '| `bstoll`이 한 일 | 횟수 |',
            '|:--|--:|',
            `| 콘솔 로그인 (모두 MFA 없음) | ${count('ConsoleLogin')} |`,
            `| 버킷 ACL 변경 (공개 → 원복) | ${count('PutBucketAcl')} |`,
            `| 보안 그룹 규칙 변경 | ${count('AuthorizeSecurityGroupIngress') + count('RevokeSecurityGroupIngress') + count('UpdateSecurityGroupRuleDescriptionsIngress')} |`,
            `| 인스턴스 콘솔 출력 조회 | ${count('GetConsoleOutput')} |`,
            `| 인스턴스 직접 종료 | ${count('TerminateInstances')}번 (${terminatedByHand().length}대) |`,
            `| 다른 사용자의 키 비활성화 | ${count('UpdateAccessKey')} |`,
            '',
            '**판단에 쓸 사실**',
            `- 출발 IP가 ${ips.length}곳입니다. 첫 로그인과 키 비활성화는 \`${firstIp}\`, 버킷·보안 그룹·인스턴스 변경은 모두 \`${ips.find((ip) => ip !== firstIp)}\`에서 했습니다. 둘 다 본인이 쓰는 곳인지 확인해 보세요.`,
            '- 브라우저(User-Agent)는 네 번 모두 같습니다.',
            `- 첫 로그인 직후 한 일이 **탈취가 의심되는 \`web_admin\` 키를 막은 것**이라, 사고에 대응하던 담당자일 가능성이 있습니다.`,
            '',
            `\`web_admin\` 키로 권한을 떠본 쪽은 사람을 특정할 수 없습니다. 외부 IP ${unique(probes.map((e) => e.ip)).length}곳에서 도구 ${unique(probes.map((e) => agentName(e.agent))).length}종으로 시도했습니다.`,
            '',
            nextHint('bstoll이 한 일 자세히', '어떻게 대응해야 해?'),
        ),
    };
};

// 사용자 한 명이 한 일
const ACTOR_ALIASES: [RegExp, string][] = [
    [/bstoll/i, 'bstoll'],
    [/web_admin|웹\s*어드민/i, 'web_admin'],
    [/btun/i, 'btun'],
    [/splunk_access|스플렁크/i, 'splunk_access'],
    [/mkraeus/i, 'mkraeus'],
    [/frothly_admin/i, 'frothly_admin'],
];
const actorIn = (text: string) => ACTOR_ALIASES.find(([pattern]) => pattern.test(text))?.[1];

const actorEntry = (name: string): DemoEntry => {
    const stats = actorStats(name);
    const mine = byActor(name);
    const tools = [ok('get_user', { user_name: name }), ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'Username', AttributeValue: name }] })];
    const search = { query: 'get_user|lookup_events', found: ['get_user', 'lookup_events'] };
    const thinking = [`${name} 사용자의 정보와, CloudTrail에 남은 이 사용자의 호출을 가져온다.`];
    if (name === 'web_admin') {
        const disabled = keyDisabled();
        return {
            search,
            tools,
            thinking: [...thinking, '호출이 모두 거부됐다. 호출마다 출발 IP와 도구가 다르다. 키가 새어 여러 곳에서 쓰인 것으로 보인다.'],
            answer: lines(
                `\`web_admin\`의 호출은 **${stats?.total ?? mine.length}건이고 모두 거부**됐습니다. 이 사용자에게 권한이 거의 없어서, 키를 가진 쪽이 무엇을 할 수 있는지 떠보다 막힌 모양입니다.`,
                '',
                '| 시각 | 호출 | 출발 IP | 도구 | 결과 |',
                '|:--|:--|:--|:--|:--|',
                ...mine.map((e) => `| ${kst(e.at)} | \`${e.name}\` (${e.source}) | ${e.ip} | ${agentName(e.agent)} | ${e.error} |`),
                '',
                `- 요청 리전이 모두 \`us-east-1\`입니다. 이 계정은 \`${FROTHLY.region}\`을 쓰므로, 도구의 기본 리전으로 보입니다.`,
                `- ${disabled ? `${when(disabled.at)}에 \`${disabled.actor}\` 계정이 이 키를 비활성화했습니다. **삭제는 아직** 안 했습니다.` : '키는 아직 활성입니다.'}`,
                '- 키를 삭제하고, 키가 어디에 적혀 있었는지(코드 저장소, 서버 설정 파일) 찾아보세요.',
                '',
                nextHint('web_admin 키 삭제해 줘', '어떻게 대응해야 해?'),
            ),
        };
    }
    if (name === 'bstoll') {
        const changes = mine.filter((e) => !['ConsoleLogin', 'GetConsoleOutput'].includes(e.name));
        return {
            search,
            tools,
            thinking: [...thinking, `변경 ${changes.length}건과 로그인·콘솔 출력 조회가 있다. 변경을 시간 순으로 보인다.`],
            answer: lines(
                `\`bstoll\`은 이 기간에 호출 **${stats?.total ?? mine.length}건**, 그중 변경 **${stats?.writes ?? changes.length}건**을 했습니다. 모두 콘솔에서 **MFA 없이** 했습니다.`,
                '',
                '| 시각 | 변경 | 대상 | 출발 IP |',
                '|:--|:--|:--|:--|',
                ...changes.map((e) => `| ${kst(e.at)} | \`${e.name}\` | ${e.target || '-'} | ${e.ip} |`),
                '',
                `- 이 밖에 콘솔 로그인 ${logins().length}번, 인스턴스 콘솔 출력 조회 ${consoleOutputs().length}번이 있습니다.`,
                '- 버킷 공개, 포트 개방, 인스턴스 종료처럼 영향이 큰 변경을 MFA 없이 할 수 있는 상태입니다. 변경 권한에 MFA 조건을 거는 것을 권합니다.',
                '',
                nextHint('누가 했어? 본인이야?', 'IP별로 보여 줘'),
            ),
        };
    }
    const writes = mine.filter((e) => !e.error);
    return {
        search,
        tools,
        thinking: [...thinking, stats ? `호출 ${stats.total}건, 변경 ${stats.writes}건이다.` : '이 기간에 호출이 없다.'],
        answer: stats
            ? lines(
                  `\`${name}\`은 이 기간에 호출 **${stats.total.toLocaleString()}건**, 변경 **${stats.writes}건**, 거부 ${stats.denied}건입니다.`,
                  '',
                  writes.length
                      ? lines('| 시각 | 변경 | 대상 |', '|:--|:--|:--|', ...writes.map((e) => `| ${kst(e.at)} | \`${e.name}\` | ${e.target || '-'} |`))
                      : '변경 호출은 없고 모두 조회입니다.',
                  '',
                  name === 'splunk_access'
                      ? '- 로그를 모으는 연동 계정입니다. 조회만 하므로 읽기 전용 권한으로 좁힐 수 있습니다.'
                      : undefined,
              )
            : lines(
                  `\`${name}\`은 IAM 사용자로 있지만, **이 기간에 호출한 기록이 없습니다.**`,
                  '',
                  '- 오래 쓰지 않은 사용자라면 콘솔 암호와 액세스 키를 비활성화하는 것을 권합니다. 마지막으로 쓴 때는 IAM의 자격 증명 보고서(credential report)로 볼 수 있습니다.',
              ),
    };
};

// IP: 특정 IP를 물으면 그 IP의 기록, 아니면 기록에 나온 IP 목록
const IP_PATTERN = /\b\d{1,3}(?:\.\d{1,3}){3}\b/;
const ipEntry = (ctx: DemoContext): DemoEntry => {
    const asked = ctx.text.match(IP_PATTERN)?.[0];
    const withIp = FROTHLY.trail.filter((e) => e.ip && IP_PATTERN.test(e.ip));
    const tools = [ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'ReadOnly', AttributeValue: 'false' }], max_results: 200 })];
    const search = { query: 'lookup_events', found: ['lookup_events'] };
    if (asked) {
        const hits = withIp.filter((e) => e.ip === asked);
        return {
            search,
            tools,
            thinking: [`${asked}에서 온 호출을 CloudTrail에서 찾는다.`, hits.length ? `${hits.length}건이 있다.` : '이 IP의 기록이 없다.'],
            answer: hits.length
                ? lines(
                      `\`${asked}\`에서 온 주요 기록은 **${hits.length}건**이고, 모두 \`${unique(hits.map((e) => e.actor)).join('`, `')}\`입니다.`,
                      '',
                      '| 시각 | 누가 | 호출 | 결과 |',
                      '|:--|:--|:--|:--|',
                      ...hits.map((e) => `| ${kst(e.at)} | ${e.actor} | \`${e.name}\` | ${e.error ?? '성공'} |`),
                      '',
                      '이 IP가 누구의 것인지는 Vigie가 조회할 수 없습니다. 사내 IP 목록이나 공개 WHOIS로 확인해 보세요.',
                  )
                : lines(
                      `\`${asked}\`에서 온 기록은 **없습니다.**`,
                      '',
                      `이 기록에 나온 외부 IP는 ${unique(withIp.map((e) => e.ip)).length}곳입니다: ${unique(withIp.map((e) => `\`${e.ip}\``)).join(', ')}.`,
                  ),
        };
    }
    const ips = unique(withIp.map((e) => e.ip!));
    return {
        search,
        tools,
        thinking: ['사람이 한 호출의 출발 IP를 모은다. AWS 서비스가 대신 한 호출은 뺀다.', `외부 IP ${ips.length}곳이다. IP마다 누가 무엇을 했는지 묶는다.`],
        answer: lines(
            `주요 기록에 나온 외부 IP는 **${ips.length}곳**입니다.`,
            '',
            '| IP | 누가 | 기록 | 처음 ~ 마지막 | 비고 |',
            '|:--|:--|--:|:--|:--|',
            ...ips.map((ip) => {
                const hits = withIp.filter((e) => e.ip === ip);
                const denied = hits.every((e) => e.error);
                return `| ${ip} | ${unique(hits.map((e) => e.actor)).join(', ')} | ${hits.length} | ${kst(hits[0].at)} ~ ${kst(hits[hits.length - 1].at)} | ${denied ? '모두 거부' : hits.some((e) => e.name === 'ConsoleLogin') ? '콘솔 로그인' : ''} |`;
            }),
            '',
            '- `web_admin`은 IP마다 한두 번씩만 시도했습니다. 여러 곳에서 같은 키를 쓴 것이라 키가 샌 것으로 보입니다.',
            '- `bstoll`은 두 IP를 썼습니다. 첫 로그인 뒤의 변경은 모두 한 IP에서 했습니다.',
        ),
    };
};

// 네트워크: 전 세계에 열린 보안 그룹
const networkEntry = (): DemoEntry => {
    const open = memcachedOpen();
    const closed = memcachedClosed();
    const groups = FROTHLY.securityGroups.filter((g) => g.openToWorld.length);
    const portText = (r: { protocol: string; port: string | null }) => (r.protocol === '-1' ? '**모든 포트**' : `${r.protocol.toUpperCase()} ${r.port}`);
    const inUse = groups.filter((g) => g.instances.length);
    return {
        search: { query: 'get_vpc_network|lookup_events', found: ['get_vpc_network', 'lookup_events'] },
        tools: [
            ok('get_vpc_network', { vpc_id: 'vpc-05cb3dfad78e47394', region: FROTHLY.region }),
            ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'EventSource', AttributeValue: 'ec2.amazonaws.com' }] }),
        ],
        thinking: [
            'VPC의 보안 그룹 규칙에서 0.0.0.0/0에 열린 것을 찾고, 오늘 보안 그룹을 바꾼 기록을 함께 본다.',
            `전 세계에 열린 보안 그룹이 ${groups.length}개인데, 실제로 인스턴스에 붙은 것은 ${inUse.length}개다. 오늘 memcached 포트를 열었다 닫은 기록이 있다.`,
        ],
        answer: lines(
            `**지금 전 세계(0.0.0.0/0)에 포트를 연 보안 그룹이 ${groups.length}개**입니다. 인스턴스에 붙어 실제로 쓰이는 것은 ${inUse.length}개입니다.`,
            '',
            '| 보안 그룹 | 열린 포트 | 붙은 인스턴스 |',
            '|:--|:--|:--|',
            ...groups
                .sort((a, b) => b.instances.length - a.instances.length)
                .map((g) => `| \`${g.name ?? g.id}\` | ${g.openToWorld.map(portText).join(', ')} | ${g.instances.map(shortInstance).join(', ') || '없음'} |`),
            '',
            `**오늘의 변경**: ${when(open.at)}에 \`${open.actor}\` 계정이 웹 서버용 그룹에 **UDP 11211(memcached)을 전 세계에** 열었고, ${closed ? `${minutesBetween(open.at, closed.at)}분 뒤 닫았습니다` : '아직 열려 있습니다'}. memcached가 밖에 열리면 증폭 DDoS에 악용될 수 있습니다.`,
            '',
            '**권장**',
            '- 붙은 인스턴스가 없는 `launch-wizard-*`·`my_sec_group`(모든 포트 개방)은 지우세요.',
            '- 웹 서버 그룹의 SSH(22)는 사무실 IP로 좁히거나 Session Manager로 바꾸세요.',
            '',
            'Vigie에는 보안 그룹을 바꾸는 도구가 없습니다. "SSH 22번 닫아 줘"라고 하면 직접 실행할 명령을 드립니다.',
        ),
    };
};

// 대응 계획: 대화에서 승인해 바로 할 수 있는 것과, 사람이 직접 해야 하는 것
const remediationEntry = ({ state }: DemoContext): DemoEntry => {
    const idle = idleInstance();
    const done = '✅ 완료 (대화에서 승인)';
    return {
        tools: [],
        thinking: [
            '앞에서 확인한 사실로 대응 순서를 정한다. Vigie가 승인을 받아 할 수 있는 변경(S3 퍼블릭 액세스 차단, EC2 중지)과 도구가 없어 사람이 직접 해야 하는 일(IAM, 보안 그룹)을 나눈다.',
        ],
        answer: lines(
            '급한 순서대로 정리했습니다. **Vigie가 승인을 받아 할 수 있는 것**과 **직접 하셔야 하는 것**을 나눴습니다.',
            '',
            '| 순서 | 할 일 | 누가 · 어떻게 | 지금 |',
            '|--:|:--|:--|:--|',
            `| 1 | \`${PUBLIC_BUCKET}\` 퍼블릭 액세스 차단 | Vigie · "퍼블릭 액세스 차단 켜 줘" → 승인 | ${state.webcodeBlocked ? done : '할 일'} |`,
            `| 2 | 공개였던 동안 \`${PUBLIC_BUCKET}\`의 파일이 바뀌었는지 확인 | 직접 · S3 서버 액세스 로그 | 할 일 |`,
            '| 3 | `web_admin` 액세스 키 **삭제**, 샌 곳 찾기 | 직접 · IAM 콘솔이나 CLI | 비활성화만 됨 |',
            '| 4 | `bstoll` 본인 확인, 콘솔 사용자에 MFA 강제 | 직접 · IAM 정책 | 할 일 |',
            '| 5 | SSH(22)·모든 포트를 연 보안 그룹 정리 | 직접 · EC2 콘솔이나 CLI | 할 일 |',
            `| 6 | 조사용 \`${idle.name}\` 정리 | Vigie · "${idle.name} 멈춰 줘" → 승인 | ${state.forensicStopped ? done : '조사가 끝나면'} |`,
            '',
            'IAM과 보안 그룹은 Vigie가 바꾸지 않습니다. 권한과 네트워크를 대화로 바꾸게 두면, 대화가 속았을 때 피해가 커지기 때문입니다. 3번은 이렇게 하시면 됩니다.',
            '',
            '```bash',
            'aws iam list-access-keys --user-name web_admin',
            'aws iam delete-access-key --user-name web_admin --access-key-id <위에서 본 키 ID>',
            '```',
            '',
            nextHint('사고 보고서로 정리해 줘'),
        ),
    };
};

// 사고 보고서 초안
const postmortemEntry = ({ state }: DemoContext): DemoEntry => {
    const acl = publicAcl()!;
    const restored = aclRestored();
    const open = memcachedOpen();
    const closed = memcachedClosed();
    const probes = recon();
    const disabled = keyDisabled();
    const firstLogin = logins()[0];
    const alarms = demoAlarms();
    return {
        tools: [],
        thinking: ['이 대화에서 확인한 기록으로 사고 보고서 초안을 쓴다. 확인한 사실과 추정을 나누고, 담당자·기한은 비워 둔다.'],
        answer: lines(
            '## 사고 보고서 (초안)',
            '',
            `**요약**: ${kst(probes[0].at)}~${kst(closed?.at ?? open.at)} (한국 시각), 계정 ${MASKED_ACCOUNT}. 샌 것으로 보이는 액세스 키가 외부에서 쓰였고(모두 거부), 같은 날 콘솔 변경으로 S3 버킷과 memcached 포트가 한동안 전 세계에 열렸습니다.`,
            '',
            '**영향**',
            `- \`${PUBLIC_BUCKET}\`: ${restored ? minutesBetween(acl.at, restored.at) : '?'}분 동안 누구나 읽기·쓰기. 파일이 바뀌었는지는 **확인 필요**`,
            `- 웹 서버 보안 그룹: UDP 11211 ${closed ? minutesBetween(open.at, closed.at) : '?'}분 개방`,
            `- \`web_admin\` 키: 외부 IP ${unique(probes.map((e) => e.ip)).length}곳에서 ${probes.length}번 시도, 권한이 없어 피해 없음`,
            '',
            '**탐지와 대응**',
            `- 보안 알람 ${alarms.firing.length}개가 울렸습니다 (보안 그룹 변경, MFA 없는 로그인). 버킷 ACL 변경으로 울린 알람은 없었습니다.`,
            `- 키는 로그인 ${disabled ? Math.max(1, Math.round((disabled.at - firstLogin.at))) : '?'}초 만에 비활성화했습니다.`,
            state.webcodeBlocked ? `- 이 대화에서 \`${PUBLIC_BUCKET}\`의 퍼블릭 액세스 차단을 켰습니다.` : undefined,
            '',
            '**원인 (기록으로 확인)**',
            '- 쓰지 않는 IAM 사용자의 액세스 키가 남아 있었습니다.',
            '- MFA 없는 콘솔 사용자가 버킷 ACL과 보안 그룹을 직접 바꿀 수 있었습니다.',
            '',
            '**개선**',
            '',
            '| 할 일 | 담당 | 기한 |',
            '|:--|:--|:--|',
            '| 쓰지 않는 사용자·키 정리, 키 사용 기록 정기 점검 | | |',
            '| 콘솔 변경 권한에 MFA 조건 | | |',
            '| 계정 단위 S3 퍼블릭 액세스 차단 | | |',
            '| 버킷 정책·ACL 변경 알람 추가 (CIS 권고) | | |',
            '| 전 세계에 열린 보안 그룹 정리 | | |',
            '',
            '담당과 기한을 채워 공유하세요. 추정이 섞인 곳(파일 변경 여부, `bstoll`의 의도)은 확인 뒤 고쳐 주세요.',
        ),
    };
};

// S3 안의 파일이 바뀌었나: Vigie로는 볼 수 없는 것을 말하고, 볼 방법을 준다
const s3ObjectsEntry = (): DemoEntry => {
    const acl = publicAcl()!;
    const restored = aclRestored();
    return {
        search: { query: 'checkS3BucketSecurity|lookup_events', found: ['checkS3BucketSecurity', 'lookup_events'] },
        tools: [
            ok('checkS3BucketSecurity', { bucket_name: PUBLIC_BUCKET }),
            ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'ResourceName', AttributeValue: PUBLIC_BUCKET }] }),
        ],
        thinking: [
            '공개였던 동안 파일이 바뀌었는지 보려면 객체를 올리고 지운 기록(데이터 이벤트)이 필요하다. 버킷 설정과 CloudTrail 조회를 먼저 해 본다.',
            'CloudTrail 조회(LookupEvents)는 관리 이벤트만 준다. 버전 관리도 꺼져 있어 이전 파일과 견줄 수 없다. 여기서 확인할 수 없다고 말하고 방법을 안내한다.',
        ],
        answer: lines(
            `**Vigie로는 확인할 수 없습니다.** 공개였던 ${kst(acl.at)}~${restored ? kst(restored.at) : '?'}에 파일이 바뀌었는지 보려면 객체를 올리고 지운 기록이 있어야 하는데, 두 곳 모두 막혀 있습니다.`,
            '',
            '- **CloudTrail 조회**: Vigie가 쓰는 `lookup_events`는 관리 이벤트(설정 변경)만 보여 줍니다. `PutObject` 같은 데이터 이벤트는 나오지 않습니다.',
            '- **버전 관리**: 이 버킷은 버전 관리가 꺼져 있어, 파일이 덮어쓰였다면 이전 내용이 남아 있지 않습니다.',
            '',
            '직접 확인할 방법입니다.',
            '1. 버킷에 **서버 액세스 로그**가 켜져 있었다면, 그 시간대의 `REST.PUT.OBJECT`·`REST.DELETE.OBJECT`를 찾으세요.',
            '2. CloudTrail 트레일이 S3 **데이터 이벤트**를 남기게 되어 있었다면, S3에 쌓인 트레일 로그에서 찾을 수 있습니다.',
            '3. 둘 다 없다면, 배포에 쓰는 원본(코드 저장소)과 버킷의 파일을 견줘 보는 수밖에 없습니다.',
            '',
            '앞으로를 위해 버전 관리와 서버 액세스 로그를 켜 두기를 권합니다.',
        ),
    };
};

// ---------------------------------------------------------------- 운영 (모든 사용자)
// 웹 서비스 상태: ALB 정상 호스트와 요청 수
const healthEntry = ({ admin }: DemoContext): DemoEntry => {
    const low = lowestHealthy();
    const busy = busiestSlot();
    const hands = terminatedByHand();
    return {
        tools: [
            ok('get_metric_data', { namespace: 'AWS/ApplicationELB', metric_name: 'HealthyHostCount', statistic: 'Average', period: 300 }),
            ok('get_metric_data', { namespace: 'AWS/ApplicationELB', metric_name: 'RequestCount', statistic: 'Sum', period: 300 }),
        ],
        thinking: [
            '웹 서비스 상태는 로드 밸런서의 정상 호스트 수와 요청 수로 본다. 최근 3시간을 5분 단위로 가져온다.',
            `정상 호스트가 평균 ${metricAverage('healthyHosts')}대이고, 한 번 ${low.value}대까지 떨어졌다. 요청은 한때 몰렸다가 줄었다.`,
        ],
        answer: lines(
            `웹 서비스는 **지금 정상**입니다. 로드 밸런서 뒤의 정상 호스트는 최근 3시간 평균 **${metricAverage('healthyHosts')}대**입니다.`,
            '',
            '| 지표 (최근 3시간) | 값 |',
            '|:--|:--|',
            `| 정상 호스트 | 평균 ${metricAverage('healthyHosts')}대 · 최대 ${metricMax('healthyHosts')}대 · **가장 적을 때 ${low.value}대 (${low.at})** |`,
            `| 요청 수 | 모두 ${metricSum('albRequests').toLocaleString()}건 · 가장 많을 때 5분에 ${busy.value}건 (${busy.at}) |`,
            '',
            `- ${low.at}의 5분 평균 ${low.value}대는, 그 5분 중 일부 동안 **정상 호스트가 없었다**는 뜻입니다. 그 시각 전후로 ALB의 5xx 응답이 있었는지 확인해 보세요.`,
            admin
                ? `- 오늘 \`bstoll\`이 웹 서버 ${hands.length}대를 직접 종료했고(${manualTerminations().map((e) => kst(e.at)).join(', ')}), 그때마다 Auto Scaling이 새 인스턴스를 띄웠습니다.`
                : `- 오늘 웹 서버 인스턴스가 ${asgLaunches()}번 새로 떴습니다. 교체되는 동안 정상 호스트가 줄 수 있습니다.`,
            '',
            nextHint('웹 서버가 왜 자꾸 교체돼?', '차트로 그려 줘'),
        ),
    };
};

// Auto Scaling 교체
const asgEntry = ({ admin }: DemoContext): DemoEntry => {
    const auto = events('TerminateInstances').filter((e) => e.actor === 'AWSServiceRoleForAutoScaling');
    const policy = events('PutScalingPolicy')[0];
    return {
        tools: [ok('listEc2Instances', {}), ...(admin ? [ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'EventName', AttributeValue: 'TerminateInstances' }] })] : [])],
        search: admin ? { query: 'lookup_events', found: ['lookup_events'] } : undefined,
        thinking: [
            admin
                ? '인스턴스 목록과 함께, 인스턴스를 종료한 CloudTrail 기록을 가져와 누가 종료했는지 본다.'
                : '인스턴스 목록으로 웹 서버 그룹의 상태를 본다. 누가 종료했는지는 CloudTrail(관리자 전용)이라 보지 않는다.',
            '사람이 먼저 종료하고 Auto Scaling이 원하는 수를 맞추려 새로 띄운 흐름이다. 장애로 교체된 것이 아니다.',
        ],
        answer: lines(
            `**장애 때문이 아니라, 사람이 인스턴스를 종료해서** 교체된 것입니다. 오늘 웹 서버 그룹은 인스턴스를 ${asgLaunches()}번 새로 띄웠습니다.`,
            '',
            admin
                ? lines(
                      '| 시각 | 누가 | 무엇 |',
                      '|:--|:--|:--|',
                      ...manualTerminations().map((e) => `| ${kst(e.at)} | ${e.actor} | 웹 서버 ${(e.target ?? '').split(',').length}대 종료 |`),
                      `| ${kst(auto[0].at)}~${kst(auto[auto.length - 1].at)} | Auto Scaling | 종료된 인스턴스를 그룹에서 빼고 새로 ${asgLaunches()}대 띄움 |`,
                      policy ? `| ${kst(policy.at)} | ${policy.actor} | 스케일링 정책 변경 (\`PutScalingPolicy\`) |` : undefined,
                  )
                : `사람이 직접 종료한 인스턴스가 ${terminatedByHand().length}대이고, Auto Scaling이 그만큼을 새로 띄웠습니다. 누가 종료했는지는 CloudTrail 조회라 관리자만 볼 수 있습니다.`,
            '',
            '- Auto Scaling 그룹 안의 인스턴스는 직접 종료하면 곧 새로 뜹니다. 줄이려면 그룹의 원하는 용량(desired capacity)을 바꾸세요.',
            `- 새 인스턴스는 부팅 동안 로드 밸런서 상태 검사를 통과하지 못해, 정상 호스트가 잠시 줄 수 있습니다.`,
        ),
    };
};

// RDS
const rdsEntry = (): DemoEntry => {
    const freeGb = round(metricAverage('rdsFreeStorage') / 1024 ** 3, 1);
    return {
        tools: [
            ok('get_metric_data', { namespace: 'AWS/RDS', metric_name: 'CPUUtilization', statistic: 'Average', period: 300 }),
            ok('get_metric_data', { namespace: 'AWS/RDS', metric_name: 'FreeStorageSpace', statistic: 'Average', period: 300 }),
        ],
        thinking: ['RDS 인스턴스의 CPU와 남은 저장 공간을 최근 3시간 5분 단위로 가져온다.', '둘 다 안정적이다. 감사 로그를 옮기는 Lambda의 오류가 있어 함께 알린다.'],
        answer: lines(
            `RDS는 **안정적**입니다 (최근 3시간).`,
            '',
            '| 지표 | 평균 | 최대 |',
            '|:--|--:|--:|',
            `| CPU | ${metricAverage('rdsCpu')}% | ${metricMax('rdsCpu')}% |`,
            `| 남은 저장 공간 | ${freeGb} GiB | 거의 변화 없음 |`,
            '',
            `- 다만 RDS 감사 로그를 옮기는 \`RDSAuditLogs\` 함수가 같은 기간에 ${metricSum(LAMBDA_ERROR_KEYS.RDSAuditLogs)}번 실패했습니다. 감사 로그가 빠졌을 수 있습니다.`,
            '- 연결 수·읽기 지연 같은 지표도 더 볼 수 있습니다.',
            '',
            nextHint('Lambda 오류 원인은?'),
        ),
    };
};

// 월말 예상 비용
const forecastEntry = ({ state }: DemoContext): DemoEntry => {
    const cost = demoCost();
    const perDay = cost.monthToDate / cost.daily.length;
    const daysLeft = cost.forecast / perDay - cost.daily.length;
    const idleMonthly = 0.0138 * 730;
    return {
        tools: [ok('cost-explorer', { operation: 'getCostForecast', granularity: 'MONTHLY', metric: 'UNBLENDED_COST' })],
        thinking: ['이번 달 남은 기간의 비용 예측을 Cost Explorer에서 가져온다.', '지금까지의 하루 평균으로 월말을 예상한다. 줄일 수 있는 항목을 함께 적는다.'],
        answer: lines(
            `이번 달은 **약 ${usd(cost.forecast)}**로 끝날 것으로 예상됩니다. 지금까지 ${usd(cost.monthToDate)}(하루 평균 ${usd(perDay)})를 썼고, ${Math.round(daysLeft)}일 남았습니다.`,
            '',
            `- 지난달 같은 기간보다 ${round((cost.monthToDate / cost.lastMonthSamePeriod - 1) * 100)}% 많은 속도입니다.`,
            state.forensicStopped
                ? '- 조사용 인스턴스를 중지해 두었으므로, 남은 기간에는 그만큼 줄어듭니다.'
                : `- 조사용 인스턴스(\`${idleInstance().name}\`)를 멈추면 한 달 기준 약 ${usd(idleMonthly)}를 줄일 수 있습니다.`,
            '- 예측은 지금까지의 사용량을 이어 그린 값이라, 인스턴스를 더 띄우면 달라집니다.',
            '',
            nextHint('서비스별 비용 내역', '비용 차트로 그려 줘'),
        ),
    };
};

// 서비스별 비용 전체
const costBreakdownEntry = (): DemoEntry => {
    const cost = demoCost();
    return {
        tools: [ok('cost-explorer', { operation: 'getCostAndUsage', granularity: 'MONTHLY', group_by: 'SERVICE' })],
        thinking: ['이번 달 비용을 서비스별로 나눠 가져온다.'],
        answer: lines(
            `이번 달 지금까지 **${usd(cost.monthToDate)}**의 서비스별 내역입니다.`,
            '',
            '| 서비스 | 비용 | 비중 |',
            '|:--|--:|--:|',
            ...cost.byService.map((s) => `| ${s.service} | ${usd(s.amount)} | ${round((s.amount / cost.monthToDate) * 100)}% |`),
            '',
            '- EC2가 가장 큽니다. 웹 서버 그룹과 조사용 인스턴스가 대부분입니다.',
            '- Datadog은 AWS Marketplace로 산 모니터링 구독이라, AWS 청구서에 함께 나옵니다.',
        ),
    };
};

// AWS Config 규정 준수: Vigie에 도구가 없다
const configEntry = ({ admin }: DemoContext): DemoEntry => ({
    tools: [],
    thinking: ['AWS Config의 규칙 평가 결과가 필요하다. Vigie의 도구 목록에는 Config를 조회하는 도구가 없다. 할 수 없다고 말하고 대신 볼 수 있는 것을 안내한다.'],
    answer: lines(
        '**Vigie에는 AWS Config를 조회하는 도구가 없어**, 규칙별 준수 여부를 직접 보여 드릴 수 없습니다.',
        '',
        '대신 이렇게 볼 수 있습니다.',
        admin
            ? '- 대화에서: "S3 버킷 보안 점검해 줘"(퍼블릭 액세스·암호화·버전 관리), "전 세계에 열린 보안 그룹"(SSH 개방)'
            : '- 대화에서: 알람·오류·비용·EC2 상태 (S3·보안 그룹 점검은 관리자 전용입니다)',
        '- 직접: 아래 명령으로 규칙을 어긴 리소스를 볼 수 있습니다.',
        '',
        '```bash',
        'aws configservice describe-compliance-by-config-rule --compliance-types NON_COMPLIANT',
        '```',
    ),
});

// Lambda 오류의 원인: 로그를 보려다 실패하고, 지표로 좁힌다 (도구 실패를 다루는 모습)
const errorLogsEntry = (): DemoEntry => {
    const peak = epochOf(peakOffset(LAMBDA_ERROR_KEYS.RDSAuditLogs));
    const logGroup = '/aws/lambda/RDSAuditLogs';
    return {
        tools: [
            failed(
                'execute_log_insights_query',
                { log_group_names: [logGroup], query_string: 'filter @message like /ERROR|Task timed out/ | stats count() by bin(5m)' },
                'ResourceNotFoundException: The specified log group does not exist.',
            ),
            ok('describe_log_groups', { log_group_name_prefix: '/aws/lambda/' }),
            ok('get_metric_data', { namespace: 'AWS/Lambda', metric_name: 'Duration', statistic: 'Maximum', group_by: 'FunctionName' }),
        ],
        thinking: [
            '오류 메시지를 보려고 함수 로그를 Logs Insights로 찾는다.',
            '함수의 로그 그룹이 없다고 나온다. 로그로는 원인을 볼 수 없으니 실행 시간 지표로 좁히고, 로그가 없는 까닭을 함께 알린다.',
        ],
        answer: lines(
            `오류 메시지를 보려 했지만 **\`${logGroup}\` 로그 그룹이 없어서** 로그를 읽지 못했습니다. 함수가 실행되는데 로그 그룹이 없다면, 로그를 쓰지 못하고 있다는 뜻입니다.`,
            '',
            '로그 없이 지표로 좁힌 것입니다.',
            '',
            '| 함수 | 오류 | 평균 실행 시간 | 최대 실행 시간 |',
            '|:--|--:|--:|--:|',
            `| RDSAuditLogs | ${metricSum(LAMBDA_ERROR_KEYS.RDSAuditLogs)} | ${Math.round(metricAverage('rdsAuditLogsDuration'))}ms | ${Math.round(metricMax('rdsAuditLogsDuration'))}ms |`,
            `| VPCFlowLogs | ${metricSum(LAMBDA_ERROR_KEYS.VPCFlowLogs)} | ${Math.round(metricAverage('vpcFlowLogsDuration'))}ms | ${Math.round(metricMax('vpcFlowLogsDuration'))}ms |`,
            '',
            `- 최대 실행 시간이 1초 안쪽이라 **시간 초과는 아닙니다.** 오류는 ${kstClock(peak)} 전후에 몰렸습니다.`,
            '- 두 함수가 같은 때 실패했으므로 공통으로 쓰는 것을 먼저 보세요: 로그를 보내는 대상(Splunk 수집 주소), 실행 역할의 권한.',
            '- 로그 그룹이 없는 것은 실행 역할에 `logs:CreateLogGroup`·`logs:PutLogEvents` 권한이 없거나, 누가 로그 그룹을 지웠을 때 생깁니다. 로그가 없으면 다음 장애도 원인을 볼 수 없으니 먼저 고치세요.',
        ),
    };
};

// 탄력적 IP 해제: 도구가 없다
const eipEntry = (): DemoEntry => {
    const eip = FROTHLY.eips.find((e) => !e.attached);
    return {
        search: { query: 'findEc2Waste', found: ['findEc2Waste'] },
        tools: [ok('findEc2Waste', {})],
        thinking: ['연결되지 않은 탄력적 IP를 확인한다. 해제하는 도구는 Vigie에 없다.'],
        answer: eip
            ? lines(
                  `연결되지 않은 탄력적 IP는 \`${eip.ip}\` 하나입니다. 쓰지 않아도 시간당 $0.005(한 달 약 ${usd(0.005 * 730)})가 나갑니다.`,
                  '',
                  'Vigie에는 탄력적 IP를 해제하는 도구가 없습니다. 해제하면 같은 IP를 다시 받을 수 없으니, DNS나 방화벽 허용 목록에 이 IP가 없는지 먼저 확인한 뒤 직접 해제하세요.',
                  '',
                  '```bash',
                  `aws ec2 describe-addresses --public-ips ${eip.ip}`,
                  'aws ec2 release-address --allocation-id <위에서 본 eipalloc-…>',
                  '```',
              )
            : '연결되지 않은 탄력적 IP는 없습니다.',
    };
};

// 오늘 현황 한눈에
const briefingEntry = ({ admin, state }: DemoContext): DemoEntry => {
    const alarms = demoAlarms();
    const cost = demoCost();
    const errors = metricSum(LAMBDA_ERROR_KEYS.RDSAuditLogs) + metricSum(LAMBDA_ERROR_KEYS.VPCFlowLogs);
    const acl = publicAcl()!;
    const restored = aclRestored();
    const open = memcachedOpen();
    const closed = memcachedClosed();
    return {
        search: admin ? { query: 'lookup_events', found: ['lookup_events'] } : undefined,
        tools: [
            ok('get_active_alarms', { max_items: 50 }),
            ok('get_metric_data', { namespace: 'AWS/Lambda', metric_name: 'Errors', statistic: 'Sum', group_by: 'FunctionName' }),
            ok('cost-explorer', { operation: 'getCostAndUsage', granularity: 'MONTHLY' }),
            ...(admin ? [ok('lookup_events', { lookup_attributes: [{ AttributeKey: 'ReadOnly', AttributeValue: 'false' }] })] : []),
        ],
        thinking: [
            admin ? '현황을 한 번에 보려면 알람, 오류, 비용, 오늘의 변경을 함께 가져온다.' : '현황을 한 번에 보려면 알람, 오류, 비용을 함께 가져온다. CloudTrail은 관리자 전용이라 부르지 않는다.',
            '영역마다 한 줄로 줄이고, 먼저 볼 것을 맨 위에 둔다.',
        ],
        answer: lines(
            '오늘 현황입니다.',
            '',
            '| 영역 | 상태 | 요약 |',
            '|:--|:--|:--|',
            admin
                ? `| 보안 | 🔴 | \`${PUBLIC_BUCKET}\` ${restored ? minutesBetween(acl.at, restored.at) : '?'}분 공개, memcached ${closed ? minutesBetween(open.at, closed.at) : '?'}분 개방 (지금은 닫힘), 샌 것으로 보이는 키 1개 |`
                : undefined,
            `| 알람 | 🟠 | ${alarms.total}개 중 ${alarms.firing.length}개 울림 (보안 알람) |`,
            `| 오류 | 🟡 | 로그 수집 Lambda 2개에서 ${errors}건 (최근 3시간) |`,
            `| 웹 서비스 | 🟢 | 정상 호스트 평균 ${metricAverage('healthyHosts')}대, 인스턴스 ${asgLaunches()}번 교체 |`,
            `| 비용 | 🟡 | 이번 달 ${usd(cost.monthToDate)}, 월말 예상 ${usd(cost.forecast)} (지난달보다 ${round((cost.monthToDate / cost.lastMonthSamePeriod - 1) * 100)}%↑) |`,
            `| 개선 권고 | ${state.webcodeBlocked && state.forensicStopped ? '🟢' : '🟡'} | ${[!state.webcodeBlocked && '퍼블릭 액세스 차단 안 된 버킷 1개', !state.forensicStopped && '유휴 EC2 1대'].filter(Boolean).join(', ') || '없음'} |`,
            '',
            admin
                ? nextHint('사고 경위를 시간 순으로', '지금 울리는 알람 알려줘', 'Lambda 오류 원인은?')
                : nextHint('지금 울리는 알람 알려줘', 'Lambda 오류 원인은?', '월말 예상 비용'),
        ),
    };
};

// ---------------------------------------------------------------- 가드: 거절하거나 할 수 없다고 답하는 요청
interface Guard {
    match: RegExp;
    build: (ctx: DemoContext) => DemoEntry;
}

const noTool = (thinking: string, answer: string): DemoEntry => ({ tools: [], thinking: [thinking], answer });

// 삭제·종료: Vigie에는 지우는 도구가 없다. 대상마다 할 수 있는 대안을 준다
// 일반 사용자가 IAM·보안 그룹을 바꿔 달라고 할 때 (대상 이름·명령은 관리자 전용 정보라 보이지 않는다)
const adminTaskRefusal = (thinking: string): DemoEntry =>
    noTool(
        thinking,
        'IAM과 보안 그룹은 Vigie가 바꾸지 않고, 그 정보도 **관리자(admins 그룹)만** 볼 수 있습니다. 관리자에게 요청해 주세요.',
    );

const destroyEntry = ({ text, admin }: DemoContext): DemoEntry => {
    const idle = idleInstance();
    const intro = '**Vigie에는 리소스를 지우거나 종료하는 도구가 없습니다.** AWS를 바꾸는 도구는 되돌리기 쉬운 네 가지(로그 보존 기간, 알람 알림, EC2 중지·시작, S3 퍼블릭 액세스 차단)뿐이고, 그것도 승인 뒤에만 실행됩니다.';
    const thinking = '지우거나 종료해 달라는 요청이다. Vigie에는 삭제 도구가 없다. 대상에 맞는 대안과 직접 할 방법을 안내한다.';
    if (/인스턴스|서버|EC2|포렌식|Forensic/i.test(text))
        return noTool(thinking, lines(intro, '', `인스턴스는 **중지**까지 할 수 있습니다. "${idle.name} 멈춰 줘"라고 하면 승인 요청을 만듭니다. 중지하면 시간당 요금은 멈추고, 디스크(EBS)는 남아 조사 자료를 보존할 수 있습니다.`, '', '완전히 없애려면 조사 자료를 스냅샷으로 남긴 뒤 직접 종료하세요.', '', '```bash', `aws ec2 create-snapshot --volume-id <볼륨 ID> --description "forensic ${idle.id}"`, `aws ec2 terminate-instances --instance-ids ${idle.id}`, '```'));
    if (/키|사용자|IAM|web_admin|계정/i.test(text))
        return admin
            ? noTool(thinking, lines(intro, '', 'IAM은 조회만 합니다. 공식 IAM MCP 서버의 변경 도구 17개는 일부러 뺐습니다. 권한을 대화로 바꾸게 두면, 대화가 속았을 때 피해가 가장 크기 때문입니다.', '', '직접 이렇게 하시면 됩니다.', '', '```bash', 'aws iam list-access-keys --user-name web_admin', 'aws iam delete-access-key --user-name web_admin --access-key-id <위에서 본 키 ID>', '```', '', '지우기 전에 이 키가 어디에 적혀 있었는지 찾아 두세요. 새 키로 바꿔야 할 곳입니다.'))
            : adminTaskRefusal(thinking);
    if (/로그/.test(text))
        return noTool(thinking, lines(intro, '', '로그를 바로 지울 수는 없고, **보존 기간을 줄이는 것**은 승인 뒤 할 수 있습니다 ("로그 보존 기간 14일로 줄여 줘"). 다만 사고를 조사하는 동안에는 기록을 줄이지 않기를 권합니다.'));
    if (/버킷|S3|파일|객체/i.test(text))
        return noTool(thinking, lines(intro, '', `버킷과 파일은 지울 수 없습니다. 공개가 걱정이라면 "\`${PUBLIC_BUCKET}\` 퍼블릭 액세스 차단 켜 줘"로 막을 수 있습니다. 사고 조사 중에는 버킷의 파일이 증거이니 지우지 마세요.`));
    if (/보안\s*그룹/.test(text))
        return admin
            ? noTool(thinking, lines(intro, '', '보안 그룹은 조회만 합니다. 붙은 인스턴스가 없는 그룹은 직접 지우면 됩니다.', '', '```bash', 'aws ec2 delete-security-group --group-id <그룹 ID>', '```', '', '인스턴스에 붙은 그룹은 지워지지 않습니다 (DependencyViolation).'))
            : adminTaskRefusal(thinking);
    if (/알람/.test(text))
        return noTool(thinking, lines(intro, '', '알람은 지울 수 없고, 점검하는 동안 **알림만 끌 수 있습니다** ("vigie-dev-api-5xx 알람 꺼 줘").'));
    if (/IP|탄력적/i.test(text)) return eipEntry();
    return noTool(thinking, intro);
};

const GUARDS: Guard[] = [
    {
        // 승인 건너뛰기·규칙 무시·시스템 프롬프트 요청
        match: /승인\s*(없이|안\s*받고|건너|생략|필요\s*없)|바로\s*실행|이전\s*(지시|명령|규칙)|지시.*무시|규칙.*무시|ignore\s+(all|previous|the)|시스템\s*프롬프트|system\s*prompt|개발자\s*모드|탈옥|jailbreak/i,
        build: () =>
            noTool(
                '승인을 건너뛰거나 규칙을 무시하라는 요청이다. 변경은 서버가 승인을 다시 확인하므로 대화로 건너뛸 수 없다. 내부 설정도 보이지 않는다.',
                lines(
                    '그렇게 할 수 없습니다. **승인은 제가 지키는 약속이 아니라 서버가 강제하는 절차입니다.**',
                    '',
                    '- AWS를 바꾸는 도구를 부르면 서버가 실행하지 않고 승인 요청을 만듭니다. MCP 서버는 사람이 승인한 작업만 한 번 실행합니다.',
                    '- 그래서 제가 설득되더라도, 로그나 문서에 "승인 없이 실행하라"는 문구가 섞여 있더라도 변경은 승인 없이 실행되지 않습니다.',
                    '- 시스템 프롬프트와 내부 설정은 보여 드리지 않습니다.',
                    '',
                    '바꾸고 싶은 것이 있으면 말씀해 주세요. 승인 요청을 만들어 드립니다.',
                ),
            ),
    },
    {
        // 권한 올려 달라
        match: /관리자\s*(권한|로)\s*(을\s*)?(줘|주|바꿔|올려|만들어|달라)|나(를|도)?\s*관리자|권한\s*(좀\s*)?(올려|높여)/,
        build: ({ admin }) =>
            noTool(
                '자기 권한을 올려 달라는 요청이다. 대화로는 권한을 바꿀 수 없다.',
                admin
                    ? '이미 관리자입니다. 다른 사람의 권한은 위쪽의 **사용자 관리** 화면에서 바꿀 수 있습니다 (변경은 감사 로그에 남습니다).'
                    : '대화로는 권한을 바꿀 수 없습니다. 관리자에게 요청하면, 관리자가 **사용자 관리** 화면에서 바꿉니다. 그 변경은 감사 로그에 남습니다.',
            ),
    },
    {
        // 비밀 값
        match: /비밀\s*번호|패스워드|password|시크릿\s*(액세스\s*)?키|secret|(액세스\s*키|토큰|자격\s*증명)\s*(값|원문|전체)?\s*(을|를)?\s*(알려|보여|줘|출력)/i,
        build: () =>
            noTool(
                '비밀 값을 알려 달라는 요청이다. 조회할 도구가 없고, 결과에 섞여도 가린다.',
                lines(
                    '비밀번호나 액세스 키의 비밀 값은 **보여 드릴 수 없습니다.**',
                    '',
                    '- 그런 값을 읽는 도구가 없습니다. AWS도 비밀 액세스 키는 만들 때 한 번만 보여 줍니다.',
                    '- 도구 결과에 키나 토큰처럼 보이는 값이 섞이면, 모델에게 보내기 전에 가립니다. 계정 ID도 끝 네 자리만 보입니다.',
                    '',
                    '어떤 키가 있고 언제 마지막으로 쓰였는지는 관리자가 물어볼 수 있습니다.',
                ),
            ),
    },
    {
        // 넓히는 변경: 공개, 포트 개방, 권한 추가, 차단 해제
        match: /(공개|퍼블릭)\s*(으로|로)\s*(열|바꿔|해|돌려)|퍼블릭\s*액세스\s*차단\s*(을\s*)?(꺼|끄|해제|풀)|(포트|보안\s*그룹|방화벽).*(열어|허용해|풀어)|0\.0\.0\.0\/0.*(열|허용)|권한\s*(을\s*)?(늘려|추가해|붙여)|정책\s*(을\s*)?(붙여|추가해)/,
        build: () =>
            noTool(
                '보안을 느슨하게 하는 변경이다. Vigie의 변경 도구는 조이는 방향뿐이라 할 수 없다.',
                lines(
                    '**그 방향의 변경은 할 수 없습니다.** Vigie의 변경 도구는 보안을 조이는 쪽만 있습니다. 퍼블릭 액세스 차단은 켜기만 하고, 보안 그룹·IAM 정책은 바꾸지 않습니다.',
                    '',
                    '오늘 이 계정에서도 버킷을 공개로 열거나 포트를 전 세계에 연 변경이 사고로 이어졌습니다. 꼭 필요하다면 범위를 좁혀서(특정 IP, 특정 객체, 기한) 콘솔에서 직접 하시고, 끝나면 되돌려 주세요.',
                ),
            ),
    },
    {
        // 지우기·종료
        match: /(삭제|지워|지우|없애|제거|종료|폐기|해제|terminate|delete|release)\s*(해|줘|주|시켜|하자|할래|해도|하고)/i,
        build: destroyEntry,
    },
    {
        // 보안 그룹 좁히기: 조이는 방향이지만 도구가 없다. 명령을 준다
        match: /(포트|보안\s*그룹|SSH|22\s*번|11211|인바운드).*(닫아|막아|좁혀|제한해|차단해|닫자|막자)/i,
        build: ({ admin }) =>
            admin
                ? noTool(
                      '보안 그룹 규칙을 좁혀 달라는 요청이다. 보안 그룹을 바꾸는 도구는 없다. 직접 실행할 명령을 준다.',
                      lines(
                          '좋은 방향이지만, **Vigie에는 보안 그룹을 바꾸는 도구가 없습니다.** 직접 실행할 명령입니다 (웹 서버 그룹의 SSH).',
                          '',
                          '```bash',
                          'aws ec2 revoke-security-group-ingress --group-id sg-0849cdeb5ef571996 \\',
                          '  --ip-permissions IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=0.0.0.0/0}]',
                          'aws ec2 authorize-security-group-ingress --group-id sg-0849cdeb5ef571996 \\',
                          '  --protocol tcp --port 22 --cidr <사무실 IP>/32',
                          '```',
                          '',
                          '- 첫 줄이 전 세계 개방을 지우고, 둘째 줄이 사무실 IP만 엽니다. 순서를 바꾸면 잠시 접속이 끊길 수 있으니 SSH로 붙어 있는 작업이 없는지 먼저 확인하세요.',
                          '- SSH를 아예 닫고 Session Manager로 접속하는 방법도 있습니다.',
                      ),
                  )
                : adminTaskRefusal('보안 그룹을 좁혀 달라는 요청이다. 도구가 없고, 보안 그룹 정보는 관리자 전용이다.'),
    },
    {
        // 범위 밖: AWS 운영과 관계없는 부탁
        match: /날씨|주식|코인|맛집|점심|저녁\s*메뉴|노래|영화|번역해|시\s*(를\s*)?써|소설|농담|운세|레시피/,
        build: () =>
            noTool(
                'AWS 운영과 관계없는 질문이다. 정중히 범위를 알린다.',
                '저는 AWS 운영을 돕는 Vigie라 그 질문에는 답하지 않습니다. 이 계정의 보안, 알람과 오류, 비용, 리소스 상태라면 무엇이든 물어봐 주세요.',
            ),
    },
    {
        // 데모 범위 밖: 다른 리전·계정·클라우드
        match: /다른\s*(리전|계정|어카운트)|(us|eu|ap|sa|ca|me|af)-(east|west|north|south|central|northeast|southeast)-\d|서울\s*리전|도쿄\s*리전|Azure|GCP|구글\s*클라우드/i,
        build: () =>
            noTool(
                '데모 데이터 밖의 리전·계정을 묻는다. 데모는 한 계정의 한 리전 기록뿐이다.',
                lines(
                    `이 데모는 Frothly 계정 한 곳의 \`${FROTHLY.region}\` 기록만 담고 있어서, 다른 리전이나 계정은 볼 수 없습니다.`,
                    '',
                    '실제 Vigie는 설치한 계정을 조회합니다. 콘솔 로그인처럼 전역 서비스의 기록은 리전과 관계없이 보입니다(이 데모의 로그인 기록이 `us-east-1`로 찍힌 까닭입니다).',
                ),
            ),
    },
    {
        // 고맙다·알겠다
        match: /^\s*(고마워|고맙습니다|감사(합니다|해요)?|땡큐|ㄳ|ㄱㅅ|thanks|thank\s*you|알겠어|알겠습니다|오케이|ok|좋아)\s*[.!~^ㅎ]*\s*$/i,
        build: () => noTool('고맙다는 말이다. 짧게 답한다.', '천만에요. 더 궁금한 것이 있으면 언제든 물어봐 주세요.'),
    },
];

// ---------------------------------------------------------------- 답변
const DEMO_ANSWERS: DemoAnswer[] = [
    {
        // 보안 감사: 루트 로그인
        id: 'login',
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
        // 사고 경위: 사람이 한 일을 시간 순으로
        id: 'timeline',
        match: /타임라인|경위|무슨\s*일이|순서대로|시간\s*순|사건\s*흐름|사고\s*흐름|어떻게\s*된\s*(일|거)/,
        adminOnly: true,
        build: timelineEntry,
    },
    {
        // 사고 보고서 초안
        id: 'postmortem',
        match: /포스트모템|회고|사후\s*분석|재발\s*방지|(사고|장애)\s*보고서/,
        adminOnly: true,
        build: postmortemEntry,
    },
    {
        // 대응 계획. 일반 사용자에게는 관리자 전용 내용 대신 지금 권한으로 손볼 것을 보인다
        id: 'remediation',
        match: /조치\s*계획|대응\s*(계획|방안)|(대응|조치)해야|어떻게\s*(대응|조치|막아야)|해야\s*할\s*일|뭐부터|무엇부터|우선\s*순위/,
        build: (ctx) => (ctx.admin ? remediationEntry(ctx) : clarify(ctx)),
    },
    {
        // 누가 했나
        id: 'who',
        match: /누가\s*(했|바꿨|열었|종료|껐|그랬)|범인/,
        adminOnly: true,
        build: whoEntry,
    },
    {
        // 공개였던 버킷의 파일이 바뀌었나
        id: 's3Objects',
        match: /(버킷|S3).*(파일|객체).*(바뀌|변조|올라|확인)|변조/i,
        adminOnly: true,
        build: s3ObjectsEntry,
    },
    {
        // 보안 감사: 최근 보안 이벤트 (심각도 순)
        id: 'security',
        match: /보안\s*이벤트|보안\s*사고|심각도|위협|침해|이상\s*징후|의심스러운|수상한|이상한\s*(활동|기록)/,
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
                    `| 🟠 높음 | \`web_admin\` 액세스 키로 권한 탐색 (거부 ${probes.length}건, IP ${unique(probes.map((e) => e.ip)).length}곳) | IAM·S3·EC2 | ${kstClock(epochOf(probes[0].at))}~${kstClock(epochOf(probes[probes.length - 1].at))} | ${disabled ? `${kstClock(epochOf(disabled.at))}에 ${disabled.actor} 계정이 키 비활성화` : '키 활성'} |`,
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
                    '',
                    nextHint('첫 번째 거 자세히', '누가 했어?', '사고 경위를 시간 순으로'),
                ),
            };
        },
    },
    {
        // 권한 관리: IAM 권한이 바뀐 사용자
        id: 'iam',
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
        id: 'leastPrivilege',
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
        // 사용자 한 명이 한 일
        id: 'actor',
        match: /bstoll|web_admin|btun|splunk_access|mkraeus|frothly_admin/i,
        adminOnly: true,
        build: ({ text }) => actorEntry(actorIn(text) ?? 'bstoll'),
    },
    {
        // 탄력적 IP (IP 질문보다 먼저)
        id: 'eip',
        match: /탄력적\s*IP|EIP|Elastic\s*IP/i,
        build: eipEntry,
    },
    {
        // 출발 IP
        id: 'ip',
        match: /\b\d{1,3}(\.\d{1,3}){3}\b|출발\s*IP|접속\s*IP|IP\s*(주소|목록)|IP\s*별|아이피|어디서\s*(접속|로그인|들어)/,
        adminOnly: true,
        build: ipEntry,
    },
    {
        // 네트워크: 전 세계에 열린 보안 그룹
        id: 'network',
        match: /보안\s*그룹|포트|SSH|11211|memcached|인바운드|방화벽|security\s*group/i,
        adminOnly: true,
        build: networkEntry,
    },
    {
        // 리소스 모니터링: EC2 CPU
        id: 'cpu',
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
                    '',
                    nextHint('왜 자꾸 교체돼?', '차트로 그려 줘', `${idle.name} 멈춰 줘`),
                ),
            };
        },
    },
    {
        // Auto Scaling 교체 (CPU 답보다 뒤: "CPU"가 들어 있으면 CPU 답)
        id: 'asg',
        match: /오토\s*스케일링|Auto\s*Scaling|교체|새로\s*(뜨|떴|띄)|인스턴스.*(계속|자꾸)/i,
        build: asgEntry,
    },
    {
        // 웹 서비스 상태 (로드 밸런서)
        id: 'health',
        match: /웹\s*(서비스|사이트)|헬스|정상\s*호스트|ALB|로드\s*밸런서|트래픽|요청\s*수|접속자|서비스\s*(괜찮|정상)|다운됐|죽었/i,
        build: healthEntry,
    },
    {
        // RDS
        id: 'rds',
        match: /RDS|데이터베이스|\bDB\b|디비/i,
        build: rdsEntry,
    },
    {
        // 리소스 모니터링: Lambda 메모리
        id: 'memory',
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
        // Lambda 오류의 원인 (오류 답보다 먼저: 원인을 물으면 로그를 본다)
        id: 'errorLogs',
        match: /(오류|에러)\s*(로그|원인|메시지)|왜\s*(실패|오류|에러)|실패\s*원인/,
        build: errorLogsEntry,
    },
    {
        // 비용 관리: 쓰지 않는 리소스. 비용 답보다 먼저 본다 (예시 질문 "비용 최적화를 위해 삭제 가능한 미사용 리소스가
        // 있나요?"에 '비용'이 들어 있어, 뒤에 두면 비용 증가 답을 받았다)
        id: 'waste',
        match: /미사용|안\s*쓰는|쓰지\s*않는|삭제\s*가능|낭비|유휴/,
        build: ({ state }) => {
            const idle = idleInstance();
            const stopped = state.forensicStopped;
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
                    stopped
                        ? `정리할 수 있는 리소스가 **1개** 있습니다. 치우면 한 달에 약 ${usd(eipMonthly)}를 아낍니다.`
                        : `정리할 수 있는 리소스가 **2개** 있습니다. 모두 치우면 한 달에 약 ${usd(eipMonthly + idleMonthly)}를 아낍니다.`,
                    '',
                    '| 리소스 | 상태 | 월 비용 (예상) |',
                    '|:--|:--|--:|',
                    eip ? `| 탄력적 IP \`${eip.ip}\` | 어느 인스턴스에도 연결되지 않음 | ${usd(eipMonthly)} |` : undefined,
                    !stopped &&
                        `| ${idle.name} (\`${idle.id}\`, ${idle.type}) | CPU 평균 ${metricAverage('forensicCpu')}% · 조사용으로 띄운 뒤 켜져 있음 | ${usd(idleMonthly)} |`,
                    '',
                    '- 연결되지 않은 EBS 볼륨은 없습니다 (종료된 웹 서버의 볼륨은 함께 지워지게 설정되어 있습니다).',
                    stopped
                        ? `- \`${idle.name}\`는 대화에서 승인해 중지했습니다 (EBS 볼륨 요금은 계속 나갑니다).`
                        : `- 인스턴스는 조사가 끝났는지 확인한 뒤 멈추세요. "${idle.name} 멈춰 줘"라고 하면 승인 요청을 만듭니다.`,
                ),
            };
        },
    },
    {
        // 월말 예상 비용
        id: 'forecast',
        match: /(예상|예측|전망|월말|말까지).*(비용|요금|청구|얼마)|(비용|요금|청구).*(예상|예측|전망|월말)/,
        build: forecastEntry,
    },
    {
        // 서비스별 비용 전체
        id: 'costBreakdown',
        match: /서비스별\s*(비용|요금)|(비용|요금)\s*(내역|구성|비중)/,
        build: costBreakdownEntry,
    },
    {
        // 비용 관리
        id: 'cost',
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
                    '',
                    nextHint('월말 예상은?', '어떻게 줄여?', '차트로 그려 줘'),
                ),
            };
        },
    },
    {
        // 알람
        id: 'alarms',
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
                    '',
                    nextHint('왜 울렸어?', '알람 꺼 줘'),
                ),
            };
        },
    },
    {
        // AWS Config 규정 준수 (도구가 없다. 알람 답보다 뒤: "CIS 알람"은 알람 답)
        id: 'config',
        match: /Config|규정|컴플라이언스|준수|CIS\s*(벤치마크|점검)|규칙\s*위반/i,
        build: configEntry,
    },
    {
        // 오류
        id: 'errors',
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
                    '',
                    nextHint('원인은?', '차트로 그려 줘'),
                ),
            };
        },
    },
    {
        // S3 공개 버킷
        id: 's3',
        match: /버킷|S3|s3|공개/,
        adminOnly: true,
        build: ({ state }) => {
            const acl = publicAcl()!;
            const restored = aclRestored();
            const blocked = state.webcodeBlocked;
            return {
                search: { query: 'listS3Buckets|checkS3BucketSecurity', found: ['listS3Buckets', 'checkS3BucketSecurity'] },
                tools: [ok('listS3Buckets', {}), ok('checkS3BucketSecurity', {})],
                thinking: [
                    '버킷 목록을 보고, 버킷마다 퍼블릭 액세스 차단·정책·ACL을 점검한다.',
                    'frothlywebcode가 오늘 공개 ACL이 걸렸다 풀렸고, 퍼블릭 액세스 차단은 여전히 꺼져 있다.',
                ],
                answer: lines(
                    `지금 공개된 버킷은 없습니다. 하지만 **\`frothlywebcode\`는 오늘 ${restored ? minutesBetween(acl.at, restored.at) : '?'}분 동안 누구나 읽고 쓸 수 있었습니다.** ${blocked ? '지금은 퍼블릭 액세스 차단이 켜져 있습니다.' : '퍼블릭 액세스 차단은 아직 꺼져 있습니다.'}`,
                    '',
                    `- ${when(acl.at)}: ${acl.actor} 계정이 ACL로 모든 사용자에게 ${acl.publicPermissions?.join('·')} 권한을 줌`,
                    restored ? `- ${when(restored.at)}: ${restored.actor} 계정이 공개 권한을 뺌` : undefined,
                    '- 암호화·버전 관리·SSL 강제도 꺼져 있습니다 (AWS Config 규칙 위반).',
                    '',
                    blocked
                        ? '퍼블릭 액세스 차단을 대화에서 승인해 켰으므로, ACL이나 버킷 정책으로 다시 공개할 수 없습니다.'
                        : '다시 공개되지 않게 퍼블릭 액세스 차단을 켜 두는 것을 권합니다. "frothlywebcode 퍼블릭 액세스 차단 켜 줘"라고 하면 승인 요청을 만듭니다.',
                ),
            };
        },
    },
    {
        // 오늘 현황 한눈에
        id: 'briefing',
        match: /요약|브리핑|현황|한눈에|전반|상태\s*(어때|알려)|오늘\s*(어때|상황|어땠)|별일|이상\s*없/,
        build: briefingEntry,
    },
    {
        // 인사·소개
        id: 'greeting',
        match: /^\s*(안녕|하이|hi|hello)|누구|소개|뭘\s*할\s*수|무엇을\s*할\s*수/i,
        build: () => ({
            tools: [],
            thinking: ['인사다. 도구 없이 무엇을 할 수 있는지 소개한다.'],
            answer: lines(
                '안녕하세요, AWS 클라우드 운영을 돕는 **Vigie(비지)**입니다.',
                '',
                '지금은 **데모**입니다. 가상 회사 Frothly의 실제 AWS 운영 기록(공개 데이터셋)으로 답합니다. 이런 것을 물어보세요.',
                '',
                '- 오늘 현황 요약해 줘',
                '- 최근 보안 이벤트를 심각도 순으로 정리해 줘',
                '- CPU 사용률이 가장 높은 EC2 인스턴스는?',
                '- 이번 달 비용이 가장 많이 늘어난 서비스는?',
                '',
                '답을 받은 뒤에는 "자세히", "두 번째 거", "누가 했어?", "어떻게 막아?", "차트로 그려 줘"처럼 이어서 물어도 됩니다. AWS를 바꾸는 일은 승인 요청을 만들고, 제게 없는 도구가 필요한 일은 직접 할 방법을 알려 드립니다.',
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
        '- 한눈에: "오늘 현황 요약해 줘"',
        '- 보안 (관리자): "최근 보안 이벤트를 심각도 순으로", "사고 경위를 시간 순으로", "누가 했어?", "전 세계에 열린 보안 그룹"',
        '- 운영: "CPU가 가장 높은 EC2", "웹 서비스 괜찮아?", "Lambda 오류 원인은?", "지금 울리는 알람"',
        '- 비용: "비용이 가장 많이 늘어난 서비스", "월말 예상 비용", "쓰지 않는 리소스"',
        '- 변경 (승인 뒤 실행): "Bud\'s Forensic AMI 멈춰 줘", "frothlywebcode 퍼블릭 액세스 차단 켜 줘"',
        '',
        '답을 받은 뒤 "자세히", "왜?", "두 번째 거", "어떻게 막아?"처럼 이어서 물어도 됩니다.',
    ),
});

// 일반 사용자가 관리자 전용 도구가 필요한 것을 물었을 때 (services/llm/tool_access.py의 안내와 같은 답)
const adminOnlyRefusal = (): DemoEntry => ({
    tools: [],
    thinking: ['CloudTrail·IAM·네트워크·S3 보안 점검이 필요한 질문이다. 이 사용자는 관리자가 아니라 그 도구를 쓸 수 없다.'],
    answer:
        '이 내용은 **관리자(admins 그룹)만 볼 수 있습니다.** CloudTrail·IAM·네트워크·S3 보안 점검 조회는 관리자 전용이라, 필요하면 관리자에게 요청해 주세요.\n\n로그·지표·비용·EC2 상태는 지금 권한으로도 물어볼 수 있습니다.',
});

// 되묻기: 무엇을 말하는지 모를 때 ("고쳐 줘", "어떻게 해?"). 지금 손볼 것을 보여 고르게 한다
const clarify = ({ admin, state }: DemoContext): DemoEntry => {
    const options = [
        admin && !state.webcodeBlocked && `\`${PUBLIC_BUCKET}\` 버킷이 다시 공개되지 않게 막기 → "퍼블릭 액세스 차단 켜 줘"`,
        admin && '샌 것으로 보이는 `web_admin` 키 정리 → "web_admin 키 어떻게 해?"',
        !state.forensicStopped && `거의 쓰지 않는 \`${idleInstance().name}\` 멈추기 → "${idleInstance().name} 멈춰 줘"`,
        '로그 수집 Lambda 오류 → "Lambda 오류 원인은?"',
        '울리는 보안 알람 2개 → "지금 울리는 알람 알려줘"',
    ].filter(Boolean);
    return {
        tools: [],
        thinking: ['무엇을 가리키는지 알 수 없다. 짐작해서 바꾸지 않고, 지금 손볼 만한 것을 보여 고르게 한다.'],
        answer: lines(
            '무엇을 말씀하시는지 확실하지 않아서, 짐작으로 바꾸지 않겠습니다. 지금 이 계정에서 손볼 만한 것은 이렇습니다.',
            '',
            ...options.map((option, index) => `${index + 1}. ${option}`),
            '',
            '어느 것인지 알려 주세요.',
        ),
    };
};

// ---------------------------------------------------------------- 이어 묻기
// 앞 주제에 딸린 물음. to: 그 주제의 답을 그대로, build: 이 자리에서만 쓰는 답 (topic: 그 뒤의 주제)
interface FollowUp {
    match: RegExp;
    to?: Topic;
    build?: (ctx: DemoContext) => DemoEntry;
    topic?: Topic;
    adminOnly?: boolean;
}

// "두 번째", "2번" (11211의 "1번"은 잡지 않게 앞이 숫자가 아닐 때만)
const nth = (n: number, word: string) => new RegExp(`${word}\\s*(번째|번\\s*째)|(^|[^0-9])${n}\\s*번`);
const WHO = /누가|누구|범인|사람이/;
const REMEDY = /어떻게\s*(해|하면|막|대응|처리|고쳐|조치)|조치|대응|막으려면|막아야|해결|고쳐|처리해|뭘\s*해야|무엇을\s*해야|할\s*일/;
const REPORT = /보고서|포스트모템|회고|문서로|보고용/;
const SAVE = /줄여|줄이|줄일|절약|아끼|아낄|낮추|낮출|최적화/;
const actorTo = (name: string): FollowUp['build'] => () => actorEntry(name);

const FOLLOW_UPS: Partial<Record<Topic, FollowUp[]>> = {
    security: [
        { match: /memcached|11211/i, to: 'network' },
        { match: nth(1, '첫'), to: 's3' },
        { match: nth(2, '두'), to: 'network' },
        { match: nth(3, '세'), build: actorTo('web_admin'), topic: 'actor', adminOnly: true },
        { match: nth(4, '네'), to: 'login' },
        { match: nth(5, '다섯'), to: 'network' },
        { match: nth(6, '여섯'), build: actorTo('bstoll'), topic: 'actor', adminOnly: true },
        { match: WHO, to: 'who' },
        { match: REPORT, to: 'postmortem' },
        { match: REMEDY, to: 'remediation' },
    ],
    timeline: [
        { match: WHO, to: 'who' },
        { match: REPORT, to: 'postmortem' },
        { match: REMEDY, to: 'remediation' },
    ],
    who: [
        { match: REMEDY, to: 'remediation' },
        { match: /IP|어디서/, to: 'ip' },
    ],
    remediation: [{ match: REPORT, to: 'postmortem' }],
    s3: [
        { match: /파일|객체|내용|바뀌|변조|올라/, to: 's3Objects' },
        { match: WHO, to: 'who' },
        { match: REMEDY, to: 'remediation' },
    ],
    network: [
        { match: WHO, to: 'who' },
        { match: REMEDY, to: 'remediation' },
    ],
    login: [
        { match: /IP|어디서/, to: 'ip' },
        { match: WHO, build: actorTo('bstoll'), topic: 'actor', adminOnly: true },
        { match: REMEDY, to: 'remediation' },
    ],
    iam: [
        { match: /키|누구|web_admin/, build: actorTo('web_admin'), topic: 'actor', adminOnly: true },
        { match: REMEDY, to: 'remediation' },
    ],
    actor: [
        { match: /IP|어디서/, to: 'ip' },
        { match: /권한|정책/, to: 'leastPrivilege' },
        { match: WHO, to: 'who' },
        { match: REMEDY, to: 'remediation' },
    ],
    cpu: [
        { match: /교체|오토|Auto|바뀌|새로\s*떠|왜/i, to: 'asg' },
        { match: /웹\s*서비스|ALB|트래픽|요청/, to: 'health' },
    ],
    asg: [{ match: WHO, build: actorTo('bstoll'), topic: 'actor', adminOnly: true }],
    health: [
        { match: /왜|원인|교체/, to: 'asg' },
        { match: /CPU/i, to: 'cpu' },
    ],
    cost: [
        { match: /예상|예측|전망|월말|말까지/, to: 'forecast' },
        { match: SAVE, to: 'waste' },
        { match: /서비스별|전체|내역|나머지|Datadog|마켓/i, to: 'costBreakdown' },
    ],
    forecast: [
        { match: SAVE, to: 'waste' },
        { match: /서비스별|내역|어디에/, to: 'costBreakdown' },
    ],
    costBreakdown: [{ match: SAVE, to: 'waste' }],
    waste: [
        { match: /EIP|탄력적|IP/i, to: 'eip' },
        { match: /얼마|예상|월말/, to: 'forecast' },
    ],
    alarms: [{ match: /왜|원인|누가|무엇\s*때문|무슨\s*일/, to: 'timeline' }],
    errors: [{ match: /원인|왜|로그|어디서|자세히/, to: 'errorLogs' }],
    rds: [{ match: /Lambda|감사\s*로그|오류/i, to: 'errorLogs' }],
    briefing: [
        { match: nth(1, '첫'), to: 'security' },
        { match: /보안/, to: 'security' },
        { match: /알람/, to: 'alarms' },
        { match: /오류/, to: 'errors' },
        { match: /웹/, to: 'health' },
        { match: /비용/, to: 'cost' },
    ],
};

// 주제 낱말 없이 더 묻는 말 ("자세히", "왜?", "그래서?")
const MORE_CUE = /자세히|자세하게|더\s*(알려|보여|자세)|구체적|왜|이유|원인|그래서|그럼|계속|다음은|그\s*다음/;
const MORE: Partial<Record<Topic, (ctx: DemoContext) => Topic>> = {
    security: () => 'timeline',
    timeline: () => 'who',
    who: () => 'remediation',
    remediation: () => 'postmortem',
    s3: () => 's3Objects',
    network: () => 'remediation',
    login: () => 'ip',
    iam: () => 'leastPrivilege',
    leastPrivilege: () => 'remediation',
    actor: () => 'timeline',
    ip: () => 'timeline',
    cpu: () => 'asg',
    asg: () => 'health',
    health: () => 'asg',
    rds: () => 'errorLogs',
    memory: () => 'errorLogs',
    errors: () => 'errorLogs',
    waste: () => 'forecast',
    forecast: () => 'costBreakdown',
    cost: () => 'costBreakdown',
    alarms: ({ admin }) => (admin ? 'timeline' : 'errors'),
    config: ({ admin }) => (admin ? 's3' : 'briefing'),
    briefing: ({ admin }) => (admin ? 'security' : 'errors'),
    postmortem: () => 'remediation',
    s3Objects: () => 'remediation',
    costBreakdown: () => 'waste',
    errorLogs: () => 'rds',
    eip: () => 'waste',
    greeting: () => 'briefing',
};

// 무엇을 가리키는지 모를 부탁 (앞 주제가 없을 때 되묻는다)
const VAGUE = /^\s*(그거|이거|저거)?\s*(좀\s*)?(고쳐|해결|처리|정리|조치|바꿔|해)\s*(줘|주세요|줄래|봐)?\s*[.!?]*\s*$|어떻게\s*해야\s*(해|돼|하지)|뭐부터|뭘\s*해야/;

const answerOf = (id: Topic) => DEMO_ANSWERS.find((answer) => answer.id === id)!;

interface Resolved {
    topic?: Topic; // 이 질문 뒤의 주제 (가드·안내는 앞 주제를 그대로 둔다)
    entry: (ctx: DemoContext) => DemoEntry;
}

// 관리자 전용이면 일반 사용자에게는 거절
const guarded = (adminOnly: boolean | undefined, build: (ctx: DemoContext) => DemoEntry) => (ctx: DemoContext) =>
    adminOnly && !ctx.admin ? adminOnlyRefusal() : build(ctx);
const toTopic = (id: Topic): Resolved => {
    const answer = answerOf(id);
    return { topic: id, entry: guarded(answer.adminOnly, answer.build) };
};

// 질문 하나를 어느 답으로 보낼지 (previous: 앞 주제). 그림 없이 규칙만: 이전 질문들을 다시 따라갈 때도 같은 함수를 쓴다
const resolve = (text: string, previous: Topic | undefined, admin: boolean): Resolved => {
    const guard = GUARDS.find((g) => g.match.test(text));
    if (guard) return { topic: previous, entry: guard.build };
    if (previous) {
        const follow = FOLLOW_UPS[previous]?.find((f) => f.match.test(text));
        if (follow?.to) return toTopic(follow.to);
        if (follow?.build) return { topic: follow.topic ?? previous, entry: guarded(follow.adminOnly, follow.build) };
    }
    const found = DEMO_ANSWERS.find((answer) => answer.match.test(text));
    if (found) return toTopic(found.id);
    const more = previous && MORE[previous];
    if (more && MORE_CUE.test(text)) return toTopic(more({ admin, state: INITIAL_DEMO_STATE, text }));
    if (VAGUE.test(text)) return { topic: previous, entry: clarify };
    return { topic: previous, entry: fallback };
};

// 이 대화의 앞 질문들(오래된 것부터)을 따라가 마지막 주제를 구한다
export const demoTopicOf = (history: string[], admin: boolean): Topic | undefined =>
    history.reduce<Topic | undefined>((topic, text) => resolve(text, topic, admin).topic, undefined);

// 질문에 맞는 데모 답 (admin: 요청자가 관리자인가, state: 대화에서 승인해 바꾼 것, history: 이 대화의 앞 질문들)
export const demoEntryFor = (text: string, admin: boolean, state: DemoState = INITIAL_DEMO_STATE, history: string[] = []): DemoEntry =>
    resolve(text, demoTopicOf(history, admin), admin).entry({ state, admin, text });

// 가드에 걸리는 질문이면 그 답 (mock/api.ts가 변경 요청을 고르기 전에 본다: "승인 없이 멈춰"는 승인 요청이 아니라 거절)
export const demoGuardFor = (text: string, admin: boolean, state: DemoState = INITIAL_DEMO_STATE): DemoEntry | undefined =>
    GUARDS.find((g) => g.match.test(text))?.build({ state, admin, text });

// 무엇을 바꿀지 모를 때 되묻는 답 (mock/api.ts: 대상 없는 "멈춰 줘")
export const demoClarify = (admin: boolean, state: DemoState): DemoEntry => clarify({ admin, state, text: '' });

// 처음 열었을 때 보이는 예시 대화의 질문. 일반 사용자에게는 관리자 전용 도구가 필요 없는 질문
export const demoFirstQuestion = (admin: boolean) =>
    admin ? '최근 보안 이벤트를 심각도 순으로 정리해줘' : 'CPU 사용률이 가장 높은 EC2 인스턴스는 무엇인가요?';

// ---------------------------------------------------------------- 차트 ("그려 줘", "차트로")
// 질문의 주제, 없으면 앞 주제에 맞는 차트. 지표는 5분마다의 점이라 시각을 x축으로 쓴다
const chart = (name: string, type: string, options: Record<string, unknown>): Artifact => ({
    ref: `artifact://charts/demo/${name}.png`,
    url: `https://example.invalid/charts/demo/${name}.png`, // 목업이라 열리지 않는다 (브라우저가 spec으로 다시 그린다)
    kind: 'chart',
    spec: { type, options } as Artifact['spec'],
});
const series = (key: string, group: string, pick: 'average' | 'sum' = 'average') =>
    metric(key)[pick].map((value, index) => ({ time: slotClock(key, index), value: round(value, 2), group }));

const CHARTS: { topics: Topic[]; build: () => DemoEntry }[] = [
    {
        topics: ['cpu', 'asg'],
        build: () => ({
            tools: [ok('getEc2CpuRanking', { hours: 3 }), ok('generateLineChart', { title: 'EC2 CPU 사용률 (%)' })],
            thinking: ['인스턴스별 CPU를 5분 단위로 가져와 선 차트로 그린다.'],
            answer: lines(
                '최근 3시간 EC2 CPU 사용률입니다 (5분 평균).',
                '',
                '![EC2 CPU 사용률](artifact://charts/demo/cpu.png)',
                '',
                `둘 다 한 자릿수에 머뭅니다. WebServers의 튀는 점은 인스턴스가 새로 뜨며 부팅하던 때입니다.`,
            ),
            artifacts: [
                chart('cpu', 'line', {
                    title: 'EC2 CPU 사용률 (%, 5분 평균)',
                    axisXTitle: '한국 시각',
                    axisYTitle: '%',
                    data: [...series('forensicCpu', idleInstance().name ?? FORENSIC_INSTANCE), ...series('asgCpu', 'WebServers')],
                }),
            ],
        }),
    },
    {
        topics: ['errors', 'errorLogs', 'rds', 'memory'],
        build: () => ({
            tools: [
                ok('get_metric_data', { namespace: 'AWS/Lambda', metric_name: 'Errors', statistic: 'Sum', group_by: 'FunctionName' }),
                ok('generateLineChart', { title: 'Lambda 오류 수' }),
            ],
            thinking: ['함수별 오류 수를 5분 단위로 가져와 선 차트로 그린다.'],
            answer: lines(
                '최근 3시간 Lambda 오류 수입니다 (5분마다).',
                '',
                '![Lambda 오류 수](artifact://charts/demo/errors.png)',
                '',
                `두 함수가 ${kstClock(epochOf(peakOffset(LAMBDA_ERROR_KEYS.RDSAuditLogs)))} 전후에 함께 실패했습니다.`,
            ),
            artifacts: [
                chart('errors', 'line', {
                    title: 'Lambda 오류 수 (5분마다)',
                    axisXTitle: '한국 시각',
                    axisYTitle: '오류',
                    data: [...series(LAMBDA_ERROR_KEYS.RDSAuditLogs, 'RDSAuditLogs', 'sum'), ...series(LAMBDA_ERROR_KEYS.VPCFlowLogs, 'VPCFlowLogs', 'sum')],
                }),
            ],
        }),
    },
    {
        topics: ['health'],
        build: () => ({
            tools: [
                ok('get_metric_data', { namespace: 'AWS/ApplicationELB', metric_name: 'RequestCount', statistic: 'Sum', period: 300 }),
                ok('get_metric_data', { namespace: 'AWS/ApplicationELB', metric_name: 'HealthyHostCount', statistic: 'Average', period: 300 }),
                ok('generateLineChart', { title: '요청 수' }),
                ok('generateLineChart', { title: '정상 호스트' }),
            ],
            thinking: ['요청 수와 정상 호스트는 단위가 달라 한 차트에 겹치지 않고 두 차트로 나눠 그린다.'],
            answer: lines(
                '최근 3시간 로드 밸런서입니다. 단위가 달라 두 차트로 나눴습니다.',
                '',
                '![요청 수](artifact://charts/demo/requests.png)',
                '',
                '![정상 호스트](artifact://charts/demo/healthy.png)',
                '',
                `정상 호스트가 ${lowestHealthy().at}에 ${lowestHealthy().value}대까지 떨어졌습니다.`,
            ),
            artifacts: [
                chart('requests', 'line', { title: '요청 수 (5분 합)', axisXTitle: '한국 시각', axisYTitle: '요청', data: series('albRequests', '요청', 'sum') }),
                chart('healthy', 'line', { title: '정상 호스트 (5분 평균)', axisXTitle: '한국 시각', axisYTitle: '대', data: series('healthyHosts', '정상 호스트') }),
            ],
        }),
    },
    {
        topics: ['cost', 'forecast', 'costBreakdown', 'waste', 'briefing'],
        build: () => {
            const cost = demoCost();
            return {
                tools: [
                    ok('cost-explorer', { operation: 'getCostAndUsage', granularity: 'DAILY', group_by: 'SERVICE' }),
                    ok('generateBarChart', { title: '서비스별 비용' }),
                    ok('generateLineChart', { title: '일별 비용' }),
                ],
                thinking: ['이번 달 비용을 서비스별·일별로 가져와 막대와 선 차트로 그린다.'],
                answer: lines(
                    `이번 달 비용입니다 (지금까지 ${usd(cost.monthToDate)}).`,
                    '',
                    '![서비스별 비용](artifact://charts/demo/cost-by-service.png)',
                    '',
                    '![일별 비용](artifact://charts/demo/cost-daily.png)',
                    '',
                    'EC2가 가장 크고, 주말에는 조금 낮습니다.',
                ),
                artifacts: [
                    chart('cost-by-service', 'bar', {
                        title: '서비스별 비용 (USD, 이번 달)',
                        data: cost.byService.map((s) => ({ category: s.service, value: s.amount })),
                    }),
                    chart('cost-daily', 'line', {
                        title: '일별 비용 (USD)',
                        axisXTitle: '날짜',
                        axisYTitle: 'USD',
                        data: cost.daily.map((d) => ({ time: d.date.slice(5), value: d.amount, group: '비용' })),
                    }),
                ],
            };
        },
    },
];

// 차트 요청에 맞는 데모 차트 (질문의 주제 → 앞 주제 → 없으면 오류·비용 개요)
export const demoChartFor = (text: string, admin: boolean, history: string[] = []): DemoEntry => {
    const topic = DEMO_ANSWERS.find((answer) => answer.match.test(text))?.id ?? demoTopicOf(history, admin);
    const found = CHARTS.find((c) => topic && c.topics.includes(topic));
    if (found) return found.build();
    const errors = CHARTS[1].build();
    const cost = CHARTS[3].build();
    return {
        tools: [...errors.tools, ...cost.tools],
        thinking: ['무엇을 그릴지 정해지지 않았다. 운영에서 가장 자주 보는 오류와 비용을 그린다.'],
        answer: lines(errors.answer, '', cost.answer),
        artifacts: [...(errors.artifacts ?? []), ...(cost.artifacts ?? [])],
    };
};
