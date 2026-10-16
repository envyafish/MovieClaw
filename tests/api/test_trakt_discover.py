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
from movieclaw_media.trakt import TraktAuthError, TraktError

_APP = "/api/v1/integrations/trakt/app"


class FakeTrakt:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self.translations: list = []
        self.summary: dict | None = None
        self.people: dict = {}
        self.videos: list = []
        self.season_videos: dict[int, list] = {}
        self.trending_error: Exception | None = None
        self.calendar: list = [
            {
                "released": "2026-10-18",
                "movie": {"title": "Dune", "year": 2026, "ids": {"trakt": 1, "tmdb": 99}},
            }
        ]
        self.show_calendar: list = []
        self.recommendations: list = []
        self.playback: list = []
        self.trending: list = [
            {
                "watchers": 3,
                "movie": {"title": "Hot", "year": 2020, "ids": {"trakt": 4, "tmdb": 5}},
            },
            {"watchers": 1, "movie": {"title": "Soon", "year": 2027, "ids": {"trakt": 6}}},
        ]

    async def get_json(self, path, *, token=None, params=None):
        self.calls.append((path, params))
        if path.endswith("/translations/zh"):
            return self.translations, {}
        if path.endswith("/people"):
            return self.people, {}
        if "/seasons/" in path and path.endswith("/videos"):
            number = int(path.split("/seasons/")[1].split("/")[0])
            return self.season_videos.get(number, []), {}
        if path.endswith("/seasons"):
            return [{"number": number} for number in self.season_videos], {}
        if path.endswith("/videos"):
            return self.videos, {}
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] in {"movies", "shows"} and parts[1].isdigit():
            ids = (self.summary or {}).get("ids") or {}
            if self.summary is None or str(ids.get("trakt")) != parts[1]:
                raise TraktError("Trakt 返回 404")
            return self.summary, {}
        if path.endswith("/trending"):
            if self.trending_error is not None:
                raise self.trending_error
            return self.trending, {"x-pagination-page-count": "3"}
        if "/calendars/my/movies/" in path:
            return self.calendar, {}
        if "/calendars/my/shows/" in path:
            return self.show_calendar, {}
        if path.startswith("/sync/playback/"):
            return self.playback, {}
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
        "playback",
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
    assert [item["title"] for item in tv["sections"][:2]] == ["继续观看", "追剧日历"]


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


async def test_show_calendar_lists_the_whole_window(client: TestClient) -> None:
    from datetime import datetime

    _save_app(client)
    await _connect()
    client.fake.translations = [{"title": "沧元图", "country": "cn"}]
    client.fake.show_calendar = [
        {
            "first_aired": "2026-10-16T02:00:00.000Z",
            "episode": {
                "season": 1,
                "number": number,
                "title": f"Episode {number}",
                "images": {"screenshot": ["media.trakt.tv/images/ep.jpg.webp"]},
            },
            "show": {
                "title": "Tales of Herding Gods",
                "network": "Youku",
                "ids": {"trakt": 9, "tmdb": 55},
                "images": {"fanart": ["media.trakt.tv/images/fan.jpg.webp"]},
            },
        }
        for number in range(1, 22)
    ]
    response = client.get("/api/v1/ui/discovery/tv/trakt-calendar")
    assert response.status_code == 200
    data = response.json()["data"]
    episodes = data["days"][0]["episodes"]
    aired = datetime.fromisoformat("2026-10-16T02:00:00+00:00").astimezone()
    weekdays = "一二三四五六日"
    assert len(episodes) == 21
    assert episodes[0]["show_title"] == "沧元图"
    assert episodes[0]["code"] == "S01E01"
    assert episodes[0]["aired_time"] == aired.strftime("%H:%M")
    assert episodes[0]["landscape_url"] == "https://media.trakt.tv/images/ep.jpg.webp"
    assert episodes[0]["title_ref"] == "trakt:tv:9"
    assert data["days"][0]["label"] == (
        f"{aired.month}月{aired.day}日 周{weekdays[aired.weekday()]}"
    )
    assert any(
        path.startswith("/calendars/my/shows/")
        and path.endswith("/30")
        and params == {"extended": "full"}
        for path, params in client.fake.calls
    )


def test_show_calendar_without_a_token(client: TestClient) -> None:
    _save_app(client)
    data = client.get("/api/v1/ui/discovery/tv/trakt-calendar").json()["data"]
    assert data["days"] == []
    assert data["notice"]["code"] == "trakt_not_connected"


async def test_show_calendar_empty_window(client: TestClient) -> None:
    _save_app(client)
    await _connect()
    data = client.get("/api/v1/ui/discovery/tv/trakt-calendar").json()["data"]
    assert data["days"] == []
    assert data["notice"]["code"] == "trakt_calendar_empty"


async def test_calendar_note_and_trakt_cards_open_trakt_details(client: TestClient) -> None:
    _save_app(client)
    await _connect()
    calendar = client.get("/api/v1/discover/collections/trakt:movie:calendar/titles").json()["data"]
    assert calendar["titles"][0]["note"] == "10月18日 上映"
    assert calendar["titles"][0]["title_ref"] == "trakt:movie:1"
    assert calendar["titles"][0]["navigable"] is True

    trending = client.get("/api/v1/discover/collections/trakt:movie:trending/titles").json()["data"]
    titles = trending["titles"]
    assert {item["title_ref"] for item in titles} == {"trakt:movie:4", "trakt:movie:6"}
    assert all(item["navigable"] is True for item in titles)
    missing = client.get("/api/v1/discover/titles/trakt:movie:6")
    assert missing.status_code == 404
    assert missing.json()["message"] == "找不到这个 Trakt 条目"


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
    playback = client.get("/api/v1/discover/collections/trakt:tv:playback/titles").json()["data"]
    assert playback["notice"]["code"] == "trakt_playback_empty"
    assert playback["collection"]["name"] == "继续观看"


