"""受限 WebDAV 文本工作区的核心逻辑（与传输层无关）。

所有裁决——锁状态、到期判断、If 条件验证、正文与版本更新——都在**单笔
SQLite 事务**内完成（``BEGIN IMMEDIATE``），保证失败请求不会改正文或版本，
也不会出现"读到过期锁、随后被别人抢先"的竞态。

功能边界（刻意保持极小）：
* 资源树在建库时固定写入，不支持创建 / 删除 / 移动；
* 集合（目录）只用于 LOCK / UNLOCK，不允许 GET / PUT；
* LOCK 只支持独占写锁（``<exclusive/>`` + ``<write/>``），Depth 0 / infinity；
* 不支持共享锁、账号与权限系统。
"""

import datetime
import os
import sqlite3
import uuid
import xml.etree.ElementTree as ET

from .conditions import IfHeaderError, parse_if_header

# 让回显 owner 片段时 DAV 命名空间保持 D: 前缀，而不是 ns0。
ET.register_namespace("D", "DAV:")

SCHEMA = """
CREATE TABLE resources (
    path        TEXT PRIMARY KEY,   -- 集合以 '/' 结尾；根目录为 '/'
    is_collection INTEGER NOT NULL,
    body        BLOB NOT NULL DEFAULT '',
    version     INTEGER NOT NULL DEFAULT 1,
    etag        TEXT NOT NULL
);
CREATE TABLE locks (
    token       TEXT PRIMARY KEY,   -- opaquelocktoken:<uuid>
    path        TEXT NOT NULL,
    depth       TEXT NOT NULL,      -- '0' 或 'infinity'
    owner_xml   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,      -- ISO8601 UTC
    expires_at  TEXT                -- NULL = 无限期
);
"""

# 建库时写入的固定目录树（集合以 '/' 结尾）。
DEFAULT_SEED = {
    "/": {"collection": True},
    "/docs/": {"collection": True},
    "/docs/intro.txt": {"body": "欢迎使用受限 WebDAV 文本工作区。\n"},
    "/docs/notes.md": {"body": "# 笔记\n\n在这里记录想法。\n"},
    "/journal/": {"collection": True},
    "/journal/log.txt": {"body": "2026-10-06 初始化工作区\n"},
}

DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 3600


class DavError(Exception):
    """一个应当转成 HTTP 响应的 WebDAV 错误。

    dav_tag: DAV 错误 XML 中的元素名（None 表示空错误体）。
    """

    def __init__(self, status, reason, dav_tag=None):
        super().__init__("%d %s" % (status, reason))
        self.status = status
        self.reason = reason
        self.dav_tag = dav_tag


def system_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _make_etag(version):
    """生成强 ETag。版本号在同一事务内自增，因此 ETag 单调且唯一对应版本。"""
    return '"%d"' % version


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def parse_lockinfo(xml_bytes):
    """解析 LOCK 请求体，强制独占写锁。返回 owner XML 字符串。"""
    if not xml_bytes:
        raise DavError(400, "lockinfo XML body required", "lock-token-submitted")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise DavError(400, "malformed lockinfo XML")

    def child(tag):
        # ElementTree 不做默认命名空间的本地名匹配，自行按 localname 找。
        for el in root.iter():
            if el.tag.split("}", 1)[-1] == tag:
                return el
        return None

    lockscope = child("lockscope")
    locktype = child("locktype")
    if lockscope is None or locktype is None:
        raise DavError(400, "lockinfo must contain lockscope and locktype")
    scopes = {el.tag.split("}", 1)[-1] for el in lockscope}
    types = {el.tag.split("}", 1)[-1] for el in locktype}
    if "shared" in scopes:
        # 明确不支持共享锁。
        raise DavError(400, "shared locks are not supported")
    if "exclusive" not in scopes:
        raise DavError(400, "only exclusive write locks are supported")
    if "write" not in types:
        raise DavError(400, "only write locks are supported")

    owner = child("owner")
    owner_xml = ""
    if owner is not None and list(owner):
        # 保留调用方提供的 owner 内部标记，响应时原样回显。
        owner_xml = "".join(ET.tostring(ch, encoding="unicode") for ch in owner)
    elif owner is not None and owner.text:
        from xml.sax.saxutils import escape

        owner_xml = escape(owner.text.strip())
    return owner_xml


class LockResult:
    def __init__(self, token, created, depth, owner_xml, created_at, expires_at,
                 timeout_seconds):
        self.token = token
        self.created = created          # True=新锁 False=刷新旧锁（令牌不变）
        self.depth = depth
        self.owner_xml = owner_xml
        self.created_at = created_at
        self.expires_at = expires_at
        self.timeout_seconds = timeout_seconds  # None = Infinite


