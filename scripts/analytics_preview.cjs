// Actual setup UI and consent interactions; intercepted runs never start setup.
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.argv[4]);

(async () => {
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
    args: ['--no-sandbox', '--disable-crash-reporter', '--disable-breakpad'],
  });
  try {
    for (const [name, width, height] of [['desktop', 1440, 900], ['mobile', 390, 844]]) {
      const page = await browser.newPage({ viewport: { width, height } });
      const runs = [];
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/api/run', route => {
        runs.push(route.request().postDataJSON());
        return route.fulfill({ status: 202, json: { started: true } });
      });
      async function ready() {
        await page.goto(process.argv[2]);
        await page.locator('#go:not([disabled])').waitFor();
        await page.evaluate(() => document.fonts.ready);
        assert.equal(await page.locator('#analytics-dialog').isVisible(), false);
      }
      await ready();
      const geometry = await page.evaluate(() => ({
        width: innerWidth, content: document.documentElement.scrollWidth,
      }));
      if (geometry.width !== width || geometry.content > width) {
        throw new Error('Setup geometry failed: ' + JSON.stringify(geometry));
      }
      assert.match(await page.locator('#analytics-status').innerText(), /sharing is off/);
      await page.screenshot({ path: path.join(process.argv[3], `top-${name}.png`) });
      await page.locator('#go').click();
      assert.equal(runs.length, 0, 'Opening consent must not submit setup');
      assert.equal(await page.locator('#analytics-title').evaluate(el => el === document.activeElement), true);
      assert.match(await page.locator('#analytics-message').innerText(), /solo developer/);
      assert.match(await page.locator('#analytics-message').innerText(), /never receive or see your messages/);
      assert.match(await page.locator('#analytics-privacy').innerText(), /isn't fully anonymous/);
      const modal = await page.locator('#analytics-dialog').evaluate(el => ({
        left: el.getBoundingClientRect().left, right: el.getBoundingClientRect().right,
        top: el.getBoundingClientRect().top, bottom: el.getBoundingClientRect().bottom,
        width: el.clientWidth, content: el.scrollWidth,
      }));
      assert.ok(modal.left >= 0 && modal.right <= width && modal.top >= 0 && modal.bottom <= height);
      assert.equal(modal.width, modal.content, 'No horizontal scrolling inside dialog');
      await page.screenshot({ path: path.join(process.argv[3], `consent-${name}.png`) });
      const decline = await page.locator('#analytics-decline').boundingBox();
      const share = await page.locator('#analytics-share').boundingBox();
      assert.ok(decline.y + decline.height <= modal.bottom && share.y + share.height <= modal.bottom,
        'Both choices must be visible without scrolling');
      await page.locator('#analytics-details summary').click();
      await page.locator('#analytics-counts').waitFor();
      assert.match(await page.locator('#analytics-counts').innerText(), /No names/);
      await page.screenshot({ path: path.join(process.argv[3], `details-${name}.png`) });
      await page.locator('#analytics-details summary').click();
      await page.locator('#analytics-title').focus();
      await page.keyboard.press('Tab');
      assert.equal(await page.locator('#analytics-details summary').evaluate(el => el === document.activeElement), true);
      await page.keyboard.press('Tab');
      assert.equal(await page.locator('#analytics-decline').evaluate(el => el === document.activeElement), true);
      await page.keyboard.press('Tab');
      assert.equal(await page.locator('#analytics-share').evaluate(el => el === document.activeElement), true);
      await page.keyboard.press('Tab');
      assert.equal(await page.evaluate(() => document.getElementById('analytics-dialog').contains(document.activeElement)), true);
      await page.keyboard.press('Escape');
      assert.equal(runs.length, 0, 'Dismissal must not submit or grant consent');
      assert.equal(await page.locator('#go').evaluate(el => el === document.activeElement), true);
      await page.locator('#go').click();
      await page.locator('#analytics-decline').click();
      await page.waitForFunction(() => document.getElementById('go').disabled);
      await page.locator('#analytics-status').filter({ hasText: 'sharing is off.' }).waitFor();
      assert.equal(runs.at(-1).analytics, false);

      await ready();
      await page.locator('#go').click();
      await page.locator('#analytics-share').click();
      await page.locator('#analytics-status').filter({ hasText: 'sharing is on.' }).waitFor();
      assert.equal(runs.at(-1).analytics, true);
      assert.equal(runs.length, 2);

      // Existing consent is disclosed; Escape keeps it and No thanks revokes it.
      await page.route('**/api/plan', async route => {
        const response = await route.fetch();
        const plan = await response.json();
        plan.analytics.sharing = true;
        await route.fulfill({ response, json: plan });
      });
      await ready();
      await page.locator('#go').click();
      assert.match(await page.locator('#analytics-current').innerText(), /currently on/);
      await page.keyboard.press('Escape');
      assert.equal(runs.length, 2);
      assert.match(await page.locator('#analytics-status').innerText(), /sharing is on/);
      await page.locator('#go').click();
      await page.locator('#analytics-decline').click();
      await page.locator('#analytics-status').filter({ hasText: 'sharing is off.' }).waitFor();
      assert.equal(runs.at(-1).analytics, false);

      // Failed submission stays retryable, without accidentally changing consent.
      await page.unroute('**/api/run');
      await page.route('**/api/run', route => route.fulfill({ status: 500, json: { error: 'fixture failure' } }));
      await ready();
      await page.locator('#go').click();
      await page.locator('#analytics-share').click();
      await page.locator('#error:not([hidden])').waitFor();
      assert.equal(await page.locator('#go').isEnabled(), true);
      await page.locator('#go').click();
      assert.equal(await page.locator('#analytics-dialog').isVisible(), true);
      await page.keyboard.press('Escape');
      assert.deepEqual(errors, []);
      console.log(name, JSON.stringify({ ...geometry, modal, interactionChecks: 8 }));
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.stack); process.exit(1); });
