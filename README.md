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

Con el hub y el gateway arriba:

```sh
python3 src/apertura_mdn.py SPY AAPL META MSFT              # tras el cierre
python3 src/apertura_mdn.py SPY AAPL META MSFT --vivo       # 09:15 ET, con pre-market
python3 src/apertura_mdn.py SPY AAPL META MSFT --pre-cierre --sin-ablacion   # en sesión, ~15:40 ET
python3 src/opciones_apertura.py SPY AAPL META MSFT         # strikes CALL/PUT largo y corto
python3 src/opciones_paper.py SPY AAPL META MSFT            # ~09:36 ET, mide en papel lo marcado la víspera
```

Salida en `data/derived/apertura_mdn/`: tabla de intervalos, escalera de
precios, backtest, y los informes `apertura_mdn_informe.md` /
`opciones_informe.md`.

## Tarea diaria automática (opcional)

Los runners de `ops/` y el generador de plists `ops/generar_plists.py`
están listos para macOS `launchd`, pero **launchd no puede leer una carpeta
dentro de `~/Desktop`, `~/Documents` ni `~/Downloads`** por la protección de
privacidad de macOS. Para automatizarlo:

```sh
cp -R "Oddyssey Prediccion de apertura" ~/Odyssey   # o cualquier ruta fuera del Escritorio
cd ~/Odyssey
python3 ops/generar_plists.py ~/Odyssey
for l in com.odyssey.apertura-vivo com.odyssey.apertura-paper com.odyssey.apertura-precierre; do
  cp "ops/$l.plist" ~/Library/LaunchAgents/
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$l.plist
done
```

En Linux, usar `cron` en vez de `launchd` con los mismos scripts de `ops/`.

Log común en `logs/apertura_mdn.log`. Ninguno de estos scripts envía
órdenes: son de solo lectura y simulación en papel.
