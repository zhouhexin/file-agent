"""批量导入持久化仓库。

仓库只执行当前用户范围内的确定性查询和写入；权限判断、状态转换和幂等冲突语义由 Service 负责，
路由与未来 MCP 适配器不得直接操作 ORM。
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    IngestBatch,
    IngestDuplicateGroup,
    IngestDuplicateGroupMember,
    IngestItem,
    IntegrationRequest,
)


class IngestionRepository:
    """封装批次、条目和外部请求幂等记录的数据库访问。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话，禁止跨请求复用。"""

        self.db = db

    def get_batch(self, *, batch_id: str) -> IngestBatch | None:
        """按主键读取批次；调用方必须继续验证用户归属。"""

        return self.db.get(IngestBatch, batch_id)

    def get_batch_for_update(self, *, batch_id: str) -> IngestBatch | None:
        """锁定批次清单行，防止追加成员与 seal 在 PostgreSQL 中交错提交。"""

        return (
            self.db.query(IngestBatch)
            .filter(IngestBatch.id == batch_id)
            .with_for_update()
            .one_or_none()
        )

    def find_batch_by_idempotency(
        self,
        *,
        user_id: str,
        client_id: str,
        idempotency_key: str,
    ) -> IngestBatch | None:
        """查找同一用户和客户端已经创建的幂等批次。"""

        return (
            self.db.query(IngestBatch)
            .filter(
                IngestBatch.user_id == user_id,
                IngestBatch.client_id == client_id,
                IngestBatch.idempotency_key == idempotency_key,
            )
            .one_or_none()
        )

    def create_batch(self, **values: object) -> IngestBatch:
        """创建尚未 seal 的批次，但不在仓库层提交事务。"""

        batch = IngestBatch(**values)
        self.db.add(batch)
        self.db.flush()
        return batch

    def create_integration_request(self, **values: object) -> IntegrationRequest:
        """记录外部写请求的载荷摘要和幂等结果。"""

        integration_request = IntegrationRequest(**values)
        self.db.add(integration_request)
        self.db.flush()
        return integration_request

    def find_integration_request(
        self,
        *,
        user_id: str,
        client_id: str,
        idempotency_key: str,
    ) -> IntegrationRequest | None:
        """查找外部决定或重试请求的持久化幂等记录。"""

        return (
            self.db.query(IntegrationRequest)
            .filter(
                IntegrationRequest.user_id == user_id,
                IntegrationRequest.client_id == client_id,
                IntegrationRequest.idempotency_key == idempotency_key,
            )
            .one_or_none()
        )

    def get_items_by_client_ids(
        self,
        *,
        batch_id: str,
        client_item_ids: Iterable[str],
    ) -> dict[str, IngestItem]:
        """批量读取已登记项，供追加清单请求进行逐项幂等比较。"""

        item_ids = list(client_item_ids)
        if not item_ids:
            return {}
        items = (
            self.db.query(IngestItem)
            .filter(
                IngestItem.batch_id == batch_id,
                IngestItem.client_item_id.in_(item_ids),
            )
            .all()
        )
        return {item.client_item_id: item for item in items}

    def get_item(self, *, item_id: str) -> IngestItem | None:
        """读取单个导入项，用于校验分页游标属于当前批次。"""

        return self.db.get(IngestItem, item_id)

    def get_item_for_update(self, *, item_id: str) -> IngestItem | None:
        """锁定单个导入项，防止同一文件的并发重传创建多份暂存 Document。"""

        return (
            self.db.query(IngestItem)
            .filter(IngestItem.id == item_id)
            .with_for_update()
            .one_or_none()
        )

    def create_item(self, **values: object) -> IngestItem:
        """创建一个固定清单项，不启动文件接收或后台任务。"""

        item = IngestItem(**values)
        self.db.add(item)
        self.db.flush()
        return item

    def list_items(
        self,
        *,
        batch_id: str,
        cursor: str | None,
        limit: int,
    ) -> list[IngestItem]:
        """按稳定外部 upload_id 分页，额外取一项用于判断下一页。"""

        query = self.db.query(IngestItem).filter(IngestItem.batch_id == batch_id)
        if cursor:
            query = query.filter(IngestItem.id > cursor)
        return query.order_by(IngestItem.id.asc()).limit(limit + 1).all()

    def list_receivable_item_ids(self, *, batch_id: str) -> list[str]:
        """列出仍处于首次接收阶段的 PENDING 条目，不包含业务失败项。"""

        rows = (
            self.db.query(IngestItem.id)
            .filter(
                IngestItem.batch_id == batch_id,
                IngestItem.stage == "RECEIVE",
                IngestItem.status == "PENDING",
                IngestItem.upload_document_version_id.is_(None),
            )
            .order_by(IngestItem.id.asc())
            .all()
        )
        return [str(row[0]) for row in rows]

    def get_active_duplicate_group(
        self,
        *,
        batch_id: str,
        user_id: str,
        workspace_id: str,
        content_sha256: str,
    ) -> IngestDuplicateGroup | None:
        """锁定同一批次和完整哈希的活动组，用户与工作区作为额外防串线条件。"""

        return (
            self.db.query(IngestDuplicateGroup)
            .filter(
                IngestDuplicateGroup.user_id == user_id,
                IngestDuplicateGroup.batch_id == batch_id,
                IngestDuplicateGroup.workspace_id == workspace_id,
                IngestDuplicateGroup.content_sha256 == content_sha256,
                IngestDuplicateGroup.status == "ACTIVE",
            )
            .with_for_update()
            .one_or_none()
        )

    def create_duplicate_group(self, **values: object) -> IngestDuplicateGroup:
        """创建以首个上传条目为主任务的内容重复组。"""

        group = IngestDuplicateGroup(**values)
        self.db.add(group)
        self.db.flush()
        return group

    def create_duplicate_group_member(self, **values: object) -> IngestDuplicateGroupMember:
        """登记批内重复组成员及其等待依赖。"""

        member = IngestDuplicateGroupMember(**values)
        self.db.add(member)
        self.db.flush()
        return member

    def count_items_by_status(self, *, batch_id: str) -> dict[str, int]:
        """在数据库中聚合条目状态，避免读取整批正文或结果 JSON。"""

        rows = (
            self.db.query(IngestItem.status, func.count(IngestItem.id))
            .filter(IngestItem.batch_id == batch_id)
            .group_by(IngestItem.status)
            .all()
        )
        return {str(status): int(count) for status, count in rows}

    def count_items(self, *, batch_id: str) -> int:
        """返回批次固定清单项数量。"""

        return int(
            self.db.query(func.count(IngestItem.id))
            .filter(IngestItem.batch_id == batch_id)
            .scalar()
            or 0
        )

    def sum_batch_expected_bytes(self, *, batch_id: str) -> int:
        """返回批次已经登记的来源快照总字节数。"""

        return int(
            self.db.query(func.sum(IngestItem.expected_size))
            .filter(IngestItem.batch_id == batch_id)
            .scalar()
            or 0
        )

    def user_committed_and_reserved_bytes(self, *, user_id: str) -> int:
        """统计用户已落盘 Document 与尚未落盘的活动批次预留容量。

        已接收条目已有对应 Document，因此从预留量排除，避免同一字节重复计算。
        """

        committed = int(
            self.db.query(func.sum(Document.size_bytes))
            .filter(
                Document.user_id == user_id,
                # 已取消或被已有文件替换的上传暂存字节会异步删除，不能继续占用用户配额。
                Document.status.notin_(["UPLOAD_CANCELLED", "UPLOAD_REPLACED_BY_EXISTING"]),
            )
            .scalar()
            or 0
        )
        reserved = int(
            self.db.query(func.sum(IngestItem.expected_size))
            .join(IngestBatch, IngestBatch.id == IngestItem.batch_id)
            .filter(
                IngestBatch.user_id == user_id,
                IngestBatch.status.notin_(["CANCELLED", "EXPIRED"]),
                IngestItem.upload_document_version_id.is_(None),
                IngestItem.status.notin_(["CANCELLED", "EXPIRED", "SKIPPED"]),
            )
            .scalar()
            or 0
        )
        return committed + reserved
