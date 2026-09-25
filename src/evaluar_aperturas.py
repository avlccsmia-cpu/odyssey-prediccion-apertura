#!/usr/bin/env python3
"""
evaluar_aperturas.py — Evalúa cada proyección de apertura contra la apertura
real, poniéndose al día solo con lo que esté pendiente. SOLO LECTURA.

Fuentes de proyecciones (se deduplican por ticker, objetivo, modo y generado):
  data/derived/apertura_mdn/proyecciones/<objetivo>/<T>_<modo>_<marca>.json   (apertura_mdn.py)
  data/derived/apertura_mdn/historico/apertura_mdn_<T>_<fecha>.json          (archivo antiguo)
  data/derived/apertura_mdn/apertura_mdn_<T>.json                             (vigente)

Apertura real: la de la barra diaria oficial de IBKR de la primera sesión
posterior al cierre de referencia (así los festivos no rompen nada). Si esa
sesión es hoy, solo se evalúa a partir de las 09:31 ET.

Métricas por proyección, en el espacio del gap respecto al cierre de referencia:
  pit            percentil de la apertura real en la distribución prevista
  en60..en95     si cayó dentro de los intervalos centrales
  signo_ok       dirección de la mediana = dirección del gap real
  err_mediana    |gap real − gap mediano previsto|        (en % del cierre)
  err_naive      |gap real|   (baseline "abre igual que el cierre")
  crps_modelo    CRPS de la distribución prevista (99 cuantiles, score de cuantiles)
  crps_empirico  CRPS del baseline empírico: los 250 gaps previos al cierre
Salida: data/derived/apertura_mdn/evaluaciones.jsonl (una línea por proyección).

Uso:  python3 src/evaluar_aperturas.py [--dir RAIZ_DE_DATOS]
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
HUB_URL = "http://127.0.0.1:8791"
ET = "America/New_York"
NIVELES = (60, 80, 90, 95)


def log(m):
    print(m, file=sys.stderr, flush=True)


def barras_diarias(ticker: str, cache_dir: str) -> pd.DataFrame:
    """Barras diarias: hub (2 M, sin caché) unidas a la caché larga de 15 años."""
    partes = []
    ruta = os.path.join(cache_dir, f"{ticker}_1day_15Y_rth1.parquet")
    if os.path.exists(ruta):
        partes.append(pd.read_parquet(ruta)[["Open", "Close"]])
    try:
        q = urllib.parse.urlencode({"ticker": ticker, "dur": "2 M", "bar": "1 day", "rth": 1, "ttl": 0})
        with urllib.request.urlopen(f"{HUB_URL}/bars?{q}", timeout=120) as r:
            d = json.loads(r.read())
        if d.get("ok") and d.get("bars"):
            b = pd.DataFrame(d["bars"])
            partes.append(pd.DataFrame({"Open": b["o"].values, "Close": b["c"].values},
                                       index=pd.to_datetime(b["t"].astype(str), format="%Y%m%d")))
        else:
            log(f"  [{ticker}] hub sin barras diarias: {d.get('error')}")
    except Exception as e:
        log(f"  [{ticker}] hub no disponible para barras diarias: {e}")
    if not partes:
        return pd.DataFrame(columns=["Open", "Close"])
    df = pd.concat(partes)
    return df[~df.index.duplicated(keep="last")].sort_index()


def crps_cuantiles(qv: np.ndarray, taus: np.ndarray, y: float) -> float:
    """CRPS ≈ 2 · media del score de cuantiles sobre los niveles dados."""
    return float(2 * np.mean(((y < qv).astype(float) - taus) * (qv - y)))


def crps_muestra(x: np.ndarray, y: float) -> float:
    x = np.sort(x)
    n = len(x)
    i = np.arange(1, n + 1)
    return float(np.mean(np.abs(x - y)) - np.sum((2 * i - n - 1) * x) / (n * n))


def proyecciones(out_dir: str) -> list[dict]:
    rutas = glob.glob(os.path.join(out_dir, "proyecciones", "*", "*.json"))
    rutas += [p for p in glob.glob(os.path.join(out_dir, "historico", "apertura_mdn_*_*.json"))
              if re.search(r"apertura_mdn_[A-Z.]+_\d{4}-\d\d-\d\d\.json$", p)]
    rutas += [p for p in glob.glob(os.path.join(out_dir, "apertura_mdn_*.json"))
              if re.search(r"apertura_mdn_[A-Z.]+\.json$", p)]
    vistas, out = set(), []
    for p in sorted(rutas):
        try:
            r = json.load(open(p))
        except Exception:
            continue
        if not all(k in r for k in ("ticker", "apertura_objetivo", "modo", "generado", "ultimo_cierre",
                                     "ultimo_cierre_fecha", "tabla", "cuantiles_gap_pct")):
            continue
        clave = (r["ticker"], r["apertura_objetivo"], r["modo"], r["generado"])
        if clave in vistas:
            continue
        vistas.add(clave)
        r["_ruta"] = p
        out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=RAIZ, help="raíz del proyecto (contiene data/)")
    args = ap.parse_args()
    out_dir = os.path.join(args.dir, "data", "derived", "apertura_mdn")
    cache_dir = os.path.join(args.dir, "data", "raw", "hub_cache")
    ruta_eval = os.path.join(out_dir, "evaluaciones.jsonl")
    hechas = set()
    if os.path.exists(ruta_eval):
        for l in open(ruta_eval):
            if l.strip():
                e = json.loads(l)
                hechas.add((e["ticker"], e["apertura_objetivo"], e["modo"], e["generado"]))

    ahora = pd.Timestamp.now(tz=ET)
    pend = [r for r in proyecciones(out_dir) if (r["ticker"], r["apertura_objetivo"], r["modo"], r["generado"]) not in hechas]
    if not pend:
        print("evaluar_aperturas: nada pendiente")
        return
    barras = {t: barras_diarias(t, cache_dir) for t in sorted({r["ticker"] for r in pend})}
    ks = np.arange(1, 100)
    taus = ks / 100.0
    nuevas, esperando = [], 0
    for r in sorted(pend, key=lambda x: (x["apertura_objetivo"], x["ticker"], x["generado"])):
        t = r["ticker"]
        b = barras[t]
        cierre_f = pd.Timestamp(r["ultimo_cierre_fecha"])
        post = b[b.index > cierre_f]
        if post.empty:
            esperando += 1
            continue
        f_open = post.index[0]
        if f_open.date() == ahora.date() and ahora.time() < dt.time(9, 31):
            esperando += 1
            continue
        o = float(post["Open"].iloc[0])
        c0 = float(r["ultimo_cierre"])
        gap = math.log(o / c0) * 100
        qv = np.array([r["cuantiles_gap_pct"][str(k)] for k in ks], dtype=float)
        pit = float(np.clip(np.interp(gap, qv, taus, left=0.0, right=1.0), 0.0, 1.0))
        tabla = {int(x["prob"]): x for x in r["tabla"]}
        prev = b[b.index <= cierre_f]
        g_hist = np.log(prev["Open"] / prev["Close"].shift(1)).dropna().values[-250:] * 100
        e = {
            "ticker": t, "apertura_objetivo": r["apertura_objetivo"], "fecha_apertura_real": str(f_open.date()),
            "modo": r["modo"], "reconstruida": bool(r.get("reconstruida", False)), "generado": r["generado"],
            "modelo_prediccion": r.get("modelo_prediccion", "anterior"),
            "cierre_ref_fecha": r["ultimo_cierre_fecha"], "cierre_ref": c0, "open_real": round(o, 4),
            "gap_real_pct": round(gap, 4), "gap_mediana_pct": r["gap_mediana_pct"],
            "p_gap_pos": r.get("prob_gap_positivo"), "pit": round(pit, 4),
            **{f"en{n}": bool(tabla[n]["gap_min_pct"] <= gap <= tabla[n]["gap_max_pct"]) for n in NIVELES if n in tabla},
            "signo_ok": bool(np.sign(r["gap_mediana_pct"]) == np.sign(gap)),
            "err_mediana_pct": round(abs(gap - r["gap_mediana_pct"]), 4),
            "err_naive_pct": round(abs(gap), 4),
            "crps_modelo": round(crps_cuantiles(qv, taus, gap), 5),
            "crps_empirico": round(crps_muestra(g_hist, gap), 5) if len(g_hist) >= 100 else None,
            "evaluado": ahora.isoformat(timespec="seconds"),
        }
        nuevas.append(e)
    if nuevas:
        with open(ruta_eval, "a") as fh:
            for e in nuevas:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"evaluar_aperturas: {len(nuevas)} nuevas, {esperando} esperando su apertura")
    for e in nuevas:
        print(f"  {e['apertura_objetivo']} {e['ticker']:5} {e['modo']:20} gap {e['gap_real_pct']:+.2f}% "
              f"(prev {e['gap_mediana_pct']:+.2f}%)  pit {e['pit']:.2f}  en80 {'sí' if e.get('en80') else 'no'}  "
              f"crps {e['crps_modelo']:.3f} vs emp {e['crps_empirico'] if e['crps_empirico'] is not None else float('nan'):.3f}")


if __name__ == "__main__":
    main()
