# 受限 WebDAV 文本工作区

一个刻意保持极小的 WebDAV 文本工作区：SQLite 保存**固定目录树、正文、ETag 与
锁**；服务端只支持已有资源的 `GET / PUT / LOCK / UNLOCK`，集合（目录）只用于
锁定，不做创建、移动、共享锁或账号系统。Docker Compose 一条命令启动服务和一个
轻量编辑页。

## 安全语义（设计目标）

- **目录锁不可被绕过**：集合的 `Depth: infinity` 独占锁保护其全部后代；他人
  无法直接 `PUT` 子文件。后代已持有冲突锁时，祖先的深度 `LOCK` 必须失败。
- **锁令牌不可超权**：写入必须满足目标资源上的**全部覆盖锁**（直接锁 +
  祖先深度锁），仅持有一个别处的令牌不能写。
- **正确令牌也不能覆盖新版本**：`PUT` 同时做强 `If-Match` 校验与 If 条件
  校验；ETag 过期返回 `412`，锁不满足返回 `423`。
- **到期裁决不可被旧令牌翻盘**：过期令牌不能 `UNLOCK` 后来建立的新锁，也不
  能再写入。时间来自**可注入时钟**，刷新锁不生成新令牌。
- **单事务裁决**：锁状态、到期清理、条件验证、正文与版本更新在同一笔
  `BEGIN IMMEDIATE` 事务内完成；任何失败都不改正文、不推进版本。

## If 头支持范围（明确收窄）

只支持 RFC 4918 If 头的**无标签条件列表**，条件只允许**正向**锁令牌与
（强）ETag：

```
If: (<opaquelocktoken:…> "ETag")   # 同一列表内 AND
If: (<token-A>) (<token-B>)        # 多个列表之间 OR
```

以下一律 `400`：`Not` 取反条件、带资源标签的列表
（`<http://…/file> (<…>)`）、弱 ETag（`W/"…"`）、方括号 ETag 列表、
任何语法残缺。

## 方法与状态码

| 操作 | 行为 |
|---|---|
| `GET /path/file` | 返回正文与强 `ETag`；集合 `405`；不存在 `404` |
| `PUT /path/file` | 仅已有文件；锁 + If + 强 `If-Match` 全过才更新，成功 `204` |
| `LOCK /path` | 仅独占写锁；集合允许 Depth `0`/`infinity`（默认 `infinity`），文件恒为 `0`；新锁 `201`，刷新 `200` |
| `UNLOCK /path` | 需 `Lock-Token: <…>`；成功 `204`，令牌过期 `409`，令牌不属于该 URI `400` |
| 其它（MKCOL/MOVE/COPY/DELETE/PROPFIND/POST/OPTIONS/HEAD） | `405`，带 `Allow: GET, PUT, LOCK, UNLOCK` |

ETag 形如 `"1"、"2"…`，每次成功 `PUT` 版本号 +1。锁默认超时 300 秒
（`Timeout: Second-N` / `Infinite`，服务端上限 1 小时）。

## 启动

```bash
docker compose up --build
# 编辑页：    http://localhost:8080/__editor__
# 示例文件：  http://localhost:8080/docs/intro.txt
```

SQLite 数据库存放在命名卷 `webdav-data`（容器内 `/data/workspace.db`），
首次启动时写入固定目录树：

```
/
├── docs/{intro.txt, notes.md}
└── journal/log.txt
```

本地无容器运行（仅用标准库，需 Python 3.10+）：

```bash
DB_PATH=./data/workspace.db LISTEN_PORT=8080 python3 -m app
```

## 编辑页流程

1. 输入已有资源路径，**取得文件和 ETag**；
2. **锁定**（LOCK Depth:0，120 秒超时，可刷新，刷新不换令牌）；
3. 在文本框编辑；
4. **保存**发送 `PUT`，带强 `If-Match` 与 `If: (<锁令牌> "ETag")`；
5. 冲突（`412`）时**本地草稿原样保留**，页面下方展示服务器**权威版本**与
   其 ETag，人工合并后再保存；`423` 表示锁已失效（到期/被祖先锁挡住），
   需重新锁定。打开两个浏览器窗口即可模拟两个客户端。

## 测试

测试通过真实 HTTP socket（两个独立连接的客户端）+ 可注入的假时钟，覆盖
目录锁/子锁交错、祖先-后代冲突、到期重锁、旧令牌解新锁失败、同版本写入
冲突、OR/AND 条件列表，以及完整页面保存流程：

```bash
python3 -m unittest discover -s tests -v
# 52 tests … OK
```

## 代码结构

```
app/
  conditions.py   # If 头词法/语法解析（只接受无标签正向列表）
  workspace.py    # 核心裁决：事务、锁、ETag、固定树（无 HTTP 依赖，时钟可注入）
  server.py       # 标准库 HTTP 层 + LOCK 响应 XML
  static/         # 轻量编辑页（无第三方依赖）
  __main__.py     # 入口
tests/            # 双客户端交错测试 + 解析器单测 + 事务竞争用例
```
