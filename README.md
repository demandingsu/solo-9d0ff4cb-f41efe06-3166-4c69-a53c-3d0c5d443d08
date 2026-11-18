# Library Journal Access API

高校图书馆期刊可访问性判定服务。把出版商/数据库商提供的期刊覆盖表按**资源包**整理进库，
检索系统用「查询日 + ISSN」查出**当日实际可打开的出版日期段**及其来源包；
学科馆员还可以按 DOI 录入**文章元数据**（ISSN、出版日、作者编号列表），
并查询两位作者之间**最少合著跳数**的连接，每一跳都附一篇读者当日真能打开的文章
及其资源包依据。

技术栈：FastAPI + SQLite（WAL 模式），Python 标准库 `sqlite3`，无 ORM。

## 快速启动

```bash
docker compose up --build
# 打开 http://localhost:8000/docs （Swagger UI，可直接操作）
```

本地开发（无需 Docker）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# 测试：pip install pytest httpx && python -m pytest tests/ -v
```

SQLite 数据文件位置由环境变量 `DB_PATH` 决定（Compose 中为 `/data/library.db`，挂在命名卷上）。

## 概念与规则

- **资源包 package**：有名称、授权起止日（`license_start`/`license_end`，闭区间）、
  启用/停用标志。只有**查询日当天处于授权期内且未停用**的包才参与判定。
- **覆盖快照 coverage snapshot**：按包整体上传，每条记录 = ISSN + 出版日期范围 +
  最近禁访月数 `embargo_months`。`PUT .../coverage` 是**全量替换**，在一个事务里
  删旧插新。
- **移动禁访墙**：禁访边界随查询日移动。查询日为 `D`、禁访 `M` 个月时，该条覆盖
  可访区间 = `[start_date, min(end_date, D − M个月)]`；区间为空则该条当日不可访。
  月份相减按日历月，日数超出目标月则截到月末（如 3 月 31 日 − 1 月 = 2 月 28 日）。
- **拆段**：多个包覆盖同一期刊时，把可访区间按「来源包集合」切分——集合发生变化的
  边界处拆段，每段列出支撑它的全部来源包。停用某包只是把它从参与集合中移除，
  其他包的覆盖行原样保留，重叠段的依据不丢。
- **一致性**：每次成功写入（建包、改授权、换快照、启停用、文章录入/更正/撤回）在同一
  事务里把全局 `data_version` 加一；写入失败整体回滚，读者继续看到旧版本。每次查询在
  **单个读事务**内完成（WAL 快照读），响应里的 `data_version` 就是本次答案实际依据的
  提交版本——并发写不会把新旧来源混进同一个答案。

### 文章元数据与合著链

- **文章 article**：以 **DOI** 为主键（接受 `10.1000/x`、`doi:10.1000/x`、
  `https://doi.org/10.1000/x`，库内统一去前缀、小写化），记录 ISSN、出版日和**按提交
  顺序保存的作者编号列表**（同一篇内不允许重复作者）。
- **录入即更正**：`PUT /articles/{doi}` 是全量替换——ISSN、出版日、作者列表整体换新，
  在一个事务内删旧插新；首次提交返回 201，再次提交同一 DOI 返回 200。撤回后的 DOI
  重新提交会自动恢复为 active。
- **撤回 withdraw**：`POST /articles/{doi}/withdraw` 把文章标记为 `withdrawn`，记录保留
  但不再为任何合著跳提供依据；重复撤回幂等，不重复增加版本号。
- **当日可访判定**：一篇文章当日可访，当且仅当存在至少一个**启用且授权覆盖查询日**的包，
  其 coverage 行的出版日期范围包含文章出版日，且文章出版日不晚于
  `查询日 − embargo_months`（与 `/access` 同一套移动禁访墙规则）。
- **真实来源**：一篇文章同时被多个包覆盖时，`sources` 列出**全部**满足条件的包，
  每个来源附 coverage 起止、禁访月数与当日禁访截断日；被禁访墙挡住的包不会出现。
- **合著链查询**：`GET /coauthor-chain?author_a=...&author_b=...&date=...` 在「当日可访
  文章」构成的作者图上做 **BFS**，返回最少跳数的一条连接（等跳路径按作者号/DOI
  确定性排序，保证同输入同输出）。响应含作者路径、每跳的文章 DOI 与来源依据；
  没有连接（含作者编号不存在）时 `connected: false` 并给出明确 `message`，绝不报错或
  编造路径；两位作者相同返回 0 跳。
- **失效与改道**：撤回/更正文章、修改资源包授权或启停用、改变查询日导致禁访边界移动后，
  失效跳对应的文章直接退出候选集，BFS 自动改用其他可访文献或重新找链；找不到则明确
  告知无连接。
