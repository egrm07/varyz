// Actual Cloud app; synthetic bridge responses never connect or upload owner data.
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.argv[3]);
(async () => {
  const browser = await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe', headless:true});
  try {
    for (const [name,width,height] of [['desktop',1440,900],['mobile',390,844]]) {
      for (const outcome of ['complete','complete_empty','verification_failed']) {
        const context = await browser.newContext({viewport:{width,height}});
        await context.addCookies([{name:'__Host-session', value:process.argv[4], url:'https://cloud.varyzai.com', secure:true}]);
        const page = await context.newPage(), errors = [], actions = [];
        const fixture = async action => {
          const response = await context.request.post('http://127.0.0.1:19387/__preview/'+action,
            {headers:{host:'cloud.varyzai.com'}});
          assert.equal(response.status(),200);
        };
        await fixture('reset');
        await page.route('https://cloud.varyzai.com/**', async route => {
          const url = new URL(route.request().url());
          const response = await route.fetch({url:'http://127.0.0.1:19387'+url.pathname+url.search,
            headers:{...route.request().headers(), host:'cloud.varyzai.com'}});
          await route.fulfill({response});
        });
        page.on('pageerror', e => errors.push(e.message));
        let state = 'connected';
        await page.route('**/computer/connect', route => route.fulfill({json:{ok:true}}));
        await page.route('http://127.0.0.1:1207/cloud/computer**', async route => {
          const request = route.request(), suffix = new URL(request.url()).pathname.split('/computer')[1];
          let value;
          if (suffix === '/choices') value = {general:2,history_projects:[{name:'*',count:4},{name:'Varyz',count:12}],report_projects:[{name:'varyz',count:8},{name:'san7.14',count:3}],report_selected:[],projects:[{name:'Varyz',count:3}],selected:[],all_projects:false,selection_locked:false};
          else if (suffix === '/preview') { actions.push('preview'); assert.deepEqual(request.postDataJSON().report_projects,outcome==='complete'?['varyz']:[]); value = {uploads:outcome==='complete_empty'?0:2,report_uploads:outcome==='complete'?8:0,deletions:0,approval:'synthetic-approval'}; }
          else if (suffix === '/finish') { actions.push('finish'); assert.equal(request.postDataJSON().approve,true); state=outcome; if(outcome==='complete') await fixture('sync'); value={state:'syncing'}; }
          else if (request.method() === 'POST') value={state:'connecting'};
          else if (!request.headers()['x-varyz-ticket']) value={installed:true,ticket:'t'.repeat(43)};
          else value={state};
          await route.fulfill({json:value});
        });
        await page.goto('https://cloud.varyzai.com/');
        await page.evaluate(() => document.fonts.ready);
        assert.equal(await page.locator('#cloud-dashboard-content').isVisible(),false);
        assert.equal(await page.locator('#computer-fallback').getAttribute('open'),null);
        if(outcome==='complete') await page.screenshot({path:path.join(process.argv[2],`connect-${name}.png`)});
        await page.locator('#connect-computer').click();
        await page.locator('#computer-project-step').waitFor({state:'visible'});
        assert.equal(await page.locator('#computer-connect-step').isVisible(),false);
        assert.equal(await page.locator('#computer-fallback').isVisible(),false);
        assert.deepEqual(actions,[]);
        assert.equal(await page.locator('#computer-report-projects input:checked').count(),0);
        if(outcome==='complete') {
          await page.locator('#computer-history-projects input').last().check();
          assert.equal(await page.locator('#computer-report-projects input:checked').count(),0);
          await page.screenshot({path:path.join(process.argv[2],`projects-${name}.png`),fullPage:true});
          await page.locator('#computer-report-projects input').first().check();
        }
        await page.locator('#computer-preview-button').click();
        await page.locator('#computer-review-step').waitFor({state:'visible'});
        assert.equal(await page.locator('#computer-project-step').isVisible(),false);
        assert.deepEqual(actions,['preview']);
        await page.locator('#computer-back-button').click();
        assert.equal(await page.locator('#computer-project-step').isVisible(),true);
        await page.locator('#computer-preview-button').click();
        if(outcome==='complete') await page.screenshot({path:path.join(process.argv[2],`approve-${name}.png`)});
        await page.locator('#computer-finish-button').click();
        if(outcome==='verification_failed') {
          await page.locator('#computer-finish-status').filter({hasText:'did not finish'}).waitFor();
          assert.equal(await page.locator('#computer-done-step').isVisible(),false);
          await page.locator('#computer-back-button').click();
          assert.equal(await page.locator('#computer-project-step').isVisible(),true);
        } else {
          await page.locator('#computer-done-step').waitFor({state:'visible'});
          assert.equal(await page.locator('#cloud-dashboard-content').isVisible(),false);
          assert.equal(await page.locator('#computer-review-step').isVisible(),false);
          assert.equal(await page.locator('#computer-done-title').evaluate(el=>el===document.activeElement),true);
          if(outcome==='complete_empty') assert.match(await page.locator('#computer-done-summary').innerText(),/no preferences to check yet/);
          else assert.match(await page.locator('#computer-done-summary').innerText(),/connection check passed/);
          await page.screenshot({path:path.join(process.argv[2],`${outcome}-${name}.png`)});
          assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
          await page.locator('.cloud-setup-action').click();
          await page.locator('#cloud-dashboard-content').waitFor({state:'visible'});
          assert.match(page.url(),/view=dashboard/);
          assert.equal(await page.locator('#cloud-dashboard-content').isVisible(),true);
          assert.equal(await page.locator('#cloud-setup-content').isVisible(),false);
          if(outcome==='complete') {
            assert.match(await page.locator('#preferences').innerText(),/1 confirmed preferences/);
            await page.locator('.cloud-preference-group > summary').click();
            assert.match(await page.locator('#preferences').innerText(),/Show one step at a time/);
          }
        }
        assert.deepEqual(errors,[]);
        await page.waitForLoadState('networkidle');
        await page.unrouteAll({behavior:'ignoreErrors'});
        await context.close();
      }
    }
    console.log('Desktop/mobile: connect, select, preview, back, approve, verified/empty completion, failure recovery and dashboard reload passed.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
