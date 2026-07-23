# 阶段四低耗两阶段文件检索实施方案

- 状态：实施完成
- 基础文档：`docs/stage-4-low-resource-two-stage-retrieval-plan.md`
- 开发原则：**先写测试验证功能缺失，再实现功能，最后验证测试通过**
- 运行模式：CPU-only、低内存、无 GPU、默认不调用外部模型或互联网
- 核心约束：**不能回退原有功能**。保留 `WorkingCopySummarySearchService` 作为紧急兼容回退，`hybrid-search`、`UserTaskReceipt` 和既有审计边界持续可用。两阶段链路由 `TWO_STAGE_RETRIEVAL_ENABLED=true` 默认启用；只有运维显式设为 `false` 才回退旧摘要检索。

---

## 开发策略：测试先行

每个任务严格按照以下步骤执行：

1. **写测试**：先编写测试用例，验证当前阶段该功能确实缺失或行为不正确
2. **确认失败**：运行测试确认失败，证明功能确实需要实现
3. **实现功能**：严格按照本方案的实现细节编写代码
4. **验证通过**：运行测试确认通过
5. **回归确认**：运行 `pytest -v` 确认全部已有测试仍通过，**不修改任何已有测试文件**

核心约束：**不能回退原有功能**。所有已有测试必须零修改通过。如有新代码导致已有测试 fail，必须先解决回归问题再继续。

测试文件按以下约定组织：

- 每个任务新增的 Service 必须配套独立测试文件，放在 `apps/api/app/tests/`
- 集成测试（端到端检索流程）放在 `apps/api/app/tests/test_two_stage_file_search.py`
- EXPLAIN 分析测试放在 `apps/api/app/tests/test_retrieval_index_usage.py`
- 旧测试文件不删除，只新增

---

## 任务 4.0：前置基线

**目标**：确认当前测试全部通过、阶段三索引可用、搜索回归样本就绪。

**步骤**：

1. 运行全量测试并记录结果：
   ```bash
   cd apps/api && pytest -v 2>&1 | tail -50
   cd apps/web && npm run build 2>&1 | tail -20
   ```

2. 确认 PostgreSQL 中 `document_chunks` 表索引存在：
   ```sql
   SELECT indexname, indexdef FROM pg_indexes
   WHERE tablename = 'document_chunks'
   AND indexname IN ('ix_document_chunks_search_vector_gin', 'ix_document_chunks_search_text_trgm');
   ```

3. 准备搜索回归样本（后续供任务 4.3~4.5 测试使用）：
   - 创建测试 fixture：`conftest.py` 级别 fixture，包含一份摘要能命中 query、一份摘要不能命中但正文能命中的文档
   - 写入文件：`apps/api/app/tests/fixtures/stage4_search_samples.py`

4. **写一个确认现有搜索行为受限的测试**：
   - 文件：`apps/api/app/tests/test_stage4_baseline.py`
   - 测试 1：创建 51 个文档，查询匹配第 51 个（最旧）—— 验证当前 `limit(500)` 实际上不能覆盖无限量
   - 测试 2：创建摘要不包含 query 但正文包含 query 的两个文档 —— 验证当前摘要搜索找不到它们
   - 运行并确认这两个测试确实 fail，证明阶段四有必要

5. **前置基线退出条件**：
   - `pytest -v` 全部通过（基线测试本身以外）
   - `npm run build` 成功
   - 回归样本 fixture 就绪
   - 基线测试确认功能缺失

---

## 任务 4.1：检索投影与迁移

**目标**：新增 `document_search_profiles` 瘦投影表、ORM 模型、Alembic migration、`DocumentSearchProfileService`。

### 涉及文件

| 动作 | 文件路径 |
|---|---|
| 新增 ORM | `apps/api/app/db/models.py` —— 新增 `DocumentSearchProfile` 类 |
| 新增 migration | `apps/api/alembic/versions/20260724_0001_create_document_search_profiles.py` |
| 新增 Service | `apps/api/app/modules/retrieval/search_profile.py` —— `DocumentSearchProfileService` |
| 修改配置 | `apps/api/app/core/config.py` —— 新增检索投影相关配置 |
| 新增测试 | `apps/api/app/tests/test_document_search_profile_service.py` |
| 新增测试 | `apps/api/app/tests/test_a_migration_20260724_document_search_profiles.py` |

### 实现细节

#### 4.1.1 ORM 模型

在 `apps/api/app/db/models.py` 中新增 `DocumentSearchProfile` 类，接在 `WorkingCopy` 模型之后：

```python
class DocumentSearchProfile(Base):
    """工作副本级瘦检索投影，只保存检索必需词项和稳定 ID。

    不保存完整 category_path_json、summary_preview 或 entities_json；
    候选收敛后的显示数据以一次批量 JOIN 从事实表读取。
    这是可重建的检索派生数据，不替代 WorkingCopy、DocumentSummary、分类建议或 Evidence 表。
    投影损坏时可以重建，不能反向修改文件客观事实。
    """

    __tablename__ = "document_search_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid4)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    working_copy_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False
    )
    document_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    document_version_id: Mapped[str] = mapped_column(
        String(36), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(40), default="ACTIVE", index=True
    )
    normalized_filename: Mapped[str | None] = mapped_column(Text, default=None)
    filename_search_text: Mapped[str | None] = mapped_column(Text, default=None)
    category_search_text: Mapped[str | None] = mapped_column(Text, default=None)
    metadata_search_text: Mapped[str | None] = mapped_column(Text, default=None)
    summary_search_text: Mapped[str | None] = mapped_column(Text, default=None)
    combined_search_text: Mapped[str | None] = mapped_column(Text, default=None)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
```

`search_vector` 字段的 TSVECTOR 类型使用 SQLAlchemy 的 `TypeDecorator` 或直接在 migration 中通过 `sa.Text()` + migration DDL 创建。由于 SQLAlchemy 没有原生的 TSVECTOR 类型且在 SQLite 中不可用，推荐用 `TypeDecorator` 封装：

```python
# 在 models.py 顶部或单独文件中
class TSVECTOR(TypeDecorator):
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import TSVECTOR as PG_TSVECTOR
            return dialect.type_descriptor(PG_TSVECTOR)
        return dialect.type_descriptor(Text)
```

#### 4.1.2 Migration

Alembic migration `20260724_0001_create_document_search_profiles.py`：

