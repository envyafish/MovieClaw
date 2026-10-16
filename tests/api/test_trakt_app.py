"""Trakt 应用凭证：只有管理员能写，Secret 加密落库且不回显。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.media_discover import reset_media_service
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import SecretBox, reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.repositories.setting_repo import SettingRepository

_APP = "/api/v1/integrations/trakt/app"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TMDB_API_KEY", "")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    reset_media_service()

    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        c.post("/api/v1/auth/bootstrap", json={"username": "admin", "password": "s3cret-pass"})
        yield c

    reset_media_service()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def test_member_cannot_read_trakt_app(client: TestClient) -> None:
    client.post("/api/v1/members", json={"username": "family", "password": "family-pass-1"})
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login", json={"username": "family", "password": "family-pass-1"})
    assert client.get(_APP).status_code == 403


def test_admin_saves_client_id_and_secret_is_not_returned(client: TestClient) -> None:
    saved = client.put(_APP, json={"client_id": "cid", "client_secret": "super-secret"})
    assert saved.status_code == 200
    assert saved.json()["data"] == {"client_id": "cid", "secret_set": True}
    assert "super-secret" not in saved.text
    again = client.get(_APP)
    assert again.json()["data"]["secret_set"] is True


def test_blank_secret_keeps_the_stored_secret(client: TestClient) -> None:
    client.put(_APP, json={"client_id": "cid", "client_secret": "super-secret"})
    kept = client.put(_APP, json={"client_id": "cid", "client_secret": ""})
    assert kept.status_code == 200
    assert kept.json()["data"]["secret_set"] is True


async def test_secret_is_encrypted_at_rest(client: TestClient) -> None:
    client.put(_APP, json={"client_id": "cid", "client_secret": "super-secret"})
    async with get_database().session() as session:
        row = await SettingRepository(session).get("integration.trakt")
    assert row is not None
    assert "super-secret" not in row.value_json
    raw = json.loads(row.value_json)
    assert SecretBox.is_encrypted(raw["client_secret"])
