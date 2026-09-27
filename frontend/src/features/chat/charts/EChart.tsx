// ECharts로 차트 하나를 그린다. 이 파일과 ECharts는 차트가 있는 답변을 열 때만 불러온다 (ArtifactView의 lazy).
// - SVG로 그려 확대해도 선명하다. 마우스를 올리면 값이 보인다 (tooltip)
// - 쓰는 차트·부품만 등록해 크기를 줄인다 (echarts/core)
// - 번들에 넣어 쓰므로 CDN 등 밖으로 요청하지 않는다 (데이터가 브라우저 밖으로 나가지 않는다)
import { useEffect, useMemo, useRef } from 'react';
import { BarChart, GraphChart, LineChart, PieChart, RadarChart, ScatterChart, TreeChart, TreemapChart } from 'echarts/charts';
import { GridComponent, LegendComponent, RadarComponent, TitleComponent, TooltipComponent } from 'echarts/components';
import * as echarts from 'echarts/core';
import { SVGRenderer } from 'echarts/renderers';
import { type ChartSpec, chartHeight, chartOption, type ChartTheme } from './chartOption';
import { WordCloud } from './WordCloud';

echarts.use([
    BarChart, GraphChart, LineChart, PieChart, RadarChart, ScatterChart, TreeChart, TreemapChart,
    GridComponent, LegendComponent, RadarComponent, TitleComponent, TooltipComponent,
    SVGRenderer,
]);

// 앱의 색·글꼴 (styles.css의 변수)을 차트에도 쓴다. 계열 색은 파랑 세 단계 (주 색 → 중간 → 옅은 파랑, 셋째는 '기타'에도)
function appTheme(): ChartTheme {
    const css = getComputedStyle(document.documentElement);
    const read = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
    const ink = read('--ink', '#0a1628');
    const line = read('--line', '#d6dee8');
    return {
        ink,
        line,
        muted: read('--ink-3', '#596d87'),
        font: read('--font-body', 'sans-serif'),
        palette: [read('--chart-1', '#1e5aa8'), read('--chart-2', '#7fa0cf'), read('--chart-3', '#c9d7ea')],
        surface: read('--surface', '#ffffff'),
        soft: read('--primary-soft', '#e9eff6'),
    };
}

export default function EChart({ spec, onError }: { spec: ChartSpec; onError: () => void }) {
    const ref = useRef<HTMLDivElement>(null);
    const height = useMemo(() => chartHeight(spec), [spec]);

    useEffect(() => {
        const element = ref.current;
        if (!element || spec.type === 'word-cloud') return;
        let chart: echarts.ECharts | undefined;
        try {
            chart = echarts.init(element, undefined, { renderer: 'svg' });
            chart.setOption(chartOption(spec, appTheme()));
        } catch (error) {
            console.warn('차트를 그리지 못해 이미지로 보여 줍니다', error);
            chart?.dispose();
            onError();
            return;
        }
        // 화면 폭이 바뀌면 다시 맞춘다 (대화 창 크기, 모바일 회전)
        const observer = new ResizeObserver(() => chart?.resize());
        observer.observe(element);
        return () => {
            observer.disconnect();
            chart?.dispose();
        };
    }, [spec, onError]);

    if (spec.type === 'word-cloud') return <WordCloud spec={spec} height={height} theme={appTheme()} />;
    return <div ref={ref} className="artifact-chart-canvas" style={{ height }} />;
}
