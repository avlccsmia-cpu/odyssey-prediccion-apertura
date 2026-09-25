#!/bin/zsh
# 09:36 ET, lunes a viernes:
#   1) evaluar_aperturas: exactitud de todas las proyecciones pendientes (se pone al día solo)
#   2) opciones_paper   : P&L en papel, a precios de la apertura, de lo marcado la víspera
# Espera al gateway hasta las 09:50. Solo registro; nunca envía órdenes.
PY=${PY:-/opt/miniconda3/bin/python3}
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') evaluar_aperturas + opciones_paper"
  ops/hub_asegurar.sh --hasta 09:50 --avisar || exit 1
  "$PY" src/evaluar_aperturas.py
  "$PY" src/opciones_paper.py SPY AAPL META MSFT NVDA
} >> logs/apertura_mdn.log 2>&1
