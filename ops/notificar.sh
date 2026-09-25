#!/bin/zsh
# Notificación en el Mac (Centro de notificaciones) y registro en logs/avisos.log.
# Uso: notificar.sh "título" "mensaje"
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$RAIZ/logs"
echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  $1 — $2" >> "$RAIZ/logs/avisos.log"
T="${1//\"/\'}"; M="${2//\"/\'}"
/usr/bin/osascript -e "display notification \"$M\" with title \"$T\" sound name \"Glass\"" 2>> "$RAIZ/logs/avisos.log"
