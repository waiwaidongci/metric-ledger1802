import csv
import io
import math
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from ..config.settings import SOURCE_BATCH_MAX_ROWS
from ..models import Metric, SourceBatch, SourceRow, SourceScope
from ..utils.checksum import rows_checksum
from ..utils.clock import now
from ..utils.serialize import record
from .audit_service import log
from .pivot_service import build_report

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SOURCE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
REQUIRED_FIELDS = ("source_key", "day", "channel", "region", "revenue", "cost", "refunds", "orders", "reference_net")
AMOUNT_FIELDS = ("revenue", "cost", "refunds", "reference_net")
TEXT_FIELDS = ("day", "region")
MAX_ROWS = max(1,SOURCE_BATCH_MAX_ROWS)
MAX_AMOUNT = 1e12
MAX_ORDERS = 10_000_000
MAX_PREVIEW_ITEMS = 500
MAX_ERRORS = 50
PREVIEW_TTL = timedelta(minutes=30)


class BatchRejected(Exception):
    """整批拒绝：任何一行不合法都不落库。"""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.message = message
        self.errors = errors or []


def _error(row, field, message):
    return {"row": row, "field": field, "message": message}


def _field_text(value, row, field, errors, max_length=64):
    text = "" if value is None else str(value).strip()
    if not text:
        errors.append(_error(row, field, "不能为空"))
    elif len(text) > max_length:
        errors.append(_error(row, field, f"最多 {max_length} 个字符"))
    return text


def _parse_amount(value, row, field, errors):
    if isinstance(value, bool):
        errors.append(_error(row, field, "必须是数字，不能是布尔值"))
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = "" if value is None else str(value).strip()
        if not text:
            errors.append(_error(row, field, "不能为空"))
            return None
        try:
            number = float(text)
        except ValueError:
            errors.append(_error(row, field, f"不是合法数字：{text[:24]}"))
            return None
    if not math.isfinite(number):
        errors.append(_error(row, field, "必须是有限数字，禁止 NaN 或 Infinity"))
        return None
    if abs(number) > MAX_AMOUNT:
        errors.append(_error(row, field, "金额超出允许范围（|金额| ≤ 1e12）"))
        return None
    if field != "reference_net" and number < 0:
        errors.append(_error(row, field, "金额不能为负数"))
        return None
    return round(number, 2)


def _parse_orders(value, row, errors):
    if isinstance(value, bool):
        errors.append(_error(row, "orders", "必须是整数，不能是布尔值"))
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            errors.append(_error(row, "orders", "必须是整数"))
            return None
        number = int(value)
    else:
        text = "" if value is None else str(value).strip()
        if not text:
            errors.append(_error(row, "orders", "不能为空"))
            return None
        try:
            number = int(text, 10)
        except ValueError:
            errors.append(_error(row, "orders", "必须是整数"))
            return None
    if number < 0:
        errors.append(_error(row, "orders", "单数不能为负数"))
        return None
    if number > MAX_ORDERS:
        errors.append(_error(row, "orders", "单数超出允许范围"))
        return None
    return number


def _normalize_row(raw, row_number, scope_channel, scope_month, errors):
    before = len(errors)
    if not isinstance(raw, dict):
        errors.append(_error(row_number, "-", "每一行必须是对象"))
        return None
    unknown = set(raw) - set(REQUIRED_FIELDS) - {"__extra__"}
    missing = set(REQUIRED_FIELDS) - set(raw)
    if raw.get("__extra__"):
        errors.append(_error(row_number, "-", "该行列数多于表头，存在多余取值"))
    if unknown:
        errors.append(_error(row_number, "-", "未知字段：" + ", ".join(sorted(unknown))))
    if missing:
        errors.append(_error(row_number, "-", "缺少字段：" + ", ".join(sorted(missing))))
        return None

    source_key = _field_text(raw.get("source_key"), row_number, "source_key", errors, 64)
    if source_key and not SOURCE_KEY_PATTERN.match(source_key):
        errors.append(_error(row_number, "source_key", "仅允许字母、数字和 . _ : -，且以字母或数字开头"))

    day = _field_text(raw.get("day"), row_number, "day", errors, 10)
    if day:
        try:
            parsed_day = date.fromisoformat(day)
        except ValueError:
            parsed_day = None
            errors.append(_error(row_number, "day", "日期必须是 YYYY-MM-DD"))
        if parsed_day and parsed_day.isoformat() != day:
            errors.append(_error(row_number, "day", "日期必须是 YYYY-MM-DD"))
        elif parsed_day and day[:7] != scope_month:
            errors.append(_error(row_number, "day", f"日期 {day} 不在范围月份 {scope_month} 内（跨范围）"))

    channel = _field_text(raw.get("channel"), row_number, "channel", errors, 64)
    if channel and channel != scope_channel:
        errors.append(_error(row_number, "channel", f"渠道 {channel} 与所选范围 {scope_channel} 不一致（跨范围）"))

    region = _field_text(raw.get("region"), row_number, "region", errors, 64)
    amounts = {field: _parse_amount(raw.get(field), row_number, field, errors) for field in AMOUNT_FIELDS}
    orders = _parse_orders(raw.get("orders"), row_number, errors)

    if len(errors) > before:
        return None
    return {
        "source_key": source_key,
        "day": day,
        "channel": scope_channel,
        "region": region,
        "revenue": amounts["revenue"],
        "cost": amounts["cost"],
        "refunds": amounts["refunds"],
        "orders": orders,
        "reference_net": amounts["reference_net"],
    }


