// 워드 클라우드 (ECharts에는 없고, 확장 패키지는 ECharts 5 전용이라 직접 그린다).
// 값이 큰 단어부터 가운데에서 나선을 따라 겹치지 않는 자리에 놓는다 (서버 chart_utils._word_cloud와 같은 방식).
// 글자는 React가 SVG <text>에 글자로만 넣는다 (HTML로 해석하지 않는다).
import { useMemo } from 'react';
import type { ChartSpec, ChartTheme } from './chartOption';

const WIDTH = 640; // viewBox 폭 (화면에서는 폭에 맞춰 늘고 준다)
// 색: 크기가 이미 값을 보이므로 색은 세 단계만. 값이 큰 다섯 단어는 주 색, 위 30%까지는 남색, 나머지는 옅은 글자색
const colorOf = (rank: number, count: number, theme: ChartTheme) =>
    rank < 5 ? theme.palette[0] : rank < Math.max(5, count * 0.3) ? theme.ink : theme.muted;

interface Placed {
    text: string;
    value: number;
    x: number;
    y: number;
    size: number;
    color: string;
}

function layout(spec: ChartSpec, height: number, theme: ChartTheme): Placed[] {
    const font = theme.font;
    const words = (Array.isArray(spec.options.data) ? spec.options.data : [])
        .map((w: Record<string, unknown>) => ({ text: String(w.text ?? '').slice(0, 60), value: Number(w.value) || 0 }))
        .filter((w) => w.text)
        .sort((a, b) => b.value - a.value)
        .slice(0, 200);
    if (!words.length) return [];
    const low = words[words.length - 1].value;
    const high = words[0].value;
    const context = document.createElement('canvas').getContext('2d');
    const top = spec.options.title ? 36 : 8;
    const boxes: [number, number, number, number][] = [];
    const placed: Placed[] = [];
    words.forEach((word, index) => {
        const size = 12 + 34 * (high > low ? (word.value - low) / (high - low) : 1);
        if (context) context.font = `600 ${size}px ${font}`;
        const w = (context?.measureText(word.text).width ?? word.text.length * size * 0.6) + 6;
        const h = size * 1.15;
        for (let step = 0; step < 2000; step += 1) {
            const angle = step * 0.35;
            const radius = 1.4 * step * 0.35;
            const x = WIDTH / 2 + radius * Math.cos(angle) * 1.4;
            const y = (height + top) / 2 + radius * Math.sin(angle);
            const box: [number, number, number, number] = [x - w / 2, y - h / 2, x + w / 2, y + h / 2];
            const inside = box[0] >= 4 && box[2] <= WIDTH - 4 && box[1] >= top && box[3] <= height - 4;
            const free = boxes.every((b) => box[2] < b[0] || box[0] > b[2] || box[3] < b[1] || box[1] > b[3]);
            if (inside && free) {
                boxes.push(box);
                placed.push({ ...word, x, y, size, color: colorOf(index, words.length, theme) });
                break;
            }
        }
    });
    return placed;
}

export function WordCloud({ spec, height, theme }: { spec: ChartSpec; height: number; theme: ChartTheme }) {
    // theme은 그릴 때마다 새 객체라, 실제로 쓰는 값만 의존 목록에 둔다 (배치를 매번 다시 하지 않게)
    const words = useMemo(() => layout(spec, height, theme), [spec, height, theme.font, theme.palette[0], theme.ink, theme.muted]);
    const title = spec.options.title ? String(spec.options.title).slice(0, 60) : '';
    return (
        <svg className="artifact-chart-canvas" viewBox={`0 0 ${WIDTH} ${height}`} role="img" aria-label={title || '워드 클라우드'}
             style={{ height, width: '100%', fontFamily: theme.font }}>
            {title ? (
                <text x={WIDTH / 2} y={22} textAnchor="middle" fontSize={14} fontWeight={600} fill={theme.ink}>
                    {title}
                </text>
            ) : null}
            {words.map((word) => (
                <text key={word.text} x={word.x} y={word.y} textAnchor="middle" dominantBaseline="central"
                      fontSize={word.size} fontWeight={600} fill={word.color}>
                    <title>{`${word.text}: ${word.value}`}</title>
                    {word.text}
                </text>
            ))}
        </svg>
    );
}
