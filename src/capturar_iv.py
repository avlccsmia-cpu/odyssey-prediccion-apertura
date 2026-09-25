#!/usr/bin/env python3
"""
capturar_iv.py — Captura diaria propia de la volatilidad implícita (sustituye a
theta_vacuum, cuya exportación externa está parada desde abril de 2026).

Para cada ticker pide al hub IBKR (/chain) la cadena CALL y PUT del viernes a
7-11 DTE y guarda una fila por día en data/raw/iv_diaria/<TICKER>.csv:
  fecha, hora, spot, iv_atm (media C/P del strike más cercano al spot),
  skew (IV put Δ-0.25 − IV call Δ0.25), expiry, dte
Misma definición que las features que apertura_mdn.py saca de theta_vacuum
(snapshot 15:30 ET, 7-14 DTE), para que las dos fuentes se puedan unir.

Correr en sesión, ~15:40 ET (lo hace ops/apertura_mdn_1540.sh). Idempotente por
día: si ya hay fila para hoy la reemplaza por la nueva. Solo lectura.

Uso:  python3 src/capturar_iv.py [TICKERS...] [--dir RUTA]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import pandas as pd

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
from apertura_mdn import ET, RAIZ, hub_ok, iv_vivo, spot_snapshot  # noqa: E402

IV_DIR = os.path.join(RAIZ, "data", "raw", "iv_diaria")
TICKERS = ["SPY", "AAPL", "META", "MSFT", "NVDA"]
CAMPOS = ["fecha", "hora", "spot", "iv_atm", "skew", "expiry", "dte"]


def ultimo_cierre(ticker: str, directorio_cache: str) -> float | None:
    ruta = os.path.join(directorio_cache, f"{ticker}_1day_15Y_rth1.parquet")
    if os.path.exists(ruta):
        return float(pd.read_parquet(ruta)["Close"].iloc[-1])
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*", default=TICKERS)
    ap.add_argument("--dir", default=IV_DIR, help="carpeta de salida")
    args = ap.parse_args()
    if not hub_ok():
        print("ERROR: el hub IBKR no responde o no está conectado", file=sys.stderr)
        sys.exit(2)
    os.makedirs(args.dir, exist_ok=True)
    cache = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(args.dir))), "raw", "hub_cache")
    if not os.path.isdir(cache):
        cache = os.path.join(RAIZ, "data", "raw", "hub_cache")
    ahora = pd.Timestamp.now(tz=ET)
    fallos = 0
    for t in [x.upper() for x in args.tickers]:
        ref = ultimo_cierre(t, cache)
        spot = None
        for intento in range(3):          # el spot del snapshot a veces llega vacío: reintentar
            try:
                spot = spot_snapshot(t, ref if ref else 100.0)
                break
            except Exception as e:
                print(f"[{t}] sin spot (intento {intento + 1}/3): {e}", file=sys.stderr)
        if spot is None:
            fallos += 1
            continue
        r = iv_vivo(t, spot)
        if not r:
            print(f"[{t}] sin IV en la cadena", file=sys.stderr)
            fallos += 1
            continue
        ahora = pd.Timestamp.now(tz=ET)
        dte = (dt.datetime.strptime(r["expiry"], "%Y%m%d").date() - ahora.date()).days
        fila = {"fecha": str(ahora.date()), "hora": ahora.strftime("%H:%M:%S"), "spot": round(float(r["spot_hub"]), 4),
                "iv_atm": round(r["iv_atm"], 5), "skew": round(r["skew"], 5), "expiry": r["expiry"], "dte": dte}
        ruta = os.path.join(args.dir, f"{t}.csv")
        nueva = pd.DataFrame([fila])[CAMPOS]
        if os.path.exists(ruta):
            prev = pd.read_csv(ruta, dtype={"expiry": str})
            prev = prev[prev["fecha"] != fila["fecha"]]
            if len(prev):
                nueva = pd.concat([prev[CAMPOS], nueva], ignore_index=True)
        nueva.to_csv(ruta, index=False)
        print(f"[{t}] {fila['fecha']} {fila['hora']}  spot {fila['spot']}  iv_atm {fila['iv_atm']:.4f}  skew {fila['skew']:+.4f}  exp {fila['expiry']} ({dte} DTE)")
    sys.exit(1 if fallos == len(args.tickers) else 0)


if __name__ == "__main__":
    main()
