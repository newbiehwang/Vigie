// 서비스 진단 층 그림: 진단 도구(diagnoseService) 기록의 팝업창 맨 위에, 처리 단계 고리 대신 보인다.
// 판정은 서버의 진단 절차(mcp/lambda_mcp/diagnose.py)가 낸 그대로이고, 여기서는 그 서비스의 길 위에 층을 놓아 그린다.
//
//   진단 층  Application Load Balancer · web-alb · 최근 1시간        ● 원인 2  ● 증상 1  ● 정상 4
//              ┌──────────────── L2 변경 ● 원인 ────────────────┐     ← 위 띠 (시간 축: 길 전체 위)
//   사용자 ›   [L3 입구·네트워크 ● 정상] › [L4 로드 밸런서 ● 증상] › [L5 컴퓨팅 ● 원인] › [L7 ● 정상]   ← 그 서비스의 길
//              [L6 권한·한도 ● 정상]                                    ← 길 옆 (길에 없는 층)
//              └──────────────── L1 AWS 자체 ● 정상 ────────────────┘   ← 바닥
//   ─────────────────────────────────────────────────────────────
//   ● 원인  L5 컴퓨팅 · 대상 EC2                                         ← 아래 설명: 문제가 있는 층을 차례로 모두 펼친다
//   인스턴스 2대 모두가 실행 중이 아닙니다 (크게)                          (원인 → 계기 L2 → 증상 → 주의, diagnosisModel)
//   i-0a1b… (web-1) stopped, … / ec2:DescribeInstances State·StateReason
//   확인한 다른 항목 · 이 층이 보는 것 (옅게)
//   ● 계기  L2 변경 · CloudTrail 쓰기 이벤트
//   관련 변경 2건 …
//
// - 그림의 칸에는 층 번호 · 이름 · 판정만 둔다. 문장(판정 글 · 부품 · 근거)은 모두 아래 설명에
// - 판정은 배지(점과 글자) 하나로만 보인다. 빨강·노랑은 점에만 쓰고 칸을 칠하지 않는다. 해당 없음은 점선과 옅은 글자
// - 칸을 누르면 그 층의 설명으로 옮겨 가 잠깐 표시한다. 펼쳐 있지 않은 층(정상 · 확인 불가 · 해당 없음)은 누르면 펼친다
// - 키보드: 칸·띠는 버튼이라 Tab으로 옮기고 Enter·Space로 누른다
import { useEffect, useRef, useState } from 'react';
import type { Diagnosis, DiagnosisLayer, DiagnosisLayerId } from '@/types/audit';
import {
    bandsOf,
    DIAGNOSIS_STATUS,
    FALLBACK_MAP,
    problemSections,
    SERVICE_MAPS,
    splitFinding,
    STATUS_ORDER,
    type DetailSection,
} from './diagnosisModel';

// 본 시간: 하루 단위면 날로 (자격 증명 24시간 → 1일, 비용 72시간 → 3일)
const periodText = (hours: number) => (hours >= 24 && hours % 24 === 0 ? `${hours / 24}일` : `${hours}시간`);
const FLASH_MS = 1400; // 칸을 눌러 옮겨 간 설명을 표시하는 시간

function StatusBadge({ layer, label }: { layer: Pick<DiagnosisLayer, 'status'>; label?: string }) {
    const status = DIAGNOSIS_STATUS[layer.status] ?? DIAGNOSIS_STATUS.unknown;
    return (
        <span className={`badge ${status.badge}`} title={status.hint}>
            {label ?? status.label}
        </span>
    );
}

