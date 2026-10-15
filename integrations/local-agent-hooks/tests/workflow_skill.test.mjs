import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, cp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { workflowLaunch } from '../scripts/workflow_mcp.mjs';
import { writeIntegrationConfig } from '../lib/local_memory.mjs';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');

test('plugin and copied distribution expose a self-contained skill and real stdio launcher', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'tmcra-workflow-package-'));
  t.after(() => rm(dir,{recursive:true,force:true}));
  const packaged = join(dir, 'plugin');
  await cp(root, packaged, {recursive:true,filter: p => !p.includes('node_modules')});
  const manifest = JSON.parse(await readFile(join(packaged,'.codex-plugin/plugin.json')));
  assert.equal(manifest.skills,'./skills/');
  assert.equal(manifest.mcpServers,'./.mcp.json');
  const mcp = JSON.parse(await readFile(join(packaged,'.mcp.json')));
  assert.deepEqual(mcp.mcpServers['tmcra-workflows'],{command:'node',args:['./scripts/workflow_mcp.mjs'],cwd:'.'});
  const skill = await readFile(join(packaged,'skills/tmcra-workflows/SKILL.md'),'utf8');
  assert.match(skill,/name: tmcra-workflows/u);
  assert.match(skill,/`tmcra_workflows` MCP/u);
  assert.match(skill,/same MCP session\/project/u);
  assert.match(skill,/Never use a shell fallback/u);
  const api=await readFile(join(packaged,'skills/tmcra-workflows/references/api.md'),'utf8');
  for (const action of ['task.upsert','decision.create','reminder.evaluate','bridge.preview','bridge.commit']) assert.ok(api.includes(action));
  const result=spawnSync(process.execPath,['--check',join(packaged,'scripts/workflow_mcp.mjs')],{encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);
});

test('configuration records runtime Python, launcher inherits stdio without token access or shell', async t => {
  const dir=await mkdtemp(join(tmpdir(),'tmcra-workflow-launch-'));t.after(()=>rm(dir,{recursive:true,force:true}));
  const configRoot=join(dir,'config');await mkdir(join(configRoot,'runtime/secrets'),{recursive:true});
  await writeFile(join(configRoot,'runtime/secrets/local-api.token'),'test-local-only-not-a-real-secret'); // public-audit: allow-test-fixture
  const runtime=join(dir,'runtime.json'),config=join(dir,'integration.json');
  await writeFile(runtime,JSON.stringify({installation:{config_root:configRoot}}));
  await writeIntegrationConfig({runtimeConfigPath:runtime,outputPath:config,workflowPython:process.execPath});
  assert.equal(JSON.parse(await readFile(config)).workflowPython,process.execPath);
  assert.deepEqual(await workflowLaunch({TMCRA_LOCAL_INTEGRATION_CONFIG:config}),{command:process.execPath,args:['-m','tmcra_local.workflow_mcp']});
  await assert.rejects(()=>workflowLaunch({TMCRA_LOCAL_INTEGRATION_CONFIG:config,TMCRA_LOCAL_PYTHON:'python -c bad'}),/absolute/u);
  await writeFile(config,JSON.stringify({workflowPython:process.execPath,tokenFile:'/does/not/exist'}));
  // Choosing an interpreter does not read credentials: the MCP client reads them.
  assert.equal((await workflowLaunch({TMCRA_LOCAL_INTEGRATION_CONFIG:config})).command,process.execPath);
});

test('installer passes the interpreter into the packaged configuration on both platforms', async()=>{
  const repo=resolve(root,'../..');
  assert.match(await readFile(join(repo,'scripts/install-codex-local.sh'),'utf8'),/--python "\$WORKFLOW_PYTHON"/u);
  assert.match(await readFile(join(repo,'scripts/install-codex-local.ps1'),'utf8'),/'--python', \$WorkflowPython/u);
  const inventory=JSON.parse(await readFile(join(repo,'PUBLIC_RELEASE_MANIFEST.json')));
  assert.ok(inventory.required_paths.includes('integrations/local-agent-hooks/skills/tmcra-workflows/SKILL.md'));
});
