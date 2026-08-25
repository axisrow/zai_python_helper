// Non-interactive adapter for the pinned @z_ai/coding-helper 0.0.7.
// The upstream exposes activate/revert/MCP operations through its wizard, so
// this adapter calls the same manager methods without a TTY or network.
import { claudeCodeManager } from '@z_ai/coding-helper/dist/lib/claude-code-manager.js';
import { openCodeManager } from '@z_ai/coding-helper/dist/lib/opencode-manager.js';
import { crushManager } from '@z_ai/coding-helper/dist/lib/crush-manager.js';
import { factoryDroidManager } from '@z_ai/coding-helper/dist/lib/factory-droid-manager.js';
import { PRESET_MCP_SERVICES } from '@z_ai/coding-helper/dist/lib/mcp-manager.js';

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

if (action === 'activate') manager.loadGLMConfig(plan, token);
else if (action === 'revert') manager.unloadGLMConfig();
else if (action === 'mcp-install') manager.installMCP(preset, token, plan);
else if (action === 'mcp-uninstall') manager.uninstallMCP(mcpId);
else throw new Error(`unknown action: ${action}`);
