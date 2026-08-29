// 分类任务按后端主分类路径展示树形回执，相关分类仍保留在逐文件卡中。
import { ChevronRight, Folder } from 'lucide-react';

import type { DocumentResult } from '../../types';
import { DocumentResultCard } from './DocumentResultCard';
import { buildClassificationTree, type ClassificationTreeNode } from './classificationTree';
import type { ChatAttachment } from './presentation';
import { findAttachmentByDocumentId } from './presentation';

type ClassificationTreeReceiptProps = {
  results: DocumentResult[];
  attachments: ChatAttachment[];
  token?: string;
  agentRunId?: string;
  onOpenAttachment?: (file: ChatAttachment) => void;
  onOpenDocument?: (documentId: string, filename: string) => void;
};

/** 展示分类树标题和全部主分类分支，不根据置信度隐藏任何文件。 */
export function ClassificationTreeReceipt({
  results,
  attachments,
  token,
  agentRunId,
  onOpenAttachment,
  onOpenDocument,
}: ClassificationTreeReceiptProps) {
  const tree = buildClassificationTree(results);
  return (
    <section className="classification-tree-receipt">
      <header className="classification-tree-header">
        <strong>已整理 {results.length} 个文件</strong>
      </header>
      <div className="classification-tree-body">
        {tree.map((node) => (
          <ClassificationBranch
            attachments={attachments}
            key={node.key}
            node={node}
            token={token}
            agentRunId={agentRunId}
            onOpenAttachment={onOpenAttachment}
            onOpenDocument={onOpenDocument}
          />
        ))}
      </div>
    </section>
  );
}

function ClassificationBranch({
  node,
  attachments,
  token,
  agentRunId,
  onOpenAttachment,
  onOpenDocument,
}: {
  node: ClassificationTreeNode;
  attachments: ChatAttachment[];
  token?: string;
  agentRunId?: string;
  onOpenAttachment?: (file: ChatAttachment) => void;
  onOpenDocument?: (documentId: string, filename: string) => void;
}) {
  return (
    <details className="classification-tree-branch" open>
      <summary className="classification-tree-node">
        <ChevronRight className="classification-tree-chevron" size={16} aria-hidden />
        <Folder className="classification-tree-folder" size={20} aria-hidden />
        <span>{node.name}</span>
        <small>{node.fileCount}</small>
      </summary>
      <div className="classification-tree-children">
        {node.children.map((child) => (
          <ClassificationBranch
            attachments={attachments}
            key={child.key}
            node={child}
            token={token}
            agentRunId={agentRunId}
            onOpenAttachment={onOpenAttachment}
            onOpenDocument={onOpenDocument}
          />
        ))}
        {node.files.length > 0 ? (
          <div className="classification-tree-files">
            {node.files.map(({ result, originalIndex }) => (
              <DocumentResultCard
                attachment={findAttachmentByDocumentId(attachments, result.document_id)}
                index={originalIndex + 1}
                key={`${result.document_id}-${originalIndex}`}
                result={result}
                token={token}
                agentRunId={agentRunId}
                showIndex={false}
                showPrimaryCategory={false}
                onOpenDocument={onOpenDocument}
                onOpenFile={onOpenAttachment}
              />
            ))}
          </div>
        ) : null}
      </div>
    </details>
  );
}
