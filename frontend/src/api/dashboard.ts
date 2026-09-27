// 홈 대시보드 API
//   GET /dashboard   서버가 모아 둔 운영 현황 (types/dashboard.ts)
// 지금은 mock(src/mock/api.ts)만 응답한다. 서버 집계(주기적으로 모아 저장)는 비용이 드는 일이라 아직 만들지 않았다
import axios from 'axios';
import type { DashboardData } from '@/types/dashboard';

export async function getDashboard(): Promise<DashboardData> {
    return (await axios.get<DashboardData>('/dashboard')).data;
}
