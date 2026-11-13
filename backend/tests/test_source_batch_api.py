import asyncio
import csv
import io

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config.database import Base, get_db
from src.controllers.dependencies import current_user
from src.models import Metric, ReportRun, SourceRow, User
from src.routes import source_batches, sources
from src.services.pivot_service import build_report


@pytest.fixture()
def app(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(email="analyst@hengchi.demo", name="宋予", role="analyst", password_hash="x")
        db.add(user)
        db.add(Metric(key="net_revenue", name="净收入", expression="revenue-refunds", unit="元", description="渠道收入减退款。", owner="陆知衡"))
        db.commit()
        db.add(ReportRun(name="历史快照", dimensions=["channel", "month"], status="completed",
                         result=build_report([], [db.query(Metric).one()], ["channel", "month"]) if False else {"kept": True}, owner="宋予"))
        db.commit()
        user_id = user.id
    application = FastAPI()
    application.include_router(sources.router)
    application.include_router(source_batches.router)

    def override_db():
        with factory() as session:
            yield session

    def override_user():
        with factory() as session:
            return session.get(User, user_id)

    application.dependency_overrides[get_db] = override_db
    application.dependency_overrides[current_user] = override_user
    return application, factory


def call(app, method, path, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def sample(key="HC-0001", revenue=100):
    return {
        "source_key": key,
        "day": "2026-09-01",
        "channel": "直营渠道",
        "region": "华东",
        "revenue": revenue,
        "cost": 50,
        "refunds": 5,
        "orders": 3,
        "reference_net": revenue - 5,
    }


def test_api_preview_confirm_and_scope_endpoints(app):
    application, factory = app
    with factory() as db:
        db.add(SourceRow(**sample("OLD-1", 100), scope_version=1))
        db.commit()

    preview = call(application, "POST", "/source-batches/preview", json={"channel": "直营渠道", "month": "2026-09", "rows": [sample("NEW-1", 500)]})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["counts"]["added"] == 1 and body["counts"]["removed"] == 1
    assert body["net_revenue_delta"] == pytest.approx(400.0)
    assert body["totals"]["before"]["canonical_net"] == pytest.approx(95.0)

    confirmed = call(application, "POST", f"/source-batches/{body['batch_id']}/confirm")
    assert confirmed.status_code == 200 and confirmed.json()["already_applied"] is False
    replay = call(application, "POST", f"/source-batches/{body['batch_id']}/confirm")
    assert replay.status_code == 200 and replay.json()["already_applied"] is True

    scopes = call(application, "GET", "/sources/scopes").json()
    assert scopes[0]["channel"] == "直营渠道" and scopes[0]["row_count"] == 1

    rows = call(application, "GET", "/sources/scope/直营渠道/2026-09").json()
    assert [row["source_key"] for row in rows] == ["NEW-1"]

    batches = call(application, "GET", "/source-batches").json()
    assert batches[0]["status"] == "applied" and batches[0]["batch_id"] == body["batch_id"]


def test_api_rejection_shape_for_bad_rows(app):
    application, _ = app
    response = call(application, "POST", "/source-batches/preview", json={"channel": "直营渠道", "month": "2026-09", "rows": [sample(), sample()]})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "重复" in detail["message"]
    assert detail["errors"][0]["field"] == "source_key"


def test_api_rejects_viewer_write(app):
    application, factory = app
    with factory() as db:
        viewer = User(email="viewer@hengchi.demo", name="叶言", role="viewer", password_hash="x")
        db.add(viewer)
        db.commit()
        viewer_id = viewer.id
    application.dependency_overrides[current_user] = lambda: factory().get(User, viewer_id)
    response = call(application, "POST", "/source-batches/preview", json={"channel": "直营渠道", "month": "2026-09", "rows": [sample()]})
    assert response.status_code == 403


def test_api_stale_batch_conflict(app):
    application, factory = app
    with factory() as db:
        db.add(SourceRow(**sample("HC-1", 100), scope_version=1))
        db.commit()
    first = call(application, "POST", "/source-batches/preview", json={"channel": "直营渠道", "month": "2026-09", "rows": [sample("HC-1", 200)]}).json()
    second = call(application, "POST", "/source-batches/preview", json={"channel": "直营渠道", "month": "2026-09", "rows": [sample("HC-1", 300)]}).json()
    assert call(application, "POST", f"/source-batches/{second['batch_id']}/confirm").status_code == 200
    stale = call(application, "POST", f"/source-batches/{first['batch_id']}/confirm")
    assert stale.status_code == 409
    assert "预览后已变化" in stale.json()["detail"]


def test_export_returns_csv_of_current_scope(app):
    application, factory = app
    with factory() as db:
        db.add(SourceRow(**sample("HC-9", 42), scope_version=1))
        db.commit()
    response = call(application, "GET", "/source-batches/直营渠道/2026-09/export")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip("﻿"))))
    assert rows[0]["source_key"] == "HC-9" and rows[0]["revenue"] == "42.0"


def test_historical_report_snapshot_stays_unchanged(app):
    application, factory = app
    with factory() as db:
        db.add(SourceRow(**sample("HC-1", 100), scope_version=1))
        db.commit()
        metric = db.query(Metric).one()
        run = ReportRun(name="历史报表", dimensions=["channel", "month"], status="completed",
                        result=build_report(db.query(SourceRow).all(), [metric], ["channel", "month"]), owner="宋予")
        db.add(run)
        db.commit()
        historical = run.result["total"]["canonical_net"]
        run_id = run.id
    call(application, "POST", "/source-batches/preview", json={"channel": "直营渠道", "month": "2026-09", "rows": [sample("HC-1", 800)]})
    call(application, "POST", f"/source-batches/{1}/confirm")
    with factory() as db:
        stored = db.get(ReportRun, run_id)
        assert stored.result["total"]["canonical_net"] == historical
        fresh = build_report(db.query(SourceRow).all(), db.query(Metric).all(), ["channel", "month"])
        assert fresh["total"]["canonical_net"] == pytest.approx(795.0)
