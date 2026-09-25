#!/bin/zsh
# 09:15 ET, lunes a viernes: distribución de apertura con el pre-market vivo
# (guarda el snapshot overnight del día). Espera al gateway hasta las 09:27.
# El .venv no tiene torch: python de miniconda.
PY=${PY:-/opt/miniconda3/bin/python3}
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') apertura_mdn --vivo"
  ops/hub_asegurar.sh --hasta 09:27 --avisar || exit 1
  "$PY" src/apertura_mdn.py SPY AAPL META MSFT NVDA --vivo --sin-ablacion "$@"
} >> logs/apertura_mdn.log 2>&1
