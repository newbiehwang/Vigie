// 홈 대시보드의 작은 차트들. 라이브러리 없이 SVG·HTML로 그린다 (값이 적고 모양이 단순하다).
// 규칙 (dataviz):
// - 크기(비용·오류)는 한 색(파랑)의 막대로. 상태(문제·주의·정상·데이터 없음)만 상태 색을 쓰고, 늘 글자와 함께 둔다
// - 막대는 바닥에 붙고 위 모서리만 둥글다. 막대 사이에 바탕색 틈을 둔다
// - 막대마다 마우스를 올리면 값이 뜬다 (막대보다 넓은 자리에서 잡는다)
// - 처음 그릴 때 막대가 바닥에서 차례로 자란다 (--i: 막대 순서, dashboard.css의 dash-grow-y)
import { type CSSProperties, useEffect, useRef, useState, type ReactNode } from 'react';
import type { HealthStatus } from '@/types/dashboard';

export const usd = (value: number, digits = 2) =>
    value.toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: digits, maximumFractionDigits: digits });

export const STATUS_LABEL: Record<HealthStatus, string> = { fail: '문제', warn: '주의', ok: '정상', none: '데이터 없음' };
export const STATUS_ORDER: HealthStatus[] = ['fail', 'warn', 'ok', 'none'];

// 바닥에 붙고 위 두 모서리만 둥근 막대 (높이가 반지름보다 작으면 반지름을 줄인다)
const barPath = (x: number, y: number, width: number, height: number, radius = 3) => {
    if (height <= 0) return '';
    const r = Math.min(radius, width / 2, height);
    return `M${x},${y + height}V${y + r}Q${x},${y} ${x + r},${y}H${x + width - r}Q${x + width},${y} ${x + width},${y + r}V${y + height}Z`;
};

// 부모 폭을 따라가는 SVG를 위해 폭을 잰다
function useWidth<T extends HTMLElement>() {
    const ref = useRef<T>(null);
    const [width, setWidth] = useState(0);
    useEffect(() => {
        const element = ref.current;
        if (!element) return;
        const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
        observer.observe(element);
        return () => observer.disconnect();
    }, []);
    return [ref, width] as const;
}

// 마우스를 올린 막대의 값 (막대 꼭대기 바로 위에 떠 있는 작은 상자). x·y는 차트 안의 자리
function Tooltip({ x, y, children }: { x: number; y: number; children: ReactNode }) {
    return (
        <div className="dash-tooltip" style={{ left: x, top: y }} role="presentation">
            {children}
        </div>
    );
}

// ---------------------------------------------------------------- 작은 막대 (오류 24시간, 숫자 카드 안)
export function Sparkbars({ values, label }: { values: number[]; label: string }) {
    const [ref, width] = useWidth<HTMLDivElement>();
    const [hover, setHover] = useState<number | null>(null);
    const height = 34;
    const max = Math.max(...values, 1);
    const step = width / values.length;
    const gap = Math.min(2, step * 0.3);
    return (
        <div ref={ref} className="dash-spark" onMouseLeave={() => setHover(null)}>
            {width ? (
                <svg width={width} height={height} role="img" aria-label={label}>
                    {values.map((value, i) => {
                        const h = Math.max((value / max) * (height - 2), value ? 2 : 0);
                        return (
                            <g key={i}>
                                <path
                                    className={`dash-spark-bar${hover === i ? ' is-hover' : ''}`}
                                    d={barPath(i * step + gap / 2, height - h, step - gap, h, 2)}
                                    style={{ '--i': i } as CSSProperties}
                                />
                                {/* 잡는 자리는 막대보다 넓게 (칸 전체) */}
                                <rect x={i * step} y={0} width={step} height={height} fill="transparent" onMouseEnter={() => setHover(i)} />
                            </g>
                        );
                    })}
                    <line className="dash-baseline" x1={0} x2={width} y1={height - 0.5} y2={height - 0.5} />
                </svg>
            ) : null}
            {hover !== null ? (
                <Tooltip x={hover * step + step / 2} y={0}>
                    {values.length - hover === 1 ? '지난 1시간' : `${values.length - hover}시간 전`} · <b>{values[hover]}건</b>
                </Tooltip>
            ) : null}
        </div>
    );
}

