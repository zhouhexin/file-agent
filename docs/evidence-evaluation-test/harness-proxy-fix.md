# 测试进程固定代理：最小修复

日期：2026-11-18。按修改前方案完成实现、自动测试及一次真实固定假问题验证。本次不改模型策略、Gold、超时、额度或Codex适配器。

## 已确认的现象

v3第一次建议调用8.4秒后返回连接异常线索；输入108,296字符、隔离检查通过。测试env没有显式代理；系统代理为127.0.0.1:7897。当前只读网络检查中，ChatGPT直连被重置，经7897代理访问`/backend-api/codex/responses`收到405和Allow: POST，证明该路径HTTP可达，不代表已验证模型调用。代理访问站点首页曾有TLS错误，因此不能用首页结果代替实际路径，也不能承诺外部网络永远稳定。

## 最小修改

独立测试env增加`EVIDENCE_HARNESS_PROXY_URL=http://127.0.0.1:7897`。仅`run_evidence_test.py harness`在加载独立env后，把此显式配置映射为子进程大小写HTTP_PROXY/HTTPS_PROXY；没有配置时保持原行为。校验URL类型、主机、端口，不回显地址、口令或异常原文。其他启动动作与正式env不改变，不自动抓取系统代理，不覆盖系统设置。

Codex适配器原本已允许继承代理变量，因此无需改其实现或Profile；旧实验的可信控制器身份、原始调用、停止状态保持原样。测试代理只改变网络通路，不改变比较器、题目或候选。新增测试验证独立env生效、大小写冲突覆盖、无配置兼容、非法地址拒绝、API/worker动作不受影响。

先完成Fake测试和只读检查，再最多用一个固定假问题执行codex-probe验证真实模型连通性；不自动跑整批实验、不读取真实文档。若外部仍失败如实保留结果，不循环重试。

## 继续测试

v2和v3未生成候选，保留原目录即可。新批次以`frozen-exploration-v1`承接已有7份候选，写入新`frozen-exploration-v4`，避免不断把没有新增候选的失败批次加入依赖链。默认先检查，通过再显式live。仍按6份候选、366次总请求和7200秒总时限；完成后才复测。

```bash
python scripts/run_evidence_test.py harness campaign-run \
  --source experiments/harness-v1/frozen-exploration-v1 \
  --output experiments/harness-v1/frozen-exploration-v4 \
  --max-rounds 6 --max-no-progress 6 --repetitions 1 \
  --max-requests 366 --max-active-seconds 7200 \
  --advisor-max-output-tokens 8000 --planner-max-output-tokens 3000
```

看到CHECK_PASSED后，同一命令加`--live`开始。完成后使用`campaign-compare --experiment experiments/harness-v1/frozen-exploration-v4`，只有stage为COMPLETED才继续`campaign-retest`，来源换成v4，输出使用新的`development-retest-v4`；其余前2份、5次、600次总额度不变。R035等原有限制继续保留。

## 实际变更与验收

- 修改`scripts/run_evidence_test.py`：在harness启动时应用明确的测试代理并校验，其他入口不注入。
- 修改私有`runtime/evidence-test/.env`：只增加`EVIDENCE_HARNESS_PROXY_URL`；正式env、系统代理、模型配置和旧实验记录不变。
- 新增`apps/api/app/tests/test_harness_proxy.py`，与原环境隔离、Provider配置测试一起37项全部通过，报告`runtime/evidence-test/experiments/harness-v1/harness-proxy-tests.xml`。
- 零调用`codex-probe`检查通过；随后只执行一次固定假问题的`codex-probe --live`，结果COMPLETED，6687ms，turn.completed，退出码0，错误事件0。实际记录输入9932、输出21 Token；未发送真实文档，未开始整批测试。模型Profile和adapter_ref与旧实验保持相同。
- 新v4整批入口只检查也已通过：10道开发题、7份历史候选、第一轮108,069字符，模型调用0次，输出目录未创建；摘要在`harness-proxy-preflight.json`。`git diff --check`通过。
- 新增本文并更新README、runbook及原操作文档。一次探测证明当前通路及认证可用于生成，不保证长输入或整批实验必然完成，实际效果仍由后续受控测试记录。
