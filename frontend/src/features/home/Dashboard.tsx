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
// - AI에게 묻는 곳은 '지금 확인할 것'의 줄뿐이다 (누르면 그 내용을 질문으로 새 대화). 숫자 카드·표·치울 것은 보기만 한다.
//   맨 위 입력칸은 그대로 (직접 물을 때)
// - 처음 그릴 때: 카드가 차례로 떠오르고(AXPI priority-card-enter), 숫자가 0에서 올라가고, 막대가 바닥에서 자란다.
//   30초마다 다시 읽을 때는 다시 움직이지 않는다 (요소가 그대로라 CSS 등장 효과가 다시 돌지 않는다)
// - 값은 수집 Lambda가 구역마다 모아 둔 것이다 (services/dashboard). 카드마다 얼마나 자주 모으는지와, 모으지 못했으면
//   '모으지 못함 · 마지막 성공 N분 전'을 작게 적는다 (Freshness). 한 번도 모으지 못한 칸은 '아직 모으지 않았습니다'
// - 화면이 보이는 동안 POLL_MS마다 다시 읽는다 (AWS 이벤트로 바뀐 알람·상태·변경이 곧 보인다). 탭이 가려지면 멈춘다.
//   다시 읽기는 모아 둔 값(DynamoDB)만 읽을 뿐 AWS를 부르지 않는다. ↻도 같다
import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getDashboard } from '@/api/dashboard';
import { LoadingCard, useMinimumVisible } from '@/components/LoadingCard';
import { RefreshButton } from '@/components/RefreshButton';
import { useToast } from '@/components/Toast';
import { Composer } from '@/features/chat/Composer';
import type { DashboardData, DashboardResource, HealthStatus, SectionName, SectionState } from '@/types/dashboard';
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

