"use server";

import { revalidatePath } from "next/cache";
import { ApiError, apiFetch } from "@/lib/api";

export async function setCallbackStatus(
  id: string,
  status: "open" | "contacted" | "resolved",
): Promise<{ error: string | null }> {
  try {
    await apiFetch(`/callback-requests/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
  } catch (e) {
    if (e instanceof ApiError) return { error: e.message };
    throw e;
  }
  revalidatePath("/callbacks");
  return { error: null };
}
