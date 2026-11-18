# 模型具体返回形态记录

## 1. 用途与位置

原错误 `SEMANTIC_RESPONSE_FORMAT_INVALID` 同时覆盖空内容、非文本、JSON数组及JSON解析失败，
仅凭异常类型无法区分。本次在统一JSON响应解析环节记录安全的结构元数据，供下一次真实测试定位。

```text
HTTP返回 → 外层JSON解析 → choices/message/content提取 → content JSON对象解析
                  ↓ 每次只观察类型、长度与固定状态
           evidence_evaluation.llm_response_shape
                  ↓ 格式失败时同样附在
           evidence_evaluation.shadow.error_details[].response_shape
```

JSON解析成功也记录一次形态，因此后续schema校验失败时，仍能查看它先前返回了什么类型。
成功JSON的形态事件 `status=COMPLETED` 仅代表JSON解析成功，不代表证据评估通过；
评估状态仍看同一 `agent_run_id` 的 `evidence_evaluation.shadow`。

本次只增加诊断，不调整模型输出长度、响应格式参数、超时、重试次数、正常返回契约或证据校验门槛。
用户后续自行发起真实模型测试；本轮没有自动调用真实接口。

## 2. 查看方式

重启原API和analysis进程后，重新提交原来的问题，再在项目根目录执行：

```bash
rg --no-ignore -n 'evidence_evaluation\.(llm_response_shape|shadow)' runtime/evidence-test/logs/
```

按时间与 `agent_run_id` 对齐同一次提问，避免把旧日志当成本次结果。
原有只筛选 `evidence_evaluation.shadow` 的命令仍可看到格式错误附带的形态；
上述命令还能看到JSON成功、但后续字段校验失败时的形态事件。

没有HTTP响应的网络/超时错误仍用原诊断；无法凭空记录返回形态。历史日志不会补写。

## 3. 字段

| 字段 | 含义 |
| --- | --- |
| `http_body_bytes` | 已读取的HTTP响应字节数，无法取得则null。 |
| `envelope_type` | 外层HTTP JSON类型：object、array、string、null等；未解析时missing。 |
| `choices_type` / `choices_count` | choices字段的类型与实际数组项数，缺失不伪造为0项。 |
| `first_choice_type` / `message_type` | 第一项choice和message的类型。 |
| `content_type` | message.content实际类型；missing与null分别记录。 |
| `content_empty` | 空字符串/仅空白、null或空容器为true；字段缺失为null。 |
| `content_chars` | content为字符串时的字符数；不是字符串时为null。 |
| `reasoning_content_type` / `reasoning_chars` | reasoning_content的类型与字符串长度，不记录思考文字。 |
| `tool_calls_count` | 返回tool_calls数组时的项数，不记录调用名、参数或执行任何Tool。 |
| `finish_reason` | stop、length、max_tokens、content_filter、tool_calls、function_call等白名单；未知值记other。 |
| `output_truncated` | 接口返回length/max_tokens时为true；finish_reason缺失、null或未知时为null，不猜测是否完整。 |
| `parsed_json_type` | content解析后的JSON顶层类型；未尝试为not_attempted，无法解析为unparseable。 |
| `parse_outcome` | 固定的具体形态状态，见下表。 |
| `summary` | 后端按状态码生成的固定中文说明，不采用供应商或模型自由文本。 |

## 4. 具体形态状态

| 状态 | 说明 |
| --- | --- |
| `HTTP_BODY_EMPTY` | HTTP响应正文为空。 |
| `HTTP_JSON_INVALID` | HTTP正文无法解析为JSON。 |
| `HTTP_TOP_LEVEL_NOT_OBJECT` | 外层JSON是数组、字符串或其它非对象类型。 |
| `CHOICES_MISSING` / `CHOICES_EMPTY` / `CHOICES_TYPE_INVALID` | 缺少choices、空数组或类型错误。 |
| `CHOICE_TYPE_INVALID` | 第一项choice不是对象。 |
| `MESSAGE_MISSING` / `MESSAGE_TYPE_INVALID` | 缺少message或message不是对象。 |
| `CONTENT_MISSING` | message里没有content字段。 |
| `CONTENT_NULL` | content明确为null。 |
| `CONTENT_EMPTY` | content是空字符串或只有空白，字符数可区分两者。 |
| `CONTENT_NON_TEXT` | content是数组、对象或其它非字符串类型。 |
| `MODEL_JSON_INVALID` | content非空，但不能按现有兼容解析器解析为JSON。 |
| `MODEL_JSON_NOT_OBJECT` | content可解析为JSON，但顶层为数组/字符串/数字/布尔/null。 |
| `MODEL_JSON_OBJECT` | content成功解析为对象，后面仍须通过schema与证据校验。 |

例如，下面是人工示例，不能当成对历史请求根因的判定：

```json
{
  "content_type": "string",
  "content_empty": true,
  "content_chars": 0,
  "reasoning_content_type": "string",
  "reasoning_chars": 6200,
  "finish_reason": "length",
  "output_truncated": true,
  "parsed_json_type": "not_attempted",
  "parse_outcome": "CONTENT_EMPTY",
  "summary": "message.content为空字符串或只有空白。"
}
```

该示例表示接口返回了思考文字、没有最终content，并报告长度限制结束。
实际是否出现这种情况，要以重启后的新记录为准，不从先前笼统错误倒推。

## 5. 安全与兼容

- 只记录白名单类型、状态、结束原因和长度；不记录响应预览、content、reasoning_content、完整原始响应、
  任意JSON键名列表、文件正文、URL、headers、密钥或Tool参数。
- 未知finish_reason统一other，未知对象类型统一unknown，自定义字段无法直接进入诊断协议。
- 形态观察器为后端注入依赖，不进入请求载荷或AgentGraphState；观察器失败不影响正常结果，也不增加调用。
- 评估专用客户端记录独立形态事件；标准JSON客户端的格式异常也携带该形态，普通调用成功返回类型不变。
- 保持markdown代码块/前后说明文本中的JSON对象兼容解析，不因新增诊断而更改解析策略。

## 6. 文件及验证

| 文件 | 作用 |
| --- | --- |
| `apps/api/app/modules/llm/response_shape.py` | 新增形态协议、结构检查和固定中文说明。 |
| `apps/api/app/tests/test_llm_response_shape.py` | 新增29项参数化场景，覆盖返回形态、长度结束、思考内容不泄漏、真实JSONL及观察器故障隔离。 |
| `apps/api/app/modules/llm/client.py` | JSON解析时采集形态；成功和格式失败各观察一次，异常保留具体形态。 |
| `apps/api/app/modules/llm/diagnostics.py` | 安全异常诊断增加可选response_shape，并投影固定中文summary。 |
| `apps/api/app/modules/evidence_evaluation/service.py` | 评估工厂注入日志观察器，形态随请求运行上下文关联。 |
| 本文、MVP、错误诊断说明、README、runbook及独立测试说明 | 记录字段、命令、兼容边界与生效步骤。 |

相关回归共522项通过，包括29项新场景。HTTP/模型均使用fake；没有调用真实模型或改写历史日志。
无需env新增项、数据库迁移、重新上传或重建索引。原API和analysis终端分别停止后，按原启动命令重启即可：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py api
```

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py analysis
```
