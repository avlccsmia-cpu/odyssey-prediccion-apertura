#!/bin/zsh
# 09:36 ET, lunes a viernes: mide en papel la apertura real y el P&L de los
# contratos marcados la tarde anterior. Solo registro.
PY=${PY:-/opt/miniconda3/bin/python3}
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') opciones_paper"
  "$(dirname "$0")/hub_asegurar.sh" || exit 1
  "$PY" src/opciones_paper.py SPY AAPL META MSFT
} >> logs/apertura_mdn.log 2>&1
