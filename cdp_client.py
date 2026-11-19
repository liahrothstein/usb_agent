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
        """Извлекает все radio/checkbox вопросы со страницы.
        Группировка по общему предку (универсально).
        Извлекает текст варианта ИЗ textContent ИЛИ img.alt.
        """
        js = r"""
        (() => {
            // Очистка старых маркеров
            document.querySelectorAll('[data-usb-q-index]').forEach(el => delete el.dataset.usbQIndex);
            document.querySelectorAll('[data-usb-option]').forEach(el => delete el.dataset.usbOption);

            const results = [];
            const allInputs = Array.from(document.querySelectorAll('input[type=radio], input[type=checkbox]'));

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

            const blockMap = new Map();
            allInputs.forEach(inp => {
                const block = findQuestionBlock(inp);
                if (!blockMap.has(block)) blockMap.set(block, []);
                blockMap.get(block).push(inp);
            });

            const blocks = Array.from(blockMap.keys()).sort((a, b) => {
                if (a === b) return 0;
                const pos = a.compareDocumentPosition(b);
                if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
                if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
                return 0;
            });

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

            // Универсальный извлекатель "текста" элемента: textContent → img.alt → title
            const getElementText = (el) => {
                if (!el) return '';
                // 1. textContent (если есть непустой)
                const t = el.textContent.replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
                if (t) return t;
                // 2. Сам элемент — img с alt
                if (el.tagName === 'IMG' && el.alt) return el.alt;
                // 3. Первый img внутри с alt
                const img = el.querySelector('img[alt]');
                if (img && img.alt) return img.alt;
                // 4. title атрибут
                if (el.title) return el.title;
                // 5. aria-label
                if (el.getAttribute && el.getAttribute('aria-label')) return el.getAttribute('aria-label');
                return '';
            };

            // РАСШИРЕННЫЙ экстрактор текста варианта (8 уровней fallback)
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
                    let sib = inp.nextElementSibling;
                    let attempts = 0;
                    while (sib && attempts < 5) {
                        const t = getElementText(sib);
                        if (t) { optText = t; break; }
                        sib = sib.nextElementSibling;
                        attempts++;
                    }
                }
                // 4. Parent — ближайший предок с текстом (но не block)
                if (!optText) {
                    let p = inp.parentElement;
                    while (p && p !== block) {
                        const t = getElementText(p);
                        if (t) { optText = t; break; }
                        p = p.parentElement;
                    }
                }
                // 5. Text-сиблинги
                if (!optText) {
                    let sib = inp.nextSibling;
                    while (sib) {
                        if (sib.nodeType === 3 && sib.textContent.trim()) {
                            optText = sib.textContent; break;
                        }
                        if (sib.nodeType === 1) {
                            const t = getElementText(sib);
                            if (t && !sib.querySelector('input')) { optText = t; break; }
                        }
                        sib = sib.nextSibling;
                    }
                }
                // 6. Предыдущий сиблинг (текст может быть перед input)
                if (!optText) {
                    let sib = inp.previousElementSibling;
                    let attempts = 0;
                    while (sib && attempts < 3) {
                        const t = getElementText(sib);
                        if (t) { optText = t; break; }
                        sib = sib.previousElementSibling;
                        attempts++;
                    }
                }
                // 7. Все img внутри block (по позиции после этого input)
                if (!optText) {
                    const imgs = block.querySelectorAll('img[alt]');
                    let found = false;
                    for (const img of imgs) {
                        if (found && img.alt) { optText = img.alt; break; }
                        if (img === inp || img.contains(inp) || inp.contains(img)) found = true;
                    }
                }
                // 8. LAST RESORT: любой элемент после input в DOM-порядке
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

            let qIndex = 0;
            for (const block of blocks) {
                qIndex++;
                const inputs = blockMap.get(block);
                if (!inputs.length) continue;
                const type = inputs[0].type;

                // Текст вопроса
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
                            const text = norm(getElementText(el));
                            if (text.length > 10 && !isStopPhrase(text)) { qText = text; break; }
                        }
                        if (qText) break;
                    }
                    if (qText) break;
                    for (const child of searchRoot.children) {
                        if (child === block || child.contains(block) || block.contains(child)) continue;
                        if (child.querySelector('input[type=radio], input[type=checkbox]')) continue;
                        const text = norm(getElementText(child));
                        if (text.length > 15 && !isStopPhrase(text)) { qText = text; break; }
                    }
                    if (qText) break;
                }
                if (!qText) {
                    const legend = block.querySelector('legend');
                    if (legend) { const t = norm(getElementText(legend)); if (t && !isStopPhrase(t)) qText = t; }
                }
                if (qText.length > 400) qText = qText.substring(0, 400);

                // Options + МАРКИРОВКА INPUT и IMG
                const options = [];
                inputs.forEach(inp => {
                    const optText = extractOptionText(inp, block);
                    if (optText && optText.trim()) {
                        const normOpt = norm(optText);
                        options.push(normOpt);
                        // МАРКИРОВКА input
                        inp.dataset.usbQIndex = String(qIndex);
                        inp.dataset.usbOption = normOpt;
                        // МАРКИРОВКА img (если текст взят из alt картинки)
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
        })()
        """
        try:
            result = self.evaluate(js)
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