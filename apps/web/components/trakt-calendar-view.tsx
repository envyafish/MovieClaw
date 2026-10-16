"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Route } from "next";
import Link from "next/link";

import { ChevronLeftIcon, ChevronRightIcon } from "@/components/icons";
import { PageNav } from "@/components/page-nav";
import { PosterImage } from "@/components/poster-image";
import {
  fetchTraktShowCalendar,
  type TraktCalendarDay,
  type TraktCalendarEpisode,
} from "@/lib/api/discover";
import { buildDiscoveryReturnPath } from "@/lib/discovery-return-path";
import { HttpError } from "@/lib/http";

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

/**
 * 追剧日历完整页，对齐 Trakt：顶部一周日期条，下面按天铺横图卡片。
 */
export function TraktCalendarView() {
  const [days, setDays] = useState<TraktCalendarDay[] | null>(null);
  const [notice, setNotice] = useState<{ code: string; message: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState(() => startOfDay(new Date()));
  const sections = useRef(new Map<string, HTMLElement>());

  useEffect(() => {
    const controller = new AbortController();
    fetchTraktShowCalendar({ signal: controller.signal })
      .then((calendar) => {
        setDays(calendar.days);
        setNotice(calendar.notice);
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted) return;
        setError(caught instanceof HttpError ? caught.message : "追剧日历加载失败");
        setDays([]);
      });
    return () => controller.abort();
  }, []);

  const today = useMemo(() => startOfDay(new Date()), []);
  const horizon = useMemo(() => addDays(today, 30), [today]);
  const counts = useMemo(() => {
    const map = new Map<string, number>();
    for (const day of days ?? []) map.set(day.date, day.episodes.length);
    return map;
  }, [days]);
  const weekStart = startOfWeek(selected);
  const week = Array.from({ length: 7 }, (_, index) => addDays(weekStart, index));
  const selectedKey = iso(selected);

  const focus = (date: Date) => {
    const clamped = date < today ? today : date > horizon ? horizon : date;
    setSelected(clamped);
    const key = iso(clamped);
    const exact = sections.current.get(key);
    if (exact) {
      exact.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    const later = (days ?? []).find((day) => day.date >= key);
    if (later) {
      sections.current.get(later.date)?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  };

  return (
    <div className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-12 page-inset">
      <PageNav
        title="追剧日历"
        fallback={{ label: "发现", href: "/discover/tv?source=trakt" as Route }}
        className="page-inset-bleed"
      />
      <header className="mt-1">
        <h1 className="text-3xl font-bold tracking-[-0.03em] text-[var(--text)]">追剧日历</h1>
      </header>
      <div className="mt-5">
        <WeekStrip
          week={week}
          today={today}
          horizon={horizon}
          selectedKey={selectedKey}
          counts={counts}
          canPrev={weekStart > startOfWeek(today)}
          canNext={addDays(weekStart, 7) <= horizon}
          onSelect={focus}
          onToday={() => focus(today)}
          onShift={(delta) => focus(addDays(selected, delta * 7))}
        />
      </div>
      {error ? <p className="mt-8 text-body text-[var(--text-muted)]">{error}</p> : null}
      {notice ? <CalendarNotice notice={notice} /> : null}
      {days === null ? (
        <p className="mt-8 text-body text-[var(--text-muted)]">正在读取追剧日历…</p>
      ) : (
        days.map((day) => (
          <section
            key={day.date}
            ref={(node) => {
              if (node) sections.current.set(day.date, node);
              else sections.current.delete(day.date);
            }}
            className="mt-8 scroll-mt-16"
          >
            <h2 className="text-xl font-semibold text-[var(--text)]">{heading(day.date)}</h2>
            <ul className="mt-4 grid grid-cols-2 gap-x-3 gap-y-5 min-[520px]:grid-cols-[repeat(auto-fill,200px)]">
              {day.episodes.map((episode) => (
                <li key={`${episode.titleRef}-${episode.code}`}>
                  <EpisodeCard episode={episode} />
                </li>
              ))}
            </ul>
          </section>
        ))
      )}
    </div>
  );
}

function WeekStrip({
  week,
  today,
  horizon,
  selectedKey,
  counts,
  canPrev,
  canNext,
  onSelect,
  onToday,
  onShift,
}: {
  week: Date[];
  today: Date;
  horizon: Date;
  selectedKey: string;
  counts: Map<string, number>;
  canPrev: boolean;
  canNext: boolean;
  onSelect: (date: Date) => void;
  onToday: () => void;
  onShift: (delta: number) => void;
}) {
  return (
    <div className="mx-auto max-w-3xl">
      <div className="mb-2 flex items-center justify-center gap-2">
        <WeekButton label="上一周" disabled={!canPrev} onClick={() => onShift(-1)}>
          <ChevronLeftIcon className="size-4" />
        </WeekButton>
        <button
          type="button"
          onClick={onToday}
          className="rounded-full px-3 py-1 text-sub font-semibold text-[var(--text)] transition hover:bg-white/10"
        >
          今天
        </button>
        <WeekButton label="下一周" disabled={!canNext} onClick={() => onShift(1)}>
          <ChevronRightIcon className="size-4" />
        </WeekButton>
      </div>
      <div className="grid grid-cols-7 gap-1">
        {week.map((date) => {
          const key = iso(date);
          const inWindow = date >= today && date <= horizon;
          const selected = key === selectedKey;
          const count = counts.get(key) ?? 0;
          return (
            <button
              key={key}
              type="button"
              disabled={!inWindow}
              onClick={() => onSelect(date)}
              className={`flex flex-col items-center rounded-xl px-1 py-2 transition ${
                selected ? "bg-white/12" : "hover:bg-white/6"
              } disabled:cursor-default disabled:opacity-35 disabled:hover:bg-transparent`}
            >
              <span className="text-[11px] text-[var(--text-muted)]">{date.getMonth() + 1}月</span>
              <span className="text-xl font-semibold leading-tight text-[var(--text)]">
                {date.getDate()}
              </span>
              <span className="text-[11px] text-[var(--text-muted)]">{WEEKDAYS[date.getDay()]}</span>
              <CountMarks count={count} />
            </button>
          );
        })}
      </div>
    </div>
  );
}

function WeekButton({
  label,
  disabled,
  onClick,
  children,
}: {
  label: string;
  disabled: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="flex size-8 items-center justify-center rounded-full text-[var(--text)] transition hover:bg-white/10 disabled:cursor-default disabled:opacity-35"
    >
      {children}
    </button>
  );
}

function CountMarks({ count }: { count: number }) {
  if (count <= 0) return <span className="mt-1 h-3" />;
  const dots = Math.min(count, 2);
  const extra = count - dots;
  return (
    <span className="mt-1 flex h-3 items-center gap-0.5 text-[10px] leading-none text-[var(--accent)]">
      {"•".repeat(dots)}
      {extra > 0 ? <span>+{extra}</span> : null}
    </span>
  );
}

function EpisodeCard({ episode }: { episode: TraktCalendarEpisode }) {
  const id = episode.titleRef.split(":").at(-1) ?? "";
  const fallbackTitle = `第${episode.number}集`;
  const line =
    episode.episodeTitle && episode.episodeTitle !== fallbackTitle
      ? `第${episode.season}季 · 第${episode.number}集 · ${episode.episodeTitle}`
      : `第${episode.season}季 · 第${episode.number}集`;
  return (
    <Link href={buildDiscoveryReturnPath("trakt", "tv", id)} className="block min-w-0">
      <div className="relative aspect-video overflow-hidden rounded-lg bg-white/5">
        <PosterImage
          src={episode.landscapeUrl || null}
          alt={`${episode.showTitle} ${episode.episodeTitle}`}
          width={200}
          className="absolute inset-0 size-full object-cover"
        />
        {episode.airedTime ? (
          <span className="absolute bottom-2 left-2 rounded bg-black/75 px-1.5 py-0.5 text-[11px] font-medium text-white">
            {airPeriod(episode.airedTime)}
          </span>
        ) : null}
      </div>
      <p className="mt-2 truncate text-body font-semibold text-[var(--text)]">{episode.showTitle}</p>
      <p className="truncate text-sub text-[var(--text-muted)]">{line}</p>
    </Link>
  );
}

function CalendarNotice({ notice }: { notice: { code: string; message: string } }) {
  const message =
    notice.code === "trakt_not_connected" ? (
      <Link href={"/settings/profile" as Route} className="underline underline-offset-2">
        {notice.message}
      </Link>
    ) : (
      notice.message
    );
  return <p className="mt-8 text-body text-[var(--text-muted)]">{message}</p>;
}

function airPeriod(time: string): string {
  const [hourText, minute] = time.split(":");
  const hour = Number(hourText);
  if (!Number.isFinite(hour) || minute === undefined) return time;
  const period = hour < 12 ? "上午" : "下午";
  return `${period} ${hour % 12 || 12}:${minute}`;
}

function heading(isoDate: string): string {
  const [year, month, day] = isoDate.split("-");
  return `${year}年${Number(month)}月${Number(day)}日`;
}

function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function startOfWeek(date: Date): Date {
  const day = date.getDay();
  return addDays(date, day === 0 ? -6 : 1 - day);
}

function addDays(date: Date, days: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function iso(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}
