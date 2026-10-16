import { createAuth } from "./auth";
import { createDesignerMock } from "./designer";
import { route } from "./routes";

const port = Number(process.env.MOCK_PORT ?? 8787);
const origin = `http://127.0.0.1:${port}`;
const designer = createDesignerMock();
const auth = createAuth({
  now: () => new Date(),
  random: () => crypto.randomUUID().replaceAll("-", ""),
  origin,
});

Bun.serve({
  port,
  hostname: "127.0.0.1",
  async fetch(req) {
    const url = new URL(req.url);
    const cookie = req.headers.get("cookie");
    if (req.method === "POST" && url.pathname.startsWith("/api/auth/")) {
      let body: unknown = {};
      try {
        body = await req.json();
      } catch {
        // an empty or non-JSON body is validated by the handler
      }
      const reply = auth.handle(url.pathname, body, cookie);
      if (!reply)
        return Response.json({ error: `no such route: ${url.pathname}` }, { status: 404 });
      const headers = new Headers();
      if (reply.setCookie) headers.set("set-cookie", reply.setCookie);
      return Response.json(reply.body, { status: reply.status, headers });
    }
    if (
      url.pathname === "/api/strategy/validate" ||
      url.pathname === "/api/backtests/run" ||
      url.pathname.startsWith("/api/backtests/jobs/")
    ) {
      let body: unknown = null;
      if (req.method === "POST") {
        try {
          body = await req.json();
        } catch {
          return Response.json({
            errors: [{ path: "$", column: null, message: "the body is not valid JSON" }],
            ok: false,
          });
        }
      }
      const r = designer(req.method, url.pathname, body, req.headers);
      if (r) return Response.json(r.body, { status: r.status });
    }
    if (req.method !== "GET") {
      return Response.json(
        {
          error:
            "read-only API: only GET is supported (plus POST /api/auth/*, /api/strategy/validate, /api/backtests/run)",
        },
        { status: 405 },
      );
    }
    const { status, body, contentType, filename } = route(
      url.pathname,
      url.searchParams,
      auth.sessionAddress(cookie),
    );
    if (contentType) {
      return new Response(String(body), {
        status,
        headers: {
          "content-type": contentType,
          "content-disposition": `attachment; filename="${filename ?? "download"}"`,
        },
      });
    }
    return Response.json(body, { status });
  },
});

console.log(`mock API on ${origin}`);
