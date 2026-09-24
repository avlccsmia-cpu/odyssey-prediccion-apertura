#!/bin/zsh
# Garantiza que el hub IBKR de sentinel-lite (solo lectura, 127.0.0.1:8791) esté
# conectado. Si no responde lo arranca contra el gateway local (4001) y espera
# hasta 90 s. Sale 0 si conectado, 1 si no.
PY=${PY:-/opt/miniconda3/bin/python3}
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
# copia autónoma del hub (solo ibapi + stdlib) en $RAIZ/hub; si no, la instalación de sentinel-lite
if [ -f "$RAIZ/hub/ibkr_hub.py" ]; then LITE=${LITE:-$RAIZ/hub}; else LITE=${LITE:-$HOME/Desktop/SENTINEL-lite/sentinel-lite}; fi
vivo() { curl -s -m 3 http://127.0.0.1:8791/health 2>/dev/null | grep -q '"conectado": *true'; }
vivo && exit 0
if ! curl -s -m 2 http://127.0.0.1:8791/health >/dev/null 2>&1; then
  echo "[hub_asegurar] hub no responde; arrancando ibkr_hub.py"
  mkdir -p "$LITE/logs"
  (cd "$LITE" && IBKR_HOST=${IBKR_HOST:-127.0.0.1} IBKR_PORT=${IBKR_PORT:-4001} nohup "$PY" ibkr_hub.py >> logs/ibkr_hub.stdout 2>&1 &)
fi
for i in {1..30}; do sleep 3; vivo && { echo "[hub_asegurar] hub conectado"; exit 0; }; done
echo "[hub_asegurar] hub sin conexión al gateway tras 90 s"; exit 1