async def test_playback_row_shows_the_paused_episode(client: TestClient) -> None:
    _save_app(client)
    await _connect()
    client.fake.playback = [
        {
            "progress": 40,
            "type": "episode",
            "episode": {"season": 1, "number": 2},
            "show": {"title": "Noragami", "year": 2014, "ids": {"trakt": 7, "tmdb": 64710}},
        }
    ]
    data = client.get("/api/v1/discover/collections/trakt:tv:playback/titles").json()["data"]
    assert data["titles"][0]["title_ref"] == "trakt:tv:7"
    assert data["titles"][0]["note"] == "S01E02 · 看到 40%"
    assert data["titles"][0]["tmdb_id"] == 64710
    assert ("/sync/playback/episodes", {"extended": "full"}) in client.fake.calls


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
    assert ("/movies/popular", {"extended": "full", "page": 2, "limit": 20}) in client.fake.calls


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


def test_trakt_posters_and_chinese_titles_without_tmdb(client: TestClient) -> None:
    _save_app(client)
    client.fake.translations = [{"title": "沧元图", "country": "cn"}]
    client.fake.trending = [
        {
            "watchers": 1,
            "movie": {
                "title": "The Demon Hunter",
                "year": 2023,
                "ids": {"trakt": 9},
                "images": {"poster": ["media.trakt.tv/images/a.jpg.webp"]},
            },
        }
    ]
    title = client.get("/api/v1/discover/collections/trakt:movie:trending/titles")
    card = title.json()["data"]["titles"][0]
    assert card["title"] == "沧元图"
    assert card["original_title"] == "The Demon Hunter"
    assert card["poster_url"] == "https://media.trakt.tv/images/a.jpg.webp"
    assert card["title_ref"] == "trakt:movie:9"
    assert card["navigable"] is True


def test_trakt_title_details_come_from_trakt(client: TestClient) -> None:
    _save_app(client)
    client.fake.summary = {
        "title": "The Demon Hunter",
        "year": 2023,
        "ids": {"trakt": 9, "tmdb": 55, "slug": "the-demon-hunter"},
        "overview": "English plot",
        "released": "2023-07-01",
        "runtime": 24,
        "country": "cn",
        "language": "zh",
        "rating": 8.2,
        "genres": ["animation"],
        "images": {"poster": ["media.trakt.tv/images/a.jpg.webp"]},
    }
    client.fake.videos = [
        {
            "title": "Official Trailer",
            "url": "https://youtu.be/abc123XYZ_-",
            "type": "trailer",
            "official": True,
        },
        {
            "title": "制作花絮",
            "url": "https://www.youtube.com/watch?v=feat123",
            "type": "featurette",
        },
    ]
    client.fake.translations = [{"title": "沧元图", "overview": "中文简介", "country": "cn"}]
    client.fake.people = {
        "cast": [{"character": "孟川", "person": {"name": "Someone"}}],
        "crew": {"directing": [{"job": "Director", "person": {"name": "Director Name"}}]},
    }
    response = client.get("/api/v1/discover/titles/trakt:movie:9")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["title"]["title"] == "沧元图"
    assert data["title"]["overview"] == "中文简介"
    assert data["title"]["title_ref"] == "trakt:movie:9"
    assert data["title"]["provider"] == "trakt"
    assert data["title"]["tmdb_id"] == 55
    assert data["title"]["extent_label"] == "24 分钟"
    assert data["title"]["poster_url"] == "https://media.trakt.tv/images/a.jpg.webp"
    assert data["metadata"]["directors"] == ["Director Name"]
    assert data["metadata"]["cast"][0]["name"] == "Someone"
    assert data["metadata"]["cast"][0]["role"] == "孟川"
    assert data["metadata"]["source_url"] == "https://trakt.tv/movies/the-demon-hunter"
    assert [(item["kind"], item["key"]) for item in data["videos"]] == [
        ("预告片", "abc123XYZ_-"),
        ("花絮", "feat123"),
    ]
    assert data["recommendations"] == []
    assert data["collection"] is None
    missing = client.get("/api/v1/discover/titles/trakt:movie:404")
    assert missing.status_code == 404


def test_show_details_include_season_trailers_and_opening(client: TestClient) -> None:
    _save_app(client)
    client.fake.summary = {
        "title": "Noragami",
        "year": 2014,
        "ids": {"trakt": 7, "slug": "noragami"},
    }
    client.fake.translations = [{"title": "野良神", "country": "cn"}]
    client.fake.videos = [
        {
            "title": f"PV {index}",
            "url": f"https://youtu.be/show{index}",
            "type": "trailer",
            "official": True,
        }
        for index in range(3)
    ]
    client.fake.season_videos = {
        2: [
            {"title": "Aragoto", "url": "https://youtu.be/sea20", "type": "trailer"},
            {"title": "Aragoto 2", "url": "https://youtu.be/sea21", "type": "trailer"},
            {"title": "Opening", "url": "https://youtu.be/open1", "type": "opening credits"},
        ]
    }
    response = client.get("/api/v1/discover/titles/trakt:tv:7")
    assert response.status_code == 200
    kinds = [item["kind"] for item in response.json()["data"]["videos"]]
    assert kinds.count("预告片") == 5
    assert kinds.count("片头") == 1


def test_search_providers_do_not_include_trakt() -> None:
    assert {item.value for item in DiscoveryProviderSelection} == {"all", "tmdb", "douban"}
