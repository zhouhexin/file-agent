---
name: file-agent-manual-excel-revision
description: 用户要把已发布的同名 .xlsx 下载后手工补全，再作为本轮附件回传服务器并保留同一张表的旧版本时使用。仅处理一份补全后的 XLSX，不从 PDF/Word 提取或新增人员行。
---

# 手工补全 Excel 回传

先确认任务明确指向已发布的完整 `.xlsx` 文件名，且用户要更新同一张表的版本。首次上传模板并提取 PDF/Word 的任务继续使用对应填表 Skill；本 Skill 只处理已发布文件的人工补全。

在用户开始编辑前调用 `excel_manual_revision_checkout(target_filename)`，向用户交付返回的 Excel 文件及 `base_document_version_id`、`base_sha256`。用户在这份基线上补全已有记录，可编辑既有单元格和格式；不要清空旧值、改表头/合并区域、增删工作表、添加人员行或公式。需要新增人员时走原 PDF/Word 追加流程。
如果目标是成果资助使用登记余额表，按 `file-agent-research-funding-usage-balance` 的回传规则处理：用户可改第一张 `使用登记汇总` 已有行 C–G、删除完整已有行，也可在中间或末尾加入已核实的独立支出。保留行的 A/B 和原有顺序不改；人工新增行 A 留空，B 填姓名，E 填本笔金额或留空待核对，F 通常留空让服务器重算。个人页保持下载时原样，由服务器重算余额、更新或移除个人页，并在发布版将 A 列重排为连续序号。直接改 F 时 G 必须写明人工更正原因。新图片的登记仍走成果资助图片追加流程。
编辑完成后先保存并关闭 Excel，再上传同名文件，避免文件被占用或在发布后再次保存。若用户直接编辑了服务器可见文件，只有本轮上传的文件与该可见文件逐字节一致时才能把修改登记为新版本；不要用另一份文件覆盖服务器上尚未登记的编辑。

用户上传补好的 Excel 时，本轮可信提交单必须恰好包含一份与目标完全同名的 `.xlsx`，任务文字写出完整文件名。调用 `excel_manual_revision_publish(submission_ref, target_filename, base_document_version_id, base_sha256, base_revision)`，基线参数只能来自编辑前 checkout 或同一已发布文件的正式发布回执，不能在上传之后重新 checkout 来绕过版本冲突。MCP 从本轮受信附件读取文件并上传；不要通过普通附件归档把它注册成第二个目标，也不要让模型直接访问本地路径。

只在回执 `status=COMPLETED`、`excel_published=true` 且能下载新版本时报告成功。回执说明目标文件名、新旧版本 ID、补充格数、服务器下载链接以及旧版本保留。若返回版本冲突、文件已变化、结构变更或 `PUBLISHING`/`NEEDS_REPAIR`，报告错误并停止；不要重复上传为新表、绕过校验或重新追加旧来源。
