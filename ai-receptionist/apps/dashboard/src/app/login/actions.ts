"use server";

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { ApiError, apiLogin } from "@/lib/api";
import { writeSession } from "@/lib/session";
import type { LoginResponse } from "@/lib/types";

export interface LoginState {
  error: string | null;
  // React 19 resets uncontrolled inputs after a form action; echo the
  // non-secret values back so a typo in the password doesn't clear them.
  tenant_slug: string;
  email: string;
}

export async function login(_prev: LoginState, formData: FormData): Promise<LoginState> {
  const tenant_slug = String(formData.get("tenant_slug") ?? "").trim();
  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");
  if (!tenant_slug || !email || !password) {
    return { error: "Enter your business ID, email and password.", tenant_slug, email };
  }

  const h = await headers();
  const clientIp = h.get("x-forwarded-for")?.split(",")[0]?.trim() || h.get("x-real-ip") || undefined;

  let result: LoginResponse;
  try {
    result = await apiLogin<LoginResponse>({ tenant_slug, email, password }, clientIp);
  } catch (e) {
    if (e instanceof ApiError && e.status === 429) {
      return {
        error: "Too many sign-in attempts. Wait a few minutes, then try again.",
        tenant_slug,
        email,
      };
    }
    if (e instanceof ApiError && e.status === 401) {
      return {
        error: "Those details don't match an account. Check them and try again.",
        tenant_slug,
        email,
      };
    }
    return {
      error: "The service is unreachable right now. Try again in a minute.",
      tenant_slug,
      email,
    };
  }

  await writeSession({
    token: result.access_token,
    tenantName: result.tenant.name,
    userName: result.user.display_name,
    role: result.user.role,
  });
  redirect("/");
}
