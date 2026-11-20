"""Главный цикл агента.

Агент в фоне опрашивает активную вкладку Chrome (через CDP),
отправляет её содержимое локальной LLM (llama.cpp) и применяет
тонкие подсказки-инъекции в DOM. При необходимости эскалирует
вопрос в Gemini (с инструментом Google Search).

Запуск:
    python main.py

Остановка: Ctrl+C в консоли или stop.bat / ./stop.sh
"""
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

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
    Поддерживает оба формата: background_tint (find_text) и placeholder_hint
    (params.value, без find_text), любой порядок ключей.
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
            hints.append(obj)
        i = j + 1
    if hints:
        return {"hints": hints, "reasoning": "(восстановлено: %d hints)" % len(hints)}
    return {}


def _is_meaningful_answer(val: str) -> bool:
    """True, если value — реальный ответ, а не заглушка модели.

    Ловит не только голое '...', но и заглушки с буквами: 'd = Math::...',
    'z = ...', 'Math::...' (модель оставила дыру), и явные отказы
    'Нет данных' / 'не знаю'. Такие hint'ы дропаются → вопрос уходит во 2-й
    проход; если модель и там не сможет — поле остаётся пустым (честный
    пропуск лучше мусора в placeholder).
    """
    if not val:
        return False
    s = val.strip()
    if not s:
        return False
    # Подстрока-дыра внутри value: '...', '…' (d = Math::..., z = ..., f(...)  )
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


def _ensure_statement_terminator(val: str) -> str:
    """Добавляет ';' к value, если это оператор C++/CLI, а не значение.

    Модель часто теряет ';' на операторах (Console::WriteLine(...), z=...,
    k=...::Parse(...)), а без неё оператор не скомпилируется. Значения (const,
    3, 6_7_3, Math::E) — не операторы, их не трогаем.
    """
    if not val:
        return val
    v = val.strip()
    if not v or v[-1] in (';', '}', '{'):
        return v
    # Признаки оператора: присваивание (=), вызов Console::, преобразование Parse
    looks_stmt = ('=' in v
                  or v.startswith('Console::')
                  or '::Parse(' in v)
    if looks_stmt:
        return v + ';'
    return v


