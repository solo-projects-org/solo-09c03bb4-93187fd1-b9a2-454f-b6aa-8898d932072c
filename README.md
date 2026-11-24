# 路灯分批换装节电量核算服务

FastAPI + SQLite 的路灯换装节电核算接口。按路段记录换灯日和逐日单灯耗电量，
用同期未换灯的对照路段扣掉季节性/用电量变化，再给出目标路段的净节电量。
所有报告是出具时刻的不可变快照；补正数据后受影响报告标为 `pending_recalc`，
原结果继续可查。

## 启动

Docker Compose：

```bash
docker compose up --build
```

- Swagger UI: http://127.0.0.1:8000/docs
- 健康检查: http://127.0.0.1:8000/healthz
- SQLite 文件在命名卷 `lights-data` 的 `/data/lights.db`

本地开发（不依赖 Docker）：

```bash
pip install -r requirements-dev.txt
LIGHTS_DB_PATH=./data/lights.db uvicorn app.main:app --reload
pytest -q
```

`scripts/demo.py` 对运行中的服务做一次完整试算 + 补正，可核对数字：

```bash
python scripts/demo.py --base-url http://127.0.0.1:8000
```

## 口径

- **比较窗口**：换灯日前 7 个日历日 + 换灯日后 7 个日历日，换灯日当天跳过。
  日 `d` 的换灯用前段为 `[d-7, d-1]`，后段为 `[d+1, d+7]`。
- **对照路段**：未填换灯日，或换灯日晚于后段末日（`d+7`），并且 14 天窗口内
  每天都有读数。没有换灯日的路段按“到后段末日仍未换灯”处理。
- **节电量**（单灯日均，kWh/灯·天）：
  - 目标减少量 = 目标前段日均 − 目标后段日均
  - 对照平均减少量 = 各对照路段减少量的算术平均
  - 净节电 = 目标减少量 − 对照平均减少量
- **暂缓出报告**：目标路段没填换灯日、窗口内有缺数、或没有任何合格对照路段。
  返回 `status=deferred` 和 `reasons`，不落报告。
- **报告内容**：比较日期（前后段起止 + 跳过日）、目标与全部对照的逐日读数及
  前后段均值、对照名单、净节电量、出具时间。
- **补正**：改换灯日（`PATCH`）、改/补日报（`PUT`）、删错误日报（`DELETE`）。
  每次补正只重新读取**被改路段**的读数，与历史报告快照逐日比对：
  - 目标：窗口变化或所用读数变化 → 待复算；
  - 对照：读数变化、不再合格（换灯日落入窗口/缺数）→ 待复算；
  - 原本不合格、补正后变合格的对照会被计入 → 待复算（对照名单变化）。
- **待复算**：报告 `status` 变 `pending_recalc`，记录 `stale_at`、
  `stale_reasons`、`stale_segment_ids`；原快照字段不变，详情接口照常返回。

## 用数分歧核对与索引补建

审计员要按路段查清“同一天的抄表用数在不同节电报告里是否被引用成不同值”。
核对只认**报告出具时的不可变快照**（含 `pending_recalc` 待复算报告），不读
现行 `readings` 日报，也不改任何旧报告；查询路径上不逐份解析快照 JSON。

### 数据结构

- `report_reading_usage`：从报告快照抽取的用数索引，每行是
  `(报告, 路段, 日期, kWh/灯·天, target/control 角色)`，主键
  `(report_id, segment_id, reading_date)`。新报告在出报告的同一个
  `BEGIN IMMEDIATE` 事务里同步写入，因此**新报告出具后立即纳入核对**。
- `usage_index_backfill_state`：单行补建状态，保存高水位
  `last_processed_report_id`、批次大小、状态（`running`/`completed`）和
  `last_error`。

### 旧库分批补建

特性上线前已存在的旧报告没有索引，按批补建：

- 每批**最多读取 100 份**旧报告（`batch_size` 上限 100），只挑选高水位之上、
  索引里尚不存在的报告；补建期间新出具的报告已由出报告事务建好索引，会被
  自动跳过，不会重复解析其快照。
- 每批是一个独立写事务：先解析快照写索引，成功后才推进高水位。任一步失败，
  整批回滚、**进度不推进**，错误记入 `last_error`；重启后状态在 SQLite 里
  持久保留，下一次调用从同一批续补。各批之间勘误和出报告照常进行。
- 触发方式为显式调用 `POST /api/usage-index/backfill/batches`（Swagger 可
  逐批点击，便于演示补建期间的查询）；传 `max_batches` 可一次跑多批直到完成。

### 核对口径（`GET .../usage-discrepancies?start=&end=`）

- 入参为路段 + 闭区间日期范围，**最多 31 个日历日**（超出或 `end<start` 返回 422）。
- 整个查询在**单一只读事务**内完成，待查标记与分歧结论来自同一已提交状态。
- **待查（`pending_check_dates`，不下结论）**：某天落在任一**未补建旧报告**
  的前段 `[before_start, before_end]` 或后段 `[after_start, after_end]` 内。
  换灯跳过日不属于任何窗口，不会被标待查。判定只用报告表的窗口列，不解析快照。
  这是保守口径：未补建报告是否真的引用了该路段要等补建后才知道，因此宁可待查。
