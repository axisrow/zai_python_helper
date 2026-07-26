# Импортируемое API

CLI — тонкая оболочка над **чистым планирующим ядром**. Это ядро — функции,
решающие, *что* менять, — импортируемо и не имеет побочных эффектов. Можно
спланировать переключение провайдера, изучить точные файловые дельты и лишь
потом решить, применять ли их. В этом ценностное предложение `zai_python_helper`:
интеллект — это библиотека, а запись — это бэкенд, который вы контролируете.

Публичная поверхность — это версионный контракт
[`__all__`](../../api/zai_python_helper.md); всё, что не входит в `__all__`, —
внутреннее и может измениться.

## Зачем импортировать

- **Без побочных эффектов до применения.** `plan_zai` читает уже загруженные
  документы и возвращает `PatchPlan`. Ничего не записывается.
- **Инспектируемо.** `PatchPlan` — упорядоченный список `FileDelta`, каждый
  адресован семантическим `FileTag`, а не сырым путём. Их можно логировать,
  диффать, отклонять или направлять в другой бэкенд.
- **Тестируемо.** Чистые функции на входе, `PatchPlan` на выходе. Никаких
  фикстур файловой системы и моков `open()`.
- **Компонуемо.** Стройте свой UX — TUI, веб-дашборд, гейт в CI — поверх того же
  планировщика, что использует CLI.

## Спланировать переключение `use zai`

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
    auth_token="<ваш auth-токен Z.ai>",
)

# `plan` — это PatchPlan, упорядоченный список FileDelta. Изучим его:
for delta in plan.deltas:
    print(delta.tag, delta.kind)
```

`plan_default` планирует обратное (`use default`):

```python
from zai_python_helper import plan_default, ProviderSpec

plan = plan_default(spec, settings_doc=..., zshrc_text=...)
```

## Проверить, активно ли уже переключение

`postconditions` — чистый предикат: истина, если документы уже отражают
активный `use zai`:

```python
from zai_python_helper import postconditions, Region

active = postconditions(Region.GLOBAL, settings_doc=..., zshrc_text=...)
```

## Применить план

Сам план ничего не записывает. Применяйте его через IO-бэкенды — те же, что
использует CLI, — чтобы сохранить контроль над записями:

```python
# JsonBackend пишет атомарно (temp + rename).
# ShellBackend добавляет/удаляет собственный обрамлённый маркерами блок (ADR-003).
for delta in plan.deltas:
    delta.apply(...)  # направьте каждую дельту в соответствующий бэкенд
```

!!! note
    Точная сигнатура `FileDelta.apply` и журналирование вокруг неё описаны в
    [справочнике API](../../api/zai_python_helper.md). Здесь важна суть:
    **план чист, применение — это IO, и вы вызываете оба.**

## Доменные типы, до которых вы дотянетесь

| Имя | Что это |
|-----|---------|
| `ProviderSpec` | Целевой провайдер: base URL + режим модели. |
| `ModelMode` | Enum: `ORIGINAL`, `DEFAULT`, `SELECT`, `CUSTOM`. |
| `Region` | Enum: `GLOBAL`, `CHINA`. |
| `PatchPlan` | Упорядоченный список `FileDelta`, описывающий полную активацию. |
| `FileDelta` | Намеченное изменение одного файла, адресованное через `FileTag`. |
| `FileTag` | Семантический идентификатор файла (отвязан от пути). |
| `Paths` | Замороженный набор всех разрешённых путей, к которым обращается инструмент. |
| `JsonBackend` / `ShellBackend` | Атомарный JSON-писатель / писатель блоков оболочки. |

Полный список — включая журналирование владения (`take_over`, `revert`) и
определение статуса (`detect_status`, `render_status`) — см. в
[справочнике API](../../api/zai_python_helper.md). Он автогенерируется из
исходников, поэтому никогда не рассинхронизирован с импортируемым кодом.

## См. также

- [Справочник API](../../api/zai_python_helper.md) — все сигнатуры, напрямую из
  `__all__`.
- [Архитектура](../../ARCHITECTURE.md) — зачем нужно разделение core/IO (ADR-001)
  и как оно защищает будущий прокси (ADR-002).
