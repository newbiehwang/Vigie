// 감사 로그 화면: 누가 언제 어떤 질문으로 어떤 도구를 불렀고 결과가 어땠는지 (GET /audit, services/llm/audit.py).
// 관리자만 여는 화면이다. 구성은 Datadog Audit Trail을 따랐다 (패널·배지·버튼 모양은 이 앱의 것을 그대로 쓴다).
//   머리: 제목 · 새로 고침(아이콘)
//   검색창(AuditSearch)
//   필터(FilterMenu: 누르면 기간·그룹 기준·종류·결과·층·요청자·도구·출처·표시를 고르는 창과 필터 초기화, 걸린 조건은 칩) · 건수
//   시간대별 막대그래프(AuditHistogram, 그룹 기준으로 색을 나눠 쌓기·드래그로 기간 좁히기)
//   목록(시각 · 요청자 · 도구 · 요약 · 결과 · 표시). 내려가면 이어서 더 그린다
//   행을 누르면 팝업창(AuditDetailModal, 이 앱의 다른 팝업창과 같은 모양)으로 자세히 보인다. ↑/↓로 앞뒤 기록, 변경 작업은 '층별로 따져 보기'
//
// 거르는 순서: 기간 → 검색어 → 필터. 필터 창의 건수는 검색어까지 적용한 기록에서 센다 (Datadog과 같다)
//
// 기간 안의 기록을 모두 받아(useAuditRecords, 2,000건까지) 거르기·건수·막대는 브라우저에서 계산한다.
// 기간과 거르기는 주소에 담는다(auditUrl.ts): 링크로 같은 화면을 나누고, 새로 고쳐도 남는다
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { LoadingCard, useMinimumVisible } from '@/components/LoadingCard';
import { RefreshButton } from '@/components/RefreshButton';
import { SortButton, sortedBy, useSort } from '@/components/SortHeader';
import { ToastHost, useToast } from '@/components/Toast';
import type { AuditRecord } from '@/types/audit';
import { formatKoreanDateTimeSeconds } from '@/utils/formatters';
import { KindLabel, ResultBadge } from './AuditDetails';
import { AuditHistogram } from './AuditHistogram';
import { AuditDetailModal } from './AuditDetailModal';
import {
    activeCount,
    keyOf,
    kindTextOf,
    matches,
    matchesQuery,
    parseQuery,
    requesterOf,
    resultRankOf,
    summaryOf,
    timeOf,
    type GroupBy,
    type Selection,
} from './auditModel';
import { AuditSearch } from './AuditSearch';
import { readUrl, writeUrl } from './auditUrl';
import { FilterMenu, ResetButton } from './FilterMenu';
import {
    DEFAULT_PERIOD,
    containsRange,
    dayStart,
    fetchRangeOf,
    isPreset,
    windowOf,
    type Period,
} from './timeWindow';
import { MAX_RECORDS, useAuditRecords } from './useAuditRecords';
import './audit.css';

// 목록 정렬: 시각(기본, 최근 것부터) · 요청자 · 도구 · 결과. 요약은 긴 글이라 정렬하지 않는다
type AuditSortKey = 'time' | 'user' | 'tool' | 'result';
const AUDIT_SORT_LABEL: Record<AuditSortKey, string> = { time: '시각', user: '요청자', tool: '도구', result: '결과' };
const auditSortValue = (record: AuditRecord, key: AuditSortKey): string | number =>
    key === 'time' ? timeOf(record) : key === 'user' ? requesterOf(record) : key === 'tool' ? kindTextOf(record) : resultRankOf(record);

const RENDER_STEP = 100; // 목록은 이만큼씩 그린다 (2,000행을 한 번에 그리지 않게). 끝에 닿으면 다음 묶음

// 목록의 행 버튼 (팝업창을 닫으면 여기로 포커스를 돌려준다). data-key로 찾는다
const rowButtonOf = (key: string) =>
    document.querySelector<HTMLButtonElement>(`.audit-row-button[data-key="${CSS.escape(key)}"]`);

