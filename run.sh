#!/usr/bin/env bash
# Linux/macOS launcher for USB Agent.
set -u
cd "$(dirname "$0")"

CHROME_PORT=9222
LLAMA_PORT=8080

echo "============================================"
echo " USB Agent launcher"
echo "============================================"

# 1. llama-server + GGUF
MODEL_FILE=$(ls models/*.gguf 2>/dev/null | head -n1 || true)
if [ -n "$MODEL_FILE" ]; then
    if [ -x ./llama/llama-server ] || [ -f ./llama/llama-server ]; then
        echo "[INFO] Запускаю llama-server с моделью $MODEL_FILE"
        chmod +x ./llama/llama-server 2>/dev/null || true
        ./llama/llama-server -m "$MODEL_FILE" --port "$LLAMA_PORT" --host 127.0.0.1 --cont-batching &
        LLAMA_PID=$!
        echo "[INFO] llama-server pid: $LLAMA_PID"
        echo "[INFO] Жду 6 сек..."
        sleep 6
    else
        echo "[WARN] ./llama/llama-server не найден."
        echo "       Скачай архив llama-bXXXX-bin-ubuntu-x64.zip (или аналог) из"
        echo "       https://github.com/ggml-org/llama.cpp/releases"
        echo "       и распакуй его содержимое в папку llama/ рядом с этим скриптом."
    fi
else
    echo "[WARN] В models/ нет .gguf файла. См. README.md."
fi

# 2. Chrome с remote debugging
CHROME_BIN=$(command -v google-chrome || command -v google-chrome-stable || command -v chromium || command -v chromium-browser || true)
if [ -n "$CHROME_BIN" ]; then
    echo "[INFO] Запускаю Chrome с --remote-debugging-port=$CHROME_PORT"
    "$CHROME_BIN" --remote-debugging-port="$CHROME_PORT" --user-data-dir="$PWD/chrome_profile" &
    CHROME_PID=$!
    sleep 2
else
    echo "[WARN] Chrome не найден. Запусти вручную:"
    echo "       google-chrome --remote-debugging-port=$CHROME_PORT"
fi

# 3. Python-агент
PYBIN=$(command -v python3 || command -v python || true)
if [ -z "$PYBIN" ]; then
    echo "[FATAL] Python не найден. Установи Python 3.8+."
    exit 1
fi
if ! "$PYBIN" -c "import websocket" 2>/dev/null; then
    echo "[INFO] Устанавливаю зависимости..."
    "$PYBIN" -m pip install -r requirements.txt
fi

echo "[INFO] Запускаю агент... (Ctrl+C для остановки)"
echo
exec "$PYBIN" main.py