def _read_csv(content):
    text = content.lstrip("﻿")
    stream = io.StringIO(text)
    try:
        reader = csv.DictReader(stream, restkey="__extra__", restval="")
        headers = reader.fieldnames
        if not headers:
            raise BatchRejected("CSV 文件为空或缺少表头", [_error(1, "-", "表头必须为：" + ",".join(REQUIRED_FIELDS))])
        normalized_headers = [header.strip() for header in headers]
        if len(set(normalized_headers)) != len(normalized_headers):
            raise BatchRejected("CSV 表头存在重复列", [_error(1, "-", "请删除重复表头")])
        unknown = set(normalized_headers) - set(REQUIRED_FIELDS)
        missing = set(REQUIRED_FIELDS) - set(normalized_headers)
        if unknown or missing:
            errors = []
            if missing:
                errors.append(_error(1, "-", "缺少列：" + ", ".join(sorted(missing))))
            if unknown:
                errors.append(_error(1, "-", "未知列：" + ", ".join(sorted(unknown))))
            raise BatchRejected("CSV 表头与模板不一致", errors)
        rows = []
        for line_number, raw in enumerate(reader, start=2):
            values = [value for key, value in raw.items() if key != "__extra__"]
            if all((value is None or str(value).strip() == "") for value in values):
                continue
            normalized = {str(key).strip(): value for key, value in raw.items() if key is not None and key != "__extra__"}
            if raw.get("__extra__"):
                normalized["__extra__"] = raw["__extra__"]
            rows.append((line_number, normalized))
        return rows
    finally:
        stream.close()


def _collect_rows(data):
    if (data.content is None) == (data.rows is None):
        raise BatchRejected("必须且只能提供 content 或 rows 之一", [_error(0, "-", "上传 CSV 内容或结构化行二选一")])
    if data.content is not None:
        return _read_csv(data.content)
    return [(index, raw) for index, raw in enumerate(data.rows, start=1)]


def _validate_payload(data):
    channel = (data.channel or "").strip()
    month = (data.month or "").strip()
    if not channel or len(channel) > 64:
        raise BatchRejected("渠道不合法", [_error(0, "channel", "渠道不能为空且最多 64 个字符")])
    if not MONTH_PATTERN.match(month):
        raise BatchRejected("月份不合法", [_error(0, "month", "月份必须是 YYYY-MM")])
    year, month_number = (int(part) for part in month.split("-"))
    try:
        date(year, month_number, 1)
    except ValueError:
        raise BatchRejected("月份不合法", [_error(0, "month", "月份超出有效范围")])
    source_rows = _collect_rows(data)
    if not source_rows:
        raise BatchRejected("批次为空", [_error(0, "-", "至少需要一行替换明细")])
    if len(source_rows) > MAX_ROWS:
        raise BatchRejected("批次过大", [_error(0, "-", f"单批最多 {MAX_ROWS} 行")])

    errors = []
    normalized = []
    for row_number, raw in source_rows:
        row = _normalize_row(raw, row_number, channel, month, errors)
        if row is not None:
            normalized.append(row)
    if errors:
        raise BatchRejected("存在坏行，整批已拒绝", errors[:MAX_ERRORS])

    seen = {}
    for row in normalized:
        seen.setdefault(row["source_key"], 0)
        seen[row["source_key"]] += 1
    duplicates = [key for key, count in seen.items() if count > 1]
    if duplicates:
        positions = {}
        for row_number, raw in source_rows:
            key = str(raw.get("source_key", "")).strip()
            if key in duplicates:
                positions.setdefault(key, []).append(row_number)
        errors = [
            _error(positions[key][0], "source_key", f"来源编号重复 {len(positions[key])} 次：{key}")
            for key in duplicates
        ]
        raise BatchRejected("来源编号重复，整批已拒绝", errors[:MAX_ERRORS])
    return channel, month, normalized


