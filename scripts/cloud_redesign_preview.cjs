const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.argv[3]);
const options = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
(async () => {
  const browser = await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  let publicRequests=Promise.resolve(), nextPublicRequest=0;
  const pacePublicRequest=()=>{
    publicRequests=publicRequests.then(async()=>{
      const delay=nextPublicRequest-Date.now();
      if(delay>0) await new Promise(resolve=>setTimeout(resolve,delay));
      nextPublicRequest=Date.now()+1000;
    });
    return publicRequests;
  };
  try {
    for (const [name,width,height] of [['desktop',1440,900],['mobile',390,844]]) {
      const context = await browser.newContext({viewport:{width,height}});
      await context.addCookies([{name:'__Host-session',value:options.session,url:'https://cloud.varyzai.com',secure:true}]);
      const page = await context.newPage(), errors=[], preferenceRequests=[], posts=[], popups=[];
      let legacy = true;
      page.on('popup',popup=>popups.push(popup));
      const fixture = async (action,method='POST') => {
        const r = await context.request.fetch('http://127.0.0.1:19387/__preview/'+action,
          {method,headers:{Host:'cloud.varyzai.com'}});
        assert.equal(r.status(),200); return r.json();
      };
      await fixture('reset');
      if (!options.live) await context.route('https://cloud.varyzai.com/**', async route => {
        await pacePublicRequest();
        const req=route.request(), url=new URL(req.url());
        if(url.pathname==='/account/preferences') preferenceRequests.push(url.searchParams);
        const response=await route.fetch({url:'http://127.0.0.1:19387'+url.pathname+url.search,
          headers:{...req.headers(),host:'cloud.varyzai.com'}});
        await route.fulfill({response});
      });
      if (options.live) {
        // Exercise public HTTPS while respecting the production request limit.
        await context.route('https://cloud.varyzai.com/**',async route=>{
          await pacePublicRequest(); await route.continue();
        });
        page.on('request',req=>{const url=new URL(req.url()); if(url.origin==='https://cloud.varyzai.com' && url.pathname==='/account/preferences') preferenceRequests.push(url.searchParams);});
      }
      await context.route('http://127.0.0.1:1207/cloud/computer**', async route => {
        const req=route.request(), url=new URL(req.url());
        if(req.method()==='POST') posts.push({path:url.pathname,body:req.postDataJSON()});
        const response=await route.fetch({url:'http://127.0.0.1:19389'+url.pathname,
          headers:{...req.headers(),host:'127.0.0.1:19389',origin:'https://cloud.varyzai.com'}});
        if(options.legacy_choices && req.method()==='GET' && url.pathname.endsWith('/choices')) {
          const value=await response.json(); delete value.setup_complete;
          await route.fulfill({response,json:value}); return;
        }
        if(legacy && req.method()==='GET' && !req.headers()['x-varyz-ticket']) {
          const value=await response.json(); delete value.browser_oauth;
          await route.fulfill({response,json:value}); return;
        }
        await route.fulfill({response});
      });
      page.on('pageerror',e=>errors.push(e.message));
      page.on('console',m=>{if(m.type()==='error' && /Content Security Policy|nonce/.test(m.text())) errors.push(m.text());});
      await page.goto('https://cloud.varyzai.com/?view=setup');
      await page.locator('#computer-status').filter({hasText:'Update local Varyz'}).waitFor();
      assert.equal(posts.length,0); // Never start OAuth automatically on a legacy helper.
      assert.equal((await fixture('state','GET')).oauth,0);
      legacy=false;
      await page.evaluate(()=>sessionStorage.clear());
      await page.reload();
      await page.locator('#computer-status').filter({hasText:'Connect this computer once'}).waitFor();
      assert.equal(await page.locator('#connect-computer').innerText(),'Connect this computer');
      assert.equal((await fixture('state','GET')).oauth,0); // No automatic OAuth.
      assert.equal(await page.locator('.cloud-intro h1').innerText(),'Your Cloud');
      await page.screenshot({path:path.join(process.argv[2],`entry-${name}.png`)});
      await page.locator('.cloud-setup-skip').click();
      await page.locator('#cloud-dashboard-content').waitFor({state:'visible'});
      assert.equal((await fixture('state','GET')).oauth,0);
      assert.equal(posts.filter(p=>p.path.endsWith('/finish')).length,0);
      await page.goto('https://cloud.varyzai.com/?view=setup');
      await page.locator('#computer-status').filter({hasText:'Connect this computer once'}).waitFor();
      await page.evaluate(()=>document.fonts.ready);
      await page.screenshot({path:path.join(process.argv[2],`connect-${name}.png`)});
      await page.locator('#connect-computer').click();
      await page.locator('#computer-project-step').waitFor({state:'visible'});
      assert.equal((await fixture('state','GET')).oauth,1);
      assert.equal(popups.length,1);
      if (!popups[0].isClosed()) await popups[0].waitForEvent('close');
      assert.equal(popups[0].isClosed(),true);
      assert.equal(await page.locator('#computer-history-projects').count(),0);
      assert.equal(await page.locator('#computer-projects input[value=alpha]').count(),1);
      assert.equal(await page.locator('#computer-projects input').count(),3); // General, alpha, beta.
      assert.equal(await page.locator('#computer-projects input[value="alpha-v14"]').count(),0);
      await page.locator('#computer-projects input[value=alpha]').check();
      assert.equal(posts.filter(p=>p.path.endsWith('/finish')).length,0);
      await page.screenshot({path:path.join(process.argv[2],`projects-${name}.png`),fullPage:true});
      await page.reload();
      await page.locator('#computer-project-step').waitFor({state:'visible'});
      assert.equal(await page.locator('#computer-projects input[value=alpha]').isChecked(),true);
      await fixture('restart');
      await page.reload();
      await page.locator('#computer-project-step').waitFor({state:'visible',timeout:25000});
      assert.equal((await fixture('state','GET')).oauth,1); // Persisted credentials restored.
      assert.equal(await page.locator('#computer-projects input[value=alpha]').isChecked(),true);
      await page.locator('#computer-preview-button').click();
      await page.locator('#computer-review-step').waitFor({state:'visible'});
      assert.match(await page.locator('#computer-upload-summary').innerText(),/48 items/);
      assert.equal(posts.filter(p=>p.path.endsWith('/finish')).length,0);
      await page.locator('#computer-back-button').click();
      await page.locator('#computer-preview-button').click();
      await page.locator('#computer-review-step').waitFor({state:'visible'});
      await page.screenshot({path:path.join(process.argv[2],`approve-${name}.png`)});
      await page.locator('#computer-finish-button').click();
      await page.locator('#computer-done-step').waitFor({state:'visible',timeout:20000});
      assert.match(await page.locator('#computer-done-summary').innerText(),/connection check passed/);
      await page.screenshot({path:path.join(process.argv[2],`done-${name}.png`)});
      await page.locator('.cloud-setup-action').click();
      await page.locator('#cloud-dashboard-content').waitFor({state:'visible'});
      assert.equal(preferenceRequests.length,0);
      assert.equal(await page.locator('.cloud-statement').count(),0);
      assert.match(await page.locator('.cloud-preference-group[data-project=alpha] summary').innerText(),/2 work reports/);
      await page.screenshot({path:path.join(process.argv[2],`dashboard-${name}.png`)});
      const alpha=page.locator('.cloud-preference-group[data-project=alpha]');
      await alpha.locator('> summary').click();
      await alpha.locator('.cloud-statement').nth(19).waitFor();
      assert.equal(await alpha.locator('.cloud-statement').count(),20);
      assert.equal(preferenceRequests.length,1);
      await alpha.locator('.cloud-load-more').click();
      await alpha.locator('.cloud-statement').nth(39).waitFor();
      assert.equal(await alpha.locator('.cloud-statement').count(),40);
      await alpha.locator('.cloud-load-more').click();
      await alpha.locator('.cloud-statement').nth(44).waitFor();
      assert.equal(await alpha.locator('.cloud-statement').count(),45);
      assert.equal(await alpha.locator('.cloud-load-more').isVisible(),false);
      await alpha.locator('> summary').click();
      await alpha.locator('> summary').click();
      assert.equal(preferenceRequests.length,3); // No repeated loads on toggle.
      await page.locator('[data-cloud-view=connections]').click();
      assert.equal(await page.locator('[data-cloud-panel=projects]').isVisible(),false);
      await page.screenshot({path:path.join(process.argv[2],`connections-${name}.png`)});
      await page.locator('[data-cloud-view=account]').click();
      await page.screenshot({path:path.join(process.argv[2],`account-${name}.png`),fullPage:true});
      await page.goBack();
      assert.equal(await page.locator('[data-cloud-panel=connections]').isVisible(),true);
      await page.locator('[data-cloud-view=projects]').click();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      assert.deepEqual(errors,[]);
      await page.locator('.cloud-nav-connect').click();
      await page.locator('#computer-project-step').waitFor({state:'visible'}).catch(async error=>{
        await page.screenshot({path:path.join(process.argv[2],`failure-${name}.png`),fullPage:true});
        const diagnostic={status:await page.locator('#computer-status').innerText({timeout:1000}).catch(()=>page.locator('body').innerText()),
          state:await fixture('state','GET'),errors};
        require('node:fs').writeFileSync(path.join(process.argv[2],`failure-${name}.json`),JSON.stringify(diagnostic,null,2));
        console.error(JSON.stringify(diagnostic)); throw error;
      });
      assert.equal(await page.locator('#computer-projects input[value=alpha]').isChecked(),true);
      assert.equal((await fixture('state','GET')).oauth,1);
      // Browser session storage can disappear independently of the local daemon.
      // The saved account and approved scopes must still recover without OAuth
      // or another upload approval, even after a full bridge restart.
      const approvedPosts=posts.filter(p=>p.path.endsWith('/finish')).length;
      await page.evaluate(()=>sessionStorage.clear());
      await fixture('restart');
      await page.reload();
      await page.locator('#computer-project-step').waitFor({state:'visible',timeout:25000});
      assert.equal(await page.locator('#computer-projects input[value=alpha]').isChecked(),true);
      assert.equal((await fixture('state','GET')).oauth,1);
      assert.equal(posts.filter(p=>p.path.endsWith('/finish')).length,approvedPosts);
      await fixture('legacy_connection_hint');
      await page.evaluate(()=>sessionStorage.clear());
      await page.goto('https://cloud.varyzai.com/');
      await page.waitForURL('**/?view=dashboard');
      await page.locator('#cloud-dashboard-content').waitFor({state:'visible'});
      assert.equal((await fixture('state','GET')).oauth,1);
      assert.equal(posts.filter(p=>p.path.endsWith('/finish')).length,approvedPosts);
      assert.equal(await page.locator('#cloud-setup-content').isVisible(),false);
      await page.screenshot({path:path.join(process.argv[2],`returning-${name}.png`)});
      await fixture('payment_pending');
      await page.goto('https://cloud.varyzai.com/?payment=received');
      await page.locator('#payment-wait-status').waitFor();
      await fixture('payment_active');
      await page.waitForURL('**/?view=dashboard');
      assert.equal((await fixture('state','GET')).oauth,1);
      assert.equal(posts.filter(p=>p.path.endsWith('/finish')).length,approvedPosts);
      await context.clearCookies();
      for (const screen of ['login','signup','recover']) {
        await page.goto('https://cloud.varyzai.com/'+screen);
        await page.evaluate(()=>document.fonts.ready);
        await page.screenshot({path:path.join(process.argv[2],`${screen}-${name}.png`),fullPage:true});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      }
      await page.waitForLoadState('networkidle');
      await context.unrouteAll({behavior:'ignoreErrors'});
      await context.close();
    }
    console.log(`${options.live ? 'Public HTTPS Cloud' : 'Real isolated Cloud'}/bridge on desktop and mobile: new-user skip without OAuth or upload, returning-user dashboard recognition without a server hint, explicit project editing, PKCE tab closure, approved sync, restart/refresh recovery, lazy loading and layout passed.`);
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
