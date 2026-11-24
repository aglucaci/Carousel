/**
 * Same-origin proxy to the CAROUSEL API (CAROUSEL_API_URL, default http://127.0.0.1:8000).
 *
 * The browser never sees CAROUSEL_API_TOKEN: it is added here, server side, to
 * writes. Anyone who can reach this web app can therefore write through it, so
 * put the web app behind your own access control when it is not local.
 */
import type { NextRequest } from "next/server";

const API = (process.env.CAROUSEL_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");

async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await ctx.params;
  const url = `${API}/api/v1/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers: Record<string, string> = { accept: "application/json" };
  const token = process.env.CAROUSEL_API_TOKEN;
  if (req.method !== "GET" && token) headers.authorization = `Bearer ${token}`;
  let body: string | undefined;
  if (req.method !== "GET") {
    body = await req.text();
    headers["content-type"] = "application/json";
  }
  try {
    const res = await fetch(url, { method: req.method, headers, body, cache: "no-store" });
    return new Response(await res.text(), {
      status: res.status,
      headers: { "content-type": res.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return Response.json({ detail: `The CAROUSEL API is not reachable at ${API}.` }, { status: 502 });
  }
}

export const GET = forward;
export const POST = forward;
export const PATCH = forward;
export const dynamic = "force-dynamic";