def _scope_rows(db, channel, month):
    return (
        db.query(SourceRow)
        .filter(SourceRow.channel == channel, SourceRow.day.like(month + "-%"))
        .order_by(SourceRow.source_key)
        .all()
    )


def _scope_state(db, channel, month):
    rows = _scope_rows(db, channel, month)
    scope = db.query(SourceScope).filter_by(channel=channel, month=month).one_or_none()
    version = scope.version if scope else 0
    return rows, version, rows_checksum(rows)


def _ensure_scope(db, channel, month, checksum):
    """为范围建立版本登记行；并发首次创建时用保存点容忍唯一键冲突。"""
    scope = db.query(SourceScope).filter_by(channel=channel, month=month).one_or_none()
    if scope:
        return scope
    try:
        with db.begin_nested():
            db.add(SourceScope(channel=channel, month=month, version=0, checksum=checksum))
            db.flush()
    except IntegrityError:
        pass
    scope = db.query(SourceScope).filter_by(channel=channel, month=month).one_or_none()
    if scope:
        return scope
    raise HTTPException(409, "来源范围版本登记失败，请重新预览")


def _cross_scope_errors(db, rows, channel, month, positions):
    errors = []
    keys = [row["source_key"] for row in rows]
    for start in range(0, len(keys), 400):
        chunk = keys[start : start + 400]
        existing = db.query(SourceRow).filter(SourceRow.source_key.in_(chunk)).all()
        for row in existing:
            if row.channel != channel or row.day[:7] != month:
                errors.append(
                    _error(
                        positions.get(row.source_key, 0),
                        "source_key",
                        f"来源编号 {row.source_key} 已属于 {row.channel}/{row.day[:7]}，不能跨范围替换",
                    )
                )
    return errors


def _row_dict(row):
    return record(row) if not isinstance(row, dict) else dict(row)


def _totals(rows):
    def total(field):
        return sum((row[field] if isinstance(row, dict) else getattr(row, field)) for row in rows)

    revenue = round(total("revenue"), 6)
    refunds = round(total("refunds"), 6)
    reference = round(total("reference_net"), 6)
    canonical = round(revenue - refunds, 6)
    return {
        "revenue": revenue,
        "cost": round(total("cost"), 6),
        "refunds": refunds,
        "orders": total("orders"),
        "reference_net": reference,
        "canonical_net": canonical,
        "difference": round(canonical - reference, 6),
    }


def _metric_values(db, rows):
    metrics = db.query(Metric).order_by(Metric.id).all()
    if not metrics:
        return None
    if not rows:
        return {}
    objects = [SimpleNamespace(**row) for row in rows] if isinstance(rows[0], dict) else rows  # 预览行不是 ORM 对象，需补齐属性访问
    try:
        report = build_report(objects, metrics, ["channel", "month"])
    except ValueError as exc:
        raise BatchRejected("新批次无法按当前口径计算，整批已拒绝", [_error(0, "-", str(exc))])
    return report["total"]["values"]


def _delta(before, after):
    if before is None or after is None:
        return None
    return {key: round(after[key] - before[key], 6) for key in after if key in before}


