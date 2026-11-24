"""OpenRouter API клиент с поддержкой веб-поиска.

OpenRouter — агрегатор LLM с OpenAI-совместимым API.
Преимущества перед прямым Gemini API:
- Нет региональных ограничений (CDN без geo-блоков, работает из BY)
- Бесплатные модели (список: https://openrouter.ai/models)
- Регистрация через GitHub/Google, без привязки карты
- Веб-поиск через плагин {"id": "web"} (эквивалент суффикса :online)

API: POST https://openrouter.ai/api/v1/chat/completions
Авторизация: Bearer <OPENROUTER_API_KEY>
Документация: https://openrouter.ai/docs/api-reference/overview

Интерфейс .ask() совпадает с GeminiClient — main.py выбирает провайдер
по полю config["cloud"]["provider"] ("gemini" или "openrouter").
"""
import json
import re
import urllib.request
import urllib.error
from typing import Optional


class OpenRouterClient:
    def __init__(self, api_key: str, model: str = "google/gemini-2.0-flash-exp:free",
                 use_search: bool = True, timeout: int = 60, max_tokens: int = 8192,
                 cdp=None):
        self.api_key = api_key
        self.model = model
        self.use_search = use_search
        self.timeout = timeout
        self.max_tokens = max_tokens
        # Если задан cdp (CDPClient), ask() шлёт POST через fetch() браузера
        # (cdp.fetch_json) — запрос идёт от имени подключённой вкладки. Иначе
        # откатывается на urllib (Python).
        self.cdp = cdp

    def ask(self, prompt: str, system: Optional[str] = None,
            temperature: Optional[float] = None) -> dict:
        url = "https://openrouter.ai/api/v1/chat/completions"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": self.model,
            "messages": messages,
            # max_tokens обязателен для cloud_first: при 20+ вопросах
            # ответ с hints может быть 2000+ токенов. Без этого поля
            # OpenRouter режет на ~1024, JSON обрывается, часть hints теряется.
            "max_tokens": self.max_tokens,
            # Отключаем chain-of-thought для reasoning-моделей (nemotron, deepseek-r1).
            # Без этого модель тратит все токены на рассуждения и не доходит до JSON.
            # Для не-reasoning моделей параметр игнорируется OpenRouter.
            "reasoning": {"enabled": False},
        }
        # temperature включает случайность для self-consistency (несколько
        # выборок + голосование). None = серверный дефолт модели (обычно 1.0).
        # 0.6–0.8 — разумный диапазон: ответы расходятся, но не рандомильно.
        if temperature is not None:
            body["temperature"] = temperature
        # Веб-поиск через плагин (эквивалент суффикса :online в имени модели).
        # OpenRouter выполняет поиск и добавляет результаты в контекст модели.
        if self.use_search:
            body["plugins"] = [{"id": "web"}]

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/usb-agent",
            "X-OpenRouter-Title": "USB Agent",
        }
        if self.cdp is not None:
            # Новая фича: запрос через fetch() браузера (CDP Runtime.evaluate),
            # не через Python urllib. Идёт от имени подключённой вкладки тем же
            # network stack / proxy / user-agent. CORS OpenRouter — '*' (проверено).
            try:
                res = self.cdp.fetch_json(url, headers, body,
                                          timeout=self.timeout + 60)
            except RuntimeError as e:
                raise RuntimeError(f"OpenRouter fetch error: {e}") from None
            if res["status"] != 200:
                raise RuntimeError(
                    f"OpenRouter HTTP {res['status']}: {res['body_text'][:400]}"
                ) from None
            try:
                raw = json.loads(res["body_text"])
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"OpenRouter: не JSON ({e}): {res['body_text'][:300]}"
                ) from None
        else:
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers=headers, method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"OpenRouter HTTP {e.code}: {err[:400]}") from None
            except urllib.error.URLError as e:
                raise RuntimeError(f"OpenRouter network error: {e.reason}") from None

        # Извлекаем текст ответа.
        # Reasoning-модели (nemotron, deepseek-r1, qwen-reasoning) кладут
        # chain-of-thought в message.reasoning, а финальный ответ в message.content.
        # Если content пуст (модель потратила токены на reasoning) — берём reasoning.
        text = ""
        try:
            msg = raw["choices"][0]["message"]
            text = msg.get("content") or ""
            if not text:
                text = msg.get("reasoning") or ""
        except (KeyError, IndexError, TypeError):
            pass

        # Источники: Chat Completions API OpenRouter не отдаёт citations
        # отдельным полем. Модели часто вставляют URL прямо в текст —
        # извлекаем их регуляркой как best-effort.
        sources = []
        for m in re.finditer(r'https?://[^\s)"<>\]]+', text):
            uri = m.group(0).rstrip('.,;)')
            sources.append({"uri": uri, "title": uri})
        seen = set()
        unique = []
        for s in sources:
            if s["uri"] not in seen:
                seen.add(s["uri"])
                unique.append(s)
        sources = unique[:5]

        return {"text": text.strip(), "sources": sources}
