# Odyssey — Predicción de apertura

Distribución probabilística del precio de apertura del día siguiente (Mixture
Density Network + Monte Carlo) y evaluador de opciones CALL/PUT para la
operación cierre→apertura. Carpeta autocontenida, pensada para copiarse a
otro equipo.

## Qué necesita esa otra máquina

1. **IB Gateway o TWS** instalado, con sesión iniciada y la API habilitada
   (Configuración → API → Settings → "Enable ActiveX and Socket Clients",
   añadir `127.0.0.1` a las IP de confianza). Anota el puerto: 4001 gateway
   real, 4002 gateway papel, 7496/7497 TWS.

2. **Python 3.12** con las dependencias de `requirements.txt`:
   ```sh
   python3 -m pip install -r requirements.txt
   ```

3. **El hub de solo lectura** (`hub/ibkr_hub.py`), que hace de intermediario
   entre este programa e IBKR — nunca envía órdenes. Arranca solo si hace
   falta con `ops/hub_asegurar.sh`, o a mano:
   ```sh
   cd hub && IBKR_HOST=127.0.0.1 IBKR_PORT=4001 python3 ibkr_hub.py
   ```
   Sirve en `http://127.0.0.1:8791`. Si el gateway de esa máquina usa otro
   puerto, ajusta `IBKR_PORT`.

Los datos de `data/raw/theta_vacuum` (IV histórica) y `data/raw/bars_5m`
(velas de 5 min) vinieron copiados como ejemplo; son opcionales — si faltan
o quedan desactualizados el script sigue funcionando, solo entrena sin esas
features y lo avisa en el log.

## Uso

Con el hub y el gateway arriba (tickers por defecto: SPY, AAPL, META, MSFT, NVDA):

```sh
python3 src/apertura_mdn.py                          # tras el cierre
python3 src/apertura_mdn.py --vivo                   # 09:15 ET, con pre-market
python3 src/apertura_mdn.py --pre-cierre --sin-ablacion   # en sesión, ~15:40 ET
python3 src/apertura_mdn.py --hasta 2026-09-23       # reconstruye la proyección de ese cierre sin datos posteriores
python3 src/capturar_iv.py                           # ~15:40 ET, IV ATM y skew del día
python3 src/opciones_apertura.py                     # strikes CALL/PUT largo y corto
python3 src/evaluar_aperturas.py                     # exactitud de todas las proyecciones pendientes
python3 src/opciones_paper.py                        # 09:31-10:05 ET, P&L en papel de lo marcado la víspera
python3 src/informe_semanal.py                       # resumen de exactitud y P&L en papel
```

Salida en `data/derived/apertura_mdn/`: tabla de intervalos, escalera de
precios, backtest, informes `apertura_mdn_informe.md` y `opciones_informe.md`,
el archivo de proyecciones en `proyecciones/`, `evaluaciones.jsonl` e
`informes_semanales/`. La IV propia se guarda en `data/raw/iv_diaria/`.

Sin el dato de pre-market (tras el cierre, `--pre-cierre`, reconstrucciones
o `--vivo` fallido) la proyección usa el modelo entrenado sin pre-market, y
el backtest que la acompaña corresponde a ese modelo. En el backtest de 250
sesiones del 25-sep-2026 esto subió la cobertura del intervalo del 80 % de
58-70 % a 72-81 % y mejoró el CRPS en los cinco tickers. La proyección con
pre-market de las 09:15 es la que aporta dirección; la de la tarde apenas
mejora al baseline empírico de 250 días.

Cómo se mide la exactitud: cada proyección se archiva al generarse y se compara
con la apertura oficial de la barra diaria de IBKR (percentil, cobertura de los
intervalos, dirección, error contra "abre igual que el cierre" y CRPS contra el
baseline empírico de 250 días). Con menos de 30 observaciones por modo no se
cambia el modelo; un cambio solo se adopta si mejora el backtest de 250
sesiones sin empeorar la cobertura.

## Tarea diaria automática (opcional)

Los runners de `ops/` y el generador de plists `ops/generar_plists.py`
están listos para macOS `launchd`, pero **launchd no puede leer una carpeta
dentro de `~/Desktop`, `~/Documents` ni `~/Downloads`** por la protección de
privacidad de macOS. Para automatizarlo:

```sh
cp -R "Oddyssey Prediccion de apertura" ~/Odyssey   # o cualquier ruta fuera del Escritorio
cd ~/Odyssey
python3 ops/generar_plists.py ~/Odyssey
for l in com.odyssey.apertura-aviso com.odyssey.apertura-vivo com.odyssey.apertura-paper \
         com.odyssey.apertura-precierre com.odyssey.apertura-semanal; do
  cp "ops/$l.plist" ~/Library/LaunchAgents/
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$l.plist
done
```

| Hora ET | Qué hace | Espera al gateway hasta |
|---|---|---|
| 09:10 | avisa en el Mac si IB Gateway no está conectado | 90 s |
| 09:15 | proyección con el pre-market vivo | 09:27 |
| 09:36 | evalúa las proyecciones pendientes y mide el P&L en papel | 09:50 |
| 15:40 | captura IV, proyección `--pre-cierre`, strikes, evaluación pendiente | 15:52 |
| vie 16:30 | informe semanal y aviso | 90 s |

Si el gateway no conecta antes de la hora límite, el job avisa en el Mac
(`ops/notificar.sh`, registro en `logs/avisos.log`).

En Linux, usar `cron` en vez de `launchd` con los mismos scripts de `ops/`;
`ops/notificar.sh` usa `osascript` y solo funciona en macOS.

Log común en `logs/apertura_mdn.log`. Ninguno de estos scripts envía
órdenes: son de solo lectura y simulación en papel.
