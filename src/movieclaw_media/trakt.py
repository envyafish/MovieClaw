"""Trakt 响应到发现卡片的映射。不读配置，也不发请求。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from movieclaw_media.models import MediaKind


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
