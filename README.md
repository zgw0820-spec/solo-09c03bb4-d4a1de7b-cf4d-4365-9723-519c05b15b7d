# 设备删除只读预检 API

库管清理误建设备前，先凭 **表名 + 主键值** 预演删除：沿指向主键的外键查出
哪些巡检 / 复测记录会被级联带走、哪些交接单会挡住删除。服务全程只读，
不执行任何 DELETE，整份回执取自同一数据库快照。

## 1. 支持范围与判定规则

**仅处理**：普通表（`relkind=r`）、单列主键、外键被引用列恰好是该单列主键。

**删除动作只分析两种**：

| 动作 | 语义 | 本服务处理 |
| --- | --- | --- |
| `ON DELETE CASCADE` | 引用行随父行删除 | 纳入拟删除集，继续向下传播 |
| `ON DELETE NO ACTION` | 约束检查时报错 | 引用行若也在拟删除集中则不阻断；否则列为阻断引用。可延迟约束（`DEFERRABLE`）按事务末 `COMMIT` 检查语义判定，回执标注 `deferrable / initially_deferred` |

**超出支持范围**（回执 `unsupported` 逐项说明原因，并抽样给出实际命中行）：

- 复合主键表 / 复合列外键 / 无主键表、非普通表（视图、分区表、外部表等）
- 外键指向非主键的唯一键
- `RESTRICT`、`SET NULL`、`SET DEFAULT` 动作

存在命中待删行的超范围边时，`summary.fully_analyzable=false`，
`deletable` 也不置真，提示人工核实，绝不给出虚假的“可删”结论。

**去重与环路**：删除集按 `(schema, 表) -> 主键值集合` 维护，固定点遍历，
同一行无论被环路还是多条级联路径命中只列一次。

## 2. 快照与并发

每次预检在一个 `REPEATABLE READ ... READ ONLY` 事务内完成全部查询，
整份回执对应单一快照；期间业务写入照常提交，既不被预检阻塞，也不会
污染回执（已用并发实测验证，见第 5 节）。

## 3. 目录结构

```
app/
  config.py     环境变量配置（DATABASE_URL / DEFAULT_SCHEMAS / 超时）
  catalog.py    pg_catalog 读取表、主键、外键元数据
  queries.py    按外键查命中行的只读 SQL（主键值按列实际类型转换）
  precheck.py   核心：边分类、删除集固定点遍历、阻断判定、超范围取证
  models.py     Pydantic 请求 / 响应模型
  main.py       FastAPI 入口、只读事务、连接池、异常处理
db/
  01_schema.sql 样例库结构（16 张表，覆盖全部场景）
  02_seed.sql   种子数据
scripts/
  http_regression.py       HTTP 端到端回归（26 项断言）
  delete_compare.py        预检预测 vs 真实 DELETE 对比（克隆库）
  snapshot_test.py         快照隔离单库实测
  http_concurrency_test.py HTTP 层并发写入实测
Dockerfile, docker-compose.yml, requirements.txt
```

## 4. 启动方式

### Docker Compose（交付方式）

```bash
docker compose up --build
# API:   http://localhost:8000
# Swagger: http://localhost:8000/docs
# PostgreSQL: localhost:5432 (device_ops / postgres / postgres)
```

样例库通过 `docker-entrypoint-initdb.d` 自动建表并灌入种子数据。

### 本地直跑（无 Docker 环境时）

