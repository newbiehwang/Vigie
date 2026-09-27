// 홈 대시보드: 운영자가 홈에서 5초 안에 알고 싶은 것을 급한 차례로 보인다.
//   운영 현황                                   dev · ap-northeast-2 · 3분 전에 모음  [↻]
//   [ 무엇이든 물어보세요…                                                    ➤ ]
//   ┌ 지금 확인할 것 ───────────────────────────────────────────────────────┐   ← 없으면 초록 한 줄
//   │ ● wga-dev-api-5xx 알람이 12분째 울리는 중  5XXError > 5 (5분)   물어보기 ↗ │
//   │ ● 승인을 기다리는 변경 1건  9분 뒤 만료                        대화에서 보기 │
//   └────────────────────────────────────────────────────────────────────┘
//   [울리는 알람 1/24] [Lambda 오류 37 ▁▂▅] [이번 달 비용 $362 +8%] [승인 대기 1]   ← 숫자 카드 (누르면 대화로)
//   [일별 비용 (막대 · 평균 점선 · 남은 날 점선 칸)      ] [서비스별 비용 (가로 막대)]
//   [리소스 상태 (분포 막대 + 정렬되는 표)              ] [최근 변경 (시간 줄)     ]
//   [치울 것 (카드 여러 개, 누르면 대화로)                                          ]
// - 카드·줄을 누르면 그 내용을 질문으로 새 대화를 시작한다 (onAsk). 대시보드가 문제를 보이고, 원인은 대화가 찾는다
// - 값은 서버가 모아 둔 것이라 '몇 분 전에 모음'을 함께 적는다. ↻는 모아 둔 값을 다시 읽을 뿐 AWS를 부르지 않는다
// - 서버 집계(GET /dashboard)는 아직 없다. mock에서만 값이 나오고, 실제 서버에서는 불러오지 못했다는 안내가 보인다
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getDashboard } from '@/api/dashboard';
import { LoadingCard, useMinimumVisible } from '@/components/LoadingCard';
import { RefreshButton } from '@/components/RefreshButton';
import { useToast } from '@/components/Toast';
import { Composer } from '@/features/chat/Composer';
import type { DashboardData, DashboardResource, HealthStatus } from '@/types/dashboard';
import { getErrorText } from '@/utils/formatters';
import { DailyCostChart, ServiceBars, Sparkbars, STATUS_LABEL, STATUS_ORDER, StatusBar, usd } from './DashboardCharts';
import { EXAMPLE_QUESTIONS } from './examples';

// ---------------------------------------------------------------- 시간 글자
const nowSeconds = () => Math.floor(Date.now() / 1000);

// 3분 전 · 2시간 전 · 3일 전
const ago = (at: number) => {
    const s = Math.max(0, nowSeconds() - at);
    if (s < 60) return '방금';
    if (s < 3600) return `${Math.floor(s / 60)}분 전`;
    if (s < 86400) return `${Math.floor(s / 3600)}시간 전`;
    return `${Math.floor(s / 86400)}일 전`;
};

// 12분째 · 3시간째
const lasting = (since: number) => {
    const s = Math.max(0, nowSeconds() - since);
    return s < 3600 ? `${Math.max(1, Math.floor(s / 60))}분째` : `${Math.floor(s / 3600)}시간째`;
};

// 9분 뒤
const until = (at: number) => `${Math.max(1, Math.ceil((at - nowSeconds()) / 60))}분 뒤`;

const percent = (now: number, before: number) => (before ? Math.round(((now - before) / before) * 1000) / 10 : 0);

// 증감 표시: ▲ 8.1% · ▼ 3% (늘면 나쁜 값이라 늘면 빨강)
function Delta({ now, before, suffix }: { now: number; before: number; suffix: string }) {
    const change = percent(now, before);
    if (!change) return <span className="dash-delta">{suffix}와 같음</span>;
    return (
        <span className={`dash-delta ${change > 0 ? 'is-up' : 'is-down'}`}>
            {change > 0 ? '▲' : '▼'} {Math.abs(change)}% <span className="dash-delta-note">{suffix}보다</span>
        </span>
    );
}

