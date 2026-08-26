// Non-interactive adapter for the pinned @z_ai/coding-helper 0.0.7.
//
// Activation has a documented public CLI command (`chelper auth reload`).
// The pinned package exposes no non-interactive revert/MCP commands: those
// operations are only reachable from the inquirer wizard (see README and
// package.json: there is no --yes/headless mode, and no package tests). Driving
// that wizard through a pipe/PTY is not reliable (exit 130 when stdin closes),
// so those categories intentionally retain the upstream manager surface until
// upstream provides a headless CLI API. This keeps the parity matrix green and
// avoids pretending that an interactive transcript is CLI-to-CLI parity.
import { claudeCodeManager } from '/usr/local/lib/node_modules/@z_ai/coding-helper/dist/lib/claude-code-manager.js';
import { openCodeManager } from '/usr/local/lib/node_modules/@z_ai/coding-helper/dist/lib/opencode-manager.js';
import { crushManager } from '/usr/local/lib/node_modules/@z_ai/coding-helper/dist/lib/crush-manager.js';
import { factoryDroidManager } from '/usr/local/lib/node_modules/@z_ai/coding-helper/dist/lib/factory-droid-manager.js';
import { PRESET_MCP_SERVICES } from '/usr/local/lib/node_modules/@z_ai/coding-helper/dist/lib/mcp-manager.js';
import { chmodSync, mkdirSync, unlinkSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { homedir } from 'node:os';
import { join } from 'node:path';

const [tool, region, action, mcpId, token] = process.argv.slice(2);
const plan = region === 'china' ? 'glm_coding_plan_china' : 'glm_coding_plan_global';
const managers = {
  'claude-code': claudeCodeManager,
  opencode: openCodeManager,
  crush: crushManager,
  'factory-droid': factoryDroidManager,
};
const manager = managers[tool];
if (!manager) throw new Error(`unknown tool: ${tool}`);
const preset = PRESET_MCP_SERVICES.find((item) => item.id === mcpId);
if (!preset) throw new Error(`unknown MCP: ${mcpId}`);

function activateViaCli() {
  const configDir = join(homedir(), '.chelper');
  const configPath = join(configDir, 'config.yaml');
  mkdirSync(configDir, { recursive: true });
  writeFileSync(configPath, `lang: en_US\nplan: ${plan}\napi_key: ${token}\n`);
  // The Docker image ships only the Claude shim.  `auth reload` checks the
  // selected tool with `which` before calling its configuration manager, so
  // create no-op presence shims for the other three matrix tools here.  They
  // live outside HOME and are not part of the parity artifact snapshot.
  const shimDir = join('/tmp', `zai-parity-cli-tools-${process.pid}`);
  mkdirSync(shimDir, { recursive: true });
  for (const name of ['opencode', 'crush', 'droid']) {
    const shimPath = join(shimDir, name);
    writeFileSync(shimPath, '#!/bin/sh\nexit 0\n');
    chmodSync(shimPath, 0o755);
  }
  const pathBefore = process.env.PATH || '';
  process.env.PATH = `${shimDir}:${pathBefore}`;
  try {
    const result = spawnSync('chelper', ['auth', 'reload', tool], {
      encoding: 'utf8',
      env: process.env,
    });
    process.stdout.write(result.stdout || '');
    process.stderr.write(result.stderr || '');
    if (result.error) throw result.error;
    if (result.status !== 0) process.exit(result.status ?? 1);
  } finally {
    process.env.PATH = pathBefore;
    unlinkSync(configPath);
  }
}

if (action === 'activate') activateViaCli();
else if (action === 'revert') manager.unloadGLMConfig();
else if (action === 'mcp-install') manager.installMCP(preset, token, plan);
else if (action === 'mcp-uninstall') manager.uninstallMCP(mcpId);
else throw new Error(`unknown action: ${action}`);
