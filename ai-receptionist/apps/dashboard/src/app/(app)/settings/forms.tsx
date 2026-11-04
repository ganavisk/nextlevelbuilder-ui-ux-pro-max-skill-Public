"use client";

import { useActionState, useRef, useState } from "react";
import { btn, Notice } from "@/components/ui";
import { saveAgent, saveCallbackPromises, saveTenant, type FormState } from "./actions";
import type { AgentConfig, VoiceSync } from "@/lib/types";

const initial: FormState = { error: null, saved: false };

const input =
  "w-full rounded-[10px] border border-separator bg-surface-2 px-3 py-2 text-[15px] text-label transition duration-150 placeholder:text-label-3 focus:border-tint focus:outline-none focus-visible:outline-none focus:ring-3 focus:ring-tint/25 disabled:text-label-2";

/** Saved is not the same as live on calls: the change is pushed to the phone
 * agent in the background. The page re-renders while that runs (see
 * voice-sync.tsx), so this notice moves from "sending" to "live" in place, next
 * to the form the user just saved, wherever they've scrolled to. */
function Status({ state, sync }: { state: FormState; sync: VoiceSync }) {
  if (state.error) return <Notice tone="bad">{state.error}</Notice>;
  if (!state.saved) return null;
  switch (sync.state) {
    case "updating":
      return <Notice tone="info">Saved. Sending it to the phone agent...</Notice>;
    case "failed":
      return (
        <Notice tone="bad">
          Saved, but the phone agent wasn&apos;t updated, so callers still hear the previous version.
          Use Try again under Integrations.
        </Notice>
      );
    case "not_connected":
      return <Notice tone="good">Saved. It goes live when a phone number is connected.</Notice>;
    default:
      return <Notice tone="good">Saved and live: new calls use it.</Notice>;
  }
}

export function AgentForm({
  agent,
  sync,
  readOnly,
}: {
  agent: AgentConfig;
  sync: VoiceSync;
  readOnly: boolean;
}) {
  const [state, action, pending] = useActionState(saveAgent, initial);
  return (
    <form action={action} className="flex max-w-2xl flex-col gap-5">
      <Status state={state} sync={sync} />
      <div className="flex flex-col gap-2">
        <label htmlFor="first_message" className="text-[13px] font-medium text-label-2">
          Greeting
        </label>
        <input
          id="first_message"
          name="first_message"
          defaultValue={agent.first_message}
          disabled={readOnly}
          className={input}
        />
        <p className="text-[13px] text-label-2">The first thing callers hear. Keep it under two sentences.</p>
      </div>
      <div className="flex flex-col gap-2">
        <label htmlFor="system_prompt" className="text-[13px] font-medium text-label-2">
          Instructions
        </label>
        <textarea
          id="system_prompt"
          name="system_prompt"
          rows={12}
          defaultValue={agent.system_prompt}
          disabled={readOnly}
          className={`${input} py-2 text-[15px] leading-[22px]`}
        />
        <p className="text-[13px] text-label-2">
          How the receptionist behaves. Saving creates version {agent.version + 1}; the current
          version stays on file.
        </p>
      </div>
      <div className="flex flex-col gap-2">
        <label htmlFor="voice_id" className="text-[13px] font-medium text-label-2">
          Voice ID
        </label>
        <input
          id="voice_id"
          name="voice_id"
          defaultValue={agent.voice_id}
          disabled={readOnly}
          className={`${input} font-mono`}
        />
        <p className="text-[13px] text-label-2">
          {agent.voice_provider} / {agent.voice_model}, language {agent.language}.
        </p>
      </div>
      {!readOnly && (
        <button type="submit" disabled={pending} className={`${btn.base} ${btn.primary} self-start`}>
          {pending ? "Saving..." : "Save changes"}
        </button>
      )}
    </form>
  );
}

export function TenantForm({
  timezone,
  sync,
  readOnly,
}: {
  timezone: string;
  sync: VoiceSync;
  readOnly: boolean;
}) {
  const [state, action, pending] = useActionState(saveTenant, initial);
  return (
    <form action={action} className="flex max-w-md flex-col gap-4">
      <Status state={state} sync={sync} />
      <div className="flex flex-col gap-2">
        <label htmlFor="timezone" className="text-[13px] font-medium text-label-2">
          Timezone
        </label>
        <input
          id="timezone"
          name="timezone"
          defaultValue={timezone}
          disabled={readOnly}
          className={`${input} font-mono`}
        />
        <p className="text-[13px] text-label-2">IANA name, such as America/Chicago. Used for booking times.</p>
      </div>
      {!readOnly && (
        <button type="submit" disabled={pending} className={`${btn.base} ${btn.primary} self-start`}>
          {pending ? "Saving..." : "Save timezone"}
        </button>
      )}
    </form>
  );
}

