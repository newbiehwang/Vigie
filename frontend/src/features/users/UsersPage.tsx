// 사용자 관리 화면 (관리자만): 권한(일반 사용자 · 결정자 · 관리자)을 정하고, 계정을 정지하거나 초대한다
// (services/llm/user_admin.py).
//   머리: 제목 · 초대(+, 팝업창 InviteModal) · 새로 고침(아이콘)
//   검색창(SearchBox, 감사 로그와 같다): 이메일·이름으로 찾기 ('/'로 이동, 띄어 쓰면 모두 들어 있는 사용자)
//   필터(UsersFilter, 감사 로그의 필터와 같은 모양): 권한 · 계정(사용 중·정지) · 가입(초대됨·가입 완료), 걸린 조건은 칩 · 건수
//   목록: 이메일(나 · 이름 · 초대됨) · 권한 · 가입일 · 계정(정지/정지 해제). 정지된 계정은 흐리게
//
// - 사용자를 모두 받아(서버는 50명씩, MAX_PAGES쪽까지) 검색·거르기·건수는 브라우저에서 계산한다 (감사 로그와 같다).
//   서버의 이메일 앞부분 검색(q)은 쓰지 않는다: 이름·중간 글자로도 찾고, 거르기 건수를 세려면 전체가 필요하다
//
// - 관리자를 아래 권한으로 내리기와 정지는 한 번 더 눌러야 한다 (되돌릴 수 있지만 그 사람의 일이 바로 막힌다)
// - 자기 권한 바꾸기·자기 정지는 버튼을 끄고, 서버도 막는다. 마지막 관리자도 서버가 막는다
// - 바꾼 내용은 감사 로그의 '사용자 관리'에 남는다
// - 권한을 바꿔도 그 사람의 화면·권한은 다시 로그인하거나 토큰이 갱신된 뒤(최대 1시간) 바뀐다
import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { listUsers, setEnabled, setRole, userErrorText } from '@/api/users';
import { ROLE_LABELS } from '@/auth/authClient';
import { LoadingCard, useMinimumVisible } from '@/components/LoadingCard';
import { RefreshButton } from '@/components/RefreshButton';
import { SearchBox } from '@/components/SearchBox';
import { SortButton, sortedBy, useSort } from '@/components/SortHeader';
import { ToastHost, useToast } from '@/components/Toast';
import { ResetButton } from '@/features/audit/FilterMenu';
import type { ManagedUser, UserRole } from '@/types/users';
import { formatKoreanDateTime } from '@/utils/formatters';
import { InviteModal } from './InviteModal';
import { UsersFilter, matchesSearch, matchesSelection, selectedCount, type UserSelection } from './UsersFilter';
import '../audit/audit.css'; // 필터·칩·건수는 감사 로그의 모양을 쓴다
import './users.css';

const MAX_PAGES = 20; // 50명씩 20쪽 = 1,000명까지 받는다 (더 있으면 알린다)

const ROLES: UserRole[] = ['member', 'decider', 'admin'];

// 목록 정렬: 이메일 · 권한(관리자 → 결정자 → 일반) · 가입일(기본, 최근 것부터) · 계정(사용 중 → 정지)
type UserSortKey = 'email' | 'role' | 'createdAt' | 'account';
const USER_SORT_LABEL: Record<UserSortKey, string> = { email: '이메일', role: '권한', createdAt: '가입일', account: '계정' };
const ROLE_RANK: Record<UserRole, number> = { admin: 0, decider: 1, member: 2 };
const userSortValue = (user: ManagedUser, key: UserSortKey): string | number =>
    key === 'email'
        ? user.email ?? user.username
        : key === 'role'
          ? ROLE_RANK[user.role] ?? 3
          : key === 'createdAt'
            ? user.createdAt ?? ''
            : user.enabled
              ? 0
              : 1;

