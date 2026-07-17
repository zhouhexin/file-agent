# 文件重命名批次范围与完整性实施计划

## 1. 目标

本阶段解决三个问题：

1. 单个受管文件必须按完整相对路径精确定位，不能用文件名模糊包含代替。
2. 目录重命名必须固化完整文件范围；批次还有待复核项时，不得创建部分 OperationPlan。
3. 没有年份或日期、但正文标题可靠时，允许直接使用 `{title}{extension}`。

## 2. 命名优先级

```text
{year}_{document_number}_{title}{extension}
-> {year}_{title}{extension}
-> {title}{extension}
-> NEEDS_REVIEW
```

纯标题模板只能使用正文或结构化文档标题。仅从原文件名回退出的标题不能触发纯标题重命名。
同标题冲突继续使用完整日期和 `_第二版`、`_第三版` 规则。

## 3. 批次门禁

`file_rename_batches` 保存对话确定的范围和统计，`file_rename_batch_items` 保存每个文件的建议、用户决策和执行结果。

文件项状态：

```text
READY
NEEDS_REVIEW
USER_NAMED
EXCLUDED
COMPLETED
FAILED
```

只有全部文件处于 `READY`、`USER_NAMED` 或 `EXCLUDED` 时，才能创建 OperationPlan。OperationPlan 包含全部可执行文件，不保存静默跳过项。

## 4. 对话处理

- `文件原文件名更正为新文件名`：更新当前最新批次中的唯一待复核项。
- `不需要`：把当前批次剩余待复核项标记为 `EXCLUDED`。
- 用户明确提供名称且批次已完整：该消息同时视为执行确认。
- 文件名匹配多个批次项：返回完整相对路径候选，不猜测文件。
- 旧版本没有批次关系的待复核记录继续使用兼容执行路径。

## 5. 大批量展示

- Tool 回执只返回待复核优先的前 10 项。
- `GET /api/file-renames/batches/{batch_id}` 返回统计和前 10 项预览。
- `GET /api/file-renames/batches/{batch_id}/items` 按状态和位置游标分页。
- OperationPlan 查询只返回前 10 个计划项，同时返回完整数量；确认执行仍读取数据库中的完整计划。
- 单批最多 500 个文件，超过时明确要求缩小目录或增加过滤条件，不允许静默截断。

## 6. 实施状态

| 阶段 | 状态 |
|---|---|
| 缺失测试与基线确认 | 已完成 |
| 批次表和 Alembic 迁移 | 已完成 |
| 正文标题单独命名 | 已完成 |
| 单文件完整相对路径定位 | 已完成 |
| 全量就绪门禁 | 已完成 |
| 对话更正、排除与批次执行 | 已完成 |
| 批次摘要和游标分页接口 | 已完成 |
| 前端渐进展示 | 已完成 |
| 完整回归验证 | 已完成：后端 361 passed、1 skipped；前端构建通过 |

## 7. 部署要求

更新后端代码后执行：

```bash
cd /path/to/file-agent
/opt/homebrew/anaconda3/envs/py311/bin/python -m alembic -c apps/api/alembic.ini upgrade head
```

目标迁移版本为 `20260716_0001`。
