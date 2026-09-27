// 차트 도구의 그릴 내용(spec) → ECharts 설정.
// spec은 MCP 차트 도구(mcp/lambda_mcp/chart_utils.py)가 PNG를 그리며 검사를 마친 값이고, 같은 모양이다.
//   { type: 'line' | 'bar' | … , options: { data, title, axisXTitle, axisYTitle, stack, group, … } }
// 글자는 모델·로그에서 온 값이라 ECharts가 글자로만 그리게 한다 (HTML로 해석하는 formatter를 쓰지 않는다).
// 색: 주 색에서 옅어지는 파랑 세 단계만 쓴다 (styles.css --chart-1~3. 글자색·남색은 차트에 쓰지 않는다).
//     계열이 넷 이상이면 큰 둘만 남기고 나머지를 '기타'(셋째 색)로 묶는다 (색을 돌려 쓰지 않는다)
import type { EChartsCoreOption } from 'echarts/core';

export interface ChartSpec {
    type: string;
    options: Record<string, unknown>;
}

export interface ChartTheme {
    ink: string; // 글자
    line: string; // 격자·축
    muted: string; // 보조 글자
    font: string;
    palette: string[]; // 계열 색 (차례대로. 셋째는 '기타'에도 쓴다)
    surface: string; // 카드 바탕 (노드·이름표 바탕)
    soft: string; // 주 색을 옅게 푼 바탕 (강조 노드)
}

export const MAX_SERIES = 3; // 이보다 많으면 큰 둘 + '기타'
const OTHER = '기타';

type Item = Record<string, unknown>;

const MAX_LABEL = 60;
const text = (value: unknown) => {
    const s = value === null || value === undefined ? '' : String(value);
    return s.length > MAX_LABEL ? `${s.slice(0, MAX_LABEL - 1)}…` : s;
};
const num = (value: unknown) => {
    const n = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(n) ? n : 0;
};
const list = (value: unknown): Item[] => (Array.isArray(value) ? (value as Item[]) : []);

// 계열(그룹)이 MAX_SERIES보다 많으면 합계가 큰 것부터 MAX_SERIES - 1개만 남기고 나머지를 '기타' 하나로 더한다
function foldGroups(groups: Map<string, Map<string, number>>) {
    if (groups.size <= MAX_SERIES) return groups;
    const total = (g: Map<string, number>) => [...g.values()].reduce((a, b) => a + b, 0);
    const ranked = [...groups.entries()].sort((a, b) => total(b[1]) - total(a[1]));
    const folded = new Map(ranked.slice(0, MAX_SERIES - 1));
    const other = new Map<string, number>();
    for (const [, values] of ranked.slice(MAX_SERIES - 1)) {
        for (const [x, v] of values) other.set(x, (other.get(x) ?? 0) + v);
    }
    folded.set(OTHER, other);
    return folded;
}

// [{x키, value, group?}] → x 순서와 그룹별 값 (서버 _series와 같다)
function series(data: Item[], key: string) {
    const xs: string[] = [];
    const groups = new Map<string, Map<string, number>>();
    for (const item of data) {
        const x = text(item[key]);
        if (!xs.includes(x)) xs.push(x);
        const group = text(item.group ?? '');
        const values = groups.get(group) ?? new Map<string, number>();
        values.set(x, (values.get(x) ?? 0) + num(item.value));
        groups.set(group, values);
    }
    return { xs, groups: foldGroups(groups) };
}

function base(options: Item, theme: ChartTheme): EChartsCoreOption {
    return {
        animationDuration: 400,
        color: theme.palette,
        textStyle: { fontFamily: theme.font, color: theme.ink },
        title: options.title
            ? { text: text(options.title), left: 'center', top: 4, textStyle: { fontSize: 14, fontWeight: 600 } }
            : undefined,
        tooltip: { confine: true },
    };
}

const axisStyle = (theme: ChartTheme) => ({
    axisLine: { lineStyle: { color: theme.line } },
    axisTick: { show: false },
    axisLabel: { color: theme.muted, hideOverlap: true },
    splitLine: { lineStyle: { color: theme.line } },
    nameTextStyle: { color: theme.muted },
});

