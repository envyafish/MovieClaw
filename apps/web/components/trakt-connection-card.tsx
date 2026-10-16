"use client";

import type { Route } from "next";
import Link from "next/link";
import { useEffect, useState } from "react";

import { useToast } from "@/components/feedback";
import { SETTINGS_BUTTON_CLASS, SettingsSection } from "@/components/settings-ui";
import { request } from "@/lib/http";
import { useSession } from "@/lib/session";
import { connectionLabel, type TraktConnectionSnapshot } from "@/lib/trakt-connection";

interface TraktConnectionView extends TraktConnectionSnapshot {
  user_code?: string | null;
  verification_url?: string | null;
  expires_in?: number | null;
}

const POLL_MS = 5000;

async function call(method: "GET" | "POST" | "DELETE", path: string) {
  const body = await request<{ data: TraktConnectionView }>(path, method === "GET" ? undefined : { method });
  return body.data;
}

export function TraktConnectionCard() {
  const toast = useToast();
  const { session } = useSession();
  const [view, setView] = useState<TraktConnectionView | null>(null);
  const [left, setLeft] = useState(0);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    call("GET", "/integrations/trakt/connection")
      .then((next) => {
        if (!cancelled) setView(next);
      })
      .catch((err: unknown) => {
        toast.error(err instanceof Error ? err.message : "读取 Trakt 连接失败");
      });
    return () => {
      cancelled = true;
    };
  }, [toast]);

  useEffect(() => {
    if (view?.status !== "pending") return;
    let stopped = false;
    const timer = window.setInterval(() => {
      void call("POST", "/integrations/trakt/connection/poll")
        .then((next) => {
          if (!stopped) setView(next);
        })
        .catch(() => {});
    }, POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [view?.status]);

  useEffect(() => {
    setLeft(view?.expires_in ?? 0);
  }, [view?.expires_in, view?.user_code]);

  useEffect(() => {
    if (view?.status !== "pending") return;
    const timer = window.setInterval(() => setLeft((seconds) => Math.max(0, seconds - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [view?.status, view?.user_code]);

  const run = async (method: "POST" | "DELETE", path: string) => {
    setBusy(true);
    try {
      setView(await call(method, path));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  if (!view) return null;
  const label = connectionLabel(view);
  const message = view.message && view.message !== label ? view.message : "";

  return (
    <SettingsSection title="Trakt">
      <div className="css-glass space-y-3 !rounded-xl px-5 py-4">
        {label && <p className="text-ui text-[var(--text)]">{label}</p>}
        {!view.app_configured && session.role !== "member" && (
          <Link href={"/settings/trakt" as Route} className="text-sub text-[var(--accent-2)] underline">
            去配置 Trakt 应用
          </Link>
        )}
        {view.status === "pending" && view.user_code && (
          <div className="space-y-1">
            <p className="font-mono text-title-lg tracking-widest text-[var(--text)]">{view.user_code}</p>
            {view.verification_url && (
              <a
                href={view.verification_url}
                target="_blank"
                rel="noreferrer"
                className="text-sub text-[var(--accent-2)] underline"
              >
                打开 Trakt 激活
              </a>
            )}
            <p className="text-sub text-[var(--text-muted)]">剩余 {left} 秒</p>
          </div>
        )}
        {message && <p className="text-sub text-[var(--text-muted)]">{message}</p>}
        {view.app_configured && view.status === "disconnected" && (
          <button
            type="button"
            className={SETTINGS_BUTTON_CLASS}
            disabled={busy}
            onClick={() => void run("POST", "/integrations/trakt/connection")}
          >
            连接 Trakt
          </button>
        )}
        {view.status === "connected" && (
          <button
            type="button"
            className={SETTINGS_BUTTON_CLASS}
            disabled={busy}
            onClick={() => void run("DELETE", "/integrations/trakt/connection")}
          >
            断开
          </button>
        )}
        {view.app_configured && view.status === "expired" && (
          <button
            type="button"
            className={SETTINGS_BUTTON_CLASS}
            disabled={busy}
            onClick={() => void run("POST", "/integrations/trakt/connection")}
          >
            重新连接
          </button>
        )}
      </div>
    </SettingsSection>
  );
}
