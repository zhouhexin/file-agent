# 分类策略离线评测输入

本目录只保存不含真实文件正文、敏感文件名和真实目录的输入规范。真实样本清单、抽取文本、Gold Labels 以及评测输出必须放在受控的非 Git 位置。

评测入口是只读脚本：

```powershell
$env:PYTHONPATH = 'apps/api'
& 'D:\anaconda\envs\myenv\python.exe' -m app.scripts.evaluate_classification_policy `
  --manifest <受控目录>\manifest.json `
  --taxonomy-snapshot apps\api\app\modules\classification\taxonomies\unified_school_file_classification.json `
  --rule-snapshot rules\classification-policies\workdata-v1.json `
  --quality-mode conservative_rules `
  --output-dir <新的空目录>
```

`manifest.json` 只引用独立的 Gold Labels 文件；运行预测时不会把标签传入分类器。样本可使用受控的 `text_path`，或在小型脱敏回归样本中使用 `text`。脚本不会写入源文件、数据库、正式分类关系、工作副本或文件系统落位。

评测输出固定为 `summary.json`、`sample-results.jsonl`、`confusion-matrix.json` 和 `version-manifest.json`。输出不包含正文、原始文件名、源文本路径或完整异常文本。未通过人工仲裁、标记为复制件或超出范围的样本会保留诊断结果，但不计入指标分母。

`shadow` 仅生成观察结果；`conservative_rules` 只能配合 `policy_mode=conservative_rules` 的规则快照；`calibrated` 只能配合已发布的 `policy_mode=calibrated` 快照。三种模式都不会执行落位或移动。
