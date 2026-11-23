"""按外键查涉及行的只读 SQL 辅助。

主键值按文本传入，使用 unnest(...::text[])::列类型 让 PostgreSQL 按列实际类型转换，
避免 bigint / uuid / numeric 等类型与 text 参数比较时不隐式转换的问题。
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row, tuple_row

# 单次 IN 查询上限，防止极大删除集把查询撑爆；分块循环处理
_BATCH = 1000
# 超范围边只抽样展示命中行
SAMPLE_LIMIT = 200


def qident(parts: list[str]) -> str:
    """拼接限定标识符，双引号转义防注入（表名/列名来自目录而非用户直拼）。"""
    return ".".join('"' + p.replace('"', '""') + '"' for p in parts)


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def rows_exist(conn: psycopg.Connection, schema: str, table: str,
               pk_column: str, pk_values: list[str]) -> set[str]:
    """返回实际存在的主键值集合（统一转字符串）。"""
    full = qident([schema, table])
    col = qident([pk_column])
    type_name = _type_name(conn, schema, table, pk_column)
    found: set[str] = set()
    with conn.cursor(row_factory=tuple_row) as cur:
        for batch in _chunks(pk_values, _BATCH):
            sql = (
                f"SELECT {col}::text FROM {full} "
                f"WHERE {col} = ANY(SELECT unnest(%s::text[])::{type_name})"
            )
            cur.execute(sql, (batch,))
            found.update(str(r[0]) for r in cur.fetchall())
    return found


def _type_name(conn: psycopg.Connection, schema: str, table: str, column: str) -> str:
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(
            """
            SELECT format_type(att.atttypid, att.atttypmod)
            FROM pg_attribute att
            JOIN pg_class cl ON cl.oid = att.attrelid
            JOIN pg_namespace ns ON ns.oid = cl.relnamespace
            WHERE ns.nspname = %s AND cl.relname = %s AND att.attname = %s
            """,
            (schema, table, column),
        )
        row = cur.fetchone()
    return row[0] if row else "text"


def referencing_pk_values(conn: psycopg.Connection, fk, target_values: list[str],
                          from_pk_column: str) -> set[str]:
    """单列外键、单列主键场景：取引用了目标值的引用表主键值。"""
    from_full = qident([fk.from_schema, fk.from_table])
    fk_col = qident([fk.from_columns[0]])
    pk_col = qident([from_pk_column])
    type_name = _type_name(conn, fk.to_schema, fk.to_table, fk.to_columns[0])
    result: set[str] = set()
    with conn.cursor(row_factory=tuple_row) as cur:
        for batch in _chunks(target_values, _BATCH):
            sql = (
                f"SELECT DISTINCT {pk_col}::text FROM {from_full} "
                f"WHERE {fk_col} = ANY(SELECT unnest(%s::text[])::{type_name})"
            )
            cur.execute(sql, (batch,))
            result.update(str(r[0]) for r in cur.fetchall())
    return result


def matching_rows(conn: psycopg.Connection, schema: str, table: str,
                  match_columns: list[str], match_values: list[Any],
                  target_schema: str, target_table: str, target_columns: list[str]
                  ) -> tuple[list[dict], bool]:
    """超范围边的通用命中查询：按外键列匹配，抽样返回整行。

    match_values 与 target_columns 对齐（单列外键时长度为 1）。
    返回 (行列表(主键/行内容字符串化), 是否截断)。
    """
    full = qident([schema, table])
    with conn.cursor(row_factory=dict_row) as cur:
        type_names = [
            _type_name(conn, target_schema, target_table, c) for c in target_columns
        ]
        raw_cols = [f"c{i}_raw" for i in range(len(type_names))]
        cast_select = ", ".join(
            f"{raw}::{tn} AS c{i}" for i, (raw, tn) in enumerate(zip(raw_cols, type_names))
        )
        if len(type_names) == 1:
            vals_from = "unnest(%s::text[]) AS v(c0_raw)"
            params = [match_values]
        else:
            placeholders = ", ".join("%s::text[]" for _ in type_names)
            col_aliases = ", ".join(raw_cols)
            vals_from = f"unnest({placeholders}) AS v({col_aliases})"
            params = [
                [row[i] for row in match_values] for i in range(len(type_names))
            ]
        where = " AND ".join(
            f"t.{qident([mc])} = vals.c{idx}"
            for idx, mc in enumerate(match_columns)
        )
        sql = (
            f"WITH vals AS (SELECT {cast_select} FROM {vals_from})"
            f" SELECT t.* FROM {full} t CROSS JOIN vals "
            f"WHERE {where} LIMIT {SAMPLE_LIMIT + 1}"
        )
        cur.execute(sql, params)
        rows = cur.fetchall()

    truncated = len(rows) > SAMPLE_LIMIT
    rows = rows[:SAMPLE_LIMIT]
    normalized = [{k: _stringify(v) for k, v in row.items()} for row in rows]
    return normalized, truncated


def _stringify(value) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