// ---------------------------------------------------------------- 상태 배지 (색만으로 알리지 않게 글자와 모양을 함께)
function StatusBadge({ status }: { status: HealthStatus }) {
    return (
        <span className={`dash-badge is-${status}`}>
            <span className="dash-badge-dot" aria-hidden="true" />
            {STATUS_LABEL[status]}
        </span>
    );
}

// 누르면 대화로 보낸다는 표시
const AskArrow = () => (
    <svg className="dash-ask-arrow" viewBox="0 0 12 12" width="11" height="11" aria-hidden="true">
        <path d="M3.5 8.5l5-5M4.5 3.5h4v4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
);

type SortKey = 'status' | 'name' | 'errors' | 'cost';
const SORT_LABEL: Record<SortKey, string> = { status: '상태', name: '리소스', errors: '오류 (24시간)', cost: '이번 달 비용' };

export function Dashboard({ onAsk }: { onAsk: (question: string) => void }) {
    const navigate = useNavigate();
    const { show: showToast } = useToast();
    const [data, setData] = useState<DashboardData | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: 'status', desc: false });
    const showLoading = useMinimumVisible(loading && !data);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            setData(await getDashboard());
            setError(null);
        } catch (err) {
            const text = getErrorText(err);
            setError(text);
            showToast('error', '운영 현황을 불러오지 못했습니다.');
        } finally {
            setLoading(false);
        }
    }, [showToast]);

    useEffect(() => {
        load();
    }, [load]);

    const counts = useMemo(() => {
        const result: Record<HealthStatus, number> = { fail: 0, warn: 0, ok: 0, none: 0 };
        data?.resources.forEach((resource) => (result[resource.status] += 1));
        return result;
    }, [data]);

    const resources = useMemo(() => {
        if (!data) return [];
        const value = (r: DashboardResource): number | string => {
            if (sort.key === 'status') return STATUS_ORDER.indexOf(r.status);
            if (sort.key === 'name') return r.label ?? r.id;
            if (sort.key === 'errors') return r.errors24h ?? -1;
            return r.costMonth ?? -1;
        };
        return [...data.resources].sort((a, b) => {
            const x = value(a);
            const y = value(b);
            const cmp = typeof x === 'string' ? x.localeCompare(String(y), 'ko') : x - (y as number);
            return sort.desc ? -cmp : cmp;
        });
    }, [data, sort]);

    const toggleSort = (key: SortKey) =>
        // 처음 누르면: 상태·이름은 오름차순(문제 먼저·가나다), 오류·비용은 큰 것부터
        setSort((prev) => (prev.key === key ? { key, desc: !prev.desc } : { key, desc: key === 'errors' || key === 'cost' }));

    return (
        <div className="dash">
            <header className="dash-head">
                <div className="dash-title-wrap">
                    <h2 className="dash-title">운영 현황</h2>
                    {data ? (
                        <p className="dash-meta">
                            <span className="dash-env">{data.env}</span>
                            {data.region} · {ago(data.generatedAt)}에 모음
                        </p>
                    ) : null}
                </div>
                <RefreshButton onClick={load} loading={loading} />
            </header>

            <div className="dash-ask">
                <Composer
                    variant="home"
                    compact
                    // 예시 질문은 입력칸을 누르면 아래에 펼쳐진다. 자리 글은 좁은 화면에서도 한 줄로 짧게
                    placeholder="무엇이든 물어보세요"
                    onSend={onAsk}
                    suggestions={EXAMPLE_QUESTIONS}
                />
            </div>

            <div className="dash-body">
                {showLoading ? <LoadingCard text="운영 현황을 불러오는 중…" /> : null}
                {!data && !loading && error ? (
                    <div className="dash-empty">
                        <p className="dash-empty-title">운영 현황을 불러오지 못했습니다.</p>
                        <p className="dash-empty-text">{error}</p>
                        <button type="button" className="plan-reload-button" onClick={load}>
                            다시 불러오기
                        </button>
                    </div>
                ) : null}

                {data ? (
                    <>
                        <Attention data={data} onAsk={onAsk} onOpenChat={() => navigate('/chat')} />

                        {/* 숫자 카드 4개 */}
                        <div className="dash-kpis">
                            <button
                                type="button"
                                className={`dash-kpi${data.alarms.firing.length ? ' is-fail' : ''}`}
                                onClick={() =>
                                    onAsk(
                                        data.alarms.firing.length
                                            ? `${data.alarms.firing[0].name} 알람 왜 울렸어?`
                                            : '지금 CloudWatch 알람 상태 알려줘',
                                    )
                                }
                            >
                                <span className="dash-kpi-label">
                                    울리는 알람 <AskArrow />
                                </span>
                                <span className="dash-kpi-value">
                                    {data.alarms.firing.length}
                                    <span className="dash-kpi-unit"> / {data.alarms.total}개</span>
                                </span>
                                <span className="dash-kpi-sub">
                                    {data.alarms.firing.length
                                        ? `${data.alarms.firing[0].name} · ${lasting(data.alarms.firing[0].since)}`
                                        : '모든 알람이 정상입니다'}
                                </span>
                            </button>

                            <button type="button" className="dash-kpi" onClick={() => onAsk('지난 24시간 Lambda 오류 원인 분석해줘')}>
                                <span className="dash-kpi-label">
                                    Lambda 오류 · 24시간 <AskArrow />
                                </span>
                                <span className="dash-kpi-value">
                                    {data.errors.total24h}
                                    <span className="dash-kpi-unit">건</span>
                                </span>
                                <Delta now={data.errors.total24h} before={data.errors.previous24h} suffix="그 전 24시간" />
                                <Sparkbars values={data.errors.hourly} label="지난 24시간 시간별 오류 수" />
                            </button>

                            <button type="button" className="dash-kpi" onClick={() => onAsk('이번 달 비용이 왜 늘었어? 서비스별로 알려줘')}>
                                <span className="dash-kpi-label">
                                    이번 달 비용 <AskArrow />
                                </span>
                                <span className="dash-kpi-value">{usd(data.cost.monthToDate, 0)}</span>
                                <Delta now={data.cost.monthToDate} before={data.cost.lastMonthSamePeriod} suffix="지난달 이맘때" />
                                <span className="dash-kpi-sub">월말 예상 {usd(data.cost.forecast, 0)}</span>
                            </button>

                            <button
                                type="button"
                                className={`dash-kpi${data.approvals.pending ? ' is-warn' : ''}`}
                                onClick={() => navigate('/chat')}
                            >
                                <span className="dash-kpi-label">승인 대기</span>
                                <span className="dash-kpi-value">
                                    {data.approvals.pending}
                                    <span className="dash-kpi-unit">건</span>
                                </span>
                                <span className="dash-kpi-sub">
                                    {data.approvals.pending && data.approvals.soonestExpiresAt
                                        ? `가장 빠른 만료 ${until(data.approvals.soonestExpiresAt)}`
                                        : '기다리는 변경이 없습니다'}
                                </span>
                            </button>
                        </div>

                        {/* 비용 */}
                        <div className="dash-row">
                            <section className="dash-card dash-card--wide" aria-labelledby="dash-daily-title">
                                <header className="dash-card-head">
                                    <h3 id="dash-daily-title">일별 비용</h3>
                                    <ul className="dash-key" aria-label="범례">
                                        <li>
                                            <span className="dash-key-bar" aria-hidden="true" />
                                            확정
                                        </li>
                                        <li>
                                            <span className="dash-key-line" aria-hidden="true" />
                                            하루 평균
                                        </li>
                                        <li>
                                            <span className="dash-key-future" aria-hidden="true" />
                                            남은 날 (예상)
                                        </li>
                                    </ul>
                                </header>
                                <DailyCostChart daily={data.cost.daily} daysInMonth={daysInMonthOf(data.cost.daily[0]?.date)} />
                                <p className="dash-card-note">비용은 하루 늦게 확정됩니다. 어제까지의 값입니다.</p>
                            </section>
                            <section className="dash-card" aria-labelledby="dash-service-title">
                                <header className="dash-card-head">
                                    <h3 id="dash-service-title">서비스별 비용</h3>
                                    <span className="dash-card-aside">{usd(data.cost.monthToDate)}</span>
                                </header>
                                <ServiceBars items={data.cost.byService} />
                            </section>
                        </div>

                        {/* 리소스 · 변경 */}
                        <div className="dash-row">
                            <section className="dash-card dash-card--wide" aria-labelledby="dash-resource-title">
                                <header className="dash-card-head">
                                    <h3 id="dash-resource-title">리소스 상태</h3>
                                </header>
                                <StatusBar counts={counts} />
                                <div className="dash-table-wrap">
                                    <table className="dash-table">
                                        <thead>
                                            <tr>
                                                {(['name', 'status', 'errors', 'cost'] as SortKey[]).map((key) => (
                                                    <th
                                                        key={key}
                                                        className={`is-${key}`}
                                                        aria-sort={sort.key === key ? (sort.desc ? 'descending' : 'ascending') : undefined}
                                                    >
                                                        <button type="button" onClick={() => toggleSort(key)}>
                                                            {SORT_LABEL[key]}
                                                            <span className={`dash-sort${sort.key === key ? ' is-active' : ''}`} aria-hidden="true">
                                                                {sort.key === key && sort.desc ? '▼' : '▲'}
                                                            </span>
                                                        </button>
                                                    </th>
                                                ))}
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {resources.map((resource) => (
                                                <tr
                                                    key={resource.id}
                                                    tabIndex={0}
                                                    onClick={() => onAsk(`${resource.label ?? resource.id} 상태 점검해줘`)}
                                                    onKeyDown={(event) => {
                                                        if (event.key === 'Enter') onAsk(`${resource.label ?? resource.id} 상태 점검해줘`);
                                                    }}
                                                    title="누르면 이 리소스를 점검하는 대화를 시작합니다"
                                                >
                                                    <td className="is-name">
                                                        <div className="dash-name-cell">
                                                            <span className="dash-kind">{resource.kind}</span>
                                                            <span className="dash-resource">
                                                                <span className="dash-resource-name">{resource.label ?? resource.id}</span>
                                                                <span className="dash-resource-detail">
                                                                    {resource.label ? `${resource.id} · ` : ''}
                                                                    {resource.detail}
                                                                </span>
                                                            </span>
                                                        </div>
                                                    </td>
                                                    <td className="is-status">
                                                        <StatusBadge status={resource.status} />
                                                    </td>
                                                    <td className="is-errors">{resource.errors24h ?? '–'}</td>
                                                    <td className="is-cost">{resource.costMonth !== undefined ? usd(resource.costMonth) : '–'}</td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </div>
                            </section>

                            <section className="dash-card" aria-labelledby="dash-change-title">
                                <header className="dash-card-head">
                                    <h3 id="dash-change-title">최근 변경</h3>
                                    <span className="dash-card-aside">24시간</span>
                                </header>
                                {data.changes.length ? (
                                    <ol className="dash-changes">
                                        {data.changes.map((change) => (
                                            <li key={`${change.at}-${change.summary}`} className={`is-${change.source}`}>
                                                <span className="dash-change-dot" aria-hidden="true" />
                                                <span className="dash-change-body">
                                                    <span className="dash-change-summary">{change.summary}</span>
                                                    <span className="dash-change-meta">
                                                        {ago(change.at)} · {change.actor} ·{' '}
                                                        <span className="dash-change-source">
                                                            {change.source === 'app' ? '이 앱에서 승인' : 'CloudTrail'}
                                                        </span>
                                                    </span>
                                                </span>
                                            </li>
                                        ))}
                                    </ol>
                                ) : (
                                    <p className="dash-card-note">지난 24시간 동안 바뀐 것이 없습니다.</p>
                                )}
                            </section>
                        </div>

                        {/* 치울 것 */}
                        {data.findings.length ? (
                            <section className="dash-card" aria-labelledby="dash-finding-title">
                                <header className="dash-card-head">
                                    <h3 id="dash-finding-title">치울 것</h3>
                                    <span className="dash-card-aside">
                                        {(() => {
                                            const savings = data.findings.reduce((sum, f) => sum + (f.savingsMonthly ?? 0), 0);
                                            return savings ? `치우면 월 ${usd(savings, 0)} 절약` : null;
                                        })()}
                                    </span>
                                </header>
                                <ul className="dash-findings">
                                    {/* 문제 먼저, 그다음 주의 */}
                                    {[...data.findings]
                                        .sort((a, b) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status))
                                        .map((finding) => (
                                        <li key={finding.kind}>
                                            <button type="button" className="dash-finding" onClick={() => onAsk(finding.question)}>
                                                <StatusBadge status={finding.status} />
                                                <span className="dash-finding-title">{finding.title}</span>
                                                <span className="dash-finding-detail">{finding.detail}</span>
                                                <span className="dash-finding-foot">
                                                    {finding.savingsMonthly ? (
                                                        <span className="dash-finding-savings">월 {usd(finding.savingsMonthly)} 절약</span>
                                                    ) : (
                                                        <span />
                                                    )}
                                                    <span className="dash-finding-ask">
                                                        물어보기 <AskArrow />
                                                    </span>
                                                </span>
                                            </button>
                                        </li>
                                    ))}
                                </ul>
                            </section>
                        ) : null}
                    </>
                ) : null}
            </div>
        </div>
    );
}

// 그 달의 날 수 (일별 비용의 첫 날짜로 안다)
const daysInMonthOf = (date?: string) => {
    const base = date ? new Date(`${date}T00:00:00`) : new Date();
    return new Date(base.getFullYear(), base.getMonth() + 1, 0).getDate();
};

// ---------------------------------------------------------------- 지금 확인할 것
// 울리는 알람, 승인을 기다리는 변경, 문제 상태의 리소스(알람 말고)를 한 곳에. 없으면 초록 한 줄
function Attention({ data, onAsk, onOpenChat }: { data: DashboardData; onAsk: (q: string) => void; onOpenChat: () => void }) {
    const items: { key: string; status: HealthStatus; title: string; sub?: string; action: string; run: () => void }[] = [
        ...data.alarms.firing.map((alarm) => ({
            key: `alarm-${alarm.name}`,
            status: 'fail' as const,
            title: `${alarm.name} 알람이 ${lasting(alarm.since)} 울리는 중`,
            sub: alarm.metric,
            action: '물어보기',
            run: () => onAsk(`${alarm.name} 알람 왜 울렸어?`),
        })),
        ...(data.approvals.pending
            ? [
                  {
                      key: 'approvals',
                      status: 'warn' as const,
                      title: `승인을 기다리는 변경 ${data.approvals.pending}건`,
                      sub: data.approvals.soonestExpiresAt ? `${until(data.approvals.soonestExpiresAt)} 만료` : undefined,
                      action: '대화에서 보기',
                      run: onOpenChat,
                  },
              ]
            : []),
        ...data.resources
            .filter((resource) => resource.status === 'fail' && resource.kind !== 'Alarm')
            .map((resource) => ({
                key: `resource-${resource.id}`,
                status: 'fail' as const,
                title: `${resource.kind} ${resource.label ?? resource.id}`,
                sub: resource.detail,
                action: '물어보기',
                run: () => onAsk(`${resource.label ?? resource.id} 상태 점검해줘`),
            })),
    ];

    if (!items.length) {
        return (
            <div className="dash-attention is-clear" role="status">
                <span className="dash-attention-dot" aria-hidden="true" />
                지금 확인할 것이 없습니다. 울리는 알람과 승인 대기가 없습니다.
            </div>
        );
    }
    return (
        <section className="dash-attention" aria-label="지금 확인할 것">
            <p className="dash-attention-title">지금 확인할 것 {items.length}</p>
            <ul>
                {items.map((item) => (
                    <li key={item.key}>
                        <button type="button" className={`dash-attention-item is-${item.status}`} onClick={item.run}>
                            <span className="dash-attention-dot" aria-hidden="true" />
                            <span className="dash-attention-text">
                                <span className="dash-attention-main">{item.title}</span>
                                {item.sub ? <span className="dash-attention-sub">{item.sub}</span> : null}
                            </span>
                            <span className="dash-attention-action">
                                {item.action} {item.action === '물어보기' ? <AskArrow /> : '→'}
                            </span>
                        </button>
                    </li>
                ))}
            </ul>
        </section>
    );
}
