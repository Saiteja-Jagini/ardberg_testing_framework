from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    repository: Mapped[str] = mapped_column(String(300), index=True)
    pr_number: Mapped[int] = mapped_column(Integer)
    installation_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(500), default="")
    head_sha: Mapped[str] = mapped_column(String(64), default="")
    base_sha: Mapped[str] = mapped_column(String(64), default="")
    instruction: Mapped[str] = mapped_column(Text)
    selected_frameworks: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(40), default="queued")
    stage: Mapped[str] = mapped_column(String(80), default="intake")
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    report: Mapped[str | None] = mapped_column(Text, nullable=True)
    check_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(80))
    agent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    node: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30))
    success: Mapped[bool | None] = mapped_column(nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InteractivePreview(Base):
    __tablename__ = "interactive_previews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    command: Mapped[str] = mapped_column(Text)
    port: Mapped[int] = mapped_column(Integer)
    ready_path: Mapped[str] = mapped_column(String(500), default="/")
    url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    container: Mapped[str | None] = mapped_column(String(100), nullable=True)
    database_container: Mapped[str | None] = mapped_column(String(100), nullable=True)
    network: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class InteractivePreviewOptions(Base):
    __tablename__ = "interactive_preview_options"

    preview_id: Mapped[str] = mapped_column(ForeignKey("interactive_previews.id"), primary_key=True)
    environment: Mapped[dict] = mapped_column(JSON, default=dict)
    setup_commands: Mapped[list | None] = mapped_column(JSON, nullable=True)


class ManualObservation(Base):
    __tablename__ = "manual_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    preview_id: Mapped[str] = mapped_column(ForeignKey("interactive_previews.id"))
    verdict: Mapped[str] = mapped_column(String(20))
    steps: Mapped[str] = mapped_column(Text)
    expected: Mapped[str] = mapped_column(Text)
    actual: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PullRequestSettings(Base):
    __tablename__ = "pull_request_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository: Mapped[str] = mapped_column(String(300), index=True)
    pr_number: Mapped[int] = mapped_column(Integer, index=True)
    instruction: Mapped[str] = mapped_column(Text)
    selected_frameworks: Mapped[list] = mapped_column(JSON, default=list)
    comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    repository: Mapped[str] = mapped_column(String(300))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GitHubInstallation(Base):
    __tablename__ = "github_installations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger)
    account_login: Mapped[str] = mapped_column(String(300))
    target_type: Mapped[str] = mapped_column(String(40))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class GitHubUser(Base):
    __tablename__ = "github_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    login: Mapped[str] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
