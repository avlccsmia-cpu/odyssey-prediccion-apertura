#!/usr/bin/env python3
"""Genera los plists de launchd de la tarea diaria para una raíz de ejecución dada.
macOS impide a launchd leer ~/Desktop, ~/Documents y ~/Downloads: la raíz debe
estar fuera (p. ej. ~/Odyssey, clon del repo).  Uso: generar_plists.py [raiz]"""
import os
import sys

raiz = os.path.abspath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..")))
logs = os.path.expanduser("~/Library/Logs/Odyssey")
TAREAS = (("com.odyssey.apertura-vivo", "apertura_mdn_0915.sh", [(9, 15)]),
          ("com.odyssey.apertura-paper", "apertura_mdn_0936.sh", [(9, 36), (10, 30), (12, 30)]),   # reintentos si HMDS cae
          ("com.odyssey.apertura-precierre", "apertura_mdn_1540.sh", [(15, 40)]))


def plist(label, script, horas):
    cal = "".join(f"\n\t\t<dict>\n\t\t\t<key>Hour</key>\n\t\t\t<integer>{hour}</integer>\n\t\t\t<key>Minute</key>\n\t\t\t<integer>{minute}</integer>"
                  f"\n\t\t\t<key>Weekday</key>\n\t\t\t<integer>{wd}</integer>\n\t\t</dict>"
                  for hour, minute in horas for wd in range(1, 6))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>EnvironmentVariables</key>
\t<dict>
\t\t<key>PYTHONUNBUFFERED</key>
\t\t<string>1</string>
\t\t<key>TZ</key>
\t\t<string>America/New_York</string>
\t\t<key>PATH</key>
\t\t<string>/opt/miniconda3/bin:/usr/local/bin:/usr/bin:/bin</string>
\t</dict>
\t<key>Label</key>
\t<string>{label}</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>/bin/zsh</string>
\t\t<string>{raiz}/ops/{script}</string>
\t</array>
\t<key>StandardErrorPath</key>
\t<string>{logs}/{label}.err.log</string>
\t<key>StandardOutPath</key>
\t<string>{logs}/{label}.out.log</string>
\t<key>StartCalendarInterval</key>
\t<array>{cal}
\t</array>
\t<key>WorkingDirectory</key>
\t<string>{raiz}</string>
</dict>
</plist>
"""


for label, script, horas in TAREAS:
    ruta = os.path.join(raiz, "ops", f"{label}.plist")
    open(ruta, "w").write(plist(label, script, horas))
    print(ruta)
