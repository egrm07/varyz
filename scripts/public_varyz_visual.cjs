/* Real publish/follow/pick/unpublish browser flow; isolated synthetic server only. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.argv[2]);
const out = path.resolve(process.argv[3] || '.tmp/public-varyz-review');
fs.mkdirSync(out, {recursive:true});
(async()=>{
 const browser = await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 const results = [];
 try {
  for (const [device,width,height] of [['desktop',1440,900],['mobile',390,844]]) {
   const owner = await browser.newContext({viewport:{width,height}});
   const guest = await browser.newContext({viewport:{width,height}});
   const visitor = await browser.newContext({viewport:{width,height}});
   const p = await owner.newPage(), g = await guest.newPage(), v = await visitor.newPage();
   const errors = [];
   for (const page of [p,g,v]) page.on('pageerror',e=>errors.push(e.message));
   const snap = async(page,name)=>{
    await page.evaluate(()=>document.fonts.ready);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),device+' overflow '+name);
    assert(!(await page.locator('body').innerText()).includes('PRIVATE_PROJECT_SENTINEL'));
    await page.screenshot({path:path.join(out,device+'-'+name+'.png'),fullPage:true});
   };
   await p.goto('http://127.0.0.1:8823/preview/as/egr');
   if (await p.getByText('Turn off Public Varyz',{exact:true}).count()) {
    await p.getByText('Turn off Public Varyz',{exact:true}).click();
    await p.getByRole('button',{name:'Turn off',exact:true}).click();
   }
   await p.goto('http://127.0.0.1:8823/account/public/topic?name=code-review');
   await snap(p,'choose');
   assert.equal(await p.locator('input[name=selected]:checked').count(),2);
   const answer = p.getByLabel('Should pull requests include tests? Yes, for changes in behavior.',{exact:false});
   assert(!(await answer.isChecked()));
   await answer.check();
   await p.getByRole('button',{name:'Finish',exact:true}).click();
   await snap(p,'review');
   await p.getByRole('checkbox',{name:'I reviewed this exact text.',exact:false}).check();
   await p.getByRole('button',{name:'Publish',exact:true}).click();
   await snap(p,'creator');
   await v.goto('http://127.0.0.1:8823/@egr');
   await snap(v,'profile');
   await v.getByRole('button',{name:'Follow @egr',exact:true}).click();
   assert(new URL(v.url()).pathname==='/login');
   await g.goto('http://127.0.0.1:8823/preview/as/guest');
   if (await g.getByRole('button',{name:'Following · Unfollow',exact:true}).count()) {
    await g.getByRole('button',{name:'Following · Unfollow',exact:true}).click();
    await g.goto('http://127.0.0.1:8823/@egr');
   }
   await g.getByRole('button',{name:'Follow @egr',exact:true}).click();
   await snap(g,'following-profile');
   await g.goto('http://127.0.0.1:8823/@ann');
   if (await g.getByRole('button',{name:'Following · Unfollow',exact:true}).count()) {
    await g.getByRole('button',{name:'Following · Unfollow',exact:true}).click();
    await g.goto('http://127.0.0.1:8823/@ann');
   }
   await g.getByRole('button',{name:'Follow @ann',exact:true}).click();
   await g.goto('http://127.0.0.1:8823/account/shared?tab=with-me#following');
   await snap(g,'pick');
   await g.getByRole('radio',{name:'@egr',exact:true}).check();
   await g.getByRole('button',{name:'Use for code review',exact:true}).click();
   await snap(g,'picked');
   await p.goto('http://127.0.0.1:8823/account/public');
   await p.getByText('Turn off Public Varyz',{exact:true}).click();
   await p.getByRole('button',{name:'Turn off',exact:true}).click();
   await v.goto('http://127.0.0.1:8823/@egr');
   await snap(v,'off');
   assert(!(await v.locator('body').innerText()).includes('read the whole diff'));
   assert.deepEqual(errors,[]);
   results.push({device,width,height,passed:true,flow:'choose → review → publish → profile → follow → pick → turn off'});
   await owner.close(); await guest.close(); await visitor.close();
  }
  fs.writeFileSync(path.join(out,'checks.json'),JSON.stringify(results,null,2));
  console.log(JSON.stringify(results));
 } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
