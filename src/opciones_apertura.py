#!/usr/bin/env python3
"""
opciones_apertura.py — Strike óptimo de CALL y PUT para la operación
"comprar al cierre de hoy, vender en la apertura de mañana", usando la
distribución de apertura de apertura_mdn.py y la cadena viva del hub IBKR.

Método
  1. Lee data/derived/apertura_mdn/apertura_mdn_<T>.json: cierre de referencia y
     los 99 percentiles del gap simulado. Reconstruye 20.000 aperturas por
     interpolación de la función cuantil (colas recortadas al 1 % y 99 %).
  2. Pide al hub /chain los strikes ±width alrededor del cierre para C y P en
     los vencimientos indicados (por defecto los dos viernes siguientes).
  3. Para cada contrato con bid/ask/IV: valor en la apertura por Black-Scholes
     con la IV del propio strike (sticky strike), T reducido 0.75 días, y
     spread absoluto igual al actual. Resultado por contrato:
        LARGO:  (bid_apertura − ask_hoy) · 100 − comisión
        CORTO:  (bid_hoy − ask_apertura) · 100 − comisión
     Se reporta esperanza, probabilidad de beneficio, percentiles 10/90 y
     retorno esperado sobre la prima.
     El valor en la apertura se obtiene como mid_hoy + [BS(S_apertura, T−0.75d) −
     BS(spot_hoy, T)], de modo que una discrepancia BS-vs-mercado de hoy no se
     cuente como beneficio.
  4. Óptimo = mayor retorno esperado sobre prima entre contratos con liquidez
     (spread ≤ --spread-max %) y 0.10 ≤ |delta| ≤ 0.90. Si ninguna esperanza es
     positiva se dice explícitamente.

Uso
  python3 src/opciones_apertura.py SPY AAPL META MSFT
  python3 src/opciones_apertura.py SPY --expiries 20260918 20260925 --comision 2.0
Salida: data/derived/apertura_mdn/opciones_<T>.json y opciones_informe.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import urllib.parse
import urllib.request

import numpy as np
from scipy.stats import norm

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
OUT_DIR = os.path.join(RAIZ, "data", "derived", "apertura_mdn")
HUB_URL = "http://127.0.0.1:8791"
R_LIBRE = 0.04


def log(m):
    print(m, file=sys.stderr, flush=True)


def viernes_siguientes(n=2):
    hoy = dt.date.today()
    d = (4 - hoy.weekday()) % 7
    d = 7 if d == 0 else d          # si hoy es viernes, el siguiente
    return [(hoy + dt.timedelta(days=d + 7 * i)).strftime("%Y%m%d") for i in range(n)]


def cadena(ticker, right, expiry, center, width, step):
    q = urllib.parse.urlencode({"ticker": ticker, "right": right, "date": expiry, "center": center,
                                "width": width, "step": step, "ttl": 60})
    with urllib.request.urlopen(f"{HUB_URL}/chain?{q}", timeout=90) as r:
        d = json.loads(r.read())
    if not d.get("ok"):
        raise RuntimeError(d.get("error"))
    return d


def bs(S, K, T, iv, right):
    """Black-Scholes vectorizado en S. T en años, iv decimal."""
    T = max(T, 1e-6)
    d1 = (np.log(S / K) + (R_LIBRE + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    if right == "C":
        return S * norm.cdf(d1) - K * math.exp(-R_LIBRE * T) * norm.cdf(d2)
    return K * math.exp(-R_LIBRE * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def muestras_apertura(r, n, rng):
    q = r["cuantiles_gap_pct"]
    ps = np.array([int(k) for k in q]) / 100.0
    gs = np.array([q[str(int(p * 100))] for p in ps])
    u = rng.uniform(0.01, 0.99, n)
    gap = np.interp(u, ps, gs)
    return r["ultimo_cierre"] * np.exp(gap / 100)


def evaluar(ticker, args, rng):
    r = json.load(open(os.path.join(OUT_DIR, f"apertura_mdn_{ticker}.json")))
    c0 = r["ultimo_cierre"]
    S = muestras_apertura(r, args.mc, rng)
    step = 1.0 if c0 < 100 else (2.5 if c0 < 400 else 5.0)
    center = round(c0 / step) * step
    hoy = dt.date.today()
    filas = []
    for expiry in args.expiries:
        dte = (dt.datetime.strptime(expiry, "%Y%m%d").date() - hoy).days
        T_open = max(dte - 0.75, 0.25) / 365.0
        for right in ("C", "P"):
            try:
                d = cadena(ticker, right, expiry, center, args.width, step)
            except Exception as e:
                log(f"  {ticker} {right} {expiry}: cadena no disponible ({e})")
                continue
            spot = d.get("spot") or c0
            for row in d["rows"]:
                bid, ask, ivp = row.get("bid"), row.get("ask"), row.get("iv_pct")
                if not (bid and ask and ivp) or ask <= 0 or ask < bid:
                    continue
                iv = ivp / 100.0
                K = row["strike"]
                spread = ask - bid
                mid_now = (bid + ask) / 2
                # cambio de valor BS entre ahora (spot, T_now) y la apertura, aplicado al mid de mercado:
                # evita que una discrepancia BS-vs-mercado de hoy se cuente como beneficio
                T_now = max(dte, 0.5) / 365.0
                bs_now = float(bs(np.array([spot]), K, T_now, iv, right)[0])
                mid_open = np.maximum(mid_now + (bs(S, K, T_open, iv, right) - bs_now), 0.0)
                bid_open = np.maximum(mid_open - spread / 2, 0.0)
                ask_open = mid_open + spread / 2
                pnl_l = (bid_open - ask) * 100 - args.comision
                pnl_c = (bid - ask_open) * 100 - args.comision
                filas.append({
                    "expiry": expiry, "dte": dte, "right": right, "strike": K, "delta": row.get("delta"),
                    "iv": round(iv, 4), "bid": bid, "ask": ask, "spread_pct": round(spread / ((bid + ask) / 2) * 100, 1),
                    "spot_chain": spot,
                    "largo": {"esperanza": round(float(pnl_l.mean()), 2), "p_beneficio": round(float((pnl_l > 0).mean()), 3),
                              "p10": round(float(np.percentile(pnl_l, 10)), 2), "p90": round(float(np.percentile(pnl_l, 90)), 2),
                              "ret_esperado_pct": round(float(pnl_l.mean() / (ask * 100)) * 100, 2)},
                    "corto": {"esperanza": round(float(pnl_c.mean()), 2), "p_beneficio": round(float((pnl_c > 0).mean()), 3),
                              "p10": round(float(np.percentile(pnl_c, 10)), 2), "p90": round(float(np.percentile(pnl_c, 90)), 2),
                              "ret_esperado_pct": round(float(pnl_c.mean() / (bid * 100)) * 100, 2)},
                })
    liq = [f for f in filas if f["spread_pct"] <= args.spread_max]

    def mejor(right, lado):
        cand = [f for f in liq if f["right"] == right and f["delta"] is not None and 0.10 <= abs(f["delta"]) <= 0.90]
        if not cand:
            return None
        return max(cand, key=lambda f: f[lado]["ret_esperado_pct"])

    optimos = {f"{lado}_{right}": mejor(right, lado) for lado in ("largo", "corto") for right in ("C", "P")}
    return {"ticker": ticker, "cierre": c0, "apertura_objetivo": r["apertura_objetivo"], "modo_modelo": r["modo"],
            "p_gap_positivo": r["prob_gap_positivo"], "gap_mediana_pct": r["gap_mediana_pct"],
            "generado": dt.datetime.now().isoformat(timespec="seconds"), "comision": args.comision,
            "optimos": optimos, "contratos": filas}


def tabla_md(res, lado):
    L = [f"| Exp | DTE | Tipo | Strike | Δ | IV | Bid | Ask | Spread % | E[P&L] $ | P(benef.) | P10 | P90 | Ret. esp. % |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for f in sorted(res["contratos"], key=lambda f: (f["expiry"], f["right"], f["strike"])):
        m = f[lado]
        L.append(f"| {f['expiry'][4:6]}-{f['expiry'][6:]} | {f['dte']} | {f['right']} | {f['strike']:.1f} | "
                 f"{(f['delta'] if f['delta'] is not None else float('nan')):+.2f} | {f['iv']:.3f} | {f['bid']:.2f} | {f['ask']:.2f} | "
                 f"{f['spread_pct']:.1f} | {m['esperanza']:+.2f} | {m['p_beneficio']:.0%} | {m['p10']:+.0f} | {m['p90']:+.0f} | {m['ret_esperado_pct']:+.1f} |")
    return L


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*", default=["SPY", "AAPL", "META", "MSFT"])
    ap.add_argument("--expiries", nargs="*", default=viernes_siguientes(2))
    ap.add_argument("--width", type=int, default=10)
    ap.add_argument("--comision", type=float, default=2.0, help="USD por contrato, ida y vuelta")
    ap.add_argument("--spread-max", type=float, default=15.0, help="spread máximo en % del mid para ser candidato")
    ap.add_argument("--mc", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)
    resultados = []
    L = ["# Opciones para la apertura: cierre → apertura", "",
         f"Generado {dt.datetime.now():%Y-%m-%d %H:%M} ET · vencimientos {', '.join(args.expiries)} · comisión {args.comision} $/contrato ida y vuelta · "
         "valoración Black-Scholes sobre las aperturas simuladas por apertura_mdn, IV sticky-strike, spread absoluto constante."]
    for t in [x.upper() for x in args.tickers]:
        log(f"[{t}] cadenas {args.expiries} …")
        res = evaluar(t, args, rng)
        resultados.append(res)
        with open(os.path.join(OUT_DIR, f"opciones_{t}.json"), "w") as fh:
            json.dump(res, fh, indent=1, ensure_ascii=False)
        print(f"\n{t}: cierre {res['cierre']}  P(gap>0)={res['p_gap_positivo']:.0%}  mediana {res['gap_mediana_pct']:+.2f}%  modelo={res['modo_modelo']}  contratos={len(res['contratos'])}")
        for k, f in res["optimos"].items():
            if f:
                m = f[k.split("_")[0]]
                print(f"  {k:8s} {f['expiry']} {f['right']} {f['strike']:.1f}  Δ{f['delta'] if f['delta'] is not None else float('nan'):+.2f}  bid/ask {f['bid']:.2f}/{f['ask']:.2f}  "
                      f"E[P&L] {m['esperanza']:+.2f}$  P(benef) {m['p_beneficio']:.0%}  p10/p90 {m['p10']:+.0f}/{m['p90']:+.0f}  ret.esp {m['ret_esperado_pct']:+.1f}%")
        L += ["", f"## {t} — cierre {res['cierre']} → apertura {res['apertura_objetivo']} (modelo {res['modo_modelo']}, P(gap>0) {res['p_gap_positivo']:.0%})", "",
              "**Óptimos** (mayor retorno esperado sobre prima, spread ≤ {:.0f} %):".format(args.spread_max), ""]
        for k, f in res["optimos"].items():
            if f:
                m = f[k.split("_")[0]]
                L.append(f"- {k}: {f['expiry']} {f['right']} {f['strike']:.1f} · E[P&L] {m['esperanza']:+.2f} $ · P(beneficio) {m['p_beneficio']:.0%} · ret. esperado {m['ret_esperado_pct']:+.1f} %")
        L += ["", "### Largo (compra ask hoy, vende bid apertura)", ""] + tabla_md(res, "largo")
        L += ["", "### Corto (vende bid hoy, recompra ask apertura)", ""] + tabla_md(res, "corto")
    ruta = os.path.join(OUT_DIR, "opciones_informe.md")
    with open(ruta, "w") as fh:
        fh.write("\n".join(L) + "\n")
    print(f"\ninforme: {ruta}")


if __name__ == "__main__":
    main()
