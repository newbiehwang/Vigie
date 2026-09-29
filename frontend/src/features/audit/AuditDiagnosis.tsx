// 서비스 진단 층 그림(제목 '진단 단계'): 진단 도구(diagnoseService) 기록의 팝업창 맨 위에, 처리 단계 고리 대신 보인다.
// 판정은 서버의 진단 절차(mcp/lambda_mcp/diagnose.py)가 낸 그대로이고, 여기서는 일곱 층을 위에서 아래로 쌓아 그린다.
// 모양 · 색 · 움직임은 처리 단계 그림(AuditLayers.tsx)을 따른다: 왼쪽 그림, 오른쪽 설명 칸.
//
//   진단 단계  Application Load Balancer · web-alb · 최근 1시간              ● 원인 2  ● 의심 1  ● 정상 4
//    ╲ L1 AWS ╱                          [L5] 인스턴스·실행 환경  대상 EC2              ● 원인
//    ╲ L2 리소스 변경 기록 ╱               대상이 실행되나. 인스턴스 상태, OS 상태 검사, …   ← 이 층이 보는 것
//    ╲ L3 네트워크 경로 ╱                  ┃ 인스턴스 2대 모두가 실행 중이 아닙니다           ← 판정(왼쪽 선 색 = 판정)
//    ╲ L4 로드 밸런서·게이트웨이 ╱          ┃ ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
//      ╲ L5 인스턴스·실행 환경 ╱ ▸         ┃ i-0a1b… (web-1) stopped, … / ec2:DescribeInstances
//    ╲ L6 권한·한도 ╱                      ┌ 확인한 다른 항목 (점선 상자) ┐
//    ╲ L7 데이터·의존성 ╱                  └───────────────────────────┘
//
// - 조각은 아래를 가리키는 셰브런 7개(위 L1 → 아래 L7). 조각 바탕 = 판정 (원인 옅은 빨강 · 의심 옅은 노랑 · 정상 옅은 회색)
// - 판정은 원인 · 의심 · 정상 세 가지로 묶어 보인다 (diagnosisModel.VIEW_OF)
// - 고른 조각은 그림자와 함께 설명 칸 쪽(오른쪽)으로 살짝 떠오른다. 다른 층을 고르면 나머지 조각은 은은하게 옅어진다
// - 처음에는 가장 먼저 읽어야 할 층을 고른다 (원인 → L2 원인 → 증상 → 주의 → 확인 불가, diagnosisModel.firstLayer).
//   고른 조각을 다시 누르면 처음 층으로 돌아온다
// - 키보드: 조각들은 탭 목록이라 Tab으로 들어가 ↑·↓(←·→)·Home·End로 옮기면 그 층이 골라진다
import { useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';
import type { Diagnosis, DiagnosisLayer, DiagnosisLayerId, DiagnosisStatus } from '@/types/audit';
import {
    FALLBACK_MAP,
    firstLayer,
    LAYER_ORDER,
    SERVICE_MAPS,
    splitFinding,
    VIEW_ORDER,
    VIEW_STATUS,
    viewOf,
    type ViewStatus,
} from './diagnosisModel';

// 본 시간: 하루 단위면 날로 (자격 증명 24시간 → 1일, 비용 72시간 → 3일)
const periodText = (hours: number) => (hours >= 24 && hours % 24 === 0 ? `${hours / 24}일` : `${hours}시간`);

// 처리 단계 그림의 판정 색 이름 (판정 개수의 점 · 설명 칸 판정 상자의 왼쪽 선): 원인 실패 빨강 · 의심 주의 노랑 · 정상 파랑
const TONE: Record<ViewStatus, 'fail' | 'warn' | 'ok'> = { cause: 'fail', suspect: 'warn', ok: 'ok' };

function StatusBadge({ status, className = '' }: { status: DiagnosisStatus; className?: string }) {
    const view = VIEW_STATUS[viewOf(status)];
    return (
        <span className={`badge ${view.badge}${className ? ` ${className}` : ''}`} title={view.hint}>
            {view.label}
        </span>
    );
}

// 설명 칸 (고른 층 하나): 층 번호 · 이름 · 부품 · 판정 → 이 층이 보는 것 → 판정 상자(판정을 정한 항목) → 확인한 다른 항목
function LayerPanel({ layer, description }: { layer: DiagnosisLayer; description: string }) {
    // 판정을 정한 항목: 서버가 층의 finding으로 고른 것과 같다 (층과 판정이 같은 첫 항목)
    const lead = layer.checks.find((check) => check.status === layer.status) ?? layer.checks[0];
    const others = layer.checks.filter((check) => check !== lead);
    const [leadHead, leadRest] = splitFinding(lead?.finding ?? '');
    return (
        <div
            className="audit-cycle-panel audit-diag-panel"
            id="audit-diag-panel"
            role="tabpanel"
            aria-labelledby={`audit-diag-tab-${layer.id}`}
            aria-live="polite"
        >
            <div className="audit-cycle-panel-head">
                <span className="audit-cycle-panel-no">{layer.id}</span>
                <strong>{layer.name}</strong>
                <span className="audit-cycle-en">{layer.component}</span>
                <StatusBadge status={layer.status} className="audit-trace-badge" />
            </div>
            {description ? <p className="audit-cycle-panel-text">{description}</p> : null}
            {lead ? (
                <div className={`audit-trace-answer-box is-${TONE[viewOf(layer.status)]}`}>
                    <p className="audit-diag-answer">{leadHead}</p>
                    {leadRest || lead.evidence ? (
                        <div className="audit-diag-answer-more">
                            {leadRest ? <p>{leadRest}</p> : null}
                            {lead.evidence ? <code>{lead.evidence}</code> : null}
                        </div>
                    ) : null}
                </div>
            ) : null}
            {others.length ? (
                <div className="audit-evidence is-mark">
                    <span className="audit-evidence-title">확인한 다른 항목</span>
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
                </div>
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
    const first = firstLayer(layers);
    // 고른 층: 누른 조각, 누르기 전에는 처음 층
    const [picked, setPicked] = useState<DiagnosisLayerId | null>(null);
    const selected = layers.find((layer) => layer.id === picked) ?? first;
    const counts = VIEW_ORDER.map((view) => ({
        view,
        n: layers.filter((layer) => viewOf(layer.status) === view).length,
    })).filter((count) => count.n);
    const clear = layers.every((layer) => viewOf(layer.status) === 'ok');
    const stackRef = useRef<HTMLDivElement>(null);

    // 누르면 그 층을 고르고, 고른 조각을 다시 누르면 처음 층으로 돌아온다
    const choose = (id: DiagnosisLayerId) => setPicked(id === selected?.id || id === first?.id ? null : id);

    // ↑·↓(←·→)로 옆 층, Home·End로 끝 층을 고르고 그 조각으로 초점을 옮긴다 (끝에서는 반대쪽 끝으로 돈다)
    const onKeyDown = (index: number) => (event: KeyboardEvent<HTMLButtonElement>) => {
        const last = layers.length - 1;
        const down = index === last ? 0 : index + 1;
        const up = index === 0 ? last : index - 1;
        const next = { ArrowDown: down, ArrowRight: down, ArrowUp: up, ArrowLeft: up, Home: 0, End: last }[event.key];
        if (next === undefined) return;
        event.preventDefault();
        const id = layers[next].id;
        setPicked(id === first?.id ? null : id);
        stackRef.current?.querySelector<HTMLElement>(`#audit-diag-tab-${id}`)?.focus();
    };

    return (
        <section className="audit-layers audit-diag" aria-labelledby="audit-diag-title">
            <div className="audit-layers-head">
                <h4 id="audit-diag-title" className="audit-layers-title">
                    진단 단계
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
                {/* 판정 개수: 처리 단계 그림과 같은 점 (원인 빨강 · 의심 노랑 · 정상 파랑) */}
                <ul className="audit-counts" aria-label="층별 판정 개수">
                    {counts.map(({ view, n }) => (
                        <li key={view} className={`is-${TONE[view]}`}>
                            <span className="audit-counts-dot" aria-hidden="true" />
                            {VIEW_STATUS[view].label} <strong>{n}</strong>
                        </li>
                    ))}
                </ul>
            </div>
            {clear ? <p className="audit-layers-question">이 절차가 보는 범위에서는 이상이 없습니다. 층을 고르면 확인한 항목이 보입니다.</p> : null}

            <div className="audit-cycle audit-diag-body">
                {/* 일곱 층: 위 L1 → 아래 L7, 아래를 가리키는 셰브런 */}
                <div
                    className={`audit-stack${picked ? ' has-pick' : ''}`}
                    role="tablist"
                    aria-orientation="vertical"
                    aria-label={`${diagnosis.serviceName} 진단 단계. 층을 고르면 오른쪽에 그 층의 설명이 보입니다`}
                    ref={stackRef}
                >
                    {layers.map((layer, index) => {
                        const view = viewOf(layer.status);
                        const isSelected = layer.id === selected?.id;
                        return (
                            <button
                                key={layer.id}
                                type="button"
                                role="tab"
                                id={`audit-diag-tab-${layer.id}`}
                                aria-selected={isSelected}
                                aria-controls="audit-diag-panel"
                                tabIndex={isSelected ? 0 : -1}
                                aria-label={`${layer.id} ${layer.name}, ${VIEW_STATUS[view].label}`}
                                title={`${layer.component} · ${VIEW_STATUS[view].label}`}
                                className={`audit-stack-seg is-${view}${isSelected ? ' is-selected' : ''}`}
                                style={{ animationDelay: `${index * 45}ms` } as CSSProperties}
                                onClick={() => choose(layer.id)}
                                onKeyDown={onKeyDown(index)}
                            >
                                <span className="audit-stack-shape">
                                    <span className="audit-stack-no">{layer.id}</span>
                                    <span className="audit-stack-label">{layer.name}</span>
                                </span>
                            </button>
                        );
                    })}
                </div>

                {/* key: 층을 바꾸면 설명 칸이 새로 나타난다 (처리 단계 그림처럼 옅게 떠오름) */}
                {selected ? <LayerPanel key={selected.id} layer={selected} description={map.descriptions[selected.id]} /> : null}
            </div>
        </section>
    );
}
