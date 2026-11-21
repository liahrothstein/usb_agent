"""Инъектор тонких подсказок в DOM через CDP.

Принимает список подсказок [{selector, action, params}] и для каждой
генерирует безопасный JS, который:
- находит элемент по селектору (если нет — тихо пропускает),
- применяет минимальную модификацию,
- не выбрасывает ошибок в страницу.
"""
import json
import logging
from typing import List, Dict

_log = logging.getLogger("usb_agent")


VALID_ACTIONS = {
    "italic_letter",
    "text_shadow",
    "letter_spacing",
    "background_tint",
    "font_weight",
    "inject_html",
    "placeholder_hint",
}


def apply_hints(cdp, hints: List[Dict]) -> int:
    """Применяет каждую подсказку. Возвращает число успешных применений."""
    if not hints:
        return 0
    applied = 0
    for h in hints:
        if not isinstance(h, dict):
            continue
        selector = h.get("selector", "")
        find_text = h.get("find_text", "")
        target_index = h.get("target_index")
        question_index = h.get("question_index")
        action = h.get("action", "")
        params = h.get("params", {}) or {}
        if action not in VALID_ACTIONS:
            continue
        # Текстовый вопрос: только question_index + action=placeholder_hint +
        # params.value (ответ → placeholder поля).
        is_text_hint = (action == "placeholder_hint" and question_index is not None
                        and bool(params.get("value", "")))
        # radio/checkbox: question_index + target_index — прямой доступ к
        # опции по data-usb-option-index внутри [data-usb-q]. Без find_text.
        is_tint_hint = (action == "background_tint" and question_index is not None
                        and target_index is not None)
        if not selector and not find_text and not is_text_hint and not is_tint_hint:
            continue
        # Локальный путь (UX-подсветка произвольных элементов страницы):
        # find_text без question_index. Короткий find_text (≤ 2, не буквы) —
        # точный match по тексту элемента (без includes-фолбэка).
        if find_text and len(find_text.strip()) <= 2 and not find_text.strip().isalpha():
            _log.info("  короткий find_text=%r — точный match по тексту элемента",
                      find_text)
        script = _build_script(selector, action, params, find_text,
                               question_index, target_index)
        if not script:
            _log.info("  пропуск: action=%s params=%r (не построен)", action, params)
            continue
        ok = cdp.inject_script(script)
        if is_text_hint:
            desc = f"placeholder={params.get('value','')!r}"
        elif is_tint_hint:
            desc = f"target_index={target_index}"
        elif find_text:
            desc = f"find_text={find_text!r}"
        else:
            desc = f"selector={selector}"
        if question_index:
            desc += f" Q{question_index}"
        status = "OK" if ok else "НЕ найден/не применён"
        _log.info("  %s | %s action=%s params=%r", status, desc, action, params)
        if ok:
            applied += 1
    return applied


def _js_str(s: str) -> str:
    """Безопасно оборачивает строку в JS-литерал."""
    return json.dumps(str(s))


