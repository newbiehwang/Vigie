"""차트를 이 Lambda 안에서 그려 다이어그램 버킷에 올린다 (matplotlib).

    차트 도구(app.py generate_*_chart 등 15개) ──▶ generate_chart_url(종류, 옵션)
        ├─ render_chart: matplotlib으로 PNG를 그린다 (한글 글꼴: Noto Sans CJK, Dockerfile)
        ├─ 다이어그램 버킷(charts/날짜/…)에 올리고 24시간짜리 presigned URL을 돌려준다 (아키텍처 다이어그램과 같다)
        └─ 그릴 내용(spec)도 돌려준다: 웹 화면은 PNG 대신 ECharts로 다시 그린다 (선명하고 값을 짚어 볼 수 있다).
           PNG는 Slack, 오래된 대화, spec이 너무 큰 경우에 쓴다

예전에는 차트 데이터를 외부 차트 서버(AntV GPT-Vis, antv-studio.alipay.com)에 보내 이미지를 받았다.
차트에는 조회한 AWS 데이터가 들어가므로 계정 밖 제3자에게 데이터가 나갔다 (docs/threat-model.md R1).
이제 이 모듈은 네트워크를 쓰지 않는다: S3 업로드 말고는 밖으로 나가는 요청이 없다 (테스트가 확인한다).

도구의 이름·인자·결과 모양({"status", "url", "chart_type", "message"})은 예전과 같다.
입력은 모델이 만든 값이라 크기를 제한한다 (그림 크기, 항목 수, 글자 길이).
"""
import io
import json
import math
import os
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

# Lambda는 /tmp만 쓸 수 있다. matplotlib이 글꼴 캐시를 쓸 곳을 먼저 정한다
os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.environ.get("TMPDIR", "/tmp"), "matplotlib"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")  # 화면 없이 PNG만 그린다
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from .diagram_utils import DIAGRAM_BUCKET, s3_client  # noqa: E402 (아키텍처 다이어그램과 같은 버킷·클라이언트)

PRESIGNED_SECONDS = 86400  # 아키텍처 다이어그램과 같이 24시간
DPI = 150
MIN_SIDE, MAX_SIDE = 300, 2000  # 그림 한 변 (픽셀, DPI 100 기준)
MAX_ITEMS = 1000  # 데이터 항목 수
MAX_NODES = 150  # 트리·그래프의 노드 수
MAX_LABEL = 60  # 글자 하나의 길이
# 브라우저가 다시 그릴 수 있게 그릴 내용(spec)도 돌려준다 (frontend EChart). 대화 기록(DynamoDB 항목 400KB)에
# 함께 저장되므로 크기를 제한하고, 넘으면 PNG만 쓴다
SPEC_LIMIT = 16000

# 글꼴: 웹과 같은 Pretendard (Dockerfile이 이미지에 넣는다). 없으면 한글 글꼴을 차례로 찾는다
KOREAN_FONTS = ["Pretendard", "Noto Sans CJK KR", "Noto Sans CJK JP", "NanumGothic", "Malgun Gothic", "AppleGothic",
                "Apple SD Gothic Neo"]
# 색: 웹과 같은 Midnight Ink 팔레트 (frontend/src/styles.css). 계열은 차례가 정해진 넷만 쓴다:
# 주 색 → 남색 → 회청색 → 옅은 선 색(넷째·'기타'). 계열이 다섯 이상이면 큰 셋 + '기타'로 묶는다 (색을 돌려 쓰지 않는다)
INK, INK_2, INK_3 = "#0A1628", "#37485E", "#596D87"  # 글자 · 설명 글 · 작은 글씨
SLATE, LINE, SOFT = "#7B93B0", "#D6DEE8", "#E9EFF6"  # 아이콘·보조 선 · 테두리·격자 · 주 색을 옅게 푼 바탕
PRIMARY = "#1E5AA8"
PALETTE = [PRIMARY, INK, SLATE, LINE]
MAX_SERIES = len(PALETTE)
OTHER = "기타"


class ChartError(ValueError):
    """모델이 넘긴 데이터로 그릴 수 없다 (모델이 읽고 고쳐 다시 부르도록 이유를 알려 준다)."""


