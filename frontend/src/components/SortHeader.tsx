// 목록의 정렬 머리글 (홈 리소스 표 · 감사 로그 · 사용자 관리가 같이 쓴다).
//   [리소스 ⇅]  [상태 ▴]  [오류 ⇅]      ⇅: 정렬할 수 있는 열 (옅은 위·아래 꺾쇠, 머리글에 마우스를 올리면 조금 진해진다)
//                                       ▴/▾: 지금 정렬한 열과 방향 (그 방향 꺾쇠만 진하게, 머리글 글자도 진하게)
// - 색은 글자와 같은 회색 계열이다 (파란색을 쓰지 않는다). 기호 글자(▲▼) 대신 SVG 꺾쇠라 크기·굵기가 머리글 글자와 맞는다
// - 누르면: 다른 열이면 그 열의 기본 방향(firstDesc: 오류·비용·시각처럼 큰 것부터 보는 열은 내림차순), 같은 열이면 방향을 뒤집는다
// - 표(<th>) 안에서는 aria-sort를 th에 둔다 (SortTh). 표가 아닌 목록 머리(div)에서는 버튼 이름에 지금 방향을 읽어 준다
import { useCallback, useRef, useState, type ReactNode } from 'react';
import './sort.css';

export interface SortState<K extends string> {
    key: K;
    desc: boolean;
}

// 정렬 상태와 머리글을 눌렀을 때의 동작. firstDesc(key): 그 열을 처음 누를 때 내림차순인가
export function useSort<K extends string>(initial: SortState<K>, firstDesc: (key: K) => boolean = () => false) {
    const [sort, setSort] = useState<SortState<K>>(initial);
    const first = useRef(firstDesc); // 그릴 때마다 새 함수가 와도 toggle은 바뀌지 않게
    first.current = firstDesc;
    const toggle = useCallback(
        (key: K) =>
            setSort((prev) => (prev.key === key ? { key, desc: !prev.desc } : { key, desc: first.current(key) })),
        [],
    );
    return [sort, toggle] as const;
}

// 두 값 비교 (글자는 한국어 순서, 숫자는 크기). 정렬 방향은 부르는 쪽이 곱한다
export function compareValues(a: string | number, b: string | number): number {
    if (typeof a === 'number' && typeof b === 'number') return a - b;
    return String(a).localeCompare(String(b), 'ko', { numeric: true });
}

// 값 뽑기(valueOf)로 정렬한 사본. 값이 같으면 원래 차례를 지킨다 (Array.prototype.sort는 안정 정렬)
export function sortedBy<T, K extends string>(items: T[], sort: SortState<K>, valueOf: (item: T, key: K) => string | number): T[] {
    const sign = sort.desc ? -1 : 1;
    return [...items].sort((a, b) => sign * compareValues(valueOf(a, sort.key), valueOf(b, sort.key)));
}

type Direction = 'none' | 'asc' | 'desc';

// 위·아래 꺾쇠 두 개. 지금 방향의 꺾쇠만 진하게
export function SortIcon({ direction }: { direction: Direction }) {
    return (
        <svg className={`sort-icon is-${direction}`} viewBox="0 0 8 12" width="8" height="12" aria-hidden="true" focusable="false">
            <path className="sort-icon-up" d="M1.25 4.75 4 2l2.75 2.75" />
            <path className="sort-icon-down" d="M1.25 7.25 4 10l2.75-2.75" />
        </svg>
    );
}

interface SortButtonProps<K extends string> {
    column: K;
    label: ReactNode;
    sort: SortState<K>;
    onSort: (key: K) => void;
    align?: 'start' | 'end'; // 숫자 열은 오른쪽 맞춤 (꺾쇠가 글자 왼쪽에 온다)
    className?: string;
    // 표 밖(div 머리)에서 쓰면 버튼 이름에 방향을 읽어 준다. 표 안(SortTh)은 th의 aria-sort가 알려 준다
    announce?: boolean;
}

export function SortButton<K extends string>({ column, label, sort, onSort, align = 'start', className, announce }: SortButtonProps<K>) {
    const active = sort.key === column;
    const direction: Direction = active ? (sort.desc ? 'desc' : 'asc') : 'none';
    const spoken =
        announce && typeof label === 'string'
            ? `${label}${active ? (sort.desc ? ', 내림차순으로 정렬됨' : ', 오름차순으로 정렬됨') : ''}. 눌러서 정렬`
            : undefined;
    return (
        <button
            type="button"
            className={`sort-button${active ? ' is-active' : ''}${align === 'end' ? ' is-end' : ''}${className ? ` ${className}` : ''}`}
            onClick={() => onSort(column)}
            aria-label={spoken}
        >
            <span className="sort-label">{label}</span>
            <SortIcon direction={direction} />
        </button>
    );
}

// 표의 머리 칸 하나 (aria-sort는 지금 정렬한 열에만)
export function SortTh<K extends string>({ className, ...props }: SortButtonProps<K>) {
    const active = props.sort.key === props.column;
    return (
        <th className={className} aria-sort={active ? (props.sort.desc ? 'descending' : 'ascending') : undefined}>
            <SortButton {...props} />
        </th>
    );
}
