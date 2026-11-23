"""从 pg_catalog 加载表与外键元数据（只读）。"""
from __future__ import annotations

from collections import defaultdict

import psycopg
from psycopg.rows import dict_row

from .models import FkMeta, TableMeta

_RELKIND_DESC = {
    "r": "普通表",
    "p": "分区表",
    "v": "视图",
    "m": "物化视图",
    "f": "外部表",
    "S": "序列",
}

# pg_constraint.contype 含义
_FK_QUERY = """
SELECT
    con.conname                                  AS constraint_name,
    f_ns.nspname                                 AS from_schema,
    f_cl.relname                                 AS from_table,
    (
        SELECT array_agg(att.attname ORDER BY u.ord)
        FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord)
        JOIN pg_attribute att
          ON att.attrelid = con.conrelid AND att.attnum = u.attnum
    )                                            AS from_columns,
    t_ns.nspname                                 AS to_schema,
    t_cl.relname                                 AS to_table,
    (
        SELECT array_agg(att.attname ORDER BY u.ord)
        FROM unnest(con.confkey) WITH ORDINALITY AS u(attnum, ord)
        JOIN pg_attribute att
          ON att.attrelid = con.confrelid AND att.attnum = u.attnum
    )                                            AS to_columns,
    CASE con.confdeltype
        WHEN 'a' THEN 'NO ACTION'
        WHEN 'r' THEN 'RESTRICT'
        WHEN 'c' THEN 'CASCADE'
        WHEN 'n' THEN 'SET NULL'
        WHEN 'd' THEN 'SET DEFAULT'
    END                                          AS on_delete,
    con.condeferrable                            AS deferrable,
    con.condeferred                              AS initially_deferred
FROM pg_constraint con
JOIN pg_class f_cl ON f_cl.oid = con.conrelid
JOIN pg_namespace f_ns ON f_ns.oid = f_cl.relnamespace
JOIN pg_class t_cl ON t_cl.oid = con.confrelid
JOIN pg_namespace t_ns ON t_ns.oid = t_cl.relnamespace
WHERE con.contype = 'f'
  AND f_ns.nspname = ANY(%(schemas)s)
ORDER BY f_ns.nspname, f_cl.relname, con.conname
"""

_PK_QUERY = """
SELECT
    ns.nspname              AS schema_name,
    cl.relname              AS table_name,
    cl.relkind              AS relkind,
    COALESCE(
        array_agg(att.attname ORDER BY ic.ord)
            FILTER (WHERE con.oid IS NOT NULL),
        ARRAY[]::name[]
    )                       AS pk_columns
FROM pg_class cl
JOIN pg_namespace ns ON ns.oid = cl.relnamespace
LEFT JOIN pg_constraint con
       ON con.conrelid = cl.oid AND con.contype = 'p'
LEFT JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS ic(attnum, ord)
       ON TRUE
LEFT JOIN pg_attribute att
       ON att.attrelid = cl.oid AND att.attnum = ic.attnum
WHERE cl.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND ns.nspname = ANY(%(schemas)s)
GROUP BY ns.nspname, cl.relname, cl.relkind
ORDER BY ns.nspname, cl.relname
"""


def relkind_desc(relkind: str) -> str:
    return _RELKIND_DESC.get(relkind, f"未知类型({relkind})")


def load_catalog(conn: psycopg.Connection, schemas: list[str]) -> tuple[dict, dict, list[FkMeta]]:
    """返回 (tables[(schema,table)] -> TableMeta, pks[(schema,table)] -> [列], fks)。"""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_PK_QUERY, {"schemas": schemas})
        rows = cur.fetchall()

    tables: dict[tuple[str, str], TableMeta] = {}
    pks: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        key = (r["schema_name"], r["table_name"])
        pk_cols = list(r["pk_columns"] or [])
        tables[key] = TableMeta(
            schema=r["schema_name"],
            table=r["table_name"],
            relkind=r["relkind"],
            relkind_desc=relkind_desc(r["relkind"]),
            pk_columns=pk_cols,
        )
        pks[key] = pk_cols

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_FK_QUERY, {"schemas": schemas})
        fk_rows = cur.fetchall()

    fks = [
        FkMeta(
            constraint=r["constraint_name"],
            from_schema=r["from_schema"],
            from_table=r["from_table"],
            from_columns=list(r["from_columns"]),
            to_schema=r["to_schema"],
            to_table=r["to_table"],
            to_columns=list(r["to_columns"]),
            on_delete=r["on_delete"],
            deferrable=r["deferrable"],
            initially_deferred=r["initially_deferred"],
        )
        for r in fk_rows
    ]
    return tables, pks, fks


def index_outgoing(fks: list[FkMeta]) -> dict[tuple[str, str], list[FkMeta]]:
    """被引用表 -> 指向它的外键列表（删除传播方向）。"""
    out: dict[tuple[str, str], list[FkMeta]] = defaultdict(list)
    for fk in fks:
        out[(fk.to_schema, fk.to_table)].append(fk)
    return out
