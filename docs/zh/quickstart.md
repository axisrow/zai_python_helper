# 快速开始

从零开始，五分钟内让 Claude Code 与 Z.ai 对话。

## 1. 安装

```bash
pip install zai-python-helper
```

从源码安装，并包含文档工具：

```bash
git clone https://github.com/axisrow/zai_python_helper.git
cd zai_python_helper
pip install -e ".[docs]"
```

验证：

```bash
zai-python-helper --version
```

## 2. 获取 Z.ai 认证令牌

从你的 [Z.ai](https://z.ai) 账户获取一个令牌（GLM Coding Plan）。导出它以便 CLI
找到，或在命令行传入 `--api-key`：

```bash
export ZAI_API_KEY="<your token>"
```

## 3. 切换到 Z.ai

```bash
zai-python-helper use zai
```

这会修改 `~/.claude/settings.json`、`~/.claude.json` 和 `~/.zshrc`，把 Claude Code
指向 Z.ai 的 Anthropic 兼容端点。原值会被记录到所有权日志中，以便干净地回退。

想要指定模型？选一个[模式](guide/modes.md)：

```bash
# 选择一个预设模型
zai-python-helper use zai --mode select --model glm-4-plus

# 或者你自己的模型 id
zai-python-helper use zai --mode custom --model "my-model" --name "My Model"

# 预览将要发生的改动，但不写入任何内容
zai-python-helper use zai --dry-run
```

## 4. 验证

```bash
zai-python-helper status
```

`status` 会显示检测到的 provider、当前激活的模型模式、区域，以及它改动过的解析路径。
如果哪里不对：

```bash
zai-python-helper doctor
```

`doctor` 会运行完整诊断，并报告第一个失败的检查项及修复提示。

## 5. 回退

```bash
zai-python-helper use default
```

恢复激活时记录的原 Anthropic 配置。如果某个键在激活后被外部编辑过，`use default`
**拒绝覆盖它**，并指引你查看日志 —— 参见
[架构 → ADR-004](../ARCHITECTURE.md)。

## 区域

支持两个区域（参见[模型模式](guide/modes.md)）：

| 区域 | 端点 |
|------|------|
| `global`（默认） | `https://api.z.ai/api/anthropic` |
| `china` | `https://api.zai.cn/api/anthropic` |

```bash
zai-python-helper use zai --region china
```

---

就这些。接下来：[四种模型选择模式](guide/modes.md)，或者如果你更想直接调用规划器，
看[可导入 API](guide/importable.md)。
