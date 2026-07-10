// 本地 fallback 卡片：能力清单 API 失败或返回空时仍可展示功能介绍。

import type { AgentCapability } from '../../types';

export type OnboardingCard = AgentCapability & {
  icon: string;
};

export const FALLBACK_ONBOARDING_CARDS: OnboardingCard[] = [
  {
    id: 'file_upload_read',
    icon: '📄',
    name: '上传和读取文件',
    description: '上传 PDF、Word、Excel、TXT、CSV 等文件后，让智能体读取正文或表格内容。',
    examples: ['帮我读取这个文件', '读取上面上传的文件并总结内容'],
  },
  {
    id: 'document_classification',
    icon: '🏷️',
    name: '文件分类',
    description: '按学校文件分类体系，为上传文件生成多标签分类建议。',
    examples: ['对刚刚上传的文件进行分类', '总结一下之前所有上传文件的分类'],
  },
  {
    id: 'spreadsheet_analysis',
    icon: '📊',
    name: '表格汇总和统计',
    description: '对 Excel、CSV、TSV 表格执行求和、统计、筛选和分组。',
    examples: ['按论文类型统计资助金额', '汇总刚刚上传 CSV 文件中的金额'],
  },
  {
    id: 'spreadsheet_workbench',
    icon: '🧮',
    name: '表格工作台',
    description: '查看工作表、字段结构、公式错误和潜在风险。',
    examples: ['查看这个 Excel 有哪些工作表和字段', '检查这份表格有没有公式错误'],
  },
  {
    id: 'ocr',
    icon: '🔍',
    name: '图片和扫描件 OCR',
    description: '识别图片或扫描 PDF 中的文字。',
    examples: ['帮我 OCR 这批扫描件', '识别这张图片里的文字'],
  },
  {
    id: 'managed_file_query',
    icon: '🗂️',
    name: '服务器受管目录查询',
    description: '查询已授权服务器目录中的文件元数据，不暴露服务器真实路径。',
    examples: ['列出学工收件箱下的所有文件', '查找学工收件箱里包含会议的文件'],
  },
  {
    id: 'classification_taxonomy',
    icon: '📚',
    name: '分类目录查询',
    description: '查看系统当前支持的文件分类目录。',
    examples: ['列出系统当前支持的文件分类目录', '现在文件有哪几种分类'],
  },
  {
    id: 'operation_plan',
    icon: '🛡️',
    name: '安全操作计划',
    description: '涉及改名、移动、删除等高风险操作时，先生成计划，确认后再执行。',
    examples: ['给这些文件生成改名建议，先不要改', '计划删除这些重复文件'],
  },
];
