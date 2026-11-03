"use client";

import { useActionState } from "react";
import { btn, Notice } from "@/components/ui";
import { saveAgent, saveTenant, type FormState } from "./actions";
import type { AgentConfig } from "@/lib/types";

const initial: FormState = { error: null, saved: false };

const input =
  "w-full rounded-[10px] border border-separator bg-surface-2 px-3 py-2 text-[15px] text-label transition duration-150 placeholder:text-label-3 focus:border-tint focus:outline-none focus-visible:outline-none focus:ring-3 focus:ring-tint/25 disabled:text-label-2";

function Status({ state }: { state: FormState }) {
  if (state.error) return <Notice tone="bad">{state.error}</Notice>;
  if (state.saved) return <Notice tone="good">Saved. New calls use the updated settings.</Notice>;
  return null;
}

export function AgentForm({ agent, readOnly }: { agent: AgentConfig; readOnly: boolean }) {
  const [state, action, pending] = useActionState(saveAgent, initial);
  return (
    <form action={action} className="flex max-w-2xl flex-col gap-5">
      <Status state={state} />
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

export function TenantForm({ timezone, readOnly }: { timezone: string; readOnly: boolean }) {
  const [state, action, pending] = useActionState(saveTenant, initial);
  return (
    <form action={action} className="flex max-w-md flex-col gap-4">
      <Status state={state} />
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
