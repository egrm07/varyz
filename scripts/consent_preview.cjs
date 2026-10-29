// Exercise the real template using synthetic API responses. Never starts setup.
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const { chromium } = require(process.argv[3]);
const out = path.resolve(process.argv[2]);
const fonts = JSON.parse(fs.readFileSync(path.join(out, 'fonts.json'), 'utf8'));
const html = fs.readFileSync(path.join(out, 'setup.html'), 'utf8');
const plan = { config: 'Synthetic archive', analytics: { sharing: false },
  ai: { selected: 'conversations', cloud_conversations: true, choices: ['conversations'] },
  agents: [{ id: 'codex', name: 'Codex', installed: true }],
  sources: [{ id: 'ai_conversations', name: 'AI chats', checked: true, lines: ['Approved conversations only'] }] };
(async () => {
  const browser = await chromium.launch({ headless: true,
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  try {
    for (const [name, width, height] of [['desktop', 1440, 900], ['mobile', 390, 844]]) {
      const page = await browser.newPage({ viewport: { width, height } });
      const errors = [], runs = [];
      let currentSharing = false;
      page.on('pageerror', error => errors.push(error.message));
      await page.route('https://consent.test/**', async route => {
        const url = new URL(route.request().url());
        if (url.pathname.startsWith('/fonts/')) return route.fulfill({
          body: fs.readFileSync(fonts[url.pathname.split('/').pop()]),
          contentType: url.pathname.endsWith('.woff2') ? 'font/woff2' : 'font/ttf' });
        if (url.pathname === '/') return route.fulfill({ body: html, contentType: 'text/html' });
        if (url.pathname === '/api/plan') return route.fulfill({ json: {
          ...plan, analytics: { sharing: currentSharing } } });
        if (url.pathname === '/api/state') return route.fulfill({ json: { started: false } });
        if (url.pathname === '/api/run') {
          runs.push(route.request().postDataJSON());
          return route.fulfill({ status: 409, json: { error: 'Preview only. No installation runs.' } });
        }
        return route.fulfill({ json: { ok: true } });
      });
      async function open() {
        await page.goto('https://consent.test/?t=synthetic-consent-preview');
        await page.locator('input[name="agent"]').waitFor({ state: 'attached' });
        await page.evaluate(() => document.fonts.ready);
        assert(await page.locator('#consent').isVisible());
        assert(await page.locator('#consent').evaluate(el => el.matches(':modal')));
        assert.equal(await page.locator('#consent-title').innerText(), 'Consent');
      }
      async function finishChoice(share) {
        await page.locator(share ? '#consent-accept' : '#consent-close').click();
        assert(await page.locator('#ai-setup').isVisible());
        await page.locator('#ai-continue').click();
        await page.locator('#sources-continue').click();
        await page.locator('#go').click();
        await page.locator('#error').filter({ hasText: 'Preview only' }).waitFor();
        assert.equal(runs.at(-1).analytics, share);
      }
      await open();
      assert.equal(runs.length, 0, 'Viewing consent never starts setup');
      assert.equal(await page.locator('#consent a').count(), 2);
      assert((await page.locator('#consent').innerText()).includes('licensed to AI companies'));
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(out, `consent-${name}.png`), fullPage: true });
      const box = await page.locator('#consent').boundingBox();
      assert(box.x >= 0 && box.y >= 0 && box.x + box.width <= width && box.y + box.height <= height);
      const body = page.locator('.consent-body');
      assert(await body.evaluate(el => el.scrollWidth <= el.clientWidth));
      const close = await page.locator('#consent-close').boundingBox();
      const accept = await page.locator('#consent-accept').boundingBox();
      assert(close.y >= box.y && close.y + close.height <= box.y + box.height);
      assert(accept.y >= box.y && accept.y + accept.height <= box.y + box.height);
      assert.equal(await page.locator('#consent button').count(), 2, 'Only X and Accept');
      for (let n = 0; n < 7; n++) {
        await page.keyboard.press('Tab');
        assert(await page.locator('#consent').evaluate(el => el.contains(document.activeElement)));
      }
      // Keyboard activation follows the same path as a click.
      await page.locator('#consent-close').focus();
      await page.keyboard.press('Enter');
      assert(await page.locator('#ai-setup').isVisible());
      assert.equal(await page.locator('#page-title').evaluate(el => el === document.activeElement), true);
      await page.locator('#consent-back').click();
      await finishChoice(false);
      await open();
      await finishChoice(true);
      // Backtracking may change the decision; the final explicit choice is sent.
      await open();
      await page.locator('#consent-accept').click();
      await page.locator('#consent-back').click();
      await finishChoice(false);
      currentSharing = true;
      await open();
      assert((await page.locator('#consent-current').innerText()).includes('currently on'));
      assert.equal(runs.length, 3, 'Opening rerun does not submit consent');
      await finishChoice(false);
      // Escape also means no sharing, even if a prior choice was Accept.
      await open();
      await page.keyboard.press('Escape');
      assert(!(await page.locator('#consent').isVisible()));
      await page.locator('#ai-continue').click();
      await page.locator('#sources-continue').click();
      await page.locator('#go').click();
      await page.locator('#error').filter({ hasText: 'Preview only' }).waitFor();
      assert.equal(runs.at(-1).analytics, false);
      assert.deepEqual(errors, []);
      console.log(`${name}: initial modal, X, Accept, Escape, focus trap, backtracking, rerun and no overflow passed`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error.stack); process.exit(1); });
