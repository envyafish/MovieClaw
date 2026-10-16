"""把 Trakt 片单编进现有的发现页协议。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from movieclaw_api.exceptions import TraktAppUnauthorizedException
from movieclaw_api.schemas.discover import (
    DiscoveredTitleView,
    DiscoveryCollectionNotice,
    DiscoveryCollectionTitlesView,
    DiscoveryCollectionView,
)
from movieclaw_api.services.discover_library import DiscoverLibraryProjectionService
from movieclaw_api.services.title_discovery import (
    make_collection_ref,
    make_title_ref,
    title_from_card,
)
from movieclaw_cache.memory import AsyncTTLCache
from movieclaw_media.models import MediaCard, MediaKind, MediaSource
from movieclaw_media.trakt import TraktAuthError, TraktItem, parse_calendar, parse_listing

_APP_UNAUTHORIZED = "Trakt 应用凭证无效，请管理员检查 Client ID 和 Client Secret"
_PUBLIC_TTL = 15 * 60
_PERSONAL_TTL = 10 * 60
_ENRICH_TTL = 24 * 60 * 60
_NOT_CONNECTED = DiscoveryCollectionNotice(code="trakt_not_connected", message="连接 Trakt 后查看")

# id, title, preview limit, full listing, needs a user token
_MOVIE: tuple[tuple[str, str, int, bool, bool], ...] = (
    ("calendar", "上映日历", 20, False, True),
    ("recommendations", "为你推荐", 20, False, True),
    ("trending", "热门", 20, True, False),
    ("popular", "流行", 20, True, False),
    ("anticipated", "最受期待", 20, True, False),
    ("boxoffice", "票房", 10, False, False),
)
_TV: tuple[tuple[str, str, int, bool, bool], ...] = (
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
                params=params,
            )
        except TraktAuthError as exc:
            raise TraktAppUnauthorizedException(_APP_UNAUTHORIZED) from exc
        payload = body if isinstance(body, list) else []
        return payload, _page_count(headers)

    async def _to_titles(
        self,
        items: list[TraktItem],
        projection: DiscoverLibraryProjectionService | None,
    ) -> list[DiscoveredTitleView]:
        prepared: list[tuple[TraktItem, MediaCard | None]] = []
        cards: list[MediaCard] = []
        configured = bool(_tmdb_key())
        for item in items:
            card = None
            title = item.title
            poster = ""
            if item.tmdb_id is not None and configured:
                enriched = await _cached_enrich(item.tmdb_id, item.kind)
                if enriched is not None:
                    title, poster = enriched
                card = MediaCard(
                    id=str(item.tmdb_id),
                    source=MediaSource.TMDB,
                    type=item.kind,
                    title=title,
                    original_title=item.title,
                    year=item.year or 0,
                    rating=0,
                    poster_url=poster,
                )
                cards.append(card)
            prepared.append((item, card))
        if projection is not None and cards:
            await projection.apply_cards(cards)
        titles: list[DiscoveredTitleView] = []
        for item, card in prepared:
            if card is not None:
                titles.append(
                    title_from_card(card).model_copy(update={"note": item.note, "navigable": True})
                )
                continue
            titles.append(
                DiscoveredTitleView(
                    title_ref=make_title_ref(MediaSource.TRAKT, str(item.trakt_id), item.kind),
                    provider=MediaSource.TRAKT,
                    external_id=str(item.trakt_id),
                    media_type=item.kind,
                    title=item.title,
                    original_title=item.title,
                    release_year=item.year,
                    poster_url="",
                    note=item.note,
                    navigable=False,
                )
            )
        return titles


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


def _personal_request(collection_id: str, media_type: MediaKind) -> tuple[str, dict | None]:
    group = _group(media_type)
    if collection_id == "calendar":
        today = datetime.now().astimezone().date().isoformat()
        return f"/calendars/my/{group}/{today}/30", None
    return f"/recommendations/{group}", {"limit": 20}


def _page_count(headers: dict) -> int:
    raw = headers.get("x-pagination-page-count") or "1"
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _empty_notice(collection_id: str) -> DiscoveryCollectionNotice:
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


async def _client():
    from movieclaw_api.services.trakt_connection import get_trakt_client

    return await get_trakt_client()


def _tmdb_key() -> str:
    from movieclaw_api.core.config import get_settings

    return get_settings().tmdb_api_key or ""


async def _cached_enrich(tmdb_id: int, kind: MediaKind) -> tuple[str, str] | None:
    try:
        return await _enrich_cache.get_or_set(
            f"tmdb:{kind.value}:{tmdb_id}",
            _ENRICH_TTL,
            lambda tmdb_id=tmdb_id, kind=kind: enrich_tmdb(tmdb_id, kind),
        )
    except Exception:
        return None


async def enrich_tmdb(tmdb_id: int, kind: MediaKind) -> tuple[str, str]:
    from movieclaw_api.services.media_discover import get_tmdb_client
    from movieclaw_api.services.network_egress import effective_tmdb_image_base_url

    data = await get_tmdb_client().get(f"/{kind.value}/{tmdb_id}", {"language": "zh-CN"})
    title = str(data.get("title") or data.get("name") or "")
    poster_path = data.get("poster_path") or ""
    poster = f"{effective_tmdb_image_base_url()}/w342{poster_path}" if poster_path else ""
    return title, poster


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
