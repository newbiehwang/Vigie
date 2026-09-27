// 홈 대시보드: 운영자가 홈에서 5초 안에 알고 싶은 것을 급한 차례로 보인다.
//   운영 현황                                   dev · ap-northeast-2 · 3분 전에 모음  [↻]
//   [ 무엇이든 물어보세요…                                                    ➤ ]
//   ┌ 지금 확인할 것 ───────────────────────────────────────────────────────┐   ← 없으면 초록 한 줄
//   │ ● vigie-dev-api-5xx 알람 12분 동안 지속  5XXError > 5 (5분)       물어보기 ↗ │
//   │ ● 승인을 기다리는 변경 1건  9분 후 만료                        대화에서 보기 │
//   └────────────────────────────────────────────────────────────────────┘
//   [울리는 알람 1/24] [Lambda 오류 37 ▁▂▅] [이번 달 비용 $362] [승인 대기 1]   ← 숫자 카드
//   [일별 비용 (막대 · 평균 점선 · 남은 날 점선 칸)      ] [서비스별 비용 (가로 막대)]
//   [리소스 상태 (분포 막대 + 정렬되는 표)              ] [최근 변경 (시간 줄)     ]   ← 최근 변경은 관리자만
//   [개선 권고 (카드 여러 개)                                                       ]
// - AI에게 묻는 곳은 '지금 확인할 것'의 줄뿐이다 (누르면 그 내용을 질문으로 새 대화). 숫자 카드·표·개선 권고는 보기만 한다.
// - 설명 글은 두지 않는다: 확인할 것이 없으면 그 상자를 아예 그리지 않고, 값이 없는 칸은 '데이터가 존재하지 않습니다'만.
//   모으는 주기('하루 1번'·'실시간')와 증감(▲ 60% 그 전 24시간보다)도 두지 않는다. 카드 제목은 숫자 카드 이름과 같은 작은 회색 글자
//   맨 위 입력칸은 그대로 (직접 물을 때)
// - 처음 그릴 때: 카드가 차례로 떠오르고(AXPI priority-card-enter), 숫자가 0에서 올라가고, 막대가 바닥에서 자란다.
//   30초마다 다시 읽을 때는 다시 움직이지 않는다 (요소가 그대로라 CSS 등장 효과가 다시 돌지 않는다)
// - 값은 수집 Lambda가 구역마다 모아 둔 것이다 (services/dashboard). 모으지 못했을 때만 카드 제목 옆에
//   '모으지 못함 · 마지막 성공 14:32 KST'를 빨갛게 적는다 (Freshness). 한 번도 모으지 못한 칸은 '데이터가 존재하지 않습니다.'
// - 화면이 보이는 동안 POLL_MS마다 다시 읽는다 (AWS 이벤트로 바뀐 알람·상태·변경이 곧 보인다). 탭이 가려지면 멈춘다.
//   다시 읽기는 모아 둔 값(DynamoDB)만 읽을 뿐 AWS를 부르지 않는다. ↻도 같다
import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getDashboard } from '@/api/dashboard';
import { useAuthStore } from '@/auth/authStore';
import { LoadingCard, useMinimumVisible } from '@/components/LoadingCard';
import { RefreshButton } from '@/components/RefreshButton';
import { SortTh, sortedBy, useSort } from '@/components/SortHeader';
import { useToast } from '@/components/Toast';
import { Composer } from '@/features/chat/Composer';
import type { DashboardData, DashboardResource, HealthStatus, SectionName, SectionState } from '@/types/dashboard';
import { getErrorText } from '@/utils/formatters';
import { DailyCostChart, ServiceBars, Sparkbars, STATUS_LABEL, STATUS_ORDER, StatusBar, usd } from './DashboardCharts';
import { examplesFor } from './examples';

// 최근 변경 카드에 보일 수. 카드 높이를 이 수만큼의 줄로 고정한다 (줄마다 요약·출처를 한 줄씩, 넘치면 …).
// 서버도 이만큼만 보낸다 (services/llm/dashboard_view.py의 MAX_CHANGES). 변경이 적거나 많아도 카드 크기는 그대로다
const MAX_CHANGES = 10;

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

