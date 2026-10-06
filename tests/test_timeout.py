"""可注入时钟驱动的到期、刷新与到期重锁裁决。"""

from tests.helpers import HttpServerTestBase


class TimeoutRefreshTest(HttpServerTestBase):
    def test_lock_uses_timeout_header(self):
        r = self.a.lock("/docs/intro.txt", timeout="Second-30")
        self.assertEqual(r.status, 201)
        self.assertIn("<D:timeout>Second-30</D:timeout>", r.text)

    def test_refresh_keeps_same_token(self):
        t1 = self.lock_token(self.a, "/docs/intro.txt", timeout="Second-60")
        self.clock.advance(40)
        # 刷新必须带自己的令牌，且不产生新令牌。
        r = self.a.refresh_lock("/docs/intro.txt", t1, timeout="Second-60")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.lock_token, "")  # 刷新响应不发新 Lock-Token 头
        self.assertIn(t1, r.text)
        # 再前进 30 秒：未刷新的锁此刻应已过期，但刷新过的还活着。
        self.clock.advance(30)
        self.assertEqual(
            self.a.request(
                "LOCK", "/docs/intro.txt", body=b"",
                headers={"Timeout": "Second-60",
                         "If": "(<%s>)" % t1},
            ).status,
            200,
        )

    def test_refresh_expired_token_fails(self):
        t1 = self.lock_token(self.a, "/docs/intro.txt", timeout="Second-60")
        self.clock.advance(61)
        r = self.a.refresh_lock("/docs/intro.txt", t1)
        self.assertEqual(r.status, 412)

    def test_expired_lock_allows_other_client_to_lock(self):
        t1 = self.lock_token(self.a, "/docs/intro.txt", timeout="Second-60")
        self.clock.advance(61)
        # B 拿到新锁（新令牌）。
        r = self.b.lock("/docs/intro.txt")
        self.assertEqual(r.status, 201)
        t2 = r.lock_token
        self.assertNotEqual(t1, t2)
        # A 的过期令牌不能解开 B 后来建立的新锁。
        bad = self.a.unlock("/docs/intro.txt", t1)
        self.assertEqual(bad.status, 409)
        # B 的锁仍在，A 裸写 / 持旧令牌写都失败。
        self.assertEqual(self.a.put("/docs/intro.txt", "x").status, 423)
        self.assertEqual(
            self.a.put(
                "/docs/intro.txt", "x",
                if_header="(<%s>)" % t1,
            ).status,
            423,
        )
        # 新锁确实还在。
        self.assertEqual(self.b.lock("/docs/intro.txt").status, 423)

    def test_expired_ancestor_lock_stops_protecting(self):
        self.lock_token(self.a, "/docs/", depth="infinity",
                        timeout="Second-60")
        self.clock.advance(61)
        # 过期祖先深度锁不再阻止后代锁定。
        self.assertEqual(self.b.lock("/docs/intro.txt").status, 201)

    def test_infinite_timeout(self):
        t = self.lock_token(self.a, "/docs/intro.txt", timeout="Infinite")
        self.clock.advance(10 ** 6)
        # 仍然有效。
        self.assertEqual(self.a.refresh_lock("/docs/intro.txt", t).status, 200)

    def test_put_after_lock_expiry_succeeds_without_token(self):
        etag = self.a.get("/docs/notes.md").etag
        self.lock_token(self.a, "/docs/notes.md", timeout="Second-60")
        self.clock.advance(61)
        r = self.b.put("/docs/notes.md", "锁已过期，自由写入", etag=etag)
        self.assertEqual(r.status, 204)

    def test_refresh_with_two_tokens_rejected(self):
        t1 = self.lock_token(self.a, "/docs/intro.txt")
        r = self.a.request(
            "LOCK", "/docs/intro.txt", body=b"",
            headers={"Timeout": "Second-60",
                     "If": "(<%s>) (<opaquelocktoken:other>)" % t1},
        )
        self.assertEqual(r.status, 400)

    def test_transactional_adjudication_under_contention(self):
        """两个并发 LOCK：恰好一个胜出，另一个见到冲突锁。"""
        import sqlite3
        import threading

        path = "/docs/notes.md"
        barrier = threading.Barrier(2)
        outcomes = []

        def attempt(seed):
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.isolation_level = None
            barrier.wait()
            try:
                conn.execute("BEGIN IMMEDIATE")
                now = self.clock()
                from app.workspace import _iso
                conn.execute(
                    "DELETE FROM locks WHERE expires_at IS NOT NULL"
                    " AND expires_at <= ?", (_iso(now),))
                hit = conn.execute(
                    "SELECT 1 FROM locks WHERE path=?", (path,)
                ).fetchone()
                if hit:
                    conn.execute("ROLLBACK")
                    outcomes.append(("conflict", seed))
                    return
                import uuid
                conn.execute(
                    "INSERT INTO locks(token,path,depth,owner_xml,"
                    "created_at,expires_at) VALUES (?,?,?,?,?,?)",
                    ("opaquelocktoken:" + str(uuid.uuid4()), path, "0",
                     "", _iso(self.clock()), None),
                )
                conn.execute("COMMIT")
                outcomes.append(("won", seed))
            except Exception as exc:  # pragma: no cover - 测试诊断
                outcomes.append(("error:%s" % exc, seed))

        t1 = threading.Thread(target=attempt, args=(1,))
        t2 = threading.Thread(target=attempt, args=(2,))
        t1.start(); t2.start(); t1.join(5); t2.join(5)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sorted(o[0] for o in outcomes), ["conflict", "won"])