// 목록 한 행. 누르면 팝업창으로 자세히 본다.
// 걸린 시간과 표시(Slack·의심 문구·가림 등)는 팝업창에서 보이므로 목록에는 두지 않는다. 도구 칸은 종류와 상관없이 검은 글자
function AuditRow({ record, selected, onOpen }: { record: AuditRecord; selected: boolean; onOpen: () => void }) {
    const summary = summaryOf(record);
    return (
        <li className={`audit-row${selected ? ' is-selected' : ''}`}>
            <button
                type="button"
                className="audit-row-button"
                data-key={keyOf(record)}
                aria-haspopup="dialog"
                aria-current={selected ? 'true' : undefined}
                onClick={onOpen}
            >
                <span className="audit-col-time">{formatKoreanDateTimeSeconds(timeOf(record))}</span>
                <span className="audit-col-user" title={record.userId}>
                    {requesterOf(record)}
                </span>
                <span className="audit-col-tool" title={record.tool}>
                    <KindLabel record={record} />
                </span>
                <span className="audit-col-summary" title={summary}>
                    {summary || <span className="audit-muted">-</span>}
                </span>
                <span className="audit-col-status">
                    <ResultBadge record={record} />
                </span>
            </button>
        </li>
    );
}

// 목록 끝에 닿으면 onReach를 부른다 (다음 묶음을 그린다)
function EndSentinel({ onReach }: { onReach: () => void }) {
    const ref = useRef<HTMLLIElement>(null);
    useEffect(() => {
        const node = ref.current;
        if (!node) return;
        const observer = new IntersectionObserver((entries) => entries[0]?.isIntersecting && onReach(), {
            root: node.parentElement,
            rootMargin: '200px',
        });
        observer.observe(node);
        return () => observer.disconnect();
    }, [onReach]);
    return <li ref={ref} className="audit-sentinel" aria-hidden="true" />;
}

