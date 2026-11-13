"""连接器配置：CLI / MCP / API 外部集成的注册与状态。"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.identity import User  # noqa: F401  导入以解析 FK 元数据


class Connector(Base):
    """一个外部集成连接器；密钥不落此表，存 integration_configs。"""

    __tablename__ = "connectors"
    __table_args__ = (
        CheckConstraint("kind IN ('cli', 'mcp', 'api')", name="kind"),
        CheckConstraint("status IN ('disabled', 'connected', 'degraded', 'error')", name="status"),
        CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
        UniqueConstraint("key", name="uq_connectors_key"),
        Index("ix_connectors_enabled", "enabled"),
        Index("ix_connectors_kind", "kind"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    icon: Mapped[str] = mapped_column(String(64), default="", server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # 非密钥配置（endpoint、command、tool_policies 等）；密钥走 integration_configs。
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    # 工具发现缓存：服务临时不可用时前端仍可展示目录（status=degraded）。
    discovered_tools: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(20), default="disabled", server_default="disabled")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
