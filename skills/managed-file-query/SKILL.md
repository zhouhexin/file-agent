# managed-file-query

## Trigger
用户要求列出、查看、搜索服务器受管目录中的文件，且条件主要来自结构化元数据。

典型表达：

```text
列出 Downloads 下所有 pdf 文件。
列出文件名包含发票的文件。
查看 file_agent_spreadsheet_patch_files 目录里的 xlsx 文件。
找名称里有合同的 docx 文件。
```

## Inputs
用户消息、会话上下文、部署层允许的受管目录 root_key、可选的目录前缀、扩展名、文件名关键字、分类路径和状态。

## Outputs
受控 Tool 计划，至少包含：

```json
{
  "intent": "LIST_MANAGED_FILES",
  "selected_skills": ["managed-file-query"],
  "steps": [
    {
      "skill": "managed-file-query",
      "tool_name": "managed-file-list",
      "input": {
        "root_key": "downloads",
        "path_prefix": "file_agent_spreadsheet_patch_files",
        "extension": "pdf",
        "filename_contains": "发票",
        "status": "ACTIVE"
      }
    }
  ]
}
```

## Allowed Tools
`managed-file-list`, `managed-file-search`, `feedback-record`

## Open Source Backing
不直接使用开源 Skill。当前执行实现为项目自研 Planner 规则和受控 Tool schema；后续可把解析规则迁移到 `skills/managed-file-query/rules.json`。

## Steps
1. 判断请求是否属于受管目录文件元数据查询。
2. 解析 root_key；root_key 必须来自部署层配置，未配置 root_key 只能按当前受管根内子目录兜底解析。
3. 解析 path_prefix；path_prefix 必须是受管目录内的相对路径，不能包含绝对路径、`.` 或 `..`。
4. 解析 extension；PDF、DOCX、XLSX、CSV、图片等文件类型必须写入 `extension`，不能写入 `path_prefix`。
5. 解析 filename_contains；“文件名包含/名称里有/带有”后的词写入 `filename_contains`。
6. 组合 `managed-file-list` 或后续 `managed-file-search` Tool 输入。
7. Tool dispatch 再执行 schema 校验和受管目录权限边界校验。

## Evidence Rules
受管目录文件列表不是正文证据回答；结果必须展示逻辑 root_key、相对路径、文件名、扩展名、大小、修改时间和状态。不得展示宿主机绝对路径。

## ChangeSet Rules
只读查询不生成 ChangeSet。若后续用户基于查询结果要求移动、复制、删除或重命名，必须转入 OperationPlan。

## OperationPlan Rules
本 Skill 自身只读，不需要确认。任何写操作必须通过 `operation-plan-create`，不得直接执行。

## Failure Handling
未找到匹配文件时说明查询条件，包括 root_key、path_prefix、extension、filename_contains。root_key 无法解析时要求用户补充目录名；不得猜测未授权路径。

## Feedback Samples
当用户指出查询解析错误时，通过 `feedback-record` 记录样本：

```json
{
  "target_type": "SKILL",
  "target_id": "managed-file-query",
  "feedback_type": "BAD_SKILL_PARSE",
  "comment": "pdf 被识别成 path_prefix",
  "context_json": {
    "message": "列出 Downloads 下所有 pdf 文件",
    "actual_input": {"root_key": "downloads", "path_prefix": "pdf"},
    "expected_input": {"root_key": "downloads", "extension": "pdf"}
  }
}
```

反馈样本只能生成候选规则或测试用例，不能自动修改生产 Skill。

## Tests
必须覆盖：

- `列出 Downloads 下所有 pdf 文件` 生成 `extension=pdf`，不生成 `path_prefix=pdf`。
- `列出 Downloads 下文件名包含发票的文件` 生成 `filename_contains=发票`。
- `列出 Downloads 下 file_agent_spreadsheet_patch_files 目录中的 pdf 文件` 同时生成 `path_prefix` 和 `extension`。
- 隐藏文件和隐藏目录不进入展示结果。
- root_key、path_prefix、extension、filename_contains 组合查询不泄露绝对路径。

## Forbidden
不得直接访问文件系统、Shell 或数据库写接口。不得把扩展名当成目录。不得把用户提供的绝对路径传给 Tool。不得展示隐藏文件、隐藏目录或宿主机绝对路径。不得根据反馈自动启用新规则。
