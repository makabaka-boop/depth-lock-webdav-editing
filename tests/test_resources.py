"""固定资源树与极小方法集。"""

from tests.helpers import HttpServerTestBase


class FixedTreeTest(HttpServerTestBase):
    def test_seed_files_gettable(self):
        for path in ("/docs/intro.txt", "/docs/notes.md", "/journal/log.txt"):
            r = self.a.get(path)
            self.assertEqual(r.status, 200)
            self.assertTrue(r.etag)

    def test_missing_resource_404(self):
        r = self.a.get("/docs/does-not-exist.txt")
        self.assertEqual(r.status, 404)

    def test_no_creation_put_missing_404(self):
        r = self.a.put("/docs/new.txt", "x")
        self.assertEqual(r.status, 404)

    def test_collection_get_and_put_rejected(self):
        self.assertEqual(self.a.get("/docs/").status, 405)
        self.assertEqual(self.a.put("/docs/", "x").status, 405)
        self.assertEqual(self.a.get("/").status, 405)

    def test_unsupported_methods_405(self):
        for method in ("MKCOL", "DELETE", "MOVE", "COPY", "PROPFIND",
                       "POST", "OPTIONS"):
            r = self.a.request(method, "/docs/intro.txt")
            self.assertEqual(r.status, 405, method)

    def test_trailing_slash_on_file_is_404_not_creation(self):
        # /docs/intro.txt/ 是一个不存在的集合路径；PUT 也不能借此创建。
        self.assertEqual(self.a.get("/docs/intro.txt/").status, 404)
        self.assertEqual(self.a.put("/docs/intro.txt/", "x").status, 404)

    def test_dot_segments_rejected(self):
        self.assertEqual(self.a.get("/docs/../journal/log.txt").status, 400)

    def test_body_failure_unchanged(self):
        before = self.a.get("/docs/intro.txt")
        # 不存在的资源 PUT 绝不能创建任何东西。
        self.a.put("/docs/new.txt", "x")
        after = self.a.get("/docs/intro.txt")
        self.assertEqual(before.etag, after.etag)
        self.assertEqual(before.body, after.body)
