"""FastAPI 只读删除预检服务。

所有预检在同一 REPEATABLE READ READ ONLY 事务（单一快照）内完成，
事务期间业务写入照常提交，且不会影响本回执的一致性。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import psycopg
import psycopg_pool
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row

from .catalog import load_catalog, relkind_desc
from .config import settings
from .models import (
    PrecheckRequest,
    PrecheckResponse,
    TableMeta,
)
from .precheck import run_precheck

_POOL: psycopg_pool.ConnectionPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _POOL
    _POOL = psycopg_pool.ConnectionPool(
        conninfo=settings.database_url,
        min_size=1,
        max_size=8,
        kwargs={"row_factory": dict_row},
        open=True,
    )
    yield
    _POOL.close()


app = FastAPI(
    title="设备删除只读预检 API",
    version="1.0.0",
    description=(
        "凭表名 + 主键值，沿指向主键的外键预演删除：列出拟删除的各表主键、"
        "阻断引用与超出支持范围的边。只读，不执行任何删除。"
    ),
    lifespan=lifespan,
)


def pool() -> psycopg_pool.ConnectionPool:
    if _POOL is None:
        raise HTTPException(503, "连接池尚未就绪")
    return _POOL


def _validate_targets(conn: psycopg.Connection, tables: dict, pks: dict,
                      targets: list, schemas: list[str]
                      ) -> list[tuple[str, str, str]]:
    resolved: list[tuple[str, str, str]] = []
    errors: list[str] = []
    for t in targets:
        schema = t.schema_ or settings.default_schemas[0]
        key = (schema, t.table)
        meta: TableMeta | None = tables.get(key)
        if meta is None:
            errors.append(
                f"表 {schema}.{t.table} 不存在或不在搜索范围（搜索 schema: {schemas}）"
            )
            continue
        if meta.relkind != "r":
            errors.append(
                f"{schema}.{t.table} 是{relkind_desc(meta.relkind)}，仅支持普通表（relkind=r）"
            )
            continue
        pk_cols = pks.get(key, [])
        if not pk_cols:
            errors.append(f"{schema}.{t.table} 没有主键，无法按主键预检")
        elif len(pk_cols) != 1:
            errors.append(
                f"{schema}.{t.table} 是复合主键 {pk_cols}，本服务仅支持单列主键"
            )
        else:
            resolved.append((schema, t.table, str(t.pk)))
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    if not resolved:
        raise HTTPException(status_code=400, detail="没有可预检的有效目标")
    return resolved


def _precheck(conn: psycopg.Connection, request: PrecheckRequest) -> PrecheckResponse:
    schemas = request.schemas or settings.default_schemas
    tables, pks, fks = load_catalog(conn, schemas)
    resolved = _validate_targets(conn, tables, pks, request.targets, schemas)
    return run_precheck(conn, tables, pks, fks, resolved, schemas)


@app.exception_handler(psycopg.Error)
def psycopg_exception_handler(request, exc: psycopg.Error):
    # 主键值无法按列类型转换等输入类数据错误 -> 400，其余数据库错误 -> 502
    status = 400 if exc.sqlstate and exc.sqlstate[:2] in ("22", "23") else 502
    return JSONResponse(
        status_code=status,
        content={
            "detail": {
                "error": type(exc).__name__,
                "sqlstate": exc.sqlstate,
                "message": str(exc).split("\n")[0],
            }
        },
    )


@app.post("/api/v1/precheck", response_model=PrecheckResponse, tags=["预检"])
def precheck_post(request: PrecheckRequest) -> PrecheckResponse:
    """批量预检：同一快照事务内完成全部计算。"""
    with pool().connection() as conn:
        with conn.transaction():
            # 事务级快照：REPEATABLE READ + READ ONLY，须为事务内首条语句
            conn.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            conn.execute(
                f"SET LOCAL statement_timeout = {settings.statement_timeout_ms}"
            )
            return _precheck(conn, request)


@app.get("/api/v1/precheck", response_model=PrecheckResponse, tags=["预检"])
def precheck_get(
    table: str = Query(..., description="表名"),
    pk: str = Query(..., description="主键值（文本传入，按列类型转换）"),
    schema: str | None = Query(None, description="schema，默认取服务配置"),
) -> PrecheckResponse:
    """单目标便捷查询，便于在 Swagger 直接试查。"""
    request = PrecheckRequest(
        targets=[{"table": table, "pk": pk, "schema": schema}]
    )
    return precheck_post(request)


@app.get("/api/v1/tables", tags=["目录"])
def list_tables(schema: str | None = None) -> list[dict[str, Any]]:
    """列出搜索范围内的表及主键（调试 / Swagger 浏览用）。"""
    schemas = [schema] if schema else settings.default_schemas
    with pool().connection() as conn:
        tables, pks, _ = load_catalog(conn, schemas)
    return [
        {
            "schema": m.schema_, "table": m.table,
            "relkind": m.relkind, "relkind_desc": m.relkind_desc,
            "pk_columns": m.pk_columns,
            "supported_target": m.relkind == "r" and len(m.pk_columns) == 1,
        }
        for m in tables.values()
    ]


@app.get("/api/v1/foreign-keys", tags=["目录"])
def list_foreign_keys(schema: str | None = None) -> list[dict[str, Any]]:
    """列出搜索范围内的外键及其删除动作 / 可延迟性。"""
    schemas = [schema] if schema else settings.default_schemas
    with pool().connection() as conn:
        _, _, fks = load_catalog(conn, schemas)
    return [fk.model_dump() for fk in fks]


@app.get("/healthz", tags=["运维"])
def health() -> dict[str, str]:
    with pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}