// 한 번 더 눌러야 실행되는 버튼 (4초 안에). 되돌릴 수 있지만 그 사람의 일이 바로 막히는 동작에 쓴다
function ConfirmButton({
    label,
    confirmLabel,
    className,
    disabled,
    title,
    onConfirm,
}: {
    label: string;
    confirmLabel: string;
    className: string;
    disabled?: boolean;
    title?: string;
    onConfirm: () => void;
}) {
    const [armed, setArmed] = useState(false);
    useEffect(() => {
        if (!armed) return;
        const timer = window.setTimeout(() => setArmed(false), 4000);
        return () => window.clearTimeout(timer);
    }, [armed]);
    return (
        <button
            type="button"
            className={`${className}${armed ? ' is-armed' : ''}`}
            disabled={disabled}
            title={title}
            onClick={() => {
                if (armed) {
                    setArmed(false);
                    onConfirm();
                } else setArmed(true);
            }}
        >
            {armed ? confirmLabel : label}
        </button>
    );
}

// 권한 고르기 (세 칸 중 하나). 관리자를 아래 권한으로 내리는 것은 한 번 더 누른다
function RoleSelect({ user, busy, onChange }: { user: ManagedUser; busy: boolean; onChange: (role: UserRole) => void }) {
    const [armed, setArmed] = useState<UserRole | null>(null);
    useEffect(() => {
        if (!armed) return;
        const timer = window.setTimeout(() => setArmed(null), 4000);
        return () => window.clearTimeout(timer);
    }, [armed]);
    return (
        <div className="users-roles" role="radiogroup" aria-label={`${user.email ?? user.username}의 권한`}>
            {ROLES.map((role) => {
                const selected = user.role === role;
                const demote = user.role === 'admin' && role !== 'admin';
                return (
                    <button
                        key={role}
                        type="button"
                        role="radio"
                        aria-checked={selected}
                        className={`users-role${selected ? ' is-selected' : ''}${armed === role ? ' is-armed' : ''}`}
                        // 자기 권한은 바꿀 수 없다 (관리자를 내리면 스스로 잠긴다. 서버도 막는다)
                        disabled={busy || user.isSelf}
                        title={user.isSelf ? '자기 권한은 바꿀 수 없습니다' : undefined}
                        onClick={() => {
                            if (selected) return;
                            if (demote && armed !== role) {
                                setArmed(role);
                                return;
                            }
                            setArmed(null);
                            onChange(role);
                        }}
                    >
                        {armed === role ? '내리기 확인' : ROLE_LABELS[role]}
                    </button>
                );
            })}
        </div>
    );
}

