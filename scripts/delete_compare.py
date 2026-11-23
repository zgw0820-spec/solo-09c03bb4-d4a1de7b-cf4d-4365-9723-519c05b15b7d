"""预检预测 vs PostgreSQL 真实 DELETE 行为对比（克隆库上执行，不污染源库）。

- 预测：调用服务自身的 run_precheck（REPEATABLE READ READ ONLY 事务）
- 实测：克隆库中开事务，SET CONSTRAINTS ALL DEFERRED 后执行 DELETE 并尝试 COMMIT
        （可延迟 NO ACTION 因此在事务末检查，与需求语义一致）
- 对比：
  * 预测有阻断 -> 真实删除必须失败，且报错约束名在预测阻断约束集合内
  * 预测可删 -> 真实删除必须提交，各表实际删除行数 == 预测行数
"""
import sys

import psycopg

sys.path.insert(0, "/workspace")

from app.catalog import load_catalog  # noqa: E402
from app.config import settings  # noqa: E402
from app.precheck import run_precheck  # noqa: E402

SRC_URL = "postgresql://postgres@127.0.0.1:5433/device_ops"
VERIFY_URL = "postgresql://postgres@127.0.0.1:5433/device_ops_verify"

CASES = [
    ("equipment", "1", "阻断：handover 不可延迟 + work_order 可延迟事务末"),
    ("equipment", "2", "干净级联：多路径去重 + 引用行同删不阻断"),
    ("equipment", "3", "可延迟 NO ACTION 引用行留存 -> 事务末阻断"),
    ("part", "1", "自引用 CASCADE 环路"),
    ("node_a", "1", "两表互指 CASCADE 环路"),
]


def predict(table: str, pk: str):
    conn = psycopg.connect(SRC_URL)
    try:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            schemas = settings.default_schemas
            tables, pks, fks = load_catalog(conn, schemas)
            resp = run_precheck(
                conn, tables, pks, fks,
                [("public", table, pk)], schemas,
            )
        return resp
    finally:
        conn.close()


def table_counts(conn) -> dict[tuple[str, str], int]:
    # 显式提交，避免隐式事务残留导致后续 transaction() 退化为 SAVEPOINT
    # （保存点退出不触发可延迟约束的提交期检查）
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n.nspname, c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'r' AND n.nspname = 'public'"
        )
        names = [(r[0], r[1]) for r in cur.fetchall()]
    counts = {}
    with conn.cursor() as cur:
        for schema, rel in names:
            cur.execute(f'SELECT count(*) FROM "{schema}"."{rel}"')
            counts[(schema, rel)] = cur.fetchone()[0]
    conn.commit()
    return counts


def attempt_delete(table: str, pk: str):
    """返回 (是否提交, 错误约束名或 None, 各表删除行数或 None)。"""
    conn = psycopg.connect(VERIFY_URL)
    before = table_counts(conn)
    assert conn.info.transaction_status == 0, "前置计数事务必须已结束"
    try:
        with conn.transaction():
            conn.execute("SET CONSTRAINTS ALL DEFERRED")
            with conn.cursor() as cur:
                cur.execute(
                    f'DELETE FROM "public"."{table}" WHERE id = %s', (int(pk),)
                )
        assert conn.info.transaction_status == 0, "顶层事务必须已 COMMIT"
        after = table_counts(conn)
        deleted = {k: before[k] - after[k] for k in before if before[k] != after[k]}
        return True, None, deleted
    except psycopg.Error as exc:
        conn.rollback()
        diag = getattr(exc, "diag", None)
        constraint = diag.constraint_name if diag is not None else None
        return False, constraint, None
    finally:
        conn.close()


def main() -> int:
    failures = 0
    for table, pk, desc in CASES:
        print(f"\n=== {table} pk={pk}：{desc} ===")
        resp = predict(table, pk)
        predicted_blockers = {
            c for b in resp.blocking_references for c in b.constraints
        }
        committed, err_con, deleted = attempt_delete(table, pk)
        print(f"  预测阻断: {sorted(predicted_blockers) or '无'}")
        print(f"  预测拟删: {[(p.table, p.row_count) for p in resp.planned_deletes]}")
        print(f"  真实提交: {committed}，报错约束: {err_con}")

        if predicted_blockers:
            if committed:
                print("  FAIL: 预测有阻断但真实删除提交了")
                failures += 1
            elif err_con not in predicted_blockers:
                print(f"  FAIL: 报错约束 {err_con} 不在预测阻断集合内")
                failures += 1
            else:
                print("  PASS: 真实删除被预测的约束阻断")
        else:
            if not committed:
                print(f"  FAIL: 预测可删但真实删除被 {err_con} 阻断")
                failures += 1
                continue
            actual = {f"public.{k[1]}": v for k, v in deleted.items() if v}
            predicted = {
                f"public.{p.table}": p.row_count for p in resp.planned_deletes
            }
            print(f"  实际删行: {actual}")
            if actual != predicted:
                print(f"  FAIL: 实际 {actual} != 预测 {predicted}")
                failures += 1
            else:
                print("  PASS: 实际删除表/行数与预测完全一致")
    print(f"\n{'=' * 50}\n结论: {'全部通过' if failures == 0 else f'{failures} 个失败'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
