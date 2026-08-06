# 与 `@z_ai/coding-helper` 的一致性

`zai_python_helper` 是对 Z.ai 专有 `@z_ai/coding-helper`（npm）可观测行为的**独立净室
重写（clean-room reimplementation）**。它不共享该包的任何源码 —— 只共享 Claude Code
如何被配置去对接 GLM Coding Plan 的*可观测行为*，从零重写并以 MIT 协议发布。

我们用**两个阶段**跟踪一致性。

<!-- TODO(i18n): review — Phase 1/2 framing is core to the project; keep precise -->

## 阶段 1 —— 逐字节的行为一致性

目标：从 `@z_ai/coding-helper` 切换到 `zai_python_helper` 的用户看到**完全一致的
可观测行为**。我们精确克隆的内容：

- **`-v` / `--version` 格式。** 不带程序名前缀的裸版本字符串，与上游 `Commander
  .version()` 输出一致。（版本*号*不同 —— 但*格式*必须一致。）由一个 Docker 一致性
  测试验证，把我们的 CLI 表面与上游工具做 diff。
- **被修改的文件集合**以及改动的**形状**（`settings.json` / `.claude.json` 中的哪些
  键、哪些 shell 环境变量）。
- **四种模型选择模式**及其环境变量语义。

阶段 1 是**严格**的：漂移是 bug，不是 feature。如果原工具做 X，我们就做 X。

## 阶段 2 —— 我们的扩展（允许漂移）

阶段 1 锁定后，我们在其上叠加原工具没有的扩展。这里**失配是可接受的、有意的** ——
我们不再追求匹配原工具，而是在它之上改进。当前的阶段 2 扩展：

- **可导入核心。** 规划逻辑是一个库（`plan_zai`、`plan_default`、`postconditions`），
  而不仅是 CLI。原工具只有 CLI。参见[可导入 API](guide/importable.md)。
- **无交互（headless）运行。** 每个操作都是一个 CLI 标志；没有交互式菜单。
  原工具驱动一个交互式提示。
- **所有权日志。** 可回退、自失效的回退语义（ADR-004），取代原工具的永久 `.bak`
  快照。
- **多文件原子激活。** 一个经过校验的 `PatchPlan`，在进程锁下用原子重命名应用
  （ADR-005）。

## 我们如何验证阶段 1

一个 Docker 一致性镜像同时构建**两个**工具，一个测试对它们的 CLI 表面做 diff
（参见 `docker/parity/` 和 `tests/parity/`）。`-v`/`--version` 格式测试在 CI 中运行。
如果我们在某个阶段 1 表面上漂移，CI 就会变红。

## 小结

| 表面 | 阶段 1（克隆） | 阶段 2（扩展） |
|------|---------------|----------------|
| 版本格式 | ✅ 逐字节 | — |
| 修改的文件 + 键形状 | ✅ 完全一致 | — |
| 模型模式 | ✅ 完全一致 | — |
| 可导入 API | — | ✅ 我们自己的 |
| 无交互标志 | — | ✅ 我们自己的 |
| 所有权日志 | — | ✅ 我们自己的 |
| 原子 PatchPlan | — | ✅ 我们自己的 |

阶段 2 扩展背后的 ADR 参见[架构](../ARCHITECTURE.md)。