class Workspace:
    def __init__(self, db_path, seed=None, now_provider=system_now,
                 default_timeout=DEFAULT_TIMEOUT_SECONDS):
        self.db_path = db_path
        self.seed = DEFAULT_SEED if seed is None else seed
        self.now_provider = now_provider
        self.default_timeout = default_timeout
        self._tls = __import__("threading").local()
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_schema()

    # ---- 连接与事务 -----------------------------------------------------

    def _conn(self):
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            # autocommit 模式：事务由本类显式开启，避免驱动隐式事务。
            conn.isolation_level = None
            self._tls.conn = conn
        return conn

    def _init_schema(self):
        conn = self._conn()
        conn.executescript(SCHEMA)
        existing = conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
        if existing:
            return
        for path, meta in self.seed.items():
            is_col = 1 if meta.get("collection") else 0
            body = b"" if is_col else meta.get("body", "").encode("utf-8")
            version = 1
            conn.execute(
                "INSERT INTO resources(path, is_collection, body, version, etag)"
                " VALUES (?,?,?,?,?)",
                (path, is_col, body, version, _make_etag(version)),
            )

    class _Transaction:
        def __init__(self, ws):
            self.ws = ws
            self.conn = None

        def __enter__(self):
            self.conn = self.ws._conn()
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
            return False

    # ---- 路径辅助 -------------------------------------------------------

    @staticmethod
    def normalize_path(path):
        """归一化 URL 路径：集合保留结尾 '/'，文件不带结尾 '/'。

        尾斜杠是有意义的：``/docs/`` 是集合，``/docs`` 不存在。
        """
        if not path:
            path = "/"
        if "\x00" in path:
            raise DavError(400, "invalid path")
        if path == "/":
            return "/"
        trailing = path.endswith("/")
        raw_parts = path.strip("/").split("/")
        if any(part in ("", ".", "..") for part in raw_parts):
            raise DavError(400, "invalid path")
        cleaned = "/" + "/".join(raw_parts)
        # 尾斜杠保留：它标记集合。文件加尾斜杠在固定树中查不到 -> 404，
        # 绝不能被当作可 PUT 创建的新名字。
        return cleaned + "/" if trailing else cleaned

    @staticmethod
    def _ancestor_collections(path):
        """资源/集合的全部祖先集合（含根），用于祖先锁覆盖查询。"""
        out = []
        prefix = "/"
        for part in path.strip("/").split("/"):
            out.append(prefix if prefix else "/")
            prefix = prefix + part + "/"
        return ["/"] + [a for a in out if a != "/"]

    def _get_resource(self, conn, path, lock=True):
        row = conn.execute(
            "SELECT * FROM resources WHERE path=?", (path,)
        ).fetchone()
        if row is None:
            raise DavError(404, "Not Found")
        return row

    def _purge_expired(self, conn, now):
        """删除所有到期锁。仅在写事务内调用，裁决基于清理后的快照。"""
        conn.execute(
            "DELETE FROM locks WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (_iso(now),),
        )

    def _active_covering_locks(self, conn, path):
        """返回当前覆盖 path 的全部有效锁（调用前须已清理过期锁）。

        覆盖 = 锁直接挂在 path 上，或挂在某祖先集合且 depth=infinity。
        """
        rows = conn.execute("SELECT * FROM locks").fetchall()
        covering = []
        ancestors = self._ancestor_collections(path)
        for row in rows:
            if row["path"] == path:
                covering.append(row)
            elif row["depth"] == "infinity" and row["path"] in ancestors:
                covering.append(row)
        return covering

    # ---- 读取 -----------------------------------------------------------

    def get(self, path):
        path = self.normalize_path(path)
        conn = self._conn()
        row = conn.execute("SELECT * FROM resources WHERE path=?", (path,)).fetchone()
        if row is None:
            raise DavError(404, "Not Found")
        if row["is_collection"]:
            raise DavError(405, "collections are not GETtable in this workspace")
        return {
            "path": row["path"],
            "body": row["body"],
            "etag": row["etag"],
            "version": row["version"],
        }

    # ---- PUT ------------------------------------------------------------

    @staticmethod
    def _parse_if_match(value):
        """解析强 If-Match（含引号的 ETag 列表）。* 单独表示"任意表示"。"""
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise DavError(400, "empty If-Match header")
        if text == "*":
            return "*"
        tags = []
        for raw in text.split(","):
            piece = raw.strip()
            if not piece:
                raise DavError(400, "malformed If-Match header")
            if piece[:2].upper() == "W/":
                # 强比较不接受弱 ETag。
                raise DavError(400, "weak ETags are not allowed in If-Match")
            if len(piece) < 2 or piece[0] != '"' or piece[-1] != '"':
                raise DavError(400, "malformed entity-tag in If-Match")
            tags.append(piece)
        if not tags:
            raise DavError(400, "malformed If-Match header")
        return tags

    @staticmethod
    def _adjudicate_if(if_lists, covering_tokens, current_etag):
        """按 OR(列表) / AND(列表内) 裁决 If 条件。

        返回 (结果, 原因)：
        * ("ok", None)           某个列表整体满足；
        * ("etag", None)         存在令牌齐全的列表，但 ETag 与当前版本不符；
        * ("token", None)        令牌对不上（缺失、错误或不全）。

        裁决在调用方的写事务内进行，``covering_tokens`` 是清理过期锁后的
        当前覆盖锁集合；提交一个已经失效的令牌因此属于令牌失败（423）。
        """
        saw_token_ready_list = False
        for conditions in if_lists:
            tokens = set()
            bad_token = False
            etag_mismatch = False
            for kind, value in conditions:
                if kind == "token":
                    tokens.add(value)
                    if value not in covering_tokens:
                        bad_token = True
                elif value != current_etag:
                    etag_mismatch = True
            # 列表必须恰好提交全部覆盖锁令牌，且不得夹带其他令牌；
            # 无锁时出现任何令牌条件都算令牌失败。
            if bad_token or tokens != covering_tokens:
                continue
            if etag_mismatch:
                saw_token_ready_list = True
                continue
            return ("ok", None)
        if saw_token_ready_list:
            return ("etag", None)
        return ("token", None)

    def put(self, path, body, if_match=None, if_header=None):
        path = self.normalize_path(path)
        if not isinstance(body, (bytes, bytearray)):
            body = body.encode("utf-8")
        try:
            match_tags = self._parse_if_match(if_match)
            if_lists = parse_if_header(if_header)
        except IfHeaderError as exc:
            raise DavError(400, str(exc))

        with self._Transaction(self) as conn:
            now = self.now_provider()
            self._purge_expired(conn, now)
            row = self._get_resource(conn, path)
            if row["is_collection"]:
                raise DavError(405, "PUT on collections is not allowed")
            current_etag = row["etag"]
            covering_tokens = {
                lk["token"] for lk in self._active_covering_locks(conn, path)
            }

            # 1) 锁与 If 条件在同一事务裁决。
            if covering_tokens and if_lists is None:
                raise DavError(423, "resource is locked",
                               "lock-token-submitted")
            if if_lists is not None:
                verdict, _ = self._adjudicate_if(
                    if_lists, covering_tokens, current_etag
                )
                if verdict == "token":
                    raise DavError(423, "required lock token not submitted",
                                   "lock-token-submitted")
                if verdict == "etag":
                    raise DavError(412, "ETag does not match",
                                   "precondition-failed")

            # 2) 强 If-Match：版本必须仍是客户端看到的版本。
            if match_tags is not None:
                if match_tags != "*" and current_etag not in match_tags:
                    raise DavError(412, "ETag does not match",
                                   "precondition-failed")

            # 3) 一切满足：正文与版本在同一事务内落盘。
            new_version = row["version"] + 1
            new_etag = _make_etag(new_version)
            conn.execute(
                "UPDATE resources SET body=?, version=?, etag=? WHERE path=?",
                (bytes(body), new_version, new_etag, path),
            )
            return {"path": path, "etag": new_etag, "version": new_version}

    # ---- LOCK / UNLOCK --------------------------------------------------

    @staticmethod
    def normalize_depth(depth_header, is_collection):
        """将 Depth 头折叠为有效值：集合 0/infinity（默认 infinity），
        非集合恒为 0。"""
        if depth_header is None:
            return "infinity" if is_collection else "0"
        d = depth_header.strip().lower()
        if d == "0":
            return "0"
        if d == "infinity":
            return "infinity" if is_collection else "0"
        raise DavError(400, "only Depth 0 and Depth infinity are supported")

    @staticmethod
    def _parse_timeout(header, default):
        if header is None:
            return default
        text = header.strip()
        if not text:
            return default
        # RFC4918: "Second-4100000000" 可逗号列举，Infinite 表示无限。
        for part in text.split(","):
            token = part.strip()
            if token.lower() == "infinite":
                return None
            if token.lower().startswith("second-"):
                try:
                    value = int(token.split("-", 1)[1])
                except (IndexError, ValueError):
                    raise DavError(400, "malformed Timeout header")
                if value <= 0:
                    raise DavError(400, "malformed Timeout header")
                return min(value, MAX_TIMEOUT_SECONDS)
        raise DavError(400, "malformed Timeout header")

    def _lock_conflicts(self, conn, path, depth):
        """返回与本次（新）独占锁冲突的现有有效锁行。"""
        conflicts = []
        rows = conn.execute("SELECT * FROM locks").fetchall()
        for row in rows:
            lp = row["path"]
            if lp == path:
                conflicts.append(row)              # 同资源
            elif depth == "infinity":
                # 请求锁挂在集合（必以 '/' 结尾）：后代路径以其为前缀；
                # 挂在文件上不可能有后代。
                if path.endswith("/") and lp.startswith(path) and lp != path:
                    conflicts.append(row)
            if row["depth"] == "infinity" and row["path"] != path:
                # 现有锁是祖先集合的深度锁：path 位于其下。
                coll = row["path"]
                ancestor = coll if coll == "/" else coll.rstrip("/") + "/"
                if coll == "/" or path.startswith(ancestor):
                    if row not in conflicts:
                        conflicts.append(row)
        return conflicts

    def lock(self, path, depth_header, timeout_header, lockinfo_xml, if_header=None):
        path = self.normalize_path(path)

        # 先识别刷新意图：刷新请求允许没有 lockinfo 体。
        refresh_token = None
        if if_header is not None and if_header.strip():
            try:
                if_lists = parse_if_header(if_header)
            except IfHeaderError as exc:
                raise DavError(400, str(exc))
            if if_lists is not None:
                flat = [v for conditions in if_lists
                        for k, v in conditions if k == "token"]
                if len(if_lists) != 1 or len(flat) != 1 or not flat:
                    raise DavError(
                        400,
                        "LOCK refresh requires exactly one lock token in If header",
                    )
                refresh_token = flat[0]

        # 新锁强制独占写锁，必须给出合法 lockinfo；刷新路径不需要请求体。
        owner_xml = ""
        if refresh_token is None:
            owner_xml = parse_lockinfo(lockinfo_xml)

        with self._Transaction(self) as conn:
            now = self.now_provider()
            self._purge_expired(conn, now)
            row = self._get_resource(conn, path)
            timeout = self._parse_timeout(timeout_header, self.default_timeout)

            # 刷新：令牌必须仍是该 URI 上的**当前有效锁**。
            if refresh_token is not None:
                existing = conn.execute(
                    "SELECT * FROM locks WHERE token=?", (refresh_token,)
                ).fetchone()
                if (
                    existing is None
                    or existing["path"] != path
                ):
                    # 过期令牌绝不能解开后来建立的新锁。
                    raise DavError(412, "lock token is not held on this resource",
                                   "lock-token-submitted")
                expires_at = None if timeout is None else _iso(
                    now + datetime.timedelta(seconds=timeout)
                )
                conn.execute(
                    "UPDATE locks SET expires_at=? WHERE token=?",
                    (expires_at, refresh_token),
                )
                remaining = (
                    None if timeout is None
                    else int((datetime.datetime.fromisoformat(expires_at) - now)
                             .total_seconds())
                )
                return LockResult(
                    refresh_token, False, existing["depth"],
                    existing["owner_xml"], existing["created_at"], expires_at,
                    remaining,
                )

            depth = self.normalize_depth(depth_header, bool(row["is_collection"]))
            conflicts = self._lock_conflicts(conn, path, depth)
            if conflicts:
                raise DavError(423, "there is a conflicting lock",
                               "lock-token-submitted")

            token = "opaquelocktoken:" + str(uuid.uuid4())
            created_at = _iso(now)
            expires_at = None if timeout is None else _iso(
                now + datetime.timedelta(seconds=timeout)
            )
            conn.execute(
                "INSERT INTO locks(token, path, depth, owner_xml,"
                " created_at, expires_at) VALUES (?,?,?,?,?,?)",
                (token, path, depth, owner_xml, created_at, expires_at),
            )
            return LockResult(token, True, depth, owner_xml, created_at,
                              expires_at, timeout)

    def unlock(self, path, token):
        path = self.normalize_path(path)
        if not token:
            raise DavError(400, "Lock-Token header required")
        token = token.strip()
        if token.startswith("<") and token.endswith(">"):
            token = token[1:-1]
        if not token:
            raise DavError(400, "malformed Lock-Token header")
        with self._Transaction(self) as conn:
            now = self.now_provider()
            self._purge_expired(conn, now)
            row = conn.execute(
                "SELECT * FROM locks WHERE token=?", (token,)
            ).fetchone()
            if row is None:
                # 已过期并被清理的令牌不能再解开（后来可能已有新锁）。
                raise DavError(409, "no such active lock token")
            if row["path"] != path:
                raise DavError(
                    400,
                    "lock token is not held on the request-URI",
                    "lock-token-unsupported",
                )
            conn.execute("DELETE FROM locks WHERE token=?", (token,))

    # ---- 诊断（供页面 / 测试） -----------------------------------------

    def active_lock_on(self, path):
        """事务内读取 path 自身的当前锁（到期即视为无）。"""
        path = self.normalize_path(path)
        with self._Transaction(self) as conn:
            now = self.now_provider()
            self._purge_expired(conn, now)
            row = conn.execute(
                "SELECT * FROM locks WHERE path=?", (path,)
            ).fetchone()
            if row is None:
                return None
            return dict(row)
