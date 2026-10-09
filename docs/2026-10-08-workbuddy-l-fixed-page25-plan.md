# WorkBuddy 教授汇总表 L 列固定第 25 页

## 目标

`file-agent-a-i-scoped-excel` 的 PDF 来源只在物理第 25 页尝试提取“任现职以来的考核情况”。第 25 页不存在、冻结裁剪图没有该正文栏目，或真实栏目无法辨认时，L 留空并记录相应原因；不再向前搜索。B–I 仍从第 1、3 页读取，同轮写入并发布 Excel。Word 继续按正文证据处理，不套用 PDF 页码。

## 契约

- 新签发任务使用 `workbuddy-scoped-pdf-v4`。服务端只按第 1、3 页尺寸确认 B–I 版式；第 25 页只作候选裁剪，不因该页尺寸不同阻断 B–I。不扫描其他页文本层，也不签发倒序定位图。页数不足 25 时 `review_discovery.status=NOT_FOUND`，只冻结 B–I 两区。
- 第 25 页存在时，服务端冻结 `annual_review.page_number=25` 和当前版式的受控矩形，暂记 `FOUND`。WorkBuddy 必须检查裁剪图确实含完整考核正文。若只有签字、投票、审批意见或栏目在框外，调用现有 `scoped_pdf_review_region_reject`；v4 直接改为 `NOT_FOUND`、作废 L 区域，不签发前一页。
- 有真实栏目时只 OCR 该栏目一次，按既有受限标点归一与 `annual_review_precheck.py` 门禁提交；`EXCELLENT_FOUND` 只写优秀年度，`NO_EXCELLENT` / `UNREADABLE` 留空。没有栏目时以 `annual_review={"status":"NOT_FOUND","years":[]}` 和两张身份图提交，L 使用 `ANNUAL_REVIEW_NOT_FOUND` 留空原因。
- 未完成的 v2/v3 任务可经 `POST /api/integrations/v1/scoped-pdf-sources/{task_id}/fixed25-upgrade` 或 MCP `scoped_pdf_fixed25_upgrade` 在同一来源版本上受控重签为 v4。旧任务标为 `SUPERSEDED`、租约失效，新任务重新 claim。已完成任务不改写。v3 倒序接口仅留给旧客户端兼容，新 Skill 不调用。

## 验证与部署

测试覆盖 22 页直接留空、25 页只冻结第 25 页、错误裁剪拒绝后留空、旧任务重签和 Excel 发布回归。实际 PDF 的第 25 页裁剪框须在 WorkBuddy 中人工核对；若不同版式的栏目不在当前裁剪框内，L 留空并记录原因，不改查其他页。沿用现有任务 JSON 与审计表，无数据库迁移。须同步部署 API/MCP、重启服务并重新装载新版插件，单独修改 Skill 不能改变服务端冻结范围。

## 2026-10-08 补充：B–I 允许留空

用户要求 B–I 不必八列全部有值。v4 PDF 提交只传已识别且有引文的字段，缺失列不生成 `field_refs`；身份区域 OCR 可提交空文本，覆盖信息的 `resolved_columns` 记录实际得到的列，L 状态仍必须提交。写回记录仍逐列覆盖目标表：有值的 B–I 必须与冻结引文完全一致，缺失列仅能标 `SOURCE_FIELD_NOT_FOUND` 或 `OCR_UNREADABLE`，且不得携带值、证据或判定。Word 来源同样逐列处理。MCP 的 `rows` 只传有值的 B–I 物理列键与可选 L，工具补齐空位；回执逐列列出值或留空原因。旧 A:I 和完整 A:AA 流程保持原有要求。此调整仍使用既有 JSON、字段及数据库表，不需要迁移。
