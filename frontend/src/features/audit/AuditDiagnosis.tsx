// 서비스 진단 층 그림: 진단 도구(diagnoseService) 기록의 팝업창 맨 위에, 처리 단계 고리 대신 보인다.
// 판정은 서버의 진단 절차(mcp/lambda_mcp/diagnose.py)가 낸 그대로이고, 여기서는 일곱 층을 차례로 놓아 그린다.
//
//   진단 층  Application Load Balancer · web-alb · 최근 1시간                    ● 원인 2  ● 증상 1  ● 정상 4
//   [L1 AWS 자체 ● 정상] → [L2 변경 ● 원인] → [L3 입구·네트워크 ● 정상] → [L4 로드 밸런서·게이트웨이 ● 증상]
//   ┌──────────────────────────────────────────────────────────────────────────────────┘   ← 줄을 바꾸는 꺾인 화살표
//   ↓
//   [L5 컴퓨팅 ● 원인]   → [L6 권한·한도 ● 정상] → [L7 데이터·의존성 ● 정상]                  ← 판정만. 고른 칸은 주 색 테두리
//   ─────────────────────────────────────────────────────────────────────────
//   ● 원인  L5 컴퓨팅 · 대상 EC2                                                 ← 아래 설명: 고른 층 하나만
//   인스턴스 2대 모두가 실행 중이 아닙니다 (크게)
//   i-0a1b… (web-1) stopped, … / ec2:DescribeInstances State·StateReason
//   확인한 다른 항목 · 이 층이 보는 것 (옅게)
//
// - 처음에는 가장 먼저 읽어야 할 문제 층을 고른다 (원인 → 계기 L2 → 증상 → 주의, diagnosisModel.problemSections).
//   문제가 없으면 확인 불가 층, 그것도 없으면 L1
// - 그림은 한 줄에 네 층씩 두 줄. 칸 사이는 → 화살표, 첫 줄 끝(L4)에서 둘째 줄 처음(L5)으로는 꺾인 화살표로 잇는다
// - 그림의 칸에는 층 번호 · 이름 · 판정만 둔다. 문장(판정 글 · 부품 · 근거)은 모두 아래 설명에
// - 판정은 배지(점과 글자) 하나로만 보인다. 빨강·노랑은 점에만 쓰고 칸을 칠하지 않는다. 해당 없음은 점선과 옅은 글자
// - 키보드: 칸들은 탭 목록이라 Tab으로 들어가 ←·→·Home·End로 옮기면 그 층이 골라진다
import { Fragment, useRef, useState, type KeyboardEvent } from 'react';
import type { Diagnosis, DiagnosisLayer, DiagnosisLayerId } from '@/types/audit';
import {
    DIAGNOSIS_STATUS,
    FALLBACK_MAP,
    LAYER_ORDER,
    problemSections,
    sectionOf,
    SERVICE_MAPS,
    splitFinding,
    STATUS_ORDER,
    type DetailSection,
} from './diagnosisModel';

// 본 시간: 하루 단위면 날로 (자격 증명 24시간 → 1일, 비용 72시간 → 3일)
const PER_ROW = 4; // 그림 한 줄의 층 수 (일곱 층 → 네 층 + 세 층)
const periodText = (hours: number) => (hours >= 24 && hours % 24 === 0 ? `${hours / 24}일` : `${hours}시간`);

function StatusBadge({ layer, label }: { layer: Pick<DiagnosisLayer, 'status'>; label?: string }) {
    const status = DIAGNOSIS_STATUS[layer.status] ?? DIAGNOSIS_STATUS.unknown;
    return (
        <span className={`badge ${status.badge}`} title={status.hint}>
            {label ?? status.label}
        </span>
    );
}

// 그림의 칸 하나: 층 번호 · 이름 · 판정만. 탭이라 누르면 아래 설명이 이 층으로 바뀐다
function LayerTab({
    layer,
    index,
    selected,
    onPick,
    onKeyDown,
}: {
    layer: DiagnosisLayer;
    index: number; // 그림의 몇 번째 칸인가 (줄과 열을 정한다)
    selected: boolean;
    onPick: () => void;
    onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => void;
}) {
    return (
        <button
            type="button"
            role="tab"
            id={`audit-diag-tab-${layer.id}`}
            aria-selected={selected}
            aria-controls="audit-diag-panel"
            tabIndex={selected ? 0 : -1}
            aria-label={`${layer.id} ${layer.name}, ${DIAGNOSIS_STATUS[layer.status]?.label ?? layer.status}`}
            className={`audit-diag-node is-${layer.status}${selected ? ' is-selected' : ''}`}
            title={layer.component}
            style={cellAt(index)}
            onClick={onPick}
            onKeyDown={onKeyDown}
        >
            <span className="audit-diag-id">{layer.id}</span>
            <span className="audit-diag-name">{layer.name}</span>
            <StatusBadge layer={layer} />
        </button>
    );
}

