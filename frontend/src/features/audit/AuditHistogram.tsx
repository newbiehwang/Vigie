// 감사 로그 목록 위의 시간대별 건수 막대그래프 (Datadog Audit Trail의 막대그래프를 따랐다).
//                                                             드래그하거나 막대를 눌러 기간 좁히기
//   ■ 성공 471  ■ 실패 43 · 8.4%                               ← 범례: 누르면 그 값으로 거른다
//   30 ┤─────────────────────────────────          ← 가는 가로 눈금선 (0 · 중간 · 윗값)
//   20 ┤──────────────▅──────────────────
//   10 ┤──▂──▅─▃█──▁─▁█─▇─█─▂──────────────
//    0 ┴──█──█─██──█─██─█─█─█───────────
//        9/24     06:00     12:00     18:00    9/25      ← 날이 바뀌는 눈금은 날짜(굵게), 나머지는 시각
//
// - 목록과 같은 기록(기간·검색어·거르기를 적용한 것)을 칸마다 센다. 칸은 기간에 따라 1분~하루 (timeWindow.bucketSizeOf)
// - 그룹 기준(Datadog의 group by): 칸마다 무엇으로 묶어 색을 나눠 쌓을지. 필터 창(FilterMenu)에서 고른다 (seriesOf)
//   · 결과: 실패(아래)·성공(위). 실패를 바닥에 두어 칸끼리 실패 건수를 비교하기 쉽게. 범례에 실패율
//   · 종류: 도구 호출·질문·변경 작업·사용자 관리
//   · 요청자·도구: 받은 기록 전체에서 많은 순 3개 + 기타 (도구는 도구 없는 기록도 '도구 없음'으로 순위에 든다)
//     순위는 받은 기록 전체(rankRecords)로 정한다: 거르기·검색·드래그로 목록이 바뀌어도 같은 사람·도구는 같은 색을 지킨다
// - 범례의 값을 누르면 왼쪽 거르기를 그 값 하나로 바꾼다 (기타·도구 없음은 누를 수 없다)
// - 가로 눈금은 막대 수와 상관없이 '보기 좋은 시각'에 찍는다 (10분·3시간·하루·5일 등, timeWindow.ticksOf)
// - 기록이 없으면 그래프 자리에 '이 기간에 기록이 없습니다'. 불러오는 동안에는 앞 그래프를 흐리게 남겨 둔다
// - 처음 그릴 때(기간·조건·그룹 기준이 바뀔 때도) 막대가 바닥에서 왼쪽부터 차례로 자라 올라온다
// - 마우스를 올리면 그 막대만 또렷하게 두고 나머지 막대를 흐리게 한다 (기록이 없는 칸은 반응하지 않는다).
//   그 칸의 시각과 건수는 막대 머리 옆 말풍선으로 보이고, 다른 막대로 옮기면 말풍선이 미끄러지듯 따라간다
//   (그래프 안에 두어 위의 버튼을 가리지 않게)
// - 드래그하면 그 구간으로, 한 칸을 누르면 그 칸으로 기간을 좁힌다. 드래그하는 동안 고른 구간의 시각을 위에 보인다
// - 움직임을 줄이는 설정이면 자라기·미끄러지기 효과를 끈다 (audit.css)
// - 키보드: 그래프에 포커스를 두고 ←/→로 칸을 옮기고 Enter로 그 칸만 본다. 칸의 내용은 화면 읽기 프로그램에도 알린다
// - 색: Midnight Ink 팔레트만 쓴다 (styles.css). 결과는 성공 주 색·실패 빨강(실패에만 쓰는 색),
//   나머지는 차례가 정해진 세 색(CATEGORICAL: 주 색 → 남색 → 회청색)과 기타 옅은 선 색. 색을 돌려 쓰지 않는다.
//   세 색은 흰 바탕에서 이웃한 색끼리의 색각 이상 구분 검사를 통과했다 (명도 차이라 누구에게나 구분된다).
//   기타(옅은 선 색)는 대비가 3:1보다 낮아 색만으로 구분하지 않게 범례(건수)·말풍선에 이름을 함께 적고, 아래 목록이 표 역할을 한다
import { useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import type { AuditRecord } from '@/types/audit';
import {
    KIND_LABELS,
    requesterOf,
    timeOf,
    toolLabelOf,
    type FacetId,
    type GroupBy,
} from './auditModel';
import { bucketSizeOf, bucketStart, formatShort, ticksOf, type TimeWindow } from './timeWindow';

// ---------------------------------------------------------------- 그룹 기준

// 쌓는 계열 하나 (아래부터 쌓는다). facet·values: 범례를 누르면 왼쪽 거르기를 이 값들로 (없으면 누를 수 없다)
interface Series {
    key: string;
    label: string;
    color: string;
    facet?: FacetId;
    values?: string[];
}

// 차례가 정해진 계열 색 (주 색 → 남색 → 회청색 → 옅은 선 색). 차례를 바꾸지 않는다.
// SVG fill에 CSS 변수를 그대로 쓴다 (styles.css의 --primary·--ink·--slate·--line)
const CATEGORICAL = ['var(--primary)', 'var(--ink)', 'var(--slate)', 'var(--line)'];
const TOP_N = CATEGORICAL.length - 1; // 많은 순 셋 + 기타(넷째 색)
const OTHER: Series = { key: '__other', label: '기타', color: CATEGORICAL[TOP_N] };
const NO_TOOL_KEY = '__none'; // 도구 없는 기록 (질문 등). 순위에 들면 '도구 없음'으로 보인다

const failedOf = (record: AuditRecord) => record.status === 'error' || record.event === 'failed';

// 결과: 실패에는 변경 작업의 실행 실패(failed)도 든다. 범례를 누르면 거르기의 결과 값 여럿으로 거른다
const RESULT_SERIES: Series[] = [
    { key: 'error', label: '실패', color: 'var(--danger)', facet: 'result', values: ['error', 'failed'] },
    {
        key: 'ok',
        label: '성공',
        color: 'var(--primary)',
        facet: 'result',
        values: ['ok', 'requested', 'approved', 'denied', 'executed'],
    },
];

// 많은 순 n개 (같으면 이름 순)
const topKeys = (records: AuditRecord[], keyOf: (r: AuditRecord) => string | undefined) => {
    const counts = new Map<string, number>();
    for (const record of records) {
        const key = keyOf(record);
        if (key) counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, TOP_N).map(([key]) => key);
};

function seriesOf(groupBy: GroupBy, rankRecords: AuditRecord[]) {
    if (groupBy === 'kind') {
        const series = (Object.keys(KIND_LABELS) as (keyof typeof KIND_LABELS)[]).map((kind, index) => ({
            key: kind,
            label: KIND_LABELS[kind],
            color: CATEGORICAL[index],
            facet: 'kind' as const,
            values: [kind],
        }));
        return { series, keyOf: (record: AuditRecord) => record.kind as string };
    }
    if (groupBy === 'requester') {
        const top = topKeys(rankRecords, (record) => record.userId);
        const names = new Map(rankRecords.map((record) => [record.userId, requesterOf(record)]));
        const series: Series[] = top.map((userId, index) => ({
            key: userId,
            label: names.get(userId) ?? userId,
            color: CATEGORICAL[index],
            facet: 'requester',
            values: [userId],
        }));
        const set = new Set(top);
        return { series: [...series, OTHER], keyOf: (record: AuditRecord) => (set.has(record.userId) ? record.userId : OTHER.key) };
    }
    if (groupBy === 'tool') {
        const toolKey = (record: AuditRecord) => record.tool || NO_TOOL_KEY;
        const top = topKeys(rankRecords, toolKey);
        const series: Series[] = top.map((tool, index) =>
            tool === NO_TOOL_KEY
                ? { key: tool, label: '도구 없음', color: CATEGORICAL[index] } // 거를 값이 없어 누를 수 없다
                : { key: tool, label: toolLabelOf(tool), color: CATEGORICAL[index], facet: 'tool', values: [tool] },
        );
        const set = new Set(top);
        return {
            series: [...series, OTHER],
            keyOf: (record: AuditRecord) => (set.has(toolKey(record)) ? toolKey(record) : OTHER.key),
        };
    }
    return { series: RESULT_SERIES, keyOf: (record: AuditRecord) => (failedOf(record) ? 'error' : 'ok') };
}

const PLOT_H = 140; // 막대가 서는 높이
const TOP = 8; // 윗값 글자가 잘리지 않게 둔 여백
const AXIS_H = 22; // 아래 눈금 글자 자리
const GUTTER = 40; // 왼쪽 세로 눈금 글자 자리
const GAP = 2; // 막대 사이·쌓은 조각 사이의 틈 (바탕색)
const MAX_BAR = 24;
const RADIUS = 4;
const GROW_SPREAD_MS = 320; // 첫 막대와 끝 막대가 자라기 시작하는 때의 차이 (왼쪽부터 차례로)

interface Bucket {
    start: number;
    counts: Record<string, number>;
    total: number;
}

// 세로 눈금: 0부터 max를 덮는 깔끔한 간격(1·2·5 × 10^k)으로 서너 개
const yTicksOf = (max: number) => {
    if (max <= 0) return { top: 1, ticks: [0] };
    const raw = max / 3;
    const power = 10 ** Math.floor(Math.log10(raw));
    const step = Math.max(1, [1, 2, 5, 10].map((m) => m * power).find((v) => v >= raw) ?? raw);
    const top = Math.ceil(max / step) * step;
    const ticks: number[] = [];
    for (let v = 0; v <= top; v += step) ticks.push(v);
    return { top, ticks };
};

// 위쪽 두 모서리만 둥근 막대 (바닥은 각지게)
const barPath = (x: number, y: number, w: number, h: number, rounded: boolean) => {
    const r = rounded ? Math.min(RADIUS, w / 2, h) : 0;
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
};

export function AuditHistogram({
    records,
    rankRecords,
    window,
    loading,
    groupBy,
    onSelect,
    onFilter,
}: {
    records: AuditRecord[];
    rankRecords: AuditRecord[]; // 요청자·도구의 많은 순을 정할 기록 (받은 기록 전체)
    window: TimeWindow;
    loading: boolean; // 다시 불러오는 중: 앞 그래프를 흐리게 남겨 둔다
    groupBy: GroupBy;
    onSelect: (window: TimeWindow) => void;
    onFilter: (facet: FacetId, values: string[]) => void; // 범례를 눌렀다
}) {
    const wrap = useRef<HTMLDivElement>(null);
    const [width, setWidth] = useState(0);
    const [active, setActive] = useState<number | null>(null); // 마우스·키보드로 가리킨 칸
    const [drag, setDrag] = useState<{ x0: number; x1: number } | null>(null);

    useLayoutEffect(() => {
        const node = wrap.current;
        if (!node) return;
        const observer = new ResizeObserver((entries) => setWidth(entries[0]?.contentRect.width ?? 0));
        observer.observe(node);
        return () => observer.disconnect();
    }, []);

    const { series, keyOf } = useMemo(() => seriesOf(groupBy, rankRecords), [groupBy, rankRecords]);
    const size = bucketSizeOf(window);
    const first = bucketStart(window.from, size);
    const buckets = useMemo(() => {
        const count = Math.max(1, Math.ceil((window.to - first) / size));
        const list: Bucket[] = Array.from({ length: count }, (_, index) => ({
            start: first + index * size,
            counts: {},
            total: 0,
        }));
        for (const record of records) {
            const bucket = list[Math.floor((Date.parse(timeOf(record)) - first) / size)];
            if (!bucket) continue;
            const key = keyOf(record);
            bucket.counts[key] = (bucket.counts[key] ?? 0) + 1;
            bucket.total += 1;
        }
        return list;
    }, [records, first, window.to, size, keyOf]);

    // 자라기 효과를 다시 낼 때마다 바뀌는 번호 (기록·기간·칸 크기·그룹 기준이 바뀌면)
    const growNo = useRef(0);
    const growKey = useMemo(() => (growNo.current += 1), [records, first, size, groupBy]);

    const totals = useMemo(() => {
        const sums: Record<string, number> = {};
        for (const bucket of buckets) for (const [key, n] of Object.entries(bucket.counts)) sums[key] = (sums[key] ?? 0) + n;
        return sums;
    }, [buckets]);
    const total = Object.values(totals).reduce((sum, n) => sum + n, 0);
    const { top, ticks: yTicks } = yTicksOf(Math.max(0, ...buckets.map((b) => b.total)));

    const plotW = Math.max(0, width - GUTTER);
    const end = first + buckets.length * size; // 마지막 칸의 끝
    const slot = plotW / buckets.length;
    const barW = Math.max(1, Math.min(MAX_BAR, slot - GAP));
    const xOfTime = (at: number) => GUTTER + ((at - first) / (end - first)) * plotW;
    const xOf = (index: number) => GUTTER + index * slot + (slot - barW) / 2;
    const yOf = (value: number) => TOP + PLOT_H - (value / top) * PLOT_H;
    const indexAt = (x: number) => Math.min(buckets.length - 1, Math.max(0, Math.floor((x - GUTTER) / slot)));
    const baseY = TOP + PLOT_H;
    const xTicks = useMemo(() => (plotW > 0 ? ticksOf(first, end, plotW) : []), [first, end, plotW]);

    // 칸 하나 또는 여러 칸을 기간으로 (전체 기간 밖으로 나가지 않게)
    const selectBuckets = (from: number, to: number) =>
        onSelect({
            from: Math.max(window.from, buckets[from].start),
            to: Math.min(window.to, buckets[to].start + size),
        });

    const pointerX = (event: PointerEvent<SVGSVGElement>) => event.clientX - event.currentTarget.getBoundingClientRect().left;
    const interactive = total > 0 && !loading;

    const onPointerDown = (event: PointerEvent<SVGSVGElement>) => {
        if (event.button !== 0 || !interactive) return;
        event.preventDefault(); // 마우스로 누를 때는 포커스(테두리)를 옮기지 않는다. 키보드로 왔을 때만 테두리가 보인다
        event.currentTarget.setPointerCapture(event.pointerId);
        const x = pointerX(event);
        setDrag({ x0: x, x1: x });
    };
    const onPointerMove = (event: PointerEvent<SVGSVGElement>) => {
        if (!interactive) return;
        const x = pointerX(event);
        if (drag) setDrag({ ...drag, x1: x });
        // 막대 단위로 반응한다: 기록이 없는 칸 위에서는 아무것도 가리키지 않는다
        const index = indexAt(x);
        setActive(buckets[index].total > 0 ? index : null);
    };
    const onPointerUp = (event: PointerEvent<SVGSVGElement>) => {
        if (!drag) return;
        const x = pointerX(event);
        const [a, b] = [Math.min(drag.x0, x), Math.max(drag.x0, x)];
        setDrag(null);
        if (b - a > 4) selectBuckets(indexAt(a), indexAt(b)); // 드래그: 그 구간
        else selectBuckets(indexAt(x), indexAt(x)); // 누르기: 그 칸
    };

    const onKeyDown = (event: KeyboardEvent<SVGSVGElement>) => {
        if (!interactive) return;
        const current = active ?? buckets.length - 1;
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
            event.preventDefault();
            setActive(Math.min(buckets.length - 1, Math.max(0, current + (event.key === 'ArrowLeft' ? -1 : 1))));
        } else if (event.key === 'Home' || event.key === 'End') {
            event.preventDefault();
            setActive(event.key === 'Home' ? 0 : buckets.length - 1);
        } else if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            selectBuckets(current, current);
        }
    };
    // 키보드로 들어오면 기록이 있는 마지막 칸부터
    const onFocus = () => {
        if (active !== null || !interactive) return;
        const last = buckets.map((b) => b.total > 0).lastIndexOf(true);
        setActive(last >= 0 ? last : buckets.length - 1);
    };

    const shown = active !== null && interactive ? buckets[active] : null;
    const rangeText = (bucket: Bucket) => `${formatShort(bucket.start)} ~ ${formatShort(bucket.start + size)}`;
    // 말풍선: 막대 머리 높이에서 막대 오른쪽에 붙인다. 오른쪽 끝에 가까우면 왼쪽으로 뒤집는다
    const TOOLTIP_W = 180;
    const tooltipTop = shown ? Math.max(0, yOf(shown.total) - 6) : 0;
    const tooltipStyle =
        active !== null
            ? xOf(active) + barW + 10 + TOOLTIP_W <= width
                ? { left: xOf(active) + barW + 10, top: tooltipTop }
                : { left: xOf(active) - 10, top: tooltipTop, transform: 'translateX(-100%)' }
            : undefined;

    // 또렷하게 둘 막대: 마우스·키보드로 가리킨 막대
    const focus = shown ? active : null;

    // 드래그하는 동안: 고를 구간(칸 단위로 맞춘 것)의 시각
    const dragRange = drag
        ? (() => {
              const a = indexAt(Math.min(drag.x0, drag.x1));
              const b = indexAt(Math.max(drag.x0, drag.x1));
              return {
                  x: (Math.min(drag.x0, drag.x1) + Math.max(drag.x0, drag.x1)) / 2,
                  text: `${formatShort(Math.max(window.from, buckets[a].start))} ~ ${formatShort(
                      Math.min(window.to, buckets[b].start + size),
                  )}`,
              };
          })()
        : null;

    // 말풍선·알림에는 그 칸에 있는 계열만 (결과로 나눌 때는 0건도), 위에 쌓인 것부터
    const rowsOf = (bucket: Bucket) =>
        [...series].reverse().filter((s) => groupBy === 'result' || (bucket.counts[s.key] ?? 0) > 0);
    const summary = (bucket: Bucket) =>
        `${rangeText(bucket)}: ${rowsOf(bucket).map((s) => `${s.label} ${bucket.counts[s.key] ?? 0}건`).join(', ')}`;
    // 범례: 이 기간에 있는 계열만 (결과는 늘 둘 다). 결과는 성공·실패 순, 나머지는 색 차례(많은 순)대로 기타를 끝에
    const legend = (groupBy === 'result' ? [...series].reverse() : series).filter(
        (s) => groupBy === 'result' || (totals[s.key] ?? 0) > 0,
    );

    return (
        <div className={`audit-histogram${loading ? ' is-loading' : ''}`}>
            <ul className="audit-histogram-legend" aria-label="범례">
                {legend.map((s) => {
                    const count = totals[s.key] ?? 0;
                    const content = (
                        <>
                            <svg width="10" height="10" aria-hidden="true">
                                <rect width="10" height="10" rx="2" fill={s.color} />
                            </svg>
                            <span className="audit-histogram-legend-name">{s.label}</span>
                            <strong>{count.toLocaleString()}</strong>
                            {groupBy === 'result' && s.key === 'error' && total > 0 ? (
                                <span className="audit-muted">· {((count / total) * 100).toFixed(1)}%</span>
                            ) : null}
                        </>
                    );
                    return (
                        <li key={s.key}>
                            {s.facet && s.values && count > 0 ? (
                                <button
                                    type="button"
                                    onClick={() => onFilter(s.facet!, s.values!)}
                                    title={`${s.label}만 보기`}
                                >
                                    {content}
                                </button>
                            ) : (
                                <span>{content}</span>
                            )}
                        </li>
                    );
                })}
                {total > 0 ? (
                    <li className="audit-histogram-hint" aria-hidden="true">
                        드래그하거나 막대를 눌러 기간 좁히기
                    </li>
                ) : null}
            </ul>
            <div ref={wrap} className="audit-histogram-plot">
                {width > 0 ? (
                    <svg
                        // 커서: 평소에는 화살표, 기록이 있는 칸 위에서는 손가락(누르면 그 칸으로), 드래그 중에는 ↔
                        className={drag ? 'is-dragging' : shown && shown.total > 0 ? 'is-over-bar' : undefined}
                        width={width}
                        height={TOP + PLOT_H + AXIS_H}
                        tabIndex={interactive ? 0 : -1}
                        role="group"
                        aria-label="시간대별 기록 건수. ←/→로 시간대를 옮기고 Enter로 그 시간대만 봅니다"
                        onPointerDown={onPointerDown}
                        onPointerMove={onPointerMove}
                        onPointerUp={onPointerUp}
                        onPointerLeave={() => !drag && setActive(null)}
                        onKeyDown={onKeyDown}
                        onFocus={onFocus}
                        onBlur={() => setActive(null)}
                    >
                        {/* 세로 눈금: 가는 가로선과 왼쪽 글자 (0은 바닥선) */}
                        {yTicks.map((value) => (
                            <g key={`y${value}`}>
                                <line
                                    x1={GUTTER}
                                    x2={width}
                                    y1={yOf(value)}
                                    y2={yOf(value)}
                                    className={value === 0 ? 'audit-histogram-axis' : 'audit-histogram-grid'}
                                />
                                {total > 0 || value === 0 ? (
                                    <text x={GUTTER - 8} y={yOf(value) + 4} textAnchor="end" className="audit-histogram-tick">
                                        {value.toLocaleString()}
                                    </text>
                                ) : null}
                            </g>
                        ))}

                        {/* 가로 눈금: 날짜(굵게)와 시각 */}
                        {xTicks.map((tick) => {
                            const x = xOfTime(tick.at);
                            if (x > width - 20) return null; // 오른쪽 끝에 걸려 잘릴 글자는 그리지 않는다
                            return (
                                <g key={`x${tick.at}`}>
                                    <line x1={x} x2={x} y1={baseY} y2={baseY + 4} className="audit-histogram-axis" />
                                    <text
                                        x={x}
                                        y={baseY + 17}
                                        textAnchor="middle"
                                        className={`audit-histogram-tick${tick.isDate ? ' is-date' : ''}`}
                                    >
                                        {tick.label}
                                    </text>
                                </g>
                            );
                        })}

                        {/* 막대: 계열을 아래부터 쌓는다. 조각 사이에 틈, 맨 위 조각만 위 모서리가 둥글다.
                                묶음의 key가 바뀌면 새로 그려져 자라기 효과가 다시 난다 (막대마다 왼쪽부터 조금씩 늦게) */}
                        <g key={growKey} className={`audit-histogram-bars${focus !== null ? ' has-focus' : ''}`}>
                            {buckets.map((bucket, index) => {
                                if (!bucket.total) return null;
                                const x = xOf(index);
                                const parts = series.filter((s) => bucket.counts[s.key]);
                                let y = baseY;
                                return (
                                    <g
                                        key={bucket.start}
                                        className={`audit-histogram-bar${index === focus ? ' is-focus' : ''}`}
                                        style={{ animationDelay: `${Math.round((index / buckets.length) * GROW_SPREAD_MS)}ms` }}
                                    >
                                        {parts.map((s, i) => {
                                            const h = ((bucket.counts[s.key] ?? 0) / top) * PLOT_H;
                                            const gap = i > 0 ? GAP : 0;
                                            const shape = barPath(x, y - gap - h, barW, h, i === parts.length - 1);
                                            y -= gap + h;
                                            return <path key={s.key} d={shape} fill={s.color} />;
                                        })}
                                    </g>
                                );
                            })}
                        </g>

                        {/* 기록이 없으면 그래프 자리에 안내 (불러오는 중이면 비워 둔다) */}
                        {total === 0 && !loading ? (
                            <text x={GUTTER + plotW / 2} y={TOP + PLOT_H / 2} textAnchor="middle" className="audit-histogram-empty">
                                이 기간에 기록이 없습니다
                            </text>
                        ) : null}

                        {/* 드래그 중인 구간 */}
                        {drag ? (
                            <rect
                                x={Math.min(drag.x0, drag.x1)}
                                y={TOP}
                                width={Math.abs(drag.x1 - drag.x0)}
                                height={PLOT_H}
                                className="audit-histogram-brush"
                            />
                        ) : null}
                    </svg>
                ) : null}

                {dragRange ? (
                    <div className="audit-histogram-brush-label" style={{ left: dragRange.x }} aria-hidden="true">
                        {dragRange.text}
                    </div>
                ) : null}

                {shown && !drag ? (
                    <div className="audit-histogram-tooltip" style={tooltipStyle} aria-hidden="true">
                        <div className="audit-histogram-tooltip-time">{rangeText(shown)}</div>
                        {rowsOf(shown).length === 0 ? <div className="audit-muted">기록 없음</div> : null}
                        {rowsOf(shown).map((s) => (
                            <div key={s.key}>
                                <i style={{ background: s.color }} />
                                <strong>{(shown.counts[s.key] ?? 0).toLocaleString()}</strong> {s.label}
                            </div>
                        ))}
                    </div>
                ) : null}
                {/* 화면 읽기 프로그램: 가리킨 칸의 내용 */}
                <span className="sr-only" aria-live="polite">
                    {shown ? summary(shown) : ''}
                </span>
            </div>
        </div>
    );
}
