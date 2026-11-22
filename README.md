# USB Agent

Резидентный помощник для решения тестов в браузере. Демон слушает глобальный
хоткей **`Ctrl+Alt+Q`**, берёт активную вкладку Chrome и помечает правильные
варианты ответов (radio/checkbox) серой подсветкой, а в текстовые поля ввода
подставляет предполагаемый ответ в `placeholder`. Работает на любой вкладке,
при любой раскладке клавиатуры, без внешних программ и ярлыков.

## Возможности

- **Глобальный хоткей `Ctrl+Alt+Q` через Win32 `RegisterHotKey`** — регистрация
  по virtual-key code (`VK=0x51`), а не по символу. Та же физическая клавиша
  срабатывает при русской, английской или любой другой раскладке. Pure ctypes
  (Python stdlib) — без `pynput`, без AutoHotkey, без ярлыков на рабочем столе.
  Windows Defender не реагирует (`RegisterHotKey` — легитимный Win32 API, не
  keyboard hook).
- **Daemon-режим** — `run.bat` поднимает llama-server + Chrome + демон один раз
  за сессию. Демон висит резидентно и обрабатывает вкладки по триггеру (хоткей
  или HTTP-запрос), не перезапуская Chrome и llama-server между запусками.
- **Cloud-first с локальным fallback** — OpenRouter (`nvidia/nemotron-3-ultra`)
  решает первично, локальная Qwen2.5-3B — fallback если облако недоступно.
  Self-consistency: 3 выборки + голосование по `target_index`, 2-й проход для
  пропущенных вопросов.
- **HTTP-триггер** — `trigger.bat` (`POST /trigger`) как альтернатива хоткею:
  если сочетание занято другой программой или хочешь триггерить скриптом.
- **Chrome cold-start без потерь** — `run.bat` убивает только stale `chrome.exe`
  на нашем порту/профиле (личный Chrome не трогается), пересоздаёт профиль с
  `--no-first-run --no-default-browser-check`, проверяет что порт 9222 ответил.

## Быстрый старт

1. Скопировать `main.py`, `run.bat`, `trigger.bat` в `D:\Development\usb_agent\`
   (поверх существующих).
2. Запустить `run.bat` один раз при старте сессии:
   ```bat
   .\run.bat
   ```
   Он поднимет llama-server (порт 8080), Chrome (`--remote-debugging-port=9222`)
   и демон (`python main.py --daemon`).
3. Открыть тест в любой вкладке Chrome, переключиться на неё, нажать `Ctrl+Alt+Q`.

В консоли и `agent.log` появится:
```
=== Триггер получен — обрабатываю активную вкладку ===
Вкладка: Тест - C++ | file:///... (текст N симв., ...)
test_structure: N вопросов
...
Применено N/N подсказок
=== Готово — жду следующий триггер (Ctrl+Alt+Q или POST /trigger) ===
```

Остановка — `Ctrl+C` в консоли `run.bat` (или `stop.bat`).

## Как это работает

```
┌─────────┐   Ctrl+Alt+Q    ┌──────────┐  CDP (9222)   ┌────────┐
│ кнопка  │ ──────────────► │  daemon  │ ────────────► │ Chrome │
└─────────┘   RegisterHotKey│ main.py  │ ◄──────────── │ вкладка │
                            │  --daemon│   DOM + hints  └────────┘
                            └────┬─────┘
                  cloud_first    │
                                 ▼
              ┌──────────────────────────────────┐
              │  OpenRouter (nemotron) — primary  │
              │  3 выборки × self-consistency     │
              │  + 2-й проход для пропущенных     │
              └──────────────────────────────────┘
                          │ fallback
                          ▼
              ┌──────────────────────────────────┐
              │  llama-server Qwen2.5-3B (8080)   │
              └──────────────────────────────────┘
```

Демон берёт первую page-type вкладку из `/json` (Chrome отдаёт их в MRU-порядке —
обычно это текущий таб), парсит структуру теста, отправляет в облако с
self-consistency (3 выборки, голосование), применяет hints через CDP
(`background_tint` для radio/checkbox, `placeholder_hint` для текстовых полей).

## Файлы

| Файл | Назначение |
| --- | --- |
| `main.py` | Агент: `--daemon` режим, `process_one_tab(force=True)`, встроенный хоткей через Win32 `RegisterHotKey` (ctypes), HTTP-триггер на порту 8765, cloud-first с локальным fallback |
| `run.bat` | Лаунчер: проверяет `/health`, `/json/version`, `/trigger/health` перед стартом; поднимает llama-server + Chrome + демон; не убивает уже запущенные процессы; cold-start Chrome с kill stale + verify порта |
| `trigger.bat` | (опционально) `POST /trigger` на демон — альтернатива хоткею |
| `config.json` | Настройки: модель, порты, `agent.hotkey`, `agent.trigger_port`, режим эскалации |
| `task.txt` | Текущая задача (текст инструкции агенту); при смене `last_page_hash` сбрасывается автоматически |
| `agent.log` | Лог работы демона |

Подробно про daemon-режим и хоткей — в [README_daemon.md](README_daemon.md).

## Конфигурация

`config.json` (опциональные новые поля, обратно-совместимы):
```json
"agent": {
  "trigger_port": 8765,
  "hotkey": "<ctrl>+<alt>+q"
}
```

Формат хоткея (pynput-совместимый, но регистрируется по VK): модификаторы в
угловых скобках через `+`, буквенные/цифровые клавиши и F-ключи как есть.
Примеры: `"<ctrl>+<alt>+q"`, `"<ctrl>+<shift>+f12"`, `"<cmd>+<shift+p"`.

Смена хоткея (по приоритету): CLI `--hotkey` → `config.json` → по умолчанию
`Ctrl+Alt+Q`.

## Требования

- Windows 10/11 (Win32 `RegisterHotKey`, `cmd.exe`, `powershell`).
- Python 3.8+ с `websocket-client` (`pip install -r requirements.txt`).
- Google Chrome (стандартные пути установки).
- `llama\llama-server.exe` + `models\*.gguf` для локальной LLM.
- OpenRouter API ключ в `config.json` для cloud-first режима.

## Поведение при ошибках

| Ситуация | Что происходит |
| --- | --- |
| Хоткей не срабатывает | Проверь `agent.log` — если `RegisterHotKey не удался (GetLastError=...)`, сочетание занято. Смени через `config.json` или `--hotkey`. Fallback: `trigger.bat`. |
| Chrome упал | Демон пишет `Chrome не отвечает на порту 9222 — жду`, ждёт; поднимешь Chrome — следующий триггер сработает. |
| llama-server упал | В cloud_first-режиме облако работает без локальной; fallback на локальную пропускается. |
| `run.bat` запущен второй раз | Видит `/health`, шлёт `/trigger` сам и выходит — второй демон не поднимается. |
| Cold-start Chrome (не запущен) | Kill stale `chrome.exe` на порту 9222/профиле → nuke profile → `--no-first-run` → verify порта. Личный Chrome не трогается. |

## Откат

Старый `main.py` и `run.bat` верните из backup, запускайте `python main.py`
(без `--daemon`) как раньше. Polling-режим без изменений. Подробности — в
[README_daemon.md](README_daemon.md) → «Откат».
