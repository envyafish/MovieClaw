from datetime import date
from zoneinfo import ZoneInfo

from movieclaw_media.models import MediaKind
from movieclaw_media.trakt import calendar_note, parse_calendar, parse_listing

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_calendar_notes() -> None:
    assert calendar_note(date(2026, 10, 18), episode=None) == "10月18日 上映"
    assert calendar_note(date(2026, 10, 8), episode=(2, 5)) == "10月8日 · S02E05"


def test_movie_calendar_and_duplicate_episodes() -> None:
    movies = parse_calendar(
        [{"released": "2026-10-18", "movie": {"title": "Dune", "year": 2026, "ids": {"trakt": 1, "tmdb": 99}}}],
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


def test_listing_unwraps_wrappers_and_keeps_items_without_tmdb() -> None:
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