const legendFor = (names: string[]) =>
    names.length > 1 || names[0] ? { top: 28, type: 'scroll' as const, data: names.map((n) => n || '값') } : undefined;

// containLabel은 눈금 글자만 셈하고 축 제목(name)은 세지 않는다: x축 제목이 있으면 아래를 비워 둔다
const grid = (options: Item) => ({
    left: 16,
    right: 24,
    bottom: options.axisXTitle ? 34 : 12,
    top: options.title ? 64 : 40,
    containLabel: true,
});

// ---------------------------------------------------------------- 축이 있는 차트

function lineOrArea(options: Item, theme: ChartTheme, area: boolean): EChartsCoreOption {
    const { xs, groups } = series(list(options.data), 'time');
    const names = [...groups.keys()];
    return {
        ...base(options, theme),
        tooltip: { trigger: 'axis', confine: true },
        legend: legendFor(names),
        grid: grid(options),
        xAxis: { type: 'category', data: xs, boundaryGap: false, name: text(options.axisXTitle), nameLocation: 'middle',
                 nameGap: 28, ...axisStyle(theme), splitLine: { show: false } },
        yAxis: { type: 'value', name: text(options.axisYTitle), ...axisStyle(theme) },
        series: names.map((name) => ({
            type: 'line',
            name: name || '값',
            data: xs.map((x) => groups.get(name)?.get(x) ?? 0),
            smooth: 0.2,
            symbolSize: 5,
            stack: options.stack ? 'total' : undefined,
            areaStyle: area ? { opacity: 0.25 } : undefined,
            emphasis: { focus: 'series' },
        })),
    };
}

function bars(options: Item, theme: ChartTheme, horizontal: boolean): EChartsCoreOption {
    const { xs, groups } = series(list(options.data), 'category');
    const names = [...groups.keys()];
    const stack = Boolean(options.stack) && names.length > 1;
    const category = { type: 'category', data: xs, ...axisStyle(theme), splitLine: { show: false } };
    const value = { type: 'value', ...axisStyle(theme) };
    const xName = { name: text(options.axisXTitle), nameLocation: 'middle', nameGap: 28 };
    const yName = { name: text(options.axisYTitle) };
    return {
        ...base(options, theme),
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, confine: true },
        legend: legendFor(names),
        grid: grid(options),
        // 가로 막대는 첫 항목이 위에 오게 (inverse)
        xAxis: horizontal ? { ...value, ...xName } : { ...category, ...xName },
        yAxis: horizontal ? { ...category, inverse: true, ...yName } : { ...value, ...yName },
        series: names.map((name) => ({
            type: 'bar',
            name: name || '값',
            data: xs.map((x) => groups.get(name)?.get(x) ?? 0),
            stack: stack ? 'total' : undefined,
            barMaxWidth: 36,
            itemStyle: { borderRadius: horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0] },
            emphasis: { focus: 'series' },
        })),
    };
}

// 원 조각: 합이 큰 둘만 남기고 나머지는 '기타' 한 조각 (조각이 MAX_SERIES 이하면 그대로)
function pieSlices(xs: string[], groups: Map<string, Map<string, number>>) {
    const slices = xs
        .map((x) => ({ name: x, value: [...groups.values()].reduce((sum, g) => sum + (g.get(x) ?? 0), 0) }))
        .sort((a, b) => b.value - a.value);
    if (slices.length <= MAX_SERIES) return slices;
    const rest = slices.slice(MAX_SERIES - 1).reduce((sum, s) => sum + s.value, 0);
    return [...slices.slice(0, MAX_SERIES - 1), { name: OTHER, value: rest }];
}

function pie(options: Item, theme: ChartTheme): EChartsCoreOption {
    const { xs, groups } = series(list(options.data), 'category');
    const inner = Math.min(Math.max(num(options.innerRadius), 0), 0.9);
    return {
        ...base(options, theme),
        tooltip: { trigger: 'item', confine: true },
        legend: { bottom: 0, type: 'scroll' },
        series: [{
            type: 'pie',
            radius: [`${Math.round(inner * 65)}%`, '65%'],
            center: ['50%', options.title ? '54%' : '48%'],
            data: pieSlices(xs, groups),
            label: { formatter: '{b}\n{d}%', color: theme.ink },
            itemStyle: { borderColor: theme.surface, borderWidth: 2, borderRadius: inner ? 4 : 0 },
        }],
    };
}

