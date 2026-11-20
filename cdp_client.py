"""Chrome DevTools Protocol клиент.

Подключается к Chrome, запущенному с флагом --remote-debugging-port=N,
и позволяет читать DOM и инжектить скрипты в активную вкладку.
Не требует установки расширения.
"""
import json
import logging
import time
import urllib.request
import urllib.error
from typing import Optional

try:
    import websocket  # pip install websocket-client
except ImportError:
    raise SystemExit(
        "[FATAL] Не установлена зависимость websocket-client.\n"
        "  Выполни: pip install -r requirements.txt"
    )

_log = logging.getLogger("usb_agent")


class CDPClient:
    def __init__(self, host: str = "localhost", port: int = 9222):
        self.host = host
        self.port = port
        self._ws = None
        self._msg_id = 0

    # --- discovery ---

    def list_tabs(self) -> list:
        url = f"http://{self.host}:{self.port}/json"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError:
            return []

    def is_chrome_up(self) -> bool:
        return bool(self.list_tabs())

    def pick_tab(self, skip_prefixes: Optional[list] = None) -> Optional[dict]:
        """Выбирает вкладку типа page, пропуская служебные URL."""
        skip_prefixes = skip_prefixes or ["chrome://", "about:", "chrome-extension://"]
        tabs = self.list_tabs()
        pages = [t for t in tabs if t.get("type") == "page"]
        if not pages:
            return None
        for t in pages:
            u = t.get("url", "")
            if u and not any(u.startswith(p) for p in skip_prefixes):
                return t
        return None

    # --- connection ---

    def connect(self, tab: Optional[dict] = None,
                skip_prefixes: Optional[list] = None) -> bool:
        if tab is None:
            tab = self.pick_tab(skip_prefixes=skip_prefixes)
        if not tab or "webSocketDebuggerUrl" not in tab:
            return False
        ws_url = tab["webSocketDebuggerUrl"]
        # Chrome отдаёт ws://localhost:..., а на Windows localhost часто
        # резолвится в IPv6 ::1, тогда как Chrome слушает на 127.0.0.1 (IPv4).
        # websocket-client не делает fallback IPv6→IPv4 — подменяем явно.
        ws_url = ws_url.replace("://localhost:", "://127.0.0.1:")
        ws_url = ws_url.replace("://[::1]:", "://127.0.0.1:")
        try:
            self._ws = websocket.create_connection(ws_url, timeout=10)
        except Exception as e:
            # Не глотаем молча — логируем реальную причину сбоя.
            _log.warning("CDP WebSocket не открылся (%s): %s",
                         type(e).__name__, e)
            self._ws = None
            return False
        self._send("Page.enable")
        self._send("Runtime.enable")
        return True

    def _send(self, method: str, params: Optional[dict] = None) -> dict:
        if self._ws is None:
            raise RuntimeError("CDP: не подключено")
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method}
        if params:
            msg["params"] = params
        self._ws.send(json.dumps(msg))
        # Читаем, пока не получим ответ с нашим id (пропускаем события)
        deadline = time.time() + 15
        while time.time() < deadline:
            raw = self._ws.recv()
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("id") == self._msg_id:
                return data
        raise TimeoutError("CDP: нет ответа на " + method)

    # --- high-level helpers ---

    def evaluate(self, expression: str, return_by_value: bool = True) -> dict:
        params = {
            "expression": expression,
            "returnByValue": return_by_value,
            "awaitPromise": True,
            "userGesture": True,
        }
        return self._send("Runtime.evaluate", params)

    def get_page_text(self, max_chars: int = 10000) -> dict:
        """Возвращает url, title и обрезанный innerText активной вкладки."""
        expr = (
            "(() => {"
            "  const text = document.body ? document.body.innerText : '';"
            f"  return {{"
            f"    url: location.href,"
            f"    title: document.title,"
            f"    text: text.slice(0, {int(max_chars)})"
            f"  }};"
            "})()"
        )
        result = self.evaluate(expr)
        try:
            value = result["result"]["result"]["value"]
            return value or {"url": "", "title": "", "text": ""}
        except (KeyError, TypeError):
            return {"url": "", "title": "", "text": ""}

    def get_selection(self) -> str:
        expr = "window.getSelection ? window.getSelection().toString() : ''"
        result = self.evaluate(expr)
        try:
            return result["result"]["result"]["value"] or ""
        except (KeyError, TypeError):
            return ""

    def get_selectors(self) -> str:
        """Собирает список реальных CSS-селекторов со страницы (теги + классы),
        чтобы LLM не выдумывала несуществующие селекторы."""
        expr = (
            "(() => {"
            "  const tags = {};"
            "  const classes = {};"
            "  document.querySelectorAll('h1,h2,h3,h4,p,li,code,pre,blockquote,table,th,td,article,main,section,header,nav,div,span,a,button').forEach(el => {"
            "    if (el.classList.contains('sr-only')) return;"
            "    if (el.offsetWidth === 0 && el.offsetHeight === 0) return;"
            "    const tag = el.tagName.toLowerCase();"
            "    tags[tag] = (tags[tag] || 0) + 1;"
            "    el.classList.forEach(c => {"
            "      if (c.length > 2 && c !== 'sr-only') classes['.' + c] = (classes['.' + c] || 0) + 1;"
            "    });"
            "  });"
            "  const tagList = Object.entries(tags).map(([t,n]) => t + '(' + n + ')').join(' ');"
            "  const classList = Object.entries(classes).sort((a,b) => b[1]-a[1]).slice(0,15).map(([c,n]) => c + '(' + n + ')').join(' ');"
            "  return 'tags: ' + tagList + ' | top classes: ' + classList;"
            "})()"
        )
        result = self.evaluate(expr)
        try:
            return result["result"]["result"]["value"] or "(селекторы не получены)"
        except (KeyError, TypeError):
            return "(селекторы не получены)"

    def get_test_structure(self) -> str:
        """Извлекает все radio/checkbox И текстовые (input[text]/textarea) вопросы
        со страницы и МАРКИРУЕТ их в DOM.

        Универсально — не зависит от структуры HTML (Moodle, рукописный HTML,
        формулы как <img alt="...">, radio+checkbox+text-input вперемешку).
        Текстовое поле внутри radio/checkbox блока ("Другое: ___") считается
        частью того вопроса, а не отдельным.

        Группировка: по наименьшему общему предку, содержащему >=2 input'ов
        того же типа (а не по атрибуту name — у checkbox'ов Moodle он свой
        для каждого варианта).

        Текст варианта: 8 уровней fallback — label[for] → closest('label') →
        сиблинги → parent → text-nodes → предыдущий сиблинг → img[alt] по
        позиции → любой элемент после input. textContent ИЛИ img.alt ИЛИ
        title ИЛИ aria-label.

        Маркировка DOM (постоянные маркеры для инжектора):
          input.dataset.usbQIndex  = номер вопроса (1-based)
          input.dataset.usbOption  = нормализованный текст варианта
          img.dataset.usbQIndex / .usbOption — если текст взят из alt картинки.
          input.dataset.usbTextInput = '1' — для текстовых полей (options пустые).

        Возвращает JSON: [{question_index, type, question_text, options}].
          type = 'radio' | 'checkbox' | 'text' (text — без вариантов, ответ
          ставится инжектором в placeholder через action=placeholder_hint).
        """
        js = r"""(() => {
  // Очистка старых маркеров (на случай повторного запуска на той же странице)
  document.querySelectorAll('[data-usb-q-index]').forEach(el => delete el.dataset.usbQIndex);
  document.querySelectorAll('[data-usb-option]').forEach(el => delete el.dataset.usbOption);
  document.querySelectorAll('[data-usb-text-input]').forEach(el => delete el.dataset.usbTextInput);

  const results = [];
  const radioInputs = Array.from(document.querySelectorAll('input[type=radio], input[type=checkbox]'));
  // Текстовые поля — отдельные вопросы (если не внутри radio/checkbox блока)
  const TEXT_SEL = 'input[type=text], input[type=number], input[type=email], input[type=url], input[type=search], input[type=tel], textarea, input:not([type])';
  const textInputs = Array.from(document.querySelectorAll(TEXT_SEL));

  // Наименьший предок inp, содержащий >=2 input'ов того же типа = "блок вопроса".
  const findQuestionBlock = (inp) => {
    const type = inp.type;
    let el = inp;
    for (let i = 0; i < 12; i++) {
      if (!el.parentElement) return inp;
      el = el.parentElement;
      const sameType = el.querySelectorAll('input[type=' + type + ']');
      if (sameType.length >= 2) return el;
    }
    return inp;
  };

  // Группируем radio/checkbox по их блоку вопроса
  const blockMap = new Map();
  radioInputs.forEach(inp => {
    const block = findQuestionBlock(inp);
    if (!blockMap.has(block)) blockMap.set(block, []);
    blockMap.get(block).push(inp);
  });
  const radioBlocks = Array.from(blockMap.keys());

  // Служебные надписи Moodle — не текст вопроса
  const STOP_PHRASES = [
    'Выберите', 'Select', 'Mark the', 'Ответ:', 'Текст вопроса',
    'Question text', 'Укажите', 'Tick the'
  ];
  const isStopPhrase = (text) => {
    const t = text.trim().toLowerCase();
    if (t.length < 3 || t.length > 100) return false;
    return STOP_PHRASES.some(p => t.startsWith(p.toLowerCase()));
  };
  const norm = (s) => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();

  // "Текст" элемента: textContent → img.alt → первый img[alt] внутри → title → aria-label
  const getElementText = (el) => {
    if (!el) return '';
    const t = el.textContent.replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
    if (t) return t;
    if (el.tagName === 'IMG' && el.alt) return el.alt;
    const img = el.querySelector('img[alt]');
    if (img && img.alt) return img.alt;
    if (el.title) return el.title;
    if (el.getAttribute && el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    return '';
  };

  // Полный текст элемента ВКЛЮЧАЯ alt картинок — для вопросов вида
  // «текст <img alt="формула"> текст». textContent теряет alt, getElementText
  // возвращает textContent целиком и до img.alt не доходит. Здесь обходим
  // потомков в DOM-порядке и склеиваем текстовые узлы с img.alt — формула
  // попадает в question_text, и облако видит полный вопрос.
  const getElementRichText = (el) => {
    if (!el) return '';
    let out = '';
    const walk = (node) => {
      if (node.nodeType === 3) { // TEXT_NODE
        const s = (node.textContent || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ');
        if (s.trim()) out += s;
        return;
      }
      if (node.nodeType !== 1) return; // ELEMENT_NODE
      if (node.tagName === 'IMG') {
        const a = (node.alt || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
        if (a) out += (out && !out.endsWith(' ') ? ' ' : '') + a;
        return; // внутрь img не заходим
      }
      if (node.tagName === 'SCRIPT' || node.tagName === 'STYLE') return;
      for (const c of node.childNodes) walk(c);
    };
    walk(el);
    return norm(out);
  };

  // 8 уровней fallback для текста варианта
  const extractOptionText = (inp, block) => {
    let optText = '';
    // 1. label[for="ID"] внутри block
    if (inp.id) {
      try {
        const lbl = block.querySelector('label[for="' + CSS.escape(inp.id) + '"]');
        if (lbl) optText = getElementText(lbl);
      } catch (e) {}
    }
    // 2. closest('label')
    if (!optText) {
      const lbl = inp.closest('label');
      if (lbl) optText = getElementText(lbl);
    }
    // 3. nextElementSibling (первые 5 соседей)
    if (!optText) {
      let sib = inp.nextElementSibling, attempts = 0;
      while (sib && attempts < 5) {
        const t = getElementText(sib);
        if (t) { optText = t; break; }
        sib = sib.nextElementSibling; attempts++;
      }
    }
    // 4. Ближайший предок с текстом (но не block)
    if (!optText) {
      let p = inp.parentElement;
      while (p && p !== block) {
        const t = getElementText(p);
        if (t) { optText = t; break; }
        p = p.parentElement;
      }
    }
    // 5. Text-сиблинги (text nodes и элементы без input'ов)
    if (!optText) {
      let sib = inp.nextSibling;
      while (sib) {
        if (sib.nodeType === 3 && sib.textContent.trim()) { optText = sib.textContent; break; }
        if (sib.nodeType === 1) {
          const t = getElementText(sib);
          if (t && !sib.querySelector('input')) { optText = t; break; }
        }
        sib = sib.nextSibling;
      }
    }
    // 6. Предыдущий сиблинг (текст может быть перед input)
    if (!optText) {
      let sib = inp.previousElementSibling, attempts = 0;
      while (sib && attempts < 3) {
        const t = getElementText(sib);
        if (t) { optText = t; break; }
        sib = sib.previousElementSibling; attempts++;
      }
    }
    // 7. img[alt] внутри block по позиции после этого input
    if (!optText) {
      const imgs = block.querySelectorAll('img[alt]');
      let found = false;
      for (const img of imgs) {
        if (found && img.alt) { optText = img.alt; break; }
        if (img === inp || img.contains(inp) || inp.contains(img)) found = true;
      }
    }
    // 8. LAST RESORT: любой элемент после input в DOM-порядке внутри block
    if (!optText) {
      const allDesc = block.querySelectorAll('*');
      let found = false;
      for (const el of allDesc) {
        if (found) {
          const t = getElementText(el);
          if (t && !el.querySelector('input') && el !== inp) { optText = t; break; }
        }
        if (el === inp || el.contains(inp)) found = true;
      }
    }
    return optText;
  };

  // Текст вопроса для блока — общий для radio/checkbox и text-input
  const findQuestionText = (block) => {
    let qText = '';
    let searchRoot = block;
    for (let depth = 0; depth < 6; depth++) {
      if (!searchRoot.parentElement) break;
      searchRoot = searchRoot.parentElement;
      const moodleSelectors = ['.qtext', '.questiontext', '.q-text', '.qtitle', '.stem', '.prompt-text', '.formulation'];
      for (const sel of moodleSelectors) {
        const els = searchRoot.querySelectorAll(sel);
        for (const el of els) {
          if (block.contains(el) || el.contains(block)) continue;
          const text = norm(getElementRichText(el));
          if (text.length > 10 && !isStopPhrase(text)) { qText = text; break; }
        }
        if (qText) break;
      }
      if (qText) break;
      for (const child of searchRoot.children) {
        if (child === block || child.contains(block) || block.contains(child)) continue;
        if (child.querySelector('input[type=radio], input[type=checkbox]')) continue;
        const text = norm(getElementRichText(child));
        if (text.length > 15 && !isStopPhrase(text)) { qText = text; break; }
      }
      if (qText) break;
    }
    if (!qText) {
      const legend = block.querySelector('legend');
      if (legend) { const t = norm(getElementRichText(legend)); if (t && !isStopPhrase(t)) qText = t; }
    }
    if (qText.length > 600) qText = qText.substring(0, 600);
    return qText;
  };

  // Текстовые поля, НЕ лежащие внутри radio/checkbox блока — отдельные вопросы.
  // Поле внутри блока radio (напр. "Другое: ___") — часть того вопроса, не отдельный.
  const textUnits = [];
  textInputs.forEach(inp => {
    const insideRadio = radioBlocks.some(b => b === inp || b.contains(inp));
    if (insideRadio) return;
    textUnits.push({ block: inp, inp: inp });
  });

  // Единый список единиц вопросов: radio/checkbox блоки + текстовые поля
  const units = [];
  radioBlocks.forEach(b => units.push({ block: b, inputs: blockMap.get(b), qtype: blockMap.get(b)[0].type }));
  textUnits.forEach(u => units.push({ block: u.block, inputs: [u.inp], qtype: 'text' }));

  // Сортировка в DOM-порядке — question_index идёт сверху вниз по странице
  units.sort((a, b) => {
    if (a.block === b.block) return 0;
    const pos = a.block.compareDocumentPosition(b.block);
    if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
    if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
    return 0;
  });

  let qIndex = 0;
  for (const unit of units) {
    qIndex++;
    const block = unit.block;
    const inputs = unit.inputs;
    const type = unit.qtype;
    const qText = findQuestionText(block);

    if (type === 'text') {
      // Текстовый вопрос: вариантов нет, маркируем само поле для инжектора
      const inp = inputs[0];
      inp.dataset.usbQIndex = String(qIndex);
      inp.dataset.usbTextInput = '1';
      results.push({ question_index: qIndex, type: 'text', question_text: qText, options: [] });
      continue;
    }

    // radio/checkbox: options + МАРКИРОВКА input и img
    const options = [];
    inputs.forEach(inp => {
      const optText = extractOptionText(inp, block);
      if (optText && optText.trim()) {
        const normOpt = norm(optText);
        options.push(normOpt);
        inp.dataset.usbQIndex = String(qIndex);
        inp.dataset.usbOption = normOpt;
        // Если текст взят из alt — маркируем и саму картинку
        const lbl = inp.closest('label') || inp.parentElement;
        if (lbl) {
          const img = lbl.tagName === 'IMG' ? lbl : lbl.querySelector('img[alt]');
          if (img && img.alt && norm(img.alt) === normOpt) {
            img.dataset.usbQIndex = String(qIndex);
            img.dataset.usbOption = normOpt;
          }
        }
      }
    });

    results.push({
      question_index: qIndex,
      type: type,
      question_text: qText,
      options: options
    });
  }
  return JSON.stringify(results);
})()"""
        try:
            result = self.evaluate(js)
            # JS-ошибки возвращаются как exceptionDetails, а не как Python-исключение —
            # ловим явно, иначе метод молча вернёт "[]" и облако останется без структуры.
            exc = result.get("result", {}).get("exceptionDetails")
            if exc:
                err_text = exc.get("exception", {}).get("description", str(exc))[:300]
                _log.error("get_test_structure JS-ошибка: %s", err_text)
                return "[]"
            value = result.get("result", {}).get("result", {}).get("value")
            if not value:
                _log.warning("get_test_structure: JS вернул пустое значение")
                return "[]"
            if isinstance(value, str):
                return value
            return json.dumps(value, ensure_ascii=False)
        except Exception as e:
            _log.warning("get_test_structure упал: %s", e)
            return "[]"

    def inject_script(self, script: str) -> bool:
        result = self.evaluate(script)
        exc = result.get("result", {}).get("exceptionDetails")
        if exc is not None:
            return False
        # Скрипт возвращает true, если элемент найден и изменён;
        # false — если querySelector не нашёл селектор. Проверяем значение,
        # а не только отсутствие исключения — иначе счётчик «применено» врёт.
        try:
            return result["result"]["result"]["value"] is True
        except (KeyError, TypeError):
            return False

    def close(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