const NEXT_OPEN = "{next_open}";

/** What callers are told after asking for a call back, during and outside
 * opening hours. Read by the callback tool on every call, so a save needs no
 * push to the phone agent: the next call uses it. */
export function CallbackPromisesForm({
  policy,
  nextOpenExample,
  readOnly,
}: {
  policy: Record<string, unknown>;
  nextOpenExample: string | null;
  readOnly: boolean;
}) {
  const [state, action, pending] = useActionState(saveCallbackPromises, initial);
  const [open, setOpen] = useState(String(policy.callback_promise ?? ""));
  const [closed, setClosed] = useState(String(policy.after_hours_promise ?? ""));
  const closedRef = useRef<HTMLTextAreaElement>(null);
  const example = nextOpenExample ?? "when you next open";

  const insertOpening = () => {
    const el = closedRef.current;
    const at = el?.selectionStart ?? closed.length;
    const next = closed.slice(0, at) + NEXT_OPEN + closed.slice(el?.selectionEnd ?? at);
    setClosed(next);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(at + NEXT_OPEN.length, at + NEXT_OPEN.length);
    });
  };

  return (
    <form action={action} className="flex max-w-2xl flex-col gap-5">
      {state.error ? (
        <Notice tone="bad">{state.error}</Notice>
      ) : state.saved ? (
        <Notice tone="good">Saved. The next call uses it.</Notice>
      ) : null}
      <div className="flex flex-col gap-2">
        <label htmlFor="callback_promise" className="text-[13px] font-medium text-label-2">
          While you&apos;re open
        </label>
        <textarea
          id="callback_promise"
          name="callback_promise"
          rows={2}
          value={open}
          onChange={(e) => setOpen(e.target.value)}
          disabled={readOnly}
          maxLength={300}
          className={`${input} resize-y`}
        />
        <p className="text-[13px] text-label-2">
          What callers hear after asking for a call back during opening hours.
        </p>
      </div>
      <div className="flex flex-col gap-2">
        <div className="flex items-end justify-between gap-3">
          <label htmlFor="after_hours_promise" className="text-[13px] font-medium text-label-2">
            After hours <span className="font-normal">(optional)</span>
          </label>
          {!readOnly && (
            <button type="button" onClick={insertOpening} className={`${btn.base} ${btn.plain} h-7 px-2 text-[13px]`}>
              Insert opening time
            </button>
          )}
        </div>
        <textarea
          ref={closedRef}
          id="after_hours_promise"
          name="after_hours_promise"
          rows={2}
          value={closed}
          onChange={(e) => setClosed(e.target.value)}
          disabled={readOnly}
          maxLength={300}
          placeholder="e.g. We're closed right now, so we'll call you back when we open, {next_open}."
          className={`${input} resize-y`}
        />
        <p className="text-[13px] text-label-2">
          Used while you&apos;re closed. <span className="font-mono">{NEXT_OPEN}</span> becomes when you
          next open, worked out from your hours. Leave empty to use the open-hours promise at all times.
        </p>
      </div>
      <div className="rounded-[10px] bg-surface-2 px-3 py-2.5 text-[14px]" aria-live="polite">
        <p className="text-[12px] font-medium uppercase tracking-wide text-label-2">Callers hear</p>
        <p className="mt-1">
          <span className="text-label-2">Open: </span>
          {open.trim() || <span className="text-label-3">nothing yet</span>}
        </p>
        <p className="mt-1">
          <span className="text-label-2">Closed: </span>
          {closed.trim() ? closed.trim().split(NEXT_OPEN).join(example) : open.trim() || "—"}
        </p>
      </div>
      {!readOnly && (
        <button type="submit" disabled={pending} className={`${btn.base} ${btn.primary} self-start`}>
          {pending ? "Saving..." : "Save callback promise"}
        </button>
      )}
    </form>
  );
}
