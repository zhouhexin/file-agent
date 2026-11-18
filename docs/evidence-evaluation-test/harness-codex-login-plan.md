# C阶段使用本机Codex登录进行复测

日期：2026-11-13。状态：开发前记录方案；现已实现并完成工程验收，未请求真实模型或启动整批live。

后续按用户要求增加[env配置方案](harness-provider-env-plan.md)：C可从测试env读取Provider、Codex模型和允许变更，CLI优先覆盖；其他接入与评分边界保持。本文“默认原API”适用于没有配置新env项的情况。

## 1. 要解决的问题

原模型服务多次因额度不足拒绝请求。本机已经安装Codex CLI 0.162.1，并显示`Logged in using ChatGPT`。这次让C阶段可以调用本机已登录的Codex，认证和续期仍交给官方CLI；项目不读取、复制或保存登录token，不把OAuth登录当成原来的API key。

官方依据：[非交互调用与复用登录](https://learn.chatgpt.com/docs/non-interactive-mode)、[认证方式](https://learn.chatgpt.com/docs/auth)。本机CLI帮助确认支持`--ignore-user-config`、`--ignore-rules`、`--ephemeral`、`--json`与`--output-schema`。

## 2. 这次只接哪一段

给`adaptive-validate`增加`--provider codex-login --codex-model <明确模型>`，默认仍为原API。选择Codex时不改测试env或全站Provider：只把C实验宿主的模型代理换成独立Codex进程。原版和固定候选使用同一Codex版本、模型、推理设置和包装指令；原规划schema、后端范围校验、Gold、评分和候选都不改。

新实验另存`adaptive-validation-codex-v1`，必须显式允许Profile变更。首次检查只调用本机版本/登录状态命令，不请求模型、不建实验目录；正常程序和其他Harness阶段继续使用原设置。

## 3. 受控调用与记录

- 每次独立运行`codex exec`，使用临时空目录、stdin传入受控原Prompt和payload，不把正文放命令参数。忽略用户配置和项目规则，禁用应用、插件、Shell、多Agent、浏览器、图像等工具能力；不复用会话，不载入Gold。
- Codex进程由宿主启动，不交给候选进程。macOS OS限制阻止其读取或写入项目与真实实验目录，并禁止启动CLI以外的子程序。候选本身继续使用现有更严格的无网络隔离，得不到Codex认证。
- 只接收最终JSON与用量。使用固定JSON字符串包装schema适配规划协议中动态字典，再解包为原始结构；原规划服务继续验证。遇到工具事件、额外轮次、无完成事件、格式异常或超时即拒绝，清理整个进程组，不把stderr或自由异常正文写入日志。
- 固定每次60秒，原总时长和最多调用次数继续有效。CLI内部HTTP请求数无法完全观察，故Codex下预算单位明确为`CODEX_INVOCATIONS`，不冒充精确HTTP次数。宿主不重试，CLI内部重试次数不承诺为0；禁用无限连接重试，整次调用到60秒仍强制清理。不自动切换模型或回落到原收费API。
- Codex接口不承诺支持原API的输出Token硬上限。Profile明确记录`output_token_limit_enforced=false`、实际接口上限未知；`planner-max-output-tokens`只保留为调用方请求值，不声称已下发硬限制。用量有则真实记录，无则记未知；Codex仍有账号限额。
- 新实验采用v3协议，绑定Provider、模型身份、CLI版本/二进制身份、调用包装身份、实际额度能力和逐调用Profile；独立比较不要求当前重新登录，也不调用模型。继续只读兼容已知旧C-v1/v2，按其原协议核验，不补写旧产物。

## 4. 文件与验收

新增宿主专用`improvement/codex_backend.py`和Fake CLI协议测试；修改C检查/执行/比较、CLI入口和相应测试；同步README、runbook及C操作文档。本次不改API/worker、上传解析索引、正式规划Prompt和真实env。

Fake测试覆盖认证未登录、参数/配置固定、两组共用Profile、结构和用量解析、工具/多轮拒绝、超时/中断清理、未知费用及配置篡改；真实OS专项仍使用Fake CLI，检查Gold/env不可读。原A/B/C和预算相关回归必须通过。用本机已登录CLI做零模型调用检查；不自动运行14题真实复测。

## 5. 最终使用方式

在原C命令上增加`--provider codex-login --codex-model gpt-6.1-sol --allow-model-profile-change`，输出换为`experiments/harness-v1/adaptive-validation-codex-v1`。模型名取本机已有默认配置的明确值；程序不在运行中悄悄加载或更换默认模型。完整先检查、后live命令在操作文档中给出。

## 6. 本机兼容性细化

真实CLI配置加载检查发现0.162.1禁止通过`model_providers.openai.*`覆盖内置Provider，所以不采用这类重试参数，不另建非官方认证代理。继续使用内置OpenAI Provider和已有ChatGPT登录。Profile记录`max_retries=null`、`host_retries=0`、`provider_retries_observable=false`，说明只有宿主不重试，内部行为不能精确承诺；原API仍维持原来零重试协议。这个调整不修改用户全局Codex配置。

## 7. 验收结果

- C及适配器专项61项通过，普通运行跳过2项OS用例；其他A/B、预算、自改、反馈、日志与配置回归317项通过、8项既有OS用例按设计跳过，共378项通过。
- 本次另行执行2项真实OS验收并通过，均用Fake CLI/Fake模型：CLI确实不能读取项目文件或执行其他程序；候选重复验证的Gold/env/其他实验仍不可读。其余8项既有OS用例未在本次重跑，不能算本次通过。
- JUnit在实验根：`codex-login-c.xml`、`codex-login-related.xml`、`codex-login-os-acceptance.xml`。早期传输专项报告是子集，不重复累计。
- 本机CLI真实帮助参数检查、固定配置加载、ChatGPT登录状态均通过，零模型调用。实际测试env下14题默认检查`CHECK_PASSED`，开发10/保留4、固定候选不变，Provider为`codex-login`；新实验目录尚未创建。
- 原真实C-v1/v2分别只读重放，均与其原缓存结论一致，目录内容不变。没有改env、全局Codex配置、正式规划Prompt或服务，没有安装依赖，也没有读取/复制登录token。
- `git diff --check`通过。真实模型可用性、连通性及额度尚未验证，需要操作者后续显式live；C原评分门槛和R035预检限制仍保持。
