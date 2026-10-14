/* Synthetic loopback review of the shared overview and owner confirmation. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {chromium} = require(process.argv[2]);
const out = path.resolve('.tmp/shared-context-review');
fs.mkdirSync(out, {recursive:true});
const base = 'http://127.0.0.1:8822';
(async()=>{
  const browser = await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  const checks = [];
  try {
    for (const [device,width,height] of [['desktop',1440,900],['mobile',390,844]]) {
      const owner = await browser.newContext({viewport:{width,height}});
      const guest = await browser.newContext({viewport:{width,height}});
      const p = await owner.newPage(), g = await guest.newPage();
      const errors = [];
      p.on('pageerror',e=>errors.push(e.message)); g.on('pageerror',e=>errors.push(e.message));
      const snap = async(page,name)=>{
        await page.evaluate(()=>document.fonts.ready);
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),device+' overflow');
        await page.screenshot({path:path.join(out,device+'-'+name+'.png'),fullPage:true});
      };
      await p.goto(base+'/preview/as/egr');
      await p.getByLabel('What is this project and what is it meant to do?').fill('A product website explaining Varyz, its optional Cloud service and pricing.');
      await p.getByLabel('Email or username',{exact:true}).fill('@joemama');
      await snap(p,'invitation');
      await p.getByRole('button',{name:'Send invitation'}).click();
      await p.waitForURL('**/account/shares/*');
      const sharePath = new URL(p.url()).pathname;
      await p.getByRole('button',{name:'Done',exact:true}).click();
      const share = sharePath.split('/').pop();
      await g.goto(base+'/preview/as/joemama');
      await g.getByRole('tab',{name:'With me',exact:true}).click();
      await g.getByRole('button',{name:'Join',exact:true}).first().click();
      await g.waitForURL('**/account/shared/*');
      const membership = g.url().split('/').pop();
      const actions = [
        {action:'report',share_id:share,work:{summary:'Built and checked the pricing section on desktop and phone.',checks:['Desktop and phone layouts passed'],open_work:['Review the Cloud pricing copy'],open_questions:['Should the FAQ explain offline use?'],next_steps:['Review the copy with the owner'],next_milestone:'Pricing section ready for publication'}},
        {action:'context',share_id:share,context:{explanation:'A product website explaining Varyz and its optional Cloud service.',story:[{text:'We dropped the crowded pricing layout after the phone review.',at:'2026-10-10',by:'egr',source:'Reviewed synthetic project notes'}]}}
      ];
      execFileSync(path.resolve('.venv/Scripts/python.exe'),['-c',"import sys,json;sys.path.insert(0,'scripts');import shared_varyz_preview as p;[p.app.state.sharing.collaborate('egr',x) for x in json.loads(sys.argv[1])]",JSON.stringify(actions)],{stdio:'pipe'});
      await p.goto(base+sharePath);
      assert(await p.getByText('We dropped the crowded pricing layout after the phone review.',{exact:true}).isVisible());
      await snap(p,'owner-review');
      await p.getByRole('button',{name:'Save shared context',exact:true}).click();
      await g.reload();
      assert(await g.getByText('A product website explaining Varyz and its optional Cloud service.',{exact:true}).isVisible());
      assert.equal(await g.getByText('We dropped the crowded pricing layout after the phone review.',{exact:true}).count(),0);
      assert(await g.getByRole('heading',{name:'Still open',exact:true}).isVisible());
      assert(await g.getByText('Review the Cloud pricing copy',{exact:true}).isVisible());
      await snap(g,'recipient');
      const access = await guest.request.get(base+'/preview/access/joemama');
      const token = (await access.json()).access_token;
      const response = await guest.request.post(base+'/mcp/shared',{headers:{Authorization:'Bearer '+token},data:{jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'shared_brief',arguments:{membership_id:membership,conversation_id:'context-visual-'+device,expected_binding_revision:0}}}});
      const result = (await response.json()).result;
      assert(!result.isError,JSON.stringify(result));
      assert.equal(result.structuredContent.collaboration.project_context.agent_context.story.length,1);
      assert.deepEqual(errors,[]);
      checks.push({device,owner_review:true,overview:true,story_agent_only:true,overflow:false});
      await owner.close(); await guest.close();
    }
    fs.writeFileSync(path.join(out,'checks.json'),JSON.stringify(checks,null,2));
    console.log(JSON.stringify(checks));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
