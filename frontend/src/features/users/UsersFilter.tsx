// 사용자 관리의 검색·필터. 감사 로그의 필터(FilterMenu·FacetSidebar)와 같은 모양·같은 클래스를 쓴다
//   [필터 1 ▾]  권한: 결정자 ✕   계정: 정지 ✕                         5명 / 12명
//   ┌───────────────────────────────────────────────┐
//   │                                     [↺ 필터 초기화] ✕ │
//   │ ▾ 권한           ▾ 계정           ▾ 가입               │
//   │ ☐ 일반 사용자 7   ☑ 사용 중  10    ☐ 초대됨(첫 로그인 전) 2 │
//   │ ☑ 결정자     3   ☐ 정지      2    ☐ 가입 완료          10 │
//   │ ☐ 관리자     2                                         │
//   └───────────────────────────────────────────────┘
// - 고르면 바로 목록에 적용된다. 한 거르기 안에서 여러 값을 고르면 그중 하나(또는), 거르기끼리는 모두(그리고)
// - 건수는 검색어와 다른 거르기를 적용하고 이 거르기만 뺀 사용자에서 센다 (감사 로그와 같다)
// - 바깥을 누르거나 Esc, 오른쪽 위 ✕로 닫는다. '필터 초기화'는 검색어와 거르기를 모두 지운다
import { useEffect, useRef, useState } from 'react';
import { ROLE_LABELS } from '@/auth/authClient';
import { ResetButton } from '@/features/audit/FilterMenu';
import type { ManagedUser } from '@/types/users';

export type UserFacetId = 'role' | 'account' | 'signup';
export type UserSelection = Partial<Record<UserFacetId, string[]>>;

interface UserFacet {
    id: UserFacetId;
    label: string;
    values: { value: string; label: string }[]; // 늘 이 차례로 보인다 (건수가 0이어도)
    valueOf: (user: ManagedUser) => string;
}

// 가입: 초대만 되고 첫 로그인(비밀번호 정하기) 전이면 초대됨
const isInvited = (user: ManagedUser) => user.status === 'FORCE_CHANGE_PASSWORD';

export const USER_FACETS: UserFacet[] = [
    {
        id: 'role',
        label: '권한',
        values: (['member', 'decider', 'admin'] as const).map((role) => ({ value: role, label: ROLE_LABELS[role] })),
        valueOf: (user) => user.role,
    },
    {
        id: 'account',
        label: '계정',
        values: [
            { value: 'enabled', label: '사용 중' },
            { value: 'disabled', label: '정지' },
        ],
        valueOf: (user) => (user.enabled ? 'enabled' : 'disabled'),
    },
    {
        id: 'signup',
        label: '가입',
        values: [
            { value: 'invited', label: '초대됨 (첫 로그인 전)' },
            { value: 'confirmed', label: '가입 완료' },
        ],
        valueOf: (user) => (isInvited(user) ? 'invited' : 'confirmed'),
    },
];

// 검색어: 띄어 쓰면 모두 들어 있는 사용자 (이메일·이름·사용자 이름, 대소문자 구분 없음)
export const matchesSearch = (user: ManagedUser, query: string) => {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return true;
    const text = [user.email, user.name, user.username].filter(Boolean).join('\n').toLowerCase();
    return words.every((word) => text.includes(word));
};

// 거르기 (except: 건수를 셀 때 그 거르기만 뺀다)
export const matchesSelection = (user: ManagedUser, selection: UserSelection, except?: UserFacetId) =>
    USER_FACETS.every((facet) => {
        const chosen = selection[facet.id] ?? [];
        return facet.id === except || !chosen.length || chosen.includes(facet.valueOf(user));
    });

export const selectedCount = (selection: UserSelection) =>
    USER_FACETS.reduce((sum, facet) => sum + (selection[facet.id]?.length ?? 0), 0);

const toggle = (selection: UserSelection, id: UserFacetId, value: string): UserSelection => {
    const chosen = selection[id] ?? [];
    return { ...selection, [id]: chosen.includes(value) ? chosen.filter((v) => v !== value) : [...chosen, value] };
};