function scatter(options: Item, theme: ChartTheme): EChartsCoreOption {
    return {
        ...base(options, theme),
        tooltip: { trigger: 'item', confine: true },
        grid: grid(options),
        xAxis: { type: 'value', name: text(options.axisXTitle), nameLocation: 'middle', nameGap: 28, scale: true,
                 ...axisStyle(theme) },
        yAxis: { type: 'value', name: text(options.axisYTitle), scale: true, ...axisStyle(theme) },
        series: [{ type: 'scatter', symbolSize: 8, data: list(options.data).map((i) => [num(i.x), num(i.y)]) }],
    };
}

function histogram(options: Item, theme: ChartTheme): EChartsCoreOption {
    const values = (Array.isArray(options.data) ? options.data : []).map(num);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const count = Math.min(Math.max(num(options.binNumber) || Math.ceil(Math.log2(values.length) + 1), 1), 200);
    const width = (max - min) / count || 1;
    const bins = Array.from({ length: count }, () => 0);
    for (const v of values) bins[Math.min(Math.floor((v - min) / width), count - 1)] += 1;
    const round = (n: number) => Number(n.toPrecision(4));
    return {
        ...base(options, theme),
        tooltip: { trigger: 'axis', confine: true },
        grid: grid(options),
        xAxis: { type: 'category', name: text(options.axisXTitle), nameLocation: 'middle', nameGap: 28,
                 data: bins.map((_, i) => `${round(min + i * width)}–${round(min + (i + 1) * width)}`),
                 ...axisStyle(theme), splitLine: { show: false } },
        yAxis: { type: 'value', name: text(options.axisYTitle) || '개수', ...axisStyle(theme) },
        series: [{ type: 'bar', data: bins, barCategoryGap: '4%', name: '개수' }],
    };
}

function radar(options: Item, theme: ChartTheme): EChartsCoreOption {
    const { xs, groups } = series(list(options.data), 'name');
    const all = [...groups.values()].flatMap((g) => [...g.values()]);
    const max = Math.max(...all, 0) * 1.1 || 1;
    const names = [...groups.keys()];
    return {
        ...base(options, theme),
        tooltip: { trigger: 'item', confine: true },
        legend: legendFor(names),
        radar: {
            indicator: xs.map((name) => ({ name, max })),
            center: ['50%', '56%'],
            radius: '62%',
            axisName: { color: theme.ink },
            splitLine: { lineStyle: { color: theme.line } },
            axisLine: { lineStyle: { color: theme.line } },
            splitArea: { show: false },
        },
        series: [{
            type: 'radar',
            data: names.map((name) => ({ name: name || '값', value: xs.map((x) => groups.get(name)?.get(x) ?? 0),
                                         areaStyle: { opacity: 0.2 } })),
        }],
    };
}

function dualAxes(options: Item, theme: ChartTheme): EChartsCoreOption {
    const categories = (Array.isArray(options.categories) ? options.categories : []).map(text);
    const items = list(options.series);
    const columns = items.filter((s) => s.type === 'column');
    const lines = items.filter((s) => s.type === 'line');
    const leftName = text(columns[0]?.axisYTitle ?? lines[0]?.axisYTitle ?? '');
    const rightName = columns.length && lines.length ? text(lines[0]?.axisYTitle ?? '') : '';
    return {
        ...base(options, theme),
        tooltip: { trigger: 'axis', confine: true },
        legend: { top: 28 },
        grid: grid(options),
        xAxis: { type: 'category', data: categories, name: text(options.axisXTitle), nameLocation: 'middle',
                 nameGap: 28, ...axisStyle(theme), splitLine: { show: false } },
        yAxis: [
            { type: 'value', name: leftName, ...axisStyle(theme) },
            ...(rightName !== '' || (columns.length && lines.length)
                ? [{ type: 'value', name: rightName, ...axisStyle(theme), splitLine: { show: false } }]
                : []),
        ],
        series: items.map((s, i) => ({
            type: s.type === 'column' ? 'bar' : 'line',
            name: text(s.axisYTitle ?? `${s.type} ${i + 1}`),
            data: (Array.isArray(s.data) ? s.data : []).map(num),
            yAxisIndex: s.type === 'line' && columns.length ? 1 : 0,
            barMaxWidth: 36,
            smooth: 0.2,
            itemStyle: s.type === 'column' ? { borderRadius: [4, 4, 0, 0] } : undefined,
        })),
    };
}