```python
"""Create document_search_profiles table for stage-4 lean search projection

Revision ID: 20260724_0001
Revises: 20260723_0001_create_document_chunk_indexes
"""

from alembic import op
import sqlalchemy as sa

revision = "20260724_0001"
down_revision = "20260723_0001_create_document_chunk_indexes"


def upgrade():
    op.create_table(
        "document_search_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("working_copy_id", sa.String(36), unique=True, nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("document_version_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="ACTIVE"),
        sa.Column("normalized_filename", sa.Text, nullable=True),
        sa.Column("filename_search_text", sa.Text, nullable=True),
        sa.Column("category_search_text", sa.Text, nullable=True),
        sa.Column("metadata_search_text", sa.Text, nullable=True),
        sa.Column("summary_search_text", sa.Text, nullable=True),
        sa.Column("combined_search_text", sa.Text, nullable=True),
        sa.Column("search_vector", sa.Text, nullable=True),
        sa.Column("source_fingerprint", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_dsp_user_ws_status", "document_search_profiles",
                    ["user_id", "workspace_id", "status"])
    op.create_index("ix_dsp_normalized_filename", "document_search_profiles",
                    ["normalized_filename"],
                    postgresql_using="btree")
    op.create_index("ix_dsp_document_id", "document_search_profiles", ["document_id"])
    op.create_index("ix_dsp_document_version_id", "document_search_profiles",
                    ["document_version_id"])

    # PostgreSQL-only GIN 索引
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE document_search_profiles "
            "ADD COLUMN search_vector TSVECTOR"
        )
        op.create_index(
            "ix_dsp_search_vector_gin", "document_search_profiles",
            ["search_vector"], postgresql_using="gin",
            postgresql_ops={"search_vector": "gin"}
        )
        op.execute(
            "CREATE INDEX ix_dsp_combined_trgm ON document_search_profiles "
            "USING gin (combined_search_text gin_trgm_ops)"
        )


def downgrade():
    op.drop_table("document_search_profiles")
```

#### 4.1.3 DocumentSearchProfileService

文件：`apps/api/app/modules/retrieval/search_profile.py`

核心方法：

- `upsert_current_profile(working_copy_id)`：
  1. 读取 `WorkingCopy` + `Document` + `DocumentSummary` + `DocumentCategorySuggestion`
  2. 计算 `normalized_filename`：去标点和小写
  3. 使用 `ChineseLexicalTokenizer` 分别对文件名、分类路径、元数据、摘要进行分词
  4. 使用 `setweight` 组合 `search_vector`：文件名权重 A、分类权重 B、元数据权重 C、摘要权重 D
  5. 计算 `source_fingerprint`：`sha256(current_version_id + normalized_filename + summary_id + classification_run_id + tokenizer_version)`
  6. 幂等 insert-on-conflict upsert（`working_copy_id` 唯一约束）

- `deactivate_profile(working_copy_id)`：将 `status` 标记为 `INACTIVE`

- `backfill_profiles(batch_size=100)`：
  1. 分页查询 `WorkingCopy` 中 `status=ACTIVE` 且不在 `document_search_profiles` 中的记录
  2. 每批 100 条调用 `upsert_current_profile`

- `reconcile_profiles(batch_size=100)`：
  1. 分页查询 `document_search_profiles` 中 `status=ACTIVE` 的记录
  2. 对比 `source_fingerprint` 与当前事实表的 fingerprint
  3. 不匹配则重新 upsert

- `_compute_fingerprint(working_copy, summary, classifications)`：确定性的版本指纹，用于 reconciliation

#### 4.1.4 事件接入点

在以下模块中新增 `DocumentSearchProfileService` 调用：

- `apps/api/app/modules/file_lifecycle/` —— 工作副本首次提交为 ACTIVE 时 upsert
- `apps/api/app/modules/file_rename/` —— 确认重命名后 upsert
- `apps/api/app/modules/classification/` —— 分类更新时 upsert
- `apps/api/app/modules/chunks/` —— 摘要或索引完成时 upsert

同步写入尽量与业务操作在同一事务内；异步事件使用可重试的幂等 upsert。

#### 4.1.5 配置项

在 `apps/api/app/core/config.py` 中新增：

```python
two_stage_retrieval_enabled: bool = True  # 默认走 CPU 两阶段检索；false 仅用于紧急回退
retrieval_document_candidate_limit: int = 30
retrieval_document_detail_limit: int = 12
retrieval_chunk_limit_per_document: int = 3
retrieval_chunk_global_limit: int = 24
retrieval_query_max_chars: int = 500
retrieval_preview_max_chars: int = 240
retrieval_statement_timeout_ms: int = 2000
retrieval_filename_trgm_min_chars: int = 4
retrieval_filename_trgm_candidate_limit: int = 20
retrieval_filename_trgm_similarity_threshold: float = 0.25
```

#### 4.1.6 测试（测试先行）

文件：`apps/api/app/tests/test_document_search_profile_service.py`

**测试 1**：写一个测试验证当前不存在 `DocumentSearchProfile` 模型或 `document_search_profiles` 表
- 断言 `import` 或表名不存在 —— 确认功能确实未实现

**测试 2**：写一个 upsert 的预期行为测试
- 创建 `WorkingCopy` + `Document` + `DocumentSummary` + `DocumentCategorySuggestion` fixture
- 调用 `upsert_current_profile(working_copy_id)`
- 断言 `document_search_profiles` 表创建成功
- 断言 `search_vector` 不为空
- 断言 `normalized_filename` 被规范化
- 断言 `source_fingerprint` 已计算
- 断言重复 upsert 不产生重复记录

**测试 3**：写一个 backfill 幂等的预期行为测试
- 创建多个 `WorkingCopy`（ACTIVE）
- 调用 `backfill_profiles(batch_size=10)`
- 断言所有工作副本都有对应 profile
- 断言重复 backfill 不产生重复

**测试 4**：写一个 reconciliation 的预期行为测试
- 手动修改 profile 的 `normalized_filename`
- 调用 `reconcile_profiles(batch_size=10)`
- 断言 `normalized_filename` 恢复

**测试 5**：写一个 deactivate 的预期行为测试
- 断言 `deactivate_profile` 后状态变为 `INACTIVE`
- 断言其他 profile 不受影响

---

## 任务 4.2：查询解析与范围解析

**目标**：实现 `FileSearchQueryParser`、`FileSearchScopeResolver` 和 L1 会话文件范围服务。

### 涉及文件

| 动作 | 文件路径 |
|---|---|
| 新增 | `apps/api/app/modules/retrieval/query_parser.py` |
| 新增 | `apps/api/app/modules/retrieval/scope_resolver.py` |
| 新增 | `apps/api/app/modules/conversations/session_files.py` |
| 新增测试 | `apps/api/app/tests/test_file_search_query_parser.py` |
| 新增测试 | `apps/api/app/tests/test_file_search_scope_resolver.py` |
| 新增测试 | `apps/api/app/tests/test_conversation_session_files.py` |

### 实现细节

#### 4.2.1 FileSearchQueryParser

