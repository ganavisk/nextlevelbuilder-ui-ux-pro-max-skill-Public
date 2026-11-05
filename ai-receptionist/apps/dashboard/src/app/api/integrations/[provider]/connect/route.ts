import { NextResponse, type NextRequest } from "next/server";
import { ApiError, apiFetch } from "@/lib/api";

// The API returns the provider's authorize URL as JSON because a browser
// navigation can't carry a Bearer token. This handler runs server-side with
// the session token, then performs the redirect from a first-party origin.
export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ provider: string }> },
) {
  const { provider } = await params;
  try {
    const { authorize_url } = await apiFetch<{ authorize_url: string }>(
      `/integrations/${encodeURIComponent(provider)}/connect`,
    );
    return NextResponse.redirect(authorize_url, 303);
  } catch (e) {
    const code = e instanceof ApiError ? e.code : "connect_failed";
    return NextResponse.redirect(new URL(`/settings?error=${encodeURIComponent(code)}`, req.url), 303);
  }
}
