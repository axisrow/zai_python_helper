# Режимы выбора модели

`use zai` может настроить Claude Code на разговор с Z.ai **четырьмя** способами.
Режим управляет тем, *какие* переменные окружения записываются и *насколько*
жёстко вы фиксируете модель.

| Режим | Что записывается | Когда использовать |
|-------|------------------|--------------------|
| **original** | только `ANTHROPIC_BASE_URL` → Z.ai | хотите, чтобы сервер сам выбирал модель (как в оригинальном `@z_ai/coding-helper`) |
| **default** | `ANTHROPIC_BASE_URL` + переменные `ANTHROPIC_DEFAULT_*_MODEL` | хотите автоматический выбор пресета Z.ai |
| **select** | base URL + конкретный пресет | хотите явный контроль над известным пресетом |
| **custom** | base URL + свой идентификатор модели + имя | бета-модели, кастомные развёртывания |

## original

Записывается только `ANTHROPIC_BASE_URL`, указывающий на эндпоинт Z.ai. Модель
выбирает сервер.

```bash
zai-python-helper use zai --mode original
```

Это ближайшее соответствие поведению вышестоящего инструмента и режим **по
умолчанию**, если `--mode` опущен.

## default

Пресеты Z.ai подключаются через переменные `ANTHROPIC_DEFAULT_*_MODEL`, поэтому
встроенное в Claude Code псевдонимирование моделей (opus/sonnet/haiku) автоматически
ложится на пресеты Z.ai.

```bash
zai-python-helper use zai --mode default
```

## select

Выберите конкретный пресет по имени. Запустите `list`, чтобы увидеть доступные
пресеты:

```bash
zai-python-helper list
```

```bash
zai-python-helper use zai --mode select --model glm-4-plus
```

## custom

Передайте свой идентификатор модели и отображаемое имя (и опционально описание
и capabilities). Полезно для бета-моделей или self-hosted эндпоинта за base URL
Z.ai.

```bash
zai-python-helper use zai \
  --mode custom \
  --model "my-custom-model" \
  --name "My Model" \
  --capabilities "effort,thinking"
```

## Эталон оригинала (`@z_ai/coding-helper` 0.0.7)

Black-box-тестирование оригинального пакета подтверждает: Claude Code **не
читает** массив `customModels`. Оригинал настраивает Claude Code переменными
окружения в `~/.claude/settings.json`:

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

Также он добавляет слиянием `"hasCompletedOnboarding": true` в
`~/.claude.json`, сохраняя несвязанные настройки. `customModels` — формат
Factory Droid, а не Claude Code. Безголовая последовательность оригинала:

```bash
chelper auth glm_coding_plan_global "$ZAI_API_KEY"
chelper auth reload claude
```

Первая команда проверяет и сохраняет токен в `~/.chelper/config.yaml`, вторая
применяет его к Claude Code. `<token>` в прозе — это placeholder в угловых
скобках, а не буквальный shell-синтаксис: неэкранированный `<token>` shell
воспримет как редирект ввода, поэтому реальное значение нужно передавать через
переменную окружения (`ZAI_API_KEY` выше), а не вставлять токен дословно.
`chelper enter claude-code` рассчитан на интерактивный TTY и не заменяет эту
последовательность в скрипте.

## Регионы

Любой режим принимает `--region`. Регион выбирает эндпоинт Z.ai:

- `global` (по умолчанию) — `https://api.z.ai/api/anthropic`
- `china` — `https://api.zai.cn/api/anthropic`

```bash
zai-python-helper use zai --mode select --model glm-4-plus --region china
```

## Разрешение токена

Auth-токен разрешается в таком порядке:

1. `--api-key` в командной строке
2. переменная окружения `ZAI_API_KEY`
3. интерактивный запрос (только если ничего из этого нет)

При автоматизации всегда передавайте `--api-key` или экспортируйте `ZAI_API_KEY`,
чтобы CLI не блокировался на запросе.

## См. также

- [Справочник CLI](../cli-reference.md) — все флаги.
- [Импортируемое API](importable.md) — управляйте `plan_zai` из Python.
