"""活动工作副本的主分类树与首次组织复核只读查询。"""

from __future__ import annotations

from collections import defaultdict
from math import ceil

from sqlalchemy.orm import Session

from app.db.models import (
    DocumentCategory,
    DocumentOrganizationDecision,
    WorkingCopy,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.image_date_policy import (
    IMAGE_DATE_CATEGORY_ROOT_ID,
    IMAGE_DATE_RELATION_SOURCE,
    image_date_from_category_path,
    image_date_virtual_node_id,
    parse_image_date_virtual_node_id,
)
from app.modules.classification.organization_schemas import (
    OrganizationFileItemResponse,
    OrganizationFilePageResponse,
    OrganizationTreeNodeResponse,
    OrganizationTreeResponse,
)
from app.modules.classification.schemas import CategoryNode
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id


ACTIVE_PRIMARY_STATUSES = ("AUTO_APPLIED", "CONFIRMED")
NEEDS_REVIEW_NODE_ID = "__needs_review__"


class OrganizationQueryError(ValueError):
    """分类目录查询参数无法由当前 taxonomy 唯一解析。"""


class ClassificationOrganizationQueryService:
    """聚合已发布文件的主分类和非 Shadow 首次组织决策。"""

    def __init__(self, db: Session) -> None:
        """保存只读数据库会话并加载当前 taxonomy。"""

        self.db = db
        self.taxonomy = load_default_taxonomy()
        self.workspace_id = get_shared_workspace_id(db)
        self.nodes_by_id: dict[str, CategoryNode] = {}
        self.paths_by_id: dict[str, list[str]] = {}
        for root in self.taxonomy.categories:
            self._index_node(root, [])

    def tree(self) -> OrganizationTreeResponse:
        """返回活动主分类树，父节点计数按后代文件去重汇总。"""

        active_copies = self._active_copy_query().all()
        active_ids = {item.id for item in active_copies}
        relations = self._active_primary_query().all()
        direct_ids: dict[str, set[str]] = defaultdict(set)
        image_date_ids: dict[str, set[str]] = defaultdict(set)
        for relation, working_copy in relations:
            image_date = (
                image_date_from_category_path(relation.category_path_json)
                if relation.source == IMAGE_DATE_RELATION_SOURCE
                and relation.category_id == IMAGE_DATE_CATEGORY_ROOT_ID
                else None
            )
            if image_date:
                image_date_ids[image_date].add(working_copy.id)
            else:
                direct_ids[relation.category_id].add(working_copy.id)

        classified_ids = {working_copy.id for _, working_copy in relations}
        review_ids = set(self._latest_review_decisions(active_ids))

        def build(node: CategoryNode, parents: list[str]) -> tuple[OrganizationTreeNodeResponse, set[str]]:
            """递归构造节点，同时用集合避免父级重复计数。"""

            path = [*parents, node.name]
            child_responses: list[OrganizationTreeNodeResponse] = []
            subtree_ids = set(direct_ids.get(node.id or "", set()))
            for child in node.children:
                child_response, child_ids = build(child, path)
                child_responses.append(child_response)
                subtree_ids.update(child_ids)
            if node.id == IMAGE_DATE_CATEGORY_ROOT_ID:
                # 日期是上传组织维度，不写入静态 taxonomy；树接口按正式关系动态
                # 投影虚拟节点，并把最近日期放在前面便于浏览。
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

        nodes = [build(root, [])[0] for root in self.taxonomy.categories]
        nodes.insert(
            0,
            OrganizationTreeNodeResponse(
                category_id=NEEDS_REVIEW_NODE_ID,
                name="待复核",
                category_path=["待复核"],
                direct_file_count=len(review_ids),
                subtree_file_count=len(review_ids),
                is_virtual=True,
            ),
        )
        return OrganizationTreeResponse(
            taxonomy_key=self.taxonomy.key,
            taxonomy_version=self.taxonomy.version,
            total_active_files=len(active_ids),
            classified_file_count=len(classified_ids),
            needs_review_file_count=len(review_ids),
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
        """按主分类或待复核条件返回稳定服务端分页结果。"""

        effective_review = review_only or category_id == NEEDS_REVIEW_NODE_ID
        if category_id == NEEDS_REVIEW_NODE_ID:
            category_id = None
        image_date = parse_image_date_virtual_node_id(category_id)
        if scope not in {"direct", "descendants"}:
            raise OrganizationQueryError("scope 只能是 direct 或 descendants")
        if category_id and image_date is None and category_id not in self.nodes_by_id:
            raise OrganizationQueryError("当前分类目录中不存在该 category_id")

        base_query = self._active_copy_query()
        relation_by_copy: dict[str, DocumentCategory] = {}
        if effective_review:
            active_ids = {row.id for row in base_query.all()}
            review_decisions = self._latest_review_decisions(active_ids)
            selected_ids = set(review_decisions)
            query = base_query.filter(WorkingCopy.id.in_(selected_ids)) if selected_ids else base_query.filter(False)
        elif image_date is not None:
            selected_ids = self._image_date_copy_ids(image_date)
            query = (
                base_query.filter(WorkingCopy.id.in_(selected_ids))
                if selected_ids
                else base_query.filter(False)
            )
            review_decisions = {}
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
                # 图片日期节点是学院根的虚拟子节点，direct 查询根节点时不能重复返回。
                image_ids = self._image_date_copy_ids()
                if image_ids:
                    query = query.filter(~WorkingCopy.id.in_(image_ids))
            review_decisions = {}
        else:
            query = base_query
            review_decisions = {}

        total = query.count()
        copies = (
            query.order_by(WorkingCopy.relative_path.asc(), WorkingCopy.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        copy_ids = [item.id for item in copies]
        if copy_ids:
            for relation, working_copy in self._active_primary_query().filter(
                WorkingCopy.id.in_(copy_ids)
            ).all():
                relation_by_copy[working_copy.id] = relation
            if not effective_review:
                review_decisions = self._latest_decisions(set(copy_ids))

        files = []
        for working_copy in copies:
            relation = relation_by_copy.get(working_copy.id)
            decision = review_decisions.get(working_copy.id)
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
        """读取图片日期规则的活动副本 ID，日期匹配在后端受控投影上完成。"""

        result: set[str] = set()
        rows = self._active_primary_query().filter(
            DocumentCategory.category_id == IMAGE_DATE_CATEGORY_ROOT_ID,
            DocumentCategory.source == IMAGE_DATE_RELATION_SOURCE,
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
