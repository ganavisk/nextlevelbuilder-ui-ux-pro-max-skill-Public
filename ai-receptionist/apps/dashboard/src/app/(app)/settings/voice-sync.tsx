"use client";

import { useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { ArrowClockwise } from "@phosphor-icons/react";
import { Badge, btn } from "@/components/ui";
import type { VoiceSync } from "@/lib/types";
import { retryVoiceSync } from "./actions";

/** How long to keep re-checking an update before leaving it to a reload. A
 * push normally lands in seconds, and the job queue's five attempts are spent
 * within about 20 s; two minutes also covers a worker that was restarting. */
const POLL_MS = 3000;
const POLL_FOR_MS = 120_000;

const DESCRIPTION: Record<VoiceSync["state"], string> = {
  live: "Callers hear your current settings.",
  updating: "Sending your latest changes to the phone agent. This usually takes a few seconds.",
  failed: "Callers still hear the previous version.",
  not_connected:
    "No phone number is connected yet. Your settings are saved and go live when the number is connected.",
};

const LABEL: Record<VoiceSync["state"], string> = {
  live: "live",
  updating: "updating",
  failed: "not updated",
  not_connected: "not connected",
};

const TONE = {
  live: "good",
  updating: "info",
  failed: "warning",
  not_connected: "neutral",
} as const satisfies Record<VoiceSync["state"], string>;

function useRefreshWhileUpdating(updating: boolean) {
  const router = useRouter();
  useEffect(() => {
    if (!updating) return;
    const started = Date.now();
    const timer = setInterval(() => {
      if (Date.now() - started > POLL_FOR_MS) return clearInterval(timer);
      // Re-renders the server component with fresh status; form inputs keep
      // their values because only changed props flow down.
      if (document.visibilityState === "visible") router.refresh();
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [updating, router]);
}

export function VoiceSyncRow({
  sync,
  canRetry,
  timeZone,
}: {
  sync: VoiceSync;
  canRetry: boolean;
  timeZone: string;
}) {
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);
  useRefreshWhileUpdating(sync.state === "updating");

  const updated =
    sync.synced_at &&
    new Intl.DateTimeFormat("en-US", {
      timeZone,
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    }).format(new Date(sync.synced_at));

  return (
    <div className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0" aria-live="polite">
        <div className="flex items-center gap-3">
          <span className="text-[15px]">Phone agent</span>
          <Badge value={LABEL[sync.state]} tone={TONE[sync.state]} />
        </div>
        <p className="mt-1 text-[13px] text-label-2">
          {DESCRIPTION[sync.state]}
          {sync.state === "failed" && sync.error && <> {sync.error}</>}
          {updated && sync.state !== "not_connected" && <> Last updated {updated}.</>}
        </p>
        {error && <p className="mt-1 text-[13px] text-critical">{error}</p>}
      </div>
      {sync.state === "failed" && canRetry && (
        <button
          type="button"
          disabled={pending}
          onClick={() =>
            start(async () => {
              const result = await retryVoiceSync();
              setError(result.error);
            })
          }
          className={`${btn.base} ${btn.tinted} self-start sm:self-auto`}
        >
          <ArrowClockwise size={14} weight="bold" aria-hidden />
          {pending ? "Retrying..." : "Try again"}
        </button>
      )}
    </div>
  );
}
