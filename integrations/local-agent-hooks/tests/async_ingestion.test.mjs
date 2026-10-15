import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,readdir,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {rememberMessage,flushOutbox,loadConfig,writeIntegrationConfig} from '../lib/local_memory.mjs';

async function fixture(fn) {
  const root=await mkdtemp(join(tmpdir(),'tmcra-hook-async-'));const previous=globalThis.fetch;
  const tokenFile=join(root,'token');await writeFile(tokenFile,'synthetic-only-bearer-test-123456789',{mode:0o600});
  const config={stateDir:root,tokenFile,baseUrl:'http://127.0.0.1:2009',timeoutMs:500,ingestionMode:'async'};
  const payload={project_id:'synthetic',session_id:'s',native_message_id:'m',role:'user',content:'SYNTHETIC BODY NEVER IN RECEIPT',actor:{actor_role:'user'}};
  try {await fn({root,config,payload});} finally {globalThis.fetch=previous;await rm(root,{recursive:true,force:true});}
}
async function receipts(root) {return Promise.all((await readdir(join(root,'receipts'))).map(async name=>JSON.parse(await readFile(join(root,'receipts',name),'utf8'))));}

test('202 is accepted pending, never committed, with explicit source/semantic status',()=>fixture(async({root,config,payload})=>{
  const paths=[];globalThis.fetch=async(url)=>{paths.push(url);return Response.json({accepted:true,indexed:false,derived_indexed:false,status:'queued',job_id:'synthetic-job',semantic_processing:'pending'},{status:202});};
  assert.equal(await rememberMessage(config,payload),'accepted_pending');
  assert.deepEqual(paths,['http://127.0.0.1:2009/v1/message-jobs']);
  const [receipt]=await receipts(root);assert.equal(receipt.indexed,false);assert.equal(receipt.derived_indexed,false);assert.equal(receipt.status,'accepted_pending');
  assert.equal(receipt.job_id,'synthetic-job');assert(!JSON.stringify(receipt).includes(payload.content));
  assert.deepEqual((await readdir(root)).filter(n=>n==='outbox'),[]);
}));
test('indexed immutable source with skipped origin does not claim derived indexing',()=>fixture(async({root,config,payload})=>{
  globalThis.fetch=async()=>Response.json({accepted:true,indexed:true,derived_indexed:false,status:'indexed',job_id:'j',semantic_processing:'skipped_origin_unverified'},{status:202});
  assert.equal(await rememberMessage(config,payload),'committed');
  const [receipt]=await receipts(root);assert.equal(receipt.indexed,true);assert.equal(receipt.derived_indexed,false);assert.equal(receipt.semantic_processing,'skipped_origin_unverified');
}));
test('async unsupported remains outbox and never falls back to blocking sync',()=>fixture(async({root,config,payload})=>{
  const paths=[];globalThis.fetch=async(url)=>{paths.push(url);return Response.json({detail:'missing'},{status:404});};
  assert.equal(await rememberMessage(config,payload),'queued');
  assert.equal((await readdir(join(root,'outbox'))).length,1);
  assert.deepEqual(paths,['http://127.0.0.1:2009/v1/message-jobs']);
  assert.equal((await receipts(root))[0].error_code,'async_unsupported');
}));
test('permanent failures do not repeatedly queue or head-block independent items',()=>fixture(async({root,config,payload})=>{
  await mkdir(join(root,'outbox'));
  for(let i=0;i<3;i++) await writeFile(join(root,'outbox',`${i}.json`),JSON.stringify({schemaVersion:1,payload:{...payload,native_message_id:'m'+i}}));
  let n=0;globalThis.fetch=async()=> ++n===1 ? Response.json({detail:'bad'},{status:400}) : n===2 ? Response.json({detail:'read-only'},{status:403}) : Response.json({accepted:true,indexed:false,status:'queued',job_id:'j'},{status:202});
  const result=await flushOutbox(config);assert.equal(result.rejected,2);assert.equal(result.accepted,1);assert.equal(result.committed,0);assert.equal(n,3);
  assert.deepEqual(await readdir(join(root,'outbox')),[]);assert.equal((await receipts(root)).filter(r=>r.status==='rejected').length,2);
  await flushOutbox(config);assert.equal(n,3);
}));
test('new config opts in async while missing legacy field remains sync',()=>fixture(async({root,config})=>{
  const old=join(root,'old.json');await writeFile(old,JSON.stringify({schemaVersion:1,...config,ingestionMode:undefined}));
  assert.equal((await loadConfig({TMCRA_LOCAL_INTEGRATION_CONFIG:old})).ingestionMode,'sync');
  const configRoot=join(root,'runtime');await mkdir(join(configRoot,'runtime','secrets'),{recursive:true});await writeFile(join(configRoot,'runtime','secrets','local-api.token'),'synthetic-config-only');
  const runtimeConfigPath=join(root,'runtime.json');await writeFile(runtimeConfigPath,JSON.stringify({installation:{config_root:configRoot}}));
  const outputPath=join(root,'new.json');await writeIntegrationConfig({runtimeConfigPath,outputPath,stateDir:join(root,'state')});
  assert.equal((await loadConfig({TMCRA_LOCAL_INTEGRATION_CONFIG:outputPath})).ingestionMode,'async');
}));



for (const terminal of ['retryable_failed','permanent_failed','interrupted','cancelled']) {
  test(`accepted ${terminal} job is recorded without pending success or automatic resubmission`,()=>fixture(async({root,config,payload})=>{
    let calls=0;globalThis.fetch=async()=>{calls++;return Response.json({accepted:true,indexed:false,derived_indexed:false,status:terminal,job_id:'j',semantic_processing:'failed'},{status:202});};
    assert.equal(await rememberMessage(config,payload),terminal);
    const [receipt]=await receipts(root);assert.equal(receipt.status,terminal);assert.equal(receipt.engine_state,terminal);assert.equal(receipt.indexed,false);
    await flushOutbox(config);assert.equal(calls,1);
  }));
}
