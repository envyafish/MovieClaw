"""每个用户的 Trakt 连接：设备码、令牌和刷新。

应用凭证仍走 SettingStore。这里的 namespace 含 owner_id，不能预注册，
所以直接读写 app_setting，敏感字段用同一套 SecretBox。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import ConflictException
from movieclaw_api.schemas.base import BaseModel
from movieclaw_api.settings import TraktAppSetting, get_setting_store
from movieclaw_api.settings.base import SettingSchema
from movieclaw_db.crypto import SecretBox, get_secret_box
from movieclaw_db.repositories.setting_repo import SettingRepository
from movieclaw_media.trakt import TraktClient, TraktDeviceRejected, TraktError, TraktPending

logger = logging.getLogger("movieclaw_api.trakt")

_USER_PREFIX = "integration.trakt.user."
_SECRET_FIELDS = frozenset({"access_token", "refresh_token", "device_code"})
_EXPIRED_MESSAGE = "验证码已过期，请重新获取"
_DEVICE_MESSAGES = {
    404: "验证码无效，请重新获取",
    409: "这个验证码已经使用过，请重新获取",
    410: _EXPIRED_MESSAGE,
    418: "已在 Trakt 上拒绝授权",
}


class TraktConnection(SettingSchema):
    status: str = "disconnected"
    access_token: str = ""
    refresh_token: str = ""
    expires_at: str = ""
    username: str = ""
    device_code: str = ""
    user_code: str = ""
    verification_url: str = ""
    poll_interval_seconds: int = 5
    device_expires_at: str = ""
    last_polled_at: str = ""


class TraktConnectionView(BaseModel):
    app_configured: bool
    status: str
    username: str | None = None
    user_code: str | None = None
    verification_url: str | None = None
    expires_in: int | None = None
    message: str | None = None


class TraktConnectionStore:
    def _namespace(self, owner_id: int) -> str:
        return f"{_USER_PREFIX}{owner_id}"

    async def get(self, owner_id: int) -> TraktConnection:
        async with get_setting_store()._db().session() as session:
            row = await SettingRepository(session).get(self._namespace(owner_id))
        if row is None:
            return TraktConnection()
        data = json.loads(row.value_json)
        _decrypt(data)
        return TraktConnection.model_validate(data)

    async def save(self, owner_id: int, connection: TraktConnection) -> None:
        data = connection.model_dump(mode="json")
        _encrypt(data)
        async with get_setting_store()._db().session() as session:
            await SettingRepository(session).upsert(
                self._namespace(owner_id),
                json.dumps(data, ensure_ascii=False),
            )

    async def delete(self, owner_id: int, *, session: AsyncSession | None = None) -> None:
        namespace = self._namespace(owner_id)
        if session is None:
            async with get_setting_store()._db().session() as own:
                hit = await SettingRepository(own).delete(namespace)
        else:
            # 调用方的事务还开着（删除成员）。另开连接会把 SQLite 锁住，
            # 所以只在这个 session 上标记删除，提交留给调用方。
            row = await SettingRepository(session).get(namespace)
            hit = row is not None
            if row is not None:
                await session.delete(row)
        if hit:
            logger.info("已删除 Trakt 连接：%s", namespace)

    async def delete_all(self) -> None:
        async with get_setting_store()._db().session() as session:
            repo = SettingRepository(session)
            namespaces = [
                row.namespace
                for row in await repo.list_all()
                if row.namespace.startswith(_USER_PREFIX)
            ]
        for namespace in namespaces:
            owner_id = int(namespace.removeprefix(_USER_PREFIX))
            await self.delete(owner_id)


async def delete_trakt_connections() -> None:
    await TraktConnectionStore().delete_all()


async def get_trakt_client() -> TraktClient:
    from movieclaw_api.services.media_discover import open_trakt_client

    return await open_trakt_client()


async def show_connection(owner_id: int) -> TraktConnectionView:
    app = await get_setting_store().get(TraktAppSetting)
    connection = await TraktConnectionStore().get(owner_id)
    return _view(app, connection)


async def start_connection(owner_id: int) -> TraktConnectionView:
    app = await get_setting_store().get(TraktAppSetting)
    if not app.client_id:
        raise ConflictException("管理员尚未配置 Trakt")
    store = TraktConnectionStore()
    connection = await store.get(owner_id)
    if connection.status == "connected":
        raise ConflictException("请先断开现有连接")
    granted = await (await get_trakt_client()).device_code()
    now = _now()
    connection.status = "pending"
    connection.device_code = granted.device_code
    connection.user_code = granted.user_code
    connection.verification_url = granted.verification_url
    connection.poll_interval_seconds = granted.interval
    connection.device_expires_at = _iso(now + timedelta(seconds=granted.expires_in))
    connection.last_polled_at = ""
    connection.access_token = ""
    connection.refresh_token = ""
    connection.expires_at = ""
    await store.save(owner_id, connection)
    return _view(app, connection)


async def poll_connection(owner_id: int) -> TraktConnectionView:
    app = await get_setting_store().get(TraktAppSetting)
    store = TraktConnectionStore()
    connection = await store.get(owner_id)
    if connection.status != "pending":
        return _view(app, connection)
    now = _now()
    deadline = _parse(connection.device_expires_at)
    if deadline is not None and now >= deadline:
        _clear_device(connection)
        connection.status = "disconnected"
        await store.save(owner_id, connection)
        return _view(app, connection, message=_EXPIRED_MESSAGE)
    last = _parse(connection.last_polled_at)
    if last is not None and (now - last).total_seconds() < connection.poll_interval_seconds:
        return _view(app, connection)
    client = await get_trakt_client()
    try:
        bundle = await client.device_token(connection.device_code)
    except TraktPending as exc:
        connection.last_polled_at = _iso(now)
        if exc.slow:
            connection.poll_interval_seconds = min(connection.poll_interval_seconds + 5, 60)
        await store.save(owner_id, connection)
        return _view(app, connection)
    except TraktDeviceRejected as exc:
        _clear_device(connection)
        connection.status = "disconnected"
        await store.save(owner_id, connection)
        return _view(app, connection, message=_DEVICE_MESSAGES[exc.status])
    username = ""
    try:
        username = await client.username(bundle.access_token)
    except TraktError:
        username = ""
    connection.status = "connected"
    connection.access_token = bundle.access_token
    connection.refresh_token = bundle.refresh_token
    connection.expires_at = _iso(now + timedelta(seconds=bundle.expires_in))
    connection.username = username
    _clear_device(connection)
    await store.save(owner_id, connection)
    return _view(app, connection)


async def disconnect(owner_id: int) -> TraktConnectionView:
    await TraktConnectionStore().delete(owner_id)
    app = await get_setting_store().get(TraktAppSetting)
    return _view(app, TraktConnection())


async def ensure_fresh_token(owner_id: int, client: TraktClient) -> str | None:
    """返回还能用的访问令牌。没连接，或刷新失败，返回 None。"""
    store = TraktConnectionStore()
    connection = await store.get(owner_id)
    if connection.status != "connected" or not connection.access_token:
        return None
    expires = _parse(connection.expires_at)
    if expires is not None and expires - _now() <= timedelta(minutes=5):
        try:
            bundle = await client.refresh(connection.refresh_token)
        except TraktError:
            connection.status = "expired"
            connection.access_token = ""
            connection.refresh_token = ""
            connection.expires_at = ""
            await store.save(owner_id, connection)
            return None
        connection.access_token = bundle.access_token
        if bundle.refresh_token:
            connection.refresh_token = bundle.refresh_token
        connection.expires_at = _iso(_now() + timedelta(seconds=bundle.expires_in))
        await store.save(owner_id, connection)
    return connection.access_token or None


def _view(
    app: TraktAppSetting,
    connection: TraktConnection,
    *,
    message: str | None = None,
) -> TraktConnectionView:
    pending = connection.status == "pending"
    expires_in = None
    if pending:
        deadline = _parse(connection.device_expires_at)
        if deadline is not None:
            expires_in = max(0, int((deadline - _now()).total_seconds()))
    return TraktConnectionView(
        app_configured=bool(app.client_id),
        status=connection.status,
        username=connection.username or None,
        user_code=connection.user_code or None if pending else None,
        verification_url=connection.verification_url or None if pending else None,
        expires_in=expires_in,
        message=message,
    )


def _clear_device(connection: TraktConnection) -> None:
    connection.device_code = ""
    connection.user_code = ""
    connection.verification_url = ""
    connection.device_expires_at = ""
    connection.last_polled_at = ""


def _encrypt(data: dict) -> None:
    box = get_secret_box()
    for name in _SECRET_FIELDS:
        value = data.get(name)
        if isinstance(value, str) and value and not SecretBox.is_encrypted(value):
            data[name] = box.encrypt(value)


def _decrypt(data: dict) -> None:
    box = get_secret_box()
    for name in _SECRET_FIELDS:
        value = data.get(name)
        if isinstance(value, str) and value:
            data[name] = box.decrypt(value)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
