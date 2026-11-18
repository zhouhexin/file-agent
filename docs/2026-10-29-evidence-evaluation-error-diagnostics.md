# 证据评估失败的分类与安全诊断

## 1. 解决的问题

原来的 `SEMANTIC_CHECK_FAILED` 只能说明语义检查失败，无法区分接口连接、额度、格式和程序异常。
本次把安全的错误元数据从模型客户端传到语义检查器，再写入 `evidence_evaluation.shadow` JSONL。
不依靠异常原文猜原因，也不把评估失败当作 MISSING、CONFLICT 或“证据充分”。

所在环节：

```text
证据包 → 语义检查 → 模型请求 / 解析 / 校验
                     ↓ 失败时保留安全元数据
          CheckResult.error_diagnostics
                     ↓
          evidence_evaluation.shadow.error_details
```

模型调用配置不完整、尚未进入语义检查的失败，在 `SHADOW_SETUP` 阶段记录。
评估故障仍不阻断原回答；已经确定的 OUTDATED 不会被语义失败抹掉。

## 2. 日志怎么看

在项目根目录执行：

```bash
rg --no-ignore -n -F 'evidence_evaluation.shadow' runtime/evidence-test/logs/
```

`runtime/` 和日志目录被 `.gitignore` 忽略，必须保留 `--no-ignore`。

以下是人工示例，表示接口明确返回额度错误；不是本次真实接口调用结果：

```json
{
  "status": "EVALUATION_FAILED",
  "error_code": "SEMANTIC_QUOTA_EXHAUSTED",
  "message": "模型账号额度、余额不足或计费受限。",
  "error_details": [
    {
      "category": "QUOTA_EXHAUSTED",
      "stage": "LLM_REQUEST",
      "exception_type": "HTTPStatusError",
      "http_status": 403,
      "message": "模型账号额度、余额不足或计费受限。"
    }
  ]
}
```

字段含义：

| 字段 | 大白话 |
| --- | --- |
| `category` | 哪一类问题，例如网络、额度或格式。 |
| `stage` | 出错时正在请求接口、解析返回内容，还是核对评估字段。 |
| `exception_type` | 白名单中的异常类名；未知自定义名字统一为 `Exception`。 |
| `http_status` | 实际取得的 HTTP 状态；取不到时为 `null`。 |
| `message` | 后端按类别生成的固定中文说明，不是服务端原始报错。 |
| `response_shape` | 格式失败时的返回类型、空值、长度、finish_reason和具体状态；非响应错误可为null。 |

2026-10-30增加[具体返回形态诊断](2026-10-30-model-response-shape-diagnostics.md)，
评估还会记录独立 `evidence_evaluation.llm_response_shape` 事件。这样能区分空content、null、
数组、JSON解析失败及长度结束；success形态事件不代表后续schema/证据评估通过。

`http_status=null` 表示没有 HTTP 响应，或后置校验层没有取得传输状态。
例如网络断开通常为 null；HTTP 200 返回非法 JSON 时可记录 200；客户端已返回后发生的
评估 schema 校验错误在当前协议下没有 HTTP 元数据，保持 null，不能猜测为 200。

## 3. 类别与阶段

| 类别 | 含义与分类依据 |
| --- | --- |
| `NETWORK_ERROR` | HTTP 客户端报告连接、传输或远端连接中断。 |
| `TIMEOUT` | HTTP 客户端超时，或接口返回408/504。 |
| `QUOTA_EXHAUSTED` | 接口返回402，或结构化 `error.code/type` 命中额度白名单。 |
| `AUTHENTICATION_FAILED` | HTTP 401。 |
| `PERMISSION_DENIED` | HTTP 403，且没有明确的额度错误码。 |
| `RATE_LIMITED` | HTTP 429，且没有明确的额度错误码。 |
| `PROVIDER_ERROR` | 其它 HTTP 5xx 服务端错误。 |
| `REQUEST_REJECTED` | 其它 HTTP 拒绝响应。 |
| `RESPONSE_FORMAT_INVALID` | HTTP 响应为空/非 JSON，或模型内容不是 JSON 对象、缺少协议结构。 |
| `SCHEMA_INVALID` | JSON 可解析，但评估字段、类型或状态不符合 schema。 |
| `EVIDENCE_VALIDATION_FAILED` | 引用 ID、原文值定位或锁定要求等校验失败。 |
| `CONFIGURATION_ERROR` | 模型连接配置缺失、URL 无效或协议不受支持。 |
| `INTERNAL_ERROR` | 评估程序发生其它内部异常。 |
| `UNKNOWN_LLM_ERROR` | 客户端抛出通用模型异常，但没有足够元数据判断原因。 |

