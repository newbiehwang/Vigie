// 서비스 진단 층 그림: 진단 도구(diagnoseService) 기록의 팝업창 맨 위에, 처리 단계 고리 대신 보인다.
// 판정은 서버의 진단 절차(mcp/lambda_mcp/diagnose.py)가 낸 그대로이고, 여기서는 그 서비스의 길 위에 층을 놓아 그린다.
//
//   진단 층  Application Load Balancer · web-alb · 최근 1시간        ● 원인 2  ● 증상 1  ● 정상 4
//   ┌ L2 변경  관련 변경 2건 — 09-29 15:16 StopInstances (alice → …) 외 1건  ● 원인 ┐   ← 위 띠 (시간 축: 계기)
//   사용자 › [L3 입구·네트워크] › [L4 로드 밸런서] › [L5 컴퓨팅] › [L7 데이터·의존성]  ← 그 서비스의 길
//   └ 길에 없는 층 중 원인·증상·주의·확인 불가만 띠로                                 ┘
//     띠에는 원인·증상·주의면 판정 한 줄(diagnosisModel.briefOf), 아니면 그 층이 보는 부품
//   그 밖의 층  [L6 권한·한도 ● 정상] [L1 AWS 자체 ● 정상]                             ← 조용한 층은 작은 칩
//   L5 컴퓨팅 · 대상 EC2  ● 원인                                                       ← 설명 칸
//   이 서비스에서 이 층이 묻는 것 (옅은 글자)
//   ● 원인  인스턴스 상태  찾은 것 / 근거 API·지표
//
// - 판정은 배지(점과 글자) 하나로만 보인다. 빨강·노랑은 점에만 쓰고 칸을 칠하지 않는다. 고른 층은 주 색 테두리
// - 해당 없음 층은 점선 테두리와 옅은 글자
// - 처음 고른 층: 원인 중 L2가 아닌 것(무엇이 고장 났나) → L2 → 증상 → 길의 첫 층. 칸·띠·칩을 누르면 그 층의 설명.
//   무엇이 고장 났나는 처음부터 열린 설명 칸이, 무엇이 계기였나는 L2 띠의 한 줄이 말한다 (같은 문장을 두 번 쓰지 않는다)
// - 키보드: 칸·띠·칩은 버튼이라 Tab으로 옮기고 Enter·Space로 고른다
import { useState } from 'react';
import type { Diagnosis, DiagnosisLayer, DiagnosisLayerId } from '@/types/audit';
import {
    bandsOf,
    briefOf,
    DIAGNOSIS_STATUS,
    FALLBACK_MAP,
    FINDING_STATUSES,
    QUIET_STATUSES,
    SERVICE_MAPS,
    STATUS_ORDER,
} from './diagnosisModel';

// 본 시간: 하루 단위면 날로 (자격 증명 24시간 → 1일, 비용 72시간 → 3일)
const periodText = (hours: number) => (hours >= 24 && hours % 24 === 0 ? `${hours / 24}일` : `${hours}시간`);

// 처음 고를 층
function firstPick(diagnosis: Diagnosis, path: DiagnosisLayerId[]): DiagnosisLayerId {
    const byStatus = (status: string) => diagnosis.layers.filter((layer) => layer.status === status).map((layer) => layer.id);
    const causes = byStatus('cause');
    return causes.find((id) => id !== 'L2') ?? causes[0] ?? byStatus('symptom')[0] ?? path[0] ?? 'L1';
}

function StatusBadge({ layer }: { layer: Pick<DiagnosisLayer, 'status'> }) {
    const status = DIAGNOSIS_STATUS[layer.status] ?? DIAGNOSIS_STATUS.unknown;
    return (
        <span className={`badge ${status.badge}`} title={status.hint}>
            {status.label}
        </span>
    );
}

// 칸(길 위의 층)과 띠(길에 없는 층)가 같이 쓰는 버튼
function LayerButton({
    layer,
    variant,
    selected,
    onPick,
}: {
    layer: DiagnosisLayer;
    variant: 'node' | 'band';
    selected: boolean;
    onPick: () => void;
}) {
    const finding = FINDING_STATUSES.includes(layer.status) ? briefOf(layer.finding || '') : '';
    return (
        <button
            type="button"
            className={`audit-diag-${variant} is-${layer.status}${selected ? ' is-selected' : ''}`}
            aria-pressed={selected}
            aria-label={`${layer.id} ${layer.name}, ${layer.component}, ${DIAGNOSIS_STATUS[layer.status]?.label ?? layer.status}`}
            onClick={onPick}
        >
            <span className="audit-diag-id">{layer.id}</span>
            <span className="audit-diag-names">
                <span className="audit-diag-name">{layer.name}</span>
                {/* 띠에 이상이 있으면 부품 대신 판정 한 줄 (L2 띠면 계기). 칸에서는 부품을 한 줄로 줄인다
                    (넘치면 말줄임표, 전체는 마우스를 올리면) */}
                {variant === 'band' && finding ? (
                    <span className="audit-diag-finding">{finding}</span>
                ) : (
                    <span className="audit-diag-component" title={layer.component}>
                        {layer.component}
                    </span>
                )}
            </span>
            <StatusBadge layer={layer} />
        </button>
    );
}

