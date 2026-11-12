"""系统初始化标记，用于检测迁移是否完成。"""

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SystemMetadata(Base):
    """保存基础设施标记，不存放业务配置或凭证。"""

    __tablename__ = "system_metadata"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
