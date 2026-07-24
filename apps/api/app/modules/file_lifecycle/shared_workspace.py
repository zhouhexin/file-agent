"""共享工作目录对应的系统工作区服务。

此模块把“用户的默认工作区”和“全部用户共用的可操作文件空间”明确分开。
任何物理工作副本、检索投影和文件操作都必须使用该系统工作区；用户身份仍由
会话、Document、OperationPlan 和审计字段保存，不能以复制文件替代权限边界。
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.db.models import Workspace


SHARED_WORKSPACE_SYSTEM_KEY = "shared-working-directory"
SHARED_WORKSPACE_TYPE = "SYSTEM_SHARED"
SHARED_WORKSPACE_STORAGE_KEY = "shared"


def get_or_create_shared_workspace(db: Session) -> Workspace:
    """返回唯一共享工作区，并在新环境首次使用时安全创建它。

    该记录没有 owner，不能成为任何用户的 default workspace；它只表达全局共享
    工作副本的持久化范围，防止历史用户工作区数量影响物理文件副本数量。
    """

    workspace = (
        db.query(Workspace)
        .filter(Workspace.system_key == SHARED_WORKSPACE_SYSTEM_KEY)
        .one_or_none()
    )
    if workspace is not None:
        return workspace
    workspace = Workspace(
        name="系统共享文件工作区",
        owner_id=None,
        is_default=False,
        workspace_type=SHARED_WORKSPACE_TYPE,
        system_key=SHARED_WORKSPACE_SYSTEM_KEY,
    )
    try:
        # 多个 worker 首次启动时可能同时发现空库；保存点保证唯一键竞争不会回滚
        # 外层导入事务，随后统一读取已经由另一方创建的共享记录。
        with db.begin_nested():
            db.add(workspace)
            db.flush()
        return workspace
    except IntegrityError:
        return (
            db.query(Workspace)
            .filter(Workspace.system_key == SHARED_WORKSPACE_SYSTEM_KEY)
            .one()
        )


def get_shared_workspace_id(db: Session) -> str:
    """取得共享工作区稳定 ID，供工作副本和检索等物理范围调用。"""

    return get_or_create_shared_workspace(db).id
