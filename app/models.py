"""Pydantic 请求 / 响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PrecheckTarget(BaseModel):
    schema_: str | None = Field(default=None, alias="schema")
    table: str
    pk: str = Field(..., description="主键值，按文本传入，服务端按列类型转换")


class PrecheckRequest(BaseModel):
    targets: list[PrecheckTarget] = Field(..., min_length=1)
    schemas: list[str] | None = Field(
        default=None, description="限定搜索外键的 schema，默认取服务配置"
    )


class SnapshotInfo(BaseModel):
    isolation_level: str
    read_only: bool
    backend_xid: int | None
    backend_xmin: str
    taken_at: str


class TargetResult(BaseModel):
    schema_: str = Field(alias="schema")
    table: str
    pk_column: str
    pk_value: str
    exists: bool


class PlannedDelete(BaseModel):
    schema_: str = Field(alias="schema")
    table: str
    pk_column: str
    pk_values: list[str]
    row_count: int


class BlockingReference(BaseModel):
    schema_: str = Field(alias="schema")
    table: str
    pk_column: str
    pk_value: str
    constraints: list[str]
    on_delete: Literal["NO ACTION"]
    deferrable: bool
    initially_deferred: bool
    references_: dict[str, Any] = Field(alias="references")


class UnsupportedItem(BaseModel):
    kind: Literal["edge", "target_table", "referencing_table"]
    constraint: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    table: str
    reason: str
    matched_rows: list[dict[str, Any]] = Field(default_factory=list)
    matched_row_count: int = 0
    truncated: bool = False


class PrecheckSummary(BaseModel):
    tables_to_delete: int
    rows_to_delete: int
    blocking_references: int
    unsupported_items: int
    deletable: bool = Field(
        description="可分析范围内无阻断；fully_analyzable=false 时结论不完整"
    )
    fully_analyzable: bool


class PrecheckResponse(BaseModel):
    snapshot: SnapshotInfo
    targets: list[TargetResult]
    planned_deletes: list[PlannedDelete]
    blocking_references: list[BlockingReference]
    unsupported: list[UnsupportedItem]
    notices: list[str]
    summary: PrecheckSummary

    model_config = {"populate_by_name": True}


class TableMeta(BaseModel):
    schema_: str = Field(alias="schema")
    table: str
    relkind: str
    relkind_desc: str
    pk_columns: list[str]


class FkMeta(BaseModel):
    constraint: str
    from_schema: str
    from_table: str
    from_columns: list[str]
    to_schema: str
    to_table: str
    to_columns: list[str]
    on_delete: str
    deferrable: bool
    initially_deferred: bool
