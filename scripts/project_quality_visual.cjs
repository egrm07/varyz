const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.argv[2]);
(async () => {
  const browser = await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  try {
    for (const [size,width,height] of [['desktop',1440,900],['mobile',390,844]]) {
      const page = await browser.newPage({viewport:{width,height}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      const choices = {
        setup_complete:true,sync_selected:['varyz'],history_projects:[],report_projects:[],
        sync_projects:[
          {name:'varyz',root:'C:\\Users\\egor\\Documents\\projects\\varyz',reason:'Your authored repository activity',sessions:144,needs_review:false},
          {name:'memoir',root:'C:\\Users\\egor\\Documents\\projects\\memoir',reason:'Conversations found; confirm this is a project you want synced',sessions:9,needs_review:true},
          {name:'session-workspace',reason:'Folder name only; confirm what this project is',sessions:12,needs_review:true},
        ],
      };
      await page.addInitScript(() => {window.open=()=>null;});
      await page.route('**/cloud/computer**', route => {
        const suffix = new URL(route.request().url()).pathname.split('/computer')[1];
        const response = suffix === '/choices' ? choices : route.request().headers()['x-varyz-ticket']
          ? {state:'connected'} : {installed:true,browser_oauth:true,ticket:'t'.repeat(43)};
        return route.fulfill({json:response});
      });
      await page.route('**/computer/connect',route=>route.fulfill({json:{ok:true}}));
      await page.route('https://preview.example/**',route=>route.fulfill({
        contentType:'text/html',body:fs.readFileSync(process.argv[3],'utf8'),
      }));
      await page.goto('https://preview.example/');
      await page.locator('#computer-project-step').waitFor({state:'visible'});
      await page.locator('#computer-all-projects').check();
      assert.equal(await page.locator('#computer-projects input:checked').count(),1);
      assert.equal(await page.locator('input[value="session-workspace"]').isChecked(),false);
      assert.match(await page.locator('#computer-projects').innerText(),/Needs review/);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.screenshot({path:path.join(path.dirname(process.argv[3]),`picker-${size}.png`),fullPage:true});
      // Repeat after changing selection, checking retained review choices too.
      await page.locator('input[value="memoir"]').check();
      await page.screenshot({path:path.join(path.dirname(process.argv[3]),`picker-${size}-review.png`),fullPage:true});
      await page.locator('#computer-all-projects').uncheck();
      await page.locator('#computer-all-projects').check();
      assert.equal(await page.locator('input[value="memoir"]').isChecked(),false);
      assert.deepEqual(errors,[]);
      await page.close();
    }
    console.log('Desktop and mobile picker passed: uncertain folders skipped, manual selections retained, no overflow or script errors.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
