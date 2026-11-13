from typing import Literal

from pydantic import BaseModel, Field

from ..config.settings import SOURCE_BATCH_MAX_ROWS


class BatchInput(BaseModel):
    channel: str = Field(min_length=1, max_length=64)
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    content: str | None = Field(default=None, max_length=4_000_000)
    rows: list[dict] | None = Field(default=None, max_length=SOURCE_BATCH_MAX_ROWS)


class BatchConfirm(BaseModel):
    batch_id: int = Field(gt=0)


class BatchCounts(BaseModel):
    existing: int
    added: int
    removed: int
    changed: int
    unchanged: int


class BatchTotals(BaseModel):
    before: dict
    after: dict
    delta: dict


class BatchMetrics(BaseModel):
    before: dict | None = None
    after: dict | None = None
    delta: dict | None = None


class BatchPreviewOut(BaseModel):
    batch_id: int
    status: str
    scope: dict
    checksum: str
    row_count: int
    counts: BatchCounts
    added: list[dict]
    removed: list[dict]
    changed: list[dict]
    truncated: bool
    totals: BatchTotals
    net_revenue_delta: float
    metrics: BatchMetrics
    reconciliation_rule: str
    created_at: str


class BatchAppliedOut(BaseModel):
    batch_id: int
    status: Literal["applied"]
    already_applied: bool
    channel: str
    month: str
    row_count: int
    checksum: str
    scope_version: int
    applied_by: str | None
    applied_at: str | None


class BatchErrorOut(BaseModel):
    row: int
    field: str
    message: str


class BatchRejection(BaseModel):
    detail: dict
