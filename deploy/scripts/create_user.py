"""通过现有 AuthService 创建部署后的初始用户。"""
from __future__ import annotations

import argparse
import sys

from fastapi import HTTPException

from app.core.database import SessionLocal
from app.modules.auth.schemas import RegisterRequest
from app.modules.auth.service import AuthService


def main() -> int:
    """解析命令行参数并创建用户，避免部署脚本绕过后端注册规则。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--display-name", default="")
    parser.add_argument("--email", default=None)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        user = AuthService(db).register(
            RegisterRequest(
                username=args.username,
                password=args.password,
                display_name=args.display_name,
                email=args.email or None,
            )
        )
    except HTTPException as exc:
        print(f"创建用户失败：{exc.detail}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print(f"已创建用户：{user.username}（id={user.id}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