def _build_script(selector: str, action: str, params: Dict,
                  find_text: str = "", question_index: int = None,
                  target_index: int = None) -> str:
    body = _action_body(action, params)
    if not body:
        return ""

    # Текстовые вопросы (input[text]/textarea): ответ → placeholder поля.
    # Элемент находится по data-usb-q-index + data-usb-text-input (без find_text,
    # без selector). body (из _action_body placeholder_hint) ставит placeholder.
    if question_index and action == "placeholder_hint":
        return (
            "(() => {\n"
            f"  const qIndex = {int(question_index)};\n"
            "  const marked = document.querySelectorAll('[data-usb-q-index]');\n"
            "  for (const el of marked) {\n"
            "    if (el.dataset.usbQIndex !== String(qIndex)) continue;\n"
            "    if (el.dataset.usbTextInput !== '1') continue;\n"
            "    if (el.dataset.usbTinted === '1') return false;\n"
            "    try {\n"
            f"      {body}\n"
            "      el.dataset.usbTinted = '1';\n"
            "      return true;\n"
            "    } catch (e) { return false; }\n"
            "  }\n"
            "  return false;\n"
            "})()"
        )

    # radio/checkbox: прямой доступ к опции по индексу внутри контейнера
    # вопроса [data-usb-q]. Никакого текстового матчинга — индекс адресует
    # ровно тот DOM-узел, который парсер пометил data-usb-option-index.
    # Универсально: не зависит от текста варианта (число, формула, код).
    if (question_index is not None and target_index is not None
            and action == "background_tint"):
        return (
            "(() => {\n"
            f"  const qIndex = {int(question_index)};\n"
            f"  const targetIdx = {int(target_index)};\n"
            "  const qContainer = document.querySelector('[data-usb-q=\"' + qIndex + '\"]');\n"
            "  if (!qContainer) return false;\n"
            "  const opt = qContainer.querySelector('[data-usb-option-index=\"' + targetIdx + '\"]');\n"
            "  if (!opt) return false;\n"
            "  if (opt.dataset.usbTinted === '1') return false;\n"
            "  // Обёртка: label, либо ближайший предок без других input'ов.\n"
            "  // Если opt — img, обёртка = label или родитель.\n"
            "  let wrap = null;\n"
            "  if (opt.tagName === 'IMG') {\n"
            "    wrap = opt.closest('label') || opt.parentElement;\n"
            "  } else {\n"
            "    wrap = opt.closest('label');\n"
            "    if (!wrap) {\n"
            "      wrap = opt.parentElement;\n"
            "      while (wrap) {\n"
            "        const childInputs = wrap.querySelectorAll('input[type=radio], input[type=checkbox]');\n"
            "        if (childInputs.length <= 1) break;\n"
            "        wrap = wrap.parentElement;\n"
            "        if (wrap && wrap.tagName === 'BODY') { wrap = opt.parentElement; break; }\n"
            "      }\n"
            "    }\n"
            "  }\n"
            "  if (!wrap) wrap = opt;\n"
            "  if (wrap.dataset.usbTinted === '1') return false;\n"
            "  const el = wrap;\n"
            "  try {\n"
            f"    {body}\n"
            "    wrap.dataset.usbTinted = '1';\n"
            "    return true;\n"
            "  } catch (e) { return false; }\n"
            "})()"
        )

    # Fallback: глобальный поиск — сначала img[alt], потом по textContent.
    if find_text:
        return (
            "(() => {\n"
            f"  const findText = {_js_str(find_text)};\n"
            "  const normFind = findText.replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();\n"
            "  const imgs = document.querySelectorAll('img[alt]');\n"
            "  for (const img of imgs) {\n"
            "    if (img.dataset.usbTinted === '1') continue;\n"
            "    const alt = img.alt.replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();\n"
            "    if (alt === normFind) {\n"
            "      const el = img;\n"
            "      try {\n"
            f"        {body}\n"
            "        img.dataset.usbTinted = '1';\n"
            "        return true;\n"
            "      } catch (e) { return false; }\n"
            "    }\n"
            "  }\n"
            "  const els = document.querySelectorAll('label,span,td,li,p,a,option,strong,em,code,pre,h1,h2,h3,h4');\n"
            "  for (const el of els) {\n"
            "    if (el.dataset.usbTinted === '1') continue;\n"
            "    const text = el.textContent.replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();\n"
            "    if (text === normFind || (normFind.length > 3 && text.length < 500 && text.includes(normFind))) {\n"
            "      try {\n"
            f"        {body}\n"
            "        el.dataset.usbTinted = '1';\n"
            "        return true;\n"
            "      } catch (e) { return false; }\n"
            "    }\n"
            "  }\n"
            "  return false;\n"
            "})()"
        )
    if not selector:
        return ""
    return (
        "(() => {\n"
        f"  const el = document.querySelector({_js_str(selector)});\n"
        "  if (!el) return false;\n"
        "  try {\n"
        f"    {body}\n"
        "    return true;\n"
        "  } catch (e) {\n"
        "    return false;\n"
        "  }\n"
        "})()"
    )


def _action_body(action: str, params: Dict) -> str:
    if action == "italic_letter":
        word = params.get("word", "")
        letter = params.get("letter", "")
        if not word or not letter or len(letter) != 1:
            return ""
        # Экранируем слово и букву для RegExp и для HTML
        word_js = _js_str(word)
        letter_js = _js_str(letter)
        # В HTML внутри <i> подставляем одну букву (одиночный символ — безопасно)
        return (
            "const html = el.innerHTML;\n"
            f"const re = new RegExp({word_js}, 'g');\n"
            f"el.innerHTML = html.replace(re, m => "
            f"m.replace({letter_js}, '<i style=\"font-style:italic\">' "
            f"+ {letter_js} + '</i>'));"
        )

    if action == "text_shadow":
        color = params.get("color", "1px 1px 2px rgba(120,120,120,0.35)")
        return f"el.style.textShadow = {_js_str(color)};"

    if action == "letter_spacing":
        val = params.get("value", "0.15em")
        return f"el.style.letterSpacing = {_js_str(val)};"

    if action == "background_tint":
        color = params.get("color", "rgba(128,128,128,0.15)")
        return f"el.style.backgroundColor = {_js_str(color)};"

    if action == "font_weight":
        w = params.get("value", "700")
        w_js = _js_str(str(w))
        tint_js = _js_str("rgba(128,128,128,0.15)")
        # Если элемент уже bold (font-weight >= 600) — font_weight не виден.
        # Fallback: подсвечиваем фон вместо жирности, чтобы инъексия была заметной.
        return (
            "const fw = window.getComputedStyle(el).fontWeight;\n"
            "const fwNum = parseInt(fw);\n"
            "if (fwNum >= 600 || fw === 'bold') {\n"
            "  el.style.backgroundColor = " + tint_js + ";\n"
            "} else {\n"
            "  el.style.fontWeight = " + w_js + ";\n"
            "}"
        )

    if action == "inject_html":
        html = params.get("html", "<span style='color:#888;font-weight:600;font-size:1.1em'>!</span>")
        return f"el.insertAdjacentHTML('beforeend', {_js_str(html)});"

    if action == "placeholder_hint":
        val = params.get("value", "")
        if not val:
            return ""
        val_js = _js_str(str(val))
        # Если у поля уже есть placeholder — дописываем ответ через "→",
        # иначе ставим ответ как placeholder. value поля НЕ трогаем —
        # это подсказка, а не готовый ответ (placeholder виден, пока поле пусто).
        return (
            "const cur = el.getAttribute('placeholder') || '';\n"
            "el.setAttribute('placeholder', cur ? (cur + ' → ' + " + val_js + ") : " + val_js + ");"
        )

    return ""