function FacetGroup({
    facet,
    users,
    selection,
    onChange,
}: {
    facet: UserFacet;
    users: ManagedUser[]; // 검색어를 적용한 사용자
    selection: UserSelection;
    onChange: (next: UserSelection) => void;
}) {
    const [open, setOpen] = useState(true);
    const chosen = selection[facet.id] ?? [];
    const base = users.filter((user) => matchesSelection(user, selection, facet.id));
    const listId = `users-facet-${facet.id}`;
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
                    {chosen.length ? <span className="audit-facet-chosen">{chosen.length}</span> : null}
                </button>
                {chosen.length ? (
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
                    {facet.values.map((item) => {
                        const count = base.filter((user) => facet.valueOf(user) === item.value).length;
                        const selected = chosen.includes(item.value);
                        return (
                            <li key={item.value} className={`audit-facet-value${count === 0 ? ' is-empty' : ''}`}>
                                <label title={item.label}>
                                    <input
                                        type="checkbox"
                                        checked={selected}
                                        onChange={() => onChange(toggle(selection, facet.id, item.value))}
                                    />
                                    <span className="audit-facet-name">{item.label}</span>
                                    <span className="audit-facet-count">{count.toLocaleString()}</span>
                                </label>
                            </li>
                        );
                    })}
                </ul>
            ) : null}
        </div>
    );
}

export function UsersFilter({
    users,
    selection,
    onChange,
    onReset,
    canReset,
    resetNo,
}: {
    users: ManagedUser[]; // 건수를 셀 사용자 (검색어를 적용한 것)
    selection: UserSelection;
    onChange: (next: UserSelection) => void;
    onReset: () => void;
    canReset: boolean; // 검색어나 거르기가 있는가
    resetNo: number; // 필터 초기화: 바뀌면 거르기 목록을 새로 그려 접기를 처음으로
}) {
    const [open, setOpen] = useState(false);
    const box = useRef<HTMLDivElement>(null);
    const button = useRef<HTMLButtonElement>(null);

    // 바깥을 누르거나 Esc를 누르면 닫는다
    useEffect(() => {
        if (!open) return;
        const onPointerDown = (event: PointerEvent) => {
            if (!box.current?.contains(event.target as Node)) setOpen(false);
        };
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key !== 'Escape') return;
            event.preventDefault();
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

    const count = selectedCount(selection);
    const chips = USER_FACETS.flatMap((facet) =>
        (selection[facet.id] ?? []).map((value) => ({
            key: `${facet.id}:${value}`,
            name: facet.label,
            label: facet.values.find((item) => item.value === value)?.label ?? value,
            onRemove: () => onChange({ ...selection, [facet.id]: (selection[facet.id] ?? []).filter((v) => v !== value) }),
        })),
    );

    return (
        <div className="audit-filter-menu" ref={box}>
            <button
                ref={button}
                type="button"
                className={`audit-filter-button${open ? ' is-open' : ''}${count ? ' is-active' : ''}`}
                aria-expanded={open}
                aria-controls="users-filter-panel"
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

            {chips.length ? (
                <ul className="audit-filter-chips" aria-label="걸린 조건">
                    {chips.map((chip) => (
                        <li key={chip.key}>
                            <span className="audit-filter-chip-facet">{chip.name}:</span>
                            <span className="audit-filter-chip-value" title={chip.label}>
                                {chip.label}
                            </span>
                            <button type="button" onClick={chip.onRemove} aria-label={`${chip.name} ${chip.label} 조건 빼기`}>
                                <svg viewBox="0 0 14 14" width="9" height="9" aria-hidden="true" focusable="false">
                                    <path d="M2 2L12 12M12 2L2 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                                </svg>
                            </button>
                        </li>
                    ))}
                </ul>
            ) : null}

            {open ? (
                <div id="users-filter-panel" className="audit-filter-panel users-filter-panel" role="dialog" aria-label="필터 세부 선택">
                    <button
                        className="vdt-model-close"
                        type="button"
                        onClick={() => {
                            setOpen(false);
                            button.current?.focus();
                        }}
                        aria-label="필터 닫기"
                    >
                        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                            <path d="M1 1L13 13M13 1L1 13" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                        </svg>
                    </button>
                    <div className="audit-filter-top users-filter-top">
                        <ResetButton onClick={onReset} disabled={!canReset} title="검색어와 거르기를 모두 지웁니다" />
                    </div>
                    <nav key={resetNo} className="audit-facets" aria-label="거르기">
                        <div className="audit-facets-columns">
                            {USER_FACETS.map((facet) => (
                                <FacetGroup key={facet.id} facet={facet} users={users} selection={selection} onChange={onChange} />
                            ))}
                        </div>
                    </nav>
                </div>
            ) : null}
        </div>
    );
}
