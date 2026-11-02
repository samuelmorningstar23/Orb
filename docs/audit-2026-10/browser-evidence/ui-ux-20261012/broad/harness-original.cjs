#!/usr/bin/env node
'use strict';
// Real backend/UI evidence only. No API fixtures, inference calls or live data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { chromium } = require('@playwright/test');
const { runAudit } = require('../tests/visual/checks.cjs');
const { jsonGet, unusedPort } = require('./sage_browser_timing.cjs');
const ROOT = path.resolve(__dirname, '..');
const sha = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const CREDS = { doctor: 'DOC001', admin: 'ADM001', nurse: 'NUR001' };
const TABS = {
  doctor: ['today', 'vigil', 'orders', 'relay', 'sage', 'scribe', 'lens', 'forecast', 'appointments',
    'radiology', 'care-plans', 'emergency', 'perioperative', 'dialysis'],
  admin: ['feedback', 'mortuary', 'cssd', 'reporting', 'finance-tools', 'record-governance',
    'radiation-safety', 'infection-surveillance', 'admin'],
  nurse: ['observations', 'vigil', 'relay'],
};
function geometryAudit() {
  const visible = e => e.checkVisibility({ visibilityProperty: true }) && e.getBoundingClientRect().width > 1;
  const selector = e => e.id ? `#${e.id}` : `${e.tagName.toLowerCase()}.${String(e.className || '').trim().split(/\s+/).join('.')}`;
  const rect = e => { const r=e.getBoundingClientRect(); return { x:r.x,y:r.y,w:r.width,h:r.height }; };
  const controls = [...document.querySelectorAll('button,input,select,textarea,a[href],summary,[role="tab"]')].filter(visible);
  const occluded = [];
  for (const e of controls) {
    const r=e.getBoundingClientRect(), x=r.x+r.width/2,y=r.y+r.height/2;
    if (x<0 || x>innerWidth || y<0 || y>innerHeight) continue;
    const top=document.elementFromPoint(x,y);
    if (top && top!==e && !e.contains(top) && !top.contains(e)) occluded.push({ selector:selector(e),text:(e.innerText||e.getAttribute('aria-label')||e.name||'').slice(0,120),rect:rect(e),cover:selector(top) });
  }
  const scrollContainers=[...document.querySelectorAll('*')].filter(visible).filter(e=>e.scrollWidth>e.clientWidth+3 && /auto|scroll/.test(getComputedStyle(e).overflowX)).map(e=>({selector:selector(e),width:e.clientWidth,scrollWidth:e.scrollWidth,rect:rect(e)}));
  return { viewport:{width:innerWidth,height:innerHeight,devicePixelRatio},document:{width:document.documentElement.clientWidth,scrollWidth:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight},theme:document.documentElement.dataset.theme,occludedControls:occluded,scrollContainers,alerts:[...document.querySelectorAll('[role="alert"]')].filter(visible).map(e=>e.innerText),activeElement:{selector:selector(document.activeElement),text:(document.activeElement.innerText||'').slice(0,80)} };
}
async function startBackend(python, port, output) {
  await unusedPort(port);
  const log=fs.createWriteStream(path.join(output,'backend.log'),{flags:'wx'});
  const child=spawn(python,[path.join(ROOT,'scripts/e2e_server.py'),'--host','127.0.0.1','--port',String(port),'--offline-modules'],{cwd:ROOT,stdio:['ignore','pipe','pipe']});
  child.stdout.on('data',c=>log.write(c));child.stderr.on('data',c=>log.write(c));
  let exit=null;const exited=new Promise(resolve=>child.once('exit',(code,signal)=>{exit={code,signal};resolve(exit);}));
  child.once('error',e=>{exit={error:e.message};});
  const stop=async()=>{if(!exit)child.kill('SIGTERM');let timer;const ended=await Promise.race([exited.then(()=>true),new Promise(resolve=>{timer=setTimeout(()=>resolve(false),25000);})]);clearTimeout(timer);log.end();assert.ok(ended,'owned backend cleanup exceeded25s');return exit;};
  try { for(let i=0;i<90;i++){if(exit)throw new Error(`owned backend exited: ${JSON.stringify(exit)}`);try{await jsonGet(`http://127.0.0.1:${port}/api/ready`);return {stop,pid:child.pid};}catch{}await delay(1000);}throw new Error('owned backend not ready'); }catch(e){await stop();throw e;}
}
async function main() {
  const opts=Object.fromEntries(process.argv.slice(2).map(arg=>{const m=/^--(python|port|out)=(.+)$/.exec(arg);assert.ok(m,`unsupported argument: ${arg}`);return [m[1],m[2]];}));
  const python=opts.python||'/private/tmp/orb-ml-upgrade-20261012/bin/python',port=Number(opts.port||18122),output=path.resolve(opts.out||'/private/tmp/orb-browser-ux-20261012');
  assert.ok(port>=1024&&port<=65535);assert.ok(!fs.existsSync(output),'refuse to replace evidence');fs.mkdirSync(output,{recursive:true});fs.mkdirSync(path.join(output,'screenshots'));
  const base=`http://127.0.0.1:${port}`;
  const result={schema:'orb.audit.real-browser-ux/1',startedAt:new Date().toISOString(),base,scope:'Owned synthetic backend and production bundle; no API mocks; inference endpoint closed port9; no shared Ollama requests.',bundleSha256:sha(path.join(ROOT,'dist/index.html')),harnessSha256:sha(__filename),launcherSha256:sha(path.join(ROOT,'scripts/e2e_server.py')),captures:[],errors:[],externalRequests:[],inferenceRequests:[],dialogFocus:[],cookieSecurity:[],contexts:[]};
  const save=()=>fs.writeFileSync(path.join(output,'results.current.json'),JSON.stringify(result,null,2)+'\n');
  let backend,browser;
  const interrupt=()=>{result.errors.push({type:'interrupted'});void browser?.close();};process.once('SIGINT',interrupt);process.once('SIGTERM',interrupt);
  try {
    backend=await startBackend(python,port,output);result.backendLauncherPid=backend.pid;browser=await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});result.browserVersion=browser.version();
    let index=0;
    for(const vp of [{name:'desktop',width:1440,height:1000},{name:'mobile',width:390,height:844}]) for(const theme of ['light','dark']) for(const role of ['doctor','admin',...(theme==='light'?['nurse']:[])]) {
      const profile={viewport:vp,theme,role,deviceScaleFactor:1,touch:vp.name==='mobile'};
      const context=await browser.newContext({viewport:{width:vp.width,height:vp.height},deviceScaleFactor:1,isMobile:profile.touch,hasTouch:profile.touch,colorScheme:theme,reducedMotion:'reduce'});
      await context.addInitScript(theme=>localStorage.setItem('orb-theme',theme),theme);
      await context.route('**/*',async route=>{const url=new URL(route.request().url());if(url.origin!==base){result.externalRequests.push({url:url.origin+url.pathname,profile});return route.abort();}if(/\/api\/(?:sage\/query|lens\/analyze|scribe\/generate)/.test(url.pathname)){result.inferenceRequests.push({path:url.pathname,profile});return route.abort();}await route.continue();});
      const page=await context.newPage();page.setDefaultTimeout(7000);
      const browserErrors=[];page.on('pageerror',e=>browserErrors.push({type:'pageerror',message:e.message}));page.on('console',m=>{if(m.type()==='error')browserErrors.push({type:'console',message:m.text().slice(0,500)});});
      const capture=async(name,steps)=>{
        await delay(200);const screenshot=`screenshots/${String(index++).padStart(3,'0')}-${vp.name}-${theme}-${role}-${name}.png`;
        const audit=await page.evaluate(runAudit);const geometry=await page.evaluate(geometryAudit);
        await page.screenshot({path:path.join(output,screenshot),fullPage:true});result.captures.push({name,profile,url:page.url(),steps,screenshot,sha256:sha(path.join(output,screenshot)),audit,geometry,browserErrors:browserErrors.splice(0)});save();console.log(JSON.stringify({capture:name,...profile,findings:audit.length,occluded:geometry.occludedControls.length,scrollWidth:geometry.document.scrollWidth}));
      };
      try {
        await page.goto(base+'/login/staff');await page.locator('#input-employee-id').waitFor();await capture('staff-login',['Open real staff login']);
        await page.locator('#input-employee-id').fill(CREDS[role]);await page.locator('#input-password').fill('password');await page.locator('#btn-login').click();await page.waitForURL('**/dashboard**');await page.locator('.shell').waitFor();
        result.cookieSecurity.push({profile,cookies:(await context.cookies()).map(({name,httpOnly,secure,sameSite,path})=>({name,httpOnly,secure,sameSite,path})),localStorageKeys:await page.evaluate(()=>Object.keys(localStorage))});
        const navIds=await page.locator('[role="tab"][id^="nav-"]').evaluateAll(nodes=>nodes.map(e=>e.id));result.contexts.push({...profile,availableNav:navIds});
        for(const tab of TABS[role]) {
          if(!navIds.includes(`nav-${tab}`)){result.errors.push({type:'nav-unavailable',tab,profile});continue;}
          try {
            await page.locator(`#nav-${tab}`).click();await page.locator(`#panel-${tab}`).waitFor({state:'visible'});await delay(300);await capture(tab,[`Login as ${role}`,`Click #nav-${tab}`]);
            const panel=page.locator(`#panel-${tab}`);
            if(['radiology','care-plans','emergency','perioperative','dialysis'].includes(tab)) {await panel.getByRole('button',{name:'New record',exact:true}).click();await capture(`${tab}-new-form`,[`Open ${tab}`,'Click New record; inspect authored form']);}
            if(['feedback','mortuary','cssd'].includes(tab)){const summaries=panel.locator('details > summary');for(let n=0;n<await summaries.count();n++)await summaries.nth(n).click();await capture(`${tab}-expanded-forms`,[`Open ${tab}`,'Expand all domain forms']);}
            if(tab==='reporting'){await panel.getByRole('button',{name:'Preview report',exact:true}).click();await delay(350);await capture('reporting-preview',['Open Reports & cohorts','Preview real scoped synthetic records']);}
            if(tab==='vigil') {const row=page.locator('.patient-row').filter({has:page.locator('.patient-row__name',{hasText:'Ananya Kapoor'})});if(await row.count()){await row.click();await page.locator('.command-bar__name').waitFor();await delay(350);await capture('patient-chart',['Open patient list','Open synthetic Ananya Kapoor chart']);}}
          }catch(e){result.errors.push({type:'scenario-error',tab,profile,message:e.message});await capture(`${tab}-error`,[`Attempt #nav-${tab}`]).catch(()=>{});}
        }
        if(role==='doctor')for(const [id,name] of [['btn-admit-patient','admit-dialog'],['btn-shortcuts','keyboard-shortcuts'],['btn-mfa-enroll','account-security']]) {
          const button=page.locator(`#${id}`);if(!await button.count()||!await button.isVisible()){result.errors.push({type:'control-unavailable',id,profile});continue;}
          try {await button.click();const dialog=page.locator('[role="dialog"]').last();await dialog.waitFor({state:'visible'});await capture(name,[`Click #${id}`]);const focus=[];for(let n=0;n<16;n++){await page.keyboard.press('Tab');focus.push(await page.evaluate(()=>{const e=document.activeElement;return {id:e.id,tag:e.tagName,text:(e.getAttribute('aria-label')||e.textContent||'').slice(0,80),inside:!!e.closest('[role="dialog"]')};}));}await page.keyboard.press('Escape');await delay(150);result.dialogFocus.push({profile,name,focus,escaped:!await dialog.isVisible(),restoredFocus:await page.evaluate(()=>document.activeElement.id)});if(await dialog.isVisible()){const close=dialog.getByRole('button',{name:/close|cancel/i}).first();if(await close.count())await close.click();else await page.reload();}}catch(e){result.errors.push({type:'dialog-error',name,profile,message:e.message});await page.reload();}
        }
        // Exercise actual theme changes, rather than testing reload-only palettes.
        const toggle=page.locator('[data-testid="theme-toggle"],.theme-toggle').first();if(await toggle.count()){await toggle.click();await capture('theme-toggle',['Click actual theme toggle']);}
      }catch(e){result.errors.push({type:'context-error',profile,message:e.message});}
      finally{result.errors.push(...browserErrors.map(e=>({...e,profile})));await context.close();save();}
    }
  }catch(e){result.errors.push({type:'fatal',message:e.stack});}
  finally{if(browser)await browser.close();if(backend)result.backendCleanup=await backend.stop();result.finishedAt=new Date().toISOString();save();fs.writeFileSync(path.join(output,'results.json'),JSON.stringify(result,null,2)+'\n');}
  console.log(JSON.stringify({output,captures:result.captures.length,errors:result.errors.length,externalRequests:result.externalRequests.length,inferenceRequests:result.inferenceRequests.length}));
}
if(require.main===module)main().catch(e=>{console.error(e);process.exitCode=1;});
module.exports={geometryAudit};
