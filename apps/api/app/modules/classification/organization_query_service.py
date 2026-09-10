"""活动工作副本的主分类树与首次组织复核只读查询。"""

from __future__ import annotations

from collections import defaultdict
from math import ceil

from sqlalchemy.orm import Session

from app.db.models import (
    ClassificationPlacementOperation,
    DocumentCategory,
    DocumentOrganizationDecision,
    WorkingCopy,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.image_date_policy import (
    IMAGE_DATE_CATEGORY_ROOT_ID,
    IMAGE_DATE_RELATION_SOURCES,
    image_date_from_category_path,
    image_date_virtual_node_id,
    parse_image_date_virtual_node_id,
)
from app.modules.classification.organization_schemas import (
    OrganizationFileItemResponse,
    OrganizationFilePageResponse,
    OrganizationPrimaryResponse,
    OrganizationTreeNodeResponse,
    OrganizationTreeResponse,
)
from app.modules.classification.schemas import CategoryNode
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id


ACTIVE_PRIMARY_STATUSES = ("AUTO_APPLIED", "CONFIRMED")
# 旧客户端可继续传该 ID，但公开 schema v2 不再生成或展示复核节点。
NEEDS_REVIEW_NODE_ID = "__needs_review__"
OTHER_CATEGORY_ID = "system.other"
_ACTIVE_PLACEMENT_STATES = ("PREPARED", "EXECUTING", "FS_APPLIED", "RECONCILING")


class OrganizationQueryError(ValueError):
    """分类目录查询参数无法由当前 taxonomy 唯一解析。"""


class ClassificationOrganizationQueryService:
    """聚合活动文件的有效主分类、待执行主分类和兼容 OTHER 视图。"""

    def __init__(self, db: Session) -> None:
        """保存只读数据库会话并加载当前 taxonomy。"""

        self.db = db
        self.taxonomy = load_default_taxonomy()
        self.workspace_id = get_shared_workspace_id(db)
        self.nodes_by_id: dict[str, CategoryNode] = {}
        self.paths_by_id: dict[str, list[str]] = {}
        self.fallback_category_ids: set[str] = {OTHER_CATEGORY_ID}
        for root in self.taxonomy.categories:
            self._index_node(root, [])
        if self.taxonomy.fallback_policy is not None:
            self.fallback_category_ids.update(
                str(category_id)
                for category_id in self.taxonomy.fallback_policy.historical_category_ids
            )

    def tree(self) -> OrganizationTreeResponse:
        """返回活动主分类树，父节点计数按后代文件去重汇总。"""

        active_copies = self._active_copy_query().all()
        active_ids = {item.id for item in active_copies}
        relation_by_copy, other_ids, _legacy_ids = self._current_primary_state(active_ids)
        direct_ids: dict[str, set[str]] = defaultdict(set)
        image_date_ids: dict[str, set[str]] = defaultdict(set)
        for working_copy_id, relation in relation_by_copy.items():
            if self._is_other_category(relation.category_id):
                continue
            image_date = (
                image_date_from_category_path(relation.category_path_json)
                if relation.source in IMAGE_DATE_RELATION_SOURCES
                and relation.category_id == IMAGE_DATE_CATEGORY_ROOT_ID
                else None
            )
            if image_date:
                image_date_ids[image_date].add(working_copy_id)
            else:
                direct_ids[relation.category_id].add(working_copy_id)

        # 新 OTHER 和历史 fallback/无可靠主类都只在该节点聚合展示；历史主类
        # ID 仍保留在文件项 effective_primary 中，不能伪造为 system.other。
        direct_ids[OTHER_CATEGORY_ID].update(other_ids)
        classified_ids = set(relation_by_copy)
        business_ids = {
            working_copy_id
            for working_copy_id, relation in relation_by_copy.items()
            if not self._is_other_category(relation.category_id)
        }

        def build(node: CategoryNode, parents: list[str]) -> tuple[OrganizationTreeNodeResponse, set[str]]:
            """递归构造节点，同时用集合避免父级重复计数。"""

            path = [*parents, node.name]
            child_responses: list[OrganizationTreeNodeResponse] = []
            subtree_ids = set(direct_ids.get(node.id or "", set()))
            for child in node.children:
                if child.visible is False:
                    continue
                child_response, child_ids = build(child, path)
                child_responses.append(child_response)
                subtree_ids.update(child_ids)
            if node.id == IMAGE_DATE_CATEGORY_ROOT_ID:
                # 年份或兼容日期是上传组织维度，不写入静态 taxonomy；树接口按
                # 正式关系动态投影虚拟节点，并把最近时间放在前面便于浏览。
                for date_label in sorted(image_date_ids):
                    date_ids = set(image_date_ids[date_label])
                    child_responses.insert(
                        0,
                        OrganizationTreeNodeResponse(
                            category_id=image_date_virtual_node_id(date_label),
                            name=date_label,
                            category_path=[*path, date_label],
                            direct_file_count=len(date_ids),
                            subtree_file_count=len(date_ids),
                            is_virtual=True,
                        ),
                    )
                    subtree_ids.update(date_ids)
            return (
                OrganizationTreeNodeResponse(
                    category_id=node.id or "/".join(path),
                    name=node.name,
                    category_path=path,
                    direct_file_count=len(direct_ids.get(node.id or "", set())),
                    subtree_file_count=len(subtree_ids),
                    children=child_responses,
                ),
                subtree_ids,
            )

        nodes = [
            build(root, [])[0]
            for root in self.taxonomy.categories
            if root.visible is not False
        ]
        return OrganizationTreeResponse(
            schema_version=2,
            taxonomy_key=self.taxonomy.key,
            taxonomy_version=self.taxonomy.version,
            total_active_files=len(active_ids),
            classified_file_count=len(classified_ids),
            business_classified_file_count=len(business_ids),
            other_file_count=len(other_ids),
            nodes=nodes,
        )

    def files(
        self,
        *,
        category_id: str | None,
        scope: str,
        review_only: bool,
        page: int,
        page_size: int,
    ) -> OrganizationFilePageResponse:
        """按主分类返回稳定服务端分页结果；旧复核请求规范到 OTHER。"""

        deprecated_compatibility = review_only or category_id == NEEDS_REVIEW_NODE_ID
        if deprecated_compatibility:
            category_id = OTHER_CATEGORY_ID
        # schema v2 不再输出或使用 review_only 语义；保留请求字段仅用于兼容旧客户端。
        effective_review = False
        image_date = parse_image_date_virtual_node_id(category_id)
        if scope not in {"direct", "descendants"}:
            raise OrganizationQueryError("scope 只能是 direct 或 descendants")
        if category_id and image_date is None and category_id not in self.nodes_by_id:
            raise OrganizationQueryError("当前分类目录中不存在该 category_id")

        base_query = self._active_copy_query()
        active_ids = {row.id for row in base_query.all()}
        relation_by_copy, other_ids, legacy_ids = self._current_primary_state(active_ids)
        if image_date is not None:
            selected_ids = self._image_date_copy_ids(image_date)
            query = (
                base_query.filter(WorkingCopy.id.in_(selected_ids))
                if selected_ids
                else base_query.filter(False)
            )
        elif category_id == OTHER_CATEGORY_ID:
            query = (
                base_query.filter(WorkingCopy.id.in_(other_ids))
                if other_ids
                else base_query.filter(False)
            )
        elif category_id:
            selected_categories = (
                self._descendant_ids(category_id) if scope == "descendants" else {category_id}
            )
            query = (
                base_query.join(
                    DocumentCategory,
                    (DocumentCategory.working_copy_id == WorkingCopy.id)
                    & (DocumentCategory.document_version_id == WorkingCopy.current_version_id),
                )
                .filter(
                    DocumentCategory.relation_role == "PRIMARY",
                    DocumentCategory.status.in_(ACTIVE_PRIMARY_STATUSES),
                    DocumentCategory.category_id.in_(selected_categories),
                )
                .distinct()
            )
            if scope == "direct" and category_id == IMAGE_DATE_CATEGORY_ROOT_ID:
                # 图片时间节点是学院根的虚拟子节点，direct 查询根节点时不能重复返回。
                image_ids = self._image_date_copy_ids()
                if image_ids:
                    query = query.filter(~WorkingCopy.id.in_(image_ids))
        else:
            query = base_query

        total = query.count()
        copies = (
            query.order_by(WorkingCopy.relative_path.asc(), WorkingCopy.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        copy_ids = [item.id for item in copies]
        decisions = self._latest_decisions(set(copy_ids)) if copy_ids else {}
        pending_by_copy = self._pending_primary_by_copy(set(copy_ids)) if copy_ids else {}
        if copy_ids:
            # active_ids 是本次查询前的当前发布集；再次从数据库读取，避免分页
            # 投影在长事务内遗漏真实有效 PRIMARY。
            current_relations, _current_other_ids, _current_legacy_ids = self._current_primary_state(
                set(copy_ids)
            )
            relation_by_copy.update(current_relations)
            legacy_ids.update(_current_legacy_ids)

        files = []
        for working_copy in copies:
            relation = relation_by_copy.get(working_copy.id)
            decision = decisions.get(working_copy.id)
            pending_primary = pending_by_copy.get(working_copy.id)
            effective_primary = (
                OrganizationPrimaryResponse(
                    category_id=relation.category_id,
                    category_path=list(relation.category_path_json or []),
                    status=relation.status,
                )
                if relation is not None
                else None
            )
            files.append(
                OrganizationFileItemResponse(
                    working_copy_id=working_copy.id,
                    document_id=working_copy.document_id,
                    document_version_id=str(working_copy.current_version_id or ""),
                    filename=working_copy.filename,
                    relative_path=working_copy.relative_path,
                    size_bytes=working_copy.size_bytes,
                    primary_category_id=relation.category_id if relation else None,
                    primary_category_path=list(relation.category_path_json or []) if relation else [],
                    primary_category_status=relation.status if relation else None,
                    classification_outcome=(
                        "OTHER"
                        if relation is None or self._is_other_category(relation.category_id)
                        else "CLASSIFIED"
                    ),
                    placement_status=str(working_copy.placement_status or "PENDING"),
                    effective_primary=effective_primary,
                    pending_primary=pending_primary,
                    legacy_location=working_copy.id in legacy_ids,
                    organization_decision=decision.decision if decision else None,
                    organization_reason_codes=list(decision.reason_codes_json or []) if decision else [],
                    updated_at=working_copy.updated_at,
                )
            )

        return OrganizationFilePageResponse(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=ceil(total / page_size) if total else 0,
            category_id=category_id,
            scope=scope,
            review_only=effective_review,
            deprecated_compatibility=deprecated_compatibility,
            files=files,
        )

    def _active_copy_query(self):
        """建立唯一共享工作区的已发布文件查询。"""

        return self.db.query(WorkingCopy).filter(
            WorkingCopy.workspace_id == self.workspace_id,
            WorkingCopy.status == "ACTIVE",
            WorkingCopy.current_version_id.is_not(None),
        )

    def _active_primary_query(self):
        """建立当前版本的活动主分类关系查询。"""

        return (
            self.db.query(DocumentCategory, WorkingCopy)
            .join(WorkingCopy, WorkingCopy.id == DocumentCategory.working_copy_id)
            .filter(
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
                WorkingCopy.current_version_id == DocumentCategory.document_version_id,
                DocumentCategory.relation_role == "PRIMARY",
                DocumentCategory.status.in_(ACTIVE_PRIMARY_STATUSES),
            )
        )

    def _current_primary_state(
        self,
        working_copy_ids: set[str],
    ) -> tuple[dict[str, DocumentCategory], set[str], set[str]]:
        """返回有效 PRIMARY、OTHER 聚合文件和历史位置文件。

        OTHER 是展示分组，不是对历史关系的重写：历史 fallback 的真实 ID 仍由
        relation_by_copy 返回，只有页面分组和 classification_outcome 归一到 OTHER。
        """

        if not working_copy_ids:
            return {}, set(), set()
        relations: dict[str, DocumentCategory] = {}
        for relation, working_copy in self._active_primary_query().filter(
            WorkingCopy.id.in_(working_copy_ids)
        ).all():
            relations[working_copy.id] = relation
        no_primary_ids = working_copy_ids - set(relations)
        fallback_ids = {
            working_copy_id
            for working_copy_id, relation in relations.items()
            if self._is_other_category(relation.category_id)
        }
        legacy_ids = {
            working_copy_id
            for working_copy_id in fallback_ids
            if relations[working_copy_id].category_id != OTHER_CATEGORY_ID
        }
        # 没有有效主类的历史活动文件不伪造成 system.other，但在新公开树中仍可
        # 从 OTHER 找到；其解析/落位状态通过独立字段如实展示。
        legacy_ids.update(no_primary_ids)
        return relations, fallback_ids | no_primary_ids, legacy_ids

    def _is_other_category(self, category_id: str | None) -> bool:
        """只依据 taxonomy 声明识别 OTHER 及历史 fallback，不按显示名猜测。"""

        normalized = str(category_id or "")
        return normalized in self.fallback_category_ids

    def _pending_primary_by_copy(
        self,
        working_copy_ids: set[str],
    ) -> dict[str, OrganizationPrimaryResponse]:
        """投影活动落位操作的冻结目标；查询绝不创建或推进操作。"""

        if not working_copy_ids:
            return {}
        rows = (
            self.db.query(ClassificationPlacementOperation)
            .filter(
                ClassificationPlacementOperation.working_copy_id.in_(working_copy_ids),
                ClassificationPlacementOperation.state.in_(_ACTIVE_PLACEMENT_STATES),
            )
            .order_by(
                ClassificationPlacementOperation.created_at.desc(),
                ClassificationPlacementOperation.id.desc(),
            )
            .all()
        )
        result: dict[str, OrganizationPrimaryResponse] = {}
        for operation in rows:
            if operation.working_copy_id in result:
                continue
            snapshot = dict(operation.decision_snapshot_json or {})
            category_id = str(operation.target_category_id or "")
            if not category_id:
                continue
            placement_status = {
                "EXECUTING": "APPLYING",
                "FS_APPLIED": "RECONCILING",
                "RECONCILING": "RECONCILING",
            }.get(operation.state, "PENDING")
            result[operation.working_copy_id] = OrganizationPrimaryResponse(
                category_id=category_id,
                category_path=list(
                    self.paths_by_id.get(category_id)
                    or snapshot.get("category_path")
                    or []
                ),
                status=placement_status,
                placement_operation_id=operation.id,
            )
        return result

    def _latest_review_decisions(
        self,
        working_copy_ids: set[str],
    ) -> dict[str, DocumentOrganizationDecision]:
        """返回当前版本最近一次需要人工复核的真实运行决策。"""

        review_decisions = {
            working_copy_id: decision
            for working_copy_id, decision in self._latest_decisions(working_copy_ids).items()
            if decision.decision == "NEEDS_REVIEW"
            and not bool((decision.feature_snapshot_json or {}).get("shadow_only"))
        }
        if not review_decisions:
            return {}
        # 用户确认或更正后会创建活动主分类，但历史首次决策仍需保留审计；
        # 虚拟待复核节点必须据当前事实排除这些已完成复核的文件。
        classified_ids = {
            working_copy.id
            for _, working_copy in self._active_primary_query()
            .filter(WorkingCopy.id.in_(set(review_decisions)))
            .all()
        }
        return {
            working_copy_id: decision
            for working_copy_id, decision in review_decisions.items()
            if working_copy_id not in classified_ids
        }

    def _image_date_copy_ids(self, date_label: str | None = None) -> set[str]:
        """读取图片时间规则的活动副本 ID，时间匹配在后端受控投影上完成。"""

        result: set[str] = set()
        rows = self._active_primary_query().filter(
            DocumentCategory.category_id == IMAGE_DATE_CATEGORY_ROOT_ID,
            DocumentCategory.source.in_(IMAGE_DATE_RELATION_SOURCES),
        ).all()
        for relation, working_copy in rows:
            relation_date = image_date_from_category_path(
                relation.category_path_json
            )
            if relation_date and (date_label is None or relation_date == date_label):
                result.add(working_copy.id)
        return result

    def _latest_decisions(
        self,
        working_copy_ids: set[str],
    ) -> dict[str, DocumentOrganizationDecision]:
        """按完成时间读取每个当前文件版本的最新组织决策。"""

        if not working_copy_ids:
            return {}
        rows = (
            self.db.query(DocumentOrganizationDecision, WorkingCopy)
            .join(WorkingCopy, WorkingCopy.id == DocumentOrganizationDecision.working_copy_id)
            .filter(
                WorkingCopy.id.in_(working_copy_ids),
                WorkingCopy.current_version_id == DocumentOrganizationDecision.document_version_id,
            )
            .order_by(
                DocumentOrganizationDecision.completed_at.desc(),
                DocumentOrganizationDecision.created_at.desc(),
                DocumentOrganizationDecision.id.desc(),
            )
            .all()
        )
        latest: dict[str, DocumentOrganizationDecision] = {}
        for decision, working_copy in rows:
            # Shadow 是离线观测事实，不得覆盖页面正在展示的真实落位决策。
            if bool((decision.feature_snapshot_json or {}).get("shadow_only")):
                continue
            latest.setdefault(working_copy.id, decision)
        return latest

    def _index_node(self, node: CategoryNode, parents: list[str]) -> None:
        """建立分类 ID 到节点和显示路径的索引。"""

        path = [*parents, node.name]
        if node.id:
            self.nodes_by_id[node.id] = node
            self.paths_by_id[node.id] = path
            if str(node.node_kind or "") == "FALLBACK":
                self.fallback_category_ids.add(node.id)
        for child in node.children:
            self._index_node(child, path)

    def _descendant_ids(self, category_id: str) -> set[str]:
        """返回指定节点及全部带稳定 ID 的后代。"""

        result: set[str] = set()

        def walk(node: CategoryNode) -> None:
            if node.id:
                result.add(node.id)
            for child in node.children:
                walk(child)

        walk(self.nodes_by_id[category_id])
        return result
