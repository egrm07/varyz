/* Real server-rendered sharing flow at desktop and phone sizes, synthetic data only. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.argv[2]);
const out = path.resolve('.tmp/shared-preview/review');
fs.mkdirSync(out,{recursive:true});
(async()=>{
 const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 const results=[];
 try {
  for(const [device,width,height] of [['desktop',1440,900],['mobile',390,844]]){
   const owner=await browser.newContext({viewport:{width,height}}),guest=await browser.newContext({viewport:{width,height}});
   const page=await owner.newPage(),g=await guest.newPage();
   const errors=[];
   page.on('pageerror',e=>errors.push(e.message));g.on('pageerror',e=>errors.push(e.message));
   const snap=async(p,name)=>{await p.evaluate(()=>document.fonts.ready);assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),device+' overflow '+name);await p.screenshot({path:path.join(out,device+'-'+name+'.png'),fullPage:true});};
   await page.goto('http://127.0.0.1:8822/preview/as/egr');
   await snap(page,'select');
   assert.equal(await page.locator('input[name=label],input[name=selected],details').count(),0);
   await page.getByLabel('Email or username',{exact:true}).fill('@joemama');
   await page.getByRole('button',{name:'Send invitation'}).click();
   await snap(page,'invite');
   assert(await page.getByRole('heading',{name:'Invitation sent',exact:true}).isVisible());
   const shareUrl=new URL(page.url()).pathname;
   await page.getByRole('button',{name:'Done',exact:true}).click();
   await g.goto('http://127.0.0.1:8822/preview/as/joemama');
   await g.getByRole('tab',{name:'With me',exact:true}).click();
   await snap(g,'accept');await g.getByRole('button',{name:'Join',exact:true}).first().click();
   await g.waitForURL('**/account/shared/*');await snap(g,'brief');
   const membership=g.url().split('/').pop();
   assert(await g.getByRole('heading',{name:'Project added',exact:true}).isVisible());
   assert.equal(await g.getByRole('link',{name:'Connect ChatGPT'}).isVisible(),false);
   assert.equal(await g.getByRole('heading',{name:'Your questions'}).isVisible(),false);
   assert.equal(await g.getByRole('button',{name:'Leave collaboration'}).isVisible(),false);
   await g.getByRole('button',{name:'Copy request'}).click();
   const access=await guest.request.get('http://127.0.0.1:8822/preview/access/joemama');const token=(await access.json()).access_token;
   await g.evaluate(()=>window.dispatchEvent(new Event('focus')));
   await g.waitForFunction(()=>document.querySelector('[data-setup-stage]').dataset.setupStage==='verify');
   assert(await g.getByText('Your AI connection is saved.',{exact:false}).isVisible());
   assert(!(await g.getByLabel('Request for your AI').inputValue()).includes(membership));
   await snap(g,'verify');
   const tool=async(name,args)=>{const r=await guest.request.post('http://127.0.0.1:8822/mcp/shared',{headers:{Authorization:'Bearer '+token},data:{jsonrpc:'2.0',id:1,method:'tools/call',params:{name,arguments:args}}});assert.equal(r.status(),200);const result=(await r.json()).result;assert(!result.isError,JSON.stringify(result));return result.structuredContent;};
   const task='visual-'+device;const brief=await tool('shared_brief',{membership_id:membership,conversation_id:task,expected_binding_revision:0});
   await g.evaluate(()=>window.dispatchEvent(new Event('focus')));
   await g.waitForFunction(()=>document.querySelector('[data-setup-stage]').dataset.setupStage==='ready');
   assert(await g.getByText('Your AI has read the current shared preferences.',{exact:true}).isVisible());
   await snap(g,'ready');
   const bound={membership_id:membership,conversation_id:task,binding_revision:brief.binding_revision};
   const q=await tool('shared_ask',{...bound,idempotency_key:'visual-question-'+device,requests:[{question:'Should we use a warm accent?',reason:'Synthetic browser acceptance question.'}]});
   await page.goto('http://127.0.0.1:8822/account/questions/'+q.results[0].question_id);await snap(page,'reply');
   await page.getByLabel('Your reply').fill('Use marigold sparingly.');await page.getByLabel('Add to shared preferences').check();
   await page.getByRole('button',{name:'Send reply'}).click();
   const status=await tool('shared_question_status',{...bound,thread_ids:[q.results[0].question_id]});assert.equal(status.questions[0].reply.answer,'Use marigold sparingly.');assert(status.brief_changed);
   await g.reload();await snap(g,'answered');
   assert(await g.getByRole('heading',{name:'Project added',exact:true}).isVisible());
   assert(await g.getByText('Your AI connection is saved.',{exact:false}).isVisible());
   await page.goto('http://127.0.0.1:8822'+shareUrl);
   await snap(page,'management');
   await page.getByText('Remove access',{exact:true}).first().click();
   await page.getByRole('checkbox',{name:'Remove this person’s access'}).check();await page.getByRole('button',{name:'Remove access'}).click();
   const denied=await guest.request.post('http://127.0.0.1:8822/mcp/shared',{headers:{Authorization:'Bearer '+token},data:{jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'shared_finish',arguments:bound}}});const rejection=await denied.json();assert(rejection.result?.isError || [401,403,404].includes(denied.status()),JSON.stringify(rejection));
   assert.deepEqual(errors,[]);results.push({device,flow:'email or username → send invitation → join → MCP question → owner shared reply → revocation',passed:true});
   await owner.close();await guest.close();
  }
  fs.writeFileSync(path.join(out,'checks.json'),JSON.stringify(results,null,2));console.log(JSON.stringify(results));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
