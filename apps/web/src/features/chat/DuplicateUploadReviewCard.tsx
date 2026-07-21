// 重复上传确认卡只展示后端脱敏候选，并把用户明确选择提交给独立确认接口。
import { useEffect, useState } from 'react';
import { AlertTriangle, FileCheck2 } from 'lucide-react';

import { decideDuplicateReview, getDuplicateReview } from '../../api/client';
import { formatError } from '../../api/errors';
import type { DuplicateDecisionResponse, DuplicateReview } from '../../types';

type DuplicateUploadReviewCardProps = {
  token: string;
  review: DuplicateReview;
  onResolved: (result: DuplicateDecisionResponse) => void;
};

export function DuplicateUploadReviewCard({
  token,
  review,
  onResolved,
}: DuplicateUploadReviewCardProps) {
  // 每张卡只处理一个上传版本，一个文件的等待或失败不能阻塞同批其他文件。
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  async function decide(
    decision: 'CONTINUE_UPLOAD' | 'USE_EXISTING_FILE' | 'CANCEL_UPLOAD',
    selectedExistingWorkingCopyId?: string,
  ) {
    setSubmitting(true);
    setError('');
    try {
      const result = await decideDuplicateReview(token, review.upload_document_version_id, {
        duplicate_review_id: review.id,
        decision,
        selected_existing_working_copy_id: selectedExistingWorkingCopyId ?? null,
      });
      onResolved(result);
    } catch (err) {
      setError(formatError(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="duplicate-review-card" aria-label={`${review.filename} 重复上传确认`}>
      <header>
        <AlertTriangle size={18} />
        <div>
          <strong>检测到相同或高度相似文件</strong>
          <span>{review.filename}</span>
        </div>
      </header>

      <div className="duplicate-review-candidates">
        {review.candidates.map((candidate) => (
          <article key={candidate.id}>
            <FileCheck2 size={16} />
            <div>
              <strong>{String(candidate.summary.message ?? '检测到相似内容')}</strong>
              {candidate.summary.filename ? <span>{String(candidate.summary.filename)}</span> : null}
              {candidate.summary.relative_path ? <small>{String(candidate.summary.relative_path)}</small> : null}
              {candidate.summary.similarity_bucket ? (
                <small>相似度：{String(candidate.summary.similarity_bucket)}</small>
              ) : null}
            </div>
            {candidate.existing_working_copy_id && review.allowed_decisions.includes('USE_EXISTING_FILE') ? (
              <button
                disabled={submitting}
                onClick={() => void decide('USE_EXISTING_FILE', candidate.existing_working_copy_id ?? undefined)}
                type="button"
              >
                使用已有文件
              </button>
            ) : null}
          </article>
        ))}
      </div>

      <footer>
        <button disabled={submitting} onClick={() => void decide('CONTINUE_UPLOAD')} type="button">
          继续上传并独立保留
        </button>
        <button className="secondary" disabled={submitting} onClick={() => void decide('CANCEL_UPLOAD')} type="button">
          取消本次上传
        </button>
      </footer>
      {error ? <p className="duplicate-review-error">{error}</p> : null}
    </section>
  );
}

export function DuplicateUploadReviewLoader({
  token,
  uploadVersionId,
}: {
  token: string;
  uploadVersionId: string;
}) {
  // 历史会话刷新后按上传版本恢复确认卡，候选仍由后端重新做权限和脱敏校验。
  const [review, setReview] = useState<DuplicateReview | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    getDuplicateReview(token, uploadVersionId)
      .then((result) => {
        if (!cancelled) setReview(result);
      })
      .catch((err) => {
        if (!cancelled) setError(formatError(err));
      });
    return () => {
      cancelled = true;
    };
  }, [token, uploadVersionId]);

  if (error) return <p className="duplicate-review-error">{error}</p>;
  if (!review) return <p className="agent-chat-response">正在读取重复文件确认状态…</p>;
  if (review.status !== 'WAITING_CONFIRMATION') {
    return <p className="agent-chat-response">重复上传确认已处理：{review.decision ?? review.status}</p>;
  }
  return (
    <DuplicateUploadReviewCard
      token={token}
      review={review}
      onResolved={(result) => setReview(result.review)}
    />
  );
}
