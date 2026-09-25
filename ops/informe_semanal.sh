#!/bin/zsh
# Viernes 16:30 ET: evalúa lo pendiente y escribe el informe semanal de exactitud.
PY=${PY:-/opt/miniconda3/bin/python3}
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') informe_semanal"
  ops/hub_asegurar.sh && "$PY" src/evaluar_aperturas.py
  "$PY" src/informe_semanal.py && \
    ops/notificar.sh "Odyssey: informe semanal listo" "data/derived/apertura_mdn/informes_semanales/informe_$(date +%Y-%m-%d).md"
} >> logs/apertura_mdn.log 2>&1