def deduplicate_hints(hints: list, test_structure_json: str) -> list:
    """Группирует hints по question_index, валидирует find_text против options,
    ограничивает radio=1, checkbox<=2 (или 3), text=1.

    - question_index из test_structure (1-based, в DOM-порядке).
    - find_text должен быть в options вопроса; если нет — перемещается в вопрос,
      где он есть (модель иногда путает похожие вопросы).
    - Текстовые вопросы (type=text): hint = action=placeholder_hint +
      params.value, без find_text. question_index обязателен; поле должно быть
      type=text, иначе перемещается на ближайший text-вопрос.
    - Нормализация: NBSP→пробел, Math::PI→π, ·/∙/×→*, пробелы схлопываются.
    """
    if not hints:
        return hints

    _log = logging.getLogger("usb_agent")

    try:
        questions = json.loads(test_structure_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        questions = []

    if not questions:
        # Нет структуры — только дедупликация по find_text
        seen = set()
        result = []
        for h in hints:
            ft = h.get("find_text", "")
            if ft and ft not in seen:
                seen.add(ft)
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
                          q_show.get("options", [])[:3])
        deduplicate_hints._logged_structure = True

    def _norm(s):
        s = (s or '').replace('\u00a0', ' ')
        # Модель часто пишет Math::PI вместо π; разные символы умножения → *
        s = re.sub(r'Math::PI', 'π', s, flags=re.IGNORECASE)
        s = re.sub(r'\bpi\b', 'π', s, flags=re.IGNORECASE)
        s = s.replace('·', '*').replace('∙', '*').replace('×', '*')
        return re.sub(r'\s+', ' ', s).strip()

    for q in questions:
        q["options_norm"] = [_norm(o) for o in q.get("options", [])]

    # question_index вне диапазона 1..N — отбрасываем (защита от галлюцинаций)
    n_questions = len(questions)
    valid_hints = []
    rejected = 0
    for h in hints:
        qi = h.get("question_index")
        if qi is not None and (qi < 1 or qi > n_questions):
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
        ft = _norm(h.get("find_text", ""))
        # Текстовый вопрос: action=placeholder_hint + params.value, без find_text.
        is_text_hint = (h.get("action") == "placeholder_hint"
                        and bool(h.get("params", {}).get("value", ""))
                        and not ft)
        if not ft and not is_text_hint:
            continue
        if is_text_hint:
            # Заглушки ('...', '???', пусто) — не ответы: дропаем, вопрос
            # останется пропущенным (честнее, чем выводить '...' в placeholder).
            if not _is_meaningful_answer(h.get("params", {}).get("value", "")):
                _log.info("  Q%s: text-hint пропущен — value=%r (заглушка)",
                          qi, h.get("params", {}).get("value", ""))
                continue
            # question_index обязателен — без него поле не найти
            if qi is None:
                unmatched.append(h)
                continue
            # Поле должно быть type=text. Но модель часто путает формат и
            # возвращает placeholder_hint (value = правильный вариант) для
            # radio/checkbox. Если value совпадает с вариантом — конвертируем
            # в background_tint и применяем к этому вопросу (а не мигрируем на
            # Q1, теряя ответ). Иначе — мигрируем на ближайший text-вопрос.
            q = questions[qi - 1] if 1 <= qi <= n_questions else None
            if q and q.get("type") != "text":
                val = h.get("params", {}).get("value", "")
                val_norm = _norm(val)
                if val_norm and val_norm in q.get("options_norm", []):
                    raw_opt = next((o for o in q.get("options", [])
                                    if _norm(o) == val_norm), val)
                    h["action"] = "background_tint"
                    h["find_text"] = raw_opt
                    h["params"] = {}
                    ft = val_norm
                    _log.info("  Конвертирован placeholder→tint Q%d: %s",
                              qi, val_norm[:60])
                    # падаем в путь find_text ниже (без continue)
                else:
                    text_qi = next((i for i, qq in enumerate(questions, 1)
                                    if qq.get("type") == "text"), None)
                    if text_qi:
                        _log.info("  Перемещён text-hint Q%d→Q%d", qi, text_qi)
                        qi = text_qi
                        h["question_index"] = qi
                        question_hints.setdefault(qi, []).append(h)
                    else:
                        unmatched.append(h)
                    continue
            else:
                h["question_index"] = qi
                question_hints.setdefault(qi, []).append(h)
                continue
        if qi is None:
            # Fallback: найти вопрос, где find_text есть в options
            for idx, q in enumerate(questions, 1):
                if ft in q.get("options_norm", []):
                    qi = idx
                    break
        if qi is not None:
            # Валидация: find_text должен быть в options этого вопроса.
            # Если нет — переместить в вопрос, где он есть.
            q = questions[qi - 1] if 1 <= qi <= n_questions else None
            if q and ft not in q.get("options_norm", []):
                found_qi = None
                for other_qi, other_q in enumerate(questions, 1):
                    if other_qi == qi:
                        continue
                    if ft in other_q.get("options_norm", []):
                        found_qi = other_qi
                        break
                if found_qi:
                    _log.info("  Перемещён hint Q%d→Q%d: %s", qi, found_qi, ft[:60])
                    qi = found_qi
            h["question_index"] = qi
            question_hints.setdefault(qi, []).append(h)
        else:
            unmatched.append(h)

    # radio → 1, checkbox → ≤2 (если выбраны почти все) или ≤3, text → 1
    result = []
    for qi, q_hints in sorted(question_hints.items()):
        q_idx = qi - 1 if qi else -1
        q = questions[q_idx] if 0 <= q_idx < n_questions else {}
        qtype = q.get("type", "")
        q_options = q.get("options_norm", [])
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
    # Гарантируем ';' на операторах C++/CLI — модель часто его теряет, а без
    # неё оператор не скомпилируется. Значения (const, 3, Math::E) не трогаем.
    for h in result:
        if h.get("action") == "placeholder_hint" and h.get("params", {}).get("value"):
            h["params"]["value"] = _ensure_statement_terminator(h["params"]["value"])
    return result