```bash
python -m venv --without-pip .venv && .venv/bin/python get-pip.py   # 视环境而定
.venv/bin/pip install -r requirements.txt
# 准备 PostgreSQL 并加载 db/*.sql 后：
DATABASE_URL=postgresql://user:pass@host:5432/device_ops \
  .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 5. 接口与 Swagger 试查

`GET /docs` 为 Swagger UI，可直接试查。主要端点：

- `POST /api/v1/precheck`：批量预检（请求体 `{"targets":[{"schema":"public","table":"equipment","pk":"1"}]}`）
- `GET /api/v1/precheck?table=equipment&pk=1`：单目标便捷查询
- `GET /api/v1/tables`、`GET /api/v1/foreign-keys`：浏览目录
- `GET /healthz`：健康检查

回执结构（节选）：

```json
{
  "snapshot": {"isolation_level": "repeatable read", "read_only": true, "...": "..."},
  "targets": [{"schema": "public", "table": "equipment", "pk_column": "id", "pk_value": "1", "exists": true}],
  "planned_deletes": [{"schema": "public", "table": "inspection", "pk_column": "id",
                       "pk_values": ["101"], "row_count": 1}],
  "blocking_references": [{"schema": "public", "table": "handover", "pk_column": "id",
                           "pk_value": "201", "constraints": ["handover_equipment_id_fkey"],
                           "on_delete": "NO ACTION", "deferrable": false,
                           "initially_deferred": false, "references": {"via": ["..."]}}],
  "unsupported": [{"kind": "edge", "constraint": "...", "table": "...",
                   "reason": "ON DELETE SET NULL 不受支持...",
                   "matched_rows": [{"...": "..."}], "matched_row_count": 1, "truncated": false}],
  "notices": ["..."],
  "summary": {"tables_to_delete": 3, "rows_to_delete": 3, "blocking_references": 2,
              "unsupported_items": 4, "deletable": false, "fully_analyzable": false}
}
```

错误约定：入参类问题（表不存在、复合主键、主键值无法转列类型）返回 4xx，
数据库侧其他错误返回 502，均带结构化 `detail`。

## 6. 实测结果

实测环境：本机免 root PostgreSQL 15.20（Debian bookworm 包解包运行，端口 5433）、
Python 3.11 + FastAPI 0.165 + psycopg 3.3.9。Docker 守护进程与 Docker Hub
在本环境不可达，故容器链路以配置交付、未在本机启动；应用与 SQL 全部在
同版本 PG 上实测。

| 场景 | 预期 | 实测 |
| --- | --- | --- |
| 设备 1 | handover（不可延迟）+ work_order（可延迟事务末）双阻断；4 条超范围边带原因与取样行 | ✅ 与预期一致 |
| 设备 2 | 5 表级联全删；多路径命中的 attachment 只列 1 次；同删的 review 不算阻断；无超范围命中 | ✅ 与预期一致 |
| 设备 3 | 可延迟 NO ACTION 留存行 retest_review=602 在事务末阻断 | ✅ 与预期一致 |
| 设备 4 / 5 | 仅复合主键表 / SET NULL 超范围边命中，`fully_analyzable=false` | ✅ 与预期一致 |
| part 自引用环路 | 3 行同表去重，可删 | ✅ 与预期一致 |
| node_a/node_b 互指环路 | 两表各 1 行，可删 | ✅ 与预期一致 |
| 批量请求 | 重复目标去重、不存在主键跳过并提示 | ✅ 与预期一致 |
| 入参校验 | 复合主键表 / 不存在表 400；非法主键值 400（sqlstate 22P02） | ✅ 与预期一致 |

- **HTTP 回归**：`scripts/http_regression.py` —— **26/26 通过**。
- **真实 DELETE 对比**（克隆库 `device_ops_verify`，`SET CONSTRAINTS ALL DEFERRED`
  后真实执行）：5 个关键场景预测与 PostgreSQL 实际行为**全部一致**——
  预测阻断的删除均在预期约束上报错回滚；预测可删的删除成功提交，
  各表实际删除行数与预测逐表相等。
- **快照隔离**：预检事务进行中另一会话提交写入，事务内计数不变、
  新事务可见新行；HTTP 层 20 并发预检 + 同时写入，回执全程稳定。
- **只读核对**：全部测试后源库 16 张表行数与种子数据逐一比对，零变化。

## 7. 剩余事项与已知限制

1. **容器链路未在本环境实跑**：无 Docker 守护进程且 Docker Hub 不可达；
   `Dockerfile` / `docker-compose.yml` 已按标准写法交付并通过 YAML 校验，
   需在有 Docker 的环境执行一次 `docker compose up --build` 复验。
2. **超范围边只抽样 200 行**（`SAMPLE_LIMIT`），回执标注 `truncated`，
   不据此推断总行数；需要精确计数可后续加 `COUNT(*) EXPLAIN` 类旁路统计。
3. **删除集按批查询（每批 1000 个主键值）**，超大删除图会产生较多往返，
   目前靠 `statement_timeout` 兜底，未做专门的体量压测。
4. 分区表（`relkind=p`）当前归入超范围并说明原因；如后续需要支持，
   需额外处理分区键必须包含主键的约束与路由语义。
5. 服务只做预检，不提供删除执行接口；真正执行删除时仍应由业务侧在
   显式事务内完成，并以数据库约束检查为最终准绳。