export function AuditPage() {
    const [params, setParams] = useSearchParams();
    const [initial] = useState(() => readUrl(params)); // 처음 열 때만 주소에서 읽는다 (그 뒤로는 화면이 주소를 쓴다)
    const [period, setPeriod] = useState<Period>(initial.period);
    const [selection, setSelection] = useState<Selection>(initial.selection);
    const [query, setQuery] = useState(initial.query);
    const [groupBy, setGroupBy] = useState<GroupBy>(initial.groupBy); // 막대그래프 그룹 기준
    const [now, setNow] = useState(() => Date.now()); // '최근 n시간'의 끝. 새로 고침하면 지금으로
    const baseWindow = useMemo(() => windowOf(period, now), [period, now]); // 받아 올 기간 ('전체'는 보관 90일)

    // 받아 올 범위(UTC 날짜): 이미 받은 범위 안에서 기간을 좁히면(전체 → 7일, 막대 드래그) 다시 받지 않는다.
    // 다만 2,000건 한도로 잘려 기간의 앞쪽이 비어 있으면 그 기간만 다시 받는다
    const wanted = fetchRangeOf(baseWindow);
    const [fetchRange, setFetchRange] = useState(wanted);
    const { records, loading, truncated, error, reload } = useAuditRecords(fetchRange);
    // 불러오지 못하면 패널 위쪽 가운데의 알림으로 (components/Toast). 목록 자리에는 짧은 안내만 남긴다
    const { show: showToast } = useToast();
    useEffect(() => {
        if (error) showToast('error', error);
    }, [error, showToast]);
    const oldest = records.length ? Date.parse(timeOf(records[records.length - 1])) : Infinity;
    useEffect(() => {
        const missing = !containsRange(fetchRange, wanted) || (truncated && !loading && baseWindow.from < oldest);
        if (missing && (fetchRange.from !== wanted.from || fetchRange.to !== wanted.to)) setFetchRange(wanted);
    }, [wanted.from, wanted.to, fetchRange, truncated, loading, baseWindow.from, oldest]); // wanted는 글자 두 개로 비교한다 (매번 새 객체)

    // 보이는 기간: '전체'는 가장 오래된 기록의 날(한국 시간 0시)부터. 보관 90일 중 기록이 없는 앞쪽을 막대그래프에서 비우지 않게
    const isAll = isPreset(period) && period.preset === 'all';
    const timeWindow = useMemo(
        () =>
            isAll && Number.isFinite(oldest)
                ? { from: Math.max(baseWindow.from, dayStart(oldest)), to: baseWindow.to }
                : baseWindow,
        [isAll, oldest, baseWindow],
    );
    const listLoading = useMinimumVisible(loading); // 흰 박스 가운데의 기다림 카드 (최소 1초)

    // 조건을 주소에 담는다 (뒤로 가기가 조건마다 쌓이지 않게 replace). 주소가 바뀌어도 다시 돌지 않게 조건만 지켜본다
    useEffect(() => {
        const next = writeUrl(params, period, selection, query, groupBy);
        if (next.toString() !== params.toString()) setParams(next, { replace: true });
    }, [period, selection, query, groupBy]);

    // 기간 안의 기록 (받은 범위는 UTC 날짜 단위라 기간보다 넓다)
    const inWindow = useMemo(
        () =>
            records.filter((record) => {
                const at = Date.parse(timeOf(record));
                return at >= timeWindow.from && at <= timeWindow.to;
            }),
        [records, timeWindow.from, timeWindow.to],
    );

    const [openKey, setOpenKey] = useState<string | null>(null); // 팝업창으로 보고 있는 기록
    const [limit, setLimit] = useState(RENDER_STEP);

    // 검색어에 맞는 기록 (거르기 목록의 건수도 여기서 센다)
    const parsed = useMemo(() => parseQuery(query), [query]);
    const searched = useMemo(() => inWindow.filter((record) => matchesQuery(record, parsed)), [inWindow, parsed]);
    const filtered = useMemo(() => searched.filter((record) => matches(record, selection)), [searched, selection]);
    // 목록에 보이는 차례 (건수·그래프는 차례와 상관없이 filtered로 센다). 팝업창의 ↑/↓도 이 차례를 따른다
    const [sort, toggleSort] = useSort<AuditSortKey>({ key: 'time', desc: true }, (key) => key === 'time');
    const listed = useMemo(() => sortedBy(filtered, sort, auditSortValue), [filtered, sort]);
    const conditions = activeCount(selection) + (query.trim() ? 1 : 0);
    // 처음 화면(기간 전체, 거르기·검색어 없음, 그룹 기준 결과)과 다른가: '필터 초기화'를 켠다
    const customized =
        conditions > 0 ||
        !isPreset(period) ||
        period.preset !== (DEFAULT_PERIOD as { preset: string }).preset ||
        groupBy !== 'result';

    // 조건이나 기록이 바뀌면 목록을 처음 묶음부터 그린다
    useEffect(() => setLimit(RENDER_STEP), [listed]);

    const openIndex = openKey ? listed.findIndex((record) => keyOf(record) === openKey) : -1;
    // 보고 있던 기록이 거르기로 목록에서 빠지면 팝업창을 닫는다
    useEffect(() => {
        if (openKey && openIndex < 0) setOpenKey(null);
    }, [openKey, openIndex]);

    const closeDetail = () => {
        const key = openKey;
        setOpenKey(null);
        if (key) rowButtonOf(key)?.focus(); // 보던 기록의 행으로 포커스를 돌려준다 (키보드로 이어서 읽는다)
    };

    // 팝업창에서 ↑/↓: 거른 목록의 앞뒤 기록으로 옮기고, 뒤의 목록도 그 행이 보이게 스크롤한다
    const move = (step: -1 | 1) => {
        const next = listed[openIndex + step];
        if (!next) return;
        const key = keyOf(next);
        setLimit((prev) => Math.max(prev, openIndex + step + 1 + RENDER_STEP / 2));
        setOpenKey(key);
        window.requestAnimationFrame(() => rowButtonOf(key)?.scrollIntoView({ block: 'nearest' }));
    };

    const showMore = useCallback(() => setLimit((prev) => prev + RENDER_STEP), []);

    // 필터 초기화(필터 창 안): 처음 열었을 때와 똑같은 화면으로 되돌린다.
    // 조건(거르기·검색어·기간 전체)뿐 아니라 화면 상태(그룹 기준, 열린 필터 창·거르기 목록의 접기·더 보기,
    // 목록 스크롤, 열린 팝업창)도 처음으로. 기간의 끝도 지금으로 맞춘다
    const [resetNo, setResetNo] = useState(0); // 바뀌면 필터 창을 닫고 거르기 목록을 새로 그린다
    const listBody = useRef<HTMLUListElement>(null);
    const resetFilters = () => {
        setSelection({});
        setQuery('');
        setPeriod(DEFAULT_PERIOD);
        setNow(Date.now());
        setGroupBy('result');
        setOpenKey(null);
        setResetNo((n) => n + 1);
        listBody.current?.scrollTo({ top: 0 });
    };

    return (
        <section className="plan-panel audit-panel" aria-label="감사 로그">
            <div className="plan-panel-header">
                <h1 className="plan-panel-eyebrow">감사 로그</h1>
                <RefreshButton
                    onClick={() => {
                        setNow(Date.now());
                        reload();
                    }}
                    loading={loading || listLoading}
                />
            </div>

            <ToastHost />

            <div className="audit-explorer">
                <div className="audit-results">
                    <AuditSearch value={query} onChange={setQuery} />
                    <div className="audit-toolbar">
                        <FilterMenu
                            records={searched}
                            allRecords={records}
                            selection={selection}
                            onChange={setSelection}
                            period={period}
                            window={timeWindow}
                            onPeriod={setPeriod}
                            groupBy={groupBy}
                            onGroupBy={setGroupBy}
                            onReset={resetFilters}
                            canReset={customized}
                            resetNo={resetNo}
                        />
                        <p className="audit-count" role="status">
                            {listLoading ? null : (
                                <>
                                    <strong>{filtered.length.toLocaleString()}건</strong>
                                    {conditions ? <span className="audit-muted"> / {inWindow.length.toLocaleString()}건</span> : null}
                                    {truncated ? (
                                        <span className="audit-truncated">
                                            최근 {MAX_RECORDS.toLocaleString()}건까지만 불러왔습니다. 기간을 줄이면 모두 봅니다
                                        </span>
                                    ) : null}
                                </>
                            )}
                        </p>
                    </div>

                    {/* 다시 불러오는 동안에도 앞 그래프·목록을 흐리게 남겨 둔다 (자리가 들썩이지 않게) */}
                    {error ? null : (
                        <AuditHistogram
                            records={filtered}
                            rankRecords={records}
                            window={timeWindow}
                            loading={listLoading}
                            groupBy={groupBy}
                            onSelect={setPeriod}
                            onFilter={(facet, values) => setSelection((prev) => ({ ...prev, [facet]: values }))}
                        />
                    )}

                    <div className={`plan-table audit-table${listLoading ? ' is-loading' : ''}`}>
                        {/* 머리글을 누르면 그 열로 정렬한다 (다시 누르면 방향을 뒤집는다) */}
                        <div className="plan-table-header audit-table-header">
                            {(['time', 'user', 'tool'] as AuditSortKey[]).map((key) => (
                                <span key={key} className={`audit-col-${key}`}>
                                    <SortButton column={key} label={AUDIT_SORT_LABEL[key]} sort={sort} onSort={toggleSort} announce />
                                </span>
                            ))}
                            <span className="audit-col-summary">요약</span>
                            <span className="audit-col-status">
                                <SortButton column="result" label={AUDIT_SORT_LABEL.result} sort={sort} onSort={toggleSort} announce />
                            </span>
                        </div>

                        {/* 처음 불러올 때는 목록을 비워 두고, 카드는 흰 박스 전체의 가운데에 띄운다 (아래 plan-panel-loading) */}
                        {error && !listLoading ? (
                            <div className="plan-panel-empty">
                                <p>감사 로그를 불러오지 못했습니다. 새로 고침을 눌러 다시 시도해 주세요.</p>
                            </div>
                        ) : error || (listLoading && filtered.length === 0) ? null : filtered.length === 0 ? (
                            <div className="plan-panel-empty">
                                {inWindow.length === 0 ? (
                                    <p>이 기간에 기록이 없습니다. 질문을 보내면 도구 호출마다 기록이 남습니다.</p>
                                ) : (
                                    <div className="audit-empty-reset">
                                        <p>
                                            {query.trim()
                                                ? `'${query.trim()}'에 맞는 기록이 없습니다.`
                                                : '이 조건에 맞는 기록이 없습니다.'}
                                        </p>
                                        <ResetButton onClick={resetFilters} disabled={!customized} />
                                    </div>
                                )}
                            </div>
                        ) : (
                            <ul ref={listBody} className="audit-table-body" aria-label="감사 기록">
                                {listed.slice(0, limit).map((record) => {
                                    const key = keyOf(record);
                                    return (
                                        <AuditRow
                                            key={key}
                                            record={record}
                                            selected={openKey === key}
                                            onOpen={() => setOpenKey(key)}
                                        />
                                    );
                                })}
                                {limit < listed.length ? <EndSentinel onReach={showMore} /> : null}
                            </ul>
                        )}
                    </div>
                </div>
            </div>

            {listLoading ? (
                <div className="plan-panel-loading">
                    <LoadingCard text="감사 로그를 불러오는 중…" />
                </div>
            ) : null}

            {openKey && openIndex >= 0 ? (
                <AuditDetailModal
                    record={listed[openIndex]}
                    onMove={move}
                    onClose={closeDetail}
                />
            ) : null}
        </section>
    );
}
