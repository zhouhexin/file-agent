import { File, FileImage, FileSpreadsheet, FileText } from 'lucide-react';

type FileTypeIconProps = {
  filename: string;
  contentType?: string;
};

export function FileTypeIcon({ filename, contentType = '' }: FileTypeIconProps) {
  // 只根据文件名和 MIME 类型做轻量展示，不影响后端真实解析能力。
  const lowerName = filename.toLowerCase();
  const size = 18;
  if (contentType.startsWith('image/')) {
    return <FileImage size={size} />;
  }
  if (lowerName.endsWith('.xls') || lowerName.endsWith('.xlsx') || lowerName.endsWith('.csv')) {
    return <FileSpreadsheet size={size} />;
  }
  if (
    lowerName.endsWith('.pdf')
    || lowerName.endsWith('.doc')
    || lowerName.endsWith('.docx')
    || lowerName.endsWith('.txt')
    || lowerName.endsWith('.md')
  ) {
    return <FileText size={size} />;
  }
  return <File size={size} />;
}