```python
@dataclass(frozen=True)
class ParsedQuery:
    original: str
    cleaned: str            # 去除了低信息量请求词
    terms: list[str]        # Jieba 分词 + 业务词典
    year: int | None        # 解析出的显式年份
    relative_year: int | None  # "去年"= -1, "前年"= -2
    taxonomy_candidates: list[str]  # 已存在的分类别名
    unit_candidates: list[str]      # 单位名称候选
    person_candidates: list[str]    # 人名候选
    doc_number: str | None          # 文号


class FileSearchQueryParser:
    def __init__(self, *, tokenizer, taxonomy, server_tz="Asia/Shanghai"):
        ...

    def parse(self, query: str) -> ParsedQuery:
        # 1. 去除低信息量请求词（复用 summary_search._QUERY_FILLER_PHRASES）
        cleaned = _remove_filler(query)

        # 2. 提取显式年份和相对年份
        year = _extract_explicit_year(cleaned)
        relative_year = _extract_relative_year(cleaned, server_tz)

        # 3. 提取文号
        doc_number = _extract_doc_number(cleaned)

        # 4. Jieba 分词提取主题词
        terms = tokenizer.tokenize(cleaned)[:64]

        # 5. 与 taxonomy 别名和业务词典匹配
        candidates = _match_taxonomy_aliases(terms, taxonomy)

        # 6. 生成绑定参数结构
        return ParsedQuery(...)
```

注意：`_extract_year` 和 `_extract_relative_year` 使用服务器时区确定性解析，不依赖 LLM。

#### 4.2.2 FileSearchScopeResolver

```python
@dataclass(frozen=True)
class ResolvedSearchScope:
    strict_document_ids: list[str]    # L0 严格范围
    conversation_document_ids: list[str]  # L1 排序加权范围
    include_workspace: bool           # 是否搜索 L4
    scope_mode: str                   # "strict" / "global"


class FileSearchScopeResolver:
    def __init__(self, *, db, user_id, workspace_id,
                 attachment_service, session_file_service):
        ...

    def resolve(self, *, query: str,
                explicit_attachments: list,
                conversation_id: str) -> ResolvedSearchScope:
        # 1. 判断是否是严格范围请求
        if _is_strict_scope_request(query):
            # 只解析 explicit_attachments
            return ResolvedSearchScope(
                strict_document_ids=[...],
                conversation_document_ids=[],
                include_workspace=False,
                scope_mode="strict"
            )

        # 2. 排序范围：L0 + L1 加权 + L4
        l0_ids = _resolve_explicit_attachments(explicit_attachments)
        l1_ids = self.session_file_service.get_session_document_ids(conversation_id)
        return ResolvedSearchScope(
            strict_document_ids=l0_ids,
            conversation_document_ids=l1_ids,
            include_workspace=True,
            scope_mode="global"
        )
```

#### 4.2.3 L1 会话文件范围服务

文件：`apps/api/app/modules/conversations/session_files.py`

```python
class SessionFileTracker:
    """追踪当前会话中已上传、引用或返回过的文件，

    用于 L1 检索范围加权。
    数据存储在内存或轻量缓存中；MVP 阶段通过 ConversationAttachmentContextService
    已解析的附件记录反查。
    """

    def get_session_document_ids(self, conversation_id: str) -> list[str]:
        """读取当前会话所有用户消息的附件 document_ids。"""
        ...

    def record_file_reference(self, conversation_id: str, document_id: str):
        """记录文件在当前会话中被引用。"""
        ...
```

#### 4.2.4 测试（测试先行）

文件：`tests/test_file_search_query_parser.py`

**测试 1**：写测试验证当前不存在 `FileSearchQueryParser` —— 确认功能未实现

**测试 2**：写一个测试描述 query_parser 的预期行为，断言其 fail
- "找我去年活动相关的奖学金材料" → `year=2025`（假设今年 2026）
- "2024年的奖学金材料" → `year=2024`
- "学生工作处资助通知" → units 包含"学生工作处"
- "帮我找一下文件" → cleaned 不包含"帮我找一下"

**测试 3**：验证低信息量请求词被去除

**测试 4**：验证解析失败时返回包含原始关键词的安全结果

文件：`tests/test_file_search_scope_resolver.py`

**测试 5**："这些文件" → strict + L0 only

**测试 6**："找我的奖学金材料" → global + include_workspace

**测试 7**：跨用户隔离：A 的附件不泄露给 B

---

## 任务 4.3：第一阶段数据库召回

**目标**：把文档级召回从 Python 遍历改为 PostgreSQL 索引查询。

### 涉及文件

| 动作 | 文件路径 |
|---|---|
| 新增 | `apps/api/app/modules/retrieval/stage1_document_recall.py` |
| 修改 | `apps/api/app/modules/retrieval/summary_search.py`（主链路迁移后保留兼容） |
| 新增测试 | `apps/api/app/tests/test_stage1_document_recall.py` |

### 实现细节

#### 4.3.1 Stage1DocumentRecallService