额度识别只使用后端白名单里的结构化错误码，例如 `insufficient_quota`，不会把错误体里
任意出现“quota”的文字当作额度不足，也不输出未识别的供应商错误码或消息。

| 阶段 | 含义 |
| --- | --- |
| `SHADOW_SETUP` | 创建旁观评估依赖或连接配置。 |
| `INTENT_PLAN_VALIDATION` | 规划阶段校验模型来源条件；不属于语义评估模型调用。 |
| `ANSWER_SUPPORT_REPAIR` | 回答结论全部被拒后，在既有预算中修正；不属于证据语义评估。 |
| `SEMANTIC_PREPARE` | 准备字段、批次与输入。 |
| `LLM_REQUEST` | 请求模型接口。 |
| `HTTP_RESPONSE_PARSE` | 解析 HTTP 响应包。 |
| `MODEL_RESPONSE_PARSE` | 读取模型内容并解析 JSON。 |
| `SEMANTIC_SCHEMA_VALIDATION` | 验证评估字段和类型。 |
| `SEMANTIC_EVIDENCE_VALIDATION` | 验证引用、原文定位及锁定要求。 |
| `SEMANTIC_RESULT_MERGE` | 合并批次、整理要求与冲突结果。 |

`error_codes` 继续兼容既有原因码；明确类别采用 `SEMANTIC_NETWORK_ERROR`、
`SEMANTIC_TIMEOUT`、`SEMANTIC_QUOTA_EXHAUSTED`、`SEMANTIC_RESPONSE_FORMAT_INVALID` 等。
证据校验保留 `UNKNOWN_EVIDENCE_ID` 等固定码。只有内部/未知模型异常仍可能使用
`SEMANTIC_CHECK_FAILED`，此时也有 `error_details` 定位类别与阶段。

## 4. 安全边界

- 不记录 API key、JWT、URL、请求正文、文件正文、模型完整输出、供应商错误体或异常原文。
- 异常类名、类别和阶段都有白名单；中文说明来自固定映射。
- 模型客户端抛出的 HTTP/网络异常文字也改为固定说明，避免其他调用方记录 `str(exc)` 时泄漏响应体。
- 不新增评估重试，不改变调用上限、超时、正常响应、成本统计或文件操作权限。
- 不新增 env 项、不迁移数据库、不改写历史日志。

## 5. 文件与验证

| 文件 | 改动 |
| --- | --- |
| `apps/api/app/modules/llm/diagnostics.py` | 新增诊断协议、固定中文说明、异常类白名单和 HTTP 分类。 |
| `apps/api/app/modules/llm/client.py` | 在 HTTP、网络和解析失败时携带安全元数据，移除原始响应体/异常文字回显。 |
| `apps/api/app/modules/evidence_evaluation/schemas.py` | CheckResult 增加可选诊断列表。 |
| `apps/api/app/modules/evidence_evaluation/semantic_checker.py` | 记录当前阶段，分类 schema/引用/内部异常，保留客户端诊断。 |
| `apps/api/app/modules/evidence_evaluation/service.py` | JSONL 增加 `error_details`、首个错误码和固定中文消息；评估失败用 WARNING。 |
| `apps/api/app/modules/evidence_answer/service.py` | 评估初始化失败也记录安全诊断，保持原回答可继续。 |
| `apps/api/app/tests/test_evidence_evaluation_diagnostics.py` | 新增39个参数化场景，覆盖分类、阶段、真实 JSONL、安全性和旁观故障隔离。 |
| 本文及 MVP/验证报告/runbook/独立测试说明/README | 同步字段解释、文件清单与生效步骤。 |

相关测试共425项通过：新增诊断测试，以及模型客户端、评估单元与集成、证据回答、Agent Runtime、
Adaptive Planner、来源限定问答回归。所有模型与 HTTP 调用都使用 deterministic fake，未调用真实付费模型。

重启原来的 API 和 analysis 进程后重新提问，才会生成新格式日志：

```bash
# 项目根目录，API终端
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py api
```

```bash
# 项目根目录，analysis终端
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py analysis
```

先在原终端按 Ctrl+C 停止旧进程，再运行对应命令。无需重新上传或重建索引。
历史 `SEMANTIC_CHECK_FAILED` 缺少的原因无法事后补齐；本次只验证新记录的诊断能力，
没有判定或修复原模型接口的连接故障。
