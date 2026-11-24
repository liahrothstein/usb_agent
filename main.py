"""Главный цикл агента.

Агент подключается к Chrome (через CDP), читает активную вкладку
и применяет подсказки-инъекции в DOM. При необходимости эскалирует
вопрос в облачную LLM (OpenRouter/Gemini).

Два режима запуска:
    python main.py            — polling: каждые N сек опрашивает активную
                                вкладку, пропуская неизменившийся контент.
    python main.py --daemon   — резидентный демон: встроенный глобальный
                                хоткей (Ctrl+Alt+Q по умолчанию, через
                                Win32 RegisterHotKey + ctypes) + HTTP-триггер
                                на http://127.0.0.1:8765/trigger. По триггеру
                                принудительно обрабатывает активную вкладку
                                (игнорирует хэш). Chrome и llama-server
                                не перезапускаются — их поднимает run.bat
                                один раз, дальше живут.
                                Хоткей не требует ярлыков на рабочем столе
                                и внешних программ — слушатель встроен в
                                сам демон и регистрируется по virtual-key
                                code (не зависит от раскладки клавиатуры).
                                Переопределение:
                                  config.json → agent.hotkey = "<ctrl>+<alt>+q"
                                  CLI → --hotkey "<ctrl>+<shift>+f12"
                                Windows-only; на других ОС — только HTTP-триггер.

Остановка: Ctrl+C в консоли или stop.bat / ./stop.sh
"""
import argparse
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import ctypes  # stdlib; нужен для Win32 RegisterHotKey (встроенный хоткей)

# --- Встроенный глобальный хоткей через Win32 RegisterHotKey (ctypes) ---
#
# Раньше тут был pynput.keyboard.GlobalHotKeys, но он сопоставляет клавиши
# по СИМВОЛУ: при активной русской раскладке физическая клавиша Q генерирует
# 'й', и зарегистрированный '<ctrl>+<alt>+q' (char='q') не совпадает —
# хоткей молча не срабатывал. RegisterHotKey регистрирует по VIRTUAL KEY
# CODE (VK_Q=0x51), который не зависит от языка ввода — та же физическая
# клавиша срабатывает при любой раскладке. Pure ctypes (stdlib), без
# внешних зависимостей, едет с папкой. Windows-only; на других ОС демон
# работает только по HTTP-триггеру (trigger.bat).
_HOTKEY_ID = 1
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000
_MOD_FLAGS = {
    'ctrl': 0x0002, 'control': 0x0002,
    'alt': 0x0001, 'menu': 0x0001, 'option': 0x0001,
    'shift': 0x0004,
    'win': 0x0008, 'cmd': 0x0008, 'super': 0x0008, 'meta': 0x0008,
}

if sys.platform == 'win32':
    from ctypes import wintypes
    _user32 = ctypes.WinDLL('user32', use_last_error=True)
    # Прототипы обязательны: без них ctypes считает всё int — на 64-битном
    # Windows указатели (HWND/WPARAM/LPARAM) обрезаются до 32 бит, и
    # RegisterHotKey/GetMessageW/PostThreadMessageW тихо ломаются.
    _user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                       wintypes.UINT, wintypes.UINT]
    _user32.RegisterHotKey.restype = wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.UnregisterHotKey.restype = wintypes.BOOL
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                    wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.GetMessageW.restype = wintypes.BOOL
    _user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM]
    _user32.PostThreadMessageW.restype = wintypes.BOOL
    _WIN32_AVAILABLE = True
else:
    _user32 = None
    _WIN32_AVAILABLE = False


def _parse_hotkey_vk(hotkey_str):
    """Парсит '<ctrl>+<alt>+q' в (mods_flag, vk_code).

    Возвращает (mods, vk): mods — int (битовая маска MOD_*), vk — int
    virtual-key code, или None если основная клавиша не распознана.
    Layout-independent: основная клавиша превращается в VK, не в символ.
    Формат совместим с pynput (для config.json): модификаторы в <скобках>
    через +, основная клавиша — буква/цифра/f-ключ/special.
    """
    parts = [p.strip().lower().strip('<>') for p in hotkey_str.split('+')]
    mods = 0
    vk = None
    _special = {'space': 0x20, 'spacebar': 0x20, 'enter': 0x0D,
                'return': 0x0D, 'tab': 0x09, 'esc': 0x1B, 'escape': 0x1B,
                'home': 0x24, 'end': 0x23, 'insert': 0x2D, 'delete': 0x2E,
                'del': 0x2E, 'pgup': 0x21, 'pgdn': 0x22,
                'left': 0x25, 'up': 0x26, 'right': 0x27, 'down': 0x28}
    for tok in parts:
        if not tok:
            continue
        if tok in _MOD_FLAGS:
            mods |= _MOD_FLAGS[tok]
        elif tok in _special:
            vk = _special[tok]
        elif len(tok) == 1 and tok.isalpha():
            vk = ord(tok.upper())  # 'a'..'z' -> 0x41..0x5A
        elif len(tok) == 1 and tok.isdigit():
            vk = ord(tok)  # '0'..'9' -> 0x30..0x39
        elif tok[:1] == 'f' and tok[1:].isdigit():
            n = int(tok[1:])
            if 1 <= n <= 24:
                vk = 0x6F + n  # F1=0x70 .. F24=0x87
    return mods, vk


def _hotkey_thread_loop(mods, vk, trigger_event, ready_event, result):
    """Поток с Win32 message pump: регистрирует глобальный хоткей и качает
    WM_HOTKEY. RegisterHotKey с hWnd=NULL доставляет сообщение в
    регистрировавший поток — поэтому тут нужен собственный GetMessageW
    loop. По WM_HOTKEY выставляет trigger_event. При shutdown основной
    поток шлёт WM_QUIT через PostThreadMessageW(tid) — GetMessageW
    возвращает 0, цикл выходит, hotkey unregister'ится.
    """
    ok = _user32.RegisterHotKey(None, _HOTKEY_ID, mods | MOD_NOREPEAT, vk)
    result['registered'] = bool(ok)
    result['err'] = ctypes.get_last_error() if not ok else 0
    ready_event.set()
    if not ok:
        return
    msg = wintypes.MSG()
    while True:
        ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if ret <= 0:  # 0 = WM_QUIT, -1 = ошибка
            break
        if msg.message == WM_HOTKEY and msg.wParam == _HOTKEY_ID:
            trigger_event.set()
    _user32.UnregisterHotKey(None, _HOTKEY_ID)

from cdp_client import CDPClient
from llm_local import LocalLLM
from cloud import GeminiClient
from cloud_openrouter import OpenRouterClient
from injector import apply_hints
from prompts import SYSTEM, USER_TEMPLATE, ESCALATION_TEMPLATE, CLOUD_SYSTEM, CLOUD_USER_TEMPLATE


# --- helpers ---

def load_config() -> dict:
    cfg_path = Path(__file__).parent / "config.json"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def setup_logging(log_file: str):
    log_path = Path(__file__).parent / log_file
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def read_task(task_file: str) -> str:
    """Читает текущую задачу из task.txt. Если файла нет — задача 'general'.

    task.txt содержит шапку с примерами (JS/SQL/Next.js) — берём ТОЛЬКО
    секцию после последнего маркера 'Текущая задача:', иначе этот мусор
    летит в промпт облаку как поле {task}.
    """
    p = Path(__file__).parent / task_file
    if not p.exists():
        return "(задача не указана — отредактируй task.txt)"
    txt = p.read_text(encoding="utf-8").strip()
    if not txt:
        return "(задача пуста — отредактируй task.txt)"
    marker = "Текущая задача:"
    idx = txt.rfind(marker)
    if idx != -1:
        task = txt[idx + len(marker):].strip()
        return task or txt
    return txt


