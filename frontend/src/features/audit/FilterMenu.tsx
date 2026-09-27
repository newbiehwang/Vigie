// 필터 버튼과 세부 선택 창. 기간·그룹 기준·거르기 목록(FacetSidebar: 종류·결과·층·요청자·도구·출처·표시)을
// 늘 펼쳐 두지 않고, '필터' 버튼을 누를 때만 아래에 연다. 걸린 조건은 버튼 옆에 조각(칩)으로 보여, 창을 열지 않아도 안다.
//   [필터 1 ▾]  기간: 전체   그룹 기준: 요청자 ✕   결과: 실패 ✕      ← 버튼의 수는 거르기(종류·결과…)만 센다
//   ┌──────────────────────────────────────────────────────────┐
//   │ 기간       [전체][1시간][4시간][1일][7일][30일][직접]  [↺ 필터 초기화] ✕ │   ← ✕: 다른 팝업창과 같은 닫기
//   │ 그룹 기준  [결과][종류][요청자][도구]                          │
//   │ ─────────────────────────────────────────                  │
//   │ ▾ 종류        ▾ 요청자        ▾ 도구                         │   ← 여러 단으로. 값마다 건수, 체크로 고르고 풀기
//   │ ☑ 질문  119   ☐ demo…  101    ☐ 로그 분석  24                │
//   └──────────────────────────────────────────────────────────┘
// - 고르면 바로 목록·그래프에 적용된다 (적용 버튼 없음). 건수는 다른 조건을 적용한 채 센다
// - 바깥을 누르거나 Esc, 오른쪽 위 ✕로 닫는다. '필터 초기화'는 처음 화면으로 되돌린다(창은 열어 둔다)
// - 칩: 기간은 늘 보인다(처음 값이 아니면 ✕로 전체로). 그룹 기준은 결과가 아닐 때, 거르기는 고른 값마다
// - 버튼의 수·파란 테두리는 거르기를 골랐을 때만. 기간·그룹 기준은 보는 방식이라 필터로 세지 않는다
import { useEffect, useRef, useState } from 'react';
import type { AuditRecord } from '@/types/audit';
import {
    FACETS,
    GROUP_OPTIONS,
    activeCount,
    requesterOf,
    type FacetId,
    type GroupBy,
    type Selection,
} from './auditModel';
import { FacetSidebar } from './FacetSidebar';
import { PeriodPicker } from './PeriodPicker';
import { DEFAULT_PERIOD, isDefaultPeriod, periodLabel, type Period, type TimeWindow } from './timeWindow';

// 칩에 보일 값 이름 (요청자는 받은 기록에서 이메일·Slack 이름을 찾는다)
const chipLabel = (id: FacetId, value: string, records: AuditRecord[]) => {
    const facet = FACETS.find((f) => f.id === id);
    if (facet?.labelOf) return facet.labelOf(value);
    if (id === 'requester') {
        const record = records.find((r) => r.userId === value);
        return record ? requesterOf(record) : value;
    }
    return value;
};

// 필터 초기화 버튼 (필터 창 오른쪽 위, 조건에 맞는 기록이 없을 때의 안내). 처음 화면이면 꺼 둔다.
// 사용자 관리 화면도 쓴다 (title: 그 화면에서 무엇을 되돌리는지)
export function ResetButton({
    onClick,
    disabled,
    title = '처음 화면으로 되돌립니다 (거르기·검색어를 지우고, 기간은 전체, 그룹 기준은 결과)',
}: {
    onClick: () => void;
    disabled: boolean;
    title?: string;
}) {
    return (
        <button type="button" className="audit-reset" onClick={onClick} disabled={disabled} title={title}>
            <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false">
                <path
                    d="M4 12a8 8 0 1 0 2.34-5.66M4 4v5h5"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                />
            </svg>
            필터 초기화
        </button>
    );
}

interface Chip {
    key: string;
    name: string; // 기간 · 그룹 기준 · 종류 …
    label: string;
    onRemove?: () => void; // 없으면 ✕ 없음 (처음 값인 기간)
}

