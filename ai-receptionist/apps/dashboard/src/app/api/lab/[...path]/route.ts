import { NextResponse, type NextRequest } from "next/server";
import { readSession } from "@/lib/session";

// The Test Lab page is interactive, so it calls the API from the browser —
// through here, so the access token stays in its httpOnly cookie. Only the
// API's /test-lab/* routes are reachable this way.
const BASE = process.env.API_BASE_URL ?? "http://localhost:8000";
const SEGMENT = /^[A-Za-z0-9_-]+$/;

async function forward(req: NextRequest, path: string[]) {
  const session = await readSession();
  if (!session) {
    return NextResponse.json({ error: { code: "authentication_required", message: "sign in" } }, { status: 401 });
  }
  if (path.length === 0 || !path.every((p) => SEGMENT.test(p))) {
    return NextResponse.json({ error: { code: "not_found", message: "no such lab route" } }, { status: 404 });
  }
  const body = req.method === "GET" || req.method === "DELETE" ? undefined : await req.text();
  const res = await fetch(`${BASE}/api/v1/test-lab/${path.join("/")}${req.nextUrl.search}`, {
    method: req.method,
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${session.token}`,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body,
  });
  return new NextResponse(await res.text(), {
    status: res.status,
    headers: { "Content-Type": res.headers.get("content-type") ?? "application/json", "Cache-Control": "no-store" },
  });
}

type Ctx = { params: Promise<{ path: string[] }> };
export async function GET(req: NextRequest, { params }: Ctx) {
  return forward(req, (await params).path);
}
export async function POST(req: NextRequest, { params }: Ctx) {
  return forward(req, (await params).path);
}
export async function DELETE(req: NextRequest, { params }: Ctx) {
  return forward(req, (await params).path);
}
