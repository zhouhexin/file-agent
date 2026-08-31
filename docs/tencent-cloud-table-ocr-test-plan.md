# 腾讯云表格 OCR 独立测试方案

## 1. 测试目标

验证 `STRUCTURED_EXTRACTION_LAYOUT_PROVIDER=tencent_cloud_table` 时，系统能够通过腾讯云
`RecognizeTableAccurateOCR` 识别图片和扫描 PDF 中的表格，并把官方返回的 `Text`、行列范围、
置信度和四点坐标转换为现有结构化抽取事实。测试同时确认：原件不被修改、未授权时不外发、
失败错误码可追踪、异步任务可结束、结果可形成字段证据和导出文件。

本方案只验收腾讯云表格 OCR 适配，不替代全项目发布验收和基础文字 OCR 验收。

## 2. 验收范围

- 图片：PNG、JPEG，至少各一份。
- 扫描 PDF：单页和多页，至少各一份。
- 表格类型：有线表格、无线表格、合并单元格，至少各一份。
- 结果字段：单元格文本、`row_start/row_end`、`column_start/column_end`、置信度、页码、坐标。
- 异常边界：未授权、缺少凭证、错误凭证、无表格、超限图片、限流或暂时性错误。
- 审计边界：异步任务、结构化运行、字段证据、ChangeSet、日志和原件哈希。

不在本次范围内：腾讯云专用 Excel 文件直接落库、公式/图表/印章专项识别、本地 Paddle
自动回退。当前腾讯云返回的表格单元格会进入系统已有的确定性字段映射、证据校验和 CSV/XLSX
导出链路。

## 3. 测试前置条件

1. 使用已开通腾讯云通用表格识别的测试账号，并只授予所需 OCR 权限。
2. 测试文件必须已获外发授权，优先使用脱敏或合成数据；严禁使用未授权个人敏感材料。
3. 真实 Secret 只写入部署环境 `.env` 或密钥管理系统，不写入测试代码、日志和 Git。
4. PostgreSQL 已执行项目既有迁移；本次 Provider 适配没有新增数据库迁移。
5. 安装依赖并从仓库根目录启动 API 与 `STRUCTURED_EXTRACTION` worker。

真实接口测试使用以下配置：

```dotenv
STRUCTURED_EXTRACTION_ENABLED=true
STRUCTURED_EXTRACTION_LAYOUT_PROVIDER=tencent_cloud_table
OCR_EXTERNAL_CONTENT_AUTHORIZED=true
TENCENT_CLOUD_OCR_SECRET_ID=<test-secret-id>
TENCENT_CLOUD_OCR_SECRET_KEY=<test-secret-key>
TENCENT_CLOUD_OCR_REGION=ap-guangzhou
TENCENT_CLOUD_OCR_ENDPOINT=ocr.tencentcloudapi.com
TENCENT_CLOUD_OCR_TIMEOUT_SECONDS=30
TENCENT_CLOUD_OCR_MAX_RETRIES=2
TENCENT_CLOUD_OCR_MAX_IMAGE_BYTES=10485760
TENCENT_CLOUD_TABLE_OCR_MAX_QPS=2
PP_STRUCTURE_ENABLED=false
```

如需测试动态字段映射 LLM，另行配置 `STRUCTURED_EXTRACTION_LLM_PROVIDER`；腾讯云表格版面识别
本身不依赖该 LLM。修改环境变量后必须重启 API 和结构化抽取 worker。

## 4. 自动化测试

### 4.1 定向测试

在仓库根目录执行：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest \
  apps/api/app/tests/test_tencent_cloud_table_provider.py \
  apps/api/app/tests/test_structured_extraction.py \
  apps/api/app/tests/test_config.py -q
```

通过标准：没有失败项；测试使用 fake client，不调用腾讯云，不产生费用。必须覆盖：官方 `Text`
字段、对象和字典响应、多页 PDF、坐标与行列映射、无表格、重试、大小限制、未授权和凭证缺失。

### 4.2 后端完整回归

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest
/opt/homebrew/anaconda3/envs/py311/bin/python -m compileall -q apps/api/app
git diff --check
```

通过标准：全部自动化测试通过；只允许项目文档已说明的环境型 skipped 项，不允许新增失败、
collection error 或语法错误。

## 5. 真实接口测试数据矩阵

| 编号 | 输入 | 重点 | 预期结果 |
|---|---|---|---|
| T01 | 清晰 PNG 有线表格 | 基础单元格 | 文本、行列、坐标完整，状态完成 |
| T02 | JPEG 无线表格 | 无边框识别 | 至少形成可复核单元格，不伪造缺失坐标 |
| T03 | 合并单元格表格 | 跨行跨列 | `row_end/column_end` 正确保留 |
| T04 | 两页扫描 PDF | 多页覆盖 | 两页均调用识别，证据页码为 1、2 |
| T05 | 表格外含普通文字 | 范围隔离 | 只把表格单元格作为 `table_cell` 证据 |
| T06 | 不含表格的图片 | 空结果 | 返回无表格/需复核，不发布虚假字段 |
| T07 | 模糊、倾斜或低分辨率表格 | 质量降级 | 低置信字段进入复核，不把猜测标为确定值 |
| T08 | 接近大小上限的图片 | 请求边界 | 在允许范围内成功；超限时返回稳定大小错误 |

每份测试数据应另外保存一份人工真值表，至少记录表格数量、页码、单元格文本、行列范围以及
5 个关键字段。逐项对比真值，不能只凭“接口返回成功”判断通过。

## 6. 手工端到端步骤