```python
class Stage1DocumentRecallService:
    """第一阶段数据库索引召回。

    召回顺序：
    1. normalized_filename 精确匹配（B-tree）
    2. Jieba/GIN search_vector 主召回（setweight: A=文件名, B=分类, C=元数据, D=摘要）
    3. 受限 pg_trgm 补召回（仅当查询 ≥ RETRIEVAL_FILENAME_TRGM_MIN_CHARS）
    """

    def __init__(self, *, db, user_id, workspace_id, config, tokenizer):
        ...

    def recall(self, *, parsed_query: ParsedQuery,
               scope: ResolvedSearchScope,
               config) -> list[dict]:
        """执行第一阶段召回，返回 Top N 候选。"""

        # 如果 PostgreSQL，走索引查询
        if self.db.bind.dialect.name == "postgresql":
            return self._recall_postgresql(parsed_query, scope, config)

        # SQLite 用 deterministic fallback
        return self._recall_deterministic(parsed_query, scope, config)

    def _recall_postgresql(self, parsed_query, scope, config):
        # 1. normalized_filename 精确匹配
        exact_matches = self._exact_filename_match(parsed_query, scope, config)

        # 2. Jieba/GIN search_vector 主召回
        #    使用 websearch_to_tsquery('simple', terms) + @@ 操作符
        gin_matches = self._gin_search(parsed_query, scope, config)

        # 3. 合并去重
        merged = self._merge_candidates(exact_matches, gin_matches)

        # 4. 如果候选不足且查询 ≥ 4 字，尝试 pg_trgm 补召回
        if len(merged) < _MIN_ACCEPTABLE_CANDIDATES and len(parsed_query.cleaned) >= config.trgm_min_chars:
            trgm_matches = self._trgm_fallback(parsed_query, scope, config)
            merged = self._merge_candidates(merged, trgm_matches)

        # 5. 应用候选上限
        merged = merged[:config.retrieval_document_candidate_limit]

        # 6. 一次性批量 JOIN 补齐显示字段
        return self._enrich(merged)

    def _exact_filename_match(self, parsed_query, scope, config):
        """normalized_filename 精确匹配（B-tree 索引）。"""
        normalized = _normalize_text(parsed_query.cleaned)
        if not normalized:
            return []

        query = (
            self.db.query(
                DocumentSearchProfile.working_copy_id,
                DocumentSearchProfile.document_version_id,
                literal(1.0).label("score"),
                literal("exact_filename").label("hit_source"),
            )
            .filter(
                DocumentSearchProfile.user_id == self.user_id,
                DocumentSearchProfile.workspace_id == self.workspace_id,
                DocumentSearchProfile.status == "ACTIVE",
                DocumentSearchProfile.normalized_filename == normalized,
            )
        )
        query = self._apply_scope_filter(query, scope)
        return [row._asdict() for row in query.all()]

    def _gin_search(self, parsed_query, scope, config):
        """Jieba/GIN search_vector 主召回。

        使用 ts_rank_cd 排序，setweight 使文件名匹配权重最高。
        """
        tokens = parsed_query.terms
        if not tokens:
            return []

        ts_query_text = " OR ".join(tokens)
        ts_query = sa.func.websearch_to_tsquery("simple", ts_query_text)

        query = (
            self.db.query(
                DocumentSearchProfile.working_copy_id,
                DocumentSearchProfile.document_version_id,
                sa.func.ts_rank_cd(
                    DocumentSearchProfile.search_vector,
                    ts_query
                ).label("score"),
                literal("gin_search").label("hit_source"),
            )
            .filter(
                DocumentSearchProfile.user_id == self.user_id,
                DocumentSearchProfile.workspace_id == self.workspace_id,
                DocumentSearchProfile.status == "ACTIVE",
                DocumentSearchProfile.search_vector.op("@@")(ts_query),
            )
        )
        query = self._apply_scope_filter(query, scope)
        query = query.order_by(sa.desc("score"))
        query = query.limit(config.retrieval_document_candidate_limit)
        return [row._asdict() for row in query.all()]

    def _trgm_fallback(self, parsed_query, scope, config):
        """受限 pg_trgm 补召回。

        只在以下条件全部满足时启用：
        - 查询长度 >= RETRIEVAL_FILENAME_TRGM_MIN_CHARS
        - 精确匹配和 GIN 召回不足
        - 使用相似度阈值过滤
        """
        if self.db.bind.dialect.name != "postgresql":
            return []

        cleaned = parsed_query.cleaned
        if len(cleaned) < config.retrieval_filename_trgm_min_chars:
            return []

        from sqlalchemy import text
        similarity = sa.func.similarity(
            DocumentSearchProfile.combined_search_text, cleaned
        )
        query = (
            self.db.query(
                DocumentSearchProfile.working_copy_id,
                DocumentSearchProfile.document_version_id,
                similarity.label("score"),
                literal("trgm_fallback").label("hit_source"),
            )
            .filter(
                DocumentSearchProfile.user_id == self.user_id,
                DocumentSearchProfile.workspace_id == self.workspace_id,
                DocumentSearchProfile.status == "ACTIVE",
                similarity >= config.retrieval_filename_trgm_similarity_threshold,
            )
        )
        query = self._apply_scope_filter(query, scope)
        query = query.order_by(sa.desc("score"))
        query = query.limit(config.retrieval_filename_trgm_candidate_limit)
        return [row._asdict() for row in query.all()]

    def _enrich(self, candidates: list[dict]) -> list[dict]:
        """候选收敛后以一次批量 JOIN 补齐显示字段。

        禁止逐文件 N+1 查询。
        """
        if not candidates:
            return []
        wc_ids = [c["working_copy_id"] for c in candidates]

        rows = (
            self.db.query(
                WorkingCopy, Document, DocumentSummary,
                DocumentCategorySuggestion
            )
            .join(Document, Document.id == WorkingCopy.document_id)
            .outerjoin(
                DocumentSummary,
                (DocumentSummary.document_id == WorkingCopy.document_id)
                & (DocumentSummary.document_version_id == WorkingCopy.current_version_id)
            )
            .outerjoin(
                DocumentCategorySuggestion,
                (DocumentCategorySuggestion.document_id == WorkingCopy.document_id)
                & (DocumentCategorySuggestion.document_version_id == WorkingCopy.current_version_id),
            )
            .filter(WorkingCopy.id.in_(wc_ids))
            .all()
        )

        # 构建 enriched 结果
        # ...
        return enriched
```

#### 4.3.2 测试（测试先行）

文件：`tests/test_stage1_document_recall.py`

**测试 1**：写测试验证当前 `WorkingCopySummarySearchService` 的 limit 限制
- 创建 51 个 `WorkingCopy`（ACTIVE），最后一个最旧
- 查询匹配最后一个
- 断言当前搜索找不到它（以此来证明需要基于索引的召回）

**测试 2**：写 `Stage1DocumentRecallService` 的预期行为测试
- 创建 51 个 `WorkingCopy` + `DocumentSearchProfile`
- 查询匹配第 51 个
- 断言 `Stage1DocumentRecallService` 能找到它（因为不受 limit(500) 限制）
- 断言返回结果不超过 `RETRIEVAL_DOCUMENT_CANDIDATE_LIMIT`

**测试 3**：精确文件名匹配测试
- `normalized_filename` 精确匹配优先

**测试 4**：GIN 搜索测试
- Jieba 分词后主题词通过 GIN 召回

**测试 5**：pg_trgm 补召回测试
- 长查询含错字时通过 trigram 补召回
- 短查询不触发 trigram

**测试 6**：跨用户隔离测试
- B 用户的文件不会在 A 的搜索结果中

**测试 7**：候选显示字段来自批量 JOIN 的测试
- 验证 `_enrich` 只发一次 JOIN 查询，不逐文件读取

**测试 8**：SQLite 降级测试
- SQLite 中使用 deterministic token 覆盖逻辑
- 断言不报错

---

## 任务 4.4：原文补召回与候选内精查

**目标**：扩展 Chunk 索引查询支持全局候选补召回，实现 `SearchEvidenceProjector`。

### 涉及文件

| 动作 | 文件路径 |
|---|---|
| 修改 | `apps/api/app/modules/retrieval/chunk_lexical_search.py` |
| 新增 | `apps/api/app/modules/retrieval/evidence_projector.py` |
| 新增测试 | `apps/api/app/tests/test_chunk_fallback_recall.py` |
| 新增测试 | `apps/api/app/tests/test_search_evidence_projector.py` |

