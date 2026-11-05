import "server-only";
import { redirect } from "next/navigation";
import { readSession } from "./session";

const BASE = process.env.API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

async function parseError(res: Response): Promise<ApiError> {
  try {
    const body = (await res.json()) as { error?: { code?: string; message?: string } };
    return new ApiError(
      res.status,
      body.error?.code ?? "unknown_error",
      body.error?.message ?? res.statusText,
    );
  } catch {
    return new ApiError(res.status, "unknown_error", res.statusText);
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const session = await readSession();
  if (!session) redirect("/login");

  const res = await fetch(`${BASE}/api/v1${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
      Authorization: `Bearer ${session.token}`,
    },
  });

  // An expired or revoked token: the cookie still exists, so middleware
  // would bounce /login straight back to /. Clear it via a route handler
  // (Server Components can't write cookies) before landing on /login.
  if (res.status === 401) redirect("/api/auth/logout");
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as T;
}

// `clientIp` is the browser's address. This server is the API's only direct
// caller, so without forwarding it the API would see every user's login as
// coming from one IP and its per-IP throttle would punish everyone together.
export async function apiLogin<T>(body: unknown, clientIp?: string): Promise<T> {
  const res = await fetch(`${BASE}/api/v1/auth/login`, {
    method: "POST",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...(clientIp ? { "X-Forwarded-For": clientIp } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as T;
}

export function query(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}
