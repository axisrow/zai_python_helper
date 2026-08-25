# Быстрый старт

От нуля до «Claude Code говорит с Z.ai» за пять минут.

## 1. Установка

```bash
pip install zai-python-helper
```

Из исходников, с инструментарием документации:

```bash
git clone https://github.com/axisrow/zai_python_helper.git
cd zai_python_helper
pip install -e ".[docs]"
```

Проверка:

```bash
zai-python-helper --version
```

## 2. Получите auth-токен Z.ai

Возьмите токен в своём аккаунте [Z.ai](https://z.ai) (GLM Coding Plan). Экспортируйте
его, чтобы CLI его нашёл, либо передавайте через `--api-key`:

```bash
export ZAI_API_KEY="<ваш токен>"
```

## 3. Переключитесь на Z.ai

```bash
zai-python-helper use zai
```

Это правит `~/.claude/settings.json`, `~/.claude.json` и `~/.zshrc`, направляя
Claude Code на Anthropic-совместимый эндпоинт Z.ai. Предыдущие значения
записываются в журнал владения, чтобы можно было чисто откатиться.

Нужна конкретная модель? Выберите [режим](guide/modes.md):

```bash
# Выбрать пресет
zai-python-helper use zai --mode select --model glm-4-plus

# Или свой идентификатор модели
zai-python-helper use zai --mode custom --model "my-model" --name "My Model"

# Посмотреть, что изменится, ничего не записывая
zai-python-helper use zai --dry-run
```

## 4. Проверка

```bash
zai-python-helper status
```

`status` показывает определённого провайдера, активный режим модели, регион и
разрешённые пути, к которым обращался инструмент. Если что-то не так:

```bash
zai-python-helper doctor
```

`doctor` прогоняет полную диагностику и сообщает первую упавшую проверку с
подсказкой по исправлению.

## 5. Откат

```bash
zai-python-helper use default
```

Восстанавливает предыдущую конфигурацию Anthropic, записанную при активации.
Если ключ был изменён снаружи после активации, `use default` **отказывается его
затирать** и указывает на журнал — см. [Архитектура → ADR-004](../ARCHITECTURE.md).

## Регионы

Поддерживаются два региона (см. [Режимы моделей](guide/modes.md)):

| Регион | Эндпоинт |
|--------|----------|
| `global` (по умолчанию) | `https://api.z.ai/api/anthropic` |
| `china` | `https://open.bigmodel.cn/api/anthropic` |

```bash
zai-python-helper use zai --region china
```

---

Это всё. Дальше: [четыре режима выбора модели](guide/modes.md) или
[импортируемое API](guide/importable.md), если предпочитаете вызывать планировщик напрямую.