### 实现细节

#### 4.4.1 DocumentChunkLexicalSearchService 扩展

在 `chunk_lexical_search.py` 中新增 `fallback_recall` 方法：

```python
def fallback_recall(self, *, query: str, user_id: str,
                    workspace_id: str,
                    max_versions: int = 10) -> list[dict]:
    """全局 Chunk GIN 候选补召回。

    只在第一阶段候选不足时调用。联结当前用户 ACTIVE 工作副本和当前版本，
    按版本聚合，最多补充 max_versions 个版本。
    只返回 document_version_id、最佳 Chunk ID、位置和分数。
    """
    if not query or self.db.bind.dialect.name != "postgresql":
        return []

    tokens = self.tokenizer.tokenize(query)[:64]
    if not tokens:
        return []

    ts_query_text = " OR ".join(tokens)
    ts_query = sa.func.websearch_to_tsquery("simple", ts_query_text)

    # 子查询：当前用户 ACTIVE 工作副本的最新版本
    active_versions = (
        self.db.query(
            WorkingCopy.document_id,
            WorkingCopy.current_version_id
        )
        .filter(
            WorkingCopy.workspace_id == workspace_id,
            WorkingCopy.status == "ACTIVE",
        )
        .subquery()
    )

    # 联结 DocumentChunk + DocumentIndexRun + active_versions
    rows = (
        self.db.query(
            DocumentChunk.document_version_id,
            DocumentChunk.id.label("chunk_id"),
            sa.func.ts_rank_cd(
                DocumentChunk.search_vector, ts_query
            ).label("score"),
        )
        .join(
            DocumentIndexRun,
            DocumentIndexRun.id == DocumentChunk.index_run_id,
        )
        .join(
            active_versions,
            (active_versions.c.current_version_id == DocumentChunk.document_version_id)
            & (active_versions.c.document_id == DocumentChunk.document_id),
        )
        .filter(
            DocumentChunk.search_vector.op("@@")(ts_query),
            DocumentIndexRun.status == "COMPLETED",
            DocumentChunk.embedding_status == "DISABLED",  # 向量关闭时跳过
        )
        .order_by(sa.desc("score"))
        .limit(self._chunk_fallback_limit or 30)
        .all()
    )

    # 按版本聚合，每版本取最佳 Chunk
    version_map = {}
    for row in rows:
        if len(version_map) >= max_versions:
            break
        if row.document_version_id not in version_map:
            version_map[row.document_version_id] = {
                "document_version_id": row.document_version_id,
                "best_chunk_id": row.chunk_id,
                "score": float(row.score),
            }
    return list(version_map.values())
```

第二阶段精查通过现有的 `search` 方法实现，但需新增 Jieba 词项优先的参数：

```python
def search(
    self, *, query: str, document_version_ids: list[str],
    limit: int = 24,  # 全局最多 24 个 Chunk
    per_document_limit: int = 3,  # 每版本最多 3 个
    min_trgm_chars: int = 4,
    ...
) -> list[dict]:
    # 现有逻辑，但增加 per_document_limit 控制
    # ...
```

#### 4.4.2 SearchEvidenceProjector

```python
class SearchEvidenceProjector:
    """按 Chunk ID 读取已持久化 Evidence 并再次校验权限。"""

    def __init__(self, *, db, user_id):
        ...

    def project(self, *, chunk_ids: list[str],
                max_preview_chars: int = 240) -> dict[str, dict]:
        """读取 EvidenceSpan，返回 {chunk_id: {page_number, sheet_name, cell_range, preview}}。

        不返回完整正文，只返回受限预览。
        """
        if not chunk_ids:
            return {}

        rows = (
            self.db.query(EvidenceSpan)
            .filter(
                EvidenceSpan.chunk_id.in_(chunk_ids),
                # 再次校验用户权限
                EvidenceSpan.document_id.in_(
                    self.db.query(Document.id).filter(
                        Document.user_id == self.user_id
                    )
                ),
            )
            .order_by(EvidenceSpan.chunk_id, EvidenceSpan.span_index)
            .all()
        )

        result = {}
        for row in rows:
            if row.chunk_id not in result:
                result[row.chunk_id] = {
                    "page_number": row.page_number,
                    "sheet_name": row.sheet_name,
                    "cell_range": row.cell_range,
                    "preview": row.quote[:max_preview_chars],
                }
        return result
```

#### 4.4.3 降级处理

- Chunk 查询超时：使用 `RETRIEVAL_STATEMENT_TIMEOUT_MS` 设置 `statement_timeout`
- 捕获 timeout 异常，返回第一阶段结果并标记 `partial=True`
- 候选版本未完成索引（`DocumentIndexRun.status != COMPLETED`）：排除该版本

#### 4.4.4 测试（测试先行）

**测试 1**：写测试验证当前 Chunk 搜索无法跨版本补召回
- 创建文档 A（摘要不包含 query，正文包含 query）
- 创建文档 B（摘要包含 query）
- 当前 `hybrid-search` 只召回 B
- 断言 A 不在结果中

**测试 2**：写 `fallback_recall` 预期行为测试
- 创建 5 个 ACTIVE 工作副本，其中 2 个正文包含 query 但摘要不包含
- 调用 `fallback_recall`，断言正确召回 2 个

**测试 3**：`evidence_projector` 预期行为测试

**测试 4**：embedding 关闭时仍可正常搜索的测试

**测试 5**：Chunk 超时降级测试

---

## 任务 4.5：确定性融合和服务统一

**目标**：实现 `TwoStageFileSearchService` 作为唯一编排入口。

### 涉及文件

| 动作 | 文件路径 |
|---|---|
| 新增 | `apps/api/app/modules/retrieval/two_stage_search.py` |
| 新增测试 | `apps/api/app/tests/test_two_stage_file_search.py` |

### 实现细节

#### 4.5.1 TwoStageFileSearchService

