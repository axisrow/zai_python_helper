# Справочник CLI

`zai-python-helper` — одноразовый CLI: подкоманды argparse, каждая опция — флаг,
без интерактивного меню (запрос появляется только если отсутствует токен и не
заданы ни `--api-key`, ни `ZAI_API_KEY`).

## Глобальные флаги

Работают **до или после** подкоманды:

| Флаг | Эффект |
|------|--------|
| `--dry-run` | Показать, что изменилось бы; ничего не записывать. |
| `--debug` | Показать полный traceback Python при ошибке (вместо однострочного сообщения). |
| `-v`, `--version` | Вывести голую строку версии (формат совпадает с вышестоящим, см. [Parity](parity.md)). |
| `-h`, `--help` | Справка по команде. |

## `list`

Показать доступные пресеты моделей Z.ai.

```bash
zai-python-helper list
zai-python-helper list --format json
```

| Флаг | Значения | По умолчанию |
|------|----------|--------------|
| `--format` | `table`, `json` | `table` |

## `use zai`

Сделать Z.ai провайдером по умолчанию для Claude Code.

```bash
zai-python-helper use zai
zai-python-helper use zai --mode select --model glm-4-plus
zai-python-helper use zai --region china --api-key "$ZAI_API_KEY"
zai-python-helper use zai --dry-run
```

| Флаг | Значения / смысл | По умолчанию |
|------|------------------|--------------|
| `--mode` | `original`, `default`, `select`, `custom` | `original` |
| `--model` | идентификатор модели (для `select` или `custom`) | — |
| `--region` | `global`, `china` | `global` |
| `--api-key` | auth-токен Z.ai (иначе env `ZAI_API_KEY` / запрос) | — |
| `--name` | отображаемое имя (только `custom`) | — |
| `--description` | описание модели (только `custom`) | — |
| `--capabilities` | например `effort,thinking` (только `custom`) | — |

Что пишет каждый режим — см. [Режимы моделей](guide/modes.md).

## `use default`

Откатиться к конфигурации Anthropic по умолчанию. Восстанавливает предыдущие
значения, записанные при активации; отказывается затирать ключ, изменённый снаружи
(см. [Архитектура → ADR-004](../ARCHITECTURE.md)). Принимает те же флаги, что
`use zai` (безвредные no-op для отката), поэтому можно передать `--dry-run`.

```bash
zai-python-helper use default
```

## `status`

Наблюдаемость только для чтения: определённый провайдер, активный режим модели,
регион и разрешённые пути, к которым обращался инструмент.

```bash
zai-python-helper status
zai-python-helper status --region china
```

| Флаг | Значения | По умолчанию |
|------|----------|--------------|
| `--region` | `global`, `china` | `global` |

## `doctor`

Полная диагностика интеграции. Сообщает первую упавшую проверку с подсказкой;
одни WARN → выход `0`, FAIL → ненулевой выход.

```bash
zai-python-helper doctor
```

## Коды выхода

CLI пишет `error: <сообщение>` в stderr и выходит ненулёвым при любой ожидаемой
ошибке (конфигурация, провайдер, валидация). С `--debug` ошибка пробрасывается,
чтобы получить полный traceback. При нормальном ходе код выхода `0`.

## См. также

- [Быстрый старт](quickstart.md) — путь за пять минут.
- [Импортируемое API](guide/importable.md) — та же мощь, в процессе.
- [Справочник API](../api/zai_python_helper.md) — лежащая в основе библиотека.