// 그림의 칸(길 위 · 길 옆)과 띠(L2 · L1): 층 번호 · 이름 · 판정만
function LayerButton({ layer, variant, onPick }: { layer: DiagnosisLayer; variant: 'node' | 'band'; onPick: () => void }) {
    return (
        <button
            type="button"
            className={`audit-diag-${variant} is-${layer.status}`}
            aria-label={`${layer.id} ${layer.name}, ${DIAGNOSIS_STATUS[layer.status]?.label ?? layer.status}. 설명으로 이동`}
            title={layer.component}
            onClick={onPick}
        >
            <span className="audit-diag-id">{layer.id}</span>
            <span className="audit-diag-name">{layer.name}</span>
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

// 아래 설명 하나: 판정 → 그 판정을 정한 항목(앞말을 크게) → 나머지와 근거 → 확인한 다른 항목 → 이 층이 보는 것(옅게)
function LayerDetail({ section, description, flash }: { section: DetailSection; description: string; flash: boolean }) {
    const { layer, label } = section;
    // 판정을 정한 항목: 서버가 층의 finding으로 고른 것과 같다 (층과 판정이 같은 첫 항목)
    const lead = layer.checks.find((check) => check.status === layer.status) ?? layer.checks[0];
    const others = layer.checks.filter((check) => check !== lead);
    const [leadHead, leadRest] = splitFinding(lead?.finding ?? '');
    return (
        <article className={`audit-diag-detail${flash ? ' is-flash' : ''}`} id={`audit-diag-${layer.id}`} tabIndex={-1}>
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
        </article>
    );
}

export function AuditDiagnosis({ diagnosis }: { diagnosis: Diagnosis }) {
    const map = SERVICE_MAPS[diagnosis.service] ?? FALLBACK_MAP;
    const layerOf = (id: DiagnosisLayerId) => diagnosis.layers.find((layer) => layer.id === id);
    const bands = bandsOf(map);
    const problems = problemSections(diagnosis.layers);
    // 누른 층 중 문제 층이 아닌 것 (누른 차례대로 문제 층 뒤에 펼친다)
    const [extra, setExtra] = useState<DiagnosisLayerId[]>([]);
    const [flash, setFlash] = useState<{ id: DiagnosisLayerId; at: number } | null>(null);
    const sections: DetailSection[] = [
        ...problems,
        ...extra
            .map(layerOf)
            .filter((layer): layer is DiagnosisLayer => !!layer)
            .filter((layer) => !problems.some((section) => section.layer.id === layer.id))
            .map((layer) => ({ layer, label: DIAGNOSIS_STATUS[layer.status]?.label ?? layer.status })),
    ];
    const counts = STATUS_ORDER.map((status) => ({
        status,
        n: diagnosis.layers.filter((layer) => layer.status === status).length,
    })).filter((count) => count.n && count.status !== 'skip');
    const detailsRef = useRef<HTMLDivElement>(null);

    // 누른 층의 설명으로 옮겨 가고, 잠깐 표시한다 (펼치는 것은 다음 그리기에서 끝나므로 그 뒤에 옮긴다)
    useEffect(() => {
        if (!flash) return;
        const target = detailsRef.current?.querySelector<HTMLElement>(`#audit-diag-${flash.id}`);
        const still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
        target?.scrollIntoView({ behavior: still ? 'auto' : 'smooth', block: 'start' });
        target?.focus({ preventScroll: true });
        const timer = window.setTimeout(() => setFlash(null), FLASH_MS);
        return () => window.clearTimeout(timer);
    }, [flash]);

    const pick = (id: DiagnosisLayerId) => {
        setExtra((opened) => (opened.includes(id) ? opened : [...opened, id]));
        setFlash({ id, at: Date.now() });
    };
    const button = (id: DiagnosisLayerId, variant: 'node' | 'band') => {
        const layer = layerOf(id);
        return layer ? <LayerButton key={id} layer={layer} variant={variant} onPick={() => pick(id)} /> : null;
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

            {/* 일곱 층: 위 띠 L2 · 들어오는 쪽 › 그 서비스의 길 › 나가는 쪽 · 길 옆 · 바닥 L1 */}
            <div className="audit-diag-map" role="group" aria-label={`${diagnosis.serviceName} 진단 층. 층을 누르면 그 층의 설명으로 옮겨 갑니다`}>
                <div className="audit-diag-lane">{button(bands.top, 'band')}</div>
                <div className="audit-diag-entry" aria-hidden="true">
                    <span className="audit-diag-end">{map.entry}</span>
                    <Arrow />
                </div>
                <ol className="audit-diag-path audit-diag-lane">
                    {map.path.map((id, index) => (
                        <li key={id} className="audit-diag-step">
                            {index ? <Arrow /> : null}
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
                {bands.side.length ? (
                    <div className="audit-diag-side audit-diag-lane">{bands.side.map((id) => button(id, 'node'))}</div>
                ) : null}
                <div className="audit-diag-lane">{button(bands.floor, 'band')}</div>
            </div>

            {/* 아래 설명: 문제가 있는 층을 차례로 모두 (누른 층은 그 뒤에) */}
            <div className="audit-diag-details" ref={detailsRef} aria-live="polite">
                {sections.length ? (
                    sections.map((section) => (
                        <LayerDetail
                            key={section.layer.id}
                            section={section}
                            description={map.descriptions[section.layer.id]}
                            flash={flash?.id === section.layer.id}
                        />
                    ))
                ) : (
                    <p className="audit-diag-clear">이 절차가 보는 범위에서는 이상이 없습니다. 층을 누르면 확인한 항목이 보입니다.</p>
                )}
            </div>
        </section>
    );
}
