"""测试支撑：可注入时钟、临时数据库、真实 HTTP 服务与两个客户端。"""

import datetime
import os
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection

from app.server import build_server
from app.workspace import Workspace

LOCKINFO = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<D:lockinfo xmlns:D="DAV:">'
    b"<D:lockscope><D:exclusive/></D:lockscope>"
    b"<D:locktype><D:write/></D:locktype>"
    b"<D:owner><D:href>client</D:href></D:owner>"
    b"</D:lockinfo>"
)


class FakeClock:
    """测试时钟：从固定时刻起步，advance() 后所有裁决都看到新时间。"""

    def __init__(self, start=None):
        self.current = start or datetime.datetime(
            2026, 10, 6, 9, 0, 0, tzinfo=datetime.timezone.utc
        )

    def __call__(self):
        return self.current

    def advance(self, seconds):
        self.current += datetime.timedelta(seconds=seconds)


class Response:
    def __init__(self, status, reason, response, body):
        self.status = status
        self.reason = reason
        self.response = response
        self.body = body

    @property
    def text(self):
        return self.body.decode("utf-8", errors="replace")

    @property
    def etag(self):
        return self.response.getheader("ETag")

    @property
    def lock_token(self):
        value = self.response.getheader("Lock-Token", "")
        return value[1:-1] if value.startswith("<") and value.endswith(">") else value

    def header(self, name):
        return self.response.getheader(name)


class Client:
    """一个 HTTP 客户端，持有自己的 keep-alive 连接（模拟独立编辑器）。"""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.conn = HTTPConnection(host, port, timeout=10)

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def request(self, method, path, body=None, headers=None):
        self.conn.request(method, path, body=body, headers=headers or {})
        resp = self.conn.getresponse()
        data = resp.read()
        return Response(resp.status, resp.reason, resp, data)
    # 语义化便捷方法 -------------------------------------------------------

    def get(self, path, headers=None):
        return self.request("GET", path, headers=headers)

    def put(self, path, body, etag=None, if_header=None, extra=None):
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if etag is not None:
            headers["If-Match"] = etag
        if if_header is not None:
            headers["If"] = if_header
        if extra:
            headers.update(extra)
        return self.request("PUT", path, body=body.encode("utf-8"),
                            headers=headers)

    def lock(self, path, depth="0", timeout="Second-60", body=LOCKINFO,
             if_header=None):
        headers = {"Depth": depth, "Timeout": timeout,
                   "Content-Type": "application/xml; charset=utf-8"}
        if if_header:
            headers["If"] = if_header
        return self.request("LOCK", path, body=body, headers=headers)

    def refresh_lock(self, path, token, timeout="Second-60"):
        return self.request(
            "LOCK", path, body=b"",
            headers={"Timeout": timeout, "If": "(<%s>)" % token},
        )

    def unlock(self, path, token):
        return self.request("UNLOCK", path,
                            headers={"Lock-Token": "<%s>" % token})

    @staticmethod
    def if_list(*entries):
        """拼接单个 AND 列表：(<token> "etag")。"""
        return "(" + " ".join(entries) + ")"

    @staticmethod
    def token_entry(token):
        return "<%s>" % token

    @staticmethod
    def etag_entry(etag):
        return etag  # 已含引号


class HttpServerTestBase(unittest.TestCase):
    """每个用例一套：临时 SQLite + 可注入时钟 + 真实线程 HTTP 服务。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="webdav-", suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)  # Workspace 自行建库
        self.clock = FakeClock()
        self.workspace = Workspace(self.db_path, now_provider=self.clock,
                                   default_timeout=60)
        self.httpd = build_server("127.0.0.1", 0, self.workspace)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        # 两个独立客户端（独立连接）。
        self.a = Client("127.0.0.1", self.port)
        self.b = Client("127.0.0.1", self.port)

    def tearDown(self):
        for c in (self.a, self.b):
            c.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        for path in (self.db_path, self.db_path + "-wal",
                     self.db_path + "-shm"):
            try:
                os.unlink(path)
            except OSError:
                pass

    def lock_token(self, client, path, **kwargs):
        resp = client.lock(path, **kwargs)
        self.assertIn(resp.status, (200, 201), resp.text)
        return resp.lock_token
