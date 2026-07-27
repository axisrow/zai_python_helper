# 可导入 API

CLI 只是一个**纯规划核心**之上的薄壳。这个核心 —— 决定*改什么*的那些函数 ——
是可导入的、无副作用的。你可以规划一次 provider 切换、检查精确的文件 delta，
然后再决定是否应用。这就是 `zai_python_helper` 的核心价值主张：智能是库，
写入是由你控制的后端。

公开接口是带版本的
[`__all__`](../../api/zai_python_helper.md) 契约 —— 不在 `__all__` 里的一切都是内部的、
可能变动。

<!-- TODO(i18n): review — the value-proposition paragraph is the project's pitch; keep sharp -->

## 为什么导入它

- **应用之前无副作用。** `plan_zai` 读取已经加载的文档并返回一个 `PatchPlan`。
  什么都不写。
- **可检视。** `PatchPlan` 是一个有序的 `FileDelta` 列表，每个 delta 由语义化的
  `FileTag` 寻址，而不是裸路径。你可以记录它们、对它们做 diff、拒绝它们，或路由到
  别的后端。
- **可测试。** 纯函数进、`PatchPlan` 出。不需要文件系统 fixture，不需要 mock
  `open()`。
- **可组合。** 在 CLI 所用的同一个规划器之上构建你自己的 UX —— 一个 TUI、一个 web
  仪表盘、一个 CI 门禁。

## 规划一次 `use zai` 切换

```python
from zai_python_helper import (
    ProviderSpec, ModelMode, Region, plan_zai,
    JsonBackend, ShellBackend, Paths, base_url_for_region,
)

spec = ProviderSpec(
    base_url=base_url_for_region(Region.GLOBAL),
    model_mode=ModelMode.ORIGINAL,
)
paths = Paths.default()

plan = plan_zai(
    spec,
    Region.GLOBAL,
    settings_doc=JsonBackend.read(paths.claude_settings),
    claude_json_doc=JsonBackend.read(paths.claude_json),
    zshrc_text=ShellBackend.read(paths.zshrc),
    auth_token="<your Z.ai auth token>",
)

# `plan` 是一个 PatchPlan —— 一个有序的 FileDelta 列表。检视它：
for delta in plan.deltas:
    print(delta.tag, delta.kind)
```

`plan_default` 规划逆操作（`use default`）：

```python
from zai_python_helper import plan_default, ProviderSpec

plan = plan_default(spec, settings_doc=..., zshrc_text=...)
```

## 检查一次切换是否已经激活

`postconditions` 是一个纯谓词 —— 当且仅当文档已经反映一次激活的 `use zai` 时为真：

```python
from zai_python_helper import postconditions, Region

active = postconditions(Region.GLOBAL, settings_doc=..., zshrc_text=...)
```

## 应用计划

计划本身从不写入。通过 IO 后端应用它 —— 跟 CLI 用的同一个 —— 这样写入仍由你控制：

```python
# JsonBackend 原子地写入（写临时文件 + 重命名）。
# ShellBackend 添加/移除一个受标记围栏管理的自有区块（ADR-003）。
for delta in plan.deltas:
    delta.apply(...)  # 把每个 delta 路由到对应的后端
```

!!! note
    `FileDelta.apply` 的确切签名以及围绕它的日志记录，位于
    [API 参考](../../api/zai_python_helper.md)中。这里要强调的是它的形状：
    **规划是纯的、应用是 IO、两者都由你调用。**

## 你会用到的领域类型

| 名称 | 它是什么 |
|------|----------|
| `ProviderSpec` | 目标 provider：base URL + 模型模式。 |
| `ModelMode` | 枚举：`ORIGINAL`、`DEFAULT`、`SELECT`、`CUSTOM`。 |
| `Region` | 枚举：`GLOBAL`、`CHINA`。 |
| `PatchPlan` | 一个有序的 `FileDelta` 列表，描述一次完整激活。 |
| `FileDelta` | 单个文件的目标变更，由 `FileTag` 寻址。 |
| `FileTag` | 语义化文件 id（与其路径解耦）。 |
| `Paths` | 工具会改动的每个已解析文件系统路径的冻结集合。 |
| `JsonBackend` / `ShellBackend` | 原子 JSON 写入器 / 自有区块 shell 写入器。 |

完整列表 —— 包括所有权日志（`take_over`、`revert`）和状态检测
（`detect_status`、`render_status`）—— 参见
[API 参考](../../api/zai_python_helper.md)。它从源码自动生成，因此永远不会与你导入的
代码失配。

## 另见

- [API 参考](../../api/zai_python_helper.md) —— 每个签名，直接来自 `__all__`。
- [架构](../../ARCHITECTURE.md) —— 为什么存在 core/IO 分离（ADR-001），以及它如何
  保护未来的 proxy（ADR-002）。
