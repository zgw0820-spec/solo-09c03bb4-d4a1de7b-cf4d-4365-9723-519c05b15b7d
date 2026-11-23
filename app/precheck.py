"""删除预检核心：在单个只读快照事务内计算删除集与阻断引用。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import tuple_row

from .models import (
    BlockingReference,
    FkMeta,
    PlannedDelete,
    PrecheckResponse,
    PrecheckSummary,
    SnapshotInfo,
    TableMeta,
    TargetResult,
    UnsupportedItem,
)
from .queries import matching_rows, qident, referencing_pk_values, rows_exist

SUPPORTED_ACTIONS = {"CASCADE", "NO ACTION"}
SAMPLE_LIMIT = 200


@dataclass
class EdgeClassification:
    fk: FkMeta
    supported: bool
    reasons: list[str] = field(default_factory=list)


def _classify(fk: FkMeta, tables: dict, pks: dict) -> EdgeClassification:
    reasons: list[str] = []
    to_key = (fk.to_schema, fk.to_table)
    from_key = (fk.from_schema, fk.from_table)
    to_tbl: TableMeta | None = tables.get(to_key)
    from_tbl: TableMeta | None = tables.get(from_key)
    to_pk = pks.get(to_key, [])
    from_pk = pks.get(from_key, [])

    if fk.on_delete not in SUPPORTED_ACTIONS:
        reasons.append(
            f"ON DELETE {fk.on_delete} 不受支持，仅分析 CASCADE 与 NO ACTION"
        )
    if to_tbl is None or to_tbl.relkind != "r":
        reasons.append("被引用表不是普通表")
    elif not to_pk:
        reasons.append("被引用表没有主键")
    elif len(to_pk) != 1:
        reasons.append("被引用表是复合主键，超出单列主键支持范围")
    elif fk.to_columns != to_pk:
        reasons.append("外键指向的列不是被引用表的主键")
    if len(fk.from_columns) != 1 or len(fk.to_columns) != 1:
        reasons.append("复合列外键超出单列支持范围")
    if from_tbl is None or from_tbl.relkind != "r":
        reasons.append("引用表不是普通表")
    elif not from_pk:
        reasons.append("引用表没有主键，无法按主键汇总")
    elif len(from_pk) != 1:
        reasons.append("引用表是复合主键，无法按主键汇总拟删除行")

    return EdgeClassification(fk=fk, supported=not reasons, reasons=reasons)


def _snapshot_info(conn: psycopg.Connection) -> SnapshotInfo:
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(
            """
            SELECT current_setting('transaction_isolation'),
                   current_setting('transaction_read_only'),
                   pg_current_xact_id_if_assigned()::text,
                   txid_snapshot_xmin(txid_current_snapshot())::text,
                   now()::text
            """
        )
        iso, ro, xid, xmin, now = cur.fetchone()
    return SnapshotInfo(
        isolation_level=iso,
        read_only=ro == "on",
        backend_xid=int(xid) if xid else None,
        backend_xmin=xmin,
        taken_at=now,
    )


def _fetch_referenced_values(conn: psycopg.Connection, schema: str, table: str,
                             pk_column: str, pk_values: set[str],
                             ref_columns: list[str]) -> list[list[str]]:
    """取待删行在“被引用列”上的实际值（用于非主键目标的超范围边匹配）。"""
    cols = ", ".join(qident([c]) + "::text" for c in ref_columns)
    sql = (
        f"SELECT {cols} FROM {qident([schema, table])} "
        f"WHERE {qident([pk_column])}::text = ANY(%s)"
    )
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(sql, (list(pk_values),))
        return [list(map(str, row)) for row in cur.fetchall()]


def run_precheck(conn: psycopg.Connection, tables: dict, pks: dict, fks: list[FkMeta],
                 resolved_targets: list[tuple[str, str, str]],
                 schemas: list[str]) -> PrecheckResponse:
    """resolved_targets: (schema, table, pk_value 文本)，表已通过入参校验。"""
    edge_index = {
        fk.constraint + "|" + fk.from_schema + "|" + fk.from_table:
        _classify(fk, tables, pks) for fk in fks
    }
    edges_by_to: dict[tuple[str, str], list[EdgeClassification]] = defaultdict(list)
    for edge in edge_index.values():
        edges_by_to[(edge.fk.to_schema, edge.fk.to_table)].append(edge)

    notices: list[str] = []

    # ---- 1. 目标行存在性 ----------------------------------------------
    target_results: list[TargetResult] = []
    existing: dict[tuple[str, str], set[str]] = defaultdict(set)
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for schema, table, value in resolved_targets:
        grouped[(schema, table)].append(value)
    for (schema, table), values in grouped.items():
        pk_col = pks[(schema, table)][0]
        found = rows_exist(conn, schema, table, pk_col, values)
        for v in values:
            target_results.append(TargetResult(
                schema=schema, table=table, pk_column=pk_col,
                pk_value=v, exists=v in found,
            ))
            if v in found:
                existing[(schema, table)].add(v)
        missing = sorted(set(values) - found)
        if missing:
            notices.append(
                f"{schema}.{table} 中不存在的主键值已跳过: {', '.join(missing)}"
            )

    # ---- 2. 固定点遍历求删除集（仅受支持的 CASCADE 边）----------------
    # deletion: 表 -> 拟删主键值集合；环路 / 多路径靠集合天然去重
    deletion: dict[tuple[str, str], set[str]] = defaultdict(set)
    # no_action_hits: 边 -> (引用表pk值 -> 命中的目标主键值集合)
    no_action_hits: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    worklist: list[tuple[str, str, list[str]]] = [
        (k[0], k[1], sorted(v)) for k, v in existing.items()
    ]
    for key, vals in existing.items():
        deletion[key].update(vals)

    while worklist:
        schema, table, values = worklist.pop()
        for edge in edges_by_to.get((schema, table), []):
            if not edge.supported:
                continue
            fk = edge.fk
            from_key = (fk.from_schema, fk.from_table)
            from_pk_col = pks[from_key][0]
            hits = referencing_pk_values(conn, fk, values, from_pk_col)
            if not hits:
                continue
            if fk.on_delete == "CASCADE":
                new = hits - deletion[from_key]
                if new:
                    deletion[from_key].update(new)
                    worklist.append((fk.from_schema, fk.from_table, sorted(new)))
            else:  # NO ACTION
                store = no_action_hits[
                    fk.constraint + "|" + fk.from_schema + "|" + fk.from_table
                ]
                for h in hits:
                    store[h].update(values)

    # ---- 3. NO ACTION 阻断判定：引用行也会删 -> 不阻断 -----------------
    # 同一引用行可能被多条边 / 环路多次命中，按行去重后聚合
    blocker_agg: dict[tuple[str, str, str], dict] = {}
    for edge_key, rows in no_action_hits.items():
        edge = edge_index[edge_key]
        fk = edge.fk
        from_key = (fk.from_schema, fk.from_table)
        surviving = set(rows) - deletion.get(from_key, set())
        to_pk_col = pks[(fk.to_schema, fk.to_table)][0]
        for pk_val in sorted(surviving):
            agg_key = (fk.from_schema, fk.from_table, pk_val)
            entry = blocker_agg.setdefault(agg_key, {
                "constraints": [], "targets": [],
                "fk": fk,
            })
            if fk.constraint not in entry["constraints"]:
                entry["constraints"].append(fk.constraint)
            for tv in sorted(rows[pk_val]):
                ref = f"{fk.to_schema}.{fk.to_table}.{to_pk_col}={tv}"
                if ref not in entry["targets"]:
                    entry["targets"].append(ref)

    blocking = [
        BlockingReference(
            schema=k[0], table=k[1],
            pk_column=pks[(k[0], k[1])][0], pk_value=k[2],
            constraints=v["constraints"],
            on_delete="NO ACTION",
            deferrable=v["fk"].deferrable,
            initially_deferred=v["fk"].initially_deferred,
            references={"via": v["targets"]},
        )
        for k, v in sorted(blocker_agg.items(), key=lambda kv: kv[0])
    ]

    # ---- 4. 超范围边：对实际命中待删行的边抽样取证 ---------------------
    unsupported: list[UnsupportedItem] = []
    for edge in edge_index.values():
        if edge.supported:
            continue
        fk = edge.fk
        to_key = (fk.to_schema, fk.to_table)
        if to_key not in deletion or not deletion[to_key]:
            continue
        to_pk = pks.get(to_key, [])
        if len(to_pk) == 1 and to_pk[0] in fk.to_columns:
            # 目标列就是单列主键：直接用待删主键值匹配
            idx = fk.to_columns.index(to_pk[0])
            match_values = sorted(deletion[to_key])
            match_cols = [fk.from_columns[idx]]
            target_cols = [fk.to_columns[idx]]
        elif len(to_pk) == 1:
            # 非主键目标列：先把待删行的被引用列实际值查出来
            match_values = _fetch_referenced_values(
                conn,
                schema=to_key[0], table=to_key[1], pk_column=to_pk[0],
                pk_values=deletion[to_key], ref_columns=fk.to_columns,
            )
            match_cols = fk.from_columns
            target_cols = fk.to_columns
        else:
            notices.append(
                f"超范围边 {fk.constraint} 的被引用表无单列主键，跳过命中行取样"
            )
            continue
        rows_out, truncated = matching_rows(
            conn, fk.from_schema, fk.from_table,
            match_cols,
            match_values,
            fk.to_schema, fk.to_table, target_cols,
        )
        # 未实际命中待删行的超范围边不参与本次删除，不进回执
        if not rows_out:
            continue
        unsupported.append(UnsupportedItem(
            kind="edge", constraint=fk.constraint,
            schema=fk.from_schema, table=fk.from_table,
            reason="；".join(edge.reasons),
            matched_rows=rows_out,
            matched_row_count=len(rows_out),
            truncated=truncated,
        ))

    # ---- 5. 组装回执 ---------------------------------------------------
    planned = []
    total_rows = 0
    for (schema, table), values in sorted(deletion.items()):
        pk_col = pks[(schema, table)][0]
        vals = sorted(values)
        total_rows += len(vals)
        planned.append(PlannedDelete(
            schema=schema, table=table, pk_column=pk_col,
            pk_values=vals, row_count=len(vals),
        ))

    has_unsupported = len(unsupported) > 0
    summary = PrecheckSummary(
        tables_to_delete=len(planned),
        rows_to_delete=total_rows,
        blocking_references=len(blocking),
        unsupported_items=len(unsupported),
        deletable=not blocking and not has_unsupported,
        fully_analyzable=not has_unsupported,
    )
    if has_unsupported:
        notices.append(
            "存在超出支持范围的外键，回执结论不完整；matched_rows 为抽样取证，"
            "实际删除前需人工核实这些边的数据库行为"
        )

    return PrecheckResponse(
        snapshot=_snapshot_info(conn),
        targets=target_results,
        planned_deletes=planned,
        blocking_references=blocking,
        unsupported=unsupported,
        notices=notices,
        summary=summary,
    )
