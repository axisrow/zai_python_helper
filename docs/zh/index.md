# zai_python_helper

> 基于 MIT 协议的 **clean-room**（净室重写）Python helper，通过修改配置文件将
> **Claude Code** 接入 **Z.ai GLM Coding Plan** —— 无需后台服务，无需二进制文件。
> 设计上**可导入**（将规划核心当作库使用）且**无交互（headless）**（每个操作都是一个
> CLI 标志）。

<!-- TODO(i18n): review — tagline tone, native polish -->

## 这是什么

`zai-python-helper` 通过编辑三个文件，把 Claude Code（以及其他编码 agent）指向
[Z.ai](https://z.ai) 的 GLM Coding Plan 端点：

- `~/.claude/settings.json` —— Claude Code 读取的模型 + base URL
- `~/.claude.json` —— Claude Code 的内部状态
- `~/.zshrc` —— shell 环境变量（写入一个受标记围栏管理的自有区块 ——
  你已有的内容绝不会被删除，参见 [架构 → ADR-003](../ARCHITECTURE.md)）

它**不是**代理，不是守护进程，也不是 Z.ai 专有 `@z_ai/coding-helper`（npm）的 fork。
它是一次独立的重新实现，匹配该工具的*可观测行为* —— 从零重写并以 MIT 协议发布。
参见[一致性（Parity）](parity.md)，了解哪些是逐字节克隆、哪些是我们自己的扩展。

## 为什么

- **无守护进程。** 一次性 CLI。运行、退出、无需挂念。
- **可回退。** 一个[所有权日志（ownership journal）](../ARCHITECTURE.md)在我们改动
  每个键之前记录其原值，因此 `use default` 能精确恢复原先的内容 —— 并且拒绝覆盖
  被外部改动过的键。
- **原子化。** 多文件 `PatchPlan` 先校验，再在进程锁下用原子重命名应用（ADR-005）。
  两个并发的 `use` 调用会串行化；崩溃的运行会在下次调用时前滚完成。
- **可导入。** 规划核心（`plan_zai`、`plan_default`、`postconditions`）和领域类型
  （`ProviderSpec`、`ModelMode`、`Region`）都是纯函数，可以从你自己的 Python 代码中
  调用。公开接口是带版本的
  [`__all__`](../api/zai_python_helper.md) 契约。

## 30 秒安装

```bash
pip install zai-python-helper
```

然后把 Claude Code 切换到 Z.ai：

```bash
# 模式 1（原始）：只设置 ANTHROPIC_BASE_URL → Z.ai，由服务端决定模型
zai-python-helper use zai

# 提供 Z.ai 认证令牌（或导出 ZAI_API_KEY）
zai-python-helper use zai --api-key "$ZAI_API_KEY"
```

切回默认的 Anthropic 配置：

```bash
zai-python-helper use default
```

只读的可观测性与诊断：

```bash
zai-python-helper status    # 当前激活了什么、在哪里、以及解析到的路径
zai-python-helper doctor    # 端到端诊断集成情况
zai-python-helper list      # 可用的 Z.ai 模型预设
```

!!! tip "想要库而不是 CLI？"
    CLI 能做的一切，你都可以在进程内完成。参见
    [可导入 API → 指南](guide/importable.md)。

## 接下来去哪

- **[快速开始](quickstart.md)** —— 5 分钟搞定 `use zai` / `use default`。
- **[模型模式](guide/modes.md)** —— 选择模型的四种方式。
- **[可导入 API](guide/importable.md)** —— 核心价值主张：用 Python 规划 + 应用。
- **[CLI 参考](cli-reference.md)** —— 每个命令和标志。
- **[API 参考](../api/zai_python_helper.md)** —— 从代码自动生成。
- **[一致性](parity.md)** —— 哪些镜像原工具、哪些是我们自己的。
