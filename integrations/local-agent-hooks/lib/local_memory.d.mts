export interface LocalIntegrationConfig {
  path: string;
  baseUrl: string;
  tokenFile: string;
  stateDir: string;
  topK: number;
  userVisibility: "project" | "global" | "both";
  timeoutMs: number;
}

export interface LocalProject {
  projectId: string;
  projectTitle: string;
  source: string;
}

export function redactSensitiveText(value: unknown): string;
export function validateLoopbackBaseUrl(value: unknown): string;
export function restrictOwnerAccess(
  path: string,
  environment?: NodeJS.ProcessEnv,
): Promise<void>;
export function restrictOwnerDirectory(
  path: string,
  environment?: NodeJS.ProcessEnv,
): Promise<void>;
export function defaultConfigPath(environment?: NodeJS.ProcessEnv): string;
export function loadConfig(environment?: NodeJS.ProcessEnv): Promise<LocalIntegrationConfig>;
export function apiRequest(
  config: LocalIntegrationConfig,
  method: string,
  path: string,
  payload?: unknown,
  environment?: NodeJS.ProcessEnv,
): Promise<Record<string, any>>;
export function resolveProject(cwdValue?: string): Promise<LocalProject>;
export function flushOutbox(
  config: LocalIntegrationConfig,
  environment?: NodeJS.ProcessEnv,
): Promise<{ attempted: number; committed: number }>;
export function rememberMessage(
  config: LocalIntegrationConfig,
  payload: Record<string, unknown>,
  environment?: NodeJS.ProcessEnv,
): Promise<"committed" | "queued">;
export function writeIntegrationConfig(options: {
  runtimeConfigPath: string;
  outputPath?: string;
  baseUrl?: string;
  stateDir?: string;
  topK?: number;
  userVisibility?: "project" | "global" | "both";
  workflowPython?: string;
}): Promise<{ configPath: string; tokenFileStoredByReference: true; secretPrinted: false }>;
