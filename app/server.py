"""受限 WebDAV 文本工作区 HTTP 服务（仅依赖标准库）。

对外只开放已有资源的 GET / PUT / LOCK / UNLOCK：
* GET 集合、PUT 集合、创建/移动/删除等一律 405；
* 不做账号系统；
* ``/__editor__`` 提供轻量编辑页。
"""

import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote

from .workspace import DavError, Workspace

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DAV_NS = "DAV:"

ALLOWED_METHODS = {"GET", "PUT", "LOCK", "UNLOCK"}


def _error_body(dav_tag, message):
    if not dav_tag:
        return b""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<D:error xmlns:D="DAV:"><D:%s/>%s</D:error>'
        % (dav_tag, ("<D:human-readable>%s</D:human-readable>" % html.escape(message))
           if message else "")
    ).encode("utf-8")


def lock_active_eligibility_xml(result):
    """构造 LOCK 成功响应的 DAV:prop（RFC 4918 锁活性）。"""
    timeout = "Infinite" if result.timeout_seconds is None else (
        "Second-%d" % result.timeout_seconds
    )
    owner = result.owner_xml or ""
    depth = "infinity" if result.depth == "infinity" else "0"
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<D:prop xmlns:D="DAV:">'
        "<D:lockdiscovery>"
        '<D:activelock>'
        "<D:lockscope><D:exclusive/></D:lockscope>"
        "<D:locktype><D:write/></D:locktype>"
        '<D:depth>%s</D:depth>'
        '<D:owner>%s</D:owner>'
        '<D:timeout>%s</D:timeout>'
        '<D:locktoken><D:href>%s</D:href></D:locktoken>'
        '<D:lockroot><D:href>%%LOCKROOT%%</D:href></D:lockroot>'
        "</D:activelock>"
        "</D:lockdiscovery>"
        "</D:prop>"
    ) % (depth, owner, timeout, html.escape(result.token))


def make_handler(workspace):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RestrictedWebdav/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 安静：测试输出不被污染
            pass

        # ---- 基础工具 ---------------------------------------------------

        def _path(self):
            return unquote(urlsplit(self.path).path)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return b""
            return self.rfile.read(length)

        def _send(self, status, body=b"", content_type="text/plain; charset=utf-8",
                  extra_headers=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra_headers or []):
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)

        def _send_dav_error(self, exc):
            body = _error_body(exc.dav_tag, str(exc))
            ctype = "application/xml; charset=utf-8" if body else "text/plain"
            self._send(exc.status, body, ctype)

        # ---- 路由 -------------------------------------------------------

        def _handle_editor(self, path):
            if path == "/__editor__":
                return self._serve_static("index.html", "text/html; charset=utf-8")
            if path == "/__editor__/app.js":
                return self._serve_static("app.js",
                                          "application/javascript; charset=utf-8")
            if path == "/__editor__/state":
                return self._editor_state()
            return False

        def _serve_static(self, name, content_type):
            full = os.path.join(STATIC_DIR, name)
            try:
                with open(full, "rb") as fh:
                    body = fh.read()
            except OSError:
                raise DavError(404, "Not Found")
            self._send(200, body, content_type,
                       extra_headers=[("Cache-Control", "no-store")])
            return True

        def _editor_state(self):
            from urllib.parse import parse_qs

            query = urlsplit(self.path).query
            target = parse_qs(query).get("path", [""])[0]
            payload = {"path": target}
            if target:
                lock = workspace.active_lock_on(target)
                if lock:
                    payload["lock"] = {
                        "token": lock["token"],
                        "depth": lock["depth"],
                        "expires_at": lock["expires_at"],
                    }
            self._send(200, json.dumps(payload),
                       "application/json; charset=utf-8",
                       extra_headers=[("Cache-Control", "no-store")])
            return True

        def do_GET(self):
            self._dispatch("GET")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_LOCK(self):
            self._dispatch("LOCK")

        def do_UNLOCK(self):
            self._dispatch("UNLOCK")

        # 明确不支持的方法：统一 405，并在 Allow 中声明极小接口。
        def do_HEAD(self):
            self._method_not_allowed()

        def do_POST(self):
            self._method_not_allowed()

        def do_DELETE(self):
            self._method_not_allowed()

        def do_MKCOL(self):
            self._method_not_allowed()

        def do_MOVE(self):
            self._method_not_allowed()

        def do_COPY(self):
            self._method_not_allowed()

        def do_PROPFIND(self):
            self._method_not_allowed()

        def do_OPTIONS(self):
            self._method_not_allowed()

        def _method_not_allowed(self):
            self.send_response(405)
            self.send_header("Allow", "GET, PUT, LOCK, UNLOCK")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _dispatch(self, method):
            try:
                path = self._path()
                if path.startswith("/__editor__"):
                    if method != "GET" or not self._handle_editor(path):
                        raise DavError(404, "Not Found")
                    return
                if method == "GET":
                    self._do_get(path)
                elif method == "PUT":
                    self._do_put(path)
                elif method == "LOCK":
                    self._do_lock(path)
                else:
                    self._do_unlock(path)
            except DavError as exc:
                try:
                    self._send_dav_error(exc)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            except (BrokenPipeError, ConnectionResetError):
                pass

        # ---- 四个方法 ---------------------------------------------------

        def _do_get(self, path):
            item = workspace.get(path)
            self._send(
                200, item["body"], "text/plain; charset=utf-8",
                extra_headers=[
                    ("ETag", item["etag"]),
                    ("Cache-Control", "no-store"),
                ],
            )

        def _do_put(self, path):
            body = self._read_body()
            result = workspace.put(
                path,
                body,
                if_match=self.headers.get("If-Match"),
                if_header=self.headers.get("If"),
            )
            self._send(
                204, b"", "text/plain",
                extra_headers=[
                    ("ETag", result["etag"]),
                    ("Cache-Control", "no-store"),
                ],
            )

        def _do_lock(self, path):
            body = self._read_body()
            result = workspace.lock(
                path,
                self.headers.get("Depth"),
                self.headers.get("Timeout"),
                body,
                if_header=self.headers.get("If"),
            )
            xml = lock_active_eligibility_xml(result)
            # lockroot 使用请求路径（集合保留结尾斜杠）。
            xml = xml.replace("%LOCKROOT%", html.escape(path, quote=True))
            status = 200 if not result.created else 201
            headers = [("Cache-Control", "no-store")]
            if result.created:
                # 仅新锁通过 Lock-Token 响应头交付令牌；刷新不创建新令牌。
                headers.append(("Lock-Token", "<%s>" % result.token))
            if result.expires_at is not None:
                headers.append(("X-Lock-Expires", result.expires_at))
            self._send(status, xml.encode("utf-8"),
                       "application/xml; charset=utf-8",
                       extra_headers=headers)

        def _do_unlock(self, path):
            token = self.headers.get("Lock-Token") or ""
            workspace.unlock(path, token)
            self._send(204, b"", "text/plain")

    return Handler


def build_server(host, port, workspace):
    return ThreadingHTTPServer((host, port), make_handler(workspace))