// ---------------------------------------------------------------- 트리·그래프

interface TreeNode {
    name: string;
    value?: number;
    children?: TreeNode[];
}

const tree = (node: unknown, depth = 0): TreeNode => {
    const item = (node && typeof node === 'object' ? node : {}) as Item;
    const children = depth < 10 ? list(item.children).map((c) => tree(c, depth + 1)) : [];
    return {
        name: text(item.name),
        ...(item.value !== undefined && item.value !== null ? { value: num(item.value) } : {}),
        ...(children.length ? { children } : {}),
    };
};

function treemap(options: Item, theme: ChartTheme): EChartsCoreOption {
    return {
        ...base(options, theme),
        tooltip: { confine: true },
        series: [{
            type: 'treemap',
            top: options.title ? 40 : 8,
            bottom: 8,
            left: 8,
            right: 8,
            roam: false,
            nodeClick: false,
            breadcrumb: { show: false },
            data: list(options.data).map((n) => tree(n)),
            label: { show: true, formatter: '{b}' },
            // 부모 칸의 이름 띠: 바탕(테두리 색)이 흰색이라 글자는 남색
            upperLabel: { show: true, height: 22, color: theme.ink },
            // 주 색 한 가지. 칸마다 이름이 있어 색으로 나누지 않는다 (채도를 바꾸면 팔레트 밖의 파랑이 생겨 쓰지 않는다)
            levels: [
                { color: [theme.palette[0]], itemStyle: { borderColor: theme.surface, borderWidth: 3, gapWidth: 3 } },
                { itemStyle: { borderColor: theme.surface, gapWidth: 2, borderWidth: 2 } },
            ],
        }],
    };
}

function mindMap(options: Item, theme: ChartTheme): EChartsCoreOption {
    return {
        ...base(options, theme),
        tooltip: { show: false },
        series: [{
            type: 'tree',
            data: [tree(options.data)],
            orient: 'LR',
            layout: 'orthogonal',
            edgeShape: 'curve',
            initialTreeDepth: -1,
            expandAndCollapse: false,
            top: 24,
            bottom: 24,
            left: '12%',
            right: '18%',
            symbol: 'circle',
            symbolSize: 8,
            lineStyle: { color: theme.line, width: 1.5, curveness: 0.5 },
            label: {
                position: 'left',
                align: 'right',
                verticalAlign: 'middle',
                fontSize: 12,
                backgroundColor: theme.surface,
                borderColor: theme.line,
                borderWidth: 1,
                borderRadius: 6,
                padding: [4, 8],
            },
            leaves: { label: { position: 'right', align: 'left' } },
        }],
    };
}

interface Graph {
    names: string[];
    edges: { source: string; target: string; name: string }[];
}

function graphOf(options: Item): Graph {
    const data = (options.data && typeof options.data === 'object' ? options.data : {}) as Item;
    const names = list(data.nodes).map((n) => text(n.name));
    const edges = list(data.edges).map((e) => ({ source: text(e.source), target: text(e.target), name: text(e.name) }));
    for (const e of edges) for (const n of [e.source, e.target]) if (!names.includes(n)) names.push(n);
    return { names: [...new Set(names)], edges };
}

// 간선 이름: 선을 따라 눕지 않게 가운데에 가로 글자로 따로 둔다 (크기 0인 노드의 이름표)
const edgeLabelNode = (name: string, text: string, x: number, y: number, theme: ChartTheme, position = 'inside') => ({
    name,
    x,
    y,
    symbolSize: 0,
    label: { show: true, formatter: text, position, color: theme.muted, fontSize: 11, backgroundColor: theme.surface,
             padding: [1, 4], borderRadius: 3 },
});

