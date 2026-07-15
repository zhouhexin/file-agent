# 受管目录分类 Profile

每个受管根使用一个版本化 JSON 文件，文件名建议为 `<root_key>.json`。目录角色仅用于图谱投影和弱标签治理，不能直接覆盖正式 taxonomy。

```json
{
  "root_key": "downloads",
  "version": "2026-07-v1",
  "default_role": "UNKNOWN",
  "rules": [
    {"path_prefix": "人事处", "role": "DEPARTMENT"},
    {
      "path_prefix": "人事处/职称评定",
      "role": "CATEGORY",
      "category_path": ["人事处", "职称评定"]
    },
    {"path_prefix": "临时", "role": "TEMPORARY"}
  ]
}
```

`PATH_AS_WEAK_LABEL` 模式下，只有 `CATEGORY` 规则会生成动态分类和 `PATH_SUGGESTS`；其他角色只保留目录层级。
