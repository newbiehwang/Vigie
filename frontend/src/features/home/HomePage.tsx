// 홈: 흰 카드(AXPI 패널) 안에 운영 현황 대시보드를 보인다 (Dashboard).
// 맨 위의 한 줄 입력칸과 대시보드의 카드·줄은 모두 새 대화로 질문을 보내고 대화 화면으로 간다.
// 예시 질문은 입력칸을 누르면 입력칸 아래에 펼쳐진다 (Composer의 suggestions). 지난 대화는 대화 탭의 '대화 목록'에서 본다.
import { useNavigate } from 'react-router-dom';
import { ToastHost } from '@/components/Toast';
import { useChatStore } from '@/stores/chatStore';
import { Dashboard } from './Dashboard';
import './home.css';
import './dashboard.css';

export function HomePage() {
    const navigate = useNavigate();

    // 새 대화로 질문을 보내고 대화 화면으로 간다. 답변은 대화 화면에서 이어서 보인다
    const ask = (question: string) => {
        const store = useChatStore.getState();
        if (!store.waitingForResponse) {
            store.newChat();
            store.sendMessage(question);
        }
        navigate('/chat');
    };

    return (
        <section className="plan-panel home-panel" aria-label="홈">
            {/* 알림 자리 (다른 탭과 같다, components/Toast) */}
            <ToastHost />
            <Dashboard onAsk={ask} />
        </section>
    );
}
