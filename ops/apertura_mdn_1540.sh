#!/bin/zsh
# 15:40 ET, lunes a viernes: decide la operación cierre→apertura.
#   1) apertura_mdn --pre-cierre : distribución de la apertura de mañana con el precio actual
#   2) opciones_apertura         : strikes CALL/PUT, largo y corto, con la cadena viva
# Solo genera ficheros; no envía órdenes. La medición en papel la hace apertura_mdn_0936.sh.
PY=${PY:-/opt/miniconda3/bin/python3}
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') apertura_mdn --pre-cierre + opciones_apertura"
  "$(dirname "$0")/hub_asegurar.sh" || exit 1
  "$PY" src/apertura_mdn.py SPY AAPL META MSFT --pre-cierre --sin-ablacion "$@" && \
  "$PY" src/opciones_apertura.py SPY AAPL META MSFT
} >> logs/apertura_mdn.log 2>&1
