"""Trakt 发现页片单：公开榜单不需要登录令牌，日历和推荐需要。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.schemas.discover import DiscoveryProviderSelection
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.media_discover import reset_media_service
from movieclaw_api.services.trakt_connection import TraktConnection, TraktConnectionStore
from movieclaw_api.services.trakt_discover import _cache, _enrich_cache
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_media.models import MediaKind
from movieclaw_media.trakt import TraktAuthError, TraktError

_APP = "/api/v1/integrations/trakt/app"


class FakeTrakt:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self.trending_error: Exception | None = None
        self.calendar: list = [
            {
                "released": "2026-10-18",
                "movie": {"title": "Dune", "year": 2026, "ids": {"trakt": 1, "tmdb": 99}},
            }
        ]
        self.recommendations: list = []
        self.trending: list = [
            {
                "watchers": 3,
                "movie": {"title": "Hot", "year": 2020, "ids": {"trakt": 4, "tmdb": 5}},
            },
            {"watchers": 1, "movie": {"title": "Soon", "year": 2027, "ids": {"trakt": 6}}},
        ]

    async def get_json(self, path, *, token=None, params=None):
        self.calls.append((path, params))
        if path.endswith("/trending"):
            if self.trending_error is not None:
                raise self.trending_error
            return self.trending, {"x-pagination-page-count": "3"}
        if "/calendars/my/movies/" in path:
            return self.calendar, {}
        if path.endswith("/recommendations/movies"):
            return self.recommendations, {}
        if path.endswith("/boxoffice"):
            return [
                {
                    "revenue": 1,
                    "movie": {"title": "Box", "year": 2024, "ids": {"trakt": 8, "tmdb": 9}},
                }
            ], {}
        return [], {"x-pagination-page-count": "2"}


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
    _cache.clear()
    _enrich_cache.clear()
    fake = FakeTrakt()

    async def get_client():
        return fake

    monkeypatch.setattr("movieclaw_api.services.trakt_connection.get_trakt_client", get_client)

    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        c.post("/api/v1/auth/bootstrap", json={"username": "admin", "password": "s3cret-pass"})
        c.fake = fake
        yield c

    reset_media_service()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()
    _cache.clear()
    _enrich_cache.clear()


def _save_app(client: TestClient) -> None:
    saved = client.put(_APP, json={"client_id": "cid", "client_secret": "super-secret"})
    assert saved.status_code == 200


async def _connect(owner_id: int = 0) -> None:
    await TraktConnectionStore().save(
        owner_id,
        TraktConnection(
            status="connected",
            access_token="access",
            refresh_token="refresh",
            expires_at="2099-01-01T00:00:00+00:00",
            username="ada",
        ),
    )


def test_unconfigured_trakt_page_tells_the_admin(client: TestClient) -> None:
    response = client.get("/api/v1/ui/discovery/movie", params={"provider": "trakt"})
    assert response.status_code == 502
    assert response.json()["message"] == "管理员尚未配置 Trakt。请在设置 → Trakt 填写应用凭证。"


def test_movie_and_tv_section_order(client: TestClient) -> None:
    _save_app(client)
    movie = client.get("/api/v1/ui/discovery/movie", params={"provider": "trakt"}).json()["data"]
    assert [item["collection_ref"].split(":")[-1] for item in movie["sections"]] == [
        "calendar",
        "recommendations",
        "trending",
        "popular",
        "anticipated",
        "boxoffice",
    ]
    tv = client.get("/api/v1/ui/discovery/tv", params={"provider": "trakt"}).json()["data"]
    refs = [item["collection_ref"] for item in tv["sections"]]
    assert "boxoffice" not in "".join(refs)
    assert tv["sections"][0]["title"] == "追剧日历"


def test_public_chart_without_a_user_token(client: TestClient) -> None:
    _save_app(client)
    response = client.get("/api/v1/discover/collections/trakt:movie:trending/titles")
    assert response.status_code == 200
    titles = response.json()["data"]["titles"]
    assert titles[0]["title"] == "Hot"
    calendar = client.get("/api/v1/discover/collections/trakt:movie:calendar/titles")
    assert calendar.status_code == 200
    assert calendar.json()["data"]["notice"]["code"] == "trakt_not_connected"
    assert calendar.json()["data"]["titles"] == []


async def test_calendar_note_and_tmdb_enrichment(client: TestClient, monkeypatch) -> None:
    _save_app(client)
    await _connect()
    monkeypatch.setenv("TMDB_API_KEY", "test-key")
    get_settings.cache_clear()

    async def enrich(tmdb_id: int, kind: MediaKind):
        if tmdb_id == 5:
            raise RuntimeError("poster failed")
        return "沙丘", "https://image.tmdb.org/t/p/w342/p.jpg"

    monkeypatch.setattr("movieclaw_api.services.trakt_discover.enrich_tmdb", enrich)
    calendar = client.get("/api/v1/discover/collections/trakt:movie:calendar/titles").json()["data"]
    assert calendar["titles"][0]["note"] == "10月18日 上映"
    assert calendar["titles"][0]["title_ref"] == "tmdb:movie:99"
    assert calendar["titles"][0]["navigable"] is True
    assert calendar["titles"][0]["title"] == "沙丘"

    trending = client.get("/api/v1/discover/collections/trakt:movie:trending/titles")
    trending = trending.json()["data"]["titles"]
    failed = next(item for item in trending if item["external_id"] == "5")
    kept = next(item for item in trending if item["title_ref"].startswith("trakt:"))
    assert failed["title"] == "Hot"
    assert failed["poster_url"] == ""
    assert failed["navigable"] is True
    assert kept["navigable"] is False
    assert kept["title_ref"] == "trakt:movie:6"
    missing = client.get("/api/v1/discover/titles/trakt:movie:6")
    assert missing.status_code == 404
    assert missing.json()["message"] == "这条目没有 TMDB 资料"


def test_invalid_application_is_a_page_level_error(client: TestClient) -> None:
    _save_app(client)
    client.fake.trending_error = TraktAuthError("rejected")
    response = client.get("/api/v1/discover/collections/trakt:movie:trending/titles")
    assert response.status_code == 502
    assert response.json()["code"] == "TRAKT_APP_UNAUTHORIZED"


async def test_empty_personal_rows_have_notices(client: TestClient) -> None:
    _save_app(client)
    await _connect()
    client.fake.calendar = []
    calendar = client.get("/api/v1/discover/collections/trakt:movie:calendar/titles").json()["data"]
    assert calendar["notice"]["code"] == "trakt_calendar_empty"
    recommendations = client.get("/api/v1/discover/collections/trakt:movie:recommendations/titles")
    recommendations = recommendations.json()["data"]
    assert recommendations["notice"]["code"] == "trakt_recommendations_empty"


def test_box_office_has_no_next_page_and_trending_does(client: TestClient) -> None:
    _save_app(client)
    listed = client.get(
        "/api/v1/discover/collections",
        params={"media_type": "movie", "provider": "trakt"},
    )
    rows = listed.json()["data"]["collections"]
    by_id = {item["collection_ref"].split(":")[-1]: item for item in rows}
    assert by_id["boxoffice"]["supports_full_listing"] is False
    assert by_id["trending"]["supports_full_listing"] is True
    client.get("/api/v1/discover/collections/trakt:movie:popular/titles", params={"page": 2})
    assert ("/movies/popular", {"page": 2, "limit": 20}) in client.fake.calls


async def test_rate_limit_does_not_drop_the_token(client: TestClient) -> None:
    _save_app(client)
    await _connect()
    client.fake.trending_error = TraktError("Trakt 返回 429")
    failed = client.get("/api/v1/discover/collections/trakt:movie:trending/titles")
    assert failed.status_code == 502
    calendar = client.get("/api/v1/discover/collections/trakt:movie:calendar/titles")
    assert calendar.status_code == 200
    assert calendar.json()["data"]["titles"][0]["title"] == "Dune"
    connection = await TraktConnectionStore().get(0)
    assert connection.status == "connected"
    assert connection.access_token == "access"


def test_search_providers_do_not_include_trakt() -> None:
    assert {item.value for item in DiscoveryProviderSelection} == {"all", "tmdb", "douban"}
