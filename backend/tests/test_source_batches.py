import csv
import io

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config.database import Base
from src.models import Metric, SourceBatch, SourceRow, SourceScope, User
from src.schemas.source_batch import BatchInput
from src.services.source_batch_service import apply_batch, preview_batch, scope_csv
from src.services.source_service import reconciliation


@pytest.fixture()
def session_factory(tmp_path):
    path = tmp_path / "batch.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 10})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def make():
        return factory()

    return make


@pytest.fixture()
def session(session_factory):
    with session_factory() as db:
        yield db


@pytest.fixture()
def user(session):
    row = User(email="analyst@hengchi.demo", name="宋予", role="analyst", password_hash="x")
    session.add(row)
    session.add(Metric(key="net_revenue", name="净收入", expression="revenue-refunds", unit="元", description="渠道收入减退款。", owner="陆知衡"))
    session.commit()
    return row


def row(key, day="2026-09-01", channel="直营渠道", region="华东", revenue=100.0, cost=50.0, refunds=5.0, orders=3, reference_net=None):
    payload = {
        "source_key": key,
        "day": day,
        "channel": channel,
        "region": region,
        "revenue": revenue,
        "cost": cost,
        "refunds": refunds,
        "orders": orders,
    }
    if reference_net is None:
        try:
            payload["reference_net"] = round(float(revenue) - float(refunds), 2)
        except (TypeError, ValueError):
            payload["reference_net"] = 0.0
    else:
        payload["reference_net"] = reference_net
    return payload


def seed_scope(session, channel="直营渠道", month="2026-09", keys=("HC-0000", "HC-0001", "HC-0002")):
    for index, key in enumerate(keys):
        session.add(SourceRow(**row(key, day=f"{month}-{index + 1:02}", channel=channel), scope_version=1))
    session.commit()


def expect_rejection(call):
    with pytest.raises(HTTPException) as caught:
        call()
    assert caught.value.status_code == 422
    detail = caught.value.detail
    assert isinstance(detail, dict) and detail["errors"], detail
    return detail


def preview(session, user, rows, month="2026-09", channel="直营渠道", content=None):
    data = BatchInput(channel=channel, month=month, rows=rows) if content is None else BatchInput(channel=channel, month=month, content=content)
    return preview_batch(session, data, user)


def test_preview_reports_add_remove_change_and_net_delta(session, user):
    seed_scope(session)
    result = preview(
        session,
        user,
        [
            row("HC-0000"),
            row("HC-0001", revenue=120.0),
            row("HC-0009", day="2026-09-09", revenue=30.0),
        ],
    )
    assert result["counts"] == {"existing": 3, "added": 1, "removed": 1, "changed": 1, "unchanged": 1}
    assert result["net_revenue_delta"] == pytest.approx(235.0 - 285.0)
    assert result["totals"]["before"]["canonical_net"] == pytest.approx(285.0)
    assert result["totals"]["after"]["canonical_net"] == pytest.approx(235.0)
    assert result["totals"]["delta"]["canonical_net"] == pytest.approx(-50.0)
    assert result["added"][0]["source_key"] == "HC-0009"
    assert result["removed"][0]["source_key"] == "HC-0002"
    assert result["changed"][0]["source_key"] == "HC-0001"
    fields = {item["field"]: item for item in result["changed"][0]["changes"]}
    assert fields["revenue"] == {"field": "revenue", "before": 100.0, "after": 120.0, "delta": 20.0}
    assert result["metrics"]["delta"]["net_revenue"] == pytest.approx(-50.0)
    # 预览不得改动来源或写入来源行。
    assert session.query(SourceRow).count() == 3
    assert session.query(SourceBatch).count() == 1


def test_duplicate_source_key_rejects_whole_batch(session, user):
    seed_scope(session)
    detail = expect_rejection(lambda: preview(session, user, [row("A-1"), row("A-1"), row("A-2")]))
    assert "重复" in detail["message"]
    assert session.query(SourceBatch).count() == 0
    assert session.query(SourceRow).count() == 3


@pytest.mark.parametrize(
    "bad",
    [
        {"channel": "书店合作", "day": "2026-09-01"},
        {"channel": "直营渠道", "day": "2026-10-01"},
        {"channel": "直营渠道", "day": "2026-08-31"},
    ],
)
def test_cross_scope_rejects(session, user, bad):
    seed_scope(session)
    detail = expect_rejection(lambda: preview(session, user, [row("A-1", **bad)]))
    assert "跨范围" in "".join(item["message"] for item in detail["errors"])


