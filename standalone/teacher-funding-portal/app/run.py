"""Run the independent service in the current Python environment."""

import argparse
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


def configure(env_file, fileagent_env=None):
    load_dotenv(env_file, override=False)
    if fileagent_env:
        values = dotenv_values(fileagent_env)
        url = values.get("DATABASE_URL")
        if not url:
            raise RuntimeError("FileAgent 配置中没有 DATABASE_URL。")
        os.environ["PORTAL_DATABASE_URL"] = url
    from .database import database_url
    database_url()  # Fail without printing credentials or falling back to SQLite.


def main():
    parser = argparse.ArgumentParser(description="在本机启动教师资助查询服务。")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--fileagent-env", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    configure(args.env_file, args.fileagent_env)
    import uvicorn
    uvicorn.run("app.main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
