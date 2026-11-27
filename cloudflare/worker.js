/**
 * MEDIABOX gateway — Cloudflare Workers
 * Fixed upstream only. This is NOT an open proxy.
 * Deploy as a Worker with a workers.dev subdomain enabled.
 */
const UPSTREAM_ORIGIN = "https://mediabox-android.onrender.com";

export default {
  async fetch(request) {
    const incoming = new URL(request.url);
    if (request.method === "TRACE" || request.method === "CONNECT") {
      return new Response("Method not allowed", { status: 405 });
    }

    const upstream = new URL(UPSTREAM_ORIGIN);
    upstream.pathname = incoming.pathname;
    upstream.search = incoming.search;

    const headers = new Headers(request.headers);
    headers.delete("host");
    headers.set("x-forwarded-host", incoming.host);
    headers.set("x-forwarded-proto", "https");

    const upstreamRequest = new Request(upstream.toString(), {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "manual"
    });

    let response;
    try {
      response = await fetch(upstreamRequest, { redirect: "manual", cache: "no-store" });
    } catch (_) {
      return new Response("MEDIABOX indisponível no momento.", {
        status: 502,
        headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" }
      });
    }

    const responseHeaders = new Headers(response.headers);
    responseHeaders.set("cache-control", "no-store, no-cache, must-revalidate");
    responseHeaders.delete("server");

    const location = responseHeaders.get("location");
    if (location) {
      try {
        const redirectUrl = new URL(location, UPSTREAM_ORIGIN);
        if (redirectUrl.hostname === upstream.hostname) {
          redirectUrl.protocol = "https:";
          redirectUrl.host = incoming.host;
          responseHeaders.set("location", redirectUrl.toString());
        }
      } catch (_) {}
    }

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders
    });
  }
};
