"""Trakt HTTP 与响应映射。不读配置，也不碰数据库。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from movieclaw_media.models import MediaKind

_DEFAULT_BASE_URL = "https://api.trakt.tv"


class TraktError(Exception):
    """Trakt 返回了失败，或响应无法使用。消息不包含响应正文。"""


class TraktAuthError(TraktError):
    """Trakt 返回 401。"""


class TraktNetworkError(TraktError):
    """连接 Trakt 失败。"""


class TraktPending(TraktError):
    """设备码还没被用户授权。"""


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_in: int


class TraktClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
        base_url: str | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.AsyncClient(
            base_url=base_url or os.environ.get("TRAKT_API_BASE_URL", _DEFAULT_BASE_URL),
            transport=transport,
            headers={
                "trakt-api-key": client_id,
                "trakt-api-version": "2",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_json(
        self,
        path: str,
        *,
        token: str | None = None,
        params: dict | None = None,
    ) -> tuple[object, dict[str, str]]:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        try:
            response = await self._http.get(path, params=params, headers=headers)
        except httpx.TransportError as exc:
            raise TraktNetworkError("无法连接 Trakt") from exc
        return _decode(response)

    async def device_code(self) -> DeviceCode:
        body, _ = await self._post("/oauth/device/code", {"client_id": self._client_id})
        data = _as_dict(body)
        return DeviceCode(
            device_code=str(data.get("device_code") or ""),
            user_code=str(data.get("user_code") or ""),
            verification_url=str(data.get("verification_url") or "https://trakt.tv/activate"),
            expires_in=int(data.get("expires_in") or 600),
            interval=int(data.get("interval") or 5),
        )

    async def device_token(self, code: str) -> TokenBundle:
        response = await self._send(
            "/oauth/device/token",
            {"code": code, "client_id": self._client_id, "client_secret": self._client_secret},
        )
        pending = _error_code(response) in {"authorization_pending", "slow_down"}
        if response.status_code == 400 and pending:
            raise TraktPending("等待用户在 Trakt 上授权")
        body, _ = _decode(response)
        return _token(body)

    async def refresh(self, refresh_token: str) -> TokenBundle:
        body, _ = await self._post(
            "/oauth/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        return _token(body)

    async def username(self, access_token: str) -> str:
        body, _ = await self.get_json("/users/settings", token=access_token)
        user = _as_dict(body).get("user")
        if not isinstance(user, dict):
            return ""
        return str(user.get("username") or "")

    async def _post(self, path: str, payload: dict) -> tuple[object, dict[str, str]]:
        return _decode(await self._send(path, payload))

    async def _send(self, path: str, payload: dict) -> httpx.Response:
        try:
            return await self._http.post(path, json=payload)
        except httpx.TransportError as exc:
            raise TraktNetworkError("无法连接 Trakt") from exc


def _decode(response: httpx.Response) -> tuple[object, dict[str, str]]:
    if response.status_code == 401:
        raise TraktAuthError("Trakt 拒绝了这个请求")
    if response.status_code >= 400:
        raise TraktError(f"Trakt 返回 {response.status_code}")
    return response.json(), {key.lower(): value for key, value in response.headers.items()}


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        return str(body.get("error") or "")
    return ""


def _as_dict(body: object) -> dict:
    return body if isinstance(body, dict) else {}


def _token(body: object) -> TokenBundle:
    data = _as_dict(body)
    return TokenBundle(
        access_token=str(data.get("access_token") or ""),
        refresh_token=str(data.get("refresh_token") or ""),
        expires_in=int(data.get("expires_in") or 0),
    )


@dataclass(frozen=True)
class TraktItem:
    trakt_id: int
    tmdb_id: int | None
    kind: MediaKind
    title: str
    year: int | None
    note: str


def calendar_note(when: date, *, episode: tuple[int, int] | None) -> str:
    text = f"{when.month}月{when.day}日"
    if episode is None:
        return f"{text} 上映"
    season, number = episode
    return f"{text} · S{season:02d}E{number:02d}"


def parse_calendar(payload: list, kind: MediaKind, *, tz: datetime.tzinfo) -> list[TraktItem]:
    items: list[TraktItem] = []
    for row in payload:
        if kind is MediaKind.MOVIE:
            released = row.get("released")
            entity = row.get("movie") or {}
            if not released:
                continue
            when = date.fromisoformat(released)
            episode = None
        else:
            aired = row.get("first_aired")
            entity = row.get("show") or {}
            episode_row = row.get("episode") or {}
            if not aired:
                continue
            parsed = datetime.fromisoformat(aired.replace("Z", "+00:00")).astimezone(tz)
            when = parsed.date()
            episode = (int(episode_row.get("season") or 0), int(episode_row.get("number") or 0))
        item = _item(entity, kind, note=calendar_note(when, episode=episode))
        if item is not None:
            items.append(item)
    return items


def parse_listing(payload: list, kind: MediaKind) -> list[TraktItem]:
    key = "movie" if kind is MediaKind.MOVIE else "show"
    items: list[TraktItem] = []
    for row in payload:
        nested = row.get(key)
        entity = nested if isinstance(nested, dict) else row
        item = _item(entity, kind, note="")
        if item is not None:
            items.append(item)
    return items


def _item(entity: dict, kind: MediaKind, *, note: str) -> TraktItem | None:
    ids = entity.get("ids") or {}
    trakt_id = ids.get("trakt")
    if not trakt_id or not entity.get("title"):
        return None
    tmdb = ids.get("tmdb")
    year = entity.get("year")
    return TraktItem(
        trakt_id=int(trakt_id),
        tmdb_id=int(tmdb) if tmdb else None,
        kind=kind,
        title=str(entity["title"]),
        year=int(year) if year else None,
        note=note,
    )