- **同版本一致性**：链路查询在单个读事务内同时读取文章、作者、coverage 与授权状态，
  路径上的每篇文献和每个来源依据都来自响应中 `data_version` 指向的同一个已提交状态，
  文章更新与覆盖修改交错时不会出现新文章配旧授权的混用。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/packages` | 登记资源包（名称 + 授权起止日） |
| GET | `/packages` / `/packages/{id}` | 查看包（含覆盖行数） |
| PUT | `/packages/{id}/license` | 修改授权起止日（事务提交） |
| PUT | `/packages/{id}/coverage` | 全量替换该包覆盖快照（原子） |
| GET | `/packages/{id}/coverage` | 查看该包当前快照 |
| POST | `/packages/{id}/deactivate` / `activate` | 停用 / 重新启用 |
| GET | `/access?issn=...&date=...` | 查询可访出版日期段及来源包 |
| PUT | `/articles/{doi}` | 按 DOI 录入/更正文章（ISSN、出版日、作者编号列表），201 新建 / 200 更正 |
| POST | `/articles/{doi}/withdraw` | 撤回文章（幂等，记录保留） |
| GET | `/articles` / `/articles/{doi}` | 列表（可按 `status` 过滤、分页）/ 单篇查看 |
| GET | `/coauthor-chain?author_a=...&author_b=...&date=...` | 最少合著跳数连接 + 每跳可访文章与来源依据 |

ISSN 接受 `0028-0836` / `00280836` / 末位 `X`（大小写均可），库内统一存 `XXXX-XXXX`
大写形式；只校验格式，不校验校验位（出版商表格常有脏数据）。

### 示例

```bash
# 1. 登记两个包
curl -X POST localhost:8000/packages -H 'Content-Type: application/json' \
  -d '{"name":"Elsevier-ScienceDirect","license_start":"2024-01-01","license_end":"2026-12-31"}'
curl -X POST localhost:8000/packages -H 'Content-Type: application/json' \
  -d '{"name":"JSTOR-Archive","license_start":"2024-01-01","license_end":"2026-12-31"}'

# 2. 上传覆盖快照（同一 ISSN，禁访月数不同）
curl -X PUT localhost:8000/packages/1/coverage -H 'Content-Type: application/json' \
  -d '{"entries":[{"issn":"0028-0836","start_date":"2010-01-01","end_date":"2026-12-31","embargo_months":0}]}'
curl -X PUT localhost:8000/packages/2/coverage -H 'Content-Type: application/json' \
  -d '{"entries":[{"issn":"0028-0836","start_date":"2015-01-01","end_date":"2026-12-31","embargo_months":12}]}'

# 3. 查询
curl 'localhost:8000/access?issn=0028-0836&date=2026-11-18'
```

响应（注意禁访墙随查询日移动、来源变化处拆段）：

```json
{
  "issn": "0028-0836",
  "date": "2026-11-18",
  "data_version": 4,
  "segments": [
    {"start": "2010-01-01", "end": "2014-12-31", "sources": ["Elsevier-ScienceDirect"]},
    {"start": "2015-01-01", "end": "2025-11-18", "sources": ["Elsevier-ScienceDirect", "JSTOR-Archive"]},
    {"start": "2025-11-19", "end": "2026-11-18", "sources": ["Elsevier-ScienceDirect"]}
  ]
}
```

### 文章录入与合著链示例

```bash
# 录入三篇文章（DOI 可带 URL 前缀，服务端规范化）
curl -X PUT localhost:8000/articles/10.1000/nature.a -H 'Content-Type: application/json' \
  -d '{"issn":"0028-0836","publication_date":"2022-03-01","authors":["alice","bob"]}'
curl -X PUT localhost:8000/articles/https://doi.org/10.1000/nature.b \
  -H 'Content-Type: application/json' \
  -d '{"issn":"00280836","publication_date":"2022-04-01","authors":["bob","carol"]}'
curl -X PUT localhost:8000/articles/10.1000/nature.c -H 'Content-Type: application/json' \
  -d '{"issn":"0028-0836","publication_date":"2022-05-01","authors":["alice","carol"]}'

# 最少跳数连接（alice-carol 直接合著，且该文同时被两个包覆盖）
curl 'localhost:8000/coauthor-chain?author_a=alice&author_b=carol&date=2026-11-18'