def _build_preview(db, channel, month, rows, scope_rows, scope_version, scope_checksum):
    positions = {row["source_key"]: index for index, row in enumerate(rows, start=1)}
    cross_scope = _cross_scope_errors(db, rows, channel, month, positions)
    if cross_scope:
        raise BatchRejected("来源编号跨范围，整批已拒绝", cross_scope[:MAX_ERRORS])

    before_map = {row.source_key: _row_dict(row) for row in scope_rows}
    after_map = {row["source_key"]: dict(row) for row in rows}
    added_keys = sorted(set(after_map) - set(before_map))
    removed_keys = sorted(set(before_map) - set(after_map))
    common_keys = sorted(set(after_map) & set(before_map))
    changed = []
    unchanged = 0
    for key in common_keys:
        before = before_map[key]
        after = after_map[key]
        changes = []
        for field in ("day", "region", "revenue", "cost", "refunds", "orders", "reference_net"):
            if before[field] != after[field]:
                item = {"field": field, "before": before[field], "after": after[field]}
                if isinstance(before[field], (int, float)) and isinstance(after[field], (int, float)):
                    item["delta"] = round(after[field] - before[field], 6)
                changes.append(item)
        if changes:
            changed.append({"source_key": key, "before": before, "after": after, "changes": changes})
        else:
            unchanged += 1

    before_totals = _totals(scope_rows)
    after_totals = _totals(rows)
    totals_delta = {key: round(after_totals[key] - before_totals[key], 6) for key in before_totals}

    metrics_before = _metric_values(db, scope_rows)
    metrics_after = _metric_values(db, rows)
    metrics_delta = _delta(metrics_before, metrics_after)

    return {
        "scope": {
            "channel": channel,
            "month": month,
            "version": scope_version,
            "checksum": scope_checksum,
        },
        "checksum": rows_checksum(rows),
        "row_count": len(rows),
        "counts": {
            "existing": len(scope_rows),
            "added": len(added_keys),
            "removed": len(removed_keys),
            "changed": len(changed),
            "unchanged": unchanged,
        },
        "added": [after_map[key] for key in added_keys[:MAX_PREVIEW_ITEMS]],
        "removed": [before_map[key] for key in removed_keys[:MAX_PREVIEW_ITEMS]],
        "changed": changed[:MAX_PREVIEW_ITEMS],
        "truncated": any(
            len(items) > MAX_PREVIEW_ITEMS
            for items in (added_keys, removed_keys, changed)
        ),
        "totals": {"before": before_totals, "after": after_totals, "delta": totals_delta},
        "net_revenue_delta": round(after_totals["canonical_net"] - before_totals["canonical_net"], 6),
        "metrics": {"before": metrics_before, "after": metrics_after, "delta": metrics_delta},
        "reconciliation_rule": "确认后本渠道月份将以完整新批替换；核对与新报表读取新批，历史报表快照保持不变。",
    }


def preview_batch(db, data, user):
    try:
        channel, month, rows = _validate_payload(data)
        scope_rows, scope_version, scope_checksum = _scope_state(db, channel, month)
        preview = _build_preview(db, channel, month, rows, scope_rows, scope_version, scope_checksum)
    except BatchRejected as exc:
        raise HTTPException(422, {"message": exc.message, "errors": exc.errors})

    batch = SourceBatch(
        batch_key=uuid.uuid4().hex,
        channel=channel,
        month=month,
        status="preview",
        scope_version=scope_version,
        scope_checksum=scope_checksum,
        row_count=len(rows),
        rows=rows,
        rows_checksum=rows_checksum(rows),
        preview=preview,
        created_by=user.email,
    )
    db.add(batch)
    db.flush()
    log(db, user.email, "source.batch.preview", f"{channel}·{month} #{batch.id}")
    db.commit()
    return {"batch_id": batch.id, **preview, "status": "preview", "created_at": batch.created_at}


def _applied_response(batch, already_applied):
    return {
        "batch_id": batch.id,
        "status": "applied",
        "already_applied": already_applied,
        "channel": batch.channel,
        "month": batch.month,
        "row_count": batch.row_count,
        "checksum": batch.applied_checksum or batch.rows_checksum,
        "scope_version": batch.scope_version + 1,
        "applied_by": batch.applied_by,
        "applied_at": batch.applied_at,
    }


