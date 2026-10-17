// varyzai.com: static site, localized prices and server-side aggregate counters.
import { metric, collect } from "./aggregate.js";
//
// Only the pages listed under assets.run_worker_first in wrangler.jsonc reach this script;
// every other file is served straight from website/. Pages ship the US dollar price in
// elements marked data-price (see PRICE_MARKS in scripts/site/build_site.py). Cloudflare
// tells us the visitor's country; the Cloud service asks Paddle what that country pays and
// we swap the text in. Only the two-letter country code leaves this Worker. Any failure
// serves the page unchanged, so the dollar price is the fallback, never an empty price.

const CLOUD = "https://cloud.varyzai.com";
const PRICING = CLOUD + "/api/pricing?country=";
const CACHE_SECONDS = 3600;

async function localPrices(country, ctx) {
  const cache = caches.default;
  const key = new Request("https://varyzai.com/__local-price/" + country);
  const hit = await cache.match(key);
  if (hit) return hit.json();
  let found = null;
  try {
    const answer = await fetch(PRICING + country, { signal: AbortSignal.timeout(2500) });
    if (answer.ok) {
      found = await answer.json();
    } else if (answer.status !== 404) {
      return null; // A passing fault: try again on the next visit.
    }
  } catch {
    return null;
  }
  const value = found && found.monthly && found.annual && found.currency ? found : {};
  ctx.waitUntil(
    cache.put(
      key,
      new Response(JSON.stringify(value), {
        headers: { "Content-Type": "application/json", "Cache-Control": `max-age=${CACHE_SECONDS}` },
      }),
    ),
  );
  return value;
}

export default {
  async fetch(request, env, ctx) {
    const started = performance.now();
    let response;
    try { response = await serve(request, env, ctx); }
    catch { response = new Response("Temporarily unavailable", {status:503, headers:{"Content-Type":"text/html; charset=utf-8"}}); }
    const value = metric(request, response, performance.now() - started);
    if (value?.kind === "probe") {
      const status = await collect(value, env.AGGREGATE_COLLECT_TOKEN);
      const probeResponse = new Response(response.body, response);
      probeResponse.headers.set("X-Varyz-Aggregate-Delivery", String(status));
      return probeResponse;
    }
    if (value) ctx.waitUntil(collect(value, env.AGGREGATE_COLLECT_TOKEN));
    return response;
  },
};

async function serve(request, env, ctx) {
    // Search Console wants its ownership file at the exact .html address, not the clean
    // URL the assets layer redirects to, so serve it straight from the clean path.
    const { pathname, search } = new URL(request.url);
    // The admin panel and creator referral links live on Varyz Cloud, where the account
    // session is. Send them there with the same path; nothing about the visitor is added.
    if (pathname === "/admin" || pathname.startsWith("/admin/")) {
      return Response.redirect(CLOUD + pathname + search, 302);
    }
    if (["/login", "/account", "/dashboard"].includes(pathname)) {
      const target = pathname === "/login" ? "/login" : "/?view=dashboard";
      return Response.redirect(CLOUD + target, 302);
    }
    const referral = /^\/r\/([A-Za-z0-9_]{3,30})\/?$/.exec(pathname);
    if (referral) {
      return Response.redirect(CLOUD + "/r/" + referral[1].toLowerCase(), 302);
    }
    // The zone's URL Rewrite Rule "Varyz discovery files through Worker" sends /llms.txt,
    // /llms-full.txt and /sitemap.xml here as /google-discovery-<file>.html (the /google*.html
    // entry in run_worker_first), so answer with the real file and its own content type.
    const discovery = /^\/google-discovery-(llms\.txt|llms-full\.txt|sitemap\.xml)\.html$/.exec(pathname);
    if (discovery) {
      const file = await env.ASSETS.fetch(new URL("/" + discovery[1], request.url));
      if (!file.ok) return file;
      const type = discovery[1].endsWith(".xml") ? "application/xml" : "text/plain";
      const response = new Response(file.body, file);
      response.headers.set("Content-Type", type + "; charset=utf-8");
      return response;
    }
    if (/^\/google[0-9a-f]+\.html$/.test(pathname)) {
      const file = await env.ASSETS.fetch(new URL(pathname.slice(0, -5), request.url));
      return new Response(file.ok ? await file.text() : "Not found", {
        status: file.ok ? 200 : 404,
        headers: { "Content-Type": "text/html; charset=utf-8" },
      });
    }
    const page = await env.ASSETS.fetch(request);
    const country = (request.cf && request.cf.country) || "";
    const html = (page.headers.get("Content-Type") || "").startsWith("text/html");
    if (request.method !== "GET" || !page.ok || !html || !/^[A-Z]{2}$/.test(country) || !["/", "/index.html", "/pricing", "/pricing.html"].includes(pathname)) {
      return page;
    }
    const local = await localPrices(country, ctx);
    if (!local || !local.currency || local.currency === "USD") return page;
    const text = {
      monthly: local.monthly,
      annual: local.annual,
      note: `Shown in ${local.currency} with tax for your location. Paddle confirms the total before payment.`,
    };
    const localized = new HTMLRewriter()
      .on("[data-price]", {
        element(element) {
          const value = text[element.getAttribute("data-price")];
          if (!value) return;
          element.setInnerContent(value);
          if (element.tagName === "data") element.setAttribute("value", value);
        },
      })
      .transform(page);
    const response = new Response(localized.body, localized);
    // The page now differs by country: keep shared caches from serving it to anyone else.
    response.headers.set("Cache-Control", "private, no-cache");
    return response;
}
