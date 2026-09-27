// 질문 기록 팝업창의 맨 위: 질문과 그 답변 하나를 대화창(ChatMessage)과 같은 모양으로 보인다.
//                                   ┌──────────────────────────┐
//                                   │ 이번 달 비용이 가장 큰 서비스는? │   ← 질문: 오른쪽 하늘색 상자
//                                   └──────────────────────────┘
//   (로고)  이번 달 비용이 가장 큰 서비스는 다음과 같습니다.             ← 답변: 상자 없이 본문 (마크다운)
//           | 서비스 | 비용 | …
// - 목록의 질문 행에는 답변 앞부분(answerPreview)만 온다. 전체는 팝업창이 열 때 따로 받는다 (GET /audit?answer=…)
// - 답변이 없는 경우: 실패한 질문(오류는 아래 항목 표에), 답변을 남기기 전에 쌓인 질문 (services/llm/audit.py '답변')
// - 답변 속 결과물(![제목](artifact://…))은 감사 로그에 그림이 없어 제목만 보인다
// - 모양은 대화창의 클래스(chat.css의 message·user-message·bot-message·markdown-content)를 그대로 쓴다.
//   사고 과정·실행 시간·승인 카드는 아래 항목 표와 다른 기록에 있으므로 여기서는 보이지 않는다
import { useEffect, useMemo, useState } from 'react';
import { fetchAnswer } from '@/api/audit';
import vigieMark from '@/assets/brand/vigie-mark.svg';
import type { AuditAnswer, AuditRecord } from '@/types/audit';
import { splitArtifacts } from '@/utils/artifacts';
import { escapeHtml, parseMarkdown } from '@/utils/markdown';
import '../chat/chat.css';

// 마크다운 본문 (대화창과 같은 순서: 글 조각은 마크다운으로, 결과물 조각은 제목만)
function MarkdownBody({ text }: { text: string }) {
    const segments = useMemo(
        () =>
            splitArtifacts(text).map((segment) =>
                segment.kind === 'text' ? { ...segment, html: parseMarkdown(escapeHtml(segment.text)) } : segment,
            ),
        [text],
    );
    return (
        <div className="message-content markdown-content">
            {segments.map((segment, index) =>
                segment.kind === 'text' ? (
                    <div key={index} dangerouslySetInnerHTML={{ __html: segment.html }} />
                ) : (
                    <p key={index} className="artifact-missing" role="note">
                        {segment.title ? `'${segment.title}' ` : ''}그림 (감사 로그에는 그림을 남기지 않습니다)
                    </p>
                ),
            )}
        </div>
    );
}

const errorText = (error: unknown) => {
    const response = (error as { response?: { status?: number; data?: { error?: string } } })?.response;
    return response?.data?.error ?? '답변을 불러오지 못했습니다.';
};

// 답변 칸: 불러오는 중 · 전체 · 불러오지 못함(앞부분만) · 답변 없음
function AnswerBody({ record }: { record: AuditRecord }) {
    const hasAnswer = record.answerChars !== undefined;
    const [answer, setAnswer] = useState<AuditAnswer | null>(null);
    const [error, setError] = useState<string | null>(null);

    // 기록이 바뀌면 부르는 쪽(AuditDetailModal)이 key로 새로 그리므로, 여기서는 처음 한 번만 받는다
    const { at, userId } = record;
    useEffect(() => {
        if (!hasAnswer) return;
        let cancelled = false;
        fetchAnswer({ at, userId })
            .then((result) => !cancelled && setAnswer(result))
            .catch((err) => !cancelled && setError(errorText(err)));
        return () => {
            cancelled = true;
        };
    }, [hasAnswer, at, userId]);

    if (!hasAnswer)
        return (
            <p className="audit-answer-note">
                {record.status === 'error'
                    ? '답변하지 못한 질문입니다. 오류는 아래 항목에 있습니다.'
                    : '답변을 기록하기 전에 쌓인 질문이라 답변이 없습니다.'}
            </p>
        );
    if (error)
        return (
            <>
                <p className="audit-answer-note is-error" role="alert">
                    {error} 앞부분만 보입니다.
                </p>
                <MarkdownBody text={record.answerPreview ?? ''} />
            </>
        );
    if (!answer)
        return (
            <div className="audit-trace-loading audit-answer-note" role="status">
                <div className="plan-inline-spinner" />
                <span>답변을 불러오는 중…</span>
            </div>
        );
    const total = answer.answerChars ?? answer.answer.length;
    return (
        <>
            <MarkdownBody text={answer.answer} />
            {total > answer.answer.length ? (
                <p className="audit-answer-note">
                    답변이 길어 앞 {answer.answer.length.toLocaleString()}자만 기록했습니다 (전체 {total.toLocaleString()}자)
                </p>
            ) : null}
        </>
    );
}

export function AuditConversation({ record }: { record: AuditRecord }) {
    return (
        <section className="audit-conversation" aria-label="질문과 답변">
            <div className="message user-message">
                <div className="message-body">
                    <MarkdownBody text={record.question ?? ''} />
                </div>
            </div>
            <div className="message bot-message">
                <div className="message-avatar" aria-hidden="true">
                    <img src={vigieMark} alt="" />
                </div>
                <div className="message-body">
                    <AnswerBody record={record} />
                </div>
            </div>
        </section>
    );
}
