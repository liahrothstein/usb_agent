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
import requests
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
    """Читает текущую задачу из task.txt. Если файла нет — задача 'general'."""
    p = Path(__file__).parent / task_file
    if not p.exists():
        return "(задача не указана — отредактируй task.txt)"
    txt = p.read_text(encoding="utf-8").strip()
    return txt or "(задача пуста — отредактируй task.txt)"


def parse_llm_json(text: str) -> dict:
    """Достаёт первый JSON-объект из ответа LLM (может быть в код-блоках)."""
    if not text:
        return {}
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
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        # JSON обрезан (OpenRouter иногда режет ответ посередине).
        # Пытаемся извлечь полные hint-объекты регуляркой.
        recovered = _recover_hints(t[start:])
        if recovered.get("hints"):
            logging.getLogger("usb_agent").info(
                "Восстановлено %d hints из обрезанного JSON", len(recovered["hints"]))
        return recovered


def _recover_hints(text: str) -> dict:
    """Извлекает полные hint-объекты из обрезанного JSON регуляркой.

    Если OpenRouter обрезал ответ посередине (нет закрывающих ] и }),
    ищем полные {"find_text": "...", "action": "..."} объекты и собираем
    из них валидный результат. Неполные объекты в конце игнорируем.
    """
    hints = []
    # (?:[^"\\]|\\.)* — содержимое строки с поддержкой экранированных кавычек
    pattern = (
        r'\{"find_text"\s*:\s*"((?:[^"\\]|\\.)*)"'
        r'\s*,\s*"action"\s*:\s*"((?:[^"\\]|\\.)*)"'
        r'(?:,\s*"params"\s*:\s*(\{[^}]*\}))?'
        r'\s*\}'
    )
    for m in re.finditer(pattern, text):
        find_text = m.group(1)
        action = m.group(2)
        params_str = m.group(3)
        params = {}
        if params_str:
            try:
                params = json.loads(params_str)
            except json.JSONDecodeError:
                pass
        hints.append({"find_text": find_text, "action": action, "params": params})
    if hints:
        return {"hints": hints, "reasoning": "(восстановлено из обрезанного JSON: %d hints)" % len(hints)}
    return {}