function Arrow() {
    return (
        <svg className="audit-diag-arrow" viewBox="0 0 8 12" width="8" height="12" aria-hidden="true">
            <path d="M1.5 1.5L6 6l-4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
    );
}

export function AuditDiagnosis({ diagnosis }: { diagnosis: Diagnosis }) {
    const map = SERVICE_MAPS[diagnosis.service] ?? FALLBACK_MAP;
    const layerOf = (id: DiagnosisLayerId) => diagnosis.layers.find((layer) => layer.id === id);
    const [picked, setPicked] = useState<DiagnosisLayerId>(() => firstPick(diagnosis, map.path));
    const selected = layerOf(picked);
    const bands = bandsOf(map);
    // 길에 없는 층: 이상이 있거나 볼 수 없었던 층은 띠로, 조용한 층(정상 · 해당 없음)은 한 줄의 칩으로
    const below = bands.below.map(layerOf).filter((layer): layer is DiagnosisLayer => !!layer);
    const loud = below.filter((layer) => !QUIET_STATUSES.includes(layer.status));
    const quiet = below.filter((layer) => QUIET_STATUSES.includes(layer.status));
    const counts = STATUS_ORDER.map((status) => ({
        status,
        n: diagnosis.layers.filter((layer) => layer.status === status).length,
    })).filter((count) => count.n && count.status !== 'skip');

    const button = (id: DiagnosisLayerId, variant: 'node' | 'band') => {
        const layer = layerOf(id);
        return layer ? (
            <LayerButton key={id} layer={layer} variant={variant} selected={picked === id} onPick={() => setPicked(id)} />
        ) : null;
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

            {/* 그 서비스의 길 위에 놓은 층들 */}
            <div className="audit-diag-map" role="group" aria-label={`${diagnosis.serviceName} 진단 층. 층을 누르면 설명이 나옵니다`}>
                {button(bands.top, 'band')}
                <ol className="audit-diag-path">
                    <li className="audit-diag-end" aria-hidden="true">
                        {map.entry}
                    </li>
                    {map.path.map((id) => (
                        <li key={id} className="audit-diag-step">
                            <Arrow />
                            {button(id, 'node')}
                        </li>
                    ))}
                    {map.exit ? (
                        <li className="audit-diag-step is-end" aria-hidden="true">
                            <Arrow />
                            <span className="audit-diag-end">{map.exit}</span>
                        </li>
                    ) : null}
                </ol>
                {loud.map((layer) => button(layer.id, 'band'))}
                {quiet.length ? (
                    <div className="audit-diag-quiet">
                        <span className="audit-diag-quiet-label">그 밖의 층</span>
                        {quiet.map((layer) => (
                            <button
                                key={layer.id}
                                type="button"
                                className={`audit-diag-chip is-${layer.status}${picked === layer.id ? ' is-selected' : ''}`}
                                aria-pressed={picked === layer.id}
                                aria-label={`${layer.id} ${layer.name}, ${layer.component}, ${DIAGNOSIS_STATUS[layer.status]?.label ?? layer.status}`}
                                title={layer.component}
                                onClick={() => setPicked(layer.id)}
                            >
                                <span className="audit-diag-id">{layer.id}</span>
                                <span className="audit-diag-name">{layer.name}</span>
                                <StatusBadge layer={layer} />
                            </button>
                        ))}
                    </div>
                ) : null}
            </div>

            {/* 설명 칸 (aria-live: 다른 층을 고르면 새 설명을 읽는다) */}
            {selected ? (
                <div className="audit-diag-panel" aria-live="polite" key={selected.id}>
                    <div className="audit-cycle-panel-head">
                        <span className="audit-cycle-panel-no">{selected.id}</span>
                        <strong>{selected.name}</strong>
                        <span className="audit-cycle-en">{selected.component}</span>
                        <StatusBadge layer={selected} />
                    </div>
                    {map.descriptions[selected.id] ? (
                        <p className="audit-cycle-panel-text">{map.descriptions[selected.id]}</p>
                    ) : null}
                    {selected.checks.length ? (
                        <ul className="audit-diag-checks" aria-label="확인한 항목">
                            {selected.checks.map((check, index) => (
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
                    ) : null}
                </div>
            ) : null}
        </section>
    );
}