export function UsersPage() {
    const [query, setQuery] = useState(''); // 검색어 (SearchBox가 입력을 멈춘 뒤 알린다)
    const [selection, setSelection] = useState<UserSelection>({});
    const [resetNo, setResetNo] = useState(0); // 필터 초기화: 거르기 목록을 새로 그린다
    const [users, setUsers] = useState<ManagedUser[]>([]);
    const [truncated, setTruncated] = useState(false); // MAX_PAGES쪽을 넘어 다 받지 못했다
    const [loading, setLoading] = useState(true);
    const listLoading = useMinimumVisible(loading); // 목록 자리의 기다림 카드 (최소 1초)
    const [error, setError] = useState<string | null>(null); // 목록을 받지 못했을 때 (알림을 띄우고, 목록 자리에 짧게 남긴다)
    // 바꾼 결과와 오류는 패널 위쪽 가운데의 알림으로 (components/Toast)
    const { show: showToast } = useToast();
    const [busy, setBusy] = useState<string | null>(null); // 바꾸는 중인 사용자
    const [inviteOpen, setInviteOpen] = useState(false);
    const requestNo = useRef(0); // 마지막 조회의 응답만 쓴다

    // 모든 쪽을 차례로 받는다
    const load = useCallback(async () => {
        const no = ++requestNo.current;
        setLoading(true);
        setError(null);
        try {
            const all: ManagedUser[] = [];
            let cursor: string | null | undefined;
            for (let page = 0; page < MAX_PAGES; page += 1) {
                const result = await listUsers('', cursor);
                if (no !== requestNo.current) return;
                all.push(...result.users);
                cursor = result.cursor;
                if (!cursor) break;
            }
            setUsers(all);
            setTruncated(Boolean(cursor));
        } catch (err) {
            if (no !== requestNo.current) return;
            setError(userErrorText(err));
            showToast('error', userErrorText(err));
            setUsers([]);
        } finally {
            if (no === requestNo.current) setLoading(false);
        }
    }, [showToast]);

    useEffect(() => {
        load();
    }, [load]);

    // 거르는 순서: 검색어 → 거르기. 필터 창의 건수는 검색어까지 적용한 사용자에서 센다 (감사 로그와 같다)
    const searched = useMemo(() => users.filter((user) => matchesSearch(user, query)), [users, query]);
    const filtered = useMemo(() => searched.filter((user) => matchesSelection(user, selection)), [searched, selection]);
    const [sort, toggleSort] = useSort<UserSortKey>({ key: 'createdAt', desc: true }, (key) => key === 'createdAt');
    const listed = useMemo(() => sortedBy(filtered, sort, userSortValue), [filtered, sort]);
    const conditions = Boolean(query.trim()) || selectedCount(selection) > 0;
    const resetFilters = () => {
        setQuery('');
        setSelection({});
        setResetNo((prev) => prev + 1);
    };

    const change = async (user: ManagedUser, action: () => Promise<ManagedUser>, done: string) => {
        setBusy(user.username);
        try {
            const updated = await action();
            setUsers((prev) => prev.map((item) => (item.username === updated.username ? updated : item)));
            showToast('success', `${updated.email ?? updated.username}: ${done}`);
        } catch (err) {
            showToast('error', userErrorText(err));
        } finally {
            setBusy(null);
        }
    };

    // 초대 팝업창에서 초대했으면 목록 맨 위에 넣는다
    const invited = (created: ManagedUser) => {
        setUsers((prev) => [created, ...prev.filter((item) => item.username !== created.username)]);
        showToast('success', `${created.email ?? created.username}에 초대 메일을 보냈습니다.`);
    };

    return (
        <section className="plan-panel users-panel" aria-label="사용자 관리">
            <div className="plan-panel-header">
                <h1 className="plan-panel-eyebrow">사용자 관리</h1>
                <div className="users-header-actions">
                    {/* 초대: 팝업창에서 이메일을 넣는다 */}
                    <button
                        type="button"
                        className="icon-button users-invite-button"
                        onClick={() => setInviteOpen(true)}
                        aria-label="사용자 초대"
                        title="사용자 초대"
                    >
                        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
                            <path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                        </svg>
                    </button>
                    <RefreshButton onClick={() => load()} loading={loading || listLoading} />
                </div>
            </div>

            <SearchBox
                value={query}
                onChange={setQuery}
                placeholder="이메일·이름으로 찾기"
                label="사용자 검색. 띄어 쓰면 모두 들어 있는 사용자"
                title="띄어 쓰면 모두 들어 있는 사용자만 (이메일·이름)"
            />
            <div className="audit-toolbar">
                <UsersFilter
                    users={searched}
                    selection={selection}
                    onChange={setSelection}
                    onReset={resetFilters}
                    canReset={conditions}
                    resetNo={resetNo}
                />
                <p className="audit-count" role="status">
                    {listLoading ? null : (
                        <span className="users-count-in">
                            <strong>{filtered.length.toLocaleString()}명</strong>
                            {conditions ? <span className="audit-muted"> / {users.length.toLocaleString()}명</span> : null}
                            {truncated ? (
                                <span className="audit-truncated">
                                    {(MAX_PAGES * 50).toLocaleString()}명까지만 불러왔습니다
                                </span>
                            ) : null}
                        </span>
                    )}
                </p>
            </div>


            <div className="plan-panel-body">
                <div className="plan-table users-table">
                    {/* 머리글을 누르면 그 열로 정렬한다 (다시 누르면 방향을 뒤집는다) */}
                    <div className="plan-table-header users-row-grid">
                        {(['email', 'role', 'createdAt', 'account'] as UserSortKey[]).map((key) => (
                            <span key={key} className={key === 'account' ? 'users-col-account' : undefined}>
                                <SortButton
                                    column={key}
                                    label={USER_SORT_LABEL[key]}
                                    sort={sort}
                                    onSort={toggleSort}
                                    align={key === 'account' ? 'end' : 'start'}
                                    announce
                                />
                            </span>
                        ))}
                    </div>

                    {/* 불러오는 동안 목록은 비워 두고, 카드는 흰 박스 전체의 가운데에 띄운다 (아래 plan-panel-loading) */}
                    {listLoading ? null : filtered.length === 0 ? (
                        error ? (
                            <div className="plan-panel-empty">
                                <p>사용자를 불러오지 못했습니다. 새로 고침을 눌러 다시 시도해 주세요.</p>
                            </div>
                        ) : (
                            <div className="plan-panel-empty">
                                {users.length ? (
                                    <>
                                        <p>조건에 맞는 사용자가 없습니다.</p>
                                        <ResetButton onClick={resetFilters} disabled={false} title="검색어와 거르기를 모두 지웁니다" />
                                    </>
                                ) : (
                                    <p>사용자가 없습니다.</p>
                                )}
                            </div>
                        )
                    ) : (
                        <ul className="plan-table-body users-table-body" aria-label="사용자 목록">
                            {/* 불러온 뒤 줄이 위에서부터 차례로 떠오른다 (--row: 순서, 14번째부터는 같이. users.css의 users-row-in).
                                불러오는 동안 목록을 비우므로 새로 고칠 때마다 다시 보인다. 새로 초대한 사용자도 떠오르며 들어온다 */}
                            {listed.map((user, index) => {
                                const rowBusy = busy === user.username;
                                return (
                                    <li
                                        key={user.username}
                                        className={`plan-table-row users-row${user.enabled ? '' : ' is-disabled'}`}
                                        style={{ '--row': Math.min(index, 14) } as CSSProperties}
                                    >
                                        <div className="users-row-grid">
                                            <span className="users-col-email" title={user.username}>
                                                {user.email ?? user.username}
                                                {user.isSelf ? <span className="users-self">나</span> : null}
                                                {/* 초대만 되고 아직 첫 로그인(비밀번호 정하기) 전 */}
                                                {user.status === 'FORCE_CHANGE_PASSWORD' ? (
                                                    <span className="users-invited">초대됨</span>
                                                ) : null}
                                                {user.name ? <span className="users-name">{user.name}</span> : null}
                                            </span>
                                            <span>
                                                <RoleSelect
                                                    user={user}
                                                    busy={rowBusy}
                                                    onChange={(role) =>
                                                        change(
                                                            user,
                                                            () => setRole(user.username, role),
                                                            `권한을 ${ROLE_LABELS[role]}로 바꿨습니다.`,
                                                        )
                                                    }
                                                />
                                            </span>
                                            <span className="users-col-date">
                                                {user.createdAt ? formatKoreanDateTime(user.createdAt) : '-'}
                                            </span>
                                            <span className="users-col-account">
                                                {user.enabled ? (
                                                    <ConfirmButton
                                                        label="정지"
                                                        confirmLabel="정지 확인"
                                                        className="users-account-button is-danger"
                                                        disabled={rowBusy || user.isSelf}
                                                        title={user.isSelf ? '자기 계정은 정지할 수 없습니다' : '로그인과 갱신을 막습니다'}
                                                        onConfirm={() =>
                                                            change(user, () => setEnabled(user.username, false), '정지했습니다.')
                                                        }
                                                    />
                                                ) : (
                                                    <button
                                                        type="button"
                                                        className="users-account-button"
                                                        disabled={rowBusy}
                                                        onClick={() =>
                                                            change(user, () => setEnabled(user.username, true), '정지를 풀었습니다.')
                                                        }
                                                    >
                                                        정지 해제
                                                    </button>
                                                )}
                                            </span>
                                        </div>
                                    </li>
                                );
                            })}
                        </ul>
                    )}
                </div>
            </div>

            {listLoading ? (
                <div className="plan-panel-loading">
                    <LoadingCard text="사용자를 불러오는 중…" />
                </div>
            ) : null}

            <ToastHost />

            {inviteOpen ? <InviteModal onInvited={invited} onClose={() => setInviteOpen(false)} /> : null}
        </section>
    );
}