// ---------------------------------------------------------------- 일별 비용 (이번 달)
// 1일부터 말일까지 칸을 두고, 확정된 날(어제까지)은 막대, 남은 날은 점선 칸으로 둔다. 점선 가로줄은 하루 평균
export function DailyCostChart({ daily, daysInMonth }: { daily: { date: string; amount: number }[]; daysInMonth: number }) {
    const [ref, width] = useWidth<HTMLDivElement>();
    const [hover, setHover] = useState<number | null>(null);
    const height = 176;
    const top = 8;
    const bottom = 22; // 날짜 글자 자리
    const left = 40; // 금액 글자 자리
    const plotH = height - top - bottom;
    const plotW = Math.max(width - left, 0);
    const amounts = daily.map((d) => d.amount);
    const average = amounts.reduce((sum, n) => sum + n, 0) / Math.max(amounts.length, 1);
    const niceMax = Math.max(10, Math.ceil(Math.max(...amounts, average) / 10) * 10);
    const ticks = [0, niceMax / 2, niceMax];
    const step = plotW / daysInMonth;
    const gap = Math.max(2, step * 0.28);
    const y = (value: number) => top + plotH - (value / niceMax) * plotH;
    const dayLabel = (i: number) => {
        const date = new Date(`${daily[i].date}T00:00:00`);
        return `${date.getMonth() + 1}월 ${date.getDate()}일 (${'일월화수목금토'[date.getDay()]})`;
    };

    return (
        <div ref={ref} className="dash-chart" onMouseLeave={() => setHover(null)}>
            {width ? (
                <svg width={width} height={height} role="img" aria-label={`이번 달 일별 비용, 하루 평균 ${usd(average)}`}>
                    {ticks.map((tick) => (
                        <g key={tick}>
                            <line className="dash-grid" x1={left} x2={width} y1={y(tick) + 0.5} y2={y(tick) + 0.5} />
                            <text className="dash-axis" x={left - 8} y={y(tick) + 4} textAnchor="end">
                                ${tick}
                            </text>
                        </g>
                    ))}
                    {Array.from({ length: daysInMonth }, (_, i) => {
                        const x = left + i * step + gap / 2;
                        const w = step - gap;
                        const known = i < daily.length;
                        const h = known ? (daily[i].amount / niceMax) * plotH : 0;
                        return (
                            <g key={i}>
                                {known ? (
                                    <path
                                        className={`dash-bar${hover === i ? ' is-hover' : ''}`}
                                        d={barPath(x, top + plotH - h, w, h)}
                                        style={{ '--i': i } as CSSProperties}
                                    />
                                ) : (
                                    // 아직 오지 않은 날: 평균 높이의 점선 칸 (예상)
                                    <rect className="dash-bar-future" x={x + 0.5} y={y(average) + 0.5} width={Math.max(w - 1, 0)} height={Math.max(plotH - (y(average) - top) - 1, 0)} rx={2} />
                                )}
                                {known ? (
                                    <rect x={left + i * step} y={top} width={step} height={plotH} fill="transparent" onMouseEnter={() => setHover(i)} />
                                ) : null}
                                {(i + 1) % 7 === 1 ? (
                                    <text className="dash-axis" x={left + i * step + step / 2} y={height - 6} textAnchor="middle">
                                        {i + 1}일
                                    </text>
                                ) : null}
                            </g>
                        );
                    })}
                    <line className="dash-average" x1={left} x2={width} y1={y(average)} y2={y(average)} />
                </svg>
            ) : null}
            {hover !== null ? (
                <Tooltip x={left + hover * step + step / 2} y={y(daily[hover].amount)}>
                    {dayLabel(hover)} · <b>{usd(daily[hover].amount)}</b>
                </Tooltip>
            ) : null}
        </div>
    );
}

// ---------------------------------------------------------------- 서비스별 비용 (가로 막대, 큰 것부터)
export function ServiceBars({ items }: { items: { service: string; amount: number }[] }) {
    const total = items.reduce((sum, item) => sum + item.amount, 0) || 1;
    const max = Math.max(...items.map((item) => item.amount), 1);
    return (
        <ul className="dash-services">
            {items.map((item) => (
                <li key={item.service} className="dash-service" title={`${item.service} ${usd(item.amount)} (${Math.round((item.amount / total) * 100)}%)`}>
                    <span className="dash-service-name">{item.service}</span>
                    <span className="dash-service-track" aria-hidden="true">
                        <span className="dash-service-fill" style={{ width: `${(item.amount / max) * 100}%` }} />
                    </span>
                    <span className="dash-service-amount">{usd(item.amount)}</span>
                    <span className="dash-service-share">{Math.round((item.amount / total) * 100)}%</span>
                </li>
            ))}
        </ul>
    );
}

// ---------------------------------------------------------------- 상태 분포 (한 줄 막대 + 범례)
export function StatusBar({ counts }: { counts: Record<HealthStatus, number> }) {
    const total = STATUS_ORDER.reduce((sum, status) => sum + counts[status], 0);
    if (!total) return null;
    return (
        <div className="dash-statusbar">
            <div className="dash-statusbar-track" role="img" aria-label={STATUS_ORDER.map((s) => `${STATUS_LABEL[s]} ${counts[s]}개`).join(', ')}>
                {STATUS_ORDER.filter((status) => counts[status]).map((status) => (
                    <span
                        key={status}
                        className={`dash-statusbar-seg is-${status}`}
                        style={{ flexGrow: counts[status] }}
                        title={`${STATUS_LABEL[status]} ${counts[status]}개`}
                    />
                ))}
            </div>
            <ul className="dash-legend">
                {STATUS_ORDER.map((status) => (
                    <li key={status} className={`is-${status}${counts[status] ? '' : ' is-zero'}`}>
                        <span className="dash-legend-dot" aria-hidden="true" />
                        {STATUS_LABEL[status]} <b>{counts[status]}</b>
                    </li>
                ))}
                <li className="dash-legend-total">전체 {total}개</li>
            </ul>
        </div>
    );
}