const PANEL_MAX_HEIGHT = 520; // 세부 선택 창의 가장 큰 높이 (넓은 화면)
const PANEL_MIN_HEIGHT = 220; // 흰 카드가 아주 낮아도 이만큼은 둔다
const PANEL_GAP = 16; // 흰 카드 바닥과의 간격

export function FilterMenu({
    records,
    allRecords,
    selection,
    onChange,
    period,
    window,
    onPeriod,
    groupBy,
    onGroupBy,
    onReset,
    canReset,
    resetNo,
}: {
    records: AuditRecord[]; // 건수를 셀 기록 (기간·검색어를 적용한 것)
    allRecords: AuditRecord[]; // 칩의 요청자 이름을 찾을 기록 (받은 기록 전체)
    selection: Selection;
    onChange: (next: Selection) => void;
    period: Period;
    window: TimeWindow;
    onPeriod: (period: Period) => void;
    groupBy: GroupBy;
    onGroupBy: (groupBy: GroupBy) => void;
    onReset: () => void; // 필터 초기화
    canReset: boolean; // 처음 화면과 다른가
    resetNo: number; // 필터 초기화: 바뀌면 거르기 목록을 새로 그려 접기·더 보기를 처음으로
}) {
    const [open, setOpen] = useState(false);
    const box = useRef<HTMLDivElement>(null);
    const button = useRef<HTMLButtonElement>(null);
    const panel = useRef<HTMLDivElement>(null);
    const [panelHeight, setPanelHeight] = useState<number>();

    // 세부 선택 창의 높이: 창 위쪽부터 흰 카드(plan-panel) 바닥까지 남은 높이로 고정한다 (PANEL_MAX_HEIGHT까지).
    // 흰 카드는 넘치는 부분을 잘라 내므로, 거르기가 많아도 창이 카드 밖으로 나가 잘리지 않게 한다. 안쪽 목록은 스크롤된다.
    // 화면 크기가 바뀌면 다시 잰다
    useEffect(() => {
        if (!open) return;
        const measure = () => {
            const card = panel.current?.closest('.plan-panel');
            if (!panel.current || !card) return;
            const room = card.getBoundingClientRect().bottom - panel.current.getBoundingClientRect().top - PANEL_GAP;
            setPanelHeight(Math.max(PANEL_MIN_HEIGHT, Math.min(PANEL_MAX_HEIGHT, Math.floor(room))));
        };
        measure();
        // 이 컴포넌트에는 window라는 속성(기간)이 있어 전역 window를 globalThis로 부른다
        globalThis.addEventListener('resize', measure);
        return () => globalThis.removeEventListener('resize', measure);
    }, [open]);

    // 바깥을 누르거나 Esc를 누르면 닫는다. 기간 '직접' 입력이 열려 있으면 Esc는 그것부터 닫는다 (PeriodPicker)
    useEffect(() => {
        if (!open) return;
        const onPointerDown = (event: PointerEvent) => {
            if (!box.current?.contains(event.target as Node)) setOpen(false);
        };
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key !== 'Escape' || box.current?.querySelector('.audit-period-popover')) return;
            event.preventDefault(); // 다른 창이 함께 닫히지 않게
            setOpen(false);
            button.current?.focus();
        };
        document.addEventListener('pointerdown', onPointerDown);
        document.addEventListener('keydown', onKeyDown, true);
        return () => {
            document.removeEventListener('pointerdown', onPointerDown);
            document.removeEventListener('keydown', onKeyDown, true);
        };
    }, [open]);

    const groupLabel = GROUP_OPTIONS.find((option) => option.value === groupBy)?.label ?? '';
    const chips: Chip[] = [
        {
            key: 'period',
            name: '기간',
            label: periodLabel(period),
            onRemove: isDefaultPeriod(period) ? undefined : () => onPeriod(DEFAULT_PERIOD),
        },
        ...(groupBy !== 'result'
            ? [{ key: 'group', name: '그룹 기준', label: groupLabel, onRemove: () => onGroupBy('result') }]
            : []),
        ...FACETS.flatMap((facet) =>
            (selection[facet.id] ?? []).map((value) => ({
                key: `${facet.id}:${value}`,
                name: facet.label,
                label: chipLabel(facet.id, value, allRecords),
                onRemove: () =>
                    onChange({ ...selection, [facet.id]: (selection[facet.id] ?? []).filter((v) => v !== value) }),
            })),
        ),
    ];
    // 버튼의 수: 고른 거르기 값의 수 (기간·그룹 기준은 세지 않는다)
    const count = activeCount(selection);

    return (
        <div className="audit-filter-menu" ref={box}>
            <button
                ref={button}
                type="button"
                className={`audit-filter-button${open ? ' is-open' : ''}${count ? ' is-active' : ''}`}
                aria-expanded={open}
                aria-controls="audit-filter-panel"
                onClick={() => setOpen((prev) => !prev)}
            >
                <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false">
                    <path d="M4 6h16M7 12h10M10 18h4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                </svg>
                필터
                {count ? <span className="audit-filter-count">{count}</span> : null}
                <span className="audit-filter-caret" aria-hidden="true">
                    ▾
                </span>
            </button>

            <ul className="audit-filter-chips" aria-label="걸린 조건">
                {chips.map((chip) => (
                    <li key={chip.key} className={chip.onRemove ? '' : 'is-fixed'}>
                        <span className="audit-filter-chip-facet">{chip.name}:</span>
                        <span className="audit-filter-chip-value" title={chip.label}>
                            {chip.label}
                        </span>
                        {chip.onRemove ? (
                            <button type="button" onClick={chip.onRemove} aria-label={`${chip.name} ${chip.label} 조건 빼기`}>
                                <svg viewBox="0 0 14 14" width="9" height="9" aria-hidden="true" focusable="false">
                                    <path d="M2 2L12 12M12 2L2 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                                </svg>
                            </button>
                        ) : null}
                    </li>
                ))}
            </ul>

            {open ? (
                <div
                    ref={panel}
                    id="audit-filter-panel"
                    className="audit-filter-panel"
                    role="dialog"
                    aria-label="필터 세부 선택"
                    style={panelHeight ? { height: panelHeight } : undefined}
                >
                    {/* 닫기: 이 앱의 다른 팝업창과 같은 오른쪽 위 ✕ */}
                    <button
                        className="vdt-model-close"
                        type="button"
                        onClick={() => {
                            setOpen(false);
                            button.current?.focus(); // 닫으면 필터 버튼으로 (키보드로 이어서)
                        }}
                        aria-label="필터 닫기"
                    >
                        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                            <path d="M1 1L13 13M13 1L1 13" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                        </svg>
                    </button>
                    <div className="audit-filter-top">
                        <div className="audit-filter-options">
                            <div className="audit-filter-option">
                                <span className="audit-filter-label">기간</span>
                                <PeriodPicker period={period} window={window} onChange={onPeriod} />
                            </div>
                            <div className="audit-filter-option">
                                <span className="audit-filter-label">그룹 기준</span>
                                <div className="audit-segment" role="group" aria-label="그룹 기준">
                                    {GROUP_OPTIONS.map((option) => (
                                        <button
                                            key={option.value}
                                            type="button"
                                            className={`audit-segment-btn${option.value === groupBy ? ' is-active' : ''}`}
                                            aria-pressed={option.value === groupBy}
                                            onClick={() => onGroupBy(option.value)}
                                        >
                                            {option.label}
                                        </button>
                                    ))}
                                </div>
                            </div>
                        </div>
                        <ResetButton onClick={onReset} disabled={!canReset} />
                    </div>
                    <FacetSidebar key={resetNo} records={records} selection={selection} onChange={onChange} />
                </div>
            ) : null}
        </div>
    );
}
