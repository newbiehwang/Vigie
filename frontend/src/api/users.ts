// 사용자 관리 (GET·POST /users, /users/{username}/..., services/llm/user_admin.py). 관리자만 부를 수 있다
import axios from 'axios';
import type { ManagedUser, UserRole, UsersPage } from '@/types/users';

const userPath = (username: string) => `/users/${encodeURIComponent(username)}`;

export async function listUsers(q = '', cursor?: string | null): Promise<UsersPage> {
    const params: Record<string, string> = {};
    if (q) params.q = q;
    if (cursor) params.cursor = cursor;
    const { data } = await axios.get<UsersPage>('/users', { params });
    return data;
}

// 초대: 임시 비밀번호가 포함된 초대 메일을 보낸다 (임시 비밀번호는 7일간 유효). 일반 사용자로 시작한다
export async function inviteUser(email: string): Promise<ManagedUser> {
    const { data } = await axios.post<ManagedUser>('/users', { email });
    return data;
}

// 권한 바꾸기: 일반 사용자 · 결정자 · 관리자 중 하나 (서버가 Cognito 그룹을 맞춘다)
export async function setRole(username: string, role: UserRole): Promise<ManagedUser> {
    const { data } = await axios.put<ManagedUser>(`${userPath(username)}/role`, { role });
    return data;
}

// 정지하면 그 사용자의 갱신 토큰도 무효가 된다 (이미 받은 토큰은 만료까지 최대 1시간 남는다)
export async function setEnabled(username: string, enabled: boolean): Promise<ManagedUser> {
    const { data } = await axios.post<ManagedUser>(`${userPath(username)}/${enabled ? 'enable' : 'disable'}`);
    return data;
}

export const userErrorText = (error: unknown) => {
    const response = (error as { response?: { status?: number; data?: { error?: string } } })?.response;
    if (response?.data?.error) return response.data.error;
    if (response?.status === 403) return '사용자 관리는 관리자만 할 수 있습니다.';
    return '요청을 처리하지 못했습니다. 잠시 뒤 다시 시도해 주세요.';
};
