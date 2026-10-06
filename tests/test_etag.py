"""ETag 版本与强 If-Match：持有正确锁也不能覆盖别人更新的版本。"""

from tests.helpers import HttpServerTestBase


class EtagVersioningTest(HttpServerTestBase):
    def test_put_bumps_etag(self):
        r1 = self.a.get("/docs/notes.md")
        r2 = self.a.put("/docs/notes.md", "新内容\n", etag=r1.etag)
        self.assertEqual(r2.status, 204)
        self.assertNotEqual(r2.etag, r1.etag)
        r3 = self.a.get("/docs/notes.md")
        self.assertEqual(r3.etag, r2.etag)
        self.assertEqual(r3.text, "新内容\n")

    def test_put_without_if_match_allowed_when_unlocked(self):
        r = self.a.put("/docs/notes.md", "随便写")
        self.assertEqual(r.status, 204)

    def test_stale_if_match_rejected_even_when_nobody_else_locks(self):
        r1 = self.a.get("/docs/notes.md")
        self.assertEqual(
            self.a.put("/docs/notes.md", "v2", etag=r1.etag).status, 204
        )
        # 客户端 A 仍拿着旧 ETag：强校验必须失败。
        stale = self.a.put("/docs/notes.md", "v3", etag=r1.etag)
        self.assertEqual(stale.status, 412)
        self.assertIsNone(stale.etag)  # 失败响应不带新 ETag

    def test_star_if_match_means_any_representation(self):
        r = self.a.put("/docs/notes.md", "x", etag="*")
        self.assertEqual(r.status, 204)

    def test_weak_etag_in_if_match_rejected(self):
        r = self.a.put("/docs/notes.md", "x", etag='W/"1"')
        self.assertEqual(r.status, 400)

    def test_failed_put_keeps_body_and_version(self):
        first = self.a.get("/journal/log.txt")
        self.a.put("/journal/log.txt", "第二次正文", etag=first.etag)
        # 旧版本写入必败。
        bad = self.a.put("/journal/log.txt", "不应落盘", etag=first.etag)
        self.assertEqual(bad.status, 412)
        check = self.a.get("/journal/log.txt")
        self.assertEqual(check.text, "第二次正文")
        # 只有第一次成功写入令版本前进一次：1 -> 2。
        self.assertEqual(check.etag, '"2"')

    def test_if_header_etag_only_or_and(self):
        r1 = self.a.get("/docs/notes.md")
        wrong = '"99"'
        # 两个列表 OR：第二个列表给出正确 ETag 即命中。
        ok = self.a.put(
            "/docs/notes.md", "a",
            if_header="(%s) (%s)" % (wrong, r1.etag),
        )
        self.assertEqual(ok.status, 204)
        # 同一列表 AND：错误 ETag 与正确 ETag 并列 -> 不命中。
        bad = self.a.put(
            "/docs/notes.md", "b",
            if_header="(%s %s)" % (wrong, ok.etag),
        )
        self.assertEqual(bad.status, 412)
        # 分开成 OR 列表则又可命中。
        good = self.a.put(
            "/docs/notes.md", "c",
            if_header="(%s) (%s)" % (wrong, ok.etag),
        )
        self.assertEqual(good.status, 204)
