// 分类树只根据后端已返回的主分类路径分组；前端不重新判断分类，也不改变分类事实。
import type { DocumentCategory, DocumentResult } from '../../types';

export type ClassificationTreeFile = {
  result: DocumentResult;
  originalIndex: number;
};

export type ClassificationTreeNode = {
  key: string;
  name: string;
  fileCount: number;
  children: ClassificationTreeNode[];
  files: ClassificationTreeFile[];
};

type MutableClassificationTreeNode = ClassificationTreeNode & {
  childMap: Map<string, MutableClassificationTreeNode>;
};

/** 将主分类路径规范为稳定层级；缺少可靠主分类时统一进入待确认。 */
export function primaryCategoryPath(result: DocumentResult): string[] {
  if (result.extraction_status === 'FAILED') return ['处理失败'];
  const primary = (result.categories ?? [])[0];
  if (!primary) return ['待确认'];
  const explicitPath = cleanPath(primary.category_path ?? []);
  if (explicitPath.length > 0) return explicitPath;
  const namePath = cleanPath(splitCategoryName(primary));
  return namePath.length > 0 ? namePath : ['待确认'];
}

/** 构造只用于展示的主分类树，每个文件严格只出现一次。 */
export function buildClassificationTree(results: DocumentResult[]): ClassificationTreeNode[] {
  const roots = new Map<string, MutableClassificationTreeNode>();
  results.forEach((result, originalIndex) => {
    const path = primaryCategoryPath(result);
    let siblings = roots;
    const ancestors: MutableClassificationTreeNode[] = [];
    let leaf: MutableClassificationTreeNode | undefined;
    for (const [pathIndex, name] of path.entries()) {
      const key = path.slice(0, pathIndex + 1).join('/');
      const current = siblings.get(name) ?? {
        key,
        name,
        fileCount: 0,
        children: [],
        files: [],
        childMap: new Map<string, MutableClassificationTreeNode>(),
      };
      if (!siblings.has(name)) siblings.set(name, current);
      ancestors.push(current);
      siblings = current.childMap;
      leaf = current;
    }
    ancestors.forEach((node) => {
      node.fileCount += 1;
    });
    leaf?.files.push({ result, originalIndex });
  });
  return Array.from(roots.values()).map(toPublicNode);
}

function cleanPath(values: string[]): string[] {
  return values.map((value) => String(value).trim()).filter(Boolean);
}

function splitCategoryName(category: DocumentCategory): string[] {
  return String(category.name || '').split(/[\\/]/);
}

function toPublicNode(node: MutableClassificationTreeNode): ClassificationTreeNode {
  return {
    key: node.key,
    name: node.name,
    fileCount: node.fileCount,
    children: Array.from(node.childMap.values()).map(toPublicNode),
    files: node.files,
  };
}
