from sqlalchemy import Index
from sqlalchemy.orm import Mapped,mapped_column
from ..config.database import Base


class SourceRow(Base):
    __tablename__="source_rows"
    __table_args__=(Index("source_rows_scope_idx","channel","day"),)
    id: Mapped[int]=mapped_column(primary_key=True)
    source_key: Mapped[str]=mapped_column(unique=True)
    day: Mapped[str]
    channel: Mapped[str]
    region: Mapped[str]
    revenue: Mapped[float]
    cost: Mapped[float]
    refunds: Mapped[float]
    orders: Mapped[int]
    reference_net: Mapped[float]
    # 替换批次标识：scope 内每次整月更正写入同一 batch_key，旧行随之删除。
    batch_key: Mapped[str|None]=mapped_column(nullable=True)
    scope_version: Mapped[int]=mapped_column(default=1)
