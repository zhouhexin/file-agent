# C阶段在env选择Codex或原API

日期：2026-11-13。状态：开发前记录方案；现已实现并完成配置检查与Fake回归，未请求真实模型。

## 1. 要达到的效果

用户不必每次复制三个模型选择参数。把这次C阶段的选择写进`runtime/evidence-test/.env`，普通启动命令就会读取它。保留原API功能及原API配置，不更换全站聊天或后台服务的Provider。

```dotenv
EVIDENCE_HARNESS_PROVIDER=codex-login
EVIDENCE_HARNESS_CODEX_MODEL=gpt-6.1-sol
EVIDENCE_HARNESS_ALLOW_MODEL_PROFILE_CHANGE=true
```

切回原API只改第一项为`openai-compatible`。原API继续读取已有`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_CHAT_MODEL`；Codex模型字段在API模式下不参与选择，不必删除。Codex登录仍由本机CLI管理，env不保存登录token。

## 2. 选择顺序与错误处理

- 每项按“显式命令行参数 → env → 原默认值”选择；没有配置时仍是原API、未允许模型变更。
- 保留`--provider`、`--codex-model`和`--allow-model-profile-change`，新增对应的`--no-allow-model-profile-change`，可明确覆盖env中的true。
- Provider只接受`openai-compatible`或`codex-login`；允许变更只接受true/false。拼写错误直接拒绝，不暗中改用可能收费的其他接口。
- env中留着Codex模型时，可以通过命令行`--provider openai-compatible`临时切回原API。显式在API命令里又传`--codex-model`仍视为矛盾参数，不能悄悄忽略。
- 默认检查显示实际Provider及三个参数的来源（命令行/env/默认/未使用），保持零模型调用。只有显式live才执行。

## 3. 最小实现边界

新增Harness专用配置模块，由CLI在既有`get_settings()`载入当前env后一次性解析，再把明确参数交给原C执行器。配置模块不重新搜索env、不读取auth文件，不进入候选快照。

不修改全局Settings或实际规划代码，避免把这次选择配置混入固定原版源码。C执行器仍记录最终实际Profile、Provider及逐调用身份，评分和v3实验协议不变。历史实验不改写，也不需要新增旧执行器兼容名单。

本机测试env只新增/更新上述三项，设为Codex；其余内容（尤其原API和数据库信息）原样保留且不输出。同步测试env示例、C操作文档、README及runbook。

## 4. 验收和文件

新增`app/modules/evidence_harness/config.py`和对应Fake测试；修改`app/scripts/evidence_harness.py`，增加CLI与独立env测试。验证默认API、env选择Codex、切回API、CLI优先、显式关闭允许变更、非法值拒绝、候选不能读取env、只读比较不受当前env选择影响。

使用真实测试env分别做Codex默认检查、命令行临时API检查，均不得请求模型或创建实验目录；核对原API及数据库配置未变。历史C实验只读比较必须保持原结论。开发不安装、重启或运行整批live。

完成后提供简化启动命令和切回API的方法，说明真实模型可用性仍需后续live验证。

## 5. 实际结果

- 本机`runtime/evidence-test/.env`已设置上述三项，选择Codex及`gpt-6.1-sol`，明确允许Profile变更；写入时核对原API及数据库配置逐行保持一致，没有输出其值。
- 默认命令读取env，真实14题检查`CHECK_PASSED`、Provider为`codex-login`，三项来源均为`ENV`；临时`--provider openai-compatible`检查同样通过，Codex模型来源为`UNUSED`。两种检查模型/建议器调用均0，新实验目录均未创建。
- 92项相关回归通过，2项既有OS用例按设计跳过；包括env优先级、参数覆盖、显式关闭、错误拒绝、默认API、真实CLI分支的Fake live和独立测试env隔离。JUnit为实验根的`harness-provider-env-related.xml`；针对配置/CLI的报告`harness-provider-env-targeted.xml`是子集，不重复累计。
- 本次未重跑OS专项。隔离执行与评分源码身份仍为`886b2907c4e17ac4fa9ce7eafb0af9a377d6c0d7f5d6123c72e805441e951345`，没有新增候选可读文件或权限；此前2项Fake OS验收见Codex接入方案。真实旧C-v1/v2只读重放均与原比较一致，目录不变。
- 新增配置模块、配置测试和本文；修改CLI、C的CLI测试、独立env测试、env示例和说明文档。全局Settings、规划Prompt、v3执行/评分协议、全站API及后台服务未改变。
- `git diff --check`通过。没有安装、重启、提取登录token或启动整批live。后续正式命令见C操作文档第13节。
