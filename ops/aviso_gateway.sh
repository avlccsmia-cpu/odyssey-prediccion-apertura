#!/bin/zsh
# 09:10 ET, lunes a viernes: si el hub no tiene conexión con IB Gateway, avisa
# en el Mac para que haya tiempo de iniciar sesión antes del job de las 09:15.
# Con --prueba envía la notificación sin comprobar nada (para verificar que llega).
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
if [ "$1" = "--prueba" ]; then
  ops/notificar.sh "Odyssey: prueba de aviso" "Así te avisaré si a las 9:10 IB Gateway no está conectado."
  exit 0
fi
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') aviso_gateway"
  if ops/hub_asegurar.sh; then
    echo "[aviso_gateway] conectado; sin aviso"
  else
    ops/notificar.sh "Odyssey: IB Gateway sin conexión" \
      "A las 9:10 no hay conexión con IBKR. Inicia sesión antes de las 9:27 para no perder el pre-market."
  fi
} >> logs/apertura_mdn.log 2>&1