# 撤回直连文章后，链路自动改道 alice -> bob -> carol
curl -X POST localhost:8000/articles/10.1000/nature.c/withdraw
curl 'localhost:8000/coauthor-chain?author_a=alice&author_b=carol&date=2026-11-18'
```

合著链响应（节选；`sources` 为当日全部真实来源，含禁访截断日）：

```json
{
  "author_a": "alice", "author_b": "carol", "date": "2026-11-18",
  "data_version": 8, "connected": true, "hops": 2,
  "path": ["alice", "bob", "carol"],
  "chain": [
    {
      "from_author": "alice", "to_author": "bob",
      "article_doi": "10.1000/nature.a", "issn": "0028-0836",
      "publication_date": "2022-03-01",
      "sources": [
        {"package_id": 1, "package": "Elsevier-ScienceDirect",
         "coverage_start": "2010-01-01", "coverage_end": "2026-12-31",
         "embargo_months": 0, "embargo_cutoff": "2026-11-18"}
      ]
    }
  ]
}
```

无连接时明确说明（HTTP 仍为 200，业务结果在 `connected`/`message` 中）：

```json
{"connected": false, "hops": 0, "path": [], "chain": [],
 "message": "no connection on the query date: no path of co-authored, openable articles ..."}
```

## 项目结构

```
app/
  main.py     FastAPI 路由与事务边界（含文章录入/撤回、合著链）
  db.py       SQLite 连接、schema、读/写事务、版本计数
  logic.py    纯函数：月份相减、禁访墙、区间拆段、文章可访来源、合著 BFS
  schemas.py  Pydantic 请求/响应模型（含 ISSN/DOI 规范化）
tests/
  test_logic.py        拆段与禁访墙单测
  test_chain_logic.py  文章可访来源与合著 BFS 单测
  test_api.py          覆盖/授权 API 集成测试（重叠覆盖、失败回滚、并发一致性）
  test_articles_api.py 文章与合著链集成测试（多证据、撤回/授权/禁访改道、交错写入）
Dockerfile / docker-compose.yml / requirements.txt
```

## 验证情况（如实说明）

**已实现并验证**（`pytest` 33 项全过 + uvicorn 实跑 curl 十场景冒烟）：

- 按包上传全量覆盖快照、登记/修改授权起止日；
- 只采用查询当日有授权且未停用的包（授权前/后查询返回空）；
- 禁访边界随查询日移动（含月末截断、整段被禁访为空）；
- 多包重叠覆盖按来源集合变化拆段，不同禁访月数也正确拆段；
- 停用包后其他包的覆盖依据保留，重新启用后恢复；
- 非法快照（日期倒置、ISSN 格式错）整体 422 拒绝，`data_version` 不变、旧数据照常服务；
- 非法授权修改（起晚于止）422 拒绝，旧授权继续生效；
- 并发读写在替换快照期间，每个响应的 `data_version` 与其段内容严格对应，未观察到新旧混用；
- 按 DOI 录入/更正/撤回文章（含 DOI URL 前缀规范化、作者去重与非空校验、撤回幂等、
  撤回后重提自动恢复）；非法录入 422 且旧记录与旧链路结果保持不变；
- 合著链 BFS 返回最少跳数路径，每跳附当日可访文章与**全部真实来源包**（含 coverage
  起止、禁访月数、禁访截断日）；同篇多包覆盖时来源完整列出，被禁访墙挡住的包不出现；
- 撤回直连文章后自动改道其他文献；授权窗口修改、包停用、查询日移动禁访边界后，失效跳
  退出并重新找链，全部来源失效时明确返回 `connected: false`；未知作者与孤岛作者均明确
  说明；同一作者查询返回 0 跳；
- 文章更新与覆盖/授权修改交错并发时，每条链路响应内部自洽且 `data_version` 唯一对应
  一个已提交状态（并发测试 4 读者 × 60 轮，未见新旧混用）；
- `/docs`（Swagger UI）与 `/openapi.json` 可用，新接口均可在 Swagger 直接录入、查询。

**未验证 / 限制**：

- 本环境无 Docker，`Dockerfile` / `docker-compose.yml` 经静态核对（基础镜像、依赖版本与
  本地实测一致的 `fastapi==0.115.6` / `uvicorn==0.32.1`、启动命令与本地实跑相同；本次新增
  仅用标准库 `datetime`，无新依赖），但未实际构建运行；
- 本地环境为 Python 3.11（Docker 镜像仍为 3.12），依赖以隔离目录方式安装，未改动
  项目 `requirements.txt`；
- SQLite 单写者，适合馆内单实例部署；多实例水平扩展需换外部数据库；
- ISSN 只校验格式不校验校验位；DOI 只做形态与前缀规范化，不联网解析；合著 BFS 在
  请求内全量扫描文章表，数据量极大时需加分页/图缓存；覆盖快照与文章录入未做鉴权
  （内网部署假设）。