// 네트워크 그래프: 노드를 원 위에 고르게 둔다 (힘 기반 배치는 노드가 적으면 한쪽으로 몰리고 매번 모양이 바뀐다)
function networkGraph(options: Item, theme: ChartTheme): EChartsCoreOption {
    const { names, edges } = graphOf(options);
    const radius = 160;
    const position = new Map(names.map((name, i) => {
        const angle = (2 * Math.PI * i) / names.length - Math.PI / 2;
        return [name, [radius * Math.cos(angle), radius * Math.sin(angle)] as [number, number]];
    }));
    const nodes: Item[] = names.map((name) => ({
        name,
        x: position.get(name)![0],
        y: position.get(name)![1],
        symbol: 'roundRect',
        symbolSize: [Math.min(24 + name.length * 8, 150), 30],
        itemStyle: { color: theme.surface, borderWidth: 1.5, borderColor: theme.palette[0] }, // 노드는 모두 같은 테두리 (이름이 구분한다)
        label: { show: true, color: theme.ink, fontSize: 12, width: 140, overflow: 'truncate' },
    }));
    edges.forEach((e, i) => {
        if (!e.name) return;
        const [x0, y0] = position.get(e.source)!;
        const [x1, y1] = position.get(e.target)!;
        nodes.push(edgeLabelNode(`\u0000edge${i}`, e.name, (x0 + x1) / 2, (y0 + y1) / 2, theme));
    });
    return {
        ...base(options, theme),
        tooltip: { show: false },
        series: [{
            type: 'graph',
            layout: 'none',
            // 좌표를 정해 둔 그림이라 가로·세로를 같은 비율로 맞춘다 (따로 늘이면 곡선·이름이 옆으로 벌어진다)
            preserveAspect: 'contain',
            roam: true,
            top: options.title ? 56 : 32,
            bottom: 32,
            left: 90,
            right: 90,
            data: nodes,
            links: edges.map((e) => ({ source: e.source, target: e.target })),
            lineStyle: { color: theme.muted, width: 1.3, opacity: 0.8 },
            silent: true,
        }],
    };
}

const FLOW_GAP_X = 180; // 흐름도 칸 사이 (그래프 좌표. 화면 크기에 맞춰 늘고 준다)
const FLOW_GAP_Y = 90;

// 흐름도: 위에서 아래로. 되돌아가는 간선(반복)을 빼고 가장 긴 경로의 깊이에 둔다 (서버 _flow_diagram과 같다)
export function flowLayout({ names, edges }: Graph) {
    const children = new Map(names.map((n) => [n, [] as string[]]));
    for (const e of edges) children.get(e.source)?.push(e.target);
    const targets = new Set(edges.map((e) => e.target));
    const state = new Map<string, 'open' | 'done'>();
    const back = new Set<string>();
    const roots = names.filter((n) => !targets.has(n));
    for (const root of [...(roots.length ? roots : names.slice(0, 1)), ...names]) {
        if (state.has(root)) continue;
        const stack: [string, number][] = [[root, 0]];
        state.set(root, 'open');
        while (stack.length) {
            const top = stack[stack.length - 1];
            const next = children.get(top[0])?.[top[1]];
            top[1] += 1;
            if (next === undefined) {
                state.set(top[0], 'done');
                stack.pop();
            } else if (state.get(next) === 'open') {
                back.add(`${top[0]}\u0000${next}`);
            } else if (!state.has(next)) {
                state.set(next, 'open');
                stack.push([next, 0]);
            }
        }
    }
    const isBack = (s: string, t: string) => s === t || back.has(`${s}\u0000${t}`);
    const depth = new Map(names.map((n) => [n, 0]));
    for (let round = 0; round < names.length; round += 1) {
        let changed = false;
        for (const e of edges) {
            if (isBack(e.source, e.target)) continue;
            const d = (depth.get(e.source) ?? 0) + 1;
            if ((depth.get(e.target) ?? 0) < d) {
                depth.set(e.target, d);
                changed = true;
            }
        }
        if (!changed) break;
    }
    const rows = new Map<number, string[]>();
    for (const n of names) rows.set(depth.get(n) ?? 0, [...(rows.get(depth.get(n) ?? 0) ?? []), n]);
    const position = new Map<string, [number, number]>();
    for (const [level, row] of rows) {
        row.forEach((n, i) => position.set(n, [(i - (row.length - 1) / 2) * FLOW_GAP_X, level * FLOW_GAP_Y]));
    }
    return { depth, position, isBack };
}