```python
class TwoStageFileSearchService:
    """两阶段文件检索唯一编排入口。

    组合文档级索引召回、原文补召回和候选 Chunk 精查，
    输出确定性融合排序结果。
    """

    # 版本化权重常量，可测试覆盖
    WEIGHT_DOCUMENT = 0.40
    WEIGHT_CHUNK = 0.35
    WEIGHT_SCOPE = 0.20
    WEIGHT_TIME = 0.05
    WEIGHT_EXACT_FILENAME_BOOST = 0.15  # 额外加权
    WEIGHT_EXACT_YEAR_BOOST = 0.10      # 额外加权

    def __init__(self, *, db, user_id, workspace_id, config,
                 tokenizer, summary_service=None):
        self.db = db
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.config = config
        self.stage1 = Stage1DocumentRecallService(
            db=db, user_id=user_id, workspace_id=workspace_id,
            config=config, tokenizer=tokenizer
        )
        self.stage2 = DocumentChunkLexicalSearchService(
            db=db, user_id=user_id, tokenizer=tokenizer
        )
        self.evidence = SearchEvidenceProjector(
            db=db, user_id=user_id
        )

    def search(self, *, query: str, scope: ResolvedSearchScope,
               parsed_query: ParsedQuery | None = None) -> dict:
        """执行两阶段检索，返回融合排序结果。"""

        # 一阶段：文档级索引召回
        stage1_results = self.stage1.recall(
            parsed_query=parsed_query or self._parse(query),
            scope=scope,
            config=self.config,
        )

        # 是否需要补召回？
        needs_fallback = self._needs_fallback(stage1_results, parsed_query)
        if needs_fallback:
            fallback = self.stage2.fallback_recall(
                query=query,
                user_id=self.user_id,
                workspace_id=self.workspace_id,
            )
            stage1_results = self._merge_fallback(stage1_results, fallback)

        # 二阶段：候选版本内 Chunk 精查
        version_ids = [r["document_version_id"] for r in stage1_results
                       if r.get("document_version_id")]
        version_ids = version_ids[:self.config.retrieval_document_detail_limit]

        chunk_results = []
        evidence_map = {}
        if version_ids:
            chunk_results = self.stage2.search(
                query=query,
                document_version_ids=version_ids,
                limit=self.config.retrieval_chunk_global_limit,
                per_document_limit=self.config.retrieval_chunk_limit_per_document,
            )
            chunk_ids = [c["chunk_id"] for c in chunk_results]
            evidence_map = self.evidence.project(
                chunk_ids=chunk_ids,
                max_preview_chars=self.config.retrieval_preview_max_chars,
            )

        # 确定性融合排序
        fused = self._fuse_and_rank(
            stage1_results=stage1_results,
            chunk_results=chunk_results,
            evidence_map=evidence_map,
            scope=scope,
            parsed_query=parsed_query,
        )

        return {
            "ok": True,
            "query": query,
            "total_returned": len(fused),
            "partial": self._check_partial(stage1_results, chunk_results),
            "results": fused,
            "user_message": self._build_user_message(fused),
        }

    def _fuse_and_rank(self, *, stage1_results, chunk_results,
                       evidence_map, scope, parsed_query):
        """确定性融合排序。

        1. 对每种信号的分值归一化到 [0, 1]
        2. 按权重融合
        3. 精确文件名、明确年份获得固定加权
        4. 同等相关时 L0 > L1 > L4
        """
        # 构建文档级得分映射
        doc_scores = {}
        for r in stage1_results:
            wc_id = r.get("working_copy_id")
            doc_scores[wc_id] = {
                "doc_score": r.get("score", 0.0),
                "hit_source": r.get("hit_source", ""),
                "working_copy_id": wc_id,
                "document_id": r.get("document_id"),
                "document_version_id": r.get("document_version_id"),
                "filename": r.get("filename", ""),
                "category_path": r.get("category_path", []),
                "overview": r.get("overview", ""),
                "year": r.get("year"),
            }

        # 构建 Chunk 得分映射（每文档取最佳 Chunk 分）
        chunk_scores_by_wc = {}
        for c in chunk_results:
            # 通过 document_version_id 关联到 working_copy_id
            wc_id = self._version_to_wc_map.get(c["document_version_id"])
            if wc_id:
                current = chunk_scores_by_wc.get(wc_id, 0.0)
                chunk_scores_by_wc[wc_id] = max(current, c.get("score", 0.0))
                # 同时记录 evidence
                if c["chunk_id"] in evidence_map and wc_id in doc_scores:
                    ev = evidence_map[c["chunk_id"]]
                    doc_scores[wc_id]["match_location"] = {
                        "page_number": ev.get("page_number"),
                        "sheet_name": ev.get("sheet_name"),
                        "cell_range": ev.get("cell_range"),
                    }
                    doc_scores[wc_id]["evidence_preview"] = ev.get("preview", "")

        # 归一化和融合
        doc_values = [s["doc_score"] for s in doc_scores.values()]
        chunk_values = list(chunk_scores_by_wc.values())
        doc_max = max(doc_values) if doc_values else 1.0
        chunk_max = max(chunk_values) if chunk_values else 1.0

        results = []
        for wc_id, scores in doc_scores.items():
            normalized_doc = scores["doc_score"] / doc_max if doc_max > 0 else 0.0
            normalized_chunk = (chunk_scores_by_wc.get(wc_id, 0.0)
                                / chunk_max if chunk_max > 0 else 0.0)

            # 范围优先级加权
            scope_weight = self._scope_weight(wc_id, scope)

            # 时间并列项
            time_weight = self._time_weight(wc_id)

            # 精确文件名/年份额外加权
            boost = 0.0
            if parsed_query and parsed_query.year:
                if scores.get("year") == parsed_query.year:
                    boost += self.WEIGHT_EXACT_YEAR_BOOST

            final_score = (
                normalized_doc * self.WEIGHT_DOCUMENT
                + normalized_chunk * self.WEIGHT_CHUNK
                + scope_weight * self.WEIGHT_SCOPE
                + time_weight * self.WEIGHT_TIME
                + boost
            )

            # 构建 match_reasons
            match_reasons = self._build_match_reasons(
                scores, chunk_scores_by_wc.get(wc_id), evidence_map, parsed_query
            )

            results.append({
                "working_copy_id": scores["working_copy_id"],
                "document_id": scores["document_id"],
                "document_version_id": scores["document_version_id"],
                "filename": scores["filename"],
                "category_path": scores["category_path"],
                "overview": scores.get("overview", "")[:500],
                "match_reasons": match_reasons,
                "match_location": scores.get("match_location"),
                "evidence_preview": scores.get("evidence_preview", ""),
                "_score": round(final_score, 6),  # 仅用于排序，不返回给用户
            })

        # 排序：按分倒序，并列时用 stable working_copy_id
        results.sort(key=lambda r: (-r["_score"], r["working_copy_id"]))
        for r in results:
            del r["_score"]

        return results

    def _needs_fallback(self, stage1_results, parsed_query):
        """判断是否需要补召回。

        条件（任一）：
        - 候选数 < 5
        - 最高分 < 0.5
        - 最长业务词未命中
        """
        if not stage1_results:
            return True
        if len(stage1_results) < 5:
            return True
        # TODO: 检查最长业务词是否命中
        return False
```

#### 4.5.2 测试（测试先行）

文件：`tests/test_two_stage_file_search.py`

