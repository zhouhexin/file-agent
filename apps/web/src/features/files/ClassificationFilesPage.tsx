// 文件分类页只浏览已发布工作副本；分类依据不足的文件会明确归入“其他”。
import { useCallback, useEffect, useState } from 'react';
import { ArrowLeft, ChevronLeft, ChevronRight, Download, FolderTree, RefreshCw } from 'lucide-react';

import {
  fetchWorkingCopyBlob,
  getClassificationOrganizationFiles,
  getClassificationOrganizationTree,
} from '../../api/client';
import { formatError } from '../../api/errors';
import type {
  OrganizationFilePageResponse,
  OrganizationTreeNode,
  OrganizationTreeResponse,
} from '../../types';
import './classification-files.css';

type ClassificationFilesPageProps = {
  token: string;
  onBack: () => void;
};

const PAGE_SIZE = 20;

function formatSize(size: number): string {
  // 文件大小只用于概览，保留一位小数即可。
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function classificationLabel(file: OrganizationFilePageResponse['files'][number]): string {
  if (file.effective_primary?.category_path.length) {
    return file.effective_primary.category_path.join(' / ');
  }
  return file.classification_outcome === 'OTHER' ? '其他' : '未识别到主分类';
}

function classificationStatus(file: OrganizationFilePageResponse['files'][number]) {
  if (file.pending_primary) {
    return <span className="classification-status neutral">分类落位处理中</span>;
  }
  if (file.classification_outcome === 'OTHER') {
    return <span className="classification-status neutral">已归入其他</span>;
  }
  if (file.primary_category_status === 'CONFIRMED') {
    return <span className="classification-status confirmed">已确认</span>;
  }
  if (file.primary_category_status === 'AUTO_APPLIED') {
    return <span className="classification-status automatic">自动归类</span>;
  }
  return <span className="classification-status neutral">处理中</span>;
}

function TreeNode({
  node,
  selectedId,
  onSelect,
}: {
  node: OrganizationTreeNode;
  selectedId: string | null;
  onSelect: (node: OrganizationTreeNode) => void;
}) {
  // 有子节点时使用原生 details，避免一次操作展开整棵深层 taxonomy。
  const [expanded, setExpanded] = useState(node.category_path.length === 1);
  const button = (
    <button
      type="button"
      className={selectedId === node.category_id ? 'file-tree-node selected' : 'file-tree-node'}
      onClick={() => onSelect(node)}
      title={node.category_path.join(' / ')}
    >
      <span>{node.name}</span>
      <strong>{node.subtree_file_count}</strong>
    </button>
  );
  if (node.children.length === 0) return <li>{button}</li>;
  return (
    <li>
      <details open={expanded}>
        <summary
          className={selectedId === node.category_id ? 'file-tree-node selected' : 'file-tree-node'}
          onClick={(event) => {
            // 由 React 持有展开状态，选择节点导致重渲染时也不会把用户折叠操作重置。
            event.preventDefault();
            setExpanded((value) => !value);
            onSelect(node);
          }}
          title={node.category_path.join(' / ')}
        >
          <span>{node.name}</span>
          <strong>{node.subtree_file_count}</strong>
        </summary>
        <ul>
          {node.children.map((child) => (
            <TreeNode
              key={child.category_id}
              node={child}
              selectedId={selectedId}
              onSelect={onSelect}
            />
          ))}
        </ul>
      </details>
    </li>
  );
}

export function ClassificationFilesPage({ token, onBack }: ClassificationFilesPageProps) {
  const [tree, setTree] = useState<OrganizationTreeResponse | null>(null);
  const [pageData, setPageData] = useState<OrganizationFilePageResponse | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedLabel, setSelectedLabel] = useState('全部文件');
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [nextTree, nextFiles] = await Promise.all([
        getClassificationOrganizationTree(token),
        getClassificationOrganizationFiles(token, {
          categoryId: selectedId ?? undefined,
          page,
          pageSize: PAGE_SIZE,
        }),
      ]);
      setTree(nextTree);
      setPageData(nextFiles);
    } catch (err) {
      setError(formatError(err));
    } finally {
      setLoading(false);
    }
  }, [page, selectedId, token]);

  useEffect(() => {
    void load();
  }, [load]);

  function selectNode(node: OrganizationTreeNode) {
    setSelectedId(node.category_id);
    setSelectedLabel(node.category_path.join(' / '));
    setPage(1);
  }

  function selectAll() {
    setSelectedId(null);
    setSelectedLabel('全部文件');
    setPage(1);
  }

  async function downloadFile(workingCopyId: string, filename: string) {
    // 通过浏览器临时 URL 下载，不在页面保存文件内容或暴露存储路径。
    try {
      setError('');
      const blob = await fetchWorkingCopyBlob(token, workingCopyId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
    } catch (err) {
      setError(formatError(err));
    }
  }

  return (
    <main className="classification-files-page">
      <header className="classification-files-header">
        <div>
          <h1><FolderTree size={24} /> 文件分类</h1>
          <p>浏览已发布的主分类结果；无法可靠细分的文件会归入“其他”。</p>
        </div>
        <div className="classification-files-actions">
          <button type="button" onClick={onBack}><ArrowLeft size={16} /> 返回聊天</button>
          <button type="button" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={16} /> 刷新
          </button>
        </div>
      </header>

      {error ? <p className="classification-files-error">{error}</p> : null}
      <section className="classification-files-workspace" aria-busy={loading}>
        <aside className="classification-tree" aria-label="文件分类目录">
          <div className="classification-tree-summary">
            <strong>分类目录</strong>
            <span>{tree?.taxonomy_version ?? '正在加载'}</span>
          </div>
          <button
            type="button"
            className={selectedId === null ? 'file-tree-node selected' : 'file-tree-node'}
            onClick={selectAll}
          >
            <span>全部文件</span>
            <strong>{tree?.total_active_files ?? 0}</strong>
          </button>
          <ul className="classification-tree-list">
            {(tree?.nodes ?? []).map((node) => (
              <TreeNode
                key={node.category_id}
                node={node}
                selectedId={selectedId}
                onSelect={selectNode}
              />
            ))}
          </ul>
        </aside>

        <div className="classification-file-list">
          <div className="classification-file-list-heading">
            <div>
              <h2>{selectedLabel}</h2>
              <p>共 {pageData?.total ?? 0} 个文件，分类节点包含其全部子分类。</p>
            </div>
          </div>
          <div className="classification-table-wrap">
            <table className="classification-table">
              <thead>
                <tr><th>文件</th><th>主分类</th><th>状态</th><th>大小</th><th>操作</th></tr>
              </thead>
              <tbody>
                {loading && !pageData ? (
                  <tr><td colSpan={5}>正在加载...</td></tr>
                ) : pageData?.files.length === 0 ? (
                  <tr><td colSpan={5}>当前范围没有已发布文件。</td></tr>
                ) : pageData?.files.map((file) => (
                  <tr key={file.working_copy_id}>
                    <td>
                      <strong>{file.filename}</strong>
                      <span>{file.relative_path}</span>
                    </td>
                    <td>{classificationLabel(file)}</td>
                    <td>{classificationStatus(file)}</td>
                    <td>{formatSize(file.size_bytes)}</td>
                    <td>
                      <button
                        type="button"
                        className="classification-download"
                        onClick={() => void downloadFile(file.working_copy_id, file.filename)}
                      >
                        <Download size={15} /> 下载
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <footer className="classification-pagination">
            <button type="button" disabled={loading || page <= 1} onClick={() => setPage((value) => value - 1)}>
              <ChevronLeft size={16} /> 上一页
            </button>
            <span>第 {pageData?.page ?? page} / {Math.max(pageData?.total_pages ?? 0, 1)} 页</span>
            <button
              type="button"
              disabled={loading || !pageData || page >= pageData.total_pages}
              onClick={() => setPage((value) => value + 1)}
            >
              下一页 <ChevronRight size={16} />
            </button>
          </footer>
        </div>
      </section>
    </main>
  );
}
