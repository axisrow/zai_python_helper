# CLI 参考

`zai-python-helper` 是一次性 CLI：argparse 子命令，每个选项都是标志，没有交互式菜单
（只有在缺少令牌、且既没传 `--api-key` 也没设置 `ZAI_API_KEY` 时才会出现提示）。

## 全局标志

这些在子命令**之前或之后**都生效：

| 标志 | 效果 |
|------|------|
| `--dry-run` | 预览将要发生的改动；不写入任何内容。 |
| `--debug` | 出错时显示完整的 Python traceback（而不是一行消息）。 |
| `-v`, `--version` | 打印裸版本字符串（与上游格式一致，参见[一致性](parity.md)）。 |
| `-h`, `--help` | 该命令的帮助。 |

## `list`

显示可用的 Z.ai 模型预设。

```bash
zai-python-helper list
zai-python-helper list --format json
```

| 标志 | 取值 | 默认 |
|------|------|------|
| `--format` | `table`, `json` | `table` |

## `use zai`

把 Z.ai 设为 Claude Code 的默认 provider。

```bash
zai-python-helper use zai
zai-python-helper use zai --mode select --model glm-4-plus
zai-python-helper use zai --region china --api-key "$ZAI_API_KEY"
zai-python-helper use zai --dry-run
```

| 标志 | 取值 / 含义 | 默认 |
|------|------------|------|
| `--mode` | `original`, `default`, `select`, `custom` | `original` |
| `--model` | 模型 id（用于 `select` 或 `custom`） | — |
| `--region` | `global`, `china` | `global` |
| `--api-key` | Z.ai 认证令牌（否则用 `ZAI_API_KEY` 环境变量 / 提示） | — |
| `--name` | 显示名称（仅 `custom` 模式） | — |
| `--description` | 模型描述（仅 `custom` 模式） | — |
| `--capabilities` | 例如 `effort,thinking`（仅 `custom` 模式） | — |

每种模式写入什么，参见[模型模式](guide/modes.md)。

## `use default`

回退到默认的 Anthropic 配置。恢复激活时记录的原值；拒绝覆盖被外部改动过的键
（参见 [架构 → ADR-004](../ARCHITECTURE.md)）。接受与 `use zai` 相同的标志
（对回退来说是无害的空操作），因此你可以传 `--dry-run`。

```bash
zai-python-helper use default
```

## `status`

只读的可观测性：检测到的 provider、当前激活的模型模式、区域，以及工具改动过的解析路径。

```bash
zai-python-helper status
zai-python-helper status --region china
```

| 标志 | 取值 | 默认 |
|------|------|------|
| `--region` | `global`, `china` | `global` |

## `doctor`

端到端诊断集成情况。报告第一个失败的检查项及修复提示；仅 WARN 时以 `0` 退出，
FAIL 则以非零值退出。

```bash
zai-python-helper doctor
```

## 退出码

CLI 在任何预期失败（配置错误、provider 错误、校验错误）时，向 stderr 打印
`error: <message>` 并以非零值退出。加上 `--debug` 它会重新抛出异常，让你看到完整的
traceback。正常运行时成功以 `0` 退出。

## 另见

- [快速开始](quickstart.md) —— 五分钟路径。
- [可导入 API](guide/importable.md) —— 同等的能力，在进程内完成。
- [API 参考](../api/zai_python_helper.md) —— 底层库。
