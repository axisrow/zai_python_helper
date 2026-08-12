# 模型选择模式

`use zai` 可以用**四种**方式配置 Claude Code 对接 Z.ai。模式控制的是*写入哪些环境
变量*以及*你对模型的约束程度*。

| 模式 | 写入的内容 | 适用场景 |
|------|-----------|----------|
| **original** | 只写入 `ANTHROPIC_BASE_URL` → Z.ai | 你想让服务端决定模型（匹配原 `@z_ai/coding-helper`） |
| **default** | `ANTHROPIC_BASE_URL` + `ANTHROPIC_DEFAULT_*_MODEL` 变量 | 你想让 Z.ai 的预设模型被自动选用 |
| **select** | base URL + 一个具体预设模型 | 你想对一个已知预设显式控制 |
| **custom** | base URL + 你自己的模型 id + 名称 | beta 模型、自定义部署 |

## original

只把 `ANTHROPIC_BASE_URL` 设为 Z.ai 端点。模型由服务端挑选。

```bash
zai-python-helper use zai --mode original
```

这是最贴近上游工具行为的模式，也是省略 `--mode` 时的**默认**模式。

## default

Z.ai 的预设模型通过 `ANTHROPIC_DEFAULT_*_MODEL` 变量接入，因此 Claude Code 内置的
模型别名（opus/sonnet/haiku）会自动映射到 Z.ai 预设。

```bash
zai-python-helper use zai --mode default
```

## select

按名称选一个具体预设。运行 `list` 查看可用预设：

```bash
zai-python-helper list
```

```bash
zai-python-helper use zai --mode select --model glm-4-plus
```

## custom

提供你自己的模型 id 和显示名称（可选地加描述和能力）。适用于 beta 模型，或位于
Z.ai base URL 之后的自托管端点。

```bash
zai-python-helper use zai \
  --mode custom \
  --model "my-custom-model" \
  --name "My Model" \
  --capabilities "effort,thinking"
```

## 上游参考（`@z_ai/coding-helper` 0.0.7）

对上游包的黑盒测试确认：Claude Code **不会读取** `customModels` 数组。上游工具在
`~/.claude/settings.json` 中通过环境变量配置 Claude Code：

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "<token>",
    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
    "API_TIMEOUT_MS": "3000000",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": 1
  }
}
```

它还会将 `"hasCompletedOnboarding": true` 合并到 `~/.claude.json`，并保留无关设置。
`customModels` 是 Factory Droid 的格式，不是 Claude Code 的格式。无头模式下可使用：

```bash
chelper auth glm_coding_plan_global "$ZAI_API_KEY"
chelper auth reload claude
```

第一条命令验证并把令牌保存到 `~/.chelper/config.yaml`，第二条将配置应用到 Claude Code。
`<token>` 是文档中的尖括号占位符，并非字面 shell 语法：未加引号的 `<token>` 会被 shell
解析为输入重定向，因此应通过环境变量（如上面的 `ZAI_API_KEY`）传入真实值，而不要直接
粘贴令牌本身。`chelper enter claude-code` 需要交互式 TTY，不适合作为无头脚本的替代方案。

## 区域

每个模式都接受 `--region`。区域选择 Z.ai 端点：

- `global`（默认）—— `https://api.z.ai/api/anthropic`
- `china` —— `https://api.zai.cn/api/anthropic`

```bash
zai-python-helper use zai --mode select --model glm-4-plus --region china
```

## 令牌解析

认证令牌按如下顺序解析：

1. 命令行上的 `--api-key`
2. `ZAI_API_KEY` 环境变量
3. 交互式提示（仅当两者都不存在时）

如果你在写脚本，总是传 `--api-key` 或导出 `ZAI_API_KEY`，这样 CLI 永远不会卡在提示上。

## 另见

- [CLI 参考](../cli-reference.md) —— 每个标志。
- [可导入 API](importable.md) —— 改用 Python 驱动 `plan_zai`。
