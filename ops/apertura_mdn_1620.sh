#!/bin/zsh
# 16:20 ET, lunes a viernes: proyección de la apertura siguiente con el cierre real
# (modelo sin pre-market). Además deja la caché del hub con la sesión de hoy, para que
# el job de las 9:15 no tenga que pedir histórico a IBKR justo antes de la apertura
# (28-sep-2026: sin esa caché tardó 17 min y terminó después de la apertura).
# Espera al gateway hasta las 16:35. Solo genera ficheros; nunca envía órdenes.
PY=${PY:-/opt/miniconda3/bin/python3}
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') apertura_mdn tras el cierre"
  ops/hub_asegurar.sh --hasta 16:35 --avisar || exit 1
  "$PY" src/apertura_mdn.py SPY AAPL META MSFT NVDA --sin-ablacion "$@"
} >> logs/apertura_mdn.log 2>&1