def _repair_json_string(s: str) -> str:
    """Чинит типичные ошибки LLM в JSON-тексте.

    1) Сырые управляющие символы (newline/tab) внутри строковых значений —
       JSON их не допускает. Межтокенный whitespace в JSON взаимозаменяем,
       так что глобальная замена на пробел безопасна.
    2) Незэкранированные бэкслеши в LaTeX/коде (\\pi, \\sqrt, \\frac, \\d) —
       \\p, \\s — невалидные JSON-эскейпы → JSONDecodeError. Удваиваем
       бэкслеш, если за ним не идёт структурно-валидный escape-символ
       (" \\ / u). \\f/\\b/\\n/\\r/\\t тоже удваиваем: в ответах теста
       form-feed/backspace никогда не подразумеваются, а \\frac/\\pi
       должны остаться литералом (иначе \\f → form-feed и find_text не
       совпадёт со страницей). Этот ремонт запускается ТОЛЬКО когда прямой
       json.loads уже упал, так что валидный JSON не затрагивается.
    """
    s = s.replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
    s = re.sub(r'\\(?!["\\/u])', lambda m: '\\\\', s)
    return s


def parse_llm_json(text: str) -> dict:
    """Достаёт первый JSON-объект из ответа LLM (может быть в код-блоках).

    Три уровня:
    1. Прямой json.loads.
    2. Ремонт (_repair_json_string) — сырые control chars и незэкранированные
       бэкслеши (\\pi, \\sqrt в ответах с LaTeX/кодом), потом json.loads.
    3. Обрезанный/битой JSON — _recover_hints извлекает hint-объекты по одному
       (балансировка скобок, per-object парсинг): один битой объект не губит
       остальные; поддерживает и background_tint, и placeholder_hint.
    """
    if not text:
        return {}
    _log = logging.getLogger("usb_agent")
    t = text.strip()
    if t.startswith("```"):
        # убираем строки-ограждения
        lines = t.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        t = "\n".join(lines)
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    candidate = t[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    repaired = _repair_json_string(candidate)
    if repaired != candidate:
        try:
            obj = json.loads(repaired)
            _log.info("parse_llm_json: JSON починен (control chars / escapes), %d hints",
                      len(obj.get("hints", [])) if isinstance(obj, dict) else 0)
            return obj
        except json.JSONDecodeError:
            pass
    recovered = _recover_hints(t[start:])
    if recovered.get("hints"):
        _log.info("Восстановлено %d hints из битого JSON", len(recovered["hints"]))
    return recovered


def _recover_hints(text: str) -> dict:
    """Извлекает полные hint-объекты из обрезанного/битого JSON по одному.

    Балансирует фигурные скобки (с учётом строк и вложенного params),
    парсит каждый объект отдельно — один битой объект не губит остальные.
    Поддерживает оба формата: background_tint (target_index) и placeholder_hint
    (params.value, без target_index), любой порядок ключей.
    """
    hints = []
    arr_start = text.find("[")
    if arr_start == -1:
        scan_from, scan_to = 0, len(text)
    else:
        scan_from = arr_start + 1
        arr_end = text.find("]", arr_start)
        scan_to = arr_end if arr_end != -1 else len(text)

    i = scan_from
    n = scan_to
    while i < n:
        b = text.find("{", i, n)
        if b == -1:
            break
        depth = 0
        j = b
        in_str = False
        esc = False
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if depth != 0:
            break  # незбалансированный (обрезано) — дальше нет смысла
        obj_str = text[b:j + 1]
        obj = None
        try:
            obj = json.loads(obj_str)
        except json.JSONDecodeError:
            try:
                obj = json.loads(_repair_json_string(obj_str))
            except json.JSONDecodeError:
                obj = None
        if isinstance(obj, dict) and "action" in obj:
            if "find_text" not in obj:
                obj["find_text"] = ""
            if "target_index" not in obj:
                obj["target_index"] = None
            hints.append(obj)
        i = j + 1
    if hints:
        return {"hints": hints, "reasoning": "(восстановлено: %d hints)" % len(hints)}
    return {}


def _is_meaningful_answer(val: str) -> bool:
    """True, если value — реальный ответ, а не заглушка модели.

    Ловит не только голое '...', но и заглушки с буквами: 'выражение = ...',
    'вызов(...)' (модель оставила дыру), и явные отказы
    'Нет данных' / 'не знаю'. Такие hint'ы дропаются → вопрос уходит во 2-й
    проход; если модель и там не сможет — поле остаётся пустым (честный
    пропуск лучше мусора в placeholder).
    """
    if not val:
        return False
    s = val.strip()
    if not s:
        return False
    # Подстрока-дыра внутри value: '...', '…' (присваивание с дырой, вызов с дырой)
    if "..." in s or "…" in s:
        return False
    low = s.lower()
    # Явный отказ модели (точное совпадение)
    if low in {"нет данных", "нет ответа", "не знаю", "no data", "неизвестно",
               "нет значения", "no answer", "н/д", "n/a", "нет", "нет ответ"}:
        return False
    # Короткие заглушки / слова-заполнители
    junk = {"???", "?", "<ответ>", "<...>", "xxx", "xx", "ответ", "твой ответ",
            "placeholder", "---", "___", "na", "none", "null", "-"}
    if low in junk:
        return False
    # только пунктуация без букв и цифр (????, ----, ____)
    if not re.search(r'[A-Za-zА-Яа-я0-9]', s):
        return False
    return True


def _is_echo_of_question(value, qtext) -> bool:
    """True, если value — эхо текста вопроса (модель не нашла ответ и вернула
    сам вопрос/метку). Ловит таймер-метки типа 'Оставшееся время', попавшие в
    test_structure как text-вопрос: под forced-answer модель эхо-возвращает их.
    Сравнение после нормализации пробелов/NBSP; для коротких меток (<=40 симв.)
    ловит также случай, когда value — префикс вопроса или наоборот.
    """
    if not value or not qtext:
        return False
    v = re.sub(r'\s+', ' ', str(value).replace('\u00a0', ' ')).strip()
    q = re.sub(r'\s+', ' ', str(qtext).replace('\u00a0', ' ')).strip()
    if not v or not q:
        return False
    if v == q:
        return True
    if len(v) >= 4 and len(q) <= 40 and (q.startswith(v) or v.startswith(q)):
        return True
    return False


def _ensure_statement_terminator(val: str) -> str:
    """Добавляет ';' к value, если это похоже на оператор/выражение
    C-подобного языка, а не на значение.

    Модель часто теряет ';' на операторах (присваивание, вызов функции),
    а без неё оператор не скомпилируется. Значения (константы, числа,
    идентификаторы) — не операторы, их не трогаем. Универсально: не зависит
    от конкретного языка теста.
    """
    if not val:
        return val
    v = val.strip()
    if not v or v[-1] in (';', '}', '{'):
        return v
    # Признаки оператора C-подобного языка: присваивание (=) или вызов
    # функции (заканчивается на ')'). Значения/константы их не имеют.
    looks_stmt = ('=' in v) or v.endswith(')')
    if looks_stmt:
        return v + ';'
    return v


def deduplicate_hints(hints: list, test_structure_json: str) -> list:
    """Группирует hints по question_index, валидирует target_index против
    options вопроса, ограничивает radio=1, checkbox<=2 (или 3), text=1.

    - question_index из test_structure (1-based, в DOM-порядке).
    - radio/checkbox: hint = action=background_tint + target_index (index
      варианта из options вопроса). target_index проверяется по диапазону
      [0, len(options)); миграции НЕТ — индекс имеет смысл только в своём
      вопросе, спутать с другим вопросом невозможно.
    - Текстовые вопросы (type=text): hint = action=placeholder_hint +
      params.value, без target_index. question_index обязателен.
    - Нормализация текста варианта (для конвертации placeholder→tint и
      восстановления find_text→target_index): NBSP→пробел, ·/∙/×→*,
      пробелы схлопываются.
    """
    if not hints:
        return hints

    _log = logging.getLogger("usb_agent")

    try:
        questions = json.loads(test_structure_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        questions = []

    if not questions:
        # Нет структуры — только дедупликация по (question_index, target_index)
        seen = set()
        result = []
        for h in hints:
            qi = h.get("question_index")
            ti = h.get("target_index")
            ft = h.get("find_text", "")
            key = (qi, ti, ft)
            if key not in seen:
                seen.add(key)
                result.append(h)
        return result

    # Логируем test_structure один раз за процесс
    if not getattr(deduplicate_hints, '_logged_structure', False):
        for q_idx_show in [0, 1, 2]:
            if q_idx_show < len(questions):
                q_show = questions[q_idx_show]
                _log.info("test_structure Q%d: type=%s, qtext=%r, options=%s",
                          q_idx_show + 1,
                          q_show.get("type"),
                          q_show.get("question_text", "")[:120],
                          [o.get("text") for o in q_show.get("options", [])][:3])
        deduplicate_hints._logged_structure = True

    def _norm(s):
        s = (s or '').replace('\u00a0', ' ')
        # Разные символы умножения → * (для fuzzy-сравнения вариантов)
        s = s.replace('·', '*').replace('∙', '*').replace('×', '*')
        return re.sub(r'\s+', ' ', s).strip()

    # options_norm: нормализованные тексты; options_by_norm: norm(text)→index
    for q in questions:
        opts = q.get("options", []) or []
        q["options_norm"] = [_norm(o.get("text", "")) for o in opts]
        q["options_by_norm"] = {_norm(o.get("text", "")): o.get("index")
                                for o in opts}

    # question_index вне диапазона 1..N — отбрасываем (защита от галлюцинаций)
    n_questions = len(questions)
    valid_hints = []
    rejected = 0
    for h in hints:
        qi = h.get("question_index")
        if qi is not None and (not isinstance(qi, int) or qi < 1 or qi > n_questions):
            rejected += 1
            continue
        valid_hints.append(h)
    if rejected:
        _log.warning("  Отброшено %d hints с question_index вне диапазона 1..%d",
                     rejected, n_questions)
    hints = valid_hints

    question_hints = {}  # qi → list of hints
    unmatched = []
    for h in hints:
        qi = h.get("question_index")
        action = h.get("action", "")
        params = h.get("params", {}) or {}
        value = params.get("value", "")
        ti = h.get("target_index")
        ft = _norm(h.get("find_text", ""))

        is_text_hint = (action == "placeholder_hint" and bool(value) and ti is None)
        is_tint_hint = (action == "background_tint" and qi is not None)

        if is_text_hint:
            # Заглушки ('...', '???', пусто) — не ответы: дропаем.
            if not _is_meaningful_answer(value):
                _log.info("  Q%s: text-hint пропущен — value=%r (заглушка)", qi, value)
                continue
            if qi is None:
                unmatched.append(h)
                continue
            q = questions[qi - 1] if 1 <= qi <= n_questions else None
            if q and _is_echo_of_question(value, q.get("question_text", "")):
                _log.info("  Q%s: text-hint пропущен — value эхо текста вопроса (%r)",
                          qi, str(value)[:50])
                continue
            if q and q.get("type") == "text":
                h["question_index"] = qi
                question_hints.setdefault(qi, []).append(h)
                continue
            # Модель вернула placeholder_hint (формат текста) для radio/checkbox,
            # но value — правильный вариант. Конвертируем в background_tint +
            # target_index (index варианта, чей текст = value).
            if q:
                val_norm = _norm(value)
                idx = (q.get("options_by_norm", {}) or {}).get(val_norm)
                if idx is not None:
                    h["action"] = "background_tint"
                    h["target_index"] = idx
                    h["find_text"] = ""
                    h["params"] = {}
                    question_hints.setdefault(qi, []).append(h)
                    _log.info("  Конвертирован placeholder→tint Q%d: index=%s",
                              qi, idx)
                    continue
                # value не сопоставлен с вариантом — мигрируем на ближайший
                # text-вопрос (текстовый ответ переносится; индекс — нет).
                text_qi = next((i for i, qq in enumerate(questions, 1)
                                if qq.get("type") == "text"), None)
                if text_qi:
                    _log.info("  Перемещён text-hint Q%d→Q%d", qi, text_qi)
                    h["question_index"] = text_qi
                    question_hints.setdefault(text_qi, []).append(h)
                else:
                    unmatched.append(h)
                continue
            unmatched.append(h)
            continue

        if is_tint_hint:
            q = questions[qi - 1] if 1 <= qi <= n_questions else None
            if not q:
                unmatched.append(h)
                continue
            opts = q.get("options", []) or []
            n_opts = len(opts)
            # target_index задан — проверяем диапазон. Миграции НЕТ.
            if ti is not None:
                if (not isinstance(ti, int)) or ti < 0 or ti >= n_opts:
                    _log.info("  Q%d: target_index=%s вне диапазона [0,%d) — дроп",
                              qi, ti, n_opts)
                    continue
                h["target_index"] = ti
                question_hints.setdefault(qi, []).append(h)
                continue
            # target_index не задан — восстановление по тексту правильного
            # варианта. Модель часто кладёт его не в find_text, а в params.value,
            # answer, text или selected — подтягиваем как find_text.
            if not ft:
                ft = _norm(value or h.get("answer") or h.get("text")
                           or h.get("selected") or "")
            if ft:
                idx = (q.get("options_by_norm", {}) or {}).get(ft)
                if idx is None:
                    # частичное совпадение (модель сократила/опечатка)
                    for o in opts:
                        ot = _norm(o.get("text", ""))
                        if ot and (ot == ft or (len(ft) > 2 and len(ot) < 500
                                   and (ot.find(ft) != -1 or ft.find(ot) != -1))):
                            idx = o.get("index")
                            break
                if idx is not None:
                    h["target_index"] = idx
                    h["find_text"] = ""
                    question_hints.setdefault(qi, []).append(h)
                    continue
            _log.info("  Q%d: tint-hint без target_index/find_text — дроп (raw=%s)",
                      qi, str(h)[:200])
            continue

        # Ни text, ни tint (странный формат или локальный UX-hint с find_text
        # без question_index) — пропускаем как есть в unmatched.
        unmatched.append(h)

    # radio → 1, checkbox → ≤2 (если выбраны почти все) или ≤3, text → 1
    result = []
    for qi, q_hints in sorted(question_hints.items()):
        q_idx = qi - 1 if qi else -1
        q = questions[q_idx] if 0 <= q_idx < n_questions else {}
        qtype = q.get("type", "")
        q_options = q.get("options", []) or []
        # Дедупликация внутри вопроса по target_index (до cap) — checkbox
        # может выдать дубли одного варианта после голосования.
        seen_ti = set()
        deduped = []
        for h in q_hints:
            ti = h.get("target_index")
            if ti is not None:
                if ti in seen_ti:
                    continue
                seen_ti.add(ti)
            deduped.append(h)
        if len(deduped) < len(q_hints):
            _log.info("  Q%d: дедуп по target_index %d→%d",
                      qi, len(q_hints), len(deduped))
        q_hints = deduped
        if qtype == "text" and len(q_hints) > 1:
            _log.info("  text Q%d: оставлен 1 из %d hints", qi, len(q_hints))
            q_hints = q_hints[:1]
        elif qtype == "radio" and len(q_hints) > 1:
            _log.info("  radio Q%d: оставлен 1 из %d hints", qi, len(q_hints))
            q_hints = q_hints[:1]
        elif qtype == "checkbox":
            if len(q_options) >= 3 and len(q_hints) >= len(q_options) - 1:
                _log.info("  checkbox Q%d: выбраны %d из %d — оставляю 2",
                          qi, len(q_hints), len(q_options))
                q_hints = q_hints[:2]
            elif len(q_hints) > 3:
                _log.info("  checkbox Q%d: оставлено 3 из %d hints", qi, len(q_hints))
                q_hints = q_hints[:3]
        result.extend(q_hints)

    result.extend(unmatched)
    # Гарантируем ';' на операторах C-подобных языков — модель часто его
    # теряет, а без неё оператор не скомпилируется. Значения/константы не трогаем.
    for h in result:
        if h.get("action") == "placeholder_hint" and h.get("params", {}).get("value"):
            h["params"]["value"] = _ensure_statement_terminator(h["params"]["value"])
    return result


def aggregate_hints_by_voting(samples, test_structure_json):
    """Self-consistency: из N выборок облака выбираем наиболее частый
    ответ на каждый вопрос. Усредняет случайность модели на нестабильных
    вопросах (на разных прогонах один и тот же вопрос может давать разные
    ответы — голосование берёт большинство).

    samples: список списков hints (по одному на выборку).
    Возвращает единый список hints-победителей голосования.
    """
    log = logging.getLogger("usb_agent")
    # question_index -> type, чтобы различать radio/checkbox/text
    qtype = {}
    try:
        for q in json.loads(test_structure_json):
            qtype[q.get("question_index")] = q.get("type", "")
    except Exception:
        pass

    def _vkey(s):
        # нормализация ключа голоса для text-value: схлопываем пробелы + lower,
        # чтобы «2, 3» и «2,3» совпадали. Для radio/checkbox нормализация не
        # нужна — голосуем по целочисленному target_index.
        return re.sub(r"\s+", " ", (s or "")).strip().lower()

    def _vote_key(h):
        # Ключ голоса: radio/checkbox → ("i", target_index) — int, без
        # нормализации (индекс однозначен). text → ("t", norm(value)).
        # Разные форматы — разные префиксы, чтобы text-value не смешивалось
        # с индексом. Без target_index/value — None (вопрос идёт в fallback).
        ti = h.get("target_index")
        if ti is not None and h.get("action") == "background_tint":
            try:
                return ("i", int(ti))
            except (TypeError, ValueError):
                return None
        v = (h.get("params") or {}).get("value") or ""
        if v:
            return ("t", _vkey(v))
        return None

    # qi -> {key: hint} — первый hint для ключа (для реконструкции)
    by_qi_key = {}
    # qi -> {key: count} — число выборок, проголосовавших за ключ
    votes = {}
    # qi -> первый hint БЕЗ распознанного ответа (fallback: вопрос не теряется)
    nokey_by_qi = {}
    seen_qi = []
    seen_set = set()
    for sample in samples:
        # одна выборка = один голос на каждый ключ (дубли внутри выборки не накручивают)
        per_sample = {}
        for h in sample:
            qi = h.get("question_index")
            if qi is None:
                continue
            if qi not in seen_set:
                seen_set.add(qi)
                seen_qi.append(qi)
            k = _vote_key(h)
            if k is None:
                # странный формат (нет ни target_index, ни params.value) —
                # запоминаем как fallback, чтобы вопрос не выпал целиком.
                nokey_by_qi.setdefault(qi, h)
                continue
            per_sample.setdefault(qi, set()).add(k)
            by_qi_key.setdefault(qi, {}).setdefault(k, h)
        for qi, keys in per_sample.items():
            for k in keys:
                votes.setdefault(qi, {}).setdefault(k, 0)
                votes[qi][k] += 1

    total = sum(len(s) for s in samples)
    log.info("  голосование: %d hints в выборках, %d вопросов с распознанным "
             "ответом, %d без (fallback)", total, len(votes), len(nokey_by_qi))

    n = len(samples)
    majority = max(2, (n + 1) // 2)  # ceil(n/2), минимум 2
    result = []
    for qi in seen_qi:
        kv = votes.get(qi, {})
        if kv:
            ordered = sorted(kv.items(), key=lambda x: (-x[1], x[0]))
            t = qtype.get(qi, "")
            if t == "checkbox":
                # варианты, за которые проголосовало большинство выборок;
                # если ни один не набрал большинства — берём топ-1 (лучше, чем пусто).
                winners = [k for k, c in ordered if c >= majority] or [ordered[0][0]]
            else:
                # radio / text: ровно один победитель (наиболее частый).
                winners = [ordered[0][0]]
            for k in winners:
                h = by_qi_key.get(qi, {}).get(k)
                if h:
                    result.append(dict(h))
        elif qi in nokey_by_qi:
            # hint был, но ответ не распознан — отдаём как есть; dedup ниже
            # попытается валидировать/конвертировать (или отбросит мусор).
            result.append(dict(nokey_by_qi[qi]))
    return result


def answer_unanswered_via_cloud(cloud_client, hints_applied, test_structure_json,
                                consistency_samples=1, temperature=None):
    """Второй проход через облако: для вопросов, на которые облако не дало
    hints в первом запросе, отправляем отдельный компактный запрос.

    При consistency_samples>1 делает N выборок с температурой и голосует
    большинством (как первый проход) — усредняет случайность модели на
    нестабильных вопросах. Каждая выборка прогоняется через dedup до
    голосования, чтобы восстановить question_index у hints из битого JSON.

    Использует существующий cloud-клиент с теми же настройками.
    Возвращает список валидированных hints (с question_index).
    """
    log = logging.getLogger("usb_agent")

    if not test_structure_json or test_structure_json == "[]":
        return []

    try:
        structure = json.loads(test_structure_json)
    except Exception:
        return []

    # Вопрос считается отвеченным, если к нему применён хотя бы один hint —
    # по question_index, а НЕ по тексту варианта. У разных вопросов могут
    # быть одинаковые варианты (например Q11 и Q47 оба содержат
    # 'if(x>5) z=a+3 else z=a-2;'): текстовый матч ложно считает второй
    # отвеченным, и он выпадает из второго прохода, оставаясь без подсказки.
    answered_qi = {h.get("question_index") for h in hints_applied
                   if h.get("question_index") is not None}

    unanswered = []
    for q in structure:
        qi = q.get("question_index")
        if qi in answered_qi:
            continue
        # text-вопрос или radio/checkbox с вариантами — есть что отвечать;
        # radio/checkbox без вариантов пропускаем (нечего подсвечивать).
        if q.get("type") == "text" or q.get("options"):
            unanswered.append(q)

    if not unanswered:
        log.info("Все вопросы отвечены облаком — второй проход не нужен")
        return []

    log.info("Пропущено вопросов: %d — отправляю в облако повторно", len(unanswered))
    for q in unanswered:
        opts_txt = "; ".join(
            "%s:%s" % (o.get("index"), (o.get("text", "") or "")[:40])
            for o in (q.get("options") or [])[:8])
        log.info("  пропуск Q%s type=%s q=%r opts=[%s]",
                 q.get("question_index"), q.get("type", ""),
                 (q.get("question_text", "") or "")[:140], opts_txt[:240])

    compact = []
    for i, q in enumerate(unanswered, 1):
        compact.append({
            "question_index": q.get("question_index", i),
            "type": q.get("type", "radio"),
            "question": q.get("question_text", "")[:300],
            "options": q.get("options", []),
        })
    questions_json = json.dumps(compact, ensure_ascii=False, indent=2)

    system_msg = (
        "Ты — помощник для решения учебных тестов и задач. "
        "Ниже вопросы, на которые ты не ответил в предыдущем запросе. "
        "Для КАЖДОГО выбери правильный ответ:\n"
        "- radio: РОВНО ОДИН наиболее вероятный вариант\n"
        "- checkbox: ТОЛЬКО правильные варианты (если не уверен — 1-2 наиболее "
        "вероятных, НИКОГДА не выбирай ВСЕ варианты)\n"
        "- radio/checkbox: верни hint с action=background_tint и target_index = "
        "index правильного варианта (поле index из options вопроса).\n"
        "- type=text (options пустой): верни ОДИН hint с action=placeholder_hint и "
        "params.value=КОНКРЕТНЫЙ ответ на этот вопрос (НЕ один шаблон для всех). "
        "Отвечай на ЯЗЫКЕ теста: если вопрос про программирование — используй "
        "синтаксис, видимый в тексте вопроса и вариантов; не выдумывай "
        "конструкции, которых в тесте нет. Если ответ — оператор/выражение "
        "языка, где точка с запятой обязательна (C/C++/Java/JS/C# и подобные), "
        "ставь её; значения, константы и модификаторы — без неё. "
        "Если в вопросе нет конкретики (переменных/чисел) — "
        "поищи их в тексте страницы рядом с вопросом. Если просится общий вид — "
        "дай шаблон. НЕ используй '...'/'…'/'???'/'<ответ>'/'Нет данных'/пусто "
        "как value — лучше пропусти вопрос. question_index обязателен.\n"
        "Формат: JSON {\"hints\": [{\"question_index\": 1, \"action\": \"background_tint\", "
        "\"target_index\": 0}, "
        "{\"question_index\": 7, \"action\": \"placeholder_hint\", \"params\": {\"value\": \"42\"}}]}"
    )
    user_msg = (f"Пропущенные вопросы:\n\n{questions_json}\n\n"
                "Верни JSON с hints для каждого вопроса. Не пропускай ни один.")

    try:
        if consistency_samples > 1:
            log.info("Облако 2-й проход: ×%d выборок (temp=%s)",
                     consistency_samples, temperature)
            samples = []
            raw_text = "(self-consistency %d выборок)" % consistency_samples
            for i in range(consistency_samples):
                try:
                    r = cloud_client.ask(user_msg, system=system_msg,
                                         temperature=temperature)
                    p = parse_llm_json(r["text"])
                    hh = p.get("hints", []) if p else []
                    # dedup ДО голосования: восстанавливает question_index для
                    # hints из компактного запроса (модель иногда не пишет qi,
                    # restore битого JSON теряет) — иначе они выпадают.
                    if hh:
                        hh = deduplicate_hints(hh, test_structure_json)
                    samples.append(hh)
                    log.info("  2-й проход выборка %d/%d: %d hints",
                             i + 1, consistency_samples, len(hh))
                except Exception as se:
                    log.warning("  2-й проход выборка %d/%d не удалась: %s",
                                i + 1, consistency_samples, se)
                    samples.append([])
            new_hints = aggregate_hints_by_voting(samples, test_structure_json)
            log.info("Облако 2-й проход: голосование → %d hints", len(new_hints))
        else:
            cloud_resp = cloud_client.ask(user_msg, system=system_msg)
            raw_text = cloud_resp["text"]
            parsed = parse_llm_json(raw_text)
            new_hints = parsed.get("hints", []) if parsed else []
    except Exception as e:
        log.warning("Второй проход облака не удался: %s", e)
        return []

    if not new_hints:
        log.warning("Облако во 2-м проходе не вернуло hints (длина: %d, начало: %.200s)",
                    len(raw_text) if isinstance(raw_text, str) else 0,
                    str(raw_text)[:200])
        return []

    def _norm(s):
        s = (s or '').replace('\u00a0', ' ')
        # Разные символы умножения → * (для сопоставления value→вариант)
        s = s.replace('·', '*').replace('∙', '*').replace('×', '*')
        return re.sub(r'\s+', ' ', s).strip()

    validated = []
    valid_indices = {q.get("question_index") for q in unanswered}
    text_qs = {q.get("question_index"): q for q in unanswered if q.get("type") == "text"}
    q_by_qi = {q.get("question_index"): q for q in unanswered}
    for h in new_hints:
        qi = h.get("question_index")
        action = h.get("action", "")
        params = h.get("params", {}) or {}
        value = params.get("value", "")
        ti = h.get("target_index")
        ft = _norm(h.get("find_text", ""))

        # Текстовый hint: placeholder_hint. value может быть пустым — тогда
        # модель решила не отвечать; логируем точно, не путаем с tint-дропом.
        is_text = (action == "placeholder_hint")
        if is_text:
            if not _is_meaningful_answer(value):
                log.warning("  Облако-2: Q%s placeholder_hint без ответа "
                            "(value=%r) — пропущен", qi, str(value)[:60])
                continue
            q_echo = q_by_qi.get(qi)
            if q_echo and _is_echo_of_question(value, q_echo.get("question_text", "")):
                log.warning("  Облако-2: Q%s placeholder_hint — value эхо текста "
                            "вопроса (%r) — пропущен", qi, str(value)[:60])
                continue
            if qi in text_qs:
                validated.append({
                    "question_index": qi,
                    "action": "placeholder_hint",
                    "params": {"value": _ensure_statement_terminator(value)}
                })
            else:
                # placeholder_hint для radio/checkbox — конвертируем в tint,
                # если value совпадает с вариантом → его target_index.
                q_match = q_by_qi.get(qi)
                val_norm = _norm(value)
                idx = None
                if q_match:
                    for o in (q_match.get("options", []) or []):
                        if _norm(o.get("text", "")) == val_norm:
                            idx = o.get("index")
                            break
                if idx is not None:
                    validated.append({
                        "question_index": qi,
                        "action": "background_tint",
                        "target_index": idx,
                        "params": {}
                    })
                    log.info("  Облако-2: конвертирован placeholder→tint Q%d: index=%s",
                             qi, idx)
                else:
                    log.warning("  Облако-2: placeholder_hint для не-text Q%d "
                                "(val=%r) — пропущен", qi, str(value)[:60])
            continue

        # tint hint: background_tint + target_index (или find_text для восстановления).
        if qi not in valid_indices:
            if ti is not None or ft:
                log.warning("  Облако-2: неизвестный question_index=%r (ti=%r, ft=%r)",
                            qi, ti, ft[:60])
            continue
        q_match = q_by_qi.get(qi)
        if not q_match:
            continue
        opts = q_match.get("options", []) or []
        n_opts = len(opts)

        # target_index задан — проверяем диапазон. Миграции НЕТ.
        if ti is not None:
            try:
                ti_i = int(ti)
            except (TypeError, ValueError):
                log.warning("  Облако-2: Q%d target_index=%r не int — пропущен", qi, ti)
                continue
            if ti_i < 0 or ti_i >= n_opts:
                log.warning("  Облако-2: Q%d target_index=%s вне диапазона [0,%d) — пропущен",
                            qi, ti, n_opts)
                continue
            validated.append({
                "question_index": qi,
                "action": "background_tint",
                "target_index": ti_i,
                "params": {}
            })
            continue

        # target_index не задан — восстановление по тексту правильного варианта.
        # Модель часто кладёт его не в find_text, а в params.value, answer, text
        # или selected — подтягиваем как find_text перед сопоставлением.
        if not ft:
            ft = _norm(value or h.get("answer") or h.get("text")
                       or h.get("selected") or "")
        if ft:
            idx = None
            for o in opts:
                if _norm(o.get("text", "")) == ft:
                    idx = o.get("index")
                    break
            if idx is None:
                # частичное совпадение (модель сократила/опечатка)
                for o in opts:
                    ot = _norm(o.get("text", ""))
                    if ot and (ot == ft or (len(ft) > 2 and len(ot) < 500
                               and (ot.find(ft) != -1 or ft.find(ot) != -1))):
                        idx = o.get("index")
                        break
            if idx is not None:
                validated.append({
                    "question_index": qi,
                    "action": "background_tint",
                    "target_index": idx,
                    "params": {}
                })
                continue
            log.warning("  Облако-2: Q%d не сопоставлен find_text=%r", qi, ft[:80])
            continue

        log.warning("  Облако-2: Q%d hint без target_index/find_text — пропущен (raw=%s)",
                    qi, str(h)[:200])

    log.info("Облако 2-й проход: %d hints → %d после валидации",
             len(new_hints), len(validated))
    return validated


def hash_text(s: str) -> str:
    return hashlib.md5(s.encode("utf-8", errors="ignore")).hexdigest()


# --- clients setup (общий для polling и daemon) ---

def _setup_clients(cfg: dict) -> dict:
    """Создаёт local/cloud/cdp клиенты и возвращает их в dict.

    Общая часть main() и daemon_main(): проверка ключа облака, создание
    LocalLLM / GeminiClient / OpenRouterClient / CDPClient. Логирование
    cloud_enabled / provider_name делаем здесь один раз.
    """
    log = logging.getLogger("usb_agent")
    chrome_cfg = cfg["chrome"]
    local_cfg = cfg["local_llm"]
    cloud_cfg = cfg["cloud"]
    agent_cfg = cfg["agent"]

    cloud_enabled = agent_cfg.get("enable_cloud_escalation", True)
    api_key = cloud_cfg.get("api_key", "")
    if not api_key or "PASTE_YOUR" in api_key:
        log.warning(
            "API ключ облака не задан в config.json — эскалация отключена. "
            "Агент будет работать только на локальной модели."
        )
        cloud_enabled = False

    local = LocalLLM(
        base_url=local_cfg["base_url"],
        model=local_cfg["model"],
        timeout=local_cfg["timeout_seconds"],
    )

    provider = cloud_cfg.get("provider", "gemini")
    if provider == "openrouter":
        cloud = OpenRouterClient(
            api_key=api_key,
            model=cloud_cfg["model"],
            use_search=cloud_cfg.get("use_google_search", True),
            timeout=cloud_cfg["timeout_seconds"],
        ) if cloud_enabled else None
        provider_name = "OpenRouter"
    else:
        cloud = GeminiClient(
            api_key=api_key,
            model=cloud_cfg["model"],
            use_search=cloud_cfg.get("use_google_search", True),
            timeout=cloud_cfg["timeout_seconds"],
        ) if cloud_enabled else None
        provider_name = "Gemini"

    cdp = CDPClient(host=chrome_cfg.get("host", "localhost"),
                    port=chrome_cfg["debug_port"])

    # Новая фича: cloud-запросы через fetch() браузера (CDP Runtime.evaluate),
    # не через Python urllib. cdp подключается к вкладке внутри process_one_tab;
    # тогда cloud.ask() шлёт POST через cdp.fetch_json(). Если вкладка пропала и
    # cdp не подключён — клиент сам откатывается на urllib (cdp.fetch_json не
    # вызовется). Атрибут ставим после создания cdp, т.к. cloud строится раньше.
    if cloud is not None and hasattr(cloud, "cdp"):
        cloud.cdp = cdp
        logging.getLogger("usb_agent").info(
            "Cloud-запросы: через fetch() браузера (CDP Runtime.evaluate), не urllib."
        )

    return {
        "local": local,
        "cloud": cloud,
        "cdp": cdp,
        "provider_name": provider_name,
        "cloud_enabled": cloud_enabled,
        "cfg": cfg,
    }


def _wait_local_llm(local, base_url: str):
    """Ждёт готовности локальной LLM — llama-server грузит модель ~20-30 сек,
    а run.bat ждёт всего 6. Без этой проверки первый запрос падает с
    WinError 10061 (connection refused).
    """
    log = logging.getLogger("usb_agent")
    log.info("Ожидание готовности локальной LLM на %s ...", base_url)
    for _ in range(60):  # до 120 сек
        if local.is_alive():
            break
        time.sleep(2)
    if local.is_alive():
        log.info("Локальная LLM готова.")
    else:
        log.warning("Локальная LLM не отвечала 120 сек — продолжаю в фоне.")


# --- один цикл обработки вкладки (общий для polling и daemon) ---

def process_one_tab(clients: dict, state: dict, force: bool = False) -> bool:
    """Один цикл обработки активной вкладки.

    clients — dict из _setup_clients: {local, cloud, cdp, provider_name, cfg, ...}
    state   — {"last_page_hash", "last_task_hash", "current_task"} персистентен
              между вызовами (один и тот же dict в main/daemon).
    force=True — игнорировать хэш страницы (ручной триггер по хоткею в
              daemon-режиме: пользователь явно хочет переобработать вкладку,
              даже если контент не изменился — например, переключился на
              другую вкладку, а pick_tab вернул её, и хэш у неё уже совпадает
              с прошлыи прогоном).
    Возвращает True, если страница была обработана; False, если пропущена
    (Chrome не отвечает, нет вкладок, контент не изменился и не force).
    """
    log = logging.getLogger("usb_agent")
    cdp = clients["cdp"]
    local = clients["local"]
    cloud = clients["cloud"]
    provider_name = clients["provider_name"]
    cfg = clients["cfg"]

    chrome_cfg = cfg["chrome"]
    local_cfg = cfg["local_llm"]
    cloud_cfg = cfg["cloud"]
    agent_cfg = cfg["agent"]

    poll = chrome_cfg["poll_interval_seconds"]
    max_chars = chrome_cfg["max_page_chars"]
    skip_urls = chrome_cfg.get("skip_urls", [])
    skip_if_no_change = agent_cfg.get("skip_if_no_change", True)
    cloud_first = cloud_cfg.get("cloud_first", False)
    consistency_samples = max(1, int(cloud_cfg.get("consistency_samples", 1) or 1))
    cloud_temperature = cloud_cfg.get("temperature")  # None = серверный дефолт

    # Проверка задачи (можно редактировать task.txt на лету)
    new_task = read_task(agent_cfg.get("task_file", "task.txt"))
    if hash_text(new_task) != state["last_task_hash"]:
        log.info("Задача обновлена: %s", new_task)
        state["current_task"] = new_task
        state["last_task_hash"] = hash_text(new_task)
        # при смене задачи — сбрасываем хэш страницы, чтобы сразу отработать
        state["last_page_hash"] = ""
    current_task = state["current_task"]

    # Подключение к Chrome
    if not cdp.is_chrome_up():
        log.info("Chrome не отвечает на порту %s — жду", chrome_cfg["debug_port"])
        return False

    tab = cdp.pick_tab(skip_prefixes=skip_urls)
    if not tab:
        pages = [t for t in cdp.list_tabs() if t.get("type") == "page"]
        if not pages:
            log.info("CDP отвечает, но page-вкладок нет. "
                     "Открой страницу в окне Chrome-агента.")
        else:
            log.info("Все %d page-вкладок пропущены по skip_urls %s:",
                     len(pages), skip_urls)
            for t in pages:
                u = (t.get("url") or "(нет url)")[:80]
                log.info("  - %s", u)
            log.info("Открой реальную страницу (не chrome://, не about:) "
                     "в окне Chrome-агента.")
        return False
    if not cdp.connect(tab=tab):
        log.info("Вкладка найдена, но WebSocket не открылся: %s",
                 (tab.get("url") or "")[:80])
        return False

    page = cdp.get_page_text(max_chars=max_chars)
    selection = cdp.get_selection()
    log.info("Вкладка: %s | %s (текст %d симв., выделение %d симв.)",
             (page.get("title") or "")[:60],
             (page.get("url") or "")[:80],
             len(page.get("text") or ""),
             len(selection or ""))

    # Контент стабилен — пропускаем (если не force — триггер по хоткею
    # всегда переобрабатывает, даже если страница та же).
    phash = hash_text(page.get("text", "") + "|" + selection)
    if not force and skip_if_no_change and phash == state["last_page_hash"]:
        log.info("Контент не изменился с прошлого цикла — пропускаю")
        cdp.close()
        return False

    if not page.get("text", "").strip():
        log.info("Текст страницы пуст — пропускаю (возможно, страница ещё грузится)")
        cdp.close()
        return False

    parsed = None
    test_structure = "[]"  # заполнится в cloud_first блоке; нужно для deduplicate_hints
    if cloud_first and cloud:
        # Структурированное описание теста: тип (radio/checkbox) +
        # варианты для каждого вопроса. Облако видит innerText без
        # HTML-структуры — без этой карты оно не отличает radio от
        # checkbox и выдумывает текст вариантов.
        try:
            test_structure = cdp.get_test_structure()
        except Exception as e:
            log.warning("get_test_structure недоступно (%s) — облако работает без структуры", e)
            test_structure = "[]"
        if test_structure and test_structure != "[]":
            try:
                _ts = json.loads(test_structure)
                log.info("test_structure: %d вопросов", len(_ts))
                if _ts:
                    q0 = _ts[0]
                    log.info("  Q1: type=%s, qtext=%r, options_count=%d",
                             q0.get("type"),
                             q0.get("question_text", "")[:80],
                             len(q0.get("options", [])))
            except Exception:
                pass
        cloud_msg = CLOUD_USER_TEMPLATE.format(
            task=current_task,
            url=page.get("url", ""),
            title=page.get("title", ""),
            max_chars=max_chars,
            page_text=page["text"],
            test_structure=test_structure,
        )
        try:
            if consistency_samples > 1:
                # Self-consistency: N выборок с температурой → голосование
                log.info("Облако (%s): анализирую страницу ×%d (temp=%s)...",
                         provider_name, consistency_samples, cloud_temperature)
                samples_hints = []
                last_reasoning = ""
                for i in range(consistency_samples):
                    try:
                        r = cloud.ask(cloud_msg, system=CLOUD_SYSTEM,
                                      temperature=cloud_temperature)
                        p = parse_llm_json(r["text"])
                        raw_hints = p.get("hints", []) if p else []
                        # Прогон через dedup ДО голосования
                        if raw_hints and test_structure and test_structure != "[]":
                            hh = deduplicate_hints(raw_hints, test_structure)
                        else:
                            hh = raw_hints
                        samples_hints.append(hh)
                        if p and p.get("reasoning"):
                            last_reasoning = p["reasoning"]
                        log.info("  выборка %d/%d: %d hints",
                                 i + 1, consistency_samples, len(hh))
                    except Exception as se:
                        log.warning("  выборка %d/%d не удалась: %s",
                                    i + 1, consistency_samples, se)
                        samples_hints.append([])
                agg = aggregate_hints_by_voting(samples_hints, test_structure)
                parsed = {"hints": agg, "reasoning": last_reasoning}
                log.info("Облако: self-consistency %d выборок → %d hints "
                         "после голосования", consistency_samples, len(agg))
                if not agg:
                    log.warning("Облако вернуло ответ без hints — fallback на локальную LLM")
                    parsed = None
            else:
                log.info("Облако (%s): анализирую страницу...", provider_name)
                cloud_resp = cloud.ask(cloud_msg, system=CLOUD_SYSTEM)
                raw_text = cloud_resp["text"]
                parsed = parse_llm_json(raw_text)
                if parsed and parsed.get("hints"):
                    log.info("Облако: %s", parsed.get("reasoning", "")[:160])
                else:
                    log.warning("Облако вернуло ответ без hints — fallback на локальную LLM")
                    log.warning("Длина ответа: %d симв. Начало (500): %.500s",
                                len(raw_text), raw_text)
                    log.warning("Конец ответа (300): %.300s",
                                raw_text[-300:] if len(raw_text) > 300 else raw_text)
                    parsed = None
        except Exception as e:
            log.error("Облако не ответило: %s — fallback на локальную LLM", e)
            parsed = None

    # Локальная LLM: fallback (облако не сработало) или основной путь
    if not parsed:
        user_msg = USER_TEMPLATE.format(
            task=current_task,
            url=page.get("url", ""),
            title=page.get("title", ""),
            selection=selection or "(нет выделения)",
            max_chars=max_chars,
            page_text=page["text"],
        )
        try:
            resp = local.chat(
                system=SYSTEM, user=user_msg,
                temperature=local_cfg["temperature"],
                max_tokens=local_cfg["max_tokens"],
            )
        except Exception as e:
            log.error("Локальная LLM не ответила: %s", e)
            cdp.close()
            return False

        parsed = parse_llm_json(resp)
        if not parsed:
            log.warning("Не распарсил JSON из ответа LLM: %.200s", resp)
            cdp.close()
            return False

        log.info("LLM: %s", parsed.get("reasoning", "")[:160])

        # Эскалация в облако если локальная модель попросила
        if parsed.get("needs_external") and cloud:
            query = parsed.get("query", "")
            if query:
                log.info("Эскалация в %s: %s", provider_name, query[:160])
                try:
                    gem = cloud.ask(
                        query,
                        system="Отвечай кратко и по делу. Давай конкретные факты.",
                    )
                except Exception as e:
                    log.error("%s не ответил: %s", provider_name, e)
                    gem = None
                if gem:
                    if gem["sources"]:
                        src_str = "\n".join(
                            f"- {s['title']}: {s['uri']}"
                            for s in gem["sources"][:5]
                        )
                    else:
                        src_str = "(источников нет)"
                    esc_msg = ESCALATION_TEMPLATE.format(
                        external_text=gem["text"][:2000],
                        sources=src_str,
                    )
                    try:
                        esc_resp = local.chat(
                            system=SYSTEM, user=esc_msg,
                            temperature=local_cfg["temperature"],
                            max_tokens=local_cfg["max_tokens"],
                        )
                        esc_parsed = parse_llm_json(esc_resp)
                        if esc_parsed:
                            parsed = esc_parsed
                            log.info("LLM после эскалации: %s",
                                     parsed.get("reasoning", "")[:160])
                    except Exception as e:
                        log.error("LLM после эскалации не ответила: %s", e)

    # Применяем инъекции (общий для обоих путей)
    hints = parsed.get("hints", []) if parsed else []
    if hints and test_structure and test_structure != "[]":
        before = len(hints)
        hints = deduplicate_hints(hints, test_structure)
        if len(hints) < before:
            log.info("Дедупликация: %d → %d hints", before, len(hints))
    applied = 0
    if hints:
        applied = apply_hints(cdp, hints)
        log.info("Применено %d/%d подсказок", applied, len(hints))
    else:
        log.info("Подсказок нет")

    # Второй проход: вопросы, на которые облако не ответило в первом запросе.
    if cloud and test_structure and test_structure != "[]":
        extra = answer_unanswered_via_cloud(
            cloud, hints, test_structure,
            consistency_samples=consistency_samples,
            temperature=cloud_temperature)
        if extra:
            ex_applied = apply_hints(cdp, extra)
            log.info("Облако 2-й проход: применено %d/%d (всего %d)",
                     ex_applied, len(extra), applied + ex_applied)

    # Хэш обновляем ТОЛЬКО после успешной обработки страницы.
    # Если LLM не ответила или JSON не распарсился — на следующем
    # цикле та же страница будет обработана повторно.
    state["last_page_hash"] = phash

    cdp.close()
    return True


# --- HTTP trigger handler для daemon-режима ---

class _TriggerHandler(BaseHTTPRequestHandler):
    """Простой HTTP-сервер для приёма триггеров от trigger.bat (хоткей).

    POST /trigger — выставляет Event, по которому daemon просыпается и
                    обрабатывает активную вкладку (force=True).
    GET  /health  — проверка что демон жив (run.bat использует перед стартом
                    нового экземпляра, и если отвечает — шлёт /trigger).
    GET  /        — короткая справка.

    Логирование отключено (log_message silenced), чтобы не засорять
    agent.log HTTP-шумом на каждый запрос /health.
    """
    # атрибут выставляется снаружи: server.trigger_event = threading.Event()
    trigger_event = None

    def do_POST(self):
        if self.path == '/trigger':
            if self.trigger_event is not None:
                self.trigger_event.set()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'triggered\n')
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'ok\n')
        elif self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'USB Agent daemon. POST /trigger to process active tab.\n')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


