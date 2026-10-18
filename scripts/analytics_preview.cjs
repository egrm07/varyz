// The real rules review page and its Varyz network popup, at one viewport.
// argv: url, out dir, playwright path, viewport name, width, height.
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.argv[4]);

const [url, out, , name, widthText, heightText] = process.argv.slice(2);
const width = Number(widthText), height = Number(heightText);

(async () => {
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
    args: ['--no-sandbox', '--disable-crash-reporter', '--disable-breakpad'],
  });
  try {
    const page = await browser.newPage({ viewport: { width, height } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const focused = selector => page.locator(selector).evaluate(el => el === document.activeElement);
    const dialog = page.locator('#analytics-dialog');

    async function openPending() {
      await page.goto(url);
      await page.locator('#statuses [aria-label^="Pending,"]').click();
      await page.locator('#keep-all:not([disabled])').waitFor();
    }

    async function reviewAndSave() {
      await openPending();
      await page.evaluate(() => document.fonts.ready);
      assert.equal(await dialog.isVisible(), false, 'Nothing is offered before a saved review');
      await page.locator('#keep-all').click();
      await page.locator('#notice').filter({ hasText: 'saved' }).waitFor();
    }

    // 1. The first saved review after the trial window: the real server offers once.
    await openPending();
    await page.evaluate(() => document.fonts.ready);
    const geometry = await page.evaluate(() => ({
      width: innerWidth, content: document.documentElement.scrollWidth,
    }));
    assert.ok(geometry.width === width && geometry.content <= width, JSON.stringify(geometry));
    await page.screenshot({ path: path.join(out, `review-${name}.png`) });
    const offered = page.waitForResponse('**/api/network/offer');
    await page.locator('#keep-all').click();
    const offer = (await (await offered).json()).offer;
    assert.ok(offer, 'The first saved review offers the network');
    await dialog.waitFor();
    assert.equal(await focused('#analytics-title'), true, 'Focus starts on the title');
    assert.match(await page.locator('#analytics-title').innerText(), /Join the Varyz network/);
    assert.match(await page.locator('#analytics-message').innerText(), /^How many times have you corrected your AI agent this week\?\s+The Varyz network/);
    assert.equal(await page.locator('#analytics-message .question').innerText(),
      'How many times have you corrected your AI agent this week?', 'The question leads on its own line');
    assert.equal(await dialog.evaluate(el => getComputedStyle(el).animationName), 'network-in', 'The popup animates in');
    await page.waitForTimeout(300);
    assert.match(await page.locator('#analytics-notice').innerText(), /licensed to AI companies/);
    assert.match(await page.locator('#analytics-privacy').textContent(), /can miss something/);
    assert.match(await page.locator('#analytics-current').innerText(), /currently off/);
    assert.equal(await page.locator('#analytics-dialog button').count(), 2, 'Only the x and one share button');
    assert.equal(await page.locator('#analytics-dialog').getAttribute('aria-labelledby'), 'analytics-title');
    const modal = await dialog.evaluate(el => {
      const box = el.getBoundingClientRect();
      return { left: box.left, right: box.right, top: box.top, bottom: box.bottom,
               width: el.clientWidth, content: el.scrollWidth };
    });
    assert.ok(modal.left >= 0 && modal.right <= width && modal.top >= 0 && modal.bottom <= height,
      'The dialog fits the viewport: ' + JSON.stringify(modal));
    assert.equal(modal.width, modal.content, 'No horizontal scrolling inside the dialog');
    const close = await page.locator('#analytics-close').boundingBox();
    const share = await page.locator('#analytics-share').boundingBox();
    assert.ok(close.x + close.width <= modal.right && close.y <= modal.top + 24, 'The x sits top right');
    assert.ok(share.y + share.height <= modal.bottom, 'The share button shows without scrolling');
    await page.screenshot({ path: path.join(out, `consent-${name}.png`) });
    await page.locator('#analytics-details summary').click();
    assert.match(await page.locator('#analytics-counts').innerText(), /placeholders/);
    await page.screenshot({ path: path.join(out, `details-${name}.png`) });
    await page.locator('#analytics-details summary').click();
    // The focus trap: x, summary, share, then back to the x, both ways.
    await page.locator('#analytics-close').focus();
    await page.keyboard.press('Tab');
    assert.equal(await focused('#analytics-details summary'), true);
    await page.keyboard.press('Tab');
    assert.equal(await focused('#analytics-share'), true);
    await page.keyboard.press('Tab');
    assert.equal(await focused('#analytics-close'), true);
    await page.keyboard.press('Shift+Tab');
    assert.equal(await focused('#analytics-share'), true);
    // Escape means no.
    const escaped = page.waitForRequest('**/api/network/answer');
    await page.keyboard.press('Escape');
    assert.deepEqual((await escaped).postDataJSON(), { share: false });
    await page.locator('#notice').filter({ hasText: 'Sharing stays off' }).waitFor();
    assert.equal(await dialog.isVisible(), false);
    assert.equal(await focused('#close'), true, 'Focus returns to the page');
    await page.screenshot({ path: path.join(out, `declined-${name}.png`) });

    // 2. Saving again in the same review: the server does not offer twice.
    await reviewAndSave();
    await page.waitForTimeout(300);
    assert.equal(await dialog.isVisible(), false, 'Shown once');

    // 3. With an offer due again (as 30 days after a no): the x means no.
    await page.route('**/api/network/offer', route => route.fulfill({ json: { offer } }));
    await reviewAndSave();
    await dialog.waitFor();
    const closed = page.waitForRequest('**/api/network/answer');
    await page.locator('#analytics-close').click();
    assert.deepEqual((await closed).postDataJSON(), { share: false });
    await page.locator('#notice').filter({ hasText: 'Sharing stays off' }).waitFor();

    // 4. Share turns it on.
    await reviewAndSave();
    await dialog.waitFor();
    await page.locator('#analytics-share').click();
    await page.locator('#notice').filter({ hasText: 'Sharing is on' }).waitFor();
    await page.screenshot({ path: path.join(out, `shared-${name}.png`) });

    assert.deepEqual(errors, []);
    console.log(name, JSON.stringify({ ...geometry, modal, interactionChecks: 12 }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.stack); process.exit(1); });
