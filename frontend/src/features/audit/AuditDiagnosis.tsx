// 서비스 진단 층 그림: 진단 도구(diagnoseService) 기록의 팝업창 맨 위에, 처리 단계 고리 대신 보인다.
// 판정은 서버의 진단 절차(mcp/lambda_mcp/diagnose.py)가 낸 그대로이고, 여기서는 일곱 층을 차례로 놓아 그린다.
//
//   진단 층  Application Load Balancer · web-alb · 최근 1시간                    □ 원인 2  □ 의심 1  □ 정상 4
//   [L1 AWS] → [L2 리소스 변경 기록] → [L3 네트워크 경로] → [L4 로드 밸런서·게이트웨이]   ← 칸 테두리 색이 판정
//        → [L5 인스턴스·실행 환경] → [L6 권한·한도] → [L7 데이터·의존성]              ← 둘째 줄은 가운데로
//   ─────────────────────────────────────────────────────────────────────────
//   L5 인스턴스·실행 환경 대상 EC2 ● 원인                                        ← 아래 설명: 고른 층 하나만
//   인스턴스 2대 모두가 실행 중이 아닙니다 (크게)
//   i-0a1b… (web-1) stopped, … / ec2:DescribeInstances State·StateReason
//   확인한 다른 항목 · 이 층이 보는 것 (옅게)
//
// - 판정은 원인 · 의심 · 정상 세 가지로 묶어 보인다 (diagnosisModel.VIEW_OF). 칸에는 판정 글 없이 테두리 색으로만,
//   판정 글은 아래 설명 제목 뒤에만 둔다. 고른 칸은 테두리 색을 옅게 한 바탕
// - 그림은 한 줄에 네 층씩. 칸 사이와 둘째 줄 첫 칸 앞은 → 화살표
// - 처음에는 가장 먼저 읽어야 할 층을 고른다 (원인 → L2 원인 → 증상 → 주의 → 확인 불가, diagnosisModel.firstLayer)
// - 키보드: 칸들은 탭 목록이라 Tab으로 들어가 ←·→·Home·End로 옮기면 그 층이 골라진다
import { Fragment, useRef, useState, type KeyboardEvent } from 'react';
import type { Diagnosis, DiagnosisLayer, DiagnosisLayerId, DiagnosisStatus } from '@/types/audit';
import { FALLBACK_MAP, firstLayer, LAYER_ORDER, SERVICE_MAPS, splitFinding, VIEW_ORDER, VIEW_STATUS, viewOf } from './diagnosisModel';

const PER_ROW = 4; // 그림 한 줄의 층 수 (일곱 층 → 네 층 + 세 층)
// 본 시간: 하루 단위면 날로 (자격 증명 24시간 → 1일, 비용 72시간 → 3일)
const periodText = (hours: number) => (hours >= 24 && hours % 24 === 0 ? `${hours / 24}일` : `${hours}시간`);

function StatusBadge({ status }: { status: DiagnosisStatus }) {
    const view = VIEW_STATUS[viewOf(status)];
    return (
        <span className={`badge ${view.badge}`} title={view.hint}>
            {view.label}
        </span>
    );
}

// 그림의 칸 하나: 층 번호와 이름만 (판정은 테두리 색). 탭이라 누르면 아래 설명이 이 층으로 바뀐다
function LayerTab({
    layer,
    selected,
    onPick,
    onKeyDown,
}: {
    layer: DiagnosisLayer;
    selected: boolean;
    onPick: () => void;
    onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => void;
}) {
    const view = viewOf(layer.status);
    return (
        <button
            type="button"
            role="tab"
            id={`audit-diag-tab-${layer.id}`}
            aria-selected={selected}
            aria-controls="audit-diag-panel"
            tabIndex={selected ? 0 : -1}
            aria-label={`${layer.id} ${layer.name}, ${VIEW_STATUS[view].label}`}
            className={`audit-diag-node is-${view}${selected ? ' is-selected' : ''}`}
            title={`${layer.component} · ${VIEW_STATUS[view].label}`}
            onClick={onPick}
            onKeyDown={onKeyDown}
        >
            <span className="audit-diag-id">{layer.id}</span>
            <span className="audit-diag-name">{layer.name}</span>
        </button>
    );
}