**测试 1**：全文搜索端到端测试
- 创建多个文档（不同文件名、分类、年份、摘要、正文内容）
- 构造完整的两阶段搜索流程
- 断言结果排序稳定

**测试 2**：正文强命中 > 弱摘要命中
- 文档 A：摘要包含 query，正文无
- 文档 B：摘要不包含 query，正文包含
- 断言 B 排在 A 前面

**测试 3**：L0 > L1 > L4 排序
- scope 包含 L0/L1/L4 文件
- 断言排序：L0 > L1 > L4

**测试 4**：精确文件名加权
- 完全匹配文件名的文件排名更高

**测试 5**：不同查询返回相同数据的排序稳定
- 执行两次相同查询，结果顺序一致

**测试 6**：无 N+1 查询（使用 pytest 的 assert_num_queries 或者 SQLAlchemy 事件计数）

**测试 7**：embedding 关闭时向量权重分配给 Chunk

---

## 任务 4.6：Agent、API 和用户投影

**目标**：接入 Agent Runtime，扩展 UserTaskReceipt，补齐 API。

### 涉及文件

| 动作 | 文件路径 |
|---|---|
| 修改 | `apps/api/app/modules/agent/tool_registry.py` |
| 修改 | `apps/api/app/modules/agent/graph.py` |
| 修改 | `apps/api/app/modules/agent/user_receipt.py` |
| 新增 | `apps/api/app/modules/retrieval/router.py`（或直接在 `agent/router.py` 中） |
| 新增测试 | `apps/api/app/tests/test_agent_search_integration.py` |
| 新增/修改 | `apps/api/app/routers/` —— `POST /api/search` |

### 实现细节

#### 4.6.1 hybrid-search handler 改造

在 `tool_registry.py` 中修改 `_search_handler`：

```python
def _search_handler(db, user_id, agent_context=None):
    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        if db is None or user_id is None:
            return {"kind": "workspace_file_search", "ok": False, ...}

        query = getattr(tool_input, "query", "")
        document_ids = list(getattr(tool_input, "document_ids", []))

        config = get_settings()

        # 旧链路：始终保留，仅在运维显式关闭两阶段检索时使用。
        if not config.two_stage_retrieval_enabled:
            return WorkingCopySummarySearchService(db=db, user_id=user_id).search(
                query=query, document_ids=document_ids,
            )

        # 新链路：默认两阶段检索。
        tokenizer = ChineseLexicalTokenizer(load_default_business_terms())
        scope_resolver = FileSearchScopeResolver(
            db=db, user_id=user_id,
            workspace_id=agent_context.workspace_id,
            ...
        )
        scope = scope_resolver.resolve(
            query=query,
            explicit_attachments=[...],
            conversation_id=agent_context.conversation_id,
        )
        parser = FileSearchQueryParser(
            tokenizer=tokenizer,
            taxonomy=load_taxonomy(),
        )
        parsed = parser.parse(query)
        service = TwoStageFileSearchService(
            db=db, user_id=user_id,
            workspace_id=agent_context.workspace_id,
            config=config,
            tokenizer=tokenizer,
        )
        result = service.search(
            query=query, scope=scope, parsed_query=parsed
        )
        result["kind"] = "workspace_file_search"
        return result
    return handler
```

#### 4.6.2 graph.py 扩展

修改 `_build_workspace_file_search_response`，支持新的搜索结果格式：

```python
def _build_workspace_file_search_response(payload: dict) -> str:
    if not payload.get("ok"):
        return f"搜索服务不可用：{payload.get('error', {}).get('message', '未知错误')}"

    results = payload.get("results", [])
    if not results:
        user_message = payload.get("user_message", "")
        return user_message or "未找到相关文件。请尝试补充主题、年份、单位或文档类型。"

    lines = [f"找到 {payload.get('total_returned', len(results))} 个相关文件："]
    for idx, r in enumerate(results, 1):
        filename = r.get("filename", "未命名")
        categories = "/".join(r.get("category_path", [])) if r.get("category_path") else ""
        cat_str = f" [{categories}]" if categories else ""
        overview = r.get("overview", "")[:200]
        reasons = "；".join(r.get("match_reasons", []))
        location = ""
        if r.get("match_location"):
            loc = r["match_location"]
            if loc.get("page_number"):
                location = f" - 第 {loc['page_number']} 页"
            elif loc.get("sheet_name"):
                location = f" - {loc['sheet_name']}"
                if loc.get("cell_range"):
                    location += f" {loc['cell_range']}"
        lines.append(
            f"{idx}. {filename}{cat_str}\n"
            f"   概览：{overview}\n"
            f"   原因：{reasons}{location}"
        )

    partial = payload.get("partial")
    if partial:
        lines.append("\n⚠️ 部分文件原文索引暂不可用，结果仅基于文件名和摘要。")

    return "\n".join(lines)
```

#### 4.6.3 UserTaskReceipt 扩展

```python
class UserTaskReceipt(BaseModel):
    task_id: str
    task_status: Literal["processing", "waiting_confirmation", "completed", "needs_attention", "failed"]
    response_type: Literal["text", "file_results", "managed_file_list", "rename_plan",
                           "operation_plan", "async_job", "file_search_results"]
    # ... existing fields ...

    # 新增：文件搜索结果
    file_search_result: FileSearchResult | None = None


class FileSearchResult(BaseModel):
    """文件搜索结果的普通用户投影。

    不得包含 Skill、Tool、AgentRun、内部队列、原文件路径、上传原文件名、
    SQL 分数、search_text、embedding 或完整正文。
    """
    query: str
    total_returned: int
    partial: bool = False
    user_message: str = ""
    files: list[SearchResultFile] = Field(default_factory=list)


class SearchResultFile(BaseModel):
    """单个文件的搜索结果显示。

    每个文件只允许包含稳定业务 ID、文件名、分类、概览、推荐原因和位置。
    """
    working_copy_id: str
    document_id: str
    document_version_id: str
    filename: str
    category_path: list[str] = Field(default_factory=list)
    year: int | None = None
    overview: str = ""
    match_reasons: list[str] = Field(default_factory=list)
    match_location: dict[str, Any] | None = None
    evidence_preview: str = ""
```

#### 4.6.4 API

```python
@router.post("/api/search", response_model=SearchAPIResponse)
def search_files(
    query: str = Body(...),
    conversation_id: str | None = Body(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """搜索当前用户工作区中的文件。

    普通用户兼容接口，与聊天入口使用相同的检索服务和权限校验。
    不接受宿主机路径、任意用户 ID 或未校验的 DocumentVersion ID。
    """
    scope = scope_resolver.resolve(query=query, ...)
    parsed = query_parser.parse(query)
    service = TwoStageFileSearchService(...)
    result = service.search(query=query, scope=scope, parsed_query=parsed)
    return SearchAPIResponse(
        ok=result["ok"],
        query=result["query"],
        total_returned=result["total_returned"],
        results=result["results"],
    )
```

