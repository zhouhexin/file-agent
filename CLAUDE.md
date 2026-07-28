# File Agent 开发说明

`agent.md` 是本仓库的最高级项目规范。本文件是给 Claude Code 的快速入口，不能替代或覆盖
`agent.md`；开始任何分析、修改或命令执行前，必须完整阅读并遵守 [agent.md](./agent.md)。

## 项目定位

File Agent 是学校/学工场景的对话式文件工作智能体。聊天框是普通用户的主入口；文件检索、证据回答、
分类、OCR、改名、移动和删除都是围绕文件工作的能力，不要把项目重构为单纯的问答系统或网盘。

普通用户不能看到 Skill、Tool、AgentRun、LangGraph 节点、数据库表名、服务器路径或后台任务中间状态。
这些只属于后端安全与审计边界。

## 文档优先级

发生冲突时依次遵循：

1. 当前用户的明确指令。
2. `agent.md`。
3. `docs/automatic-organization-conversational-access-implementation-plan.md`。
4. `agent.md` 中列出的其余架构、API、数据库和阶段计划文档。

当前阶段计划位于 `docs/`；修改实现前应先定位并阅读与任务相关的计划、接口或数据模型文档。

## 架构速览

- 后端：`apps/api/app`，FastAPI + SQLAlchemy + Alembic + PostgreSQL/pgvector。
- 前端：`apps/web`，React + TypeScript + Vite。
- Agent：`apps/api/app/modules/agent`，受控 LangGraph、Planner、Tool Registry。
- 文件生命周期：`apps/api/app/modules/file_lifecycle`。
- 对话与附件上下文：`apps/api/app/modules/conversations`。
- 证据回答/RAG：`apps/api/app/modules/evidence_answer`、`retrieval`、`chunks`。
- 测试：`apps/api/app/tests`；LLM/embedding 测试必须使用 deterministic fake。

## 必须遵守的实现边界

- LLM 只做理解、编排和基于证据的表达；不得直接访问文件系统、Shell、数据库写接口或绕过 Tool。
- 所有副作用必须经过白名单 Tool、schema 校验、审计记录；高风险文件操作必须经过 OperationPlan 和用户确认。
- 原件不可覆盖或删除。工作副本、原件、派生件和版本血缘必须保持可追溯。
- 所有用户共享唯一物理工作目录；用户默认 workspace 只用于会话、上传来源、反馈与审计。
- 完整文件名是最高优先级范围：唯一活动工作副本只读取该文件；同名先让用户选择；未命中只展示相似文件选择卡。相似候选在用户确认前不得读取或作为回答依据。
- 回收站文件不能被检索或读取；精确命中时提示用户恢复。
- 普通用户的回复只显示任务结果、文件卡、明确待确认项和最终结果，不显示后台生命周期文案。
- 数字、日期、金额和表格计算必须由确定性工具计算；回答结论必须有原文证据支持。
- “低耗模式”只约束 LLM 调用预算，不能削弱导入、解析、OCR、索引、检索、审计或安全边界。

## 工作方式

- 修改前先检查工作树；保留用户已有的未提交改动，不使用 `git reset --hard`、`git checkout --` 等破坏性命令。
- 功能开发和 Bug 修复坚持最小必要修改：只修改实现当前目标所必需的代码、配置、迁移和回归测试；不得顺带重构、格式化、改名、升级依赖或改变无关行为。若确需扩大修改范围，必须先说明原因并取得用户确认。
- 使用 `rg` 搜索；使用 `apply_patch` 编辑文件。
- Python 代码、测试、注释和 docstring 保持中文说明风格，并为行为变化添加回归测试。
- 不因普通开发任务擅自提交 Git；只有用户明确要求时才提交。
- 数据库结构变化必须提供 Alembic 迁移，不能只依赖 ORM `create_all`。

## 常用验证命令

```bash
# 后端（从仓库根目录）
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest -q

# 前端构建
npm --prefix apps/web run build

# 补丁格式检查
git diff --check
```

实际 Python 路径应优先沿用用户当前已配置的环境；不要擅自创建、切换或要求新的虚拟环境。

## 运行与诊断

- 后端结构化日志默认在 `logs/file-agent-YYYY-MM-DD.log`，不要记录正文、OCR 全文、JWT、密码、API key 或完整 prompt。
- 文件同步和异步导入依赖 worker；空闲时 worker 没有输出是正常的，但失败应记录可诊断的服务端异常信息。
- 遇到真实数据或物理文件问题，先做只读检查并明确目标范围；不得清空数据库、工作目录、回收站或上传归档，除非用户明确授权且路径经过确认。
