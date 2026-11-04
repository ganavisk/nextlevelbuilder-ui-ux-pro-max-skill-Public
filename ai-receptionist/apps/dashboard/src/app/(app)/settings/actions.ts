"use server";

import { revalidatePath } from "next/cache";
import { ApiError, apiFetch } from "@/lib/api";
import type { AgentConfigUpdate } from "@/lib/types";

export interface FormState {
  error: string | null;
  saved: boolean;
}

function fail(e: unknown): FormState {
  if (e instanceof ApiError) {
    return {
      error: e.status === 403 ? "Your role can view settings but not change them." : e.message,
      saved: false,
    };
  }
  throw e;
}

export async function saveAgent(_prev: FormState, formData: FormData): Promise<FormState> {
  const body: AgentConfigUpdate = {
    system_prompt: String(formData.get("system_prompt") ?? ""),
    first_message: String(formData.get("first_message") ?? ""),
    voice_id: String(formData.get("voice_id") ?? "").trim(),
  };
  if (!body.system_prompt?.trim() || !body.first_message?.trim() || !body.voice_id) {
    return { error: "Prompt, greeting and voice ID can't be empty.", saved: false };
  }
  try {
    await apiFetch("/tenants/me/agent-config", { method: "PATCH", body: JSON.stringify(body) });
  } catch (e) {
    return fail(e);
  }
  revalidatePath("/settings");
  return { error: null, saved: true };
}

export async function saveTenant(_prev: FormState, formData: FormData): Promise<FormState> {
  const timezone = String(formData.get("timezone") ?? "").trim();
  if (!timezone) return { error: "Timezone can't be empty.", saved: false };
  try {
    await apiFetch("/tenants/me", { method: "PATCH", body: JSON.stringify({ timezone }) });
  } catch (e) {
    return fail(e);
  }
  revalidatePath("/settings");
  return { error: null, saved: true };
}

export async function retryVoiceSync(): Promise<FormState> {
  try {
    await apiFetch("/tenants/me/voice-sync", { method: "POST" });
  } catch (e) {
    return fail(e);
  }
  revalidatePath("/settings");
  return { error: null, saved: true };
}

export async function saveCallbackPromises(_prev: FormState, formData: FormData): Promise<FormState> {
  const callback_promise = String(formData.get("callback_promise") ?? "").trim();
  const after_hours_promise = String(formData.get("after_hours_promise") ?? "").trim();
  if (!callback_promise) return { error: "The callback promise can't be empty.", saved: false };
  try {
    await apiFetch("/tenants/me/callback-promises", {
      method: "PUT",
      body: JSON.stringify({ callback_promise, after_hours_promise }),
    });
  } catch (e) {
    return fail(e);
  }
  revalidatePath("/settings");
  return { error: null, saved: true };
}

export async function saveCallbackTriggers(_prev: FormState, formData: FormData): Promise<FormState> {
  const triggers = String(formData.get("triggers") ?? "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  try {
    await apiFetch("/tenants/me/callback-triggers", { method: "PUT", body: JSON.stringify({ triggers }) });
  } catch (e) {
    return fail(e);
  }
  revalidatePath("/settings");
  return { error: null, saved: true };
}
