from sqlalchemy import JSON, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from ..config.database import Base
from ..utils.clock import now


class SourceScope(Base):
    """按渠道 + 月份隔离的来源版本。每次整月更正后版本加一。"""

    __tablename__ = "source_scopes"
    __table_args__ = (UniqueConstraint("channel", "month", name="source_scope_unique"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str]
    month: Mapped[str]
    version: Mapped[int] = mapped_column(default=1)
    checksum: Mapped[str] = mapped_column(default="")
    updated_at: Mapped[str] = mapped_column(default=now)


class SourceBatch(Base):
    """一次整月更正的上传批次：先预览，确认后才替换来源。"""

    __tablename__ = "source_batches"
    __table_args__ = (Index("source_batches_scope_idx", "channel", "month", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_key: Mapped[str] = mapped_column(unique=True)
    channel: Mapped[str]
    month: Mapped[str]
    status: Mapped[str] = mapped_column(default="preview")
    scope_version: Mapped[int]
    scope_checksum: Mapped[str]
    row_count: Mapped[int]
    rows: Mapped[list] = mapped_column(JSON)
    rows_checksum: Mapped[str] = mapped_column(default="")
    preview: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str]
    created_at: Mapped[str] = mapped_column(default=now)
    applied_by: Mapped[str | None] = mapped_column(nullable=True)
    applied_at: Mapped[str | None] = mapped_column(nullable=True)
    applied_checksum: Mapped[str | None] = mapped_column(nullable=True)
