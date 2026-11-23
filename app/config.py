"""服务配置（全部来自环境变量，带本地开发默认值）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    default_schemas: list[str]
    statement_timeout_ms: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql://postgres@127.0.0.1:5433/device_ops",
            ),
            default_schemas=os.getenv("DEFAULT_SCHEMAS", "public").split(","),
            statement_timeout_ms=int(os.getenv("STATEMENT_TIMEOUT_MS", "15000")),
        )


settings = Settings.from_env()