- 其余日期按索引核对，逐日给出：
  - `discrepancy`：同一天存在不同用数，按读数分组，每组列出引用它的
    `report_id`、`role`（target/control）和报告当前 `status`
    （`issued`/`pending_recalc` 都在列）；
  - `consistent`：所有报告引用同一用数；
  - `no_data`：没有任何报告在该天引用该路段（含换灯跳过日）。

### Swagger 演示（补建期间查询）

```bash
docker compose up --build
# 1) 播种 250 份“旧报告”（含一处真实分歧），并把它们重置为未补建状态
docker compose exec api python scripts/seed_legacy_reports.py
# 2) Swagger 打开 http://127.0.0.1:8000/docs ：
#    GET  /api/usage-index/backfill                         看剩余份数
#    POST /api/usage-index/backfill/batches?max_batches=1   每次补 100 份
#    GET  /api/segments/{对照路段id}/usage-discrepancies
#         ?start=2026-03-03&end=2026-03-17                  补建期间窗口日全部 pending_check
# 3) 再点几次补建批次直到 status=completed，重跑核对：
#    2026-03-13 返回 discrepancy（9.7 vs 9.0 两组报告号 + 角色）
```

## 并发正确性

出报告和所有补正都使用 `BEGIN IMMEDIATE` 显式写事务。SQLite 会把两者串行化：
补正要么在出报告的快照读取前提交（新报告直接采用新值），要么在报告插入后执行
（一定能看到并标记该报告），不存在“报告插入前读取、补正提交后漏标”的交错窗口。
测试 `test_correction_interleaved_with_report_issuance_is_not_missed` 用线程和
快照读钩子重现了该交错。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查 |
| POST | `/api/segments` | 建路段；`retrofit_date` 省略/null 表示未换灯 |
| GET | `/api/segments` | 路段列表 |
| GET | `/api/segments/{id}` | 路段详情 |
| PATCH | `/api/segments/{id}` | 补正换灯日（`null` 清空），返回影响报告 |
| GET | `/api/segments/{id}/readings` | 日报查询，可带 `start`/`end` |
| PUT | `/api/segments/{id}/readings` | 批量新增/覆盖日报 |
| DELETE | `/api/segments/{id}/readings/{date}` | 删除单日错误日报 |
| POST | `/api/reports?segment_id=` | 出报告（201）或暂缓（200 + `deferred` + reasons） |
| GET | `/api/reports` | 报告列表，可按目标路段/状态筛选 |
| GET | `/api/reports/{id}` | 报告详情（含快照、待复算原因） |
| GET | `/api/usage-index/backfill` | 用数索引补建进度 |
| POST | `/api/usage-index/backfill/batches` | 跑至多 `max_batches` 个补建批次（每批 ≤100 份） |
| GET | `/api/segments/{id}/usage-discrepancies` | 按路段核对 ≤31 天内报告引用用数的分歧 |

补正响应统一含：`changed`、`changed_readings`、`affected_reports`
（`report_id` / `previous_status` / `status`）、`impact`（复核报告数、新标记数、
已待复算数、未受影响数、本次重读路段——只有被补正的那个路段）。

## Swagger 试算 + 补正示例

`POST /api/segments`

```json
{"name": "人民路", "retrofit_date": "2026-03-10"}
```

`PUT /api/segments/1/readings`

```json
{"readings":[
  {"date":"2026-03-03","kwh_per_lamp":10.0},
  {"date":"2026-03-04","kwh_per_lamp":10.0},
  {"date":"2026-03-05","kwh_per_lamp":10.0},
  {"date":"2026-03-06","kwh_per_lamp":10.0},
  {"date":"2026-03-07","kwh_per_lamp":10.0},
  {"date":"2026-03-08","kwh_per_lamp":10.0},
  {"date":"2026-03-09","kwh_per_lamp":10.0},
  {"date":"2026-03-11","kwh_per_lamp":8.0},
  {"date":"2026-03-12","kwh_per_lamp":8.0},
  {"date":"2026-03-13","kwh_per_lamp":8.0},
  {"date":"2026-03-14","kwh_per_lamp":8.0},
  {"date":"2026-03-15","kwh_per_lamp":8.0},
  {"date":"2026-03-16","kwh_per_lamp":8.0},
  {"date":"2026-03-17","kwh_per_lamp":8.0}
]}
```

对照路段不填 `retrofit_date`，同样补齐 14 天读数，然后
`POST /api/reports?segment_id=1`。
补正对照读数：`PUT /api/segments/2/readings` 只传被改的那一天，响应
`affected_reports` 会列出被标为 `pending_recalc` 的报告；`GET /api/reports/{id}`
仍能看到原始净节电量与补正原因。

## 测试

```bash
pytest -q
```

覆盖：窗口边界与跳过当天、对照筛选（未换灯 / 后段末日后换灯 / 缺数排除）、
净节电算术、目标缺数与无对照暂缓、报告快照不可变、换灯日/目标读数/对照读数/
对照增减各类补正标记、只重读被改路段、重复补正不重复标记、重复日期 upsert、
以及出报告与补正并发的交错场景。

`tests/test_usage_audit.py` 覆盖用数索引与核对：新报告即时入索引并查出分歧、
待复算报告仍参与、分批补建与完成、**失败批次回滚且不推进进度并记录
`last_error`**、重启后续补、补建期间仅窗口覆盖日标待查、查询单事务同快照、
补建期间新报告即时纳入且不被重复解析、31 天边界与 404/422 校验、批次上限
100、核对不触碰 `readings` 表、补建不改动旧报告。
