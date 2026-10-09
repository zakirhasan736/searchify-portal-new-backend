"""Changing a plan package must not reset an existing website limit."""

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import User
from app.security import get_current_user
from tests.db_fixtures import drop_user, make_user


def test_changing_plan_package_keeps_site_limit():
    admin = make_user(prefix="plan_admin", role="ROLE_ADMIN", plan="starter", site_limit=12)
    app.dependency_overrides[get_current_user] = lambda: admin
    client = TestClient(app)
    try:
        res = client.post("/api/v1/operator/workspace/plan", json={"plan": "agency", "user_id": admin.id})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["ok"] is True
        assert body["plan"] == "agency"
        assert body["limit"] == 12

        with SessionLocal() as db:
            row = db.get(User, admin.id)
            assert row.plan == "agency"
            assert row.site_limit == 12

        res = client.post(
            "/api/v1/operator/workspace/plan",
            json={"site_limit": 20, "user_id": admin.id},
        )
        assert res.status_code == 200, res.text
        assert res.json()["limit"] == 20
        assert res.json()["plan"] == "agency"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        drop_user(admin.id)
