// 서비스 진단 층 그림: 진단 도구(diagnoseService) 기록의 팝업창 맨 위에, 처리 단계 고리 대신 보인다.
// 판정은 서버의 진단 절차(mcp/lambda_mcp/diagnose.py)가 낸 그대로이고, 여기서는 일곱 층을 차례로 놓아 그린다.
//
//   진단 층  Application Load Balancer · web-alb · 최근 1시간                    ● 원인 2  ● 증상 1  ● 정상 4
//   [L1 AWS 자체 ● 정상] [L2 변경 ● 원인] [L3 입구·네트워크 ● 정상] [L4 로드 밸런서·게이트웨이 ● 증상]
//   [L5 컴퓨팅 ● 원인] [L6 권한·한도 ● 정상] [L7 데이터·의존성 ● 정상]     ← 실제로는 가로 한 줄 (판정만). 고른 층은 아래에 주 색 선
//   ─────────────────────────────────────────────────────────────────────────
//   ● 원인  L5 컴퓨팅 · 대상 EC2                                                 ← 아래 설명: 고른 층 하나만
//   인스턴스 2대 모두가 실행 중이 아닙니다 (크게)
//   i-0a1b… (web-1) stopped, … / ec2:DescribeInstances State·StateReason
//   확인한 다른 항목 · 이 층이 보는 것 (옅게)
//
// - 처음에는 가장 먼저 읽어야 할 문제 층을 고른다 (원인 → 계기 L2 → 증상 → 주의, diagnosisModel.problemSections).
//   문제가 없으면 확인 불가 층, 그것도 없으면 L1
// - 그림의 칸에는 층 번호 · 이름 · 판정만 둔다. 문장(판정 글 · 부품 · 근거)은 모두 아래 설명에
// - 판정은 배지(점과 글자) 하나로만 보인다. 빨강·노랑은 점에만 쓰고 칸을 칠하지 않는다. 해당 없음은 점선과 옅은 글자
// - 키보드: 칸들은 탭 목록이라 Tab으로 들어가 ←·→·Home·End로 옮기면 그 층이 골라진다
import { useRef, useState, type KeyboardEvent } from 'react';
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
    selected,
    onPick,
    onKeyDown,
}: {
    layer: DiagnosisLayer;
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
            onClick={onPick}
            onKeyDown={onKeyDown}
        >
            <span className="audit-diag-id">{layer.id}</span>
            <span className="audit-diag-name">{layer.name}</span>
            <StatusBadge layer={layer} />
        </button>
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

            {/* 일곱 층: L1 → L7 가로 한 줄 */}
            <div
                className="audit-diag-map"
                role="tablist"
                aria-label={`${diagnosis.serviceName} 진단 층. 층을 고르면 아래에 그 층의 설명이 보입니다`}
                ref={tabsRef}
            >
                {layers.map((layer, index) => (
                    <LayerTab
                        key={layer.id}
                        layer={layer}
                        selected={layer.id === selected?.id}
                        onPick={() => setPicked(layer.id)}
                        onKeyDown={onKeyDown(index)}
                    />
                ))}
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