def apply_batch(db, batch_id, user):
    batch = db.get(SourceBatch, batch_id)
    if not batch:
        raise HTTPException(404, "替换批次不存在")
    if batch.status == "applied":
        log(db, user.email, "source.batch.replay", f"{batch.channel}·{batch.month} #{batch.id}")
        db.commit()
        return _applied_response(batch, True)
    if batch.status != "preview":
        raise HTTPException(409, "批次正在处理中，请稍后刷新查看")

    if rows_checksum(batch.rows) != batch.rows_checksum:
        raise HTTPException(409, "批次内容校验失败，请重新预览后再确认")

    # 允许超过 30 分钟的预览间隔，但不能无限期确认旧批次。
    if batch.created_at[:19] <= (datetime.now(timezone.utc) - PREVIEW_TTL).isoformat()[:19]:
        db.rollback()
        raise HTTPException(409, "预览已过期，请重新预览后再确认")

    scope_rows, scope_version, scope_checksum = _scope_state(db, batch.channel, batch.month)
    if scope_version != batch.scope_version or scope_checksum != batch.scope_checksum:
        raise HTTPException(409, "来源在预览后已变化，旧批次已拒绝，请重新预览")

    # 首次更正某范围时先登记版本行（版本 0），使确认时的条件更新能原子抢占。
    scope = _ensure_scope(db, batch.channel, batch.month, scope_checksum)

    claimed = db.execute(
        update(SourceBatch)
        .where(SourceBatch.id == batch.id, SourceBatch.status == "preview")
        .values(status="applying", applied_by=user.name, applied_at=now())
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        fresh = db.get(SourceBatch, batch.id)
        if fresh and fresh.status == "applied":
            log(db, user.email, "source.batch.replay", f"{fresh.channel}·{fresh.month} #{fresh.id}")
            db.commit()
            return _applied_response(fresh, True)
        raise HTTPException(409, "批次正在被其他操作确认，请稍后刷新")

    # 重新校验落库前的存储内容，防止批次记录被篡改后直接生效。
    errors = []
    for index, raw in enumerate(batch.rows, start=1):
        if index > MAX_ROWS:
            errors.append(_error(index, "-", f"单批最多 {MAX_ROWS} 行"))
            break
        row = _normalize_row(raw, index, batch.channel, batch.month, errors)
        if row is None:
            break
    if errors:
        db.rollback()
        raise HTTPException(409, "批次内容已损坏，请重新预览后再确认")
    positions = {row["source_key"]: index for index, row in enumerate(batch.rows, start=1)}
    cross_scope = _cross_scope_errors(db, batch.rows, batch.channel, batch.month, positions)
    if cross_scope:
        db.rollback()
        raise HTTPException(409, "来源编号已跨范围，请重新预览")
    new_version = scope_version + 1
    new_checksum = rows_checksum(batch.rows)
    # 条件更新版本号：并发批次只有一个能从这里通过，其余整批放弃。
    advanced = db.execute(
        update(SourceScope)
        .where(SourceScope.id == scope.id, SourceScope.version == batch.scope_version)
        .values(version=new_version, checksum=new_checksum, updated_at=now())
        .execution_options(synchronize_session=False)
    )
    if advanced.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "来源在预览后已被其他更正替换，旧批次已拒绝，请重新预览")

    db.query(SourceRow).filter(
        SourceRow.channel == batch.channel, SourceRow.day.like(batch.month + "-%")
    ).delete(synchronize_session=False)
    # 让会话丢弃已删除的旧行身份，再插入同编号的新行。
    db.expunge_all()
    for row in batch.rows:
        db.add(SourceRow(**row, batch_key=batch.batch_key, scope_version=new_version))

    batch = db.get(SourceBatch, batch_id)
    batch.status = "applied"
    batch.applied_by = batch.applied_by or user.name
    batch.applied_at = batch.applied_at or now()
    batch.applied_checksum = new_checksum
    log(db, user.email, "source.batch.apply", f"{batch.channel}·{batch.month} #{batch.id}")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "来源编号与其他范围冲突，整批未生效，请重新预览")
    return _applied_response(batch, False)


def list_batches(db):
    batches = db.query(SourceBatch).order_by(SourceBatch.id.desc()).limit(30).all()
    return [
        {
            "batch_id": batch.id,
            "channel": batch.channel,
            "month": batch.month,
            "status": batch.status,
            "row_count": batch.row_count,
            "counts": batch.preview.get("counts", {}),
            "net_revenue_delta": batch.preview.get("net_revenue_delta"),
            "created_by": batch.created_by,
            "created_at": batch.created_at,
            "applied_by": batch.applied_by,
            "applied_at": batch.applied_at,
        }
        for batch in batches
    ]


def scope_export_rows(db, channel, month):
    if channel not in {row[0] for row in db.query(SourceRow.channel).distinct()}:
        raise HTTPException(404, "该渠道没有来源明细")
    if not MONTH_PATTERN.match(month):
        raise HTTPException(422, "月份必须是 YYYY-MM")
    rows = _scope_rows(db, channel, month)
    return rows


def scope_csv(db, channel, month):
    rows = scope_export_rows(db, channel, month)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(REQUIRED_FIELDS)
    for row in rows:
        writer.writerow([getattr(row, field) for field in REQUIRED_FIELDS])
    return out.getvalue()
