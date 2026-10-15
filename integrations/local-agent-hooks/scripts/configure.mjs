import { resolve } from "node:path";
import { writeIntegrationConfig } from "../lib/local_memory.mjs";

function option(name, fallback = "") {
  const index = process.argv.indexOf(name);
  return index >= 0 ? String(process.argv[index + 1] || "") : fallback;
}

const runtimeConfig = option("--runtime-config");
if (!runtimeConfig) throw new Error("--runtime-config is required");

const result = await writeIntegrationConfig({
  runtimeConfigPath: resolve(runtimeConfig),
  workflowPython: option("--python"),
  outputPath: option("--output") || process.env.TMCRA_LOCAL_INTEGRATION_CONFIG || undefined,
  baseUrl: option("--base-url", "http://127.0.0.1:2009"),
  stateDir: option("--state-dir") || process.env.TMCRA_LOCAL_INTEGRATION_STATE_DIR || undefined,
  topK: Number(option("--top-k", "8")),
  userVisibility: option("--user-visibility", "both"),
});
process.stdout.write(`${JSON.stringify({ status: "configured", ...result })}\n`);
