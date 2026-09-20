# WorkBuddy 文档内容提取到 Excel：执行记录

## D00：开发基线

- 状态：自动验证通过；真实 WorkBuddy 能力验证待 D01。
- 基线提交：`c8f7668`。
- 迁移 head：`20260920_0001`。
- 工作树：保留既有图片累计表格 Demo 与其他用户变更，未清理或重置。
- 已执行命令：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/api/app/tests/test_structured_datasets.py apps/api/app/tests/test_structured_extraction.py apps/api/app/tests/test_external_extraction_api.py apps/api/app/tests/test_config.py -q --tb=short
PYTHONPATH=apps/mcp /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/mcp/tests/test_dataset_tools.py apps/mcp/tests/test_attachment_transfer.py apps/mcp/tests/test_workbuddy_submission.py apps/mcp/tests/test_client.py apps/mcp/tests/test_server.py -q --tb=short
```

- 结果：API 93 passed（5 条 PyMuPDF SWIG deprecation warnings）；MCP 56 passed。
- 未完成项：D01 真实 WorkBuddy 能力矩阵和样本尚未执行；该项需要在真实宿主环境完成，不能以自动假结果替代。

## D02–D08：仓库实现状态

- 状态：代码闭环已实现，本地确定性测试通过；D01、D09 的真实环境验收仍未完成。
- D02–D03：已增加冻结 `WORKBUDDY_DOCUMENT` profile、服务端派生执行者、任务与版本策略字段、连续迁移 `20260920_0002`，默认开关关闭；DEFAULT 模式继续排除新增默认字段以保持摘要兼容。
- D04–D05：已实现独立 get/claim/renew/source/submit API、租约校验、受控原文件下载、大小和 SHA-256 核验、完整/部分/失败验收、逐区段页面/元素持久化及 COMPLETE 后续流程。结果请求在 Pydantic 深层解析前执行 4 MiB 原始请求体限制；claim、renew 和 submit 均写入不含 token、路径或正文的 ToolInvocation、ChangeSet 和 ChangeItem。精确重复先进入原决策；选择继续后先等待宿主正文，不能直接归档。取消或终态条目拒绝晚到领取、续租、下载和新提交。
- D06：受保护版本只复用同版本 `workbuddy-external-document` 完整运行；正文读取、转换、force reprocess、索引的最新运行选择均禁止回落服务器解析。发布工作副本时复制正文、元素、覆盖 metadata 和不可回退策略。
- D07：source 返回正式 `source_units`；Word 页码保持 null，PDF 使用真实页码。append 使用 `source_unit_id + quote + start/end` 校验字段证据，并在派生元素保存来源运行、来源页、引文范围及原元素 ID。数据集返回文件数和格式计数，空结果不推进 revision。
- D08：MCP 仅在两个开关同时打开时注册 `document_extraction_get/claim/renew/submit`；claim 实际流式下载并校验后才返回 ResourceLink。插件 Skill 已改为图片、Word、PDF 的完整调用顺序和正式 evidence_refs。

## 自动验证证据

```bash
PYTHONPATH=apps/api:apps/mcp /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest \
  apps/api/app/tests/test_structured_datasets.py \
  apps/api/app/tests/test_document_extraction_contracts.py \
  apps/api/app/tests/test_document_extraction_api.py \
  apps/mcp/tests/test_dataset_tools.py \
  apps/mcp/tests/test_document_extraction_tools.py \
  apps/mcp/tests/test_server.py -q
# 28 passed

PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest \
  apps/api/app/tests/test_file_lifecycle.py \
  apps/api/app/tests/test_integration_ingest_api.py \
  apps/api/app/tests/test_document_index_service.py \
  apps/api/app/tests/test_config.py -q --tb=short
# 141 passed, 5 existing PyMuPDF SWIG deprecation warnings

PYTHONPATH=apps/api:apps/mcp /opt/homebrew/anaconda3/envs/py311/bin/python -m compileall -q \
  apps/api/app apps/mcp/file_agent_mcp
git diff --check
# passed
```

提交前完整回归：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/api/app/tests -q --tb=short
# 1375 passed, 19 skipped, 9 warnings

PYTHONPATH=apps/mcp /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/mcp/tests -q --tb=short
# 95 passed
```

当前 Alembic 唯一 head 为 `20260920_0002`；已对当前配置的 PostgreSQL 开发库执行 `upgrade head`。文件域重置清单已同步包含三张 structured dataset 表。

新增自动场景包含：Word 无物理页码、PDF 真实页码、正式证据 UUID/引文范围、跨用户向同一数据集追加、四条记录合计 400、XLSX 下载核对、MCP 工具条件注册、claim 下载哈希校验、PARTIAL 不归档、工作副本正文和元素复制。

## 尚未完成且不能由本地假结果代替

- D01：尚未在真实 WorkBuddy 列出实际图片、DOC/DOCX、PDF 工具及定位能力，也未创建和运行计划规定的 11 条无隐私样本与失败样本。
- D02/D09：当前 PostgreSQL 开发库已完成 upgrade；尚未在独立 PostgreSQL 测试库执行 downgrade，并验证并发 claim、submit 和 dataset append。
- D09：尚未用真实 WorkBuddy 完成 11 行、金额 1400.00 的 XLSX 人工核对，以及 DOC 独立 3 行、500.00 验证；也未完成重启恢复烟测。
- D10：连接器 1.0.3 已携带两个 Demo 开关，本机开发配置也已启用，Settings 和 `.env.example` 仍默认关闭。尚未重新安装插件、重启 API/worker 并完成真实 WorkBuddy 会话验收。

因此当前结论是“仓库内 Demo 实现基本完成，本地自动验证通过；两份方案定义的整体任务尚未严格验收完成”。
