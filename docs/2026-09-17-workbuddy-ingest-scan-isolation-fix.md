# WorkBuddy 附件导入与受管目录扫描隔离修复

## 背景与结论

2026-09-16 的 WorkBuddy 会话附件导入连续四次在约 120 秒后失败，MCP 端仅显示空错误。同期受管目录全量扫描持续运行约 55 分钟，并且调度器按 5 分钟周期在扫描结束后立即安排下一轮全量扫描。附件提交单本身有效，导入接口没有返回业务失败回执；问题属于长时间资源竞争和客户端未将 `httpx` 超时转换为可读错误的组合缺陷。

本修复不修改分类目录、分类规则、文件内容、原件保护、导入授权或批次幂等语义，也不新增数据库迁移。

## 改造范围

### 1. 周期全量扫描冷却

新增 `MANAGED_ROOT_FULL_SCAN_MIN_INTERVAL_SECONDS`，默认 3600 秒。scheduler/API 启动时的常规全量对账仅在满足以下条件时为某个受管根提交 `RECONCILE_MANAGED_ROOT`：

- 没有该根处于 `PENDING` 或 `RUNNING` 的扫描或对账任务；
- 最近一次终态全量扫描结束时间已经超过冷却窗口，或不存在历史扫描。

这避免大目录扫描完成后仅间隔数分钟又启动下一次全量扫描。目录 watcher 发现真实新增、修改或删除时仍直接提交带 `reason=watcher` 的对账任务，不受冷却窗口限制；管理员手动任务也不受影响。因此实时文件变化、首次部署和显式处理能力保持不变，受影响的仅是无变更的高频兜底全量扫描。

### 2. API 进程隔离

生产 API 容器通过 `API_UVICORN_WORKERS` 配置 Uvicorn worker 数，模板默认 2。扫描、解析和物化继续由独立 worker 队列执行；两个 API 进程用于避免单一 API 进程被慢请求或资源抖动时让所有 WorkBuddy 请求一起等待。该配置不改变路由、认证、数据库模型或任务语义。

### 3. MCP 超时可读化

MCP 新增本地环境变量 `FILE_AGENT_API_TIMEOUT_SECONDS`，默认 30 秒，范围 5–120 秒。针对 WorkBuddy 附件提交所需的创建批次、登记清单、封存清单和上传内容请求：

- 捕获 `httpx.TimeoutException`，返回明确的 `FILE_AGENT_REQUEST_TIMEOUT` 错误；
- 捕获网络请求异常，返回明确的 `FILE_AGENT_REQUEST_ERROR` 错误；
- 错误文案提示用户稍后重试，并说明不会重传已确认成功的批次。

同一 WorkBuddy submission 一直使用稳定幂等键，因此请求在服务器端已经成功、但响应在客户端超时的情形下，重试会恢复同一批次，不会重复导入文件。

## 验收

1. 最近一小时内存在已完成扫描时，scheduler 不再为该根创建常规对账任务。
2. 活跃扫描存在时，scheduler 不再创建重复对账任务。
3. 无扫描或冷却已过期时，scheduler 仍创建新的对账任务。
4. watcher 真实变更仍能立即提交对账任务。
5. MCP 创建批次请求超时时，调用方获得非空且可行动的错误码与文案。
6. 生产 compose 启动 API 时传入可校验的 API worker 数。

## 部署

无需 Alembic 迁移。更新代码镜像后重建 `api`、`scheduler`、`watcher` 和相关 worker。服务器 `deploy/.env` 中建议增加：

```dotenv
API_UVICORN_WORKERS=2
MANAGED_ROOT_FULL_SCAN_MIN_INTERVAL_SECONDS=3600
```

用户电脑的 WorkBuddy MCP 配置可选增加：

```json
"FILE_AGENT_API_TIMEOUT_SECONDS": "30"
```

未设置时使用上述安全默认值。
