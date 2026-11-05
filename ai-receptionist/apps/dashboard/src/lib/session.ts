import "server-only";
import { cookies } from "next/headers";
import type { Role } from "./types";

export const SESSION_COOKIE = "rx_session";
const MAX_AGE_SECONDS = 60 * 60; // matches the API's access_token_ttl_minutes default

export interface Session {
  token: string;
  tenantName: string;
  userName: string;
  role: Role;
}

export async function readSession(): Promise<Session | null> {
  const raw = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Session;
    return parsed.token ? parsed : null;
  } catch {
    return null;
  }
}

export async function writeSession(session: Session): Promise<void> {
  (await cookies()).set(SESSION_COOKIE, JSON.stringify(session), {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: MAX_AGE_SECONDS,
  });
}
