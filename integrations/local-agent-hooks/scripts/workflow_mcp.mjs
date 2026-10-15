import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { isAbsolute, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { defaultConfigPath } from "../lib/local_memory.mjs";

// Keep credentials in the existing owner-local configuration/token file. The
// launcher only chooses the installed interpreter; it never opens the token.
export async function workflowLaunch(environment = process.env) {
  const config = JSON.parse(await readFile(defaultConfigPath(environment), "utf8"));
  const configured = environment.TMCRA_LOCAL_PYTHON || config.workflowPython;
  if (configured && (!isAbsolute(configured) || /[\u0000\r\n]/u.test(configured))) {
    throw new Error("TMCRA workflow Python must be an absolute executable path");
  }
  return {
    command: configured || (process.platform === "win32" ? "python" : "python3"),
    args: ["-m", "tmcra_local.workflow_mcp"],
  };
}

export async function main(environment = process.env) {
  const launch = await workflowLaunch(environment);
  const child = spawn(launch.command, launch.args, { stdio: "inherit", shell: false, env: environment, windowsHide: true });
  for (const signal of ["SIGINT", "SIGTERM"]) process.once(signal, () => child.kill(signal));
  child.once("error", () => {
    process.stderr.write("TMCRA workflow MCP did not start. Re-run the local integration installer with the installed runtime Python.\n");
    process.exitCode = 1;
  });
  child.once("exit", (code) => { process.exitCode = code ?? 1; });
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(() => {
    process.stderr.write("TMCRA workflow MCP configuration is missing or invalid. Run the local integration installer first.\n");
    process.exitCode = 1;
  });
}