1. 记录测试文件原始 SHA-256。
2. 通过聊天入口上传或选择文件，输入明确任务，例如“提取这张表中的姓名、学号和金额并以表格展示”。
3. 确认 Agent Catalog 已暴露 `extract-image-structured-data`；未授权或缺少密钥时该 Tool 不应暴露。
4. 确认请求返回异步处理中状态，而不是阻塞 API；启动中的 worker 应领取
   `STRUCTURED_IMAGE_EXTRACTION` 任务。
5. 等待最终回执，核对记录数、字段数、需复核数、页码/坐标证据和导出文件。
6. 对多页 PDF 核对每一页均有证据，不允许只处理第一页。
7. 再次提交完全相同的文件和字段 Schema，确认复用已完成结果，不重复调用腾讯云。
8. 再次计算原件 SHA-256，必须与步骤 1 相同。

## 7. 数据库与日志核对

使用测试运行 ID 执行只读查询：

```sql
SELECT id, document_id, provider, model_name, status,
       record_count, review_count, quality_band, error_code,
       layout_extraction_run_id, created_at, updated_at
FROM structured_extraction_runs
WHERE id = '<structured_extraction_run_id>';

SELECT record_index, field_key, field_label, raw_text,
       normalized_value_json, confidence, status,
       page_number, bbox_json, evidence_element_ids_json,
       warning_codes_json
FROM structured_extraction_fields
WHERE structured_extraction_run_id = '<structured_extraction_run_id>'
ORDER BY record_index, field_key;

SELECT element_index, label, text_content, page_number,
       bbox_json, content_layer, metadata_json
FROM document_elements
WHERE extraction_run_id = '<layout_extraction_run_id>'
ORDER BY element_index;

SELECT page_number, metadata_json
FROM document_pages
WHERE extraction_run_id = '<layout_extraction_run_id>'
ORDER BY page_number;

SELECT id, extractor, parser_name, parser_version, parser_config_hash, status
FROM document_extraction_runs
WHERE id = '<layout_extraction_run_id>';
```

通过标准：

- `structured_extraction_runs.provider` 为 `tencent_cloud_table`；关联解析运行的 `parser_name` 为
  `tencent_cloud_table`，`parser_version` 为 `RecognizeTableAccurateOCR@2018-11-19`。
- `document_elements` 中表格元素带真实页码、文本、行列元数据和可用坐标；无法取得的值保持空，
  不得填造默认业务事实。
- `document_pages.metadata_json.provider_request_id` 保存腾讯云请求 ID，便于运维关联，但普通用户
  回执和日志不展示该 ID 对应的请求内容。
- 字段值可追溯到 `evidence_element_ids_json`；低置信或缺证据字段进入复核。
- 成功或失败均有终态、ToolInvocation 和 ChangeSet；失败不会让前端永久显示“正在处理”。
- 日志可按 `request_id`、`agent_run_id`、`document_id` 定位处理阶段，但不包含 Secret、图片
  Base64、OCR 全文或绝对原件路径。

## 8. 关闭式失败测试

按顺序执行，每次修改配置后重启 API 与 worker：

1. `OCR_EXTERNAL_CONTENT_AUTHORIZED=false`：Tool 不进入 Adaptive Catalog；直接调用服务时返回
   `OCR_EXTERNAL_CONTENT_NOT_AUTHORIZED`，腾讯客户端调用次数为 0。
2. 清空 SecretId/SecretKey：返回 `OCR_PROVIDER_CONFIG_INVALID`，不创建异步任务。
3. 使用无效测试凭证：运行失败并保留 `OCR_PROVIDER_AUTH_FAILED`，日志不输出凭证。
4. 模拟限流或腾讯临时错误：最多按配置重试，最终错误为
   `OCR_PROVIDER_TEMPORARY_FAILURE`，不会无限重试。
5. 超出图片请求大小：返回 `OCR_IMAGE_TOO_LARGE`；原件不被压缩或覆盖。
6. 关闭结构化能力：`STRUCTURED_EXTRACTION_ENABLED=false` 时 Tool 不对 Planner 开放。

腾讯云表格 Provider 不得在失败后隐式调用 Paddle 或其他在线模型。

## 9. 性能与费用观察

- 以 10、50、100 页三个批次记录端到端耗时、单页 P50/P95、错误率、重试次数和腾讯云请求数。
- 默认 `TENCENT_CLOUD_TABLE_OCR_MAX_QPS=2`，确认并发情况下没有超过部署配额。
- 相同文件、相同内容版本、相同字段 Schema 的第二次请求应命中复用，腾讯云请求数不增加。
- 对 PDF 逐页调用会按页产生请求和费用；压测前必须设置费用预算与告警。
- 任何包含真实敏感信息的性能样本都必须先脱敏并获得外发授权。

## 10. 验收与回滚标准

满足以下条件才可启用：自动化回归全绿；T01-T08 均有记录且无严重错误；多页无漏页；原件哈希
不变；无未授权外发；错误码和异步终态可追踪；人工真值关键字段准确率达到业务约定门槛。

若出现漏页、行列错位、权限越界、错误码丢失、任务永久等待或原件变化，必须停止上线。回滚只需
把 `STRUCTURED_EXTRACTION_LAYOUT_PROVIDER` 改回 `pp_structure_v3`（并按需启用
`PP_STRUCTURE_ENABLED=true`），或将 `STRUCTURED_EXTRACTION_ENABLED=false` 完全关闭该 Tool，然后
重启 API 与结构化抽取 worker；无需回滚数据库结构。

## 11. 官方契约依据

- [腾讯云 RecognizeTableAccurateOCR API](https://cloud.tencent.com/document/api/866/86721)
- [腾讯云 OCR 数据结构（TableCellInfo、TableInfo）](https://cloud.tencent.com/document/product/866/33527)
