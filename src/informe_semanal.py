#!/usr/bin/env python3
"""
informe_semanal.py — Resumen de la exactitud de las proyecciones de apertura y
del P&L en papel, para decidir con datos si hay que tocar el modelo.

Lee data/derived/apertura_mdn/evaluaciones.jsonl (de evaluar_aperturas.py) y
opciones_paper.jsonl. Por cada ticker, objetivo y modo cuenta solo la última
proyección generada antes de la apertura (la que estaba vigente).

Métricas, por modo y por ticker × modo, del periodo y del acumulado:
  cobertura de los intervalos 60/80/90/95 frente a lo esperado,
  PIT medio (ideal 0.50) y % de aperturas en las colas (<5 % o >95 %, ideal 10 %),
  CRPS del modelo / CRPS del baseline empírico (<1 = el modelo aporta),
  acierto de dirección y % de días en que la mediana bate a "abre igual que el cierre".

Regla de decisión: con menos de 30 observaciones por modo no se cambia nada. Un
cambio solo se adopta si mejora el backtest de 250 sesiones sin empeorar la cobertura.

Uso:  python3 src/informe_semanal.py [--dias 7] [--dir RAIZ]
Salida: data/derived/apertura_mdn/informes_semanales/informe_<fecha>.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
ESPERADO = {"en60": 0.60, "en80": 0.80, "en90": 0.90, "en95": 0.95}
MIN_N = 30


def cargar(out_dir):
    ruta = os.path.join(out_dir, "evaluaciones.jsonl")
    if not os.path.exists(ruta):
        return pd.DataFrame()
    e = pd.DataFrame([json.loads(l) for l in open(ruta) if l.strip()])
    if "modelo_prediccion" not in e:
        e["modelo_prediccion"] = "anterior"
    e["modelo_prediccion"] = e["modelo_prediccion"].fillna("anterior")
    # antes del 25-sep-2026 las proyecciones sin pre-market usaban el modelo completo: van en su propia fila
    e["modo"] = np.where(e["modelo_prediccion"] == "anterior",
                         np.where(e["modo"] == "vivo", "vivo", e["modo"] + " (modelo anterior)"), e["modo"])
    e = e.sort_values("generado").groupby(["ticker", "apertura_objetivo", "modo"], as_index=False).tail(1)
    e["fecha"] = pd.to_datetime(e["fecha_apertura_real"])
    return e


def agregado(g: pd.DataFrame) -> dict:
    d = {"n": len(g)}
    for k in ESPERADO:
        d[k] = g[k].mean() if k in g else np.nan
    d["pit"] = g["pit"].mean()
    d["colas"] = ((g["pit"] < 0.05) | (g["pit"] > 0.95)).mean()
    emp = g["crps_empirico"].dropna()
    d["crps_ratio"] = g.loc[emp.index, "crps_modelo"].mean() / emp.mean() if len(emp) else np.nan
    d["signo"] = g["signo_ok"].mean()
    d["bate_naive"] = (g["err_mediana_pct"] < g["err_naive_pct"]).mean()
    return d


def fila_md(etq, d):
    f = lambda x: "—" if pd.isna(x) else f"{x:.0%}"
    r = "—" if pd.isna(d["crps_ratio"]) else f"{d['crps_ratio']:.2f}"
    return (f"| {etq} | {d['n']} | {f(d['en60'])} | {f(d['en80'])} | {f(d['en90'])} | {f(d['en95'])} | "
            f"{d['pit']:.2f} | {f(d['colas'])} | {r} | {f(d['signo'])} | {f(d['bate_naive'])} |")


CAB = ["| | n | en 60 % | en 80 % | en 90 % | en 95 % | PIT medio | colas | CRPS mod/emp | dirección | mediana bate a 'cierre' |",
       "|---|---|---|---|---|---|---|---|---|---|---|"]


def alertas(e: pd.DataFrame) -> list[str]:
    out = []
    for modo, g in e.groupby("modo"):
        d = agregado(g)
        if d["n"] < MIN_N:
            out.append(f"- {modo}: {d['n']} observaciones; con menos de {MIN_N} no se saca ninguna conclusión.")
            continue
        if not 0.70 <= d["en80"] <= 0.90:
            out.append(f"- {modo}: cobertura del 80 % en {d['en80']:.0%}, fuera de 70-90 %: revisar la calibración.")
        if d["colas"] > 0.15:
            out.append(f"- {modo}: {d['colas']:.0%} de aperturas en las colas (ideal 10 %): colas demasiado estrechas.")
        if not pd.isna(d["crps_ratio"]) and d["crps_ratio"] >= 1:
            out.append(f"- {modo}: el CRPS del modelo no bate al baseline empírico ({d['crps_ratio']:.2f}).")
    for (t, modo), g in e.groupby(["ticker", "modo"]):
        d = agregado(g)
        if d["n"] >= MIN_N and not pd.isna(d["crps_ratio"]) and d["crps_ratio"] >= 1:
            out.append(f"- {t} en {modo}: el modelo no bate al baseline ({d['crps_ratio']:.2f}) con {d['n']} observaciones.")
    return out or ["- Nada que señalar."]


def pnl_papel(out_dir, desde):
    ruta = os.path.join(out_dir, "opciones_paper.jsonl")
    if not os.path.exists(ruta):
        return ["Sin registros de opciones en papel."]
    filas = []
    for l in open(ruta):
        if not l.strip():
            continue
        r = json.loads(l)
        if pd.Timestamp(r["fecha"]) < desde or r.get("valido") is False:
            continue
        for c in r.get("contratos", []):
            if c.get("pnl_papel") is not None:
                filas.append({"clave": c["clave"], "pnl": c["pnl_papel"], "prev": c.get("esperanza_prevista")})
    if not filas:
        return ["Sin P&L de opciones medido en el periodo."]
    p = pd.DataFrame(filas)
    L = ["| Tipo | n | Suma $ | Media $ | Media prevista $ | P(>0) |", "|---|---|---|---|---|---|"]
    for k, g in p.groupby("clave"):
        L.append(f"| {k} | {len(g)} | {g.pnl.sum():+.0f} | {g.pnl.mean():+.1f} | {g.prev.mean():+.1f} | {(g.pnl > 0).mean():.0%} |")
    return L


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dias", type=int, default=7)
    ap.add_argument("--dir", default=RAIZ)
    args = ap.parse_args()
    out_dir = os.path.join(args.dir, "data", "derived", "apertura_mdn")
    e = cargar(out_dir)
    hoy = pd.Timestamp(dt.date.today())
    desde = hoy - pd.Timedelta(days=args.dias - 1)
    L = [f"# Informe semanal de aperturas — {hoy.date()}", "",
         f"Periodo: {desde.date()} a {hoy.date()}. Las proyecciones reconstruidas se hicieron después, con datos "
         "cortados al cierre de referencia; son honestas pero no se generaron en vivo, por eso van aparte.", ""]
    if e.empty:
        L.append("Sin evaluaciones todavía.")
    else:
        per = e[e["fecha"] >= desde]
        for titulo, datos in (("Periodo", per), ("Acumulado", e)):
            L += [f"## {titulo}: por modo", ""] + CAB
            for modo, g in datos.groupby("modo"):
                L.append(fila_md(modo, agregado(g)))
            L += ["", f"Esperado si está calibrado: 60 / 80 / 90 / 95 %, PIT 0.50, colas 10 %.", ""]
        L += ["## Periodo: por ticker y modo", ""] + CAB
        for (t, modo), g in per.groupby(["ticker", "modo"]):
            L.append(fila_md(f"{t} · {modo}", agregado(g)))
        fallos = per[(per["pit"] < 0.05) | (per["pit"] > 0.95)].sort_values("fecha")
        L += ["", "## Aperturas en las colas (percentil <5 % o >95 %)", ""]
        if fallos.empty:
            L.append("Ninguna.")
        else:
            L += ["| Fecha | Ticker | Modo | Gap real | Gap previsto | Percentil |", "|---|---|---|---|---|---|"]
            for _, r in fallos.iterrows():
                L.append(f"| {r.fecha_apertura_real} | {r.ticker} | {r.modo} | {r.gap_real_pct:+.2f} % | "
                         f"{r.gap_mediana_pct:+.2f} % | {r.pit:.0%} |")
        L += ["", "## Señales para el modelo (acumulado)", ""] + alertas(e)
    L += ["", "## Opciones en papel (periodo)", ""] + pnl_papel(out_dir, desde)
    L += ["", f"Regla: con menos de {MIN_N} observaciones por modo no se cambia nada; un cambio solo se adopta si "
          "mejora el backtest de 250 sesiones sin empeorar la cobertura."]
    txt = "\n".join(L) + "\n"
    d = os.path.join(out_dir, "informes_semanales")
    os.makedirs(d, exist_ok=True)
    ruta = os.path.join(d, f"informe_{hoy.date()}.md")
    open(ruta, "w").write(txt)
    print(txt)
    print(f"informe: {ruta}")


if __name__ == "__main__":
    main()
