import type { StructuredExtractionResult } from '../../types';


export type StructuredExtractionTableLayout = {
  businessColumnCount: number;
  totalColumnCount: number;
  minimumWidth: number;
};


/**
 * 表格列完全由后端验证后的动态 field_schema 决定；序号是唯一的固定展示列。
 * 每列预留稳定的最小宽度，业务列较多时由外层容器横向滚动，不能静默隐藏列。
 */
export function buildStructuredExtractionTableLayout(
  result: Pick<StructuredExtractionResult, 'field_schema'>,
): StructuredExtractionTableLayout {
  const businessColumnCount = result.field_schema.length;
  const totalColumnCount = businessColumnCount + 1;
  return {
    businessColumnCount,
    totalColumnCount,
    minimumWidth: Math.max(620, 120 + businessColumnCount * 160),
  };
}
