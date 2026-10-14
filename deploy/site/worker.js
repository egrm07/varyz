// varyzai.com: the static site, with Cloud prices shown in the visitor's own currency.
//
// Only the pages listed under assets.run_worker_first in wrangler.jsonc reach this script;
// every other file is served straight from website/. Pages ship the US dollar price in
// elements marked data-price (see PRICE_MARKS in scripts/site/build_site.py). Cloudflare
// tells us the visitor's country; the Cloud service asks Paddle what that country pays and
// we swap the text in. Only the two-letter country code leaves this Worker. Any failure
// serves the page unchanged, so the dollar price is the fallback, never an empty price.

const PRICING = "https://cloud.varyzai.com/api/pricing?country=";
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
    // Search Console wants its ownership file at the exact .html address, not the clean
    // URL the assets layer redirects to, so serve it straight from the clean path.
    const { pathname } = new URL(request.url);
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
    if (request.method !== "GET" || !page.ok || !html || !/^[A-Z]{2}$/.test(country)) {
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
  },
};