#### 4.6.5 测试（测试先行）

**测试 1**：写测试确认当前 `hybrid-search` handler 不调用 `TwoStageFileSearchService`

**测试 2**：写预期行为测试：Agent 发 SEARCH_FILES 意图 → `TwoStageFileSearchService` 被调用

**测试 3**：`UserTaskReceipt` 返回 `response_type=file_search_results`

**测试 4**：`POST /api/search` 与聊天搜索结果一致

**测试 5**：搜索接口拒绝未校验的 `document_version_id`

---

## 任务 4.7：前端文件搜索结果卡

**目标**：聊天页展示逐文件搜索结果卡。

### 涉及文件

| 动作 | 文件路径 |
|---|---|
| 修改 | `apps/web/src/types.ts` |
| 修改 | `apps/web/src/features/chat/AgentRunReceipt.tsx` |
| 新增 | `apps/web/src/features/chat/SearchResultCard.tsx` |
| 新增 | `apps/web/src/features/chat/FileSearchResultsReceipt.tsx` |

### 实现细节

#### 4.7.1 TypeScript 类型

```typescript
// types.ts 新增
export interface SearchResultFile {
  working_copy_id: string;
  document_id: string;
  document_version_id: string;
  filename: string;
  category_path: string[];
  year: number | null;
  overview: string;
  match_reasons: string[];
  match_location: {
    page_number?: number;
    sheet_name?: string;
    cell_range?: string;
  } | null;
  evidence_preview: string;
}

export interface FileSearchResult {
  query: string;
  total_returned: number;
  partial: boolean;
  user_message: string;
  files: SearchResultFile[];
}

// TaskResult 新增 file_search_result 字段
export interface TaskResult {
  // ... existing fields ...
  file_search_result?: FileSearchResult;
}
```

#### 4.7.2 SearchResultCard 组件

```tsx
interface Props {
  file: SearchResultFile;
  onOpenDetail: (documentId: string) => void;
  onDownload: (documentId: string) => void;
}

export const SearchResultCard: React.FC<Props> = ({ file, onOpenDetail, onDownload }) => {
  const categoryLabel = file.category_path?.length > 0
    ? file.category_path.join(" / ")
    : "未分类";
  const yearLabel = file.year ? `（${file.year}）` : "";

  return (
    <div className="search-result-card">
      <div className="search-result-header">
        <span className="search-result-filename">
          {file.filename}{yearLabel}
        </span>
        <span className="search-result-category">{categoryLabel}</span>
      </div>
      {file.overview && (
        <p className="search-result-overview">{file.overview}</p>
      )}
      {file.match_reasons.length > 0 && (
        <ul className="search-result-reasons">
          {file.match_reasons.map((reason, i) => (
            <li key={i}>{reason}</li>
          ))}
        </ul>
      )}
      {file.match_location && (
        <div className="search-result-location">
          {file.match_location.page_number && `第 ${file.match_location.page_number} 页`}
          {file.match_location.sheet_name && file.match_location.sheet_name}
          {file.match_location.cell_range && ` ${file.match_location.cell_range}`}
        </div>
      )}
      {file.evidence_preview && (
        <blockquote className="search-result-preview">
          {file.evidence_preview}
        </blockquote>
      )}
      <div className="search-result-actions">
        <button onClick={() => onOpenDetail(file.document_id)}>
          查看详情
        </button>
        <button onClick={() => onDownload(file.document_id)}>
          下载
        </button>
      </div>
    </div>
  );
};
```

#### 4.7.3 AgentRunReceipt 扩展

在 `AgentRunReceipt.tsx` 中新增分支：

```tsx
if (taskResult.response_type === "file_search_results" && taskResult.file_search_result) {
  return (
    <FileSearchResultsReceipt
      result={taskResult.file_search_result}
      onOpenDetail={handleOpenDetail}
      onDownload={handleDownload}
    />
  );
}
```

#### 4.7.4 测试（测试先行）

- 测试组件渲染：空结果、有结果、部分降级状态
- 测试查看更多分页
- 测试构建不报错

---

## 任务 4.8：文档、回归和真实烟测

**目标**：更新文档，执行全量测试，完成手工烟测。

### 涉及文件

| 动作 | 文件路径 |
|---|---|
| 修改 | `README.md` |
| 修改 | `docs/runbook.md` |
| 修改 | `docs/api-contract.md` |
| 修改 | `docs/database-schema.md` |
| 修改 | `apps/api/.env.example` |

### 步骤

1. 更新 `README.md`：新增阶段四功能说明（"自然语言搜索整理后的文件"）
2. 更新 `docs/runbook.md`：新增阶段四配置项说明和 tuning 建议
3. 更新 `docs/api-contract.md`：新增 `POST /api/search` 接口文档和 `file_search_results` 响应
4. 更新 `docs/database-schema.md`：新增 `document_search_profiles` 表
5. 更新 `apps/api/.env.example`：新增阶段四配置项
6. 执行 Alembic upgrade/downgrade/upgrade 验证
7. 执行后端全量测试和前端 build
8. 按第 14 节完成手工烟测

### 自动化测试矩阵（26 项）

详见 `docs/stage-4-low-resource-two-stage-retrieval-plan.md` 第 13 节的完整测试矩阵。

### 手工烟测

详见 `docs/stage-4-low-resource-two-stage-retrieval-plan.md` 第 14 节的手工烟测清单。

---

## 提交顺序

```
1. test: confirm stage-4 baseline gaps in current search   (任务 4.0)
2. feat: add rebuildable document search profiles           (任务 4.1)
3. feat: add controlled file search scope and query parsing (任务 4.2)
4. feat: implement indexed document candidate retrieval     (任务 4.3)
5. feat: add bounded chunk fallback and deterministic fusion (任务 4.4 + 4.5)
6. feat: expose safe conversational file search receipts    (任务 4.6)
7. feat: add chat file search result cards                  (任务 4.7)
8. test: cover low-resource two-stage retrieval end to end  (任务 4.8)
9. docs: document stage-four deployment and smoke testing   (任务 4.8)
```

每次提交前运行对应局部测试；最终提交前运行 `pytest -v`、`npm run build` 和 PostgreSQL migration 验证。

---

## 不做的内容（延后到阶段五）

- LLM 事实回答和正式引用持久化
- embedding/向量召回上线
- Neo4j/GraphRAG 参与检索排序
- 自动查询改写或用户画像
- 根据搜索自动移动/重命名/删除
- 让用户选择检索引擎或模型
