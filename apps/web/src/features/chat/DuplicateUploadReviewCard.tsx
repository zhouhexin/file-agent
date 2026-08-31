// 重复上传确认卡只展示后端脱敏候选，并把用户明确选择提交给独立确认接口。
import { useEffect, useState } from 'react';
import { AlertTriangle, Eye, FileCheck2 } from 'lucide-react';

import { decideDuplicateReview, getDuplicateReview } from '../../api/client';
import { formatError } from '../../api/errors';
import type { DuplicateCandidate, DuplicateDecisionResponse, DuplicateReview } from '../../types';
import { DuplicateComparisonDialog } from './DuplicateComparisonDialog';

type DuplicateUploadReviewCardProps = {
  token: string;
  review: DuplicateReview;
  onResolved: (result: DuplicateDecisionResponse) => void;
};

function canCompareCandidate(candidate: DuplicateCandidate) {
  return Boolean(
    candidate.existing_document_id
    || (
      String(candidate.summary.managed_root_key ?? '').trim()
      && String(candidate.summary.managed_relative_path ?? '').trim()
    ),
  );
}

export function DuplicateUploadReviewCard({
  token,
  review,
  onResolved,
}: DuplicateUploadReviewCardProps) {
  // 每张卡只处理一个上传版本，一个文件的等待或失败不能阻塞同批其他文件。
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [comparisonCandidate, setComparisonCandidate] = useState<DuplicateCandidate | null>(null);
  const [latestReview, setLatestReview] = useState(review);

  useEffect(() => {
    // 卡片可能收到查重刚完成时的短暂状态；重新读取一次，避免可复用 ID 已就绪但按钮仍不可用。
    let cancelled = false;
    setLatestReview(review);
    getDuplicateReview(token, review.upload_document_version_id)
      .then((result) => {
        if (!cancelled) setLatestReview(result);
      })
      .catch(() => {
        // 刷新失败时保留已取得的确认卡，用户仍可继续上传或取消，不把只读刷新变成阻断错误。
      });
    return () => {
      cancelled = true;
    };
  }, [review.id, review.upload_document_version_id, token]);

  async function decide(
    decision: 'CONTINUE_UPLOAD' | 'USE_EXISTING_FILE' | 'CANCEL_UPLOAD',
    selectedExistingWorkingCopyId?: string,
  ) {
    setSubmitting(true);
    setError('');
    try {
      const result = await decideDuplicateReview(token, latestReview.upload_document_version_id, {
        duplicate_review_id: latestReview.id,
        decision,
        selected_existing_working_copy_id: selectedExistingWorkingCopyId ?? null,
      });
      setComparisonCandidate(null);
      onResolved(result);
    } catch (err) {
      setError(formatError(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="duplicate-review-card" aria-label={`${latestReview.filename} 重复上传确认`}>
      <header>
        <AlertTriangle size={18} />
        <div>
          <strong>检测到相同或相似文件</strong>
          <span>{latestReview.filename}</span>
        </div>
      </header>

      <div className="duplicate-review-candidates">
        {latestReview.candidates.map((candidate) => (
          <article key={candidate.id}>
            <FileCheck2 size={16} />
            <div>
              <strong>{String(candidate.summary.message ?? '检测到相似内容')}</strong>
              {candidate.summary.filename ? <span>{String(candidate.summary.filename)}</span> : null}
              {candidate.summary.relative_path || candidate.summary.managed_relative_path ? (
                <small>{String(candidate.summary.relative_path ?? candidate.summary.managed_relative_path)}</small>
              ) : null}
              {candidate.summary.similarity_bucket ? (
                <small>相似度：{String(candidate.summary.similarity_bucket)}</small>
              ) : null}
            </div>
            <div className="duplicate-review-candidate-actions">
              {canCompareCandidate(candidate) ? (
                <button
                  className="secondary"
                  disabled={submitting}
                  onClick={() => setComparisonCandidate(candidate)}
                  type="button"
                >
                  <Eye size={15} />对比查看
                </button>
              ) : null}
              <button
                disabled={
                  submitting
                  || !candidate.existing_working_copy_id
                  || !latestReview.allowed_decisions.includes('USE_EXISTING_FILE')
                }
                onClick={() => void decide('USE_EXISTING_FILE', candidate.existing_working_copy_id ?? undefined)}
                title={
                  candidate.existing_working_copy_id
                  && latestReview.allowed_decisions.includes('USE_EXISTING_FILE')
                    ? '直接使用共享工作目录中的现有文件'
                    : '现有文件尚未准备为可直接使用的工作副本'
                }
                type="button"
              >
                使用现有文件
              </button>
              {candidate.existing_working_copy_id
              && latestReview.allowed_decisions.includes('USE_EXISTING_FILE') ? null : (
                <small className="duplicate-review-action-hint">
                  现有文件尚未准备完成，暂时不能选择
                </small>
              )}
            </div>
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
      {comparisonCandidate ? (
        <DuplicateComparisonDialog
          candidate={comparisonCandidate}
          decisionError={error}
          onClose={() => setComparisonCandidate(null)}
          onDecision={(decision, workingCopyId) => void decide(decision, workingCopyId)}
          review={latestReview}
          submitting={submitting}
          token={token}
        />
      ) : null}
    </section>
  );
}

export function DuplicateUploadReviewLoader({
  token,
  uploadVersionId,
  onResolved,
}: {
  token: string;
  uploadVersionId: string;
  onResolved?: () => void;
}) {
  // 历史会话刷新后按上传版本恢复确认卡，候选仍由后端重新做权限和脱敏校验。
  const [review, setReview] = useState<DuplicateReview | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    getDuplicateReview(token, uploadVersionId)
      .then((result) => {
        if (!cancelled) {
          setReview(result);
          if (result.status !== 'WAITING_CONFIRMATION') {
            // 兼容刷新前已经完成的旧确认卡：通知父视图移除整行，而不是只留下空头像。
            onResolved?.();
          }
        }
      })
      .catch((err) => {
        if (!cancelled) setError(formatError(err));
      });
    return () => {
      cancelled = true;
    };
  }, [token, uploadVersionId, onResolved]);

  if (error) return <p className="duplicate-review-error">{error}</p>;
  if (!review) return <p className="agent-chat-response">正在读取重复文件确认状态…</p>;
  if (review.status !== 'WAITING_CONFIRMATION') {
    // 决策结果由附件状态体现；内部枚举保留在后端审计中，不进入普通聊天消息流。
    return null;
  }
  return (
      <DuplicateUploadReviewCard
      token={token}
      review={review}
      onResolved={(result) => {
        setReview(result.review);
        onResolved?.();
      }}
    />
  );
}