def test_source_key_owned_by_other_scope_rejects(session, user):
    seed_scope(session, channel="书店合作", month="2026-10", keys=("HC-0500",))
    detail = expect_rejection(lambda: preview(session, user, [row("HC-0500")]))
    assert "跨范围" in "".join(item["message"] for item in detail["errors"])
    assert "HC-0500" in "".join(item["message"] for item in detail["errors"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("revenue", "abc"),
        ("revenue", -1),
        ("cost", float("nan")),
        ("refunds", float("inf")),
        ("orders", 2.5),
        ("orders", -3),
        ("day", "2026-13-01"),
        ("day", "2026-9-1"),
        ("reference_net", "not-a-number"),
    ],
)
def test_invalid_values_reject_whole_batch(session, user, field, value):
    seed_scope(session)
    detail = expect_rejection(lambda: preview(session, user, [row("A-1"), row("A-2", **{field: value})]))
    assert any(item["row"] == 2 for item in detail["errors"]), detail
    assert session.query(SourceBatch).count() == 0
    assert session.query(SourceRow).count() == 3


def test_missing_or_blank_source_key_rejected(session, user):
    seed_scope(session)
    broken = row("A-2")
    broken["source_key"] = "   "
    expect_rejection(lambda: preview(session, user, [row("A-1"), broken]))
    missing = row("A-3")
    del missing["region"]
    expect_rejection(lambda: preview(session, user, [missing]))


def test_csv_bad_header_and_ragged_rows_rejected(session, user):
    seed_scope(session)
    missing_column = "source_key,day,channel,region,revenue,cost,refunds,orders\nA-1,2026-09-01,直营渠道,华东,10,5,1,2\n"
    expect_rejection(lambda: preview(session, user, None, content=missing_column))

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["source_key", "day", "channel", "region", "revenue", "cost", "refunds", "orders", "reference_net"])
    writer.writerow(["A-1", "2026-09-01", "直营渠道", "华东", 10, 5, 1, 2, 9, "surplus"])
    detail = expect_rejection(lambda: preview(session, user, None, content=out.getvalue()))
    assert "列数" in "".join(item["message"] for item in detail["errors"])

    # 空批次同样整批拒绝。
    empty = "source_key,day,channel,region,revenue,cost,refunds,orders,reference_net\n"
    expect_rejection(lambda: preview(session, user, None, content=empty))


def test_csv_roundtrip_and_confirm(session_factory, user):
    with session_factory() as session:
        seed_scope(session)
        content = scope_csv(session, "直营渠道", "2026-09")
        rows = list(csv.DictReader(io.StringIO(content)))
        rows[0]["revenue"] = "500.00"
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        result = preview(session, user, None, content=out.getvalue())
        assert result["counts"]["changed"] == 1
        applied = apply_batch(session, result["batch_id"], user)
        assert applied["status"] == "applied" and applied["already_applied"] is False
        assert applied["scope_version"] == 1
    with session_factory() as check:
        updated = check.query(SourceRow).filter_by(source_key=rows[0]["source_key"]).one()
        assert updated.revenue == pytest.approx(500.0)
        assert updated.scope_version == 1
        assert check.query(SourceRow).count() == 3


def test_confirm_replaces_scope_and_affects_reconciliation(session_factory, user):
    with session_factory() as session:
        seed_scope(session, channel="直营渠道", month="2026-09")
        seed_scope(session, channel="书店合作", month="2026-10", keys=("HC-0500", "HC-0501"))
        before = reconciliation(session)
        result = preview(session, user, [row("HC-0000", revenue=1000.0)])
        apply_batch(session, result["batch_id"], user)
    with session_factory() as check:
        assert check.query(SourceRow).filter(SourceRow.channel == "直营渠道").count() == 1
        assert check.query(SourceRow).filter(SourceRow.channel == "书店合作").count() == 2
        after = reconciliation(check)
        assert after["total"]["canonical_net"] - before["total"]["canonical_net"] == pytest.approx(
            result["net_revenue_delta"]
        )
        scope = check.query(SourceScope).filter_by(channel="直营渠道", month="2026-09").one()
        assert scope.version == 1
        assert check.query(SourceRow).filter_by(channel="直营渠道").one().scope_version == 1