def _setup_font() -> None:
    names = {font.name for font in font_manager.fontManager.ttflist}
    family = next((name for name in KOREAN_FONTS if name in names), None)
    if family:
        plt.rcParams["font.family"] = family
    plt.rcParams["axes.unicode_minus"] = False  # 한글 글꼴에는 유니코드 빼기 기호가 없을 수 있다
    # 글자·축·격자도 웹의 색으로 (기본값은 검정)
    plt.rcParams.update({
        "text.color": INK, "axes.labelcolor": INK_3, "axes.titlecolor": INK,
        "xtick.color": INK_3, "ytick.color": INK_3, "axes.edgecolor": LINE, "grid.color": LINE,
    })


_setup_font()


# ---------------------------------------------------------------- 공통

def _text(value: Any) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= MAX_LABEL else text[:MAX_LABEL - 1] + "…"


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool):
        raise ChartError(f"{what} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ChartError(f"{what} must be a number, got {value!r}")
    if not math.isfinite(number):
        raise ChartError(f"{what} must be a finite number")
    return number


def _items(data: Any, limit: int = MAX_ITEMS) -> List[Any]:
    if not isinstance(data, list) or not data:
        raise ChartError("data must be a non-empty list")
    if len(data) > limit:
        raise ChartError(f"too many items ({len(data)} > {limit})")
    return data


def _figure(options: Dict[str, Any], polar: bool = False):
    width = min(max(int(options.get("width") or 600), MIN_SIDE), MAX_SIDE)
    height = min(max(int(options.get("height") or 400), MIN_SIDE), MAX_SIDE)
    fig = plt.figure(figsize=(width / 100, height / 100), dpi=DPI)
    ax = fig.add_subplot(111, polar=polar)
    if options.get("title"):
        ax.set_title(_text(options["title"]), fontsize=12, pad=12)
    return fig, ax


def _axis_titles(ax, options: Dict[str, Any]) -> None:
    if options.get("axisXTitle"):
        ax.set_xlabel(_text(options["axisXTitle"]))
    if options.get("axisYTitle"):
        ax.set_ylabel(_text(options["axisYTitle"]))
    ax.grid(True, axis="y", alpha=0.3)
    ax.spines[["top", "right"]].set_visible(False)


def _fold(groups: "OrderedDict[str, Dict[str, float]]") -> "OrderedDict[str, Dict[str, float]]":
    """그룹이 MAX_SERIES보다 많으면 합계가 큰 셋만 남기고 나머지를 '기타' 하나로 더한다 (frontend foldGroups와 같다)."""
    if len(groups) <= MAX_SERIES:
        return groups
    ranked = sorted(groups.items(), key=lambda item: -sum(item[1].values()))
    folded: "OrderedDict[str, Dict[str, float]]" = OrderedDict(ranked[:MAX_SERIES - 1])
    other: Dict[str, float] = {}
    for _, values in ranked[MAX_SERIES - 1:]:
        for x, value in values.items():
            other[x] = other.get(x, 0.0) + value
    folded[OTHER] = other
    return folded


def _series(data: List[Dict[str, Any]], key: str) -> Tuple[List[str], "OrderedDict[str, Dict[str, float]]"]:
    """[{key, value, group?}] → (x 순서, {그룹: {x: 값}}). group이 없으면 그룹 하나."""
    xs: List[str] = []
    groups: "OrderedDict[str, Dict[str, float]]" = OrderedDict()
    for item in data:
        if not isinstance(item, dict) or key not in item or "value" not in item:
            raise ChartError(f"each item needs '{key}' and 'value'")
        x = _text(item[key])
        if x not in xs:
            xs.append(x)
        group = _text(item.get("group", ""))
        groups.setdefault(group, {})[x] = groups.get(group, {}).get(x, 0.0) + _number(item["value"], "value")
    return xs, _fold(groups)


def _legend(ax, groups) -> None:
    if len(groups) > 1 or next(iter(groups), ""):
        ax.legend(fontsize=8, frameon=False)


# ---------------------------------------------------------------- 축이 있는 차트

def _line(options, area: bool = False):
    fig, ax = _figure(options)
    xs, groups = _series(_items(options.get("data")), "time")
    positions = range(len(xs))
    base = [0.0] * len(xs)
    for index, (group, values) in enumerate(groups.items()):
        ys = [values.get(x, 0.0) for x in xs]
        if options.get("stack"):
            ys = [b + y for b, y in zip(base, ys)]
        color = PALETTE[index % len(PALETTE)]
        ax.plot(positions, ys, marker="o", markersize=3, color=color, label=group or None)
        if area:
            ax.fill_between(positions, base if options.get("stack") else 0, ys, color=color, alpha=0.3)
        if options.get("stack"):
            base = ys
    _ticks(ax, xs)
    _axis_titles(ax, options)
    _legend(ax, groups)
    return fig


def _ticks(ax, labels: List[str], horizontal: bool = False) -> None:
    step = max(1, math.ceil(len(labels) / 20))  # 눈금 글자가 겹치지 않게 최대 20개
    ticks = list(range(0, len(labels), step))
    if horizontal:
        ax.set_yticks(ticks, [labels[i] for i in ticks], fontsize=8)
    else:
        ax.set_xticks(ticks, [labels[i] for i in ticks], fontsize=8,
                      rotation=30 if len(labels) > 6 else 0, ha="right" if len(labels) > 6 else "center")


def _bars(options, horizontal: bool):
    fig, ax = _figure(options)
    xs, groups = _series(_items(options.get("data")), "category")
    stack = bool(options.get("stack")) and len(groups) > 1
    count = 1 if stack or len(groups) == 1 else len(groups)
    width = 0.8 / count
    base = [0.0] * len(xs)
    for index, (group, values) in enumerate(groups.items()):
        ys = [values.get(x, 0.0) for x in xs]
        offset = 0 if count == 1 else (index - (count - 1) / 2) * width
        positions = [i + offset for i in range(len(xs))]
        color = PALETTE[index % len(PALETTE)]
        if horizontal:
            ax.barh(positions, ys, height=width, left=base if stack else None, color=color, label=group or None)
        else:
            ax.bar(positions, ys, width=width, bottom=base if stack else None, color=color, label=group or None)
        if stack:
            base = [b + y for b, y in zip(base, ys)]
    _ticks(ax, xs, horizontal)
    if horizontal:
        ax.invert_yaxis()  # 첫 항목이 위에 오게
    _axis_titles(ax, options)
    if horizontal:
        ax.grid(True, axis="x", alpha=0.3)
        ax.grid(False, axis="y")
    _legend(ax, groups)
    return fig


def _pie(options):
    fig, ax = _figure(options)
    xs, groups = _series(_items(options.get("data")), "category")
    values = [sum(g.get(x, 0.0) for g in groups.values()) for x in xs]
    if any(v < 0 for v in values) or not sum(values):
        raise ChartError("pie values must be non-negative and not all zero")
    # 큰 조각부터. 조각이 MAX_SERIES보다 많으면 큰 셋 + '기타' (frontend pieSlices와 같다)
    slices = sorted(zip(xs, values), key=lambda s: -s[1])
    if len(slices) > MAX_SERIES:
        slices = slices[:MAX_SERIES - 1] + [(OTHER, sum(v for _, v in slices[MAX_SERIES - 1:]))]
    xs, values = [x for x, _ in slices], [v for _, v in slices]
    inner = min(max(float(options.get("innerRadius") or 0), 0.0), 0.9)
    _, _, percents = ax.pie(values, labels=xs, colors=[PALETTE[i % len(PALETTE)] for i in range(len(xs))],
                            autopct="%1.1f%%", startangle=90, counterclock=False, textprops={"fontsize": 8},
                            wedgeprops={"edgecolor": "white", "linewidth": 1.5, **({"width": 1 - inner} if inner else {})})
    for index, percent in enumerate(percents):  # 짙은 조각(주 색·남색) 위의 비율은 흰 글자
        percent.set_color("white" if PALETTE[index % len(PALETTE)] in (PRIMARY, INK) else INK)
    ax.axis("equal")
    return fig


def _scatter(options):
    fig, ax = _figure(options)
    data = _items(options.get("data"))
    for item in data:
        if not isinstance(item, dict) or "x" not in item or "y" not in item:
            raise ChartError("each item needs 'x' and 'y'")
    ax.scatter([_number(i["x"], "x") for i in data], [_number(i["y"], "y") for i in data], color=PALETTE[0],
               alpha=0.8, s=18)
    _axis_titles(ax, options)
    return fig


def _histogram(options):
    fig, ax = _figure(options)
    values = [_number(v, "value") for v in _items(options.get("data"), MAX_ITEMS * 10)]
    bins = options.get("binNumber") or "auto"
    if bins != "auto":
        bins = min(max(int(bins), 1), 200)
    ax.hist(values, bins=bins, color=PALETTE[0], edgecolor="white")
    _axis_titles(ax, options)
    return fig


def _radar(options):
    fig, ax = _figure(options, polar=True)
    names, groups = _series(_items(options.get("data")), "name")
    if len(names) < 3:
        raise ChartError("radar needs at least 3 names")
    angles = [2 * math.pi * i / len(names) for i in range(len(names))] + [0.0]
    for index, (group, values) in enumerate(groups.items()):
        ys = [values.get(n, 0.0) for n in names]
        ys.append(ys[0])
        color = PALETTE[index % len(PALETTE)]
        ax.plot(angles, ys, color=color, label=group or None)
        ax.fill(angles, ys, color=color, alpha=0.2)
    ax.set_xticks(angles[:-1], names, fontsize=8)
    ax.tick_params(axis="y", labelsize=7)
    _legend(ax, groups)
    return fig


def _dual_axes(options):
    fig, ax = _figure(options)
    categories = [_text(c) for c in _items(options.get("categories"))]
    series = _items(options.get("series"), 10)
    columns = [s for s in series if isinstance(s, dict) and s.get("type") == "column"]
    lines = [s for s in series if isinstance(s, dict) and s.get("type") == "line"]
    if len(columns) + len(lines) != len(series):
        raise ChartError("each series needs type 'column' or 'line'")
    right = ax.twinx() if columns and lines else ax
    width = 0.8 / max(len(columns), 1)
    handles = []
    for index, item in enumerate(series):
        ys = [_number(v, "series data") for v in _items(item.get("data"))]
        if len(ys) != len(categories):
            raise ChartError("each series needs one value per category")
        color = PALETTE[index % len(PALETTE)]
        label = _text(item.get("axisYTitle") or item["type"])
        if item["type"] == "column":
            offset = (columns.index(item) - (len(columns) - 1) / 2) * width
            handles.append(ax.bar([i + offset for i in range(len(ys))], ys, width=width, color=color, label=label))
            ax.set_ylabel(label)
        else:
            handles += right.plot(range(len(ys)), ys, marker="o", color=color, label=label)
            right.set_ylabel(label)
    _ticks(ax, categories)
    if options.get("axisXTitle"):
        ax.set_xlabel(_text(options["axisXTitle"]))
    ax.legend(handles=handles, fontsize=8, frameon=False, loc="upper left")
    return fig


def _word_cloud(options):
    """글자 크기로 값을 보인다. 가운데부터 나선을 따라 겹치지 않는 자리에 놓는다."""
    fig, ax = _figure(options)
    data = _items(options.get("data"), 200)
    words = []
    for item in data:
        if not isinstance(item, dict) or "text" not in item or "value" not in item:
            raise ChartError("each item needs 'text' and 'value'")
        words.append((_text(item["text"]), _number(item["value"], "value")))
    words.sort(key=lambda w: -w[1])
    low, high = min(w[1] for w in words), max(w[1] for w in words)
    ax.set_xlim(-1, 1)
    ax.set_ylim(-1, 1)
    ax.axis("off")
    renderer = fig.canvas.get_renderer()
    placed: List[Any] = []
    for index, (word, value) in enumerate(words):
        size = 8 + 28 * ((value - low) / (high - low) if high > low else 1)
        # 크기가 값을 보이므로 색은 세 단계만: 큰 다섯 단어 주 색, 위 30%까지 남색, 나머지 옅은 글자색 (frontend와 같다)
        color = PRIMARY if index < 5 else INK if index < max(5, len(words) * 0.3) else INK_3
        artist = ax.text(0, 0, word, fontsize=size, ha="center", va="center", color=color)
        for step in range(1500):  # 나선: 반지름을 조금씩 키우며 빈자리를 찾는다
            angle = step * 0.35
            radius = 0.004 * step
            artist.set_position((radius * math.cos(angle) * 1.3, radius * math.sin(angle)))
            box = artist.get_window_extent(renderer).expanded(1.05, 1.1)
            inside = ax.bbox.contains(box.x0, box.y0) and ax.bbox.contains(box.x1, box.y1)
            if inside and not any(box.overlaps(other) for other in placed):
                placed.append(box)
                break
        else:
            artist.remove()  # 자리가 없으면 작은 단어는 뺀다
    return fig


# ---------------------------------------------------------------- 트리·그래프

def _tree(node: Any, depth: int = 0, count: Optional[List[int]] = None) -> Dict[str, Any]:
    """{name, children?} 검사와 정리 (노드 수·깊이 제한)."""
    count = count if count is not None else [0]
    if not isinstance(node, dict) or "name" not in node:
        raise ChartError("each node needs a 'name'")
    count[0] += 1
    if count[0] > MAX_NODES or depth > 10:
        raise ChartError(f"too many nodes (max {MAX_NODES}) or too deep (max 10)")
    children = node.get("children") or []
    if not isinstance(children, list):
        raise ChartError("children must be a list")
    return {"name": _text(node["name"]), "value": node.get("value"),
            "children": [_tree(child, depth + 1, count) for child in children]}


def _box(ax, x: float, y: float, text: str, color: str, size: int = 8, fill: str = "white") -> None:
    ax.text(x, y, text, ha="center", va="center", fontsize=size, zorder=3,
            bbox={"boxstyle": "round,pad=0.35", "facecolor": fill, "edgecolor": color, "linewidth": 1.2})


def _mind_map(options):
    fig, ax = _figure(options)
    root = _tree(options.get("data"))
    positions: Dict[int, Tuple[float, float]] = {}
    leaves = [0]

    def place(node, depth):  # 잎은 차례로 아래로, 가지는 자식들의 가운데 (왼쪽에서 오른쪽으로 깊어진다)
        if node["children"]:
            ys = [place(child, depth + 1) for child in node["children"]]
            y = sum(ys) / len(ys)
        else:
            y = -leaves[0]
            leaves[0] += 1
        positions[id(node)] = (depth, y)
        return y

    place(root, 0)

    def draw(node, depth):
        x, y = positions[id(node)]
        for index, child in enumerate(node["children"]):
            cx, cy = positions[id(child)]
            ax.plot([x, (x + cx) / 2, (x + cx) / 2, cx], [y, y, cy, cy], color=SLATE, lw=1, zorder=1)
            draw(child, depth + 1)
        # 단계마다 색을 바꾸지 않는다 (위치가 단계를 보인다). 뿌리만 옅은 파랑 바탕으로 (남색 글자가 읽히게)
        _box(ax, x, y, node["name"], PRIMARY, 10 if depth == 0 else 8, SOFT if depth == 0 else "white")

    draw(root, 0)
    ax.axis("off")
    ax.margins(0.15, 0.08)
    return fig


def _fishbone(options):
    """가시(원인)를 등뼈 위아래로 번갈아 두고, 머리(문제)는 오른쪽에 둔다."""
    fig, ax = _figure(options)
    root = _tree(options.get("data"))
    causes = root["children"] or [{"name": "(원인 없음)", "children": []}]
    count = math.ceil(len(causes) / 2)
    gap = 1.8  # 가지 사이 (작은 원인 글자가 옆 가지와 겹치지 않게)
    ax.plot([0, count * gap + 0.6], [0, 0], color=INK_2, lw=2)
    _box(ax, count * gap + 1.1, 0, root["name"], INK, 10, SOFT)  # 머리(문제): 옅은 파랑 바탕, 남색 테두리
    for index, cause in enumerate(causes):
        side = 1 if index % 2 == 0 else -1
        x = (index // 2 + 1) * gap
        top = (x - 0.6, side * 1.0)
        ax.plot([top[0], x], [top[1], 0], color=INK_3, lw=1.2)
        _box(ax, top[0], top[1] + side * 0.12, cause["name"], LINE)
        for sub_index, sub in enumerate(cause["children"][:6]):
            t = (sub_index + 1) / (min(len(cause["children"]), 6) + 1)
            bx, by = top[0] + (x - top[0]) * t, top[1] * (1 - t)
            ax.plot([bx - 0.35, bx], [by, by], color=SLATE, lw=0.8)
            ax.text(bx - 0.37, by, sub["name"], ha="right", va="center", fontsize=7)
    ax.set_xlim(-0.8, count * gap + 2.2)
    ax.set_ylim(-1.5, 1.5)
    ax.axis("off")
    return fig


def _graph(options) -> Tuple[List[str], List[Tuple[str, str, str]]]:
    data = options.get("data") or {}
    nodes = _items(data.get("nodes"), MAX_NODES)
    names = []
    for node in nodes:
        if not isinstance(node, dict) or "name" not in node:
            raise ChartError("each node needs a 'name'")
        names.append(_text(node["name"]))
    edges = []
    for edge in data.get("edges") or []:
        if not isinstance(edge, dict) or "source" not in edge or "target" not in edge:
            raise ChartError("each edge needs 'source' and 'target'")
        source, target = _text(edge["source"]), _text(edge["target"])
        for name in (source, target):
            if name not in names:
                names.append(name)  # 노드 목록에 없는 끝점도 그린다
        edges.append((source, target, _text(edge.get("name", ""))))
    if len(edges) > MAX_NODES * 3:
        raise ChartError("too many edges")
    return names, edges


def _arrow(ax, start, end, label: str, directed: bool) -> None:
    ax.annotate("", xy=end, xytext=start, zorder=1,
                arrowprops={"arrowstyle": "-|>" if directed else "-", "color": INK_3, "lw": 1,
                            "shrinkA": 14, "shrinkB": 14})
    if label:
        ax.text((start[0] + end[0]) / 2, (start[1] + end[1]) / 2, label, fontsize=7, color=INK_2,
                ha="center", va="center", bbox={"facecolor": "white", "edgecolor": "none", "pad": 1})


def _network_graph(options):
    """노드를 원 위에 고르게 둔다 (간선이 몰리지 않고, 노드가 몇 개든 같은 모양)."""
    fig, ax = _figure(options)
    names, edges = _graph(options)
    positions = {name: (math.cos(2 * math.pi * i / len(names) + math.pi / 2),
                        math.sin(2 * math.pi * i / len(names) + math.pi / 2)) for i, name in enumerate(names)}
    for source, target, label in edges:
        _arrow(ax, positions[source], positions[target], label, directed=False)
    for name in names:
        _box(ax, *positions[name], name, PRIMARY)
    ax.set_xlim(-1.4, 1.4)
    ax.set_ylim(-1.3, 1.3)
    ax.set_aspect("equal")
    ax.axis("off")
    return fig


def _flow_diagram(options):
    """위에서 아래로 흐르는 단계. 되돌아가는 간선(반복)은 빼고 가장 긴 경로의 깊이에 둔다.
    되돌아가는 간선은 옆으로 휘어 그린다."""
    fig, ax = _figure(options)
    names, edges = _graph(options)
    targets = {target for _source, target, _label in edges}
    children: Dict[str, List[str]] = {name: [] for name in names}
    for source, target, _label in edges:
        children[source].append(target)
    # 깊이 우선 탐색으로 되돌아가는 간선(지금 경로 위의 노드로 가는 간선)을 찾는다
    back, state = set(), {}
    roots = [name for name in names if name not in targets] or names[:1]
    for root in roots + names:
        if root in state:
            continue
        stack = [(root, iter(children[root]))]
        state[root] = "open"
        while stack:
            node, rest = stack[-1]
            child = next(rest, None)
            if child is None:
                state[node] = "done"
                stack.pop()
            elif state.get(child) == "open":
                back.add((node, child))
            elif child not in state:
                state[child] = "open"
                stack.append((child, iter(children[child])))
    depth = {name: 0 for name in names}
    forward = [(s_, t) for s_, t, _l in edges if (s_, t) not in back and s_ != t]
    for _ in range(len(names)):  # 되돌아가는 간선을 뺐으니 노드 수만큼 밀면 끝난다
        changed = False
        for source, target in forward:
            if depth[target] < depth[source] + 1:
                depth[target] = depth[source] + 1
                changed = True
        if not changed:
            break
    rows: Dict[int, List[str]] = {}
    for name in names:
        rows.setdefault(depth[name], []).append(name)
    positions = {}
    for level, row in rows.items():
        for index, name in enumerate(row):
            positions[name] = ((index - (len(row) - 1) / 2) * 1.6, -level)
    # 되돌아가는 간선은 왼쪽, 단계를 건너뛰는 간선은 오른쪽으로 휜 곡선 (사이 노드를 지나는 직선과 겹치지 않게).
    # 곡선이 가장 멀리 나가는 곳은 두 끝 사이 거리 × 0.4 ÷ 2 만큼 옆이다 (matplotlib arc3). 이름은 그 바깥에 둔다
    reach = [0.0]
    for source, target, label in edges:
        (x0, y0), (x1, y1) = positions[source], positions[target]
        if (source, target) in back or source == target or depth[target] - depth[source] > 1:
            side = -1 if y1 > y0 or source == target else 1  # 위로 가면 왼쪽, 아래로 가면 오른쪽
            ax.annotate("", xy=(x1, y1), xytext=(x0, y0), zorder=1,
                        arrowprops={"arrowstyle": "-|>", "color": INK_3, "lw": 1, "shrinkA": 14,
                                    "shrinkB": 14, "linestyle": "--" if side < 0 else "-",
                                    # 같은 값이라도 선의 방향에 따라 휘는 쪽이 바뀐다: 위로 가면 왼쪽, 아래로 가면 오른쪽
                                    "connectionstyle": "arc3,rad=-0.4"})
            bulge = 0.4 * math.hypot(x1 - x0, y1 - y0) / 2
            reach.append(bulge)
            if label:
                ax.text((x0 + x1) / 2 + side * (bulge + 0.1), (y0 + y1) / 2, label, fontsize=7, color=INK_2,
                        ha="left" if side > 0 else "right", va="center")
        else:
            _arrow(ax, (x0, y0), (x1, y1), label, directed=True)
    for name in names:
        _box(ax, *positions[name], name, PRIMARY)
    xs = [x for x, _y in positions.values()]
    margin = max(1.6, max(reach) + 1.4)  # 휜 선과 그 이름까지 들어오게
    ax.set_xlim(min(xs) - margin, max(xs) + margin)
    ax.set_ylim(-max(depth.values()) - 0.6, 0.6)
    ax.axis("off")
    return fig


def _treemap(options):
    """값에 비례하는 넓이의 사각형. 깊이마다 가로·세로를 번갈아 나눈다 (slice-and-dice)."""
    fig, ax = _figure(options)
    nodes = [_tree(item) for item in _items(options.get("data"), MAX_NODES)]

    def total(node) -> float:
        if node["value"] is not None:
            return max(_number(node["value"], "value"), 0.0)
        return sum(total(child) for child in node["children"])

    def split(items, x, y, w, h, depth):
        whole = sum(total(item) for item in items) or 1.0
        offset = 0.0
        for item in items:
            share = total(item) / whole
            if depth % 2 == 0:
                box = (x + offset * w, y, w * share, h)
            else:
                box = (x, y + offset * h, w, h * share)
            offset += share
            # 주 색 한 가지. 칸마다 이름이 있어 색으로 나누지 않는다. 아래 단계는 주 색을 옅게 푼 바탕(28%)에 남색 글자
            ax.add_patch(Rectangle(box[:2], box[2], box[3], facecolor=PRIMARY if depth == 0 else "#C0D0E6",
                                   edgecolor="white", lw=2 if depth == 0 else 0.8))
            if item["children"] and box[3] > 0.12:
                # 이름은 위쪽 띠에, 하위는 그 아래에 (하위가 이름을 가리지 않게)
                header = min(0.06, box[3] * 0.3)
                ax.text(box[0] + 0.01, box[1] + box[3] - header / 2, item["name"], ha="left", va="center",
                        fontsize=8, color="white", fontweight="bold")
                split(item["children"], box[0], box[1], box[2], box[3] - header, depth + 1)
            elif box[2] > 0.06 and box[3] > 0.05:
                ax.text(box[0] + box[2] / 2, box[1] + box[3] / 2, item["name"], ha="center", va="center",
                        fontsize=8 if depth == 0 else 7, color="white" if depth == 0 else INK)

    split(nodes, 0, 0, 1, 1, 0)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    return fig


RENDERERS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "line": _line,
    "area": lambda options: _line(options, area=True),
    "bar": lambda options: _bars(options, horizontal=True),
    "column": lambda options: _bars(options, horizontal=False),
    "pie": _pie,
    "scatter": _scatter,
    "histogram": _histogram,
    "radar": _radar,
    "dual-axes": _dual_axes,
    "word-cloud": _word_cloud,
    "treemap": _treemap,
    "mind-map": _mind_map,
    "fishbone-diagram": _fishbone,
    "network-graph": _network_graph,
    "flow-diagram": _flow_diagram,
}

def render_chart(chart_type: str, options: Dict[str, Any]) -> bytes:
    """차트 PNG. 모르는 종류나 그릴 수 없는 데이터면 ChartError."""
    renderer = RENDERERS.get(chart_type)
    if renderer is None:
        raise ChartError(f"unknown chart type: {chart_type}")
    fig = renderer(options)
    try:
        fig.tight_layout()
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=DPI, facecolor="white")
        return buffer.getvalue()
    finally:
        plt.close(fig)


