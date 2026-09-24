#!/usr/bin/env python3
"""
opciones_paper.py — Medición en papel, en la apertura, de lo que marcó
opciones_apertura.py la tarde anterior. SOLO REGISTRO: no envía órdenes.

Correr ~09:36 ET (con reintentos a 10:30 y 12:30 por si el histórico de IBKR
no responde a la apertura; es idempotente: no repite un ticker ya medido). Para cada ticker:
  1. Lee data/derived/apertura_mdn/opciones_<T>.json (entrada de la sesión previa)
     y apertura_mdn_<T>.json (distribución prevista para hoy).
  2. Apertura real de hoy (hub, 1 min RTH): gap real, percentil (PIT) dentro de
     la distribución prevista y si cayó en los intervalos 80/90 %.
  3. Para los 4 óptimos (largo/corto × C/P): cotización actual del contrato
     (hub /chain) y P&L en papel:
        largo = (bid_ahora − ask_entrada)·100 − comisión
        corto = (bid_entrada − ask_ahora)·100 − comisión
  4. Añade una línea a opciones_paper.jsonl, archiva los JSON de entrada en
     historico/ y muestra el acumulado.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
OUT_DIR = os.path.join(RAIZ, "data", "derived", "apertura_mdn")
HIST = os.path.join(OUT_DIR, "historico")
LOG = os.path.join(OUT_DIR, "opciones_paper.jsonl")
HUB_URL = "http://127.0.0.1:8791"
ET = "America/New_York"


def log(m):
    print(m, file=sys.stderr, flush=True)


def hub_json(ruta, **q):
    with urllib.request.urlopen(f"{HUB_URL}{ruta}?{urllib.parse.urlencode(q)}", timeout=120) as r:
        d = json.loads(r.read())
    if not d.get("ok"):
        raise RuntimeError(d.get("error"))
    return d


def apertura_real(ticker, hoy):
    d = hub_json("/bars", ticker=ticker, dur="1 D", bar="1 min", rth=1, ttl=0)
    b = pd.DataFrame(d["bars"])
    ts = pd.to_datetime(b["t"].astype(int), unit="s", utc=True).dt.tz_convert(ET)
    b = b[ts.dt.date == hoy]
    if b.empty:
        raise RuntimeError("sin velas RTH de hoy todavía")
    return float(b["o"].iloc[0]), float(b["c"].iloc[-1])


def cotizacion(ticker, right, expiry, strike, spot):
    step = 1.0 if spot < 100 else (2.5 if spot < 400 else 5.0)
    d = hub_json("/chain", ticker=ticker, right=right, date=expiry, center=strike, width=1, step=step, ttl=0)
    for row in d["rows"]:
        if abs(row["strike"] - strike) < 1e-6:
            return row
    raise RuntimeError(f"strike {strike} no está en la cadena")


def main():
    tickers = [x.upper() for x in sys.argv[1:]] or ["SPY", "AAPL", "META", "MSFT"]
    hoy = pd.Timestamp.now(tz=ET).date()
    os.makedirs(HIST, exist_ok=True)
    registros = []
    ya = set()
    if os.path.exists(LOG):
        for l in open(LOG):
            if l.strip():
                r = json.loads(l)
                ya.add((r["fecha"], r["ticker"]))
    for t in tickers:
        if (str(hoy), t) in ya:
            log(f"[{t}] ya medido hoy; nada que hacer")
            continue
        ruta_op = os.path.join(OUT_DIR, f"opciones_{t}.json")
        ruta_ap = os.path.join(OUT_DIR, f"apertura_mdn_{t}.json")
        if not (os.path.exists(ruta_op) and os.path.exists(ruta_ap)):
            log(f"[{t}] sin entrada previa; nada que medir")
            continue
        op = json.load(open(ruta_op))
        ap = json.load(open(ruta_ap))
        f_entrada = dt.datetime.fromisoformat(op["generado"]).date()
        if f_entrada >= hoy or ap["apertura_objetivo"] != str(hoy):
            log(f"[{t}] entrada del {f_entrada} con objetivo {ap['apertura_objetivo']}; hoy es {hoy}: nada que medir")
            continue
        try:
            open_real, ultimo = apertura_real(t, hoy)
        except Exception as e:
            log(f"[{t}] apertura real no disponible: {e}")
            continue
        c0 = ap["ultimo_cierre"]
        gap = float(np.log(open_real / c0) * 100)
        q = ap["cuantiles_gap_pct"]
        pit = float(np.mean([q[str(k)] < gap for k in range(1, 100)]))
        tabla = {r["prob"]: r for r in ap["tabla"]}
        reg = {"fecha": str(hoy), "ticker": t, "entrada": str(f_entrada), "modo_modelo": ap["modo"],
               "cierre_ref": c0, "open_real": round(open_real, 2), "gap_real_pct": round(gap, 3),
               "gap_mediana_prev_pct": ap["gap_mediana_pct"], "p_gap_pos_prev": ap["prob_gap_positivo"],
               "pit": round(pit, 3),
               "en80": bool(tabla[80]["gap_min_pct"] <= gap <= tabla[80]["gap_max_pct"]),
               "en90": bool(tabla[90]["gap_min_pct"] <= gap <= tabla[90]["gap_max_pct"]),
               "signo_ok": bool(np.sign(ap["gap_mediana_pct"]) == np.sign(gap)),
               "contratos": []}
        for clave, f in op["optimos"].items():
            if not f:
                continue
            lado = clave.split("_")[0]
            try:
                row = cotizacion(t, f["right"], f["expiry"], f["strike"], c0)
                bid, ask = row.get("bid"), row.get("ask")
                if lado == "largo":
                    pnl = (bid - f["ask"]) * 100 - op["comision"] if bid is not None else None
                else:
                    pnl = (f["bid"] - ask) * 100 - op["comision"] if ask is not None else None
                reg["contratos"].append({"clave": clave, "expiry": f["expiry"], "right": f["right"], "strike": f["strike"],
                                         "entrada_bid": f["bid"], "entrada_ask": f["ask"], "salida_bid": bid, "salida_ask": ask,
                                         "esperanza_prevista": f[lado]["esperanza"], "p_benef_prevista": f[lado]["p_beneficio"],
                                         "pnl_papel": None if pnl is None else round(pnl, 2)})
            except Exception as e:
                log(f"[{t}] {clave} {f['right']} {f['strike']}: {e}")
        registros.append(reg)
        with open(LOG, "a") as fh:
            fh.write(json.dumps(reg, ensure_ascii=False) + "\n")
        for src in (ruta_op, ruta_ap):
            shutil.copy(src, os.path.join(HIST, os.path.basename(src).replace(".json", f"_{f_entrada}.json")))
        print(f"{t}: cierre {c0} -> open {open_real:.2f} gap {gap:+.2f}% (prev. mediana {ap['gap_mediana_pct']:+.2f}%, PIT {pit:.2f}, "
              f"en80 {'sí' if reg['en80'] else 'no'}, signo {'ok' if reg['signo_ok'] else 'no'})")
        for c in reg["contratos"]:
            print(f"    {c['clave']:8s} {c['expiry']} {c['right']} {c['strike']:.1f}  entrada {c['entrada_bid']}/{c['entrada_ask']}  "
                  f"salida {c['salida_bid']}/{c['salida_ask']}  P&L papel {c['pnl_papel']}  (previsto E {c['esperanza_prevista']:+.2f})")
    if os.path.exists(LOG):
        todos = [json.loads(l) for l in open(LOG) if l.strip()]
        n = len(todos)
        pnl = {}
        for r in todos:
            for c in r["contratos"]:
                if c["pnl_papel"] is not None:
                    pnl.setdefault(c["clave"], []).append(c["pnl_papel"])
        print(f"\nacumulado: {n} registros · cobertura 80 % {np.mean([r['en80'] for r in todos]):.0%} · signo {np.mean([r['signo_ok'] for r in todos]):.0%}")
        for k, v in sorted(pnl.items()):
            print(f"    {k:8s} n={len(v):3d}  suma {sum(v):+8.2f} $  media {np.mean(v):+7.2f} $  P(>0) {np.mean(np.array(v) > 0):.0%}")


if __name__ == "__main__":
    main()