function flowDiagram(options: Item, theme: ChartTheme): EChartsCoreOption {
    const graph = graphOf(options);
    const { depth, position, isBack } = flowLayout(graph);
    const nodes: Item[] = graph.names.map((name) => ({
        name,
        x: position.get(name)?.[0] ?? 0,
        y: position.get(name)?.[1] ?? 0,
        symbol: 'roundRect',
        symbolSize: [120, 34],
        itemStyle: {
            color: theme.surface,
            borderWidth: 1.5,
            borderColor: theme.palette[0], // 단계마다 색을 바꾸지 않는다 (위아래 위치가 단계를 보인다)
        },
        label: { show: true, color: theme.ink, fontSize: 12, width: 108, overflow: 'truncate' },
    }));
    const links: Item[] = [];
    graph.edges.forEach((e, i) => {
        const [x0, y0] = position.get(e.source) ?? [0, 0];
        const [x1, y1] = position.get(e.target) ?? [0, 0];
        const back = isBack(e.source, e.target);
        const skip = (depth.get(e.target) ?? 0) - (depth.get(e.source) ?? 0) > 1;
        // 되돌아가는 간선은 왼쪽 점선 곡선, 단계를 건너뛰는 간선은 오른쪽 곡선 (사이 노드를 지나는 직선과 겹치지 않게).
        // 같은 값이라도 선의 방향(위·아래)에 따라 휘는 쪽이 바뀌어, 둘 다 같은 값을 쓴다
        const curve = back || skip ? 0.5 : 0;
        links.push({ source: e.source, target: e.target,
                     lineStyle: back ? { type: 'dashed', curveness: curve } : { curveness: curve } });
        if (!e.name) return;
        // 간선 이름은 선을 따라 눕지 않게 따로 둔다: 직선은 가운데 오른쪽, 곡선은 가장 멀리 나간 곳 바깥
        const length = Math.hypot(x1 - x0, y1 - y0);
        const side = back ? -1 : 1;
        const bulge = curve ? (curve * length) / 2 + 16 : 0;
        nodes.push(edgeLabelNode(`\u0000edge${i}`, e.name, (x0 + x1) / 2 + side * (bulge || 10), (y0 + y1) / 2,
                                 theme, side > 0 ? 'right' : 'left'));
    });
    return {
        ...base(options, theme),
        tooltip: { show: false },
        series: [{
            type: 'graph',
            layout: 'none',
            // 좌표를 정해 둔 그림이라 가로·세로를 같은 비율로 맞춘다 (따로 늘이면 곡선·이름이 옆으로 벌어진다)
            preserveAspect: 'contain',
            roam: true,
            top: options.title ? 48 : 24,
            bottom: 24,
            left: 80,
            right: 80,
            data: nodes,
            links,
            edgeSymbol: ['none', 'arrow'],
            edgeSymbolSize: 8,
            lineStyle: { color: theme.muted, width: 1.3 },
            silent: true,
        }],
    };
}

// 차트 높이 (픽셀). 트리·흐름도는 단계·잎 수에 맞춘다. 나머지는 도구가 받은 height를 쓴다
export function chartHeight(spec: ChartSpec): number {
    const clamp = (h: number) => Math.min(Math.max(Math.round(h), 260), 720);
    const options = spec.options ?? {};
    if (spec.type === 'flow-diagram') {
        const { depth } = flowLayout(graphOf(options));
        return clamp((Math.max(0, ...depth.values()) + 1) * FLOW_GAP_Y + 60);
    }
    if (spec.type === 'mind-map') {
        const leaves = (node: TreeNode): number =>
            node.children?.length ? node.children.reduce((sum, c) => sum + leaves(c), 0) : 1;
        return clamp(leaves(tree(options.data)) * 38 + 60);
    }
    return clamp(Math.min(num(options.height) || 400, 520));
}

