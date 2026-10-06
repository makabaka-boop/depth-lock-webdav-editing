"""If 头解析：无标签正向列表、OR/AND；Not 与带标签列表明确拒绝。"""

import unittest

from app.conditions import IfHeaderError, parse_if_header


class ParseIfHeaderTest(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertIsNone(parse_if_header(None))
        self.assertIsNone(parse_if_header("   "))

    def test_single_token_list(self):
        lists = parse_if_header("(<opaquelocktoken:abc>)")
        self.assertEqual(lists, [[("token", "opaquelocktoken:abc")]])

    def test_token_and_etag_in_one_list_is_and(self):
        lists = parse_if_header('(<opaquelocktoken:t1> "v2")')
        self.assertEqual(
            lists,
            [[("token", "opaquelocktoken:t1"), ("etag", '"v2"')]],
        )

    def test_multiple_lists_are_or(self):
        lists = parse_if_header(
            '(<opaquelocktoken:t1>) (<opaquelocktoken:t2> "v3")'
        )
        self.assertEqual(len(lists), 2)
        self.assertEqual(lists[0], [("token", "opaquelocktoken:t1")])
        self.assertEqual(lists[1], [
            ("token", "opaquelocktoken:t2"), ("etag", '"v3"')
        ])

    def test_etag_with_doubled_quotes(self):
        # ETag 是 opaque quoted-string，双引号在内部以成对形式出现。
        lists = parse_if_header('(<opaquelocktoken:t1> "a""b")')
        self.assertEqual(lists[0][1], ("etag", '"a""b"'))

    def test_not_condition_rejected(self):
        with self.assertRaises(IfHeaderError):
            parse_if_header("(Not <opaquelocktoken:t1>)")

    def test_tagged_list_rejected(self):
        with self.assertRaises(IfHeaderError):
            parse_if_header("</docs/a.txt> (<opaquelocktoken:t1>)")

    def test_weak_etag_rejected(self):
        with self.assertRaises(IfHeaderError):
            parse_if_header('(W/"v1")')

    def test_malformed(self):
        bad = [
            "(<opaquelocktoken:t1>",       # 未闭合
            "<opaquelocktoken:t1>",        # 不是列表
            "()",                          # 空列表
            "(<opaquelocktoken:t1> extra", # 多余裸词
            '("v1"',                       # 未闭合
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(IfHeaderError):
                    parse_if_header(value)


if __name__ == "__main__":
    unittest.main()