# --- main: polling mode (прежний режим, без изменений в поведении) ---

def main():
    cfg = load_config()
    setup_logging(cfg["agent"].get("log_file", "agent.log"))
    log = logging.getLogger("usb_agent")

    clients = _setup_clients(cfg)
    local = clients["local"]
    cloud = clients["cloud"]
    cdp = clients["cdp"]
    provider_name = clients["provider_name"]

    chrome_cfg = cfg["chrome"]
    local_cfg = cfg["local_llm"]
    cloud_cfg = cfg["cloud"]
    agent_cfg = cfg["agent"]
    poll = chrome_cfg["poll_interval_seconds"]

    log.info("=== USB Agent запущен (polling mode) ===")
    log.info("Опрос каждые %s сек. Chrome порт: %s. Локальная LLM: %s",
             poll, chrome_cfg["debug_port"], local_cfg["base_url"])
    if cloud:
        log.info("Эскалация в %s включена (модель %s).",
                 provider_name, cloud_cfg["model"])
        if cloud_cfg.get("cloud_first", False):
            log.info("Режим cloud_first: облако решает первично, локальная LLM — fallback.")
    else:
        log.info("Эскалация в облако ВЫКЛЮЧЕНА.")
    log.info("Текущая задача: %s", read_task(agent_cfg.get("task_file", "task.txt")))
    log.info("Остановка: Ctrl+C или stop.bat / ./stop.sh")
    log.info("(Daemon mode: python main.py --daemon)")

    _wait_local_llm(local, local_cfg["base_url"])

    state = {
        "last_page_hash": "",
        "last_task_hash": "",
        "current_task": read_task(agent_cfg.get("task_file", "task.txt")),
    }

    while True:
        try:
            process_one_tab(clients, state, force=False)
            time.sleep(poll)
        except KeyboardInterrupt:
            log.info("Останавливаюсь...")
            try:
                cdp.close()
            except Exception:
                pass
            break
        except Exception as e:
            log.error("Непредвиденная ошибка: %s", e, exc_info=True)
            try:
                cdp.close()
            except Exception:
                pass
            time.sleep(poll)


