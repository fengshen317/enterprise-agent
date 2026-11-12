"""任务、执行账本与持久化事件；任务归属不由模型提供。"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Task(Base):
    """逻辑任务，当前执行状态由 current_run_id 指向的尝试决定。"""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("length(trim(goal)) > 0", name="goal_nonempty"),
        ForeignKeyConstraint(
            ["id", "current_run_id"],
            ["task_runs.task_id", "task_runs.id"],
            name="fk_tasks_current_run",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("length(idempotency_key) > 0", name="idempotency_nonempty"),
        ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            name="fk_tasks_membership",
        ),
        UniqueConstraint("workspace_id", "owner_user_id", "idempotency_key", name="uq_tasks_owner_idempotency"),
        CheckConstraint("next_event_sequence > 0", name="event_sequence_positive"),
        Index("ix_tasks_owner_created", "workspace_id", "owner_user_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column()
    owner_user_id: Mapped[UUID] = mapped_column()
    source_conversation_id: Mapped[UUID | None] = mapped_column(ForeignKey("conversations.id"))
    goal: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    current_run_id: Mapped[UUID | None] = mapped_column()
    next_event_sequence: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskRun(Base):
    """单次执行与租约；fencing_token 阻止旧执行器提交。"""

    __tablename__ = "task_runs"
    __table_args__ = (
        CheckConstraint(
            "(status IN ('completed','failed','cancelled')) = (finished_at IS NOT NULL)", name="terminal_timestamp"
        ),
        CheckConstraint("(lease_owner IS NULL) = (lease_expires_at IS NULL)", name="lease_pair"),
        UniqueConstraint("task_id", "id", name="uq_task_runs_task_id_id"),
        CheckConstraint(
            "status IN ('pending','planning','awaiting_confirmation','running','retry_wait',"
            "'paused','completed','failed','cancelled')",
            name="status",
        ),
        CheckConstraint("fencing_token >= 0 AND version > 0", name="versions_positive"),
        Index("ix_task_runs_due", "status", "next_retry_at", "lease_expires_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending")
    error_code: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str | None] = mapped_column(Text)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    paused_from: Mapped[str | None] = mapped_column(String(32))
    confirmed_plan_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskPlan(Base):
    """计划版本只新增，不原地修改已确认内容。"""

    __tablename__ = "task_plans"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision_positive"),
        UniqueConstraint("run_id", "revision", name="uq_task_plans_run_revision"),
        ForeignKeyConstraint(
            ["task_id", "run_id"],
            ["task_runs.task_id", "task_runs.id"],
            ondelete="CASCADE",
            name="fk_task_plans_task_run",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column()
    run_id: Mapped[UUID] = mapped_column()
    revision: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskStep(Base):
    """步骤定义快照与本轮进度。"""

    __tablename__ = "task_steps"
    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="attempt_count"),
        CheckConstraint("(status = 'succeeded') = (result IS NOT NULL)", name="result_state"),
        UniqueConstraint("run_id", "key", name="uq_task_steps_run_key"),
        ForeignKeyConstraint(
            ["task_id", "run_id"],
            ["task_runs.task_id", "task_runs.id"],
            ondelete="CASCADE",
            name="fk_task_steps_task_run",
        ),
        CheckConstraint(
            "status IN ('pending','running','retry_wait','paused','succeeded','failed','cancelled','skipped')",
            name="status",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column()
    run_id: Mapped[UUID] = mapped_column()
    key: Mapped[str] = mapped_column(String(40))
    position: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    result: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskStepAttempt(Base):
    """步骤尝试保留中断与失败，不覆盖历史。"""

    __tablename__ = "task_step_attempts"
    __table_args__ = (
        CheckConstraint("status IN ('running','completed','failed','interrupted')", name="status"),
        CheckConstraint("attempt_no > 0", name="attempt_positive"),
        CheckConstraint("(status = 'running') = (finished_at IS NULL)", name="terminal_timestamp"),
        UniqueConstraint("step_id", "attempt_no", name="uq_task_step_attempt_number"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    step_id: Mapped[UUID] = mapped_column(ForeignKey("task_steps.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="running", server_default="running")
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskAction(Base):
    """模型或工具的逻辑调用，也是步骤内部持久化检查点。"""

    __tablename__ = "task_actions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','retry_wait','succeeded','failed','interrupted','cancelled','paused')",
            name="status",
        ),
        CheckConstraint("kind IN ('plan','model','tool')", name="kind"),
        CheckConstraint("attempt_count >= 0", name="attempt_count"),
        CheckConstraint("(status = 'succeeded') = (result IS NOT NULL)", name="result_state"),
        UniqueConstraint("run_id", "input_digest", name="uq_task_actions_run_digest"),
        ForeignKeyConstraint(
            ["task_id", "run_id"],
            ["task_runs.task_id", "task_runs.id"],
            ondelete="CASCADE",
            name="fk_task_actions_task_run",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column()
    run_id: Mapped[UUID] = mapped_column()
    step_key: Mapped[str | None] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(64))
    input_digest: Mapped[str] = mapped_column(String(64))
    input_data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    result: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskActionAttempt(Base):
    """每个真实外部请求独立计数，不能把网络重试隐藏在 SDK 内。"""

    __tablename__ = "task_action_attempts"
    __table_args__ = (
        CheckConstraint("status IN ('running','completed','failed','interrupted')", name="status"),
        CheckConstraint("attempt_no > 0", name="attempt_positive"),
        CheckConstraint("(status = 'running') = (finished_at IS NULL)", name="terminal_timestamp"),
        UniqueConstraint("action_id", "attempt_no", name="uq_task_action_attempt_number"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    action_id: Mapped[UUID] = mapped_column(ForeignKey("task_actions.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="running", server_default="running")
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskEvent(Base):
    """任务行锁分配有序事件编号，SSE 重连只重放这些记录。"""

    __tablename__ = "task_events"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="sequence_positive"),
        UniqueConstraint("task_id", "sequence", name="uq_task_events_task_sequence"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
