"use server";

import { revalidatePath } from "next/cache";
import { ApiError, apiFetch } from "@/lib/api";

export async function cancelAppointment(id: string): Promise<{ error: string | null }> {
  try {
    await apiFetch(`/appointments/${encodeURIComponent(id)}/cancel`, { method: "POST" });
  } catch (e) {
    if (e instanceof ApiError) return { error: e.message };
    throw e;
  }
  revalidatePath("/appointments");
  return { error: null };
}
