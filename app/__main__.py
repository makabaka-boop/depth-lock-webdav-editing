"""开发/容器入口：python -m app

环境变量：
* LISTEN_HOST（默认 0.0.0.0）
* LISTEN_PORT（默认 8080）
* DB_PATH（默认 /data/workspace.db，开发时可用本地路径）
* LOCK_TIMEOUT_SECONDS（默认 300）
"""

import os

from .server import build_server
from .workspace import Workspace


def main():
    host = os.environ.get("LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("LISTEN_PORT", "8080"))
    db_path = os.environ.get("DB_PATH", "/data/workspace.db")
    timeout = int(os.environ.get("LOCK_TIMEOUT_SECONDS", "300"))

    workspace = Workspace(db_path, default_timeout=timeout)
    httpd = build_server(host, port, workspace)
    print("restricted WebDAV workspace listening on %s:%d (db=%s)"
          % (host, port, db_path), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