// 알람이 이어진 시간: '12분 동안 지속' · '3시간 동안 지속'
const lasting = (since: number) => {
    const s = Math.max(0, nowSeconds() - since);
    const span = s < 3600 ? `${Math.max(1, Math.floor(s / 60))}분` : `${Math.floor(s / 3600)}시간`;
    return `${span} 동안 지속`;
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

// 9분 후
const until = (at: number) => `${Math.max(1, Math.ceil((at - nowSeconds()) / 60))}분 후`;

// ---------------------------------------------------------------- 상태 배지 (공통 배지 components/badge.css)
// 문제: 옅은 빨강 · 주의: 옅은 노랑 · 정상: 글자와 회청색 점 · 데이터 없음: 옅은 점
const BADGE_VARIANT: Record<HealthStatus, string> = {
    fail: 'is-alert',
    warn: 'is-warn',
    ok: 'is-quiet',
    none: 'is-quiet is-empty',
};

function StatusBadge({ status }: { status: HealthStatus }) {
    return <span className={`badge ${BADGE_VARIANT[status]}`}>{STATUS_LABEL[status]}</span>;
}

// 누르면 대화로 보낸다는 표시
const AskArrow = () => (
    <svg className="dash-ask-arrow" viewBox="0 0 12 12" width="11" height="11" aria-hidden="true">
        <path d="M3.5 8.5l5-5M4.5 3.5h4v4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
);

const POLL_MS = 30_000;

// 모으지 못한 구역만 알린다: '모으지 못함 · 마지막 성공 14:32 KST' (빨강, 마우스를 올리면 까닭).
// 잘 모으고 있으면 아무것도 적지 않는다 (예전의 '실시간'·'하루 1번' 같은 주기 글자는 뺐다)
function Freshness({ data, section }: { data: DashboardData; section: SectionName }) {
    const state: SectionState | undefined = data.sections?.[section];
    if (!state || state.ok) return null;
    return (
        <span className="dash-fresh is-stale" title={state.error ?? undefined}>
            모으지 못함{state.lastSuccessAt ? ` · 마지막 성공 ${kst(state.lastSuccessAt)}` : ''}
        </span>
    );
}

// 한 번도 모으지 못한 칸
function NotYet() {
    return <p className="dash-card-note dash-notyet">데이터가 존재하지 않습니다.</p>;
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
        </div>
    );
}

type SortKey = 'status' | 'name' | 'errors' | 'cost';
const SORT_LABEL: Record<SortKey, string> = { status: '상태', name: '리소스', errors: '오류', cost: '이번 달 비용' };
// 처음 누르면: 상태·이름은 오름차순(문제 먼저·가나다), 오류·비용은 큰 것부터
const SORT_FIRST_DESC = (key: SortKey) => key === 'errors' || key === 'cost';
const sortValue = (r: DashboardResource, key: SortKey): number | string =>
    key === 'status' ? STATUS_ORDER.indexOf(r.status) : key === 'name' ? r.label ?? r.id : key === 'errors' ? r.errors24h ?? -1 : r.costMonth ?? -1;
// 리소스 표는 한 번에 이만큼의 줄만 보이고, 나머지는 표 안에서 스크롤해 본다 (dashboard.css의 .dash-table-wrap)
const VISIBLE_RESOURCE_ROWS = 10;

