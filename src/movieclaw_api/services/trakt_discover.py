"""把 Trakt 片单编进现有的发现页协议。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from movieclaw_api.exceptions import TraktAppUnauthorizedException
from movieclaw_api.schemas.discover import (
    DiscoveredTitleDetailsView,
    DiscoveredTitleMetadata,
    DiscoveredTitleView,
    DiscoveryCollectionNotice,
    DiscoveryCollectionTitlesView,
    DiscoveryCollectionView,
    TraktCalendarDayView,
    TraktCalendarEpisodeView,
    TraktShowCalendarView,
)
from movieclaw_api.services.discover_library import DiscoverLibraryProjectionService
from movieclaw_api.services.title_discovery import make_collection_ref, make_title_ref
from movieclaw_cache.memory import AsyncTTLCache
from movieclaw_media.models import MediaCastMember, MediaImage, MediaKind, MediaSource, MediaVideo
from movieclaw_media.trakt import (
    TraktAuthError,
    TraktCalendarEpisode,
    TraktDetail,
    TraktError,
    TraktItem,
    TraktNetworkError,
    chinese_title,
    parse_calendar,
    parse_detail,
    parse_listing,
    parse_playback,
    parse_show_calendar,
)

_WEEKDAYS = "一二三四五六日"

_APP_UNAUTHORIZED = "Trakt 应用凭证无效，请管理员检查 Client ID 和 Client Secret"
_PUBLIC_TTL = 15 * 60
_PERSONAL_TTL = 10 * 60
_ENRICH_TTL = 24 * 60 * 60
_NOT_CONNECTED = DiscoveryCollectionNotice(code="trakt_not_connected", message="连接 Trakt 后查看")

# id, title, preview limit, full listing, needs a user token
_MOVIE: tuple[tuple[str, str, int, bool, bool], ...] = (
    ("playback", "继续观看", 20, False, True),
    ("calendar", "上映日历", 20, False, True),
    ("recommendations", "为你推荐", 20, False, True),
    ("trending", "热门", 20, True, False),
    ("popular", "流行", 20, True, False),
    ("anticipated", "最受期待", 20, True, False),
    ("boxoffice", "票房", 10, False, False),
)
_TV: tuple[tuple[str, str, int, bool, bool], ...] = (
    ("playback", "继续观看", 20, False, True),
    ("calendar", "追剧日历", 20, False, True),
    ("recommendations", "为你推荐", 20, False, True),
    ("trending", "热门", 20, True, False),
    ("popular", "流行", 20, True, False),
    ("anticipated", "最受期待", 20, True, False),
)

_cache = AsyncTTLCache()
_enrich_cache = AsyncTTLCache()


async def require_trakt_app() -> None:
    from movieclaw_api.exceptions import UpstreamServiceException
    from movieclaw_api.settings import TraktAppSetting, get_setting_store

    setting = await get_setting_store().get(TraktAppSetting)
    if not setting.client_id:
        raise UpstreamServiceException("管理员尚未配置 Trakt。请在设置 → Trakt 填写应用凭证。")


def trakt_collections(media_type: MediaKind) -> list[DiscoveryCollectionView]:
    rows = _MOVIE if media_type is MediaKind.MOVIE else _TV
    return [_descriptor(media_type, row) for row in rows]


class TraktDiscoverService:
    async def browse(
        self,
        collection_id: str,
        media_type: MediaKind,
        *,
        owner_id: int,
        limit: int,
        page: int,
        projection: DiscoverLibraryProjectionService | None,
    ) -> DiscoveryCollectionTitlesView:
        row = _spec(media_type, collection_id)
        if row is None:
            raise LookupError("该片单不存在或当前来源未开放浏览")
        descriptor = _descriptor(media_type, row)
        _collection_id, _title, _preview, _full, personal = row
        if personal:
            return await self._personal(
                descriptor,
                collection_id,
                media_type,
                owner_id=owner_id,
                limit=limit,
                projection=projection,
            )
        page_no = 1 if collection_id == "boxoffice" else page
        payload, total_pages = await _cache.get_or_set(
            f"trakt:{descriptor.collection_ref}:page:{page_no}",
            _PUBLIC_TTL,
            lambda: self._public_page(collection_id, media_type, page=page_no, limit=limit),
        )
        titles = await self._to_titles(parse_listing(payload, media_type), projection)
        return DiscoveryCollectionTitlesView(
            collection=descriptor,
            titles=titles,
            returned_count=len(titles),
            truncated=False,
            page=page_no,
            total_pages=1 if collection_id == "boxoffice" else total_pages,
            total_results=len(titles),
            has_more=collection_id != "boxoffice" and page_no < total_pages,
        )

    async def _personal(
        self,
        descriptor: DiscoveryCollectionView,
        collection_id: str,
        media_type: MediaKind,
        *,
        owner_id: int,
        limit: int,
        projection: DiscoverLibraryProjectionService | None,
    ) -> DiscoveryCollectionTitlesView:
        from movieclaw_api.services.trakt_connection import ensure_fresh_token

        client = await _client()
        token = await ensure_fresh_token(owner_id, client)
        if token is None:
            return _empty(descriptor, _NOT_CONNECTED)

        async def load() -> list:
            return await self._fetch_personal(collection_id, media_type, owner_id, token)

        try:
            payload = await _cache.get_or_set(
                f"trakt:{descriptor.collection_ref}:user:{owner_id}",
                _PERSONAL_TTL,
                load,
            )
        except TraktAuthError:
            await _expire(owner_id)
            return _empty(descriptor, _NOT_CONNECTED)
        if collection_id == "calendar":
            items = parse_calendar(
                payload,
                media_type,
                tz=datetime.now().astimezone().tzinfo or UTC,
            )
        elif collection_id == "playback":
            items = parse_playback(payload, media_type)
        else:
            items = parse_listing(payload, media_type)
        notice = _empty_notice(collection_id) if not items else None
        titles = await self._to_titles(items[:limit], projection)
        return DiscoveryCollectionTitlesView(
            collection=descriptor,
            titles=titles,
            returned_count=len(titles),
            truncated=len(items) > limit,
            page=1,
            total_pages=1,
            total_results=len(items),
            has_more=False,
            notice=notice,
        )

    async def _fetch_personal(
        self,
        collection_id: str,
        media_type: MediaKind,
        owner_id: int,
        token: str,
    ) -> list:
        client = await _client()
        path, params = _personal_request(collection_id, media_type)
        params = _with_images(params)
        try:
            body, _headers = await client.get_json(path, token=token, params=params)
        except TraktAuthError:
            from movieclaw_api.services.trakt_connection import ensure_fresh_token

            if await _force_due(owner_id) is None:
                raise
            refreshed = await ensure_fresh_token(owner_id, client)
            if not refreshed:
                raise
            body, _headers = await client.get_json(path, token=refreshed, params=params)
        return body if isinstance(body, list) else []

    async def _public_page(
        self,
        collection_id: str,
        media_type: MediaKind,
        *,
        page: int,
        limit: int,
    ) -> tuple[list, int]:
        client = await _client()
        params = None if collection_id == "boxoffice" else {"page": page, "limit": limit}
        try:
            body, headers = await client.get_json(
                _public_path(collection_id, media_type),
                params=_with_images(params),
            )
        except TraktAuthError as exc:
            raise TraktAppUnauthorizedException(_APP_UNAUTHORIZED) from exc
        payload = body if isinstance(body, list) else []
        return payload, _page_count(headers)

    async def details(self, media_type: MediaKind, trakt_id: int) -> DiscoveredTitleDetailsView:
        detail = await _enrich_cache.get_or_set(
            f"trakt:detail:{media_type.value}:{trakt_id}",
            _ENRICH_TTL,
            lambda: _load_detail(media_type, trakt_id),
        )
        return _detail_view(detail)

    async def show_calendar(self, owner_id: int) -> TraktShowCalendarView:
        """追剧日历完整页：同一 30 天窗口，不截成发现行的预览条数。"""
        from movieclaw_api.services.trakt_connection import ensure_fresh_token

        client = await _client()
        token = await ensure_fresh_token(owner_id, client)
        if token is None:
            return TraktShowCalendarView(days=[], notice=_NOT_CONNECTED)

        ref = make_collection_ref(MediaSource.TRAKT, MediaKind.TV, "calendar")

        async def load() -> list:
            return await self._fetch_personal("calendar", MediaKind.TV, owner_id, token)

        try:
            payload = await _cache.get_or_set(
                f"trakt:{ref}:user:{owner_id}",
                _PERSONAL_TTL,
                load,
            )
        except TraktAuthError:
            await _expire(owner_id)
            return TraktShowCalendarView(days=[], notice=_NOT_CONNECTED)
        tz = datetime.now().astimezone().tzinfo or UTC
        episodes = parse_show_calendar(payload, tz=tz)
        if not episodes:
            return TraktShowCalendarView(days=[], notice=_empty_notice("calendar"))
        translated = await _chinese_titles(_title_stubs(episodes))
        return TraktShowCalendarView(days=_calendar_days(episodes, translated))

    async def _to_titles(
        self,
        items: list[TraktItem],
        _projection: DiscoverLibraryProjectionService | None,
    ) -> list[DiscoveredTitleView]:
        translated = await _chinese_titles(items)
        return [
            DiscoveredTitleView(
                title_ref=make_title_ref(MediaSource.TRAKT, str(item.trakt_id), item.kind),
                provider=MediaSource.TRAKT,
                external_id=str(item.trakt_id),
                media_type=item.kind,
                title=translated.get((item.kind.value, item.trakt_id)) or item.title,
                original_title=item.title,
                release_year=item.year,
                poster_url=item.poster_url,
                note=item.note,
                navigable=True,
                tmdb_id=item.tmdb_id,
            )
            for item in items
        ]


def _descriptor(
    media_type: MediaKind,
    row: tuple[str, str, int, bool, bool],
) -> DiscoveryCollectionView:
    collection_id, title, limit, full, _personal = row
    return DiscoveryCollectionView(
        collection_ref=make_collection_ref(MediaSource.TRAKT, media_type, collection_id),
        provider=MediaSource.TRAKT,
        media_type=media_type,
        name=title,
        default_limit=limit,
        supports_full_listing=full,
    )


def _spec(media_type: MediaKind, collection_id: str):
    rows = _MOVIE if media_type is MediaKind.MOVIE else _TV
    return next((row for row in rows if row[0] == collection_id), None)


def _group(media_type: MediaKind) -> str:
    return "movies" if media_type is MediaKind.MOVIE else "shows"


def _public_path(collection_id: str, media_type: MediaKind) -> str:
    if collection_id == "boxoffice":
        return "/movies/boxoffice"
    return f"/{_group(media_type)}/{collection_id}"


def _with_images(params: dict | None) -> dict:
    merged = {"extended": "full"}
    if params:
        merged.update(params)
    return merged


def _personal_request(collection_id: str, media_type: MediaKind) -> tuple[str, dict | None]:
    group = _group(media_type)
    if collection_id == "calendar":
        today = datetime.now().astimezone().date().isoformat()
        return f"/calendars/my/{group}/{today}/30", None
    if collection_id == "playback":
        kind = "movies" if media_type is MediaKind.MOVIE else "episodes"
        return f"/sync/playback/{kind}", None
    return f"/recommendations/{group}", {"limit": 20}


def _page_count(headers: dict) -> int:
    raw = headers.get("x-pagination-page-count") or "1"
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _title_stubs(episodes: list[TraktCalendarEpisode]) -> list[TraktItem]:
    seen: dict[int, TraktCalendarEpisode] = {}
    for episode in episodes:
        seen.setdefault(episode.trakt_id, episode)
    return [
        TraktItem(
            trakt_id=episode.trakt_id,
            tmdb_id=episode.tmdb_id,
            kind=MediaKind.TV,
            title=episode.show_title,
            year=None,
            note="",
        )
        for episode in seen.values()
    ]


def _calendar_days(
    episodes: list[TraktCalendarEpisode],
    translated: dict[tuple[str, int], str],
) -> list[TraktCalendarDayView]:
    grouped: dict[str, list[TraktCalendarEpisodeView]] = {}
    order: list[str] = []
    labels: dict[str, str] = {}
    for episode in episodes:
        day = episode.aired.date()
        key = day.isoformat()
        if key not in grouped:
            order.append(key)
            grouped[key] = []
            labels[key] = f"{day.month}月{day.day}日 周{_WEEKDAYS[day.weekday()]}"
        code = f"S{episode.season:02d}E{episode.number:02d}"
        grouped[key].append(
            TraktCalendarEpisodeView(
                title_ref=make_title_ref(MediaSource.TRAKT, str(episode.trakt_id), MediaKind.TV),
                show_title=translated.get((MediaKind.TV.value, episode.trakt_id))
                or episode.show_title,
                episode_title=episode.episode_title,
                season=episode.season,
                number=episode.number,
                code=code,
                aired_time=episode.aired.strftime("%H:%M"),
                landscape_url=episode.landscape_url,
                network=episode.network,
                overview=episode.overview,
                runtime=episode.runtime,
            )
        )
    return [
        TraktCalendarDayView(date=key, label=labels[key], episodes=grouped[key]) for key in order
    ]


def _empty_notice(collection_id: str) -> DiscoveryCollectionNotice:
    if collection_id == "playback":
        return DiscoveryCollectionNotice(code="trakt_playback_empty", message="没有未看完的内容")
    if collection_id == "calendar":
        return DiscoveryCollectionNotice(code="trakt_calendar_empty", message="未来 30 天没有安排")
    return DiscoveryCollectionNotice(
        code="trakt_recommendations_empty",
        message="Trakt 还没有足够的观看记录",
    )


def _empty(
    descriptor: DiscoveryCollectionView,
    notice: DiscoveryCollectionNotice,
) -> DiscoveryCollectionTitlesView:
    return DiscoveryCollectionTitlesView(
        collection=descriptor,
        titles=[],
        returned_count=0,
        truncated=False,
        total_results=0,
        notice=notice,
    )


async def _chinese_titles(items: list[TraktItem]) -> dict[tuple[str, int], str]:
    pending = {(item.kind, item.trakt_id) for item in items}

    async def load(kind: MediaKind, trakt_id: int) -> tuple[tuple[str, int], str]:
        try:
            title = await _enrich_cache.get_or_set(
                f"trakt:zh:{kind.value}:{trakt_id}",
                _ENRICH_TTL,
                lambda kind=kind, trakt_id=trakt_id: _load_chinese_title(kind, trakt_id),
            )
        except Exception:
            title = ""
        return (kind.value, trakt_id), title

    loaded = await asyncio.gather(*(load(kind, trakt_id) for kind, trakt_id in pending))
    return dict(loaded)


async def _load_chinese_title(kind: MediaKind, trakt_id: int) -> str:
    group = "movies" if kind is MediaKind.MOVIE else "shows"
    client = await _client()
    try:
        body, _headers = await client.get_json(f"/{group}/{trakt_id}/translations/zh")
    except TraktNetworkError:
        raise
    except TraktError as exc:
        if str(exc).endswith("404"):
            return ""
        raise
    return chinese_title(body)


async def _client():
    from movieclaw_api.services.trakt_connection import get_trakt_client

    return await get_trakt_client()


async def _load_detail(kind: MediaKind, trakt_id: int) -> TraktDetail:
    group = "movies" if kind is MediaKind.MOVIE else "shows"
    client = await _client()
    try:
        body, _headers = await client.get_json(f"/{group}/{trakt_id}", params={"extended": "full"})
    except TraktError as exc:
        if str(exc).endswith("404"):
            raise LookupError("找不到这个 Trakt 条目") from exc
        raise
    translation, people, videos = await asyncio.gather(
        _optional_json(client, f"/{group}/{trakt_id}/translations/zh"),
        _optional_json(client, f"/{group}/{trakt_id}/people", params={"extended": "full"}),
        _videos(client, kind, trakt_id),
    )
    detail = parse_detail(body, kind, translation=translation, people=people, videos=videos)
    if detail is None:
        raise LookupError("找不到这个 Trakt 条目")
    return detail


async def _videos(client, kind: MediaKind, trakt_id: int) -> list:
    """电影只读自己的视频。剧集页上的预告和片头有一部分挂在季上。"""
    if kind is MediaKind.MOVIE:
        body = await _optional_json(client, f"/movies/{trakt_id}/videos")
        return body if isinstance(body, list) else []
    show_videos, seasons = await asyncio.gather(
        _optional_json(client, f"/shows/{trakt_id}/videos"),
        _optional_json(client, f"/shows/{trakt_id}/seasons"),
    )
    season_rows = seasons if isinstance(seasons, list) else []
    numbers = [
        row["number"]
        for row in season_rows
        if isinstance(row, dict) and isinstance(row.get("number"), int)
    ]
    extra = await asyncio.gather(
        *(
            _optional_json(client, f"/shows/{trakt_id}/seasons/{number}/videos")
            for number in numbers
        )
    )
    merged = list(show_videos) if isinstance(show_videos, list) else []
    for rows in extra:
        if isinstance(rows, list):
            merged.extend(rows)
    return merged


async def _optional_json(client, path: str, *, params: dict | None = None) -> object:
    try:
        body, _headers = await client.get_json(path, params=params)
    except TraktError:
        if path.endswith("/translations/zh") or path.endswith("/videos"):
            return []
        return {}
    return body


def _detail_view(detail: TraktDetail) -> DiscoveredTitleDetailsView:
    group = "movies" if detail.kind is MediaKind.MOVIE else "shows"
    poster = _still(detail.poster_url, 342, 513)
    backdrop = _still(detail.backdrop_url, 1280, 720)
    video = [
        MediaVideo(
            key=item.key,
            name=item.name,
            kind=item.kind,
            thumbnail_url=f"https://i.ytimg.com/vi/{item.key}/hqdefault.jpg",
            embed_url=f"https://www.youtube-nocookie.com/embed/{item.key}",
            watch_url=f"https://www.youtube.com/watch?v={item.key}",
        )
        for item in detail.videos
    ]
    return DiscoveredTitleDetailsView(
        title=DiscoveredTitleView(
            title_ref=make_title_ref(MediaSource.TRAKT, str(detail.trakt_id), detail.kind),
            provider=MediaSource.TRAKT,
            external_id=str(detail.trakt_id),
            media_type=detail.kind,
            title=detail.title,
            original_title=detail.original_title,
            release_year=detail.year,
            provider_rating=detail.rating,
            genres=list(detail.genres),
            extent_label=detail.extent,
            overview=detail.overview,
            poster_url=detail.poster_url,
            backdrop_url=detail.backdrop_url or None,
            tmdb_id=detail.tmdb_id,
        ),
        metadata=DiscoveredTitleMetadata(
            directors=[person.name for person in detail.directors],
            director_credits=[_member(person) for person in detail.directors],
            cast=[_member(person) for person in detail.cast],
            country=detail.country,
            language=detail.language,
            released=detail.released,
            network=detail.network or None,
            source_url=f"https://trakt.tv/{group}/{detail.slug}",
            english_title=detail.original_title,
        ),
        backdrop_original_url=detail.backdrop_url or None,
        videos=video,
        backdrops=[backdrop] if backdrop is not None else [],
        posters=[poster] if poster is not None else [],
    )


def _member(person) -> MediaCastMember:
    return MediaCastMember(
        name=person.name,
        role=person.role or None,
        avatar_url=person.avatar_url or None,
    )


def _still(url: str, width: int, height: int) -> MediaImage | None:
    if not url:
        return None
    return MediaImage(preview_url=url, full_url=url, width=width, height=height)


async def _expire(owner_id: int) -> None:
    from movieclaw_api.services.trakt_connection import TraktConnectionStore

    store = TraktConnectionStore()
    connection = await store.get(owner_id)
    connection.status = "expired"
    connection.access_token = ""
    connection.refresh_token = ""
    connection.expires_at = ""
    await store.save(owner_id, connection)


async def _force_due(owner_id: int):
    from movieclaw_api.services.trakt_connection import TraktConnectionStore

    store = TraktConnectionStore()
    connection = await store.get(owner_id)
    if connection.status != "connected" or not connection.refresh_token:
        return None
    connection.expires_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await store.save(owner_id, connection)
    return connection
