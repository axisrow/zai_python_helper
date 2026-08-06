# 贡献

`zai_python_helper` 基于 MIT 协议，欢迎贡献。本页是开发准备的简短版本；
[架构](../ARCHITECTURE.md)是设计部分的完整版本。

## 开发准备

```bash
git clone https://github.com/axisrow/zai_python_helper.git
cd zai_python_helper
pip install -e ".[dev,docs]"
```

`dev` = pytest、ruff、构建工具。`docs` = griffe + mkdocs 站点工具
（即[本站](https://axisrow.github.io/zai_python_helper/)）。

## 运行测试

```bash
make test            # pytest，排除 e2e
pytest -m "not e2e"  # 等价
```

端到端测试（标记为 `e2e`）是可选的。

## Lint

```bash
make lint    # ruff check .
ruff check . # 等价
```

## 保持文档同步

API 参考（`docs/api/zai_python_helper.md`）和 `llms.txt` 是**从源码自动生成的** ——
docstring 和类型标注是事实来源。如果提交的文档相对代码发生漂移，CI 会失败。

你改了 docstring 或签名之后：

```bash
make docs          # 重新生成 docs/api/ + llms.txt
git add docs/ llms.txt
```

`make docs` 目标是幂等的且排序的，因此 `git diff` 保持干净。

## 本地预览本站

```bash
mkdocs serve
```

打开打印出的 URL（默认 <http://127.0.0.1:8000>）。
[语言选择器](https://github.com/ultrabug/mkdocs-static-i18n)在
English / Русский / 中文 之间切换。

## 人类文档 i18n 规则

- **英文是事实来源**（`docs/en/`）。翻译到 `docs/ru/` 和 `docs/zh/`。
- **结构同步。** 一个 CI 检查验证 `ru/` 和 `zh/` 包含与 `en/` 相同的 `.md` 文件集合。
  如果你在 `en/` 加了一个页面，就在同一个 PR 里给 `ru/` 和 `zh/` 加上对应页面
  （哪怕是占位）。
- **不要盲目机翻。** 机翻草稿是一个可接受的起点，但任何需要母语者复查的地方都标记上
  `<!-- TODO(i18n): review -->`。

## 提交风格

使用 [Conventional Commits](https://www.conventionalcommits.org/) —— `feat:`、
`fix:`、`docs:` 等等。保持提交聚焦。

## 报告问题

在 [GitHub](https://github.com/axisrow/zai_python_helper/issues) 上开一个 issue。
对于安全敏感的报告，倾向于用私密渠道而不是公开 issue。
