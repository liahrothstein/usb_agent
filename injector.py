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
        question_index = h.get("question_index")
        action = h.get("action", "")
        params = h.get("params", {}) or {}
        if action not in VALID_ACTIONS:
            continue
        # Текстовый вопрос: нет selector/find_text, только question_index +
        # action=placeholder_hint + params.value (ответ → placeholder поля).
        is_text_hint = (action == "placeholder_hint" and question_index is not None
                        and bool(params.get("value", "")))
        if not selector and not find_text and not is_text_hint:
            continue
        # Дедупликация по find_text УБРАНА: одинаковый текст в разных вопросах
        # (напр. "4" в Q3 и Q15) — это разные hints. Поиск идёт по dataset-
        # маркерам (data-usb-q-index + data-usb-option), поэтому короткий
        # find_text подсвечивается именно в своём вопросе, а не в первом
        # попавшемся. Уже подсвеченные элементы пропускаются через usbTinted.
        if find_text and len(find_text.strip()) <= 2 and not find_text.strip().isalpha():
            _log.warning("  ВНИМАНИЕ: короткий find_text=%r — может совпасть с чужим элементом",
                         find_text)
        script = _build_script(selector, action, params, find_text, question_index)
        if not script:
            _log.info("  пропуск: action=%s params=%r (не построен)", action, params)
            continue
        ok = cdp.inject_script(script)
        if is_text_hint:
            desc = f"placeholder={params.get('value','')!r}"
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
                  find_text: str = "", question_index: int = None) -> str:
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

    # Приоритет 1: поиск по dataset-маркерам, проставленным get_test_structure.
    # Универсально — не зависит ни от порядка групп, ни от HTML-структуры:
    # input ИЛИ img с data-usb-q-index=qIndex И data-usb-option=normFind.
    if find_text and question_index:
        return (
            "(() => {\n"
            f"  const findText = {_js_str(find_text)};\n"
            f"  const qIndex = {int(question_index)};\n"
            "  const normFind = findText.replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();\n"
            "  const allMarked = document.querySelectorAll('[data-usb-q-index]');\n"
            "  let targetEl = null;\n"
            "  for (const el of allMarked) {\n"
            "    if (el.dataset.usbQIndex !== String(qIndex)) continue;\n"
            "    if (el.dataset.usbOption === normFind) { targetEl = el; break; }\n"
            "  }\n"
            "  // Fallback: частичное совпадение (модель вернула с опечаткой/обрезкой)\n"
            "  if (!targetEl) {\n"
            "    for (const el of allMarked) {\n"
            "      if (el.dataset.usbQIndex !== String(qIndex)) continue;\n"
            "      const opt = el.dataset.usbOption || '';\n"
            "      if (opt.length > 3 && (opt.includes(normFind) || normFind.includes(opt))) {\n"
            "        targetEl = el; break;\n"
            "      }\n"
            "    }\n"
            "  }\n"
            "  if (!targetEl) return false;\n"
            "  // Обёртка: label, либо ближайший предок без других input'ов.\n"
            "  // Если target — img, обёртка = label или родитель.\n"
            "  let wrap = null;\n"
            "  if (targetEl.tagName === 'IMG') {\n"
            "    wrap = targetEl.closest('label') || targetEl.parentElement;\n"
            "  } else {\n"
            "    wrap = targetEl.closest('label');\n"
            "    if (!wrap) {\n"
            "      wrap = targetEl.parentElement;\n"
            "      while (wrap) {\n"
            "        const childInputs = wrap.querySelectorAll('input[type=radio], input[type=checkbox]');\n"
            "        if (childInputs.length <= 1) break;\n"
            "        wrap = wrap.parentElement;\n"
            "        if (wrap && wrap.tagName === 'BODY') { wrap = targetEl.parentElement; break; }\n"
            "      }\n"
            "    }\n"
            "    // Если обёртка не содержит img с маркером, но внутри есть img с тем же alt — подсветим img\n"
            "    if (wrap && !wrap.querySelector('img[data-usb-option]')) {\n"
            "      const img = wrap.querySelector('img[alt]');\n"
            "      if (img) {\n"
            "        const imgAlt = img.alt.replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();\n"
            "        if (imgAlt === normFind) wrap = img;\n"
            "      }\n"
            "    }\n"
            "  }\n"
            "  if (!wrap) wrap = targetEl;\n"
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
