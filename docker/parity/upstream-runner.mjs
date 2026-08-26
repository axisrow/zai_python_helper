// Non-interactive adapter for the pinned @z_ai/coding-helper 0.0.7.
// The reference side is deliberately exercised through the public `chelper`
// CLI, not by importing upstream manager classes.
import { chmodSync, existsSync, mkdirSync, unlinkSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { homedir } from 'node:os';
import { join } from 'node:path';

const [tool, region, action, mcpId, token] = process.argv.slice(2);
const plan = region === 'china' ? 'glm_coding_plan_china' : 'glm_coding_plan_global';
if (!tool || !region || !action || !mcpId || !token) throw new Error('usage: upstream-runner.mjs TOOL REGION ACTION MCP TOKEN');

// `auth reload` is the upstream public non-interactive activation command.
const configDir = join(homedir(), '.chelper');
mkdirSync(configDir, { recursive: true });
const configPath = join(configDir, 'config.yaml');
writeFileSync(configPath, `lang: en_US\nplan: ${plan}\napi_key: ${token}\n`);

// The wizard requires the coding-tool executable to exist. The parity image
// intentionally has no real tools, so provide harmless presence-check shims.
const shimDir = join('/tmp', `zai-parity-tools-${process.pid}`);
mkdirSync(shimDir, { recursive: true });
for (const name of ['claude', 'opencode', 'crush', 'droid']) {
  const path = join(shimDir, name);
  if (!existsSync(path)) {
    writeFileSync(path, '#!/bin/sh\nexit 0\n');
    chmodSync(path, 0o755);
  }
}
process.env.PATH = `${shimDir}:${process.env.PATH || ''}`;

function run(args, input = '') {
  const result = spawnSync('chelper', args, { input, encoding: 'utf8', env: process.env });
  process.stdout.write(result.stdout || '');
  process.stderr.write(result.stderr || '');
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

function runInteractive(args, input = '') {
  const quote = (value) => `'${value.replaceAll("'", "'\\\"'\\\"'")}'`;
  const command = ['chelper', ...args].map((part, index) => index === 0 ? part : quote(part)).join(' ');
  const result = spawnSync('sh', ['-c', `{ printf '%s' ${quote(input)}; sleep 2; } | script -qec ${quote(command)} /dev/null`], {
    encoding: 'utf8',
    env: process.env,
  });
  process.stdout.write(result.stdout || '');
  process.stderr.write(result.stderr || '');
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

function finish() {
  // The upstream CLI config is setup input, not part of any tool action.
  unlinkSync(configPath);
}


if (action === 'activate') {
  run(['auth', 'reload', tool]);
} else if (action === 'revert') {
  // Active tool menu: select Unload, confirm, then select Exit.
  runInteractive(['enter', tool], '\u001b[B\ny\n\u001b[B\u001b[B\u001b[B\u001b[B\u001b[B\u001b[B\n');
} else if (action === 'mcp-install' || action === 'mcp-uninstall') {
  const ids = ['zai-mcp-server', 'web-search-prime', 'web-reader', 'zread'];
  const index = ids.indexOf(mcpId);
  if (index < 0) throw new Error(`unknown MCP: ${mcpId}`);
  const selectService = '\u001b[B'.repeat(index + 2);
  // Enter MCP menu, select built-in, perform its first action, return, exit.
  runInteractive(['enter', tool], `\u001b[B\u001b[B\n${selectService}\n\n\u001b[B\u001b[B\n${'\u001b[B'.repeat(6)}\n`);
} else {
  throw new Error(`unknown action: ${action}`);
}
finish();
