"""每个用户用设备码连接自己的 Trakt。Trakt 本身用假客户端。"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.media_discover import reset_media_service
from movieclaw_api.services.trakt_connection import TraktConnectionStore
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import SecretBox, reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.repositories.setting_repo import SettingRepository
from movieclaw_media.trakt import DeviceCode, TokenBundle, TraktError, TraktPending

_APP = "/api/v1/integrations/trakt/app"
_CONN = "/api/v1/integrations/trakt/connection"
_POLL = "/api/v1/integrations/trakt/connection/poll"


class FakeTrakt:
    def __init__(self, *, interval: int = 5, username_error: bool = False) -> None:
        self.interval = interval
        self.username_error = username_error
        self.token_calls = 0

    async def device_code(self) -> DeviceCode:
        return DeviceCode("device-secret", "ABCD1234", "https://trakt.tv/activate", 600, self.interval)

    async def device_token(self, code: str) -> TokenBundle:
        self.token_calls += 1
        if self.token_calls == 1:
            raise TraktPending("waiting")
        return TokenBundle("access", "refresh", 86400)

    async def username(self, access_token: str) -> str:
        if self.username_error:
            raise TraktError("settings failed")
        return "ada"

    async def refresh(self, refresh_token: str) -> TokenBundle:
        return TokenBundle("access-2", "refresh-2", 86400)


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


def _install(monkeypatch, fake: FakeTrakt) -> None:
    async def get_client():
        return fake

    monkeypatch.setattr("movieclaw_api.services.trakt_connection.get_trakt_client", get_client)


def _save_app(client: TestClient, *, client_id: str = "cid", secret: str = "super-secret") -> None:
    assert client.put(_APP, json={"client_id": client_id, "client_secret": secret}).status_code == 200


def _login(client: TestClient, username: str, password: str) -> None:
    client.post("/api/v1/auth/logout")
    assert client.post("/api/v1/auth/login", json={"username": username, "password": password}).status_code == 200


async def _row(owner_id: int):
    async with get_database().session() as session:
        return await SettingRepository(session).get(f"integration.trakt.user.{owner_id}")


def test_start_requires_application(client: TestClient) -> None:
    response = client.post(_CONN)
    assert response.status_code == 409
    assert response.json()["message"] == "管理员尚未配置 Trakt"


async def test_device_code_stays_server_side(client: TestClient, monkeypatch, caplog) -> None:
    fake = FakeTrakt()
    _install(monkeypatch, fake)
    _save_app(client)
    caplog.set_level(logging.INFO)
    started = client.post(_CONN)
    assert started.status_code == 200
    assert started.json()["data"]["user_code"] == "ABCD1234"
    assert "device-secret" not in started.text
    row = await _row(0)
    assert row is not None
    stored = json.loads(row.value_json)
    assert SecretBox.is_encrypted(stored["device_code"])
    assert "device-secret" not in row.value_json

    pending = client.post(_POLL)
    assert pending.json()["data"]["status"] == "pending"
    again = client.post(_POLL)
    assert again.json()["data"]["status"] == "pending"
    assert fake.token_calls == 1

    connection = await TraktConnectionStore().get(0)
    connection.last_polled_at = "2000-01-01T00:00:00+00:00"
    await TraktConnectionStore().save(0, connection)
    done = client.post(_POLL)
    assert done.json()["data"]["status"] == "connected"
    assert done.json()["data"]["username"] == "ada"
    row = await _row(0)
    assert row is not None
    stored = json.loads(row.value_json)
    assert SecretBox.is_encrypted(stored["access_token"])
    assert stored["access_token"] != "access"
    text = caplog.text
    for secret in ("super-secret", "device-secret", "refresh"):
        assert secret not in text


def test_connected_user_must_disconnect_first(client: TestClient, monkeypatch) -> None:
    fake = FakeTrakt(interval=0)
    _install(monkeypatch, fake)
    _save_app(client)
    assert client.post(_CONN).status_code == 200
    client.post(_POLL)  # pending
    connected = client.post(_POLL)
    assert connected.json()["data"]["status"] == "connected"
    again = client.post(_CONN)
    assert again.status_code == 409
    assert again.json()["message"] == "请先断开现有连接"


async def test_expired_user_can_reconnect(client: TestClient, monkeypatch) -> None:
    fake = FakeTrakt(interval=0)
    _install(monkeypatch, fake)
    _save_app(client)
    client.post(_CONN)
    client.post(_POLL)
    assert client.post(_POLL).json()["data"]["status"] == "connected"
    connection = await TraktConnectionStore().get(0)
    connection.status = "expired"
    connection.access_token = ""
    connection.refresh_token = ""
    await TraktConnectionStore().save(0, connection)
    restarted = client.post(_CONN)
    assert restarted.status_code == 200
    assert restarted.json()["data"]["status"] == "pending"


def test_member_cannot_see_another_users_name(client: TestClient, monkeypatch) -> None:
    fake = FakeTrakt(interval=0)
    _install(monkeypatch, fake)
    _save_app(client)
    created = client.post("/api/v1/members", json={"username": "family", "password": "family-pass-1"})
    assert created.status_code == 200
    _login(client, "family", "family-pass-1")
    client.post(_CONN)
    client.post(_POLL)
    assert client.post(_POLL).json()["data"]["username"] == "ada"
    _login(client, "admin", "s3cret-pass")
    admin_view = client.get(_CONN).json()["data"]
    assert admin_view["username"] is None
    assert admin_view["status"] == "disconnected"


def test_credential_change_drops_member_connection(client: TestClient, monkeypatch) -> None:
    fake = FakeTrakt(interval=0)
    _install(monkeypatch, fake)
    _save_app(client)
    client.post("/api/v1/members", json={"username": "family", "password": "family-pass-1"})
    _login(client, "family", "family-pass-1")
    client.post(_CONN)
    client.post(_POLL)
    assert client.post(_POLL).json()["data"]["status"] == "connected"
    _login(client, "admin", "s3cret-pass")
    _save_app(client, secret="new-secret")
    _login(client, "family", "family-pass-1")
    assert client.get(_CONN).json()["data"]["status"] == "disconnected"

    client.post(_CONN)
    client.post(_POLL)
    assert client.post(_POLL).json()["data"]["status"] == "connected"
    _login(client, "admin", "s3cret-pass")
    _save_app(client, secret="")
    _login(client, "family", "family-pass-1")
    assert client.get(_CONN).json()["data"]["status"] == "connected"
    _login(client, "admin", "s3cret-pass")
    _save_app(client, client_id="other", secret="")
    _login(client, "family", "family-pass-1")
    assert client.get(_CONN).json()["data"]["status"] == "disconnected"


async def test_delete_member_removes_only_that_connection(client: TestClient, monkeypatch) -> None:
    fake = FakeTrakt(interval=0)
    _install(monkeypatch, fake)
    _save_app(client)
    client.post(_CONN)
    client.post(_POLL)
    assert client.post(_POLL).json()["data"]["status"] == "connected"
    created = client.post("/api/v1/members", json={"username": "family", "password": "family-pass-1"})
    member_id = created.json()["data"]["id"]
    _login(client, "family", "family-pass-1")
    client.post(_CONN)
    client.post(_POLL)
    assert client.post(_POLL).json()["data"]["status"] == "connected"
    _login(client, "admin", "s3cret-pass")
    assert client.delete(f"/api/v1/members/{member_id}").status_code == 200
    assert await _row(member_id) is None
    assert await _row(0) is not None


async def test_username_failure_still_connects(client: TestClient, monkeypatch) -> None:
    _install(monkeypatch, FakeTrakt(interval=0, username_error=True))
    _save_app(client)
    client.post(_CONN)
    client.post(_POLL)
    done = client.post(_POLL).json()["data"]
    assert done["status"] == "connected"
    assert done["username"] is None


async def test_expired_device_code(client: TestClient, monkeypatch) -> None:
    _install(monkeypatch, FakeTrakt())
    _save_app(client)
    client.post(_CONN)
    connection = await TraktConnectionStore().get(0)
    connection.device_expires_at = "2000-01-01T00:00:00+00:00"
    await TraktConnectionStore().save(0, connection)
    expired = client.post(_POLL)
    body = expired.json()["data"]
    assert body["status"] == "disconnected"
    assert body["message"] == "验证码已过期，请重新获取"
