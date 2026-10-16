"""Trakt HTTP 与响应映射。不读配置，也不碰数据库。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit

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
    poster_url: str = ""


def playback_note(progress: object, episode: tuple[int, int] | None) -> str:
    """未看完进度。剧集带上季集，方便同一部剧的多条进度分开。"""
    percent = ""
    try:
        value = max(0, min(99, int(float(progress) + 0.5)))
    except (TypeError, ValueError):
        value = None
    if value is not None:
        percent = f"看到 {value}%"
    if episode is None:
        return percent
    season, number = episode
    label = f"S{season:02d}E{number:02d}"
    return f"{label} · {percent}" if percent else label


def parse_playback(payload: list, kind: MediaKind) -> list[TraktItem]:
    """GET /sync/playback 里未看完的电影或剧集，保持 Trakt 的最近暂停顺序。"""
    wanted = "movie" if kind is MediaKind.MOVIE else "episode"
    items: list[TraktItem] = []
    for row in payload:
        if not isinstance(row, dict) or row.get("type") != wanted:
            continue
        if kind is MediaKind.MOVIE:
            entity = row.get("movie") or {}
            episode = None
        else:
            entity = row.get("show") or {}
            episode_row = row.get("episode") or {}
            episode = (int(episode_row.get("season") or 0), int(episode_row.get("number") or 0))
        item = _item(entity, kind, note=playback_note(row.get("progress"), episode))
        if item is not None:
            items.append(item)
    return items


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


@dataclass(frozen=True)
class TraktCalendarEpisode:
    """追剧日历里的一集。横图优先用剧集截图，没有再退到剧的横版剧照。"""

    trakt_id: int
    tmdb_id: int | None
    show_title: str
    episode_title: str
    season: int
    number: int
    aired: datetime
    landscape_url: str
    network: str
    overview: str
    runtime: int | None


def parse_show_calendar(payload: object, *, tz: datetime.tzinfo) -> list[TraktCalendarEpisode]:
    """把 /calendars/my/shows 按播出顺序展开。没有播出时间或剧名的行丢掉。"""
    if not isinstance(payload, list):
        return []
    episodes: list[TraktCalendarEpisode] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        show = row.get("show") if isinstance(row.get("show"), dict) else {}
        episode = row.get("episode") if isinstance(row.get("episode"), dict) else {}
        aired = _aired_local(row.get("first_aired"), tz)
        ids = show.get("ids") if isinstance(show.get("ids"), dict) else {}
        trakt_id = ids.get("trakt")
        title = str(show.get("title") or "").strip()
        if aired is None or not trakt_id or not title:
            continue
        season = _as_int(episode.get("season"))
        number = _as_int(episode.get("number"))
        episode_title = str(episode.get("title") or "").strip()
        if not episode_title and number:
            episode_title = f"第{number}集"
        tmdb = ids.get("tmdb")
        runtime = _as_int(episode.get("runtime"))
        episodes.append(
            TraktCalendarEpisode(
                trakt_id=int(trakt_id),
                tmdb_id=int(tmdb) if tmdb else None,
                show_title=title,
                episode_title=episode_title,
                season=season,
                number=number,
                aired=aired,
                landscape_url=_landscape_url(episode, show),
                network=str(show.get("network") or "").strip(),
                overview=str(episode.get("overview") or "").strip(),
                runtime=runtime or None,
            )
        )
    return episodes


def _aired_local(value: object, tz: datetime.tzinfo) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def _landscape_url(episode: dict, show: dict) -> str:
    return (
        _image_url(episode.get("images") or {}, "screenshot")
        or _image_url(show.get("images") or {}, "fanart")
        or _image_url(show.get("images") or {}, "thumb")
    )


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
        poster_url=_poster_url(entity),
    )


def _image_url(images: object, kind: str) -> str:
    posters = images.get(kind) if isinstance(images, dict) else None
    if not isinstance(posters, list) or not posters or not isinstance(posters[0], str):
        return ""
    path = posters[0].strip()
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    return f"https://{path}"


def _poster_url(entity: dict) -> str:
    return _image_url(entity.get("images") or {}, "poster")


def _preferred_field(payload: object, field: str) -> str:
    """从 translations/zh 里按简体优先取一个字段。没有简体时用第一条。"""
    if not isinstance(payload, list):
        return ""
    ranked = {"cn": 0, "sg": 1, "hk": 2, "tw": 3}
    chosen = ""
    chosen_rank = len(ranked)
    for row in payload:
        if not isinstance(row, dict):
            continue
        text = str(row.get(field) or "").strip()
        if not text:
            continue
        rank = ranked.get(str(row.get("country") or "").lower(), len(ranked))
        if not chosen or rank < chosen_rank:
            chosen = text
            chosen_rank = rank
    return chosen


def chinese_title(payload: object) -> str:
    """从 Trakt 的 translations/zh 里取简体标题。没有简体时用第一条译名。"""
    return _preferred_field(payload, "title")


_GENRES = {
    "action": "动作",
    "adventure": "冒险",
    "animation": "动画",
    "anime": "动漫",
    "comedy": "喜剧",
    "crime": "犯罪",
    "documentary": "纪录片",
    "drama": "剧情",
    "family": "家庭",
    "fantasy": "奇幻",
    "history": "历史",
    "holiday": "节日",
    "horror": "恐怖",
    "music": "音乐",
    "musical": "歌舞",
    "mystery": "悬疑",
    "romance": "爱情",
    "science-fiction": "科幻",
    "short": "短片",
    "sport": "运动",
    "superhero": "超级英雄",
    "suspense": "悬疑",
    "thriller": "惊悚",
    "war": "战争",
    "western": "西部",
    "children": "儿童",
    "reality": "真人秀",
    "talk-show": "脱口秀",
    "game-show": "游戏节目",
    "news": "新闻",
    "soap": "肥皂剧",
}
_COUNTRIES = {
    "us": "美国",
    "cn": "中国",
    "jp": "日本",
    "kr": "韩国",
    "gb": "英国",
    "uk": "英国",
    "fr": "法国",
    "de": "德国",
    "hk": "中国香港",
    "tw": "中国台湾",
    "ca": "加拿大",
    "au": "澳大利亚",
    "in": "印度",
    "es": "西班牙",
    "it": "意大利",
    "ru": "俄罗斯",
}
_LANGUAGES = {
    "en": "英语",
    "zh": "汉语",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "it": "意大利语",
    "ru": "俄语",
    "pt": "葡萄牙语",
    "hi": "印地语",
    "th": "泰语",
}


def _place_name(code: object, names: dict[str, str]) -> str:
    text = str(code or "").strip()
    if not text:
        return ""
    return names.get(text.lower(), text.upper() if len(text) <= 3 else text)


def youtube_id(url: object) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/")[0]
    if "youtube.com" not in host:
        return ""
    query = parse_qs(parsed.query).get("v", [""])[0]
    if query:
        return query
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"embed", "shorts", "v"}:
        return parts[1]
    return ""


@dataclass(frozen=True)
class TraktCredit:
    name: str
    role: str = ""
    avatar_url: str = ""


@dataclass(frozen=True)
class TraktVideo:
    key: str
    name: str
    kind: str


@dataclass(frozen=True)
class TraktDetail:
    trakt_id: int
    kind: MediaKind
    title: str
    original_title: str
    year: int | None
    overview: str
    rating: float
    genres: tuple[str, ...]
    extent: str
    released: str
    country: str
    language: str
    network: str
    poster_url: str
    backdrop_url: str
    slug: str
    directors: tuple[TraktCredit, ...]
    cast: tuple[TraktCredit, ...]
    tmdb_id: int | None = None
    videos: tuple[TraktVideo, ...] = ()


# Trakt /videos 的 type。顺序与详情页一致：预告和片头在前，花絮和幕后在后。
_VIDEO_TYPES = {
    "trailer": (0, "预告片"),
    "opening credits": (1, "片头"),
    "teaser": (2, "先导预告"),
    "clip": (3, "片段"),
    "featurette": (4, "花絮"),
    "behind the scenes": (5, "幕后"),
}
# 先导、片段、花絮、幕后经常几十上百条，分开限额才不会把预告和片头挤掉。
_VIDEO_CAP = {
    "预告片": 12,
    "片头": 8,
    "先导预告": 4,
    "片段": 4,
    "花絮": 4,
    "幕后": 4,
}


def parse_videos(payload: object) -> tuple[TraktVideo, ...]:
    """从 Trakt 的视频列表取出能内嵌的 YouTube 视频。"""
    if not isinstance(payload, list):
        return ()
    buckets: dict[int, list[tuple[bool, str, str, str, str]]] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        kind = _VIDEO_TYPES.get(str(row.get("type") or "").strip().lower())
        key = youtube_id(row.get("url"))
        if kind is None or not key:
            continue
        name = str(row.get("title") or "").strip() or kind[1]
        published = str(row.get("published_at") or "")
        buckets.setdefault(kind[0], []).append(
            (not bool(row.get("official")), published, key, name, kind[1])
        )
    seen: set[str] = set()
    videos: list[TraktVideo] = []
    for order in sorted(buckets):
        rows = sorted(buckets[order], key=lambda item: item[1], reverse=True)
        rows.sort(key=lambda item: item[0])
        kept = 0
        for _unofficial, _published, key, name, label in rows:
            if key in seen:
                continue
            seen.add(key)
            videos.append(TraktVideo(key=key, name=name, kind=label))
            kept += 1
            if kept == _VIDEO_CAP[label]:
                break
    return tuple(videos)


def parse_detail(
    entity: object,
    kind: MediaKind,
    *,
    translation: object = None,
    people: object = None,
    videos: object = None,
) -> TraktDetail | None:
    """把一部电影或剧集的 Trakt 摘要整理成详情页要用的字段。"""
    if not isinstance(entity, dict):
        return None
    ids = entity.get("ids") or {}
    trakt_id = ids.get("trakt")
    original = str(entity.get("title") or "").strip()
    if not trakt_id or not original:
        return None
    translated = _preferred_field(translation, "title")
    english = str(entity.get("overview") or "").strip()
    overview = _preferred_field(translation, "overview") or english
    year = entity.get("year")
    runtime = entity.get("runtime")
    episodes = entity.get("aired_episodes")
    if kind is MediaKind.MOVIE and runtime:
        extent = f"{int(runtime)} 分钟"
    elif episodes:
        extent = f"{int(episodes)} 集"
    else:
        extent = ""
    released = str(entity.get("released") or entity.get("first_aired") or "")
    if "T" in released:
        released = released.split("T", 1)[0]
    genres = tuple(
        label
        for genre in entity.get("genres") or []
        if (label := _GENRES.get(str(genre), ""))
    )
    directors, cast = _credits(people)
    images = entity.get("images") or {}
    tmdb = ids.get("tmdb")
    return TraktDetail(
        trakt_id=int(trakt_id),
        kind=kind,
        title=translated or original,
        original_title=original,
        year=int(year) if year else None,
        overview=overview,
        rating=float(entity.get("rating") or 0),
        genres=genres,
        extent=extent,
        released=released,
        country=_place_name(entity.get("country"), _COUNTRIES),
        language=_place_name(entity.get("language"), _LANGUAGES),
        network=str(entity.get("network") or ""),
        poster_url=_image_url(images, "poster"),
        backdrop_url=_image_url(images, "fanart"),
        slug=str(ids.get("slug") or trakt_id),
        directors=directors,
        cast=cast,
        tmdb_id=int(tmdb) if tmdb else None,
        videos=parse_videos(videos),
    )


def _credits(payload: object) -> tuple[tuple[TraktCredit, ...], tuple[TraktCredit, ...]]:
    data = payload if isinstance(payload, dict) else {}
    directors: list[TraktCredit] = []
    for row in (data.get("crew") or {}).get("directing") or []:
        if not isinstance(row, dict) or str(row.get("job") or "") not in {"Director", "Creator"}:
            continue
        credit = _credit(row.get("person"))
        if credit is not None:
            directors.append(credit)
    cast: list[TraktCredit] = []
    for row in data.get("cast") or []:
        if not isinstance(row, dict):
            continue
        credit = _credit(row.get("person"), role=str(row.get("character") or "").strip())
        if credit is not None:
            cast.append(credit)
        if len(cast) == 16:
            break
    return tuple(directors), tuple(cast)


def _credit(person: object, *, role: str = "") -> TraktCredit | None:
    if not isinstance(person, dict):
        return None
    name = str(person.get("name") or "").strip()
    if not name:
        return None
    return TraktCredit(
        name=name,
        role=role,
        avatar_url=_image_url(person.get("images") or {}, "headshot"),
    )
