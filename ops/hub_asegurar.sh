#!/bin/zsh
# Garantiza que el hub IBKR (solo lectura, 127.0.0.1:8791) esté conectado al
# gateway. Si el hub no responde lo arranca y espera a que conecte.
# Uso: hub_asegurar.sh [--hasta HH:MM] [--avisar]
#   --hasta HH:MM  sigue esperando hasta esa hora de Nueva York (por defecto 90 s)
#   --avisar       si al final no hay conexión, notificación en el Mac
# Sale 0 si conectado, 1 si no.
PY=${PY:-/opt/miniconda3/bin/python3}
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
# copia autónoma del hub (solo ibapi + stdlib) en $RAIZ/hub; si no, la instalación de sentinel-lite
if [ -f "$RAIZ/hub/ibkr_hub.py" ]; then LITE=${LITE:-$RAIZ/hub}; else LITE=${LITE:-$HOME/Desktop/SENTINEL-lite/sentinel-lite}; fi
HASTA=""; AVISAR=0
while [ $# -gt 0 ]; do
  case "$1" in
    --hasta) HASTA="$2"; shift 2 ;;
    --avisar) AVISAR=1; shift ;;
    *) shift ;;
  esac
done
vivo() { curl -s -m 3 http://127.0.0.1:8791/health 2>/dev/null | grep -q '"conectado": *true'; }
responde() { curl -s -m 2 http://127.0.0.1:8791/health >/dev/null 2>&1; }
arrancar() {
  if ! responde && ! pgrep -f "ibkr_hub.py" >/dev/null; then
    echo "[hub_asegurar] hub no responde; arrancando ibkr_hub.py"
    mkdir -p "$LITE/logs"
    (cd "$LITE" && IBKR_HOST=${IBKR_HOST:-127.0.0.1} IBKR_PORT=${IBKR_PORT:-4001} nohup "$PY" ibkr_hub.py >> logs/ibkr_hub.stdout 2>&1 &)
  fi
}
vivo && exit 0
if [ -n "$HASTA" ]; then
  LIMITE=$(TZ=America/New_York date -j -f "%Y-%m-%d %H:%M" "$(TZ=America/New_York date +%Y-%m-%d) $HASTA" +%s)
  DESC="$HASTA ET"
else
  LIMITE=$(( $(date +%s) + 90 ))
  DESC="90 s"
fi
arrancar
echo "[hub_asegurar] sin conexión al gateway; espero hasta $DESC"
while [ "$(date +%s)" -lt "$LIMITE" ]; do
  sleep 10
  vivo && { echo "[hub_asegurar] hub conectado a las $(TZ=America/New_York date +%H:%M:%S)"; exit 0; }
  arrancar
done
echo "[hub_asegurar] hub sin conexión al gateway tras esperar hasta $DESC"
if [ "$AVISAR" = 1 ]; then
  "$RAIZ/ops/notificar.sh" "Odyssey: sin conexión con IBKR" \
    "El gateway no respondió hasta $DESC. Revisa IB Gateway y la aprobación 2FA."
fi
exit 1