// 피시본: 머리(문제)는 오른쪽, 원인은 등뼈 위아래로 번갈아. 좌표를 정해 그래프 선으로 그린다
function fishbone(options: Item, theme: ChartTheme): EChartsCoreOption {
    const root = tree(options.data);
    const causes = root.children?.length ? root.children : [{ name: '(원인 없음)' }];
    const gap = 200;
    const count = Math.ceil(causes.length / 2);
    const nodes: Item[] = [];
    const links: Item[] = [];
    const point = (name: string, x: number, y: number) => nodes.push({ name, x, y, symbolSize: 0, label: { show: false } });
    point('__tail', 0, 0);
    nodes.push({ name: root.name, x: count * gap + 90, y: 0, symbol: 'roundRect', symbolSize: [130, 36],
                 itemStyle: { color: theme.soft, borderColor: theme.palette[0], borderWidth: 1.5 }, // 머리(문제): 옅은 파랑 바탕, 주 색 테두리
                 label: { show: true, fontWeight: 600, color: theme.ink } });
    links.push({ source: '__tail', target: root.name, lineStyle: { width: 3, color: theme.ink } });
    causes.forEach((cause, i) => {
        const side = i % 2 === 0 ? -1 : 1; // ECharts는 y가 아래로 커진다: 짝수는 위
        const x = (Math.floor(i / 2) + 1) * gap;
        const top: [number, number] = [x - 70, side * 120];
        const joint = `__joint${i}`;
        point(joint, x, 0);
        nodes.push({ name: `${cause.name}​${i}`, x: top[0], y: top[1] + side * 14, symbol: 'roundRect',
                     symbolSize: [110, 28], itemStyle: { color: theme.surface, borderColor: theme.line, borderWidth: 1.5 },
                     label: { show: true, formatter: cause.name, color: theme.ink } });
        links.push({ source: `${cause.name}​${i}`, target: joint, lineStyle: { color: theme.muted, width: 1.5 } });
        (cause.children ?? []).slice(0, 6).forEach((sub, j, subs) => {
            const t = (j + 1) / (subs.length + 1);
            const bx = top[0] + (x - top[0]) * t;
            const by = top[1] * (1 - t);
            point(`__bone${i}_${j}`, bx, by);
            nodes.push({ name: `${sub.name}​${i}_${j}`, x: bx - 60, y: by, symbolSize: 0,
                         label: { show: true, formatter: sub.name, position: 'left', color: theme.muted, fontSize: 11 } });
            links.push({ source: `${sub.name}​${i}_${j}`, target: `__bone${i}_${j}`,
                         lineStyle: { color: theme.line, width: 1 } });
        });
    });
    return {
        ...base(options, theme),
        tooltip: { show: false },
        // 좌표는 노드의 가운데라, 오른쪽 머리 상자(폭 130)가 들어오게 오른쪽을 비운다
        series: [{ type: 'graph', layout: 'none', preserveAspect: 'contain', roam: true, top: 40, bottom: 24, left: 60, right: 80,
                   data: nodes, links, label: { fontSize: 12 }, silent: true }],
    };
}

type Builder = (options: Item, theme: ChartTheme) => EChartsCoreOption;

const BUILDERS: Record<string, Builder> = {
    line: (o, t) => lineOrArea(o, t, false),
    area: (o, t) => lineOrArea(o, t, true),
    bar: (o, t) => bars(o, t, true),
    column: (o, t) => bars(o, t, false),
    pie,
    scatter,
    histogram,
    radar,
    'dual-axes': dualAxes,
    treemap,
    'mind-map': mindMap,
    'network-graph': networkGraph,
    'flow-diagram': flowDiagram,
    'fishbone-diagram': fishbone,
};

// 워드 클라우드는 ECharts에 없어(확장은 ECharts 5 전용) WordCloud.tsx가 SVG로 그린다
export const isEChartsType = (type: string) => type in BUILDERS;

export function chartOption(spec: ChartSpec, theme: ChartTheme): EChartsCoreOption {
    const build = BUILDERS[spec.type];
    if (!build) throw new Error(`unknown chart type: ${spec.type}`);
    return build(spec.options ?? {}, theme);
}