// → 화살표 (칸 사이, 둘째 줄 첫 칸 앞)
function Arrow() {
    return (
        <span className="audit-diag-arrow" aria-hidden="true">
            <svg viewBox="0 0 18 10" width="18" height="10">
                <path d="M1 5h14M11.5 1.5L15 5l-3.5 3.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
        </span>
    );
}

// 아래 설명 (고른 층 하나): 층 제목과 판정 → 그 판정을 정한 항목(앞말을 크게) → 나머지와 근거 → 확인한 다른 항목 → 이 층이 보는 것(옅게)
function LayerDetail({ layer, description }: { layer: DiagnosisLayer; description: string }) {
    // 판정을 정한 항목: 서버가 층의 finding으로 고른 것과 같다 (층과 판정이 같은 첫 항목)
    const lead = layer.checks.find((check) => check.status === layer.status) ?? layer.checks[0];
    const others = layer.checks.filter((check) => check !== lead);
    const [leadHead, leadRest] = splitFinding(lead?.finding ?? '');
    return (
        <div className="audit-diag-detail" id="audit-diag-panel" role="tabpanel" aria-labelledby={`audit-diag-tab-${layer.id}`}>
            <div className="audit-diag-panel-head">
                <span className="audit-diag-id">{layer.id}</span>
                <strong>{layer.name}</strong>
                <span className="audit-diag-panel-component">{layer.component}</span>
                <StatusBadge status={layer.status} />
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
                            <li key={`${check.name}-${index}`}>
                                <StatusBadge status={check.status} />
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
    const rows = Array.from({ length: Math.ceil(layers.length / PER_ROW) }, (_, row) => layers.slice(row * PER_ROW, (row + 1) * PER_ROW));
    const [picked, setPicked] = useState<DiagnosisLayerId | null>(null);
    const selected = layers.find((layer) => layer.id === picked) ?? firstLayer(layers);
    const counts = VIEW_ORDER.map((view) => ({
        view,
        n: layers.filter((layer) => viewOf(layer.status) === view).length,
    })).filter((count) => count.n);
    const clear = layers.every((layer) => viewOf(layer.status) === 'ok');
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
                {/* 범례를 겸한 판정 개수: 칸과 같은 모양의 테두리 사각형 */}
                <ul className="audit-counts" aria-label="층별 판정 개수">
                    {counts.map(({ view, n }) => (
                        <li key={view} className={`is-diag-${view}`}>
                            <span className="audit-counts-dot" aria-hidden="true" />
                            {VIEW_STATUS[view].label} <strong>{n}</strong>
                        </li>
                    ))}
                </ul>
            </div>

            {/* 일곱 층: 한 줄에 네 층씩. 둘째 줄부터는 첫 칸 앞에 → (칸들은 가운데로) */}
            <div
                className="audit-diag-map"
                role="tablist"
                aria-label={`${diagnosis.serviceName} 진단 층. 층을 고르면 아래에 그 층의 설명이 보입니다`}
                ref={tabsRef}
            >
                {rows.map((row, rowIndex) => (
                    <div key={rowIndex} className="audit-diag-row">
                        {row.map((layer, column) => {
                            const index = rowIndex * PER_ROW + column;
                            return (
                                <Fragment key={layer.id}>
                                    {column || rowIndex ? <Arrow /> : null}
                                    <LayerTab
                                        layer={layer}
                                        selected={layer.id === selected?.id}
                                        onPick={() => setPicked(layer.id)}
                                        onKeyDown={onKeyDown(index)}
                                    />
                                </Fragment>
                            );
                        })}
                        {/* 둘째 줄부터: 앞의 화살표만큼 뒤를 비워 칸들이 가운데에 오게 한다 */}
                        {rowIndex ? <span className="audit-diag-arrow" aria-hidden="true" /> : null}
                    </div>
                ))}
            </div>

            {/* 아래 설명: 고른 층 하나 */}
            <div className="audit-diag-details">
                {clear ? (
                    <p className="audit-diag-clear">이 절차가 보는 범위에서는 이상이 없습니다. 층을 고르면 확인한 항목이 보입니다.</p>
                ) : null}
                {selected ? <LayerDetail layer={selected} description={map.descriptions[selected.id]} /> : null}
            </div>
        </section>
    );
}
