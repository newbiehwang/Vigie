// 사용자 초대 팝업창. 사용자 관리 머리의 + 버튼으로 연다. 모양과 여닫는 효과는 이 앱의 다른 팝업창(기록 팝업창,
// 대화 목록)과 같은 create-plan-model이다: 화면을 어둡게 덮고 가운데에 뜬다, 오른쪽 위 ✕, 닫을 때 접히는 효과.
//   사용자 초대                                            ✕
//   입력한 이메일로 임시 비밀번호가 포함된 초대 메일을 보냅니다. 임시 비밀번호는 7일간 유효합니다.
//   이메일  [name@example.com                ]
//   (오류: 이미 있는 사용자입니다)
//                                     [취소] [다음]
//   ↓ 다음: 보내기 전에 한 번 확인한다 (메일은 보내면 되돌릴 수 없다)
//   이 이메일로 초대를 보낼까요?
//   ┌ new.person@example.com ┐
//                               [이메일 고치기] [초대 보내기]
// - 열리면 이메일 칸에 포커스. Enter로 확인 단계로, 확인 단계에서는 '초대 보내기'에 포커스
// - Esc: 확인 단계면 이메일 고치기로 돌아가고, 입력 단계면 닫는다. ✕·바깥 누르기·취소로 닫는다 (보내는 중에는 닫지 않는다)
// - 보내지 못하면 창을 닫지 않고 입력 단계로 돌아가 서버의 오류 문구를 보인다 (고쳐서 다시 보내게)
// - 사용자 관리 패널(plan-panel)은 등장 효과로 transform이 남아 그 안의 position: fixed가 패널 기준이 되므로 document.body에 그린다
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { createPortal } from 'react-dom';
import { inviteUser, userErrorText } from '@/api/users';
import type { ManagedUser } from '@/types/users';

const CLOSE_MS = 180; // 닫히는 효과 시간 (다른 팝업창과 같다)

export function InviteModal({ onInvited, onClose }: { onInvited: (user: ManagedUser) => void; onClose: () => void }) {
    const [email, setEmail] = useState('');
    const [sending, setSending] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [isClosing, setIsClosing] = useState(false);
    const [confirming, setConfirming] = useState(false); // 보내기 전 확인 단계
    const input = useRef<HTMLInputElement>(null);
    const sendButton = useRef<HTMLButtonElement>(null);

    // after: 닫히는 효과가 끝난 뒤 할 일 (초대한 사용자를 목록에 넣기)
    const close = (after?: () => void) => {
        if (isClosing || sending) return;
        setIsClosing(true);
        window.setTimeout(() => {
            after?.();
            onClose();
        }, CLOSE_MS);
    };

    // 단계가 바뀌면 포커스를 옮긴다: 입력 단계는 이메일 칸, 확인 단계는 '초대 보내기'
    useEffect(() => {
        if (confirming) sendButton.current?.focus();
        else input.current?.focus();
    }, [confirming]);

    const edit = () => {
        if (sending) return;
        setConfirming(false);
    };

    useEffect(() => {
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key !== 'Escape' || event.defaultPrevented) return;
            event.preventDefault();
            if (confirming) edit();
            else close();
        };
        document.addEventListener('keydown', onKeyDown);
        return () => document.removeEventListener('keydown', onKeyDown);
    });

    // 입력 단계의 '다음' (Enter): 확인 단계로
    const next = (event: FormEvent) => {
        event.preventDefault();
        if (!email.trim()) return;
        setError(null);
        setConfirming(true);
    };

    // 확인 단계의 '초대 보내기'
    const send = async () => {
        const address = email.trim();
        if (!address || sending) return;
        setSending(true);
        setError(null);
        try {
            const created = await inviteUser(address);
            setSending(false);
            setIsClosing(true);
            window.setTimeout(() => {
                onInvited(created);
                onClose();
            }, CLOSE_MS);
        } catch (err) {
            setSending(false);
            setError(userErrorText(err));
            setConfirming(false); // 입력 단계로 돌아가 고치게 (포커스는 위 효과가 이메일 칸으로)
        }
    };

    return createPortal(
        <div className={`create-plan-model-overlay${isClosing ? ' is-closing' : ''}`} role="presentation" onClick={() => close()}>
            <div
                className="create-plan-model users-invite-model"
                role="dialog"
                aria-modal="true"
                aria-labelledby="users-invite-title"
                onClick={(event) => event.stopPropagation()}
            >
                <button className="vdt-model-close" type="button" onClick={() => close()} aria-label="닫기 (Esc)" disabled={sending}>
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                        <path d="M1 1L13 13M13 1L1 13" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                    </svg>
                </button>
                <div className="users-invite-head">
                    <h3 id="users-invite-title" className="create-plan-step-heading">
                        사용자 초대
                    </h3>
                    <p>입력한 이메일로 임시 비밀번호가 포함된 초대 메일을 보냅니다. 임시 비밀번호는 7일간 유효합니다.</p>
                </div>
                {confirming ? (
                    <div className="users-invite-form users-invite-confirm">
                        <p className="users-invite-question">이 이메일로 초대를 보낼까요?</p>
                        <p className="users-invite-address">{email.trim()}</p>
                        <div className="users-invite-actions">
                            <button type="button" className="confirm-delete-cancel-btn" onClick={edit} disabled={sending}>
                                이메일 고치기
                            </button>
                            <button ref={sendButton} type="button" className="plan-create-button" onClick={send} disabled={sending}>
                                {sending ? '보내는 중…' : '초대 보내기'}
                            </button>
                        </div>
                    </div>
                ) : (
                    <form className="users-invite-form" onSubmit={next}>
                        <label className="users-invite-field">
                            <span>이메일</span>
                            <input
                                ref={input}
                                type="email"
                                className="users-input"
                                placeholder="name@example.com"
                                value={email}
                                onChange={(event) => setEmail(event.target.value)}
                                required
                                aria-invalid={error ? true : undefined}
                                aria-describedby={error ? 'users-invite-error' : undefined}
                            />
                        </label>
                        {error ? (
                            <p id="users-invite-error" className="users-invite-error" role="alert">
                                {error}
                            </p>
                        ) : null}
                        <div className="users-invite-actions">
                            <button type="button" className="confirm-delete-cancel-btn" onClick={() => close()}>
                                취소
                            </button>
                            <button type="submit" className="plan-create-button" disabled={!email.trim()}>
                                다음
                            </button>
                        </div>
                    </form>
                )}
            </div>
        </div>,
        document.body,
    );
}