def deduplicate_hints(hints: list, test_structure_json: str) -> list:
    """Группирует hints по question_index, валидирует find_text против options,
    ограничивает radio=1, checkbox<=2 (или 3).
    """
    if not hints:
        return hints

    _log = logging.getLogger("usb_agent")

    try:
        questions = json.loads(test_structure_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        questions = []

    if not questions:
        seen = set()
        result = []
        for h in hints:
            ft = h.get("find_text", "")
            if ft and ft not in seen:
                seen.add(ft)
                result.append(h)
        return result

    # Логируем test_structure один раз
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
        # Нормализация Math::PI ↔ π (модель часто пишет Math::PI вместо π)
        s = re.sub(r'Math::PI', 'π', s, flags=re.IGNORECASE)
        s = re.sub(r'\bpi\b', 'π', s, flags=re.IGNORECASE)
        return re.sub(r'\s+', ' ', s).strip()

    # Нормализуем options в test_structure
    for q in questions:
        q["options_norm"] = [_norm(o) for o in q.get("options", [])]

    # Валидация question_index: отбрасываем вне диапазона
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

    # Для каждого hint находим question_index
    question_hints = {}  # qi → list of hints
    unmatched = []
    for h in hints:
        qi = h.get("question_index")
        ft = _norm(h.get("find_text", ""))
        if not ft:
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
                # Поиск вопроса, где find_text есть в options
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

    # Ограничения: radio=1, checkbox<=2 (или 3)
    result = []
    for qi, q_hints in sorted(question_hints.items()):
        q_idx = qi - 1 if qi else -1
        q = questions[q_idx] if 0 <= q_idx < n_questions else {}
        qtype = q.get("type", "")
        q_options = q.get("options_norm", [])

        if qtype == "radio" and len(q_hints) > 1:
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
    return result

def answer_unanswered_via_cloud(cloud_client, hints_applied, test_structure_json):
    """
    Второй проход через облако: для вопросов, на которые облако не дало hints
    в первом запросе, отправляем отдельный компактный запрос.
    Использует существующий cloud-клиент (OpenRouter/Gemini) с теми же настройками.
    """
    log = logging.getLogger(__name__)

    if not test_structure_json or test_structure_json == "[]":
        return []

    try:
        structure = json.loads(test_structure_json)
    except Exception:
        return []

    answered_texts = {h.get("find_text", "").strip() for h in hints_applied if h.get("find_text")}

    unanswered = []
    for q in structure:
        options = q.get("options", [])
        if not options:
            continue
        if not any(opt.strip() in answered_texts for opt in options):
            unanswered.append(q)

    if not unanswered:
        log.info("Все вопросы отвечены облаком — второй проход не нужен")
        return []

    log.info("Пропущено вопросов: %d — отправляю в облако повторно", len(unanswered))

    # Компактный JSON только с пропущенными вопросами
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
        "- checkbox: ТОЛЬКО правильные варианты (если не уверен — 1-2 наиболее вероятных, "
        "НИКОГДА не выбирай ВСЕ варианты)\n"
        "Возвращай find_text ТОЧНО как он написан в options (без нумерации, без префиксов). "
        "Формат: JSON {\"hints\": [{\"question_index\": 1, \"find_text\": \"...\", \"action\": \"background_tint\", \"params\": {}}]}"
    )

    user_msg = f"Пропущенные вопросы:\n\n{questions_json}\n\nВерни JSON с hints для каждого вопроса. Не пропускай ни один."

    try:
        cloud_resp = cloud_client.ask(user_msg, system=system_msg)
        raw_text = cloud_resp["text"]
        parsed = parse_llm_json(raw_text)
        new_hints = parsed.get("hints", []) if parsed else []
    except Exception as e:
        log.warning("Второй проход облака не удался: %s", e)
        return []

    if not new_hints:
        log.warning("Облако во 2-м проходе не вернуло hints (длина: %d, начало: %.200s)",
                    len(raw_text), raw_text)
        return []

    import re
    from difflib import SequenceMatcher

    def _norm(s):
        s = (s or '').replace('\u00a0', ' ')
        # Нормализация Math::PI ↔ π (модель часто пишет Math::PI вместо π)
        s = re.sub(r'Math::PI', 'π', s, flags=re.IGNORECASE)
        s = re.sub(r'\bpi\b', 'π', s, flags=re.IGNORECASE)
        # Разные символы умножения → *
        s = s.replace('·', '*').replace('∙', '*').replace('×', '*')
        return re.sub(r'\s+', ' ', s).strip()

    # Агрессивная нормализация для fuzzy matching: убираем все пробелы вокруг операторов
    def _fuzzy(s):
        s = _norm(s).lower()
        s = re.sub(r'\s*([*/+\-=()])\s*', r'\1', s)  # "g * π / 180" → "g*π/180"
        s = re.sub(r'\s+', '', s)  # убираем все пробелы
        return s

    validated = []
    valid_indices = {q.get("question_index") for q in unanswered}
    for h in new_hints:
        qi = h.get("question_index")
        ft = _norm(h.get("find_text", ""))
        if not ft or qi not in valid_indices:
            if ft:
                log.warning("  Облако-2: неизвестный question_index=%r для find_text=%r", qi, ft[:60])
            continue
        if len(ft) > 3 and ft[0].isdigit() and ft[1:3] in (". ", ") "):
            ft = ft[3:].strip()

        # Сопоставляем с реальными options вопроса
        q_match = next((q for q in unanswered if q.get("question_index") == qi), None)
        if not q_match:
            continue

        # Сопоставление 3 уровня:
        # 1. Точное (с нормализацией)
        # 2. Fuzzy (без пробелов и регистронезависимо)
        # 3. Включение (один содержит другой)
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

        # Fuzzy с Левенштейном: найти самое похожее option
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
            # ДИАГНОСТИКА: показать реальные options
            real_opts = [_norm(o) for o in q_match.get("options", [])]
            log.warning("  Облако-2: не сопоставлено find_text=%r (Q%d)", ft[:80], qi)
            log.warning("    Реальные options Q%d: %s", qi, real_opts)
            log.warning("    Fuzzy find_text: %r", ft_fuzzy[:80])
            log.warning("    Fuzzy options:   %s", [_fuzzy(o) for o in q_match.get("options", [])])

    log.info("Облако 2-й проход: %d hints → %d после валидации", len(new_hints), len(validated))
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
                # Диагностика
                if test_structure and test_structure != "[]":
                    try:
                        _ts = json.loads(test_structure)
                        log.info("test_structure: %d вопросов", len(_ts))
                        if _ts:
                            q0 = _ts[0]
                            log.info("  Q1: type=%s, qtext=%r, options_count=%d",
                                    q0.get("type"), q0.get("question_text", "")[:80],
                                    len(q0.get("options", [])))
                    except Exception:
                        pass
                else:
                    log.warning("test_structure ПУСТОЙ — облако не увидит вопросов")
                cloud_msg = CLOUD_USER_TEMPLATE.format(
                    task=current_task,
                    url=page.get("url", ""),
                    title=page.get("title", ""),
                    max_chars=max_chars,
                    test_structure=test_structure,
                )
                try:
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
            if hints:
                applied = apply_hints(cdp, hints)
                log.info("Применено %d/%d подсказок", applied, len(hints))

                # === ВТОРОЙ ПРОХОД: облако повторно для пропущенных ===
                if cloud and test_structure and test_structure != "[]":
                    try:
                        extra_hints = answer_unanswered_via_cloud(cloud, hints, test_structure)
                        if extra_hints:
                            # Дедупликация против уже применённых
                            existing = {h.get("find_text") for h in hints}
                            extra_hints = [h for h in extra_hints if h.get("find_text") not in existing]
                            # Радио-дедупликация для второго прохода
                            extra_hints = deduplicate_hints(extra_hints, test_structure)
                            if extra_hints:
                                extra_applied = apply_hints(cdp, extra_hints)
                                log.info("Облако 2-й проход: применено %d/%d (всего %d)",
                                        extra_applied, len(extra_hints), applied + extra_applied)
                                hints.extend(extra_hints)
                    except Exception as e:
                        log.warning("Второй проход не удался: %s", e)
            else:
                log.info("Подсказок нет")

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