export function Dashboard({ onAsk }: { onAsk: (question: string) => void }) {
    const navigate = useNavigate();
    // 예시 질문: 관리자가 아니면 관리자 전용 도구가 필요한 질문(CloudTrail·IAM)을 뺀다 (examples.ts)
    const user = useAuthStore((s) => s.user);
    const suggestions = useMemo(() => examplesFor(user), [user]);
    const { show: showToast } = useToast();
    const [data, setData] = useState<DashboardData | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [sort, toggleSort] = useSort<SortKey>({ key: 'status', desc: false }, SORT_FIRST_DESC);
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

    const resources = useMemo(() => (data ? sortedBy(data.resources, sort, sortValue) : []), [data, sort]);

    return (
        <div className="dash">
            <header className="dash-head">
                <div className="dash-title-wrap">
                    <h2 className="dash-title">운영 현황</h2>
                    {data ? (
                        <p className="dash-meta">
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
                    suggestions={suggestions}
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
                                    {data.alarms.firing.length ? (
                                        <span className="dash-kpi-sub">
                                            {`${data.alarms.firing[0].name} · ${lasting(data.alarms.firing[0].since)}`}
                                        </span>
                                    ) : null}
                                </div>
                            ) : (
                                <EmptyKpi label="울리는 알람" data={data} section="alarms" order={1} />
                            )}

                            {data.errors ? (
                                <div className="dash-kpi dash-enter" style={enter(2)}>
                                    <span className="dash-kpi-label">
                                        Lambda 오류
                                        <Freshness data={data} section="errors" />
                                    </span>
                                    <span className="dash-kpi-value">
                                        <CountUp value={data.errors.total24h} />
                                        <span className="dash-kpi-unit">건</span>
                                    </span>
                                    <Sparkbars values={data.errors.hourly} label="지난 24시간 시간별 오류 수" />
                                </div>
                            ) : (
                                <EmptyKpi label="Lambda 오류" data={data} section="errors" order={2} />
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
                                    <span className="dash-kpi-sub">월말 예상 {usd(data.cost.forecast, 0)}</span>
                                </div>
                            ) : (
                                <EmptyKpi label="이번 달 비용" data={data} section="cost" order={3} />
                            )}

                            <div className="dash-kpi dash-enter" style={enter(4)}>
                                <span className="dash-kpi-label">
                                    승인 대기
                                </span>
                                <span className="dash-kpi-value">
                                    <CountUp value={data.approvals.pending} />
                                    <span className="dash-kpi-unit">건</span>
                                </span>
                                {data.approvals.unavailable ? (
                                    <span className="dash-kpi-sub">승인 요청을 읽지 못했습니다</span>
                                ) : data.approvals.pending && data.approvals.soonestExpiresAt ? (
                                    <span className="dash-kpi-sub">가장 이른 만료 {until(data.approvals.soonestExpiresAt)}</span>
                                ) : null}
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
                            </section>
                            <section className="dash-card dash-enter" style={enter(6)} aria-labelledby="dash-service-title">
                                <header className="dash-card-head">
                                    <h3 id="dash-service-title">서비스별 비용</h3>
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
                                <NotYet />
                            </section>
                        )}

                        {/* 리소스 · 변경. 최근 변경은 관리자에게만 온다(일반 사용자는 null): 없으면 리소스 표가 한 줄을 다 쓴다 */}
                        <div className={data.changes ? 'dash-row' : 'dash-row dash-row--single'}>
                            <section className="dash-card dash-card--wide dash-enter" style={enter(7)} aria-labelledby="dash-resource-title">
                                <header className="dash-card-head">
                                    <h3 id="dash-resource-title">
                                        리소스 상태 <Freshness data={data} section="resources" />
                                    </h3>
                                </header>
                                {!data.sections?.resources && !resources.length ? <NotYet /> : null}
                                <StatusBar counts={counts} />
                                {/* 10줄까지 보이고 나머지는 표 안에서 스크롤. 머리 줄은 스크롤해도 위에 붙어 있다 */}
                                <div
                                    className={`dash-table-wrap${resources.length > VISIBLE_RESOURCE_ROWS ? ' is-scrolling' : ''}`}
                                    style={{ '--visible-rows': VISIBLE_RESOURCE_ROWS } as CSSProperties}
                                    tabIndex={resources.length > VISIBLE_RESOURCE_ROWS ? 0 : undefined}
                                    aria-label={resources.length > VISIBLE_RESOURCE_ROWS ? '리소스 목록 (스크롤)' : undefined}
                                >
                                    <table className="dash-table">
                                        <thead>
                                            <tr>
                                                {(['name', 'status', 'errors', ...(hasCost ? ['cost'] : [])] as SortKey[]).map((key) => (
                                                    <SortTh
                                                        key={key}
                                                        className={`is-${key}`}
                                                        column={key}
                                                        label={SORT_LABEL[key]}
                                                        sort={sort}
                                                        onSort={toggleSort}
                                                        align={key === 'errors' || key === 'cost' ? 'end' : 'start'}
                                                    />
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
                                                                <span className="dash-resource-name" title={resource.label ?? resource.id}>
                                                                    {resource.label ?? resource.id}
                                                                </span>
                                                                <span
                                                                    className="dash-resource-detail"
                                                                    title={`${resource.label ? `${resource.id} · ` : ''}${resource.detail}`}
                                                                >
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
                                        전체 {data.resourceTotal}개 중 문제·주의 우선 {resources.length}개 표시
                                    </p>
                                ) : null}
                            </section>

                            {data.changes ? (
                                <section
                                    className="dash-card dash-card--changes dash-enter"
                                    style={{ ...enter(8), '--change-rows': MAX_CHANGES } as CSSProperties}
                                    aria-labelledby="dash-change-title"
                                >
                                    <header className="dash-card-head">
                                        <h3 id="dash-change-title">
                                            최근 변경 <Freshness data={data} section="changes" />
                                        </h3>
                                    </header>
                                    {data.changes.length ? (
                                        <ol className="dash-changes">
                                            {data.changes.slice(0, MAX_CHANGES).map((change) => (
                                                <li key={`${change.at}-${change.summary}`} className={`is-${change.source}`}>
                                                    <span className="dash-change-dot" aria-hidden="true" />
                                                    <span className="dash-change-body">
                                                        {/* 한 줄로 자르고, 전체는 마우스를 올리면 보인다 */}
                                                        <span className="dash-change-summary" title={change.summary}>
                                                            {change.summary}
                                                        </span>
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
                                        <NotYet />
                                    )}
                                </section>
                            ) : null}
                        </div>

                        {/* 개선 권고 */}
                        {data.findings.length ? (
                            <section className="dash-card dash-enter" style={enter(9)} aria-labelledby="dash-finding-title">
                                <header className="dash-card-head">
                                    <h3 id="dash-finding-title">개선 권고</h3>
                                    <span className="dash-card-aside">
                                        {(() => {
                                            const savings = data.findings.reduce((sum, f) => sum + (f.savingsMonthly ?? 0), 0);
                                            return savings ? `적용 시 월 ${usd(savings, 0)} 절감` : null;
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
                                                    <span className="dash-finding-savings">월 {usd(finding.savingsMonthly)} 절감</span>
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
            title: `${alarm.name} 알람 ${lasting(alarm.since)}`,
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

    if (!items.length) return null; // 확인할 것이 없으면 상자를 그리지 않는다
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
