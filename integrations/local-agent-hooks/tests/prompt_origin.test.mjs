import test from 'node:test';
import assert from 'node:assert/strict';
import { classifyPromptOrigin, permitsPromptCapture, normalizeQueuedOrigin } from '../lib/prompt_origin.mjs';

test('role, text templates and actor_type never establish human authorship', () => {
  for (const prompt of ['ordinary synthetic fixture', '<heartbeat>quoted by user</heartbeat>', 'suggested-next-user-message: quoted documentation']) {
    assert.equal(classifyPromptOrigin({ role:'user',actor_type:'human',prompt }).origin_kind,'unknown');
    assert.equal(permitsPromptCapture(classifyPromptOrigin({prompt})),true);
  }
});
test('explicit metadata routes generated/automatic inputs away from capture without inspecting body', () => {
  for(const input of [{origin_kind:'app_generated'},{is_synthetic:true},{is_generated:true},{automation_id:'test-fixture'},{origin_kind:'automation'}]) {
    assert.equal(permitsPromptCapture(classifyPromptOrigin({...input,prompt:'quoted fixture'})),false);
  }
  for(const origin_kind of ['human_authored','human_adopted']) {
    const declared=classifyPromptOrigin({origin_kind,prompt:'suggested-next-user-message: quoted documentation'});
    assert.equal(declared.provenance_strength,'host_declared');
    assert.equal(permitsPromptCapture(declared),true);
  }
  assert.equal(permitsPromptCapture(classifyPromptOrigin({origin_kind:'human_authored',is_automated:true})),false);
});
test('legacy queued identities remain byte-compatible and do not become humans', () => {
  const old={actor_role:'user',actor_type:'human'};
  assert.deepEqual(normalizeQueuedOrigin(old),old);
  assert.equal(classifyPromptOrigin(old).origin_kind,'unknown');
  assert.equal(normalizeQueuedOrigin({origin_kind:'human_authored'},'assistant').origin_kind,'unknown');
  assert.equal(permitsPromptCapture(normalizeQueuedOrigin({origin_kind:'automation'})),false);
});

import { handleUserPrompt, flushOutbox } from '../lib/local_memory.mjs';
import { mkdtemp, mkdir, writeFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

test('automatic event exits before config, recall, pending state or model dispatch', async () => {
  const result = await handleUserPrompt({prompt:'synthetic',is_generated:true},'codex',{});
  assert.deepEqual(result,{continue:true});
});

test('real queued replay discards declared automation, preserves legacy role-only body without promotion', async () => {
  const root=await mkdtemp(join(tmpdir(),'tmcra-origin-outbox-'));
  const previous=globalThis.fetch;
  try {
    const outbox=join(root,'outbox');await mkdir(outbox);
    const token=join(root,'token');await writeFile(token,'synthetic-test-token-long-enough-1234',{mode:0o600});
    const payload={project_id:'test',session_id:'s',native_message_id:'id',role:'user',content:'synthetic quoted suggestion',actor:{actor_role:'user'}};
    await writeFile(join(outbox,'0.json'),JSON.stringify({schemaVersion:1,payload:{...payload,actor:{origin_kind:'automation'}}}));
    await writeFile(join(outbox,'1.json'),JSON.stringify({schemaVersion:1,payload}));
    const sent=[];
    globalThis.fetch=async (_url,options) => {sent.push(JSON.parse(options.body));return new Response('{}',{status:200});};
    const result=await flushOutbox({stateDir:root,baseUrl:'http://127.0.0.1:2009',tokenFile:token,timeoutMs:500});
    assert.equal(result.committed,1);assert.equal(sent.length,1);
    assert.deepEqual(sent[0].actor,{actor_role:'user'});
    assert.equal(sent[0].content,payload.content);
    assert.deepEqual(await readdir(outbox),[]);
  } finally {globalThis.fetch=previous;await rm(root,{recursive:true,force:true});}
});
