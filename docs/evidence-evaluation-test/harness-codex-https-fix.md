# Codex连接修复：明确使用HTTPS，等待真实终态

2026-11-14，开发前记录；现已实现并完成Fake与OS验收，未请求云端模型。

本机只读doctor确认：HTTP端点可达，WebSocket握手被重置。0.162.1官方源码还表明顶层error事件后CLI状态仍可为Running，不能收到第一个error就清理进程。参考[事件处理源码](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec/src/event_processor_with_jsonl_output.rs)、[配置说明](https://learn.chatgpt.com/docs/config-file/config-reference)。

最小修复：

1. 新增`EVIDENCE_HARNESS_CODEX_TRANSPORT=https`，可选auto；CLI可临时覆盖。原API忽略此env项，保留原功能。未配置仍用旧auto模式。
2. HTTPS模式使用独立Provider ID、`requires_openai_auth=true`、`wire_api=responses`、`supports_websockets=false`。不覆盖保留的openai ID，不复制token，不设置外部代理地址；让Codex按ChatGPT登录选择原生端点。配置请求及流重试为0，仍以CLI调用计数，实际HTTP次数不可完全观察。用户全局Codex配置不改。
3. 对顶层error只记录脱敏线索并继续等待；turn.failed、非零退出、超时、工具事件和非法JSON仍失败。只有一轮、正常退出、完成事件和合法JSON都满足才能成功，不把重连提示当作答案，也不由宿主新增重试。
4. Profile绑定HTTPS及独立Adapter参数身份。原auto Profile保持原字段和身份。兼容已知启动修复执行器`c9902ecf2b0c0b6af67e47e1823d9f2e56c7e4bb1128fdeb04ae9642db944926`的只读比较；旧实验不改。
5. 本机测试env只新增HTTPS项，不修改当前模型、原API或数据库。先完成零调用检查，再由用户运行一次codex-probe --live；HTTPS可达不等于已经验证推理成功。

修改Codex适配器、Harness配置及CLI、C Profile核验和Fake测试，更新env示例、README/runbook/C操作文档。验收覆盖error后恢复、error后失败/无完成/非零退出、工具拒绝、HTTPS参数/认证、两种Profile和旧记录、env覆盖与API保留。OS测试仍Fake；不自动请求模型、不重跑整批、不放宽隔离或改Gold。

## 实际验收

- 109项相关测试通过，2项OS在普通运行中按设计跳过；另行2项真实OS验收通过，均Fake。覆盖HTTPS参数、恢复/失败/未完成/退出码、两种Profile、固定候选重放、env/CLI及原API选择。未修改API服务代码或隔离权限。
- JUnit为实验根的`codex-https-related.xml`与`codex-https-os.xml`；43项早期定向测试是子集，不重复累计。没有为此重跑不相关的全站测试或付费评测。
- 本机原生CLI实际加载HTTPS配置成功，ChatGPT登录可识别；沙箱外doctor确认HTTP端点可达、WebSocket未启用。此检查不请求模型，不能证明真实推理成功。
- 当前测试env只增加`EVIDENCE_HARNESS_CODEX_TRANSPORT=https`；当前模型与原API参数不改。`codex-probe`零调用检查通过，Profile为HTTPS_SSE，配置来源ENV。
- 原三轮真实实验（C-v1、C-v2、Codex-v1）只读比较均与原缓存一致，目录不变。`git diff --check`通过。下一步由用户运行一次codex-probe --live，成功后再考虑新目录整批验证。

## 2026-11-14后续真实诊断

用户提供HTTPS模式的FAILED：12.285秒、turn.failed、CONNECTION_HINT、无Token。只读检查发现没有保存该次CLI原始错误，现有摘要不足以确定具体断线原因。进一步在相同外层OS隔离内执行doctor，HTTP端点检查也通过，未发起模型推理；这仍不能证明所有推理请求可成功。

经用户单次真实诊断授权，加载当前测试env后直接调用同一Codex适配器、相同HTTPS配置及固定假问题，额外仅一次CLI调用：COMPLETED，正常JSON、turn.completed、退出码0，输入9928Token、输出21Token，无error通知。仅在内存观察并脱敏错误，未用真实文档、未更改模型/程序/网络配置、未重跑整批。此结果证明该适配器和配置至少当前可成功，不等于间歇性故障已根治，也不是C路由验收。

用户失败入口是独立启动命令，本次成功入口是直接调用同一适配器；还应核对原终端的网络/证书环境。后续先获取原终端`codex doctor --json`中的network.env和network.provider_reachability，不凭已有模糊提示继续改模型或传输配置。
