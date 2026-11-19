"""Клиент к локальному llama.cpp-серверу.

llama-server (из llama.cpp) выставляет OpenAI-совместимый API на
/v1/chat/completions. Это позволяет не тянуть тяжёлый SDK —
достаточно urllib из стандартной библиотеки.
"""
import json
import urllib.request
import urllib.error


class LocalLLM:
    def __init__(self, base_url: str = "http://127.0.0.1:8080",
                 model: str = "local", timeout: int = 45):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def chat(self, system: str, user: str,
             temperature: float = 0.3, max_tokens: int = 700) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"local LLM HTTP {e.code}: {err[:300]}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"local LLM недоступен на {self.base_url}: {e.reason}. "
                "Проверь, что llama-server запущен."
            ) from None
        try:
            return body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError):
            raise RuntimeError(f"local LLM: неожиданный ответ: {str(body)[:300]}")

    def is_alive(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False
