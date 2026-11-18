# Codex启动提示被误当成工具事件：最小修复

日期：2026-11-13。状态：开发前记录方案；现已完成修复与Fake/OS验收，未请求云端模型。

2026-11-14修正：官方实现证实顶层error事件后也可以继续运行，原“error立即失败”判断过早。当前以[HTTPS连接修复方案](harness-codex-https-fix.md)为准：等待真实终态，保留失败、退出码、JSON和工具校验。

## 1. 本次实际结果与定位

`adaptive-validation-codex-v1`流程结束但INCONCLUSIVE，156次CLI调用全部REQUEST_REJECTED，两组均回落规则或预检，没有有效Adaptive结果。不能把两组15/42当成模型对照成绩。

代码中只有遇到非reasoning/agent_message的item事件才会产生此类别。用相同本机CLI、同一参数和固定假问题，在OS明确禁止全部网络的5秒探针中复现：`thread.started -> item.completed(error，功能/skill提示) -> item.completed(error，选定模型元数据未匹配提示) -> turn.started`。没有云端模型请求；工具事件并未出现。

官方0.162.1协议将`ThreadItemDetails::Error`定义为非致命提示，工具执行另有独立类型。参见[官方协议源码](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec/src/exec_events.rs)、[JSON事件说明](https://learn.chatgpt.com/docs/non-interactive-mode)。原适配器混淆了这两类事件，是这次必须修复的兼容问题。

该轮实验使用用户选择的`gpt-6-Luna`。本地模型缓存未匹配该名称只是线索，不足以证明云端不支持；本次不自动替换或修改env，后续探针以用户当前env为准。

## 2. 修改边界

- 单独处理合法的`item.type=error`提示：只记录有限的脱敏摘要，不将其作为工具调用，也不把它当作模型结果。
- 顶层`error`记录后继续等待真实终态；`turn.failed`、非零退出、缺少完成事件仍失败；Shell、文件变更、MCP、子Agent、搜索、计划等真实操作事件继续拒绝。只有单轮、明确完成、合法JSON仍可成功，OS限制不变。
- 每次Codex调用新增`codex_event_diagnostic`：阶段、最后事件/条目种类、启动提示数量、有限原因提示枚举、是否开始/完成、清理前退出码。不保存错误原文、prompt、推理、路径、账户或token。原API记录格式不变。
- 增加`harness codex-probe`：默认仅本机配置和登录检查，`--live`才用固定假问题调用一次Codex、检查JSON与用量。读取同一测试env，不使用真实题目、Gold或文件；无自动重试，不创建实验或改变评分。失败输出脱敏摘要。先单次验证，再决定是否跑整批。
- 只读兼容已知旧C-v3执行器身份`886b2907c4e17ac4fa9ce7eafb0af9a377d6c0d7f5d6123c72e805441e951345`，继续重放原产物；不得改写这次失败实验。原Adapter参数身份和评分标准不变，新执行器代码身份自然变化。

## 3. 文件和验收

修改Codex适配器、宿主Journal、C旧身份兼容、CLI和Fake测试；同步操作文档、README与runbook。Fake验证启动提示后正常完成、提示后真正失败、真实工具拒绝、缺少完成、脱敏摘要、逐调用记录、单次probe不自动请求/不重试、历史只读兼容。OS专项仍用Fake模型。旧三轮实验只读重算，模型env保持不变；开发不请求云端模型或再次跑整批。

完成后明确告知：本次全部失败及原因、修复内容、先执行单次probe的命令，以及云端模型是否可用仍待probe验证。

## 4. 操作步骤

先在项目根目录检查当前env、CLI和登录，零模型调用：

```bash
python scripts/run_evidence_test.py harness codex-probe
```

只有下一条会发起一次真实Codex调用，固定假问题，不使用真实题目或文件：

```bash
python scripts/run_evidence_test.py harness codex-probe --live
```

应显示`stage=COMPLETED`、`requests_used=1`，并在`codex_event_diagnostic`看到事件摘要。启动提示允许存在，但不能替代成功终态。失败时请提供这次探针的JSON；它不包含提示、推理、文件正文或认证。模型未匹配元数据是线索，云端模型不支持、认证、网络和额度问题仍需真正的调用结果确认，不据缓存自动改模型。

先确认单次探针成功，再使用原三轮命令，输出换为新的`experiments/harness-v1/adaptive-validation-codex-v2`；保留失败的v1。C的R035预检仍不属于有效Adaptive比较，本修复不修改该验收门槛。

## 5. 实际验收

- Codex/C/env专项102项通过，2项OS按设计跳过；其他A/B、预算、反馈、自改和日志相关303项通过，8项OS按设计跳过，共405项通过。早期23项传输专项是子集，不重复累计。
- 本次另行2项真实OS专项通过，均使用Fake CLI/Fake模型；Gold/env/其他实验不可读，其他程序不可执行，权限没有放宽。其余8项既有OS用例未在本次重跑。
- JUnit在实验根：`codex-startup-related.xml`、`codex-startup-legacy.xml`、`codex-startup-os.xml`。
- 实际测试env的`codex-probe`显示CHECK_PASSED、requests_used/model_calls均0，配置来源ENV。没有执行`codex-probe --live`或重新运行真实三轮，模型是否可用仍待单次真实探针。
- 原真实C-v1、C-v2和Codex-v1均只读重放，比较结果与各自缓存一致，原目录未改写。修复只涉及适配器、Journal脱敏记录、旧执行器身份兼容、CLI、Fake测试及文档；没有修改模型env、固定候选、Gold、正式规划代码或原API行为。
- `git diff --check`通过。断网定位探针受到OS网络禁止规则保护，没有云端模型请求；正常开发验证只用Fake。

## 6. 2026-11-14单次探针核对

用户提供的单次结果运行约11.9秒，已进入TURN_ACTIVE，最后为顶层error且未收到完成；启动提示不再触发工具误拦截。`CONNECTION_HINT`来自错误文字的有限匹配，只说明连接/流中断线索，不能独立证明网络不通，也不能证明额度不足。

本机最新模型目录列有`gpt-6-luna`；当前env与本次probe的模型身份都对应`gpt-6-Luna`，大小写不同。已只将该env模型ID修正为目录中的全小写值，其他配置逐行核对未变。修正后零调用`codex-probe`检查通过，model_ref为`c4b5a917552265e5fafafe6cbd4330942045da42e40c70738fe20719b3c8b3d7`。

此修正消除了一处已确认的配置不一致，但尚不能证明连接失败由它单独导致。未修改程序、未请求真实模型；下一步仍由用户执行一次`codex-probe --live`，根据修正后的结果继续定位，不直接跑三轮。