// 그림의 자리: 칸 n번째는 (n / PER_ROW)번째 줄, 열은 칸 · 화살표 · 칸 · … 이 번갈아 (칸은 홀수 열).
// 칸 줄 사이에는 꺾인 화살표 줄이 하나씩 끼므로 칸 줄은 1 · 3 · 5 …번째
const LAST_COLUMN = PER_ROW * 2 - 1;
const cellAt = (index: number) => ({
    gridRow: Math.floor(index / PER_ROW) * 2 + 1,
    gridColumn: (index % PER_ROW) * 2 + 1,
});

// 칸 사이의 → 화살표
function Arrow({ index }: { index: number }) {
    const cell = cellAt(index);
    return (
        <span className="audit-diag-arrow" style={{ gridRow: cell.gridRow, gridColumn: cell.gridColumn + 1 }} aria-hidden="true">
            <svg viewBox="0 0 18 10" width="18" height="10">
                <path d="M1 5h14M11.5 1.5L15 5l-3.5 3.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
        </span>
    );
}

// 줄을 바꾸는 꺾인 화살표: 윗줄 마지막 칸 아래에서 내려와 왼쪽으로 가로질러, 아랫줄 첫 칸 위로 내려간다.
// 세 조각(내려오며 꺾임 · 가로줄 · 꺾여 내려가며 화살촉)을 각 열에 두어, 칸 폭이 바뀌어도 칸 가운데에 맞는다
function WrapArrow({ row }: { row: number }) {
    const gridRow = row * 2; // 칸 줄 row와 row + 1 사이
    return (
        <>
            <span className="audit-diag-wrap is-start" style={{ gridRow, gridColumn: LAST_COLUMN }} aria-hidden="true" />
            <span className="audit-diag-wrap is-mid" style={{ gridRow, gridColumn: `2 / ${LAST_COLUMN}` }} aria-hidden="true" />
            <span className="audit-diag-wrap is-end" style={{ gridRow, gridColumn: 1 }} aria-hidden="true" />
        </>
    );
}

// 아래 설명 (고른 층 하나): 판정 → 그 판정을 정한 항목(앞말을 크게) → 나머지와 근거 → 확인한 다른 항목 → 이 층이 보는 것(옅게)
function LayerDetail({ section, description }: { section: DetailSection; description: string }) {
    const { layer, label } = section;
    // 판정을 정한 항목: 서버가 층의 finding으로 고른 것과 같다 (층과 판정이 같은 첫 항목)
    const lead = layer.checks.find((check) => check.status === layer.status) ?? layer.checks[0];
    const others = layer.checks.filter((check) => check !== lead);
    const [leadHead, leadRest] = splitFinding(lead?.finding ?? '');
    return (
        <div className="audit-diag-detail" id="audit-diag-panel" role="tabpanel" aria-labelledby={`audit-diag-tab-${layer.id}`}>
            <div className="audit-diag-panel-head">
                <StatusBadge layer={layer} label={label} />
                <span className="audit-diag-id">{layer.id}</span>
                <strong>{layer.name}</strong>
                <span className="audit-diag-panel-component">{layer.component}</span>
            </div>
            {lead ? (
                <div className="audit-diag-lead">
                    <p className="audit-diag-lead-head">{leadHead}</p>
                    {leadRest ? <p className="audit-diag-lead-rest">{leadRest}</p> : null}
                    {lead.evidence ? <code>{lead.evidence}</code> : null}
                </div>
            ) : null}
            {others.length ? (
                <>
                    <p className="audit-diag-others-title">확인한 다른 항목</p>
                    <ul className="audit-diag-checks" aria-label="확인한 다른 항목">
                        {others.map((check, index) => (
                            <li key={`${check.name}-${index}`} className={`is-${check.status}`}>
                                <StatusBadge layer={check} />
                                <div className="audit-diag-check-body">
                                    <strong>{check.name}</strong>
                                    <span>{check.finding}</span>
                                    {check.evidence ? <code>{check.evidence}</code> : null}
                                </div>
                            </li>
                        ))}
                    </ul>
                </>
            ) : null}
            {description ? (
                <p className="audit-diag-about">
                    <span>이 층이 보는 것</span>
                    {description}
                </p>
            ) : null}
        </div>
    );
}

