from datetime import date
from zoneinfo import ZoneInfo

from movieclaw_media.models import MediaKind
from movieclaw_media.trakt import (
    calendar_note,
    chinese_title,
    parse_calendar,
    parse_detail,
    parse_listing,
    parse_playback,
    parse_show_calendar,
    parse_videos,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_playback_keeps_recent_progress_and_episode() -> None:
    payload = [
        {
            "progress": 65.4,
            "type": "movie",
            "movie": {"title": "Dune", "year": 2021, "ids": {"trakt": 1}},
        },
        {
            "progress": 10,
            "type": "episode",
            "episode": {"season": 2, "number": 5},
            "show": {"title": "Noragami", "year": 2014, "ids": {"trakt": 2}},
        },
    ]
    movies = parse_playback(payload, MediaKind.MOVIE)
    shows = parse_playback(payload, MediaKind.TV)
    assert [(item.title, item.note) for item in movies] == [("Dune", "看到 65%")]
    assert [(item.title, item.note) for item in shows] == [("Noragami", "S02E05 · 看到 10%")]


def test_calendar_notes() -> None:
    assert calendar_note(date(2026, 10, 18), episode=None) == "10月18日 上映"
    assert calendar_note(date(2026, 10, 8), episode=(2, 5)) == "10月8日 · S02E05"


def test_movie_calendar_and_duplicate_episodes() -> None:
    movies = parse_calendar(
        [
            {
                "released": "2026-10-18",
                "movie": {"title": "Dune", "year": 2026, "ids": {"trakt": 1, "tmdb": 99}},
            }
        ],
        MediaKind.MOVIE,
        tz=SHANGHAI,
    )
    assert movies[0].note == "10月18日 上映"
    assert movies[0].tmdb_id == 99

    shows = parse_calendar(
        [
            {
                "first_aired": "2026-10-17T16:00:00.000Z",
                "episode": {"season": 2, "number": 5},
                "show": {"title": "Show", "year": 2024, "ids": {"trakt": 7, "tmdb": 8}},
            },
            {
                "first_aired": "2026-10-18T16:00:00.000Z",
                "episode": {"season": 2, "number": 6},
                "show": {"title": "Show", "year": 2024, "ids": {"trakt": 7, "tmdb": 8}},
            },
        ],
        MediaKind.TV,
        tz=SHANGHAI,
    )
    assert [item.note for item in shows] == ["10月18日 · S02E05", "10月19日 · S02E06"]
    assert shows[0].trakt_id == 7


def test_show_calendar_prefers_screenshot_then_fanart() -> None:
    episodes = parse_show_calendar(
        [
            {
                "first_aired": "2026-10-16T02:00:00.000Z",
                "episode": {
                    "season": 1,
                    "number": 99,
                    "title": "Episode 99",
                    "runtime": 23,
                    "overview": "A long night.",
                    "images": {"screenshot": ["media.trakt.tv/images/ep.jpg.webp"]},
                },
                "show": {
                    "title": "The Demon Hunter",
                    "network": "Youku",
                    "ids": {"trakt": 9, "tmdb": 55},
                    "images": {"fanart": ["media.trakt.tv/images/fan.jpg.webp"]},
                },
            },
            {
                "first_aired": "2026-10-17T02:00:00.000Z",
                "episode": {
                    "season": 1,
                    "number": 100,
                    "title": "",
                    "images": {"screenshot": []},
                },
                "show": {
                    "title": "The Demon Hunter",
                    "ids": {"trakt": 9},
                    "images": {
                        "fanart": [],
                        "thumb": ["media.trakt.tv/images/thumb.jpg.webp"],
                    },
                },
            },
        ],
        tz=SHANGHAI,
    )
    assert episodes[0].landscape_url == "https://media.trakt.tv/images/ep.jpg.webp"
    assert episodes[0].aired.hour == 10
    assert episodes[0].episode_title == "Episode 99"
    assert episodes[0].runtime == 23
    assert episodes[0].network == "Youku"
    assert episodes[0].tmdb_id == 55
    assert episodes[1].landscape_url == "https://media.trakt.tv/images/thumb.jpg.webp"
    assert episodes[1].episode_title == "第100集"
    assert episodes[1].tmdb_id is None
    kind = MediaKind.MOVIE
    trending = parse_listing(
        [{"watchers": 3, "movie": {"title": "Hot", "year": 2020, "ids": {"trakt": 4, "tmdb": 5}}}],
        kind,
    )
    anticipated = parse_listing(
        [{"list_count": 9, "movie": {"title": "Soon", "year": 2027, "ids": {"trakt": 6}}}],
        kind,
    )
    popular = parse_listing(
        [{"title": "Pop", "year": 2019, "ids": {"trakt": 2, "tmdb": 3}}],
        kind,
    )
    assert trending[0].title == "Hot" and trending[0].tmdb_id == 5
    assert anticipated[0].tmdb_id is None and anticipated[0].trakt_id == 6
    assert popular[0].title == "Pop"


def test_poster_url_and_simplified_chinese_title() -> None:
    items = parse_listing(
        [
            {
                "title": "The Demon Hunter",
                "year": 2023,
                "ids": {"trakt": 9},
                "images": {"poster": ["media.trakt.tv/images/a.jpg.webp"]},
            }
        ],
        MediaKind.TV,
    )
    assert items[0].poster_url == "https://media.trakt.tv/images/a.jpg.webp"
    assert (
        chinese_title(
            [
                {"title": "滄元圖", "country": "tw"},
                {"title": "沧元图", "country": "cn"},
            ]
        )
        == "沧元图"
    )


def test_five_trailers_and_an_opening_all_stay() -> None:
    payload = [
        {"title": f"PV {index}", "url": f"https://youtu.be/trail{index}", "type": "trailer"}
        for index in range(5)
    ]
    payload.append(
        {"title": "片头曲", "url": "https://youtu.be/open01", "type": "opening credits"}
    )
    videos = parse_videos(payload)
    assert [video.kind for video in videos].count("预告片") == 5
    assert ("片头", "片头曲") in [(video.kind, video.name) for video in videos]


def test_videos_keep_each_kind_when_teasers_crowd_the_list() -> None:
    payload = [
        {"title": f"Teaser {index}", "url": f"https://youtu.be/teaser{index:02d}", "type": "teaser"}
        for index in range(10)
    ]
    payload.append({"title": "制作花絮", "url": "https://youtu.be/feat999", "type": "featurette"})
    videos = parse_videos(payload)
    assert sum(video.kind == "先导预告" for video in videos) == 4
    assert ("花絮", "制作花絮") in [(video.kind, video.name) for video in videos]


def test_detail_uses_simplified_chinese_images_and_credits() -> None:
    detail = parse_detail(
        {
            "title": "The Demon Hunter",
            "year": 2023,
            "overview": "English plot",
            "released": "2023-07-01",
            "runtime": 24,
            "country": "cn",
            "language": "zh",
            "rating": 8.2,
            "genres": ["animation", "action", "none"],
            "ids": {"trakt": 9, "tmdb": 55, "slug": "the-demon-hunter"},
            "images": {
                "poster": ["media.trakt.tv/images/a.jpg.webp"],
                "fanart": ["media.trakt.tv/images/b.jpg.webp"],
            },
        },
        MediaKind.MOVIE,
        translation=[{"title": "沧元图", "overview": "中文简介", "country": "cn"}],
        people={
            "cast": [
                {
                    "character": "孟川",
                    "person": {
                        "name": "Someone",
                        "images": {"headshot": ["media.trakt.tv/images/h.jpg.webp"]},
                    },
                }
            ],
            "crew": {"directing": [{"job": "Director", "person": {"name": "Director Name"}}]},
        },
        videos=[
            {
                "title": "Official Trailer",
                "url": "https://www.youtube.com/watch?v=abc123XYZ_-",
                "site": "youtube",
                "type": "trailer",
                "official": True,
                "published_at": "2023-01-01T00:00:00.000Z",
            },
            {
                "title": "制作花絮",
                "url": "https://youtu.be/feat123",
                "site": "youtube",
                "type": "featurette",
                "official": True,
                "published_at": "2023-02-01T00:00:00.000Z",
            },
            {
                "title": "Not YouTube",
                "url": "https://vimeo.com/1",
                "site": "vimeo",
                "type": "clip",
            },
        ],
    )
    assert detail is not None
    assert detail.title == "沧元图"
    assert detail.original_title == "The Demon Hunter"
    assert detail.overview == "中文简介"
    assert detail.poster_url == "https://media.trakt.tv/images/a.jpg.webp"
    assert detail.backdrop_url == "https://media.trakt.tv/images/b.jpg.webp"
    assert [(video.kind, video.name, video.key) for video in detail.videos] == [
        ("预告片", "Official Trailer", "abc123XYZ_-"),
        ("花絮", "制作花絮", "feat123"),
    ]
    assert detail.genres == ("动画", "动作")
    assert detail.country == "中国"
    assert detail.language == "汉语"
    assert detail.extent == "24 分钟"
    assert detail.directors[0].name == "Director Name"
    assert detail.cast[0].role == "孟川"
    assert detail.cast[0].avatar_url == "https://media.trakt.tv/images/h.jpg.webp"
    assert detail.slug == "the-demon-hunter"
    assert detail.tmdb_id == 55