# --- daemon mode: ждёт HTTP-триггер, по триггеру обрабатывает активную вкладку ---

def daemon_main(hotkey_override=None):
    cfg = load_config()
    setup_logging(cfg["agent"].get("log_file", "agent.log"))
    log = logging.getLogger("usb_agent")

    clients = _setup_clients(cfg)
    local = clients["local"]
    cloud = clients["cloud"]
    cdp = clients["cdp"]
    provider_name = clients["provider_name"]

    chrome_cfg = cfg["chrome"]
    local_cfg = cfg["local_llm"]
    cloud_cfg = cfg["cloud"]
    agent_cfg = cfg["agent"]
    trigger_port = int(agent_cfg.get("trigger_port", 8765))

    log.info("=== USB Agent запущен (daemon mode) ===")
    log.info("Chrome порт: %s. Локальная LLM: %s",
             chrome_cfg["debug_port"], local_cfg["base_url"])
    if cloud:
        log.info("Эскалация в %s включена (модель %s).",
                 provider_name, cloud_cfg["model"])
        if cloud_cfg.get("cloud_first", False):
            log.info("Режим cloud_first: облако решает первично, локальная LLM — fallback.")
    else:
        log.info("Эскалация в облако ВЫКЛЮЧЕНА.")
    log.info("Текущая задача: %s", read_task(agent_cfg.get("task_file", "task.txt")))

    _wait_local_llm(local, local_cfg["base_url"])

    # HTTP-триггер сервер
    trigger_event = threading.Event()
    handler_cls = type('_BoundTriggerHandler', (_TriggerHandler,),
                       {'trigger_event': trigger_event})
    try:
        server = HTTPServer(('127.0.0.1', trigger_port), handler_cls)
    except OSError as e:
        log.error("Не удалось занять порт %d для trigger-сервера: %s",
                  trigger_port, e)
        log.error("Возможно, демон уже запущен — шлём триггер существующему и выходим.")
        # Пытаемся дёрнуть уже запущенный демон через HTTP (run.bat делает то же).
        try:
            import urllib.request
            req = urllib.request.Request(f"http://127.0.0.1:{trigger_port}/trigger",
                                         method='POST', data=b'')
            urllib.request.urlopen(req, timeout=5)
            log.info("Триггер отправлен существующему демону.")
        except Exception as ee:
            log.error("И триггер не отправился: %s", ee)
        return

    threading.Thread(target=server.serve_forever, daemon=True).start()

    # Встроенный глобальный хоткей (Win32 RegisterHotKey через ctypes) —
    # слушает клавиатуру в фоне, не требует ярлыков на рабочем столе и
    # внешних программ. Хоткей и HTTP-триггер сосуществуют: оба выставляют
    # один и тот же trigger_event. Регистрация по virtual-key code —
    # раскладкаНЕважна (pynput тут не годился: он матчил по символу, и при
    # русской раскладке физическая Q -> 'й', хоткей молча не срабатывал).
    # config: agent.hotkey = "<ctrl>+<alt>+q" (формат pynput-совместимый).
    # CLI: --hotkey "<ctrl>+<shift>+f12" перекрывает config.
    hotkey_thread = None
    hotkey_str = hotkey_override or agent_cfg.get("hotkey", "<ctrl>+<alt>+q")
    if _WIN32_AVAILABLE:
        mods, vk = _parse_hotkey_vk(hotkey_str)
        if vk is None:
            log.warning("Хоткей %r: не распознал основную клавишу — "
                        "fallback на HTTP-триггер. Формат: '<ctrl>+<alt>+q', "
                        "'<ctrl>+<shift>+f12'.", hotkey_str)
        else:
            ready_event = threading.Event()
            result = {}
            hotkey_thread = threading.Thread(
                target=_hotkey_thread_loop,
                args=(mods, vk, trigger_event, ready_event, result),
                daemon=True)
            hotkey_thread.start()
            ready_event.wait(timeout=2.0)
            if result.get('registered'):
                log.info("Хоткей: %s (Win32 RegisterHotKey, VK=0x%02X) — "
                         "нажми для обработки активной вкладки. "
                         "РаскладкаНЕважна (регистрация по VK, не по символу).",
                         hotkey_str, vk)
            else:
                err = result.get('err', 0)
                log.warning("RegisterHotKey не удался (GetLastError=%d) для %r — "
                            "возможно, сочетание уже занято другой программой. "
                            "Fallback на HTTP-триггер (trigger.bat).",
                            err, hotkey_str)
                hotkey_thread = None
    else:
        log.warning("Win32 недоступен (sys.platform=%s) — встроенный хоткей "
                    "отключён. Только HTTP-триггер: trigger.bat или POST /trigger.",
                    sys.platform)

    log.info("Trigger: POST http://127.0.0.1:%d/trigger", trigger_port)
    log.info("Health:  GET  http://127.0.0.1:%d/health", trigger_port)
    log.info("Остановка: Ctrl+C или stop.bat / ./stop.sh")
    log.info("Жду триггер... (переключись на вкладку с тестом и нажми хоткей)")

    state = {
        "last_page_hash": "",
        "last_task_hash": "",
        "current_task": read_task(agent_cfg.get("task_file", "task.txt")),
    }

    # Цикл ожидания триггера.
    #
    # ВАЖНО: trigger_event.wait() БЕЗ таймаута на Windows блокирует основной
    # поток в C-level WaitForSingleObject(INFINITE) — Python-обработчик SIGINT
    # (Ctrl+C) не может прервать этот wait, потому что ему нужен GIL и
    # граница байткода, а основной поток застрял в C. Polling-режим раньше
    # останавливался по Ctrl+C только потому, что time.sleep(poll) сам по
    # себе прерывается сигналом. Здесь — ждём короткими порциями (0.5 сек):
    # каждые 0.5 сек основной поток возвращается в Python, и KeyboardInterrupt
    # может быть доставлен. Задержка 0.5 сек на старте обработки триггера
    # незаметна (человек нажимает хоткей осознанно, не спамит).
    while True:
        try:
            if not trigger_event.wait(timeout=0.5):
                continue  # таймаут — просто перепроверяем (даёт Ctrl+C шанс)
            trigger_event.clear()
            log.info("=== Триггер получен — обрабатываю активную вкладку ===")
            process_one_tab(clients, state, force=True)
            log.info("=== Готово — жду следующий триггер (Ctrl+Alt+Q или POST /trigger) ===")
        except KeyboardInterrupt:
            log.info("Останавливаюсь...")
            try:
                if hotkey_thread is not None and hotkey_thread.ident is not None:
                    _user32.PostThreadMessageW(hotkey_thread.ident, WM_QUIT, 0, 0)
                    hotkey_thread.join(timeout=1.0)
            except Exception:
                pass
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                cdp.close()
            except Exception:
                pass
            break
        except Exception as e:
            log.error("Ошибка при обработке триггера: %s", e, exc_info=True)
            try:
                cdp.close()
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="USB Agent")
    parser.add_argument("--daemon", action="store_true",
                        help="Резидентный режим: встроенный глобальный хоткей "
                             "(Win32 RegisterHotKey, по умолчанию Ctrl+Alt+Q) + HTTP-триггер на "
                             "127.0.0.1:8765/trigger (или trigger_port из config.json). "
                             "По триггеру обрабатывает активную вкладку (force=True). "
                             "Chrome и llama-server должны быть уже запущены run.bat.")
    parser.add_argument("--hotkey", default=None,
                        help="Глобальный хоткей в daemon-режиме, формат "
                             "'<ctrl>+<alt>+q', '<ctrl>+<shift>+f12'. "
                             "Перекрывает agent.hotkey из config.json. "
                             "Регистрируется по virtual-key code (раскладка-независимо).")
    args = parser.parse_args()
    if args.daemon:
        daemon_main(hotkey_override=args.hotkey)
    else:
        main()
