import {chromium} from '@playwright/test';
import {spawn, execFileSync} from 'node:child_process';
import {mkdtemp, mkdir, rm, readFile, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';

const temporary = await mkdtemp(path.join(os.tmpdir(),'userese-browser-'));
const project = path.join(temporary,'project');
const python = process.env.PYTHON || 'python3';
execFileSync(python,['-m','userese_video','demo',project]);
const server = spawn(python,['-m','userese_video','serve',project,'--port','0'],{stdio:['ignore','pipe','pipe']});
let browser;
try {
  const url = await new Promise((resolve,reject)=>{
    const timeout = setTimeout(()=>reject(new Error('Server startup timed out')),15000);
    let output='';
    server.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/http:\/\/[^\s]+/);if(match){clearTimeout(timeout);resolve(match[0]);}});
    server.on('exit',code=>{clearTimeout(timeout);reject(new Error(`Server stopped: ${code}`));});
  });
  browser = await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? {executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH} : {})});
  const context = await browser.newContext({viewport:{width:1440,height:1100}});
  const page = await context.newPage();
  const errors=[]; page.on('pageerror',error=>errors.push(error.message));
  page.on('console',message=>{if(message.type()==='error'&&!message.text().includes('409')) errors.push(message.text());});
  await page.goto(url); await page.locator('#workspace').waitFor({state:'visible'});
  await page.waitForFunction(()=>document.querySelectorAll('#takes button').length===2);
  const initial = JSON.parse(await readFile(path.join(project,'decisions.json'),'utf8'));
  await page.locator('[data-take="opening-a"]').click();
  assert.deepEqual(JSON.parse(await readFile(path.join(project,'decisions.json'),'utf8')),initial,'Browsing must not save a decision');
  await page.locator('#listen').click();
  await page.waitForFunction(()=>document.querySelector('#player').videoWidth>0&&document.querySelector('#player').currentSrc.includes('/media/preview/'),null,{timeout:60000});
  await page.locator('#player').evaluate(video=>video.pause());
  await page.locator('#note').fill('保留这一遍的完整表达');
  await page.locator('.transcript summary').click();
  await page.locator('#captions input').fill('人工纠正后的开场字幕');
  await page.locator('#keep').click();
  await page.waitForFunction(()=>document.querySelector('#save-receipt').textContent.includes('已保存到本机'));
  let saved = JSON.parse(await readFile(path.join(project,'decisions.json'),'utf8'));
  assert.equal(saved.choices.opening.take,'opening-a');
  assert.equal(saved.captions['source-001-cue-0001'],'人工纠正后的开场字幕');
  await page.reload(); await page.waitForFunction(()=>document.querySelector('#note').value==='保留这一遍的完整表达');
  assert.equal(await page.locator('#saved-status').textContent(),'✓ 使用');
  await page.locator('#context').click();
  await page.waitForFunction(()=>document.querySelector('#job-status').textContent==='试听已准备好。',null,{timeout:60000});
  await page.locator('#player').evaluate(video=>video.pause());
  await page.locator('#segments button').nth(1).click(); await page.locator('#skip').click();
  await page.waitForFunction(()=>document.querySelector('#saved-status').textContent==='− 不放');
  await page.locator('#segments button').nth(2).click(); await page.locator('#keep').click();
  await page.waitForFunction(()=>document.querySelector('#progress-count').textContent==='3 / 3');
  await page.locator('#build').click();
  await page.waitForFunction(()=>document.querySelector('#job-status').textContent.includes('新版本已完成'),null,{timeout:120000});
  await page.waitForFunction(()=>document.querySelector('#draft-player').videoWidth>0);
  assert.ok(Math.abs(await page.locator('#draft-player').evaluate(video=>video.duration)-6)<.1);
  const srt = await page.request.get(new URL((await page.locator('#downloads a').nth(2).getAttribute('href')),url).toString());
  assert.match(await srt.text(),/人工纠正后的开场字幕/);
  const zip = await page.request.get(new URL((await page.locator('#downloads a').first().getAttribute('href')),url).toString());
  assert.equal(zip.status(),200); assert.equal((await zip.body()).subarray(0,2).toString(),'PK');
  await page.locator('#draft-player').evaluate(video=>{video.currentTime=.5;}); await page.locator('#jump').click();
  assert.equal(await page.locator('#segment-title').textContent(),'选一个更好的开头');
  const downloadPromise=page.waitForEvent('download'); await page.locator('#export').click(); const download=await downloadPromise;
  const snapshot=path.join(temporary,'export.json'); await download.saveAs(snapshot);
  await page.locator('#note').fill('另一次修改'); await page.locator('#save').click();
  await page.waitForFunction(()=>document.querySelector('#save-receipt').textContent.includes('已保存到本机'));
  await page.locator('#import').setInputFiles(snapshot);
  await page.waitForFunction(()=>document.querySelector('#save-receipt').textContent.includes('已导入并保存'));
  assert.equal(await page.locator('#note').inputValue(),'保留这一遍的完整表达');
  // A stale tab must not overwrite a newer saved revision.
  const second = await page.context().newPage(); await second.goto(new URL('/',url).toString()); await second.locator('#workspace').waitFor({state:'visible'});
  await page.locator('#note').fill('主页面的新意见'); await page.locator('#save').click();
  await page.waitForFunction(()=>document.querySelector('#save-receipt').textContent.includes('已保存到本机'));
  await second.locator('#note').fill('旧页面的意见'); await second.locator('#save').click();
  await second.waitForFunction(()=>document.querySelector('#notice').textContent.includes('其他页面'));
  saved=JSON.parse(await readFile(path.join(project,'decisions.json'),'utf8'));
  assert.equal(saved.choices.opening.note,'主页面的新意见');
  await second.close({runBeforeUnload:false});
  // Cover narrow phones through wide desktop, with touch-friendly targets.
  const widths=[320,390,768,1440]; const results=[];
  await mkdir('.working/qa',{recursive:true});
  for(const selector of ['#player','#draft-player']) {
    await page.locator(selector).evaluate(async video=>{await video.play();});
    await page.waitForFunction(selector=>{const video=document.querySelector(selector);return video.readyState>=3&&!video.seeking&&video.currentTime>.1;},selector);
    await page.locator(selector).evaluate(video=>video.pause());
  }
  for(const width of widths){
    await page.setViewportSize({width,height:1050});
    const metrics=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,
      tiny:[...document.querySelectorAll('button,select,a')].filter(el=>el.getClientRects().length&&el.getBoundingClientRect().height<43&&!el.classList.contains('brand')).map(el=>el.textContent)}));
    assert.ok(metrics.scroll<=width,`Overflow at ${width}: ${metrics.scroll}`);assert.deepEqual(metrics.tiny,[],`Small targets at ${width}`);
    results.push(metrics);await page.screenshot({path:`.working/qa/workbench-${width}.png`,fullPage:true});
  }
  await page.setViewportSize({width:1440,height:1080}); await page.locator('#player').evaluate(video=>video.pause());
  await mkdir('docs/images',{recursive:true});
  if(process.env.UPDATE_SCREENSHOT==='1') await page.screenshot({path:'docs/images/workbench.png',fullPage:true});
  assert.deepEqual(errors,[]);
  await writeFile('.working/qa/browser.json',JSON.stringify({passed:true,viewports:results,errors,checks:['candidate playback','context playback','explicit choice','notes','caption correction','reload','render','download zip','timeline jump','export/import','conflict protection']},null,2));
  console.log('Browser E2E passed: playback, editing, rendering, download, snapshots, conflicts, and 4 viewport sizes.');
} finally {
  if(browser) await browser.close(); server.kill('SIGTERM'); await rm(temporary,{recursive:true,force:true});
}
