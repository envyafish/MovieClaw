"use client";

import { useEffect, useState } from "react";

import { useToast } from "@/components/feedback";
import {
  SETTINGS_INPUT_CLASS,
  SETTINGS_PRIMARY_BUTTON_CLASS,
  SettingsCard,
} from "@/components/settings-ui";
import { request } from "@/lib/http";

interface TraktAppView {
  client_id: string;
  secret_set: boolean;
}

export function TraktSettingsSection() {
  const toast = useToast();
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [secretSet, setSecretSet] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    request<{ data: TraktAppView }>("/integrations/trakt/app")
      .then((body) => {
        if (cancelled) return;
        setClientId(body.data.client_id);
        setSecretSet(body.data.secret_set);
      })
      .catch((err: unknown) => {
        toast.error(err instanceof Error ? err.message : "读取 Trakt 应用失败");
      });
    return () => {
      cancelled = true;
    };
  }, [toast]);

  const save = async () => {
    setBusy(true);
    try {
      const body = await request<{ data: TraktAppView }>("/integrations/trakt/app", {
        method: "PUT",
        body: JSON.stringify({ client_id: clientId.trim(), client_secret: secret }),
      });
      setClientId(body.data.client_id);
      setSecret("");
      setSecretSet(body.data.secret_set);
      toast.success("已保存");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingsCard
      title="Trakt 应用"
      description={
        <>
          在{" "}
          <a
            href="https://trakt.tv/oauth/applications"
            target="_blank"
            rel="noreferrer"
            className="underline underline-offset-2"
          >
            Trakt 创建应用
          </a>
          。Redirect URI 填 http://localhost。每个成员再各自连接自己的账号。
        </>
      }
      hint={secretSet ? "Client Secret 已设置。留空表示不修改。" : "Client Secret 留空表示不修改。"}
      action={
        <button
          type="button"
          className={SETTINGS_PRIMARY_BUTTON_CLASS}
          disabled={busy || !clientId.trim()}
          onClick={() => void save()}
        >
          {busy ? "保存中" : "保存"}
        </button>
      }
    >
      <div className="space-y-3">
        <label className="block text-sub text-[var(--text-muted)]">
          Client ID
          <input
            value={clientId}
            onChange={(event) => setClientId(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            className={`${SETTINGS_INPUT_CLASS} mt-1 w-full font-mono`}
          />
        </label>
        <label className="block text-sub text-[var(--text-muted)]">
          Client Secret
          <input
            type="password"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
            autoComplete="new-password"
            placeholder={secretSet ? "已设置" : ""}
            className={`${SETTINGS_INPUT_CLASS} mt-1 w-full font-mono`}
          />
        </label>
        {secretSet && <p className="text-sub text-[var(--text-muted)]">已设置</p>}
      </div>
    </SettingsCard>
  );
}
