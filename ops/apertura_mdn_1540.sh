#!/bin/zsh
# 15:40 ET, lunes a viernes: decide la operación cierre→apertura.
#   1) capturar_iv       : IV ATM y skew propios del día (sustituye a theta_vacuum)
#   2) apertura_mdn      : distribución de la apertura de mañana con el precio actual (--pre-cierre)
#   3) opciones_apertura : strikes CALL/PUT, largo y corto, con la cadena viva
#   4) evaluar_aperturas : se pone al día con lo que haya quedado pendiente
# Espera al gateway hasta las 15:52. Solo genera ficheros; nunca envía órdenes.
PY=${PY:-/opt/miniconda3/bin/python3}
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') capturar_iv + apertura_mdn --pre-cierre + opciones_apertura"
  ops/hub_asegurar.sh --hasta 15:52 --avisar || exit 1
  "$PY" src/capturar_iv.py SPY AAPL META MSFT NVDA
  "$PY" src/apertura_mdn.py SPY AAPL META MSFT NVDA --pre-cierre --sin-ablacion "$@" && \
  "$PY" src/opciones_apertura.py SPY AAPL META MSFT NVDA
  "$PY" src/evaluar_aperturas.py
} >> logs/apertura_mdn.log 2>&1