def generate_chart_url(chart_type: str, options: Dict[str, Any]) -> Dict[str, Any]:
    """차트를 그려 다이어그램 버킷에 올리고 presigned URL을 돌려준다 (계정 밖으로 데이터를 보내지 않는다)."""
    try:
        image = render_chart(chart_type, options)
    except ChartError as error:
        return {"status": "error", "chart_type": chart_type, "message": f"Invalid chart data: {error}"}
    except Exception as error:  # matplotlib 내부 오류도 도구 결과로 돌려준다
        plt.close("all")
        return {"status": "error", "chart_type": chart_type, "message": f"Error generating chart: {error}"}
    try:
        key = f"charts/{datetime.now().strftime('%Y/%m/%d')}/{chart_type}_{uuid.uuid4().hex[:8]}.png"
        s3_client.put_object(Bucket=DIAGRAM_BUCKET, Key=key, Body=image, ContentType="image/png")
        url = s3_client.generate_presigned_url("get_object", Params={"Bucket": DIAGRAM_BUCKET, "Key": key},
                                               ExpiresIn=PRESIGNED_SECONDS)
    except Exception as error:
        return {"status": "error", "chart_type": chart_type, "message": f"Error uploading chart: {error}"}
    result = {"status": "success", "url": url, "s3_key": key, "chart_type": chart_type,
              "message": f"Chart generated successfully: {chart_type}"}
    # 그릴 내용: PNG를 그리며 검사를 마친 값이다. 모델에는 가지 않는다 (LLM Lambda artifacts.py가 떼어 화면에 준다)
    spec = {"type": chart_type, "options": options}
    if len(json.dumps(spec, ensure_ascii=False, default=str)) <= SPEC_LIMIT:
        result["spec"] = spec
    return result


def validate_chart_data(data: List[Dict], required_fields: List[str]) -> bool:
    """Validate that chart data contains required fields."""
    if not data or not isinstance(data, list):
        return False

    for item in data:
        if not isinstance(item, dict) or not all(field in item for field in required_fields):
            return False

    return True
