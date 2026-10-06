"""解析 RFC 4918 的 If 请求头。

本工作区明确只支持 **无标签（untagged）条件列表**，且条件只允许正向的
锁令牌 (<opaquelocktoken:...>) 与 ETag ("...")：

    If: (<token> ["etag"]) ( ... )
        └─ 同一列表内 AND ─┘ └ 多个列表按 OR

显式不支持并返回 400 的情况：
* "Not" 取反条件；
* 带资源标签的列表 (例如 ``<http://host/file> (<token>)``)；
* 任何语法不完整的表达式。
"""

import re

_TOKEN_RE = re.compile(r"<([^>]*)>")
_ETAG_RE = re.compile(r'"((?:[ !#-~]|"")*)"')


class IfHeaderError(ValueError):
    """If 头语法非法或使用了明确不支持的特性。"""


class _Scanner:
    def __init__(self, text):
        self.text = text
        self.pos = 0

    def skip_space(self):
        n = len(self.text)
        while self.pos < n and self.text[self.pos] in " \t":
            self.pos += 1

    def starts(self, literal):
        return self.text.startswith(literal, self.pos)

    def match_word(self, word):
        """大小写不敏感地吃掉一个由分隔符界定的关键字。"""
        end = self.pos + len(word)
        if self.text[self.pos:end].lower() != word.lower():
            return False
        if end < len(self.text):
            nxt = self.text[end]
            if nxt not in " \t()":
                return False
        self.pos = end
        return True


def _read_token(scanner):
    m = _TOKEN_RE.match(scanner.text, scanner.pos)
    if not m or not m.group(1).strip():
        raise IfHeaderError("malformed lock token in If header")
    scanner.pos = m.end()
    return m.group(1)


def _read_etag(scanner):
    m = _ETAG_RE.match(scanner.text, scanner.pos)
    if not m:
        raise IfHeaderError("malformed entity-tag in If header")
    scanner.pos = m.end()
    return '"%s"' % m.group(1)


def _read_entity_tag(scanner):
    """一个实体标签必须是强 ETag；W/ 弱 ETag 在锁场景明确不支持。"""
    if scanner.starts("W/") or scanner.starts("w/"):
        raise IfHeaderError("weak entity tags are not supported in If header")
    return _read_etag(scanner)


def _read_resource_tag(scanner):
    _read_token(scanner)


def _read_state_token(scanner):
    token = _read_token(scanner)
    if not token.lower().startswith("opaquelocktoken:"):
        raise IfHeaderError("only opaquelocktoken state tokens are supported")
    return ("token", token)


def _read_condition(scanner):
    scanner.skip_space()
    negated = False
    if scanner.match_word("not"):
        negated = True
        scanner.skip_space()
    if scanner.starts("<"):
        cond = _read_state_token(scanner)
    elif scanner.starts("["):
        raise IfHeaderError("ETag lists using '[ ]' are not supported")
    elif scanner.starts('"') or scanner.starts("W/") or scanner.starts("w/"):
        cond = ("etag", _read_entity_tag(scanner))
    else:
        raise IfHeaderError("expected a state token or entity-tag in If header")
    if negated:
        raise IfHeaderError("negated (Not) conditions are not supported")
    return cond


def _read_untagged_list(scanner):
    if not scanner.starts("("):
        raise IfHeaderError("expected '(' to begin an untagged condition list")
    scanner.pos += 1
    conditions = []
    while True:
        scanner.skip_space()
        if scanner.pos >= len(scanner.text):
            raise IfHeaderError("unterminated condition list")
        if scanner.starts(")"):
            scanner.pos += 1
            break
        conditions.append(_read_condition(scanner))
    if not conditions:
        raise IfHeaderError("empty condition list")
    return conditions


def parse_if_header(value):
    """解析 If 头，返回条件列表的列表（OR-AND 结构）。

    每个条件是 ``("token", token)`` 或 ``("etag", etag)``，etag 含引号。
    空值 / 仅空白返回 ``None``，表示调用方应当当作没有提供 If 头。
    """
    if value is None or not value.strip():
        return None
    scanner = _Scanner(value)
    lists = []
    while True:
        scanner.skip_space()
        if scanner.pos >= len(scanner.text):
            break
        # 带标签列表：<resource-tag> ( ... ) —— 明确不支持。
        if scanner.starts("<"):
            save = scanner.pos
            token = _read_token(scanner)
            scanner.skip_space()
            if scanner.starts("("):
                raise IfHeaderError(
                    "tagged condition lists are not supported: %s" % token
                )
            scanner.pos = save
        lists.append(_read_untagged_list(scanner))
    if not lists:
        raise IfHeaderError("no condition lists found in If header")
    return lists
