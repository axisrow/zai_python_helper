# Контрибьютинг

`zai_python_helper` под MIT и открыт для контрибуций. Эта страница — короткая
версия dev-настройки; [Архитектура](../ARCHITECTURE.md) — длинная версия дизайна.

## Dev-настройка

```bash
git clone https://github.com/axisrow/zai_python_helper.git
cd zai_python_helper
pip install -e ".[dev,docs]"
```

`dev` = pytest, ruff, инструмент сборки. `docs` = griffe + инструментарий сайта
mkdocs ([этот сайт](https://axisrow.github.io/zai_python_helper/)).

## Запуск тестов

```bash
make test            # pytest, без e2e
pytest -m "not e2e"  # эквивалент
```

End-to-end тесты (метка `e2e`) — по желанию.

## Линт

```bash
make lint    # ruff check .
ruff check . # эквивалент
```

## Держите доку в синке

Справочник API (`docs/api/zai_python_helper.md`) и `llms.txt`
**автогенерируются из исходников** — docstring'и и аннотации типов это источник
истины. CI падает, если закоммиченная дока рассинхронизирована с кодом.

После изменения docstring'а или сигнатуры:

```bash
make docs          # регенерировать docs/api/ + llms.txt
git add docs/ llms.txt
```

Таргет `make docs` идемпотентен и отсортирован, поэтому `git diff` остаётся чистым.

## Локальный превью этого сайта

```bash
mkdocs serve
```

Откройте напечатанный URL (по умолчанию <http://127.0.0.1:8000>).
[Селектор языка](https://github.com/ultrabug/mkdocs-static-i18n) переключает
между English / Русский / 中文.

## Правила i18n человекочитаемой доки

- **Английский — источник истины** (`docs/en/`). Переводите в `docs/ru/` и
  `docs/zh/`.
- **Структура синхронна.** CI-чек проверяет, что `ru/` и `zh/` содержат тот же
  набор `.md`, что `en/`. Добавили страницу в `en/` — добавьте парную (хотя бы
  заглушку) в `ru/` и `zh/` в том же PR.
- **Не переводите вслепую машиной.** Машинный черновик — приемлемая база, но
  помечайте места для ревью носителем через `<!-- TODO(i18n): review -->`.

## Стиль коммитов

[Conventional Commits](https://www.conventionalcommits.org/) — `feat:`, `fix:`,
`docs:` и т.д. Держите коммиты сфокусированными.

## Заведение issue

Открывайте issue на [GitHub](https://github.com/axisrow/zai_python_helper/issues).
Для чувствительных к безопасности отчётов предпочтите приватный канал публичному
issue.
