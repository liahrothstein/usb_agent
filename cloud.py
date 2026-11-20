"""Gemini API клиент с инструментом Google Search.

Использует REST-эндпоинт generativelanguage.googleapis.com.
Не требует SDK — только urllib из стандартной библиотеки.

Модели (актуальные на момент написания, проверь в Google AI Studio):
- gemini-3.8-flash  — текущая стабильная, поддерживает google_search
- gemini-2.5-flash  — может быть ещё доступна как fallback
- gemini-1.5-flash  — старый fallback; для поиска использует
                      инструмент google_search_retrieval (другой формат)

Примечание: gemini-2.0-flash была отключена Google (deprecated Feb 2026,
retired Jun 2026). API возвращает 404 с подсказкой использовать
gemini-3.8-flash.
"""
import json
import urllib.request
import urllib.error
from typing import Optional


class GeminiClient:
    def __init__(self, api_key: str, model: str = "gemini-3.8-flash",
                 use_search: bool = True, timeout: int = 30):
        self.api_key = api_key
        self.model = model
        self.use_search = use_search
        self.timeout = timeout

    def ask(self, prompt: str, system: Optional[str] = None,
            temperature: Optional[float] = None) -> dict:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        # temperature для self-consistency (несколько выборок + голосование).
        # None = серверный дефолт Gemini.
        if temperature is not None:
            body["generationConfig"] = {"temperature": temperature}
        # Инструмент поиска. Для 1.5-семьи другой ключ.
        if self.use_search:
            if self.model.startswith("gemini-1.5"):
                body["tools"] = [{"google_search_retrieval": {}}]
            else:
                body["tools"] = [{"google_search": {}}]

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gemini HTTP {e.code}: {err[:400]}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Gemini network error: {e.reason}") from None

        # Извлекаем текст ответа
        text = ""
        try:
            parts = raw["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if "text" in p)
        except (KeyError, IndexError, TypeError):
            pass

        # Извлекаем источники из grounding metadata
        sources = []
        try:
            gm = raw["candidates"][0]["groundingMetadata"]
            for chunk in gm.get("groundingChunks", []):
                web = chunk.get("web", {})
                if web.get("uri"):
                    sources.append({
                        "uri": web["uri"],
                        "title": web.get("title", "") or web["uri"],
                    })
        except (KeyError, IndexError, TypeError):
            pass

        return {"text": text.strip(), "sources": sources}
