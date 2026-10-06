"""端到端主场景：两个客户端交错目录锁、子锁、到期重锁、同版本写入，
最后完整走一遍页面保存流程。只有仍持有效锁且版本未变的编辑才能落盘。
"""

import re

from tests.helpers import HttpServerTestBase


class TwoClientInterleaveTest(HttpServerTestBase):
    def test_directory_lock_sublock_expiry_relock_and_same_version_put(self):
        # ---- 阶段 1：A 对 /docs/ 加 infinity 独占锁 ---------------------
        ta = self.lock_token(self.a, "/docs/", depth="infinity",
                             timeout="Second-60")
        # B 不能锁定集合本身，也不能锁定其后代。
        self.assertEqual(self.b.lock("/docs/", depth="infinity").status, 423)
        self.assertEqual(self.b.lock("/docs/intro.txt").status, 423)

        # A 可以在自己的目录锁保护下写子文件（锁令牌 AND 当前 ETag）。
        etag_a = self.a.get("/docs/intro.txt").etag
        ok = self.a.put(
            "/docs/intro.txt", "A 在目录锁内写入\n",
            etag=etag_a,
            if_header=self.a.if_list(self.a.token_entry(ta),
                                     self.a.etag_entry(etag_a)),
        )
        self.assertEqual(ok.status, 204)
        # B 拿到 A 的新 ETag 仍不能写：没有有效锁令牌。
        etag_b = self.b.get("/docs/intro.txt").etag
        self.assertEqual(etag_b, ok.etag)
        self.assertEqual(
            self.b.put(
                "/docs/intro.txt", "B 强行写入",
                etag=etag_b,
                if_header=self.b.if_list(self.b.etag_entry(etag_b)),
            ).status,
            423,
        )

        # ---- 阶段 2：A 解锁目录，B 锁定单个子文件 -----------------------
        self.assertEqual(self.a.unlock("/docs/", ta).status, 204)
        tb = self.lock_token(self.b, "/docs/intro.txt", timeout="Second-60")

        # B 持子锁期间：A 的目录 infinity 锁必须失败（后代冲突）。
        self.assertEqual(self.a.lock("/docs/", depth="infinity").status, 423)

        # B 写成功；A 即使知道当前版本也写不了（无锁）。
        cur = self.a.get("/docs/intro.txt").etag
        self.assertEqual(
            self.a.put("/docs/intro.txt", "A 同版本但无锁", etag=cur).status,
            423,
        )
        good = self.b.put(
            "/docs/intro.txt", "B 在子锁内写入\n",
            etag=cur,
            if_header=self.b.if_list(self.b.token_entry(tb),
                                     self.b.etag_entry(cur)),
        )
        self.assertEqual(good.status, 204)

        # ---- 阶段 3：B 的锁到期，A 重新锁定并写入 -----------------------
        self.clock.advance(61)
        # B 拿旧令牌的任何动作都不再被承认。
        self.assertEqual(
            self.b.put(
                "/docs/intro.txt", "B 过期后写入",
                etag=good.etag,
                if_header=self.b.if_list(self.b.token_entry(tb),
                                         self.b.etag_entry(good.etag)),
            ).status,
            423,
        )
        self.assertEqual(self.b.unlock("/docs/intro.txt", tb).status, 409)
        # A 重锁成功。
        ta2 = self.lock_token(self.a, "/docs/intro.txt", timeout="Second-60")
        self.assertNotEqual(ta2, tb)

        stale = self.a.get("/docs/intro.txt").etag
        self.assertEqual(stale, good.etag)

        # 模拟"别人已更新"：A 先解锁，B 取锁写一版，再还给 A。
        self.assertEqual(self.a.unlock("/docs/intro.txt", ta2).status, 204)
        tb2 = self.lock_token(self.b, "/docs/intro.txt", timeout="Second-60")
        bumped = self.b.put(
            "/docs/intro.txt", "别人在 A 编辑期间推进的版本\n",
            etag=stale,
            if_header=self.b.if_list(self.b.token_entry(tb2),
                                     self.b.etag_entry(stale)),
        )
        self.assertEqual(bumped.status, 204)
        self.b.unlock("/docs/intro.txt", tb2)
        ta3 = self.lock_token(self.a, "/docs/intro.txt", timeout="Second-60")

        # A 持有效锁，但基于旧 ETag 写入必须失败：正确锁令牌也不能覆盖新版本。
        conflict = self.a.put(
            "/docs/intro.txt", "基于旧版本的编辑",
            etag=stale,
            if_header=self.a.if_list(self.a.token_entry(ta3),
                                     self.a.etag_entry(stale)),
        )
        self.assertEqual(conflict.status, 412)

        # 用权威 ETag 重放同一批编辑 -> 落盘成功。
        latest = self.a.get("/docs/intro.txt")
        merged = self.a.put(
            "/docs/intro.txt", "基于最新版本合并后的编辑\n",
            etag=latest.etag,
            if_header=self.a.if_list(self.a.token_entry(ta3),
                                     self.a.etag_entry(latest.etag)),
        )
        self.assertEqual(merged.status, 204)
        final = self.a.get("/docs/intro.txt")
        self.assertEqual(final.text, "基于最新版本合并后的编辑\n")
        self.assertEqual(final.etag, merged.etag)

        # 成功写入共 4 次（A 目录锁内、B 子锁内、B 期间推进、A 合并），
        # ETag 从 "1" 前进到 "5"；所有被拒写入都未推进版本。
        self.assertEqual(final.etag, '"5"')