def aggregate_hints_by_voting(samples, test_structure_json):
    """Self-consistency: из N выборок облака выбираем наиболее частый
    ответ на каждый вопрос. Усредняет случайность модели на нестабильных
    вопросах (например Q50 на разных прогонах давала «Console::WriteLine(...)»,
    «0», «Время истекло» — голосование берёт большинство).

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
        # нормализация ключа голоса: схлопываем пробелы + lower, чтобы
        # «2, 3» и «2,3» совпадали. Содержательно разные записи формул
        # (Math::Log(z)/Log(2) vs Math::Log(z,2)) останутся разными ключами —
        # для голосования важна частота одинаковых ответов, не семантика.
        return re.sub(r"\s+", " ", (s or "")).strip().lower()

    def _answer_of(h):
        # «ответ» hint'а из ЛЮБОГО формата: find_text (background_tint) ИЛИ
        # params.value (placeholder_hint, либо background_tint с value в
        # params — модель иногда отдаёт radio именно так). Без этого
        # background_tint без find_text терялся и radio-вопрос выпадал.
        ft = h.get("find_text") or ""
        if ft:
            return ft
        v = (h.get("params") or {}).get("value") or ""
        if v:
            return v
        return ""

    def _hint_key(h):
        a = _answer_of(h)
        if not a:
            return None
        return ("a", _vkey(a))

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
            k = _hint_key(h)
            if k is None:
                # странный формат (нет ни find_text, ни params.value) —
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
        "Ты — помощник для решения тестов по C++/CLI и программированию. "
        "Ниже вопросы, на которые ты не ответил в предыдущем запросе. "
        "Для КАЖДОГО выбери правильный ответ:\n"
        "- radio: РОВНО ОДИН наиболее вероятный вариант\n"
        "- checkbox: ТОЛЬКО правильные варианты (если не уверен — 1-2 наиболее "
        "вероятных, НИКОГДА не выбирай ВСЕ варианты)\n"
        "- type=text (без вариантов): верни ОДИН hint с action=placeholder_hint и "
        "params.value=КОНКРЕТНЫЙ ответ на этот вопрос (НЕ шаблон «z = x + y;» для "
        "всех). Читай формулировку: вывод→Console::WriteLine(\"текст\");, "
        "ввод→Console::ReadLine();, константа→const, очистка→Console::Clear();, "
        "корень→Math::Sqrt(x). Операторы (Console::, z=..., ::Parse) всегда "
        "заканчивай точкой с запятой; значения (const, число) — без неё. "
        "Если в вопросе нет конкретики (переменных/чисел) — "
        "поищи их в тексте страницы рядом с вопросом. Если просится общий вид — "
        "дай шаблон («переменная = выражение;»). НЕ используй '...'/'…'/'???'/"
        "'Math::...'/'z = ...'/'Нет данных'/пусто как value — лучше пропусти "
        "вопрос. question_index обязателен, find_text не нужен\n"
        "Возвращай find_text ТОЧНО как он написан в options (без нумерации, без "
        "префиксов). Для формул копируй alt/текст дословно.\n"
        "Формат: JSON {\"hints\": [{\"question_index\": 1, \"find_text\": \"...\", "
        "\"action\": \"background_tint\", \"params\": {}}, "
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

    from difflib import SequenceMatcher

    def _norm(s):
        s = (s or '').replace('\u00a0', ' ')
        s = re.sub(r'Math::PI', 'π', s, flags=re.IGNORECASE)
        s = re.sub(r'\bpi\b', 'π', s, flags=re.IGNORECASE)
        s = s.replace('·', '*').replace('∙', '*').replace('×', '*')
        return re.sub(r'\s+', ' ', s).strip()

    # Агрессивная нормализация для fuzzy: без пробелов, lower
    def _fuzzy(s):
        s = _norm(s).lower()
        s = re.sub(r'\s*([*/+\-=()])\s*', r'\1', s)
        s = re.sub(r'\s+', '', s)
        return s

    validated = []
    valid_indices = {q.get("question_index") for q in unanswered}
    text_qs = {q.get("question_index"): q for q in unanswered if q.get("type") == "text"}
    for h in new_hints:
        qi = h.get("question_index")
        ft = _norm(h.get("find_text", ""))
        # Текстовый hint: action=placeholder_hint + params.value (без find_text)
        is_text = (h.get("action") == "placeholder_hint"
                   and bool(h.get("params", {}).get("value", "")))
        if is_text:
            val = h.get("params", {}).get("value", "")
            if not _is_meaningful_answer(val):
                log.info("  Облако-2: Q%s text-hint пропущен — value=%r (заглушка)",
                         qi, val)
                continue
            if qi in text_qs:
                validated.append({
                    "find_text": "",
                    "question_index": qi,
                    "action": "placeholder_hint",
                    "params": {"value": _ensure_statement_terminator(val)}
                })
            else:
                # Модель вернула placeholder_hint (формат текста) для
                # radio/checkbox, но val — правильный вариант. Конвертируем.
                q_match = next((q for q in unanswered
                                if q.get("question_index") == qi), None)
                val_norm = _norm(val)
                opts_norm = [_norm(o) for o in (q_match or {}).get("options", [])]
                if q_match and val_norm and val_norm in opts_norm:
                    raw_opt = next((o for o in q_match.get("options", [])
                                    if _norm(o) == val_norm), val)
                    validated.append({
                        "find_text": raw_opt,
                        "question_index": qi,
                        "action": "background_tint",
                        "params": {}
                    })
                    log.info("  Облако-2: конвертирован placeholder→tint Q%d: %s",
                             qi, val_norm[:60])
                else:
                    log.warning("  Облако-2: placeholder_hint для не-text Q%d "
                                "(val=%r) — пропущен", qi, str(val)[:60])
            continue
        if not ft or qi not in valid_indices:
            if ft:
                log.warning("  Облако-2: неизвестный question_index=%r для find_text=%r",
                            qi, ft[:60])
            continue
        if len(ft) > 3 and ft[0].isdigit() and ft[1:3] in (". ", ") "):
            ft = ft[3:].strip()

        q_match = next((q for q in unanswered if q.get("question_index") == qi), None)
        if not q_match:
            continue

        # 3 уровня: точное → fuzzy → включение
        matched_opt = None
        ft_fuzzy = _fuzzy(ft)
        for opt_raw in q_match.get("options", []):
            opt_s = _norm(opt_raw)
            opt_fuzzy = _fuzzy(opt_raw)
            if ft == opt_s:
                matched_opt = opt_s
                break
            if ft_fuzzy and opt_fuzzy and ft_fuzzy == opt_fuzzy:
                matched_opt = opt_s
                break
            if opt_fuzzy and ft_fuzzy and (opt_fuzzy.startswith(ft_fuzzy) or ft_fuzzy.startswith(opt_fuzzy)):
                matched_opt = opt_s
                break

        # Левенштейн: самое похожее option
        if not matched_opt and len(ft) >= 3:
            try:
                best_ratio = 0
                best_opt = None
                for opt_raw in q_match.get("options", []):
                    opt_s = _norm(opt_raw)
                    ratio = SequenceMatcher(None, _fuzzy(ft), _fuzzy(opt_raw)).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_opt = opt_s
                if best_opt and best_ratio >= 0.75:
                    log.info("  Облако-2: fuzzy match (ratio=%.2f) %r → %r",
                             best_ratio, ft[:60], best_opt[:60])
                    matched_opt = best_opt
            except Exception:
                pass

        if matched_opt:
            validated.append({
                "find_text": matched_opt,
                "question_index": qi,
                "action": "background_tint",
                "params": {}
            })
        else:
            real_opts = [_norm(o) for o in q_match.get("options", [])]
            log.warning("  Облако-2: не сопоставлено find_text=%r (Q%d)", ft[:80], qi)
            log.warning("    Реальные options Q%d: %s", qi, real_opts)
            log.warning("    Fuzzy find_text: %r", ft_fuzzy[:80])
            log.warning("    Fuzzy options:   %s",
                        [_fuzzy(o) for o in q_match.get("options", [])])

    log.info("Облако 2-й проход: %d hints → %d после валидации",
             len(new_hints), len(validated))
    return validated


def hash_text(s: str) -> str:
    return hashlib.md5(s.encode("utf-8", errors="ignore")).hexdigest()


# --- main loop ---

def main():
    cfg = load_config()
    setup_logging(cfg["agent"].get("log_file", "agent.log"))
    log = logging.getLogger("usb_agent")

    chrome_cfg = cfg["chrome"]
    local_cfg = cfg["local_llm"]
    cloud_cfg = cfg["cloud"]
    agent_cfg = cfg["agent"]

    # Проверка ключа облачного провайдера
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

    # Выбор облачного провайдера: "gemini" (прямой Google API) или
    # "openrouter" (агрегатор, без региональных ограничений, есть free tier).
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

    poll = chrome_cfg["poll_interval_seconds"]
    max_chars = chrome_cfg["max_page_chars"]
    skip_urls = chrome_cfg.get("skip_urls", [])
    skip_if_no_change = agent_cfg.get("skip_if_no_change", True)
    cloud_first = cloud_cfg.get("cloud_first", False)
    # Self-consistency: число выборок облака + температура для расхождения.
    # consistency_samples=1 — без голосования (старое поведение). >=3 —
    # несколько вызовов и голосование большинством по каждому вопросу.
    consistency_samples = max(1, int(cloud_cfg.get("consistency_samples", 1) or 1))
    cloud_temperature = cloud_cfg.get("temperature")  # None = серверный дефолт

    log.info("=== USB Agent запущен ===")
    log.info("Опрос каждые %s сек. Chrome порт: %s. Локальная LLM: %s",
             poll, chrome_cfg["debug_port"], local_cfg["base_url"])
    if cloud:
        log.info("Эскалация в %s включена (модель %s).",
                 provider_name, cloud_cfg["model"])
        if cloud_first:
            log.info("Режим cloud_first: облако решает первично, локальная LLM — fallback.")
    else:
        log.info("Эскалация в облако ВЫКЛЮЧЕНА.")
    log.info("Текущая задача: %s", read_task(agent_cfg.get("task_file", "task.txt")))
    log.info("Остановка: Ctrl+C или stop.bat / ./stop.sh")

    # Ждём готовности локальной LLM — llama-server грузит модель ~20-30 сек,
    # а run.bat ждёт всего 6. Без этой проверки первый запрос падает с
    # WinError 10061 (connection refused), и страница помечается обработанной.
    log.info("Ожидание готовности локальной LLM на %s ...", local_cfg["base_url"])
    for _ in range(60):  # до 120 сек
        if local.is_alive():
            break
        time.sleep(2)
    if local.is_alive():
        log.info("Локальная LLM готова.")
    else:
        log.warning("Локальная LLM не отвечала 120 сек — продолжаю в фоне.")

    last_page_hash = ""
    last_task_hash = ""
    current_task = read_task(agent_cfg.get("task_file", "task.txt"))

    while True:
        try:
            # Проверка задачи (можно редактировать task.txt на лету)
            new_task = read_task(agent_cfg.get("task_file", "task.txt"))
            if hash_text(new_task) != last_task_hash:
                log.info("Задача обновлена: %s", new_task)
                current_task = new_task
                last_task_hash = hash_text(new_task)
                # при смене задачи — сбрасываем хэш страницы, чтобы сразу отработать
                last_page_hash = ""

            # Подключение к Chrome
            if not cdp.is_chrome_up():
                log.info("Chrome не отвечает на порту %s — жду",
                         chrome_cfg["debug_port"])
                time.sleep(poll)
                continue

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
                time.sleep(poll)
                continue
            if not cdp.connect(tab=tab):
                log.info("Вкладка найдена, но WebSocket не открылся: %s",
                         (tab.get("url") or "")[:80])
                time.sleep(poll)
                continue

            page = cdp.get_page_text(max_chars=max_chars)
            selection = cdp.get_selection()
            log.info("Вкладка: %s | %s (текст %d симв., выделение %d симв.)",
                     (page.get("title") or "")[:60],
                     (page.get("url") or "")[:80],
                     len(page.get("text") or ""),
                     len(selection or ""))

            # Контент стабилен — пропускаем
            phash = hash_text(page.get("text", "") + "|" + selection)
            if skip_if_no_change and phash == last_page_hash:
                log.info("Контент не изменился с прошлого цикла — пропускаю")
                cdp.close()
                time.sleep(poll)
                continue

            if not page.get("text", "").strip():
                log.info("Текст страницы пуст — пропускаю (возможно, страница ещё грузится)")
                cdp.close()
                time.sleep(poll)
                continue

            parsed = None
            test_structure = "[]"  # заполнится в cloud_first блоке; нужно для deduplicate_hints
            if cloud_first and cloud:
                # Структурированное описание теста: тип (radio/checkbox) +
                # варианты для каждого вопроса. Облако видит innerText без
                # HTML-структуры — без этой карты оно не отличает radio от
                # checkbox и выдумывает текст вариантов.
                # Оборачиваем в try/except: если метода нет или JS упал —
                # работаем по старому пути (только innerText).
                try:
                    test_structure = cdp.get_test_structure()
                except Exception as e:
                    log.warning("get_test_structure недоступно (%s) — облако работает без структуры", e)
                    test_structure = "[]"
                # Диагностика: сразу видно, сколько вопросов найдено и как
                # извлёкся первый — если options пустые, экстрактор не справился
                # с разметкой и облако останется без вариантов.
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
                        # большинством по каждому вопросу. Усредняет случайность
                        # модели (Q50 на разных прогонах: «Console::WriteLine(...)»,
                        # «0», «Время истекло» — голосование берёт большинство).
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
                                # Прогон через dedup ДО голосования: восстанавливает
                                # question_index для radio/checkbox hints (по find_text
                                # в options) — как в одинарном режиме. Без этого hints
                                # без qi (часто у восстановленного битого JSON) теряются
                                # при голосовании на `if qi is None: continue`, и radio
                                # выпадают (было 20/50 вместо ~50).
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
                            # Диагностика: длина ответа + начало + конец (виден ли
                            # закрывающий }). Лог ограничен 800 симв., но raw_text
                            # может быть длиннее — нужна полная картина.
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
                    time.sleep(poll)
                    continue

                parsed = parse_llm_json(resp)
                if not parsed:
                    log.warning("Не распарсил JSON из ответа LLM: %.200s", resp)
                    cdp.close()
                    time.sleep(poll)
                    continue

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
            # Post-processing: для radio-вопросов оставляем только 1 ответ
            # (модель часто маркирует несколько), плюс дедупликация по find_text.
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

            # Второй проход: вопросы, на которые облако не ответило в первом
            # запросе. Облако получает компактный JSON только с пропущенными
            # вопросами и часто добирает их со второй попытки.
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
            last_page_hash = phash

            cdp.close()
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


if __name__ == "__main__":
    main()