def test_stale_preview_rejected_after_source_changes(session_factory, user):
    with session_factory() as session:
        seed_scope(session)
        first = preview(session, user, [row("HC-0000", revenue=200.0)])
        second = preview(session, user, [row("HC-0000", revenue=300.0)])
        apply_batch(session, second["batch_id"], user)
        with pytest.raises(HTTPException) as caught:
            apply_batch(session, first["batch_id"], user)
        assert caught.value.status_code == 409
        assert "预览后已变化" in caught.value.detail
    with session_factory() as check:
        assert check.query(SourceRow).filter_by(source_key="HC-0000").one().revenue == pytest.approx(300.0)


def test_stale_preview_rejected_when_rows_edited_outside_batch(session_factory, user):
    with session_factory() as session:
        seed_scope(session)
        pending = preview(session, user, [row("HC-0000", revenue=200.0)])
    with session_factory() as outside:
        outside.query(SourceRow).filter_by(source_key="HC-0002").update({"revenue": 999.0})
        outside.commit()
    with session_factory() as session:
        with pytest.raises(HTTPException) as caught:
            apply_batch(session, pending["batch_id"], user)
        assert caught.value.status_code == 409
        assert "预览后已变化" in caught.value.detail


def test_replay_same_batch_is_idempotent(session_factory, user):
    with session_factory() as session:
        seed_scope(session)
        pending = preview(session, user, [row("HC-0000", revenue=321.0)])
        first = apply_batch(session, pending["batch_id"], user)
    responses = []
    for _ in range(2):
        with session_factory() as session:
            responses.append(apply_batch(session, pending["batch_id"], user))
    for response in responses:
        assert response["already_applied"] is True
        assert response["checksum"] == first["checksum"]
        assert response["scope_version"] == first["scope_version"]
    with session_factory() as check:
        assert check.query(SourceRow).count() == 1
        assert check.query(SourceRow).filter_by(source_key="HC-0000").one().scope_version == 1
        assert check.query(SourceScope).one().version == 1


def test_concurrent_confirmation_only_one_wins(session_factory, user):
    with session_factory() as setup:
        seed_scope(setup, keys=("HC-0000", "HC-0001"))
    with session_factory() as session:
        first = preview(session, user, [row("HC-0000", revenue=111.0)])
    with session_factory() as session:
        second = preview(session, user, [row("HC-0000", revenue=222.0)])

    with session_factory() as a, session_factory() as b:
        assert apply_batch(a, first["batch_id"], user)["status"] == "applied"
        with pytest.raises(HTTPException) as caught:
            apply_batch(b, second["batch_id"], user)
        assert caught.value.status_code == 409
    with session_factory() as check:
        rows = check.query(SourceRow).all()
        assert len(rows) == 1 and rows[0].revenue == pytest.approx(111.0)
        assert check.query(SourceScope).one().version == 1


def test_confirm_twice_on_same_batch_under_race_returns_applied(session_factory, user):
    with session_factory() as setup:
        seed_scope(setup, keys=("HC-0000",))
    with session_factory() as session:
        pending = preview(session, user, [row("HC-0000", revenue=88.0)])
    with session_factory() as a, session_factory() as b:
        one = apply_batch(a, pending["batch_id"], user)
        two = apply_batch(b, pending["batch_id"], user)
        assert one["already_applied"] is False
        assert two["already_applied"] is True
    with session_factory() as check:
        assert check.query(SourceRow).count() == 1


def test_tampered_batch_rows_rejected(session_factory, user):
    with session_factory() as session:
        seed_scope(session)
        pending = preview(session, user, [row("HC-0000", revenue=200.0)])
    with session_factory() as tamper:
        batch = tamper.get(SourceBatch, pending["batch_id"])
        rows = [dict(item) for item in batch.rows]
        rows[0]["revenue"] = 999999.0
        batch.rows = rows
        tamper.commit()
    with session_factory() as session:
        with pytest.raises(HTTPException) as caught:
            apply_batch(session, pending["batch_id"], user)
        assert caught.value.status_code == 409
    with session_factory() as check:
        assert check.query(SourceRow).filter_by(source_key="HC-0000").one().revenue == pytest.approx(100.0)

