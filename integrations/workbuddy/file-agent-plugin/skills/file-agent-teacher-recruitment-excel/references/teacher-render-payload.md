# 教师引进列内格式载荷示例

这里的 `source_unit_id`、`quote`、`start/end` 都必须来自本轮正式 `structured_dataset_source(mode="FULL")` 的真实返回值。示例文字和 UUID 仅说明结构，不能复制为某位教师的数据。`fragment_index` 从本格 `fragments[0]` 起算，`parts[].text` 必须是所指原文 `quote` 中逐字存在的子串。

## I 列学历

假设正式来源有两个独立原文片段：“博士毕业 2026.09 西安理工大学 计算机科学与技术”和“本科毕业 2017.07 西南民族大学 网络工程”，但硕士栏没有记录。本格提交两条带来源位置的 `fragments`，不把“硕博连读”写进 `raw_text`：

```json
{
  "fragments": [
    {"source_unit_id": "<正式UUID-1>", "quote": "博士毕业 2026.09 西安理工大学 计算机科学与技术"},
    {"source_unit_id": "<正式UUID-2>", "quote": "本科毕业 2017.07 西南民族大学 网络工程"}
  ],
  "teacher_render": {
    "kind": "EDUCATION",
    "items": [
      {"stage": "博士", "parts": [
        {"fragment_index": 0, "role": "GRADUATION", "text": "2026.09"},
        {"fragment_index": 0, "role": "SCHOOL", "text": "西安理工大学"},
        {"fragment_index": 0, "role": "MAJOR", "text": "计算机科学与技术"}
      ]},
      {"stage": "本科", "parts": [
        {"fragment_index": 1, "role": "GRADUATION", "text": "2017.07"},
        {"fragment_index": 1, "role": "SCHOOL", "text": "西南民族大学"},
        {"fragment_index": 1, "role": "MAJOR", "text": "网络工程"}
      ]}
    ]
  }
}
```

服务端生成的 Excel I 格：

```text
博士：2026.09
西安理工大学
计算机科学与技术

硕士：硕博连读

本科：2017.07
西南民族大学
网络工程
```

若片段在来源单元中重复，必须附上正式文本中的真实 `start/end` 字符范围。来源明确显示硕士阶段时必须提交该阶段的真实片段，不能触发默认句。

## J/K/L 与 M/N/O 的同号规则

J 的 `teacher_render` 是 `kind="PAPER"`，每篇一条 `items`，`ordinal` 从 1 连续递增；每个条目用 `role="TEXT"` 指向论文题目。K/L 的每条只保留有证据的论文，沿用 J 中的原 `ordinal`。K 的刊号和刊名用 `ISSN`、`JOURNAL_NAME`，作者角色等用 `TEXT`；L 的原文收录/IF 用 `TEXT`，明确的 SCI/SSCI/A&HCI 分区用 `ZONE`。L 根据期刊名称或 ISSN 触发学校目录判级时，`teacher_render.catalog_version_id` 必须等于本轮 `reference_catalog_get` 返回的版本；服务端会再次精确查询并校验。会议不传期刊目录字段。每篇最终展示为 `1.内容`，篇间一个换行。

**CCF 按篇优先：**某个 L `item` 的正式来源 `TEXT` 已含 CCF 标签时，该篇即使还有 `ZONE`、`ISSN` 或 `JOURNAL_NAME`，服务端也只保留原文展示，不读取 `xaut-journals-2019`，不追加学校 A++/A+/A/B 等级；同一 L 字段的其他无 CCF 篇目照常匹配。只有 CCF 篇目时不传 `catalog_version_id`，也不调用 `reference_catalog_get/query`；与无 CCF 篇目混合时，字段级 `catalog_version_id` 只供后者使用。外部搜索所得、未写入正式来源的 CCF 信息不能伪装为 `TEXT` 标签或触发此规则。

L 列每篇只要有已确定等级，最终显示就把等级排在该篇序号之后、其他事实之前，例如 `1.A++类,SCI-1区,IF:9.098`、`2.B类,被引3次`、`3.CCF A类会议,EI索引`。学校等级由冻结目录与分区规则生成；CCF 等级只能来自该篇正式来源 `TEXT`。缺等级时不添加占位，不改变 J/K/L 的论文对应序号。

原文片段和 `raw_text` 即使带 `<br>` 也保持原样；仅展示和写入 Excel 时将 `<br>`、`<br/>`、`<br />`（不区分大小写）替换成换行。发布核对忽略等价换行与多余空白，不忽略正文、序号或等级。自动换行和行高尽力设置，不作为单独的发布门槛。

例如来源写 `SCI/A&HCI/SSCI中科院1 区，IF:9.098，被引0 次`，L 的同一 `item.parts` 要分别给 `{"role":"ZONE","text":"SCI/A&HCI/SSCI中科院1 区"}` 和 `{"role":"TEXT","text":"IF:9.098，被引0 次"}`，两者的 `fragment_index` 都指向包含对应原文的 L 引文，并设置冻结的 `catalog_version_id`。服务端按 2019 版规则生成 `A++类`。若该篇 K 的正式来源另含 `ISSN 0031-3203 Pattern Recognition`，L 还需引用那条真实来源为本格 `fragments`，用 `ISSN` 与 `JOURNAL_NAME` 子串提供目录查找；两个角色不重复显示。不要仅在 K 提交 ISSN 后期待 L 自动跨列取值。`JCR Q1` 只能作为 `TEXT`，不能据此生成中科院分区等级。分区与目录命中等级冲突时，等级留空并在回执说明。

M/N/O 的 `kind="PROJECT"`，分别用本列真实原文 `TEXT` 片段组成条目；同一项目使用同一个 `ordinal`，项目间空一行。某列缺少某项目时不造 `n.` 占位，后续项目不重新编号。P 的 `kind="ACHIEVEMENT"`，条目间一个换行。I–P 的实际物理列与本次目标真实表头不一致时不要使用这些格式标识。

## 发布前核对

`excel_writeback_apply.rows` 中有值的格必须是上述规则的**最终展示字符串**，A 和所有空格省略。下载预览需逐格核对 J/K/L 的同号关系、M/N/O 的同号关系、I 的阶段空行和新行的自动换行/行高；`excel_writeback_publish` 会再次核对格式值、来源引文、目录版本和工作簿差异。失败时按错误码停止，不能报“已保存”。
