#!/usr/bin/env bash
# Останавливает llama-server и Python-агента. Chrome оставляет запущенным.
echo "Останавливаю llama-server и агент..."
pkill -f llama-server 2>/dev/null || true
pkill -f "main.py" 2>/dev/null || true
echo "Готово. Chrome остаётся запущенным."