// 마지막 업데이트 시각: '14:32 KST' (오늘이 아니면 '9월 26일 14:32 KST')
const kst = (at: number) => {
    const date = new Date(at * 1000);
    const day = (d: Date) => d.toLocaleDateString('ko-KR', { timeZone: 'Asia/Seoul' });
    const time = date.toLocaleTimeString('ko-KR', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    const prefix =
        day(date) === day(new Date())
            ? ''
            : `${date.toLocaleDateString('ko-KR', { timeZone: 'Asia/Seoul', month: 'long', day: 'numeric' })} `;
    return `${prefix}${time} KST`;
};

// 처음 그릴 때 숫자를 0에서 올린다 (한 번만. 그 뒤 값이 바뀌면 바로 바꾼다). 움직임 줄이기 설정이면 올리지 않는다
function useCountUp(value: number, duration = 700) {
    const [shown, setShown] = useState(() => (reducedMotion() ? value : 0));
    const done = useRef(false);
    useEffect(() => {
        if (done.current || reducedMotion()) {
            setShown(value);
            return;
        }
        const start = performance.now();
        let frame = 0;
        const step = (now: number) => {
            const t = Math.min(1, (now - start) / duration);
            setShown(value * (1 - Math.pow(1 - t, 3))); // 끝에서 느려진다
            if (t < 1) frame = requestAnimationFrame(step);
            else done.current = true;
        };
        frame = requestAnimationFrame(step);
        return () => cancelAnimationFrame(frame);
    }, [value, duration]);
    return shown;
}

const reducedMotion = () =>
    typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

function CountUp({ value, format = (n) => String(Math.round(n)) }: { value: number; format?: (n: number) => string }) {
    return <>{format(useCountUp(value))}</>;
}

// 차례로 떠오르는 순서 (CSS의 dash-enter, 50ms씩)
const enter = (order: number) => ({ '--enter': order }) as CSSProperties;

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

const POLL_MS = 30_000;

// 구역마다 모으는 때 (services/dashboard/lambda_function.py의 Scheduler·이벤트)
const CADENCE: Record<SectionName, string> = {
    alarms: '실시간',
    resources: '실시간',
    changes: '실시간',
    errors: '5분마다',
    usage: '1시간마다',
    cost: '하루 1번',
};

// 얼마나 새 값인가: '5분마다 · 2분 전' / 실패하면 '모으지 못함 · 마지막 성공 3시간 전' (빨강, 마우스를 올리면 까닭)
function Freshness({ data, section }: { data: DashboardData; section: SectionName }) {
    const state: SectionState | undefined = data.sections?.[section];
    if (!state) return <span className="dash-fresh is-missing">아직 모으지 않음</span>;
    if (!state.ok) {
        return (
            <span className="dash-fresh is-stale" title={state.error ?? undefined}>
                모으지 못함{state.lastSuccessAt ? ` · 마지막 성공 ${kst(state.lastSuccessAt)}` : ''}
            </span>
        );
    }
    return (
        <span className="dash-fresh" title={state.collectedAt ? `마지막 업데이트 ${kst(state.collectedAt)}` : undefined}>
            {CADENCE[section]}
        </span>
    );
}

// 한 번도 모으지 못한 칸
function NotYet({ section }: { section: SectionName }) {
    return (
        <p className="dash-card-note dash-notyet">
            아직 모으지 않았습니다. {CADENCE[section] === '실시간' ? '곧' : CADENCE[section]} 모읍니다.
        </p>
    );
}

// 한 번도 모으지 못한 숫자 카드 (누를 것이 없다)
function EmptyKpi({ label, data, section, order }: { label: string; data: DashboardData; section: SectionName; order: number }) {
    return (
        <div className="dash-kpi is-empty dash-enter" style={enter(order)}>
            <span className="dash-kpi-label">
                {label}
                <Freshness data={data} section={section} />
            </span>
            <span className="dash-kpi-value">–</span>
            <span className="dash-kpi-sub">{CADENCE[section]} 모읍니다</span>
        </div>
    );
}

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

    // quiet: 주기적으로 다시 읽을 때. 기다림 표시와 실패 알림을 띄우지 않는다 (앞 화면을 그대로 둔다)
    const load = useCallback(
        async (quiet = false) => {
            if (!quiet) setLoading(true);
            try {
                setData(await getDashboard());
                setError(null);
            } catch (err) {
                if (quiet) return;
                setError(getErrorText(err));
                showToast('error', '운영 현황을 불러오지 못했습니다.');
            } finally {
                if (!quiet) setLoading(false);
            }
        },
        [showToast],
    );

    useEffect(() => {
        load();
    }, [load]);

    // 화면이 보이는 동안만 POLL_MS마다. 다시 보이면 바로 한 번 읽는다
    useEffect(() => {
        let timer: number | undefined;
        const start = () => {
            window.clearInterval(timer);
            timer = window.setInterval(() => load(true), POLL_MS);
        };
        const onVisibility = () => {
            if (document.hidden) {
                window.clearInterval(timer);
            } else {
                load(true);
                start();
            }
        };
        if (!document.hidden) start();
        document.addEventListener('visibilitychange', onVisibility);
        return () => {
            window.clearInterval(timer);
            document.removeEventListener('visibilitychange', onVisibility);
        };
    }, [load]);

    // 상태별 개수: 서버가 잘리기 전 모든 리소스로 센 값 (없으면 받은 줄로 센다)
    const counts = useMemo(() => {
        if (data?.resourceCounts) return data.resourceCounts;
        const result: Record<HealthStatus, number> = { fail: 0, warn: 0, ok: 0, none: 0 };
        data?.resources.forEach((resource) => (result[resource.status] += 1));
        return result;
    }, [data]);
    // 리소스별 비용은 실제 서버가 주지 않는다 (유료 설정이 필요하다). 값이 하나도 없으면 열을 뺀다
    const hasCost = Boolean(data?.resources.some((resource) => resource.costMonth !== undefined));

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
                            {data.region} · 마지막 업데이트 시각: {data.generatedAt ? kst(data.generatedAt) : '아직 없음'}
                        </p>
                    ) : null}
                </div>
                <RefreshButton onClick={() => load()} loading={loading} />
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

            {/* 기다림 카드: 흰 카드(plan-panel) 한가운데 (다른 탭과 같은 plan-panel-loading) */}
            {showLoading ? (
                <div className="plan-panel-loading">
                    <LoadingCard text="운영 현황을 불러오는 중…" />
                </div>
            ) : null}

            <div className="dash-body">
                {!data && !loading && error ? (
                    <div className="dash-empty">
                        <p className="dash-empty-title">운영 현황을 불러오지 못했습니다.</p>
                        <p className="dash-empty-text">{error}</p>
                        <button type="button" className="plan-reload-button" onClick={() => load()}>
                            다시 불러오기
                        </button>
                    </div>
                ) : null}

                {/* 기다림 카드가 사라진 뒤에 그린다 (그래야 카드가 떠오르는 효과가 보인다) */}
                {data && !showLoading ? (
                    <>
                        <div className="dash-enter" style={enter(0)}>
                            <Attention data={data} onAsk={onAsk} onOpenChat={() => navigate('/chat')} />
                        </div>

                        {/* 숫자 카드 4개 */}
                        <div className="dash-kpis">
                            {data.alarms ? (
                                <div
                                    className={`dash-kpi dash-enter${data.alarms.firing.length ? ' is-fail' : ''}`}
                                    style={enter(1)}
                                >
                                    <span className="dash-kpi-label">
                                        울리는 알람
                                        <Freshness data={data} section="alarms" />
                                    </span>
                                    <span className="dash-kpi-value">
                                        <CountUp value={data.alarms.firing.length} />
                                        <span className="dash-kpi-unit"> / {data.alarms.total}개</span>
                                    </span>
                                    <span className="dash-kpi-sub">
                                        {data.alarms.firing.length
                                            ? `${data.alarms.firing[0].name} · ${lasting(data.alarms.firing[0].since)}`
                                            : '모든 알람이 정상입니다'}
                                    </span>
                                </div>
                            ) : (
                                <EmptyKpi label="울리는 알람" data={data} section="alarms" order={1} />
                            )}

                            {data.errors ? (
                                <div className="dash-kpi dash-enter" style={enter(2)}>
                                    <span className="dash-kpi-label">
                                        Lambda 오류 · 24시간
                                        <Freshness data={data} section="errors" />
                                    </span>
                                    <span className="dash-kpi-value">
                                        <CountUp value={data.errors.total24h} />
                                        <span className="dash-kpi-unit">건</span>
                                    </span>
                                    <Delta now={data.errors.total24h} before={data.errors.previous24h} suffix="그 전 24시간" />
                                    <Sparkbars values={data.errors.hourly} label="지난 24시간 시간별 오류 수" />
                                </div>
                            ) : (
                                <EmptyKpi label="Lambda 오류 · 24시간" data={data} section="errors" order={2} />
                            )}

                            {data.cost ? (
                                <div className="dash-kpi dash-enter" style={enter(3)}>
                                    <span className="dash-kpi-label">
                                        이번 달 비용
                                        <Freshness data={data} section="cost" />
                                    </span>
                                    <span className="dash-kpi-value">
                                        <CountUp value={data.cost.monthToDate} format={(n) => usd(n, 0)} />
                                    </span>
                                    <Delta now={data.cost.monthToDate} before={data.cost.lastMonthSamePeriod} suffix="지난달 이맘때" />
                                    <span className="dash-kpi-sub">월말 예상 {usd(data.cost.forecast, 0)}</span>
                                </div>
                            ) : (
                                <EmptyKpi label="이번 달 비용" data={data} section="cost" order={3} />
                            )}

                            <div className={`dash-kpi dash-enter${data.approvals.pending ? ' is-warn' : ''}`} style={enter(4)}>
                                <span className="dash-kpi-label">
                                    승인 대기
                                    <span className="dash-fresh">실시간</span>
                                </span>
                                <span className="dash-kpi-value">
                                    <CountUp value={data.approvals.pending} />
                                    <span className="dash-kpi-unit">건</span>
                                </span>
                                <span className="dash-kpi-sub">
                                    {data.approvals.unavailable
                                        ? '승인 요청을 읽지 못했습니다'
                                        : data.approvals.pending && data.approvals.soonestExpiresAt
                                          ? `가장 빠른 만료 ${until(data.approvals.soonestExpiresAt)}`
                                          : '기다리는 변경이 없습니다'}
                                </span>
                            </div>
                        </div>

                        {/* 비용 */}
                        {data.cost ? (
                        <div className="dash-row">
                            <section className="dash-card dash-card--wide dash-enter" style={enter(5)} aria-labelledby="dash-daily-title">
                                <header className="dash-card-head">
                                    <h3 id="dash-daily-title">
                                        일별 비용 <Freshness data={data} section="cost" />
                                    </h3>
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
                            <section className="dash-card dash-enter" style={enter(6)} aria-labelledby="dash-service-title">
                                <header className="dash-card-head">
                                    <h3 id="dash-service-title">서비스별 비용</h3>
                                    <span className="dash-card-aside">{usd(data.cost.monthToDate)}</span>
                                </header>
                                <ServiceBars items={data.cost.byService} />
                            </section>
                        </div>
                        ) : (
                            <section className="dash-card dash-enter" style={enter(5)} aria-label="비용">
                                <header className="dash-card-head">
                                    <h3>
                                        일별 비용 <Freshness data={data} section="cost" />
                                    </h3>
                                </header>
                                <NotYet section="cost" />
                            </section>
                        )}

                        {/* 리소스 · 변경 */}
                        <div className="dash-row">
                            <section className="dash-card dash-card--wide dash-enter" style={enter(7)} aria-labelledby="dash-resource-title">
                                <header className="dash-card-head">
                                    <h3 id="dash-resource-title">
                                        리소스 상태 <Freshness data={data} section="resources" />
                                    </h3>
                                    {data.sections?.usage ? null : <span className="dash-card-aside">오류·CPU는 1시간마다</span>}
                                </header>
                                {!data.sections?.resources && !resources.length ? <NotYet section="resources" /> : null}
                                <StatusBar counts={counts} />
                                <div className="dash-table-wrap">
                                    <table className="dash-table">
                                        <thead>
                                            <tr>
                                                {(['name', 'status', 'errors', ...(hasCost ? ['cost'] : [])] as SortKey[]).map((key) => (
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
                                                <tr key={resource.id}>
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
                                                    {hasCost ? (
                                                        <td className="is-cost">
                                                            {resource.costMonth !== undefined ? usd(resource.costMonth) : '–'}
                                                        </td>
                                                    ) : null}
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </div>
                                {data.resourceTotal && data.resourceTotal > resources.length ? (
                                    <p className="dash-card-note dash-table-more">
                                        문제·주의를 먼저 {resources.length}개 보입니다. 모두 {data.resourceTotal}개입니다.
                                    </p>
                                ) : null}
                            </section>

                            <section className="dash-card dash-enter" style={enter(8)} aria-labelledby="dash-change-title">
                                <header className="dash-card-head">
                                    <h3 id="dash-change-title">
                                        최근 변경 <Freshness data={data} section="changes" />
                                    </h3>
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
                            <section className="dash-card dash-enter" style={enter(9)} aria-labelledby="dash-finding-title">
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
                                        .map((finding, index) => (
                                            <li key={finding.kind} className="dash-finding dash-enter" style={enter(10 + index)}>
                                                <StatusBadge status={finding.status} />
                                                <span className="dash-finding-title">{finding.title}</span>
                                                <span className="dash-finding-detail">{finding.detail}</span>
                                                {finding.savingsMonthly ? (
                                                    <span className="dash-finding-savings">월 {usd(finding.savingsMonthly)} 절약</span>
                                                ) : null}
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
        ...(data.alarms?.firing ?? []).map((alarm) => ({
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
