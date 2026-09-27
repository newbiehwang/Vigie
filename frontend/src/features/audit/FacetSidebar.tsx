// 감사 로그의 거르기 목록 (Datadog Audit Trail의 facet 목록을 따랐다). '필터' 버튼을 누르면 열리는 창(FilterMenu) 안에 여러 단으로 놓인다.
//   ▾ 결과                 지우기
//     ☑ 성공          120      ← 체크(또는 이름)를 누르면 고르거나 푼다
//     ☐ 실패           12
//   ▾ 요청자
//     … 6개까지 보이고 '더 보기 (n)'
// 건수는 다른 거르기를 모두 적용하고 이 거르기만 뺀 기록에서 센다 (auditModel.facetValues)
import { useMemo, useState } from 'react';
import type { AuditRecord } from '@/types/audit';
import { FACETS, facetValues, toggleValue, type FacetDef, type Selection } from './auditModel';

const TOP_N = 6; // 값이 많은 거르기(요청자·도구)는 처음에 이만큼만 보인다

function FacetGroup({
    facet,
    records,
    selection,
    onChange,
}: {
    facet: FacetDef;
    records: AuditRecord[];
    selection: Selection;
    onChange: (next: Selection) => void;
}) {
    const [open, setOpen] = useState(true);
    const [showAll, setShowAll] = useState(false);
    const values = useMemo(() => facetValues(records, selection, facet), [records, selection, facet]);
    if (values.length === 0) return null; // 이 기간·조건에 값이 없는 거르기는 숨긴다

    const chosen = selection[facet.id]?.length ?? 0;
    // 처음 TOP_N개 + 그 밖에서 고른 값 (고른 값이 '더 보기' 뒤에 숨지 않게)
    const shown = showAll ? values : values.filter((item, index) => index < TOP_N || item.selected);
    const hidden = values.length - shown.length;
    const listId = `audit-facet-${facet.id}`;

    return (
        <div className="audit-facet">
            <div className="audit-facet-head">
                <button
                    type="button"
                    className="audit-facet-toggle"
                    aria-expanded={open}
                    aria-controls={listId}
                    onClick={() => setOpen((prev) => !prev)}
                >
                    <span className="audit-facet-caret" aria-hidden="true">
                        ▸
                    </span>
                    {facet.label}
                    {chosen ? <span className="audit-facet-chosen">{chosen}</span> : null}
                </button>
                {chosen ? (
                    <button
                        type="button"
                        className="audit-facet-clear"
                        onClick={() => onChange({ ...selection, [facet.id]: [] })}
                        aria-label={`${facet.label} 조건 지우기`}
                    >
                        지우기
                    </button>
                ) : null}
            </div>
            {open ? (
                <ul id={listId} className="audit-facet-values">
                    {shown.map((item) => (
                        <li key={item.value} className={`audit-facet-value${item.count === 0 ? ' is-empty' : ''}`}>
                            <label title={item.label}>
                                <input
                                    type="checkbox"
                                    checked={item.selected}
                                    onChange={() => onChange(toggleValue(selection, facet.id, item.value))}
                                />
                                <span className="audit-facet-name">{item.label}</span>
                                <span className="audit-facet-count">{item.count.toLocaleString()}</span>
                            </label>
                        </li>
                    ))}
                    {hidden > 0 || (showAll && values.length > TOP_N) ? (
                        <li>
                            <button type="button" className="audit-facet-more" onClick={() => setShowAll((prev) => !prev)}>
                                {showAll ? '접기' : `더 보기 (${hidden})`}
                            </button>
                        </li>
                    ) : null}
                </ul>
            ) : null}
        </div>
    );
}

export function FacetSidebar({
    records,
    selection,
    onChange,
}: {
    records: AuditRecord[];
    selection: Selection;
    onChange: (next: Selection) => void;
}) {
    return (
        // 바깥(nav)은 세로로 스크롤하고, 안쪽이 여러 단으로 나뉜다 (창 높이가 고정이라 단을 나누는 상자가 스크롤까지 맡으면
        // 넘치는 거르기가 아래가 아니라 옆 단으로 밀려나 보이지 않는다)
        <nav className="audit-facets" aria-label="거르기">
            <div className="audit-facets-columns">
                {FACETS.map((facet) => (
                    <FacetGroup key={facet.id} facet={facet} records={records} selection={selection} onChange={onChange} />
                ))}
            </div>
        </nav>
    );
}