export function AuditDiagnosis({ diagnosis }: { diagnosis: Diagnosis }) {
    const map = SERVICE_MAPS[diagnosis.service] ?? FALLBACK_MAP;
    // 그림의 차례대로 (서버가 준 차례와 상관없이 L1 → L7)
    const layers = LAYER_ORDER.map((id) => diagnosis.layers.find((layer) => layer.id === id)).filter(
        (layer): layer is DiagnosisLayer => !!layer,
    );
    const problems = problemSections(diagnosis.layers);
    const first = problems[0]?.layer.id ?? layers.find((layer) => layer.status === 'unknown')?.id ?? layers[0]?.id;
    const [picked, setPicked] = useState<DiagnosisLayerId | null>(null);
    const selected = layers.find((layer) => layer.id === (picked ?? first));
    const counts = STATUS_ORDER.map((status) => ({
        status,
        n: diagnosis.layers.filter((layer) => layer.status === status).length,
    })).filter((count) => count.n && count.status !== 'skip');
    const tabsRef = useRef<HTMLDivElement>(null);

    // ←·→로 옆 층, Home·End로 끝 층을 고르고 그 칸으로 초점을 옮긴다 (끝에서는 반대쪽 끝으로 돈다)
    const onKeyDown = (index: number) => (event: KeyboardEvent<HTMLButtonElement>) => {
        const last = layers.length - 1;
        const next = { ArrowRight: index === last ? 0 : index + 1, ArrowLeft: index === 0 ? last : index - 1, Home: 0, End: last }[
            event.key
        ];
        if (next === undefined) return;
        event.preventDefault();
        setPicked(layers[next].id);
        tabsRef.current?.querySelector<HTMLElement>(`#audit-diag-tab-${layers[next].id}`)?.focus();
    };

    return (
        <section className="audit-layers audit-diag" aria-labelledby="audit-diag-title">
            <div className="audit-layers-head">
                <h4 id="audit-diag-title" className="audit-layers-title">
                    진단 층
                    <span className="audit-diag-target">
                        {diagnosis.serviceName} · <code>{diagnosis.resource}</code>
                        {diagnosis.target ? (
                            <>
                                {' → '}
                                <code>{diagnosis.target}</code>
                            </>
                        ) : null}{' '}
                        · 최근 {periodText(diagnosis.hours)}
                    </span>
                </h4>
                <ul className="audit-counts" aria-label="층별 판정 개수">
                    {counts.map(({ status, n }) => (
                        <li key={status} className={`is-diag-${status}`}>
                            <span className="audit-counts-dot" aria-hidden="true" />
                            {DIAGNOSIS_STATUS[status].label} <strong>{n}</strong>
                        </li>
                    ))}
                </ul>
            </div>

            {/* 일곱 층: L1 → L4 / 꺾인 화살표 / L5 → L7 */}
            <div
                className="audit-diag-map"
                role="tablist"
                aria-label={`${diagnosis.serviceName} 진단 층. 층을 고르면 아래에 그 층의 설명이 보입니다`}
                ref={tabsRef}
            >
                {layers.map((layer, index) => {
                    const next = index + 1 < layers.length;
                    const rowEnd = (index + 1) % PER_ROW === 0;
                    return (
                        <Fragment key={layer.id}>
                            <LayerTab
                                layer={layer}
                                index={index}
                                selected={layer.id === selected?.id}
                                onPick={() => setPicked(layer.id)}
                                onKeyDown={onKeyDown(index)}
                            />
                            {next && !rowEnd ? <Arrow index={index} /> : null}
                            {next && rowEnd ? <WrapArrow row={(index + 1) / PER_ROW} /> : null}
                        </Fragment>
                    );
                })}
            </div>

            {/* 아래 설명: 고른 층 하나 */}
            <div className="audit-diag-details">
                {problems.length ? null : (
                    <p className="audit-diag-clear">이 절차가 보는 범위에서는 이상이 없습니다. 층을 고르면 확인한 항목이 보입니다.</p>
                )}
                {selected ? <LayerDetail section={sectionOf(selected, problems)} description={map.descriptions[selected.id]} /> : null}
            </div>
        </section>
    );
}
