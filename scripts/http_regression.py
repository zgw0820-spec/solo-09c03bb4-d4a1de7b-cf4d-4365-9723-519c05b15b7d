"""HTTP 端到端回归：覆盖阻断 / 干净级联 / 事务末检查 / 环路 / 去重 / 校验 / 超范围。

前置：服务已在 127.0.0.1:8000 运行，样例库已加载 db/ 下脚本。
运行：python -I scripts/http_regression.py
"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def get(path):
    return call("GET", path)


def post(body):
    return call("POST", "/api/v1/precheck", body)


def names(rows, key="table"):
    return sorted(r[key] for r in rows)


checks = []


def check(name: str, cond: bool, detail=""):
    checks.append((name, cond, detail))


# 1. 设备1：handover 不可延迟阻断 + work_order 可延迟阻断 + 4 条超范围边
s, r = get("/api/v1/precheck?table=equipment&pk=1")
check("设备1 HTTP200", s == 200)
block_tables = names(r["blocking_references"])
check("设备1 阻断 handover+work_order", block_tables == ["handover", "work_order"])
check("设备1 work_order 标记可延迟",
      any(b["table"] == "work_order" and b["deferrable"]
          and b["initially_deferred"] for b in r["blocking_references"]))
check("设备1 handover 标记不可延迟",
      any(b["table"] == "handover" and not b["deferrable"]
          for b in r["blocking_references"]))
check("设备1 拟删仅级联表",
      names(r["planned_deletes"]) == ["equipment", "inspection", "retest"])
check("设备1 超范围4条", r["summary"]["unsupported_items"] == 4)
check("设备1 快照只读可重复读",
      r["snapshot"]["isolation_level"] == "repeatable read"
      and r["snapshot"]["read_only"] is True)

# 2. 设备2：干净级联，多路径 attachment 只列一次，引用行同删不阻断
s, r = get("/api/v1/precheck?table=equipment&pk=2")
check("设备2 无阻断", r["summary"]["blocking_references"] == 0)
check("设备2 拟删5表且 attachment 仅1行",
      names(r["planned_deletes"]) == [
          "equipment", "inspection", "inspection_attachment", "retest", "retest_review"]
      and next(p for p in r["planned_deletes"]
               if p["table"] == "inspection_attachment")["row_count"] == 1)
check("设备2 无命中超范围边", r["summary"]["unsupported_items"] == 0)
check("设备2 fully_analyzable 且 deletable",
      r["summary"]["fully_analyzable"] is True
      and r["summary"]["deletable"] is True)

# 3. 设备3：可延迟 NO ACTION 留存行 -> 阻断
s, r = get("/api/v1/precheck?table=equipment&pk=3")
check("设备3 阻断 retest_review",
      block_tables == ["retest_review"]
      or names(r["blocking_references"]) == ["retest_review"])
check("设备3 阻断约束可延迟", r["blocking_references"][0]["deferrable"] is True)

# 4. part 自引用环路：3 行同表只列一次
s, r = get("/api/v1/precheck?table=part&pk=1")
part_rows = next(p for p in r["planned_deletes"] if p["table"] == "part")
check("part 环路3行去重", part_rows["pk_values"] == ["1", "2", "3"])

# 5. node_a/node_b 互指环路
s, r = get("/api/v1/precheck?table=node_a&pk=1")
check("互指环路2表各1行",
      names(r["planned_deletes"]) == ["node_a", "node_b"]
      and all(p["row_count"] == 1 for p in r["planned_deletes"]))

# 6. 复合主键表拒绝
s, r = post({"targets": [{"table": "tag_assignment", "pk": "11"}]})
check("复合主键表 400", s == 400 and "复合主键" in r["detail"]["errors"][0])

# 7. 不存在的表
s, r = get("/api/v1/precheck?table=ghost&pk=1")
check("不存在表 400", s == 400)

# 8. 非法主键类型
s, r = get("/api/v1/precheck?table=equipment&pk=abc")
check("非法主键值 400", s == 400 and r["detail"]["sqlstate"] == "22P02")

# 9. 不存在的主键行 -> 空删除集 + 提示
s, r = get("/api/v1/precheck?table=equipment&pk=88888")
check("不存在行 exists=false", r["targets"][0]["exists"] is False)
check("不存在行 空删除集", r["summary"]["rows_to_delete"] == 0)

# 10. 批量请求 + 同目标去重
s, r = post({"targets": [
    {"table": "part", "pk": "2"}, {"table": "part", "pk": "2"},
    {"table": "part", "pk": "999"}, {"table": "node_b", "pk": "1"}]})
check("批量 HTTP200", s == 200)
part = next(p for p in r["planned_deletes"] if p["table"] == "part")
check("批量同目标去重", part["pk_values"] == ["2", "3"])
check("批量缺失行提示", any("999" in n for n in r["notices"]))

# 11. 超范围原因可读性
s, r = get("/api/v1/precheck?table=equipment&pk=5")
check("SET NULL 超范围原因",
      "SET NULL" in r["unsupported"][0]["reason"]
      and r["unsupported"][0]["matched_row_count"] == 1)

# 12. 健康检查与 Swagger
s, _ = get("/healthz")
check("healthz 200", s == 200)
try:
    with urllib.request.urlopen(BASE + "/docs", timeout=10) as resp:
        check("Swagger UI 200", resp.status == 200)
except urllib.error.HTTPError as exc:
    check("Swagger UI 200", False, str(exc.code))

passed = sum(1 for _, ok, _ in checks if ok)
for name, ok, detail in checks:
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
print(f"\n{passed}/{len(checks)} 通过")
raise SystemExit(0 if passed == len(checks) else 1)
