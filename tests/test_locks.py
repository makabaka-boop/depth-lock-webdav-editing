"""独占写锁语义：深度、祖先/后代冲突、集合仅用于锁定。"""

from tests.helpers import LOCKINFO, HttpServerTestBase


class ExclusiveLockTest(HttpServerTestBase):
    def test_lock_file_default_depth_is_zero(self):
        r = self.a.lock("/docs/intro.txt")
        self.assertEqual(r.status, 201)
        self.assertIn("<D:depth>0</D:depth>", r.text)
        self.assertTrue(r.lock_token.startswith("opaquelocktoken:"))

    def test_lock_file_with_depth_infinity_becomes_zero(self):
        r = self.a.lock("/docs/intro.txt", depth="infinity")
        self.assertEqual(r.status, 201)
        self.assertIn("<D:depth>0</D:depth>", r.text)

    def test_lock_collection_depth_infinity(self):
        r = self.a.lock("/docs/", depth="infinity")
        self.assertEqual(r.status, 201)
        self.assertIn("<D:depth>infinity</D:depth>", r.text)
        self.assertIn("<D:href>/docs/</D:href>", r.text)

    def test_depth_1_rejected(self):
        r = self.a.lock("/docs/", depth="1")
        self.assertEqual(r.status, 400)

    def test_shared_lock_rejected(self):
        shared = LOCKINFO.replace(b"<D:exclusive/>", b"<D:shared/>")
        r = self.a.lock("/docs/intro.txt", body=shared)
        self.assertEqual(r.status, 400)

    def test_non_write_lock_rejected(self):
        bad = LOCKINFO.replace(b"<D:write/>", b"<D:read/>")
        r = self.a.lock("/docs/intro.txt", body=bad)
        self.assertEqual(r.status, 400)

    def test_lock_missing_resource_404(self):
        self.assertEqual(self.a.lock("/docs/nope.txt").status, 404)

    def test_same_resource_second_lock_fails(self):
        self.assertEqual(self.a.lock("/docs/intro.txt").status, 201)
        self.assertEqual(self.b.lock("/docs/intro.txt").status, 423)

    def test_collection_depth0_does_not_block_child(self):
        # Depth:0 的集合锁只锁集合本身。
        self.assertEqual(self.a.lock("/docs/", depth="0").status, 201)
        self.assertEqual(self.b.lock("/docs/intro.txt").status, 201)

    def test_ancestor_infinity_protects_descendants(self):
        t = self.lock_token(self.a, "/docs/", depth="infinity")
        # 后代 LOCK 被祖先深度锁挡住。
        self.assertEqual(self.b.lock("/docs/intro.txt").status, 423)
        # 后代裸 PUT 423，错误令牌 PUT 423。
        self.assertEqual(self.b.put("/docs/intro.txt", "x").status, 423)
        self.assertEqual(
            self.b.put(
                "/docs/intro.txt", "x",
                if_header="(<opaquelocktoken:deadbeef>)",
            ).status,
            423,
        )
        # 持锁人令牌 + 当前 ETag 可以写后代。
        etag = self.b.get("/docs/intro.txt").etag
        ok = self.b.put(
            "/docs/intro.txt", "祖先锁保护下的写入\n",
            etag=etag,
            if_header=self.b.if_list(self.b.token_entry(t),
                                     self.b.etag_entry(etag)),
        )
        self.assertEqual(ok.status, 204)
        # 别人的令牌不可以。
        other = self.lock_token(self.b, "/journal/log.txt")
        blocked = self.b.put(
            "/docs/intro.txt", "坏",
            etag=ok.etag,
            if_header=self.b.if_list(self.b.token_entry(other),
                                     self.b.etag_entry(ok.etag)),
        )
        self.assertEqual(blocked.status, 423)

    def test_descendant_lock_blocks_ancestor_infinity(self):
        # 后代已持有冲突锁时，祖先的深度锁必须失败。
        self.assertEqual(self.b.lock("/docs/intro.txt").status, 201)
        self.assertEqual(self.a.lock("/docs/", depth="infinity").status, 423)
        # 但祖先 Depth:0（不覆盖后代）可以成功。
        self.assertEqual(self.a.lock("/docs/", depth="0").status, 201)

    def test_descendant_collection_blocks_ancestor_infinity(self):
        self.assertEqual(self.a.lock("/docs/", depth="0").status, 201)
        # 根的 infinity 锁与 /docs/ 的 depth0 锁冲突（/docs/ 是根后代）。
        self.assertEqual(self.b.lock("/", depth="infinity").status, 423)

    def test_unlock_then_relock_allowed(self):
        t = self.lock_token(self.a, "/docs/intro.txt")
        self.assertEqual(self.a.unlock("/docs/intro.txt", t).status, 204)
        self.assertEqual(self.b.lock("/docs/intro.txt").status, 201)

    def test_unlock_with_wrong_token_shape(self):
        self.lock_token(self.a, "/docs/intro.txt")
        r = self.b.request("UNLOCK", "/docs/intro.txt", headers={})
        self.assertEqual(r.status, 400)

    def test_unlock_token_bound_to_other_uri(self):
        t1 = self.lock_token(self.a, "/docs/intro.txt")
        # 令牌不能在别的 URI 上解除。
        r = self.a.unlock("/docs/notes.md", t1)
        self.assertEqual(r.status, 400)

    def test_root_infinity_protects_everything(self):
        self.lock_token(self.a, "/", depth="infinity")
        self.assertEqual(self.b.lock("/docs/intro.txt").status, 423)
        self.assertEqual(self.b.lock("/docs/").status, 423)
        self.assertEqual(self.b.lock("/journal/log.txt").status, 423)
        etag = self.b.get("/journal/log.txt").etag
        self.assertEqual(
            self.b.put("/journal/log.txt", "x", etag=etag).status, 423
        )