class EditorPageFlowTest(HttpServerTestBase):
    """模拟页面：取文件+ETag -> LOCK -> 编辑 -> 保存；
    冲突时草稿保留并能展示权威版本。"""

    def _editor_lock(self, client, path):
        r = client.lock(path, depth="0", timeout="Second-120")
        self.assertIn(r.status, (200, 201))
        token = r.lock_token
        m = re.search(r"<D:timeout>([^<]+)</D:timeout>", r.text)
        self.assertTrue(m)
        return token, m.group(1)

    def test_full_page_save_flow_with_two_browser_windows(self):
        path = "/journal/log.txt"

        # 窗口 A：取得文件和 ETag，锁定，编辑但暂时不保存。
        load_a = self.a.get(path)
        self.assertEqual(load_a.status, 200)
        token_a, _ = self._editor_lock(self.a, path)

        # 窗口 B：同时也想编辑 -> 锁定失败（423）。
        load_b = self.b.get(path)
        self.assertEqual(load_b.etag, load_a.etag)
        self.assertEqual(self.b.lock(path, depth="0").status, 423)

        # A 用页面的保存头（If-Match + If: (token etag)）保存成功。
        saved = self.a.put(
            path, "窗口A的编辑\n",
            etag=load_a.etag,
            if_header="(<%s> %s)" % (token_a, load_a.etag),
        )
        self.assertEqual(saved.status, 204)
        self.assertNotEqual(saved.etag, load_a.etag)

        # A 解锁；B 取得锁并基于权威新版本再改。
        self.assertEqual(self.a.unlock(path, token_a).status, 204)
        load_b2 = self.b.get(path)
        self.assertEqual(load_b2.text, "窗口A的编辑\n")
        token_b, _ = self._editor_lock(self.b, path)
        saved_b = self.b.put(
            path, "窗口B在A基础上的编辑\n",
            etag=load_b2.etag,
            if_header="(<%s> %s)" % (token_b, load_b2.etag),
        )
        self.assertEqual(saved_b.status, 204)

        # A 回来（锁已不属于它），仍用旧 ETag/旧令牌保存 -> 被拒，
        # 随后 GET 取回权威版本展示给用户，本地草稿不动。
        rejected = self.a.put(
            path, "窗口A迟到的离线草稿\n",
            etag=load_a.etag,
            if_header="(<%s> %s)" % (token_a, load_a.etag),
        )
        self.assertEqual(rejected.status, 423)  # 锁无效优先裁决
        authority = self.a.get(path)
        self.assertEqual(authority.status, 200)
        self.assertEqual(authority.text, "窗口B在A基础上的编辑\n")
        self.assertEqual(authority.etag, saved_b.etag)
        # A 的草稿只是局部变量，从未落盘。
        self.assertNotIn("离线草稿", authority.text)

        # 只有"仍持有效锁且版本未变"才能落盘：B 用旧 ETag 再写 -> 412。
        stale = self.b.put(
            path, "B基于旧版本\n",
            etag=load_b2.etag,
            if_header="(<%s> %s)" % (token_b, load_b2.etag),
        )
        self.assertEqual(stale.status, 412)
        fresh = self.b.get(path)
        final = self.b.put(
            path, "B基于权威版本的最终编辑\n",
            etag=fresh.etag,
            if_header="(<%s> %s)" % (token_b, fresh.etag),
        )
        self.assertEqual(final.status, 204)
        self.assertEqual(self.b.get(path).text,
                         "B基于权威版本的最终编辑\n")

    def test_editor_assets_served(self):
        page = self.a.get("/__editor__")
        self.assertEqual(page.status, 200)
        self.assertIn("text/html", page.header("Content-Type"))
        js = self.a.get("/__editor__/app.js")
        self.assertEqual(js.status, 200)
        self.assertIn(b"If-Match", js.body)
        self.assertIn(b"authority", js.body)

    def test_editor_lock_state_endpoint(self):
        from urllib.parse import quote
        token = self.lock_token(self.a, "/docs/intro.txt",
                                timeout="Second-60")
        r = self.a.get("/__editor__/state?path="
                       + quote("/docs/intro.txt"))
        self.assertEqual(r.status, 200)
        self.assertIn(token, r.text)
        self.clock.advance(61)
        r2 = self.a.get("/__editor__/state?path="
                        + quote("/docs/intro.txt"))
        # 锁到期后状态中不再包含锁信息。
        self.assertNotIn('"token"', r2.text)
