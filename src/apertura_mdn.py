#!/usr/bin/env python3
"""
apertura_mdn.py — Distribución probabilística del precio de apertura.

Modela el gap overnight  g = ln(Open_t / Close_{t-1})  con una Mixture Density
Network (MDN) condicionada a features de las sesiones previas, al movimiento
overnight (ES + pre-market) y a datos locales del proyecto (IV de theta_vacuum,
velas 5m de IBKR). Genera la distribución de la apertura por Monte Carlo y una
tabla de intervalos con probabilidad 60 … 100 %.

Grupos de features
  Todos los precios vienen de IBKR a través del hub de sentinel-lite
  (127.0.0.1:8791). Sin hub no hay datos: el script aborta. Las descargas se
  cachean por día en data/raw/hub_cache/ para no repetir peticiones.

  base       cierres diarios (IBKR 1 day, 15 Y): retornos, vol realizada, |gap|
             previos, rango, posición del cierre, volumen relativo, SPY, día de la semana.
  overnight  snapshot ~9:00/9:15 ET de la sesión t+1:
               mkt_on = ln(SPY_premkt_09:00 / SPY_close_prev) · 100   (IBKR 1 hour rth=0, 2 Y)
               pm     = ln(PreMkt_09:00 / Close_prev) · 100           (IBKR 1 hour rth=0, 2 Y)
               pm_idio = pm − mkt_on ;  on_disp = 1 si hay dato
             En histórico se usa la vela 1h de las 08:00 (cierra a las 09:00) para
             no filtrar nada posterior a las 9:15. Con --vivo se usa la última vela
             de 1 min rth=0 del hub y se guarda un snapshot en
             overnight_snapshots.csv que sustituye al 1h para esa fecha.
  iv         theta_vacuum/<T>/<AAAA>/<MM>.parquet (snapshot opciones 15:30 ET):
               iv_atm (media C/P del strike más cercano al spot, DTE 7-14),
               iv_dia = iv_atm/√252·100 (movimiento diario implícito en %),
               iv_rv  = iv_atm / (vol_20·√252), iv_chg = Δ iv_atm,
               skew   = IV put Δ-0.25 − IV call Δ0.25 ;  iv_disp
  m5         data/raw/bars_5m/<AAAA>/<MM>/<T>.parquet (IBKR 5m RTH), con relleno
             IBKR 5 mins 60 D vía hub para fechas recientes que falten:
               ret_ult30m = ln(C_15:55 / C_15:25)·100, ret_ult5m = ln(C_15:55/C_15:50)·100 ; m5_disp

Modelo
  MDN (MLP -> K gaussianas) con NLL, ensamble de N semillas, calibración
  conformal del sigma sobre validación, Monte Carlo -> cuantiles -> tabla.

Backtest walk-forward (modelo congelado antes del tramo), ablación:
  MDN completo · MDN sin overnight · MDN solo cierres · empírico 250 d · EWMA-bootstrap
  Métricas: CRPS, acierto de signo, MAE de la mediana, cobertura 60-99, ancho 90.

Uso (usar el python con torch, p.ej. /opt/miniconda3/bin/python3; el .venv no lo tiene)
  python3 src/apertura_mdn.py SPY AAPL META MSFT               # tras el cierre
  python3 src/apertura_mdn.py SPY AAPL META MSFT --vivo        # 9:15 ET con gateway/hub
  python3 src/apertura_mdn.py SPY AAPL META MSFT --pre-cierre --sin-ablacion   # 15:50 ET, antes de operar cierre→apertura
  python3 src/apertura_mdn.py SPY --dias-test 250 --etiqueta _bt250

Salida  data/derived/apertura_mdn/
  apertura_mdn_<TICKER>[etq].json   tabla + backtest + detalle
  apertura_mdn[etq].json            resumen
  apertura_mdn_informe[etq].md      informe legible
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import os
import sys

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
OUT_DIR = os.path.join(RAIZ, "data", "derived", "apertura_mdn")
SNAPSHOTS = os.path.join(OUT_DIR, "overnight_snapshots.csv")
THETA_DIR = os.path.join(RAIZ, "data", "raw", "theta_vacuum")   # copia local (ejecución fuera del Escritorio)
if not os.path.isdir(THETA_DIR):
    THETA_DIR = os.path.expanduser("~/Desktop/theta_vacuum")
BARS5M_DIR = os.path.join(RAIZ, "data", "raw", "bars_5m")
if not os.path.isdir(BARS5M_DIR):   # corriendo desde otra copia del repo: usar el histórico de SENTINEL-NEXT
    BARS5M_DIR = os.path.expanduser("~/Desktop/SENTINEL-NEXT/data/raw/bars_5m")
LITE_DIR = os.path.expanduser("~/Desktop/SENTINEL-lite/sentinel-lite")
HUB_URL = "http://127.0.0.1:8791"
CACHE_DIR = os.path.join(RAIZ, "data", "raw", "hub_cache")
IV_DIARIA_DIR = os.path.join(RAIZ, "data", "raw", "iv_diaria")      # captura propia (src/capturar_iv.py)
PROY_DIR = os.path.join(OUT_DIR, "proyecciones")                    # archivo de cada proyección generada
TICKERS_DEF = ["SPY", "AAPL", "META", "MSFT", "NVDA"]
HASTA = None   # --hasta: reconstruir la proyección con datos solo hasta ese cierre (pd.Timestamp)
_HUB_HIST_CAIDO = [False]   # se activa al primer timeout del histórico: el resto usa caché
NIVELES = [60, 65, 70, 75, 80, 85, 90, 95, 99, 100]
ESCALERA = [99, 95, 90, 80, 70, 60, 50, 40, 30, 20, 10, 5, 2.5, 1]   # P(apertura >= precio)
ET = "America/New_York"
COLS_ON = ["mkt_on", "pm", "pm_idio", "on_disp"]
COLS_IV = ["iv_atm", "iv_dia", "iv_rv", "iv_chg", "skew", "iv_disp"]
COLS_M5 = ["ret_ult30m", "ret_ult5m", "m5_disp"]
AUX = ("target", "close_ref", "open_next", "fecha_next")
torch.set_num_threads(max(1, os.cpu_count() // 2))


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------
# Datos (IBKR vía hub)
# ----------------------------------------------------------------------------
def hub_ok() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(HUB_URL + "/health", timeout=3) as r:
            d = json.loads(r.read())
        return bool(d.get("ok") and d.get("conectado"))
    except Exception:
        return False


def hub_bars(ticker: str, dur: str, bar: str, rth: int, cache: bool = True, ttl: int = 0) -> pd.DataFrame:
    """Velas IBKR por el hub. Índice: fecha naive (1 day) o timestamp ET (intradía).
    Cache en parquet válida durante el día ET en que se descargó (no para 1 min)."""
    import urllib.parse
    import urllib.request
    os.makedirs(CACHE_DIR, exist_ok=True)
    clave = f"{ticker}_{bar.replace(' ', '')}_{dur.replace(' ', '')}_rth{rth}.parquet"
    ruta = os.path.join(CACHE_DIR, clave)
    ahora = pd.Timestamp.now(tz=ET)
    hoy = ahora.date()
    if cache and bar != "1 min" and os.path.exists(ruta):
        mt = pd.Timestamp(os.path.getmtime(ruta), unit="s", tz="UTC").tz_convert(ET)
        tras_cierre = ahora.time() >= dt.time(16, 5)
        # fresca si se descargó hoy y, tras el cierre, después del cierre (no vale una copia de la mañana)
        fresca = mt.date() == hoy and not (tras_cierre and mt.time() < dt.time(16, 5))
        if fresca and bar == "1 day":
            ult = pd.read_parquet(ruta).index[-1].date()
            esperada = hoy if tras_cierre else (hoy - pd.offsets.BDay(1)).date()
            fresca = ult >= esperada
        if fresca:
            return pd.read_parquet(ruta)
    import time
    import urllib.error
    q = urllib.parse.urlencode({"ticker": ticker, "dur": dur, "bar": bar, "rth": rth, "ttl": ttl})
    if _HUB_HIST_CAIDO[0] and cache and os.path.exists(ruta):
        return pd.read_parquet(ruta)      # ya sabemos que el histórico no responde: caché directa
    d, ultimo_err = None, None
    for intento in range(4):
        try:
            with urllib.request.urlopen(f"{HUB_URL}/bars?{q}", timeout=300) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                ultimo_err = json.loads(e.read()).get("error") or str(e)
            except Exception:
                ultimo_err = str(e)
            d = None
        except Exception as e:
            ultimo_err, d = str(e), None
        if d is not None and d.get("ok") and d.get("bars"):
            break
        if d is not None and not d.get("ok"):
            ultimo_err = d.get("error")
        log(f"  hub /bars {ticker} {bar} {dur} rth={rth}: {ultimo_err} (intento {intento + 1}/4)")
        if "timeout" in str(ultimo_err) and intento >= 1:
            break                          # granja HMDS caída: no insistir
        time.sleep(5 * (intento + 1))
    if d is None or not d.get("ok") or not d.get("bars"):
        if cache and os.path.exists(ruta):
            fecha_cache = pd.Timestamp(os.path.getmtime(ruta), unit="s", tz="UTC").tz_convert(ET)
            log(f"  AVISO {ticker} {bar} {dur}: hub sin respuesta ({ultimo_err}); uso la caché del {fecha_cache:%Y-%m-%d %H:%M}")
            if "timeout" in str(ultimo_err):
                _HUB_HIST_CAIDO[0] = True
            return pd.read_parquet(ruta)
        raise RuntimeError(f"hub /bars {ticker} {bar} {dur}: {ultimo_err}")
    b = pd.DataFrame(d["bars"])
    if bar == "1 day":
        idx = pd.to_datetime(b["t"].astype(str), format="%Y%m%d")
    else:
        idx = pd.DatetimeIndex(pd.to_datetime(b["t"].astype(int), unit="s", utc=True)).tz_convert(ET)
    df = pd.DataFrame({"Open": b["o"].values, "High": b["h"].values, "Low": b["l"].values,
                       "Close": b["c"].values, "Volume": b["v"].values.astype(float)}, index=idx)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if cache and bar != "1 min":
        df.to_parquet(ruta)
    return df


PRE_CIERRE = False   # --pre-cierre: conservar la vela parcial de hoy (precio actual como cierre de referencia)


def descargar(ticker: str, periodo: str = "15 Y") -> pd.DataFrame:
    """Diario OHLCV de IBKR. Descarta la vela de hoy si la sesión no ha cerrado,
    salvo en modo --pre-cierre, donde la vela parcial (sin cache) es la referencia."""
    ahora = pd.Timestamp.now(tz=ET)
    en_sesion = ahora.time() < dt.time(16, 5)
    if HASTA is not None:        # reconstrucción: nada posterior al cierre de HASTA
        df = hub_bars(ticker, periodo, "1 day", rth=1).dropna()
        return df[df.index <= HASTA]
    df = hub_bars(ticker, periodo, "1 day", rth=1, cache=not (PRE_CIERRE and en_sesion)).dropna()
    if len(df) and df.index[-1].date() == ahora.date() and en_sesion and not PRE_CIERRE:
        df = df.iloc[:-1]
    return df


def _serie_1h_a_hora(df1h: pd.DataFrame, hora: int, campo: str = "Close") -> pd.Series:
    d = df1h.tz_convert(ET) if df1h.index.tz is not None else df1h.tz_localize(ET)
    sel = d[d.index.hour == hora]
    s = sel[campo].copy()
    s.index = pd.to_datetime(sel.index.date)
    return s[~s.index.duplicated(keep="last")]


def premarket_0900(ticker: str) -> pd.Series:
    """Precio a las 09:00 ET por fecha (cierre de la vela 1h de las 08:00, rth=0, 2 Y)."""
    return _serie_1h_a_hora(hub_bars(ticker, "2 Y", "1 hour", rth=0), 8)


def overnight_historico(ticker: str, diario: pd.DataFrame, spy: pd.DataFrame,
                        spy_0900: pd.Series) -> pd.DataFrame:
    """Por fecha de sesión D: mkt_on (SPY) y pm (ticker) en %, a las 09:00 ET de D."""
    fechas = diario.index
    out = pd.DataFrame(index=fechas)
    out["mkt_on"] = np.log(spy_0900.reindex(fechas).values / spy["Close"].shift(1).reindex(fechas).values) * 100
    stk_0900 = spy_0900 if ticker == "SPY" else premarket_0900(ticker)
    out["pm"] = np.log(stk_0900.reindex(fechas).values / diario["Close"].shift(1).values) * 100
    if os.path.exists(SNAPSHOTS):
        snap = pd.read_csv(SNAPSHOTS, parse_dates=["fecha"])
        snap = snap[snap["ticker"] == ticker].drop_duplicates("fecha", keep="last").set_index("fecha")
        comunes = out.index.intersection(snap.index)
        out.loc[comunes, "mkt_on"] = snap.loc[comunes, "mkt_on"].values
        out.loc[comunes, "pm"] = snap.loc[comunes, "pm"].values
    return out


def spot_snapshot(ticker: str, ref: float) -> float:
    """Último precio del ticker vía snapshot de mercado (/chain devuelve spot)."""
    import urllib.parse
    import urllib.request
    hoy = dt.date.today()
    dias = (4 - hoy.weekday()) % 7
    if dias < 7:
        dias += 7
    expiry = (hoy + dt.timedelta(days=dias)).strftime("%Y%m%d")
    step = 1.0 if ref < 100 else (2.5 if ref < 400 else 5.0)
    q = urllib.parse.urlencode({"ticker": ticker, "right": "C", "date": expiry, "center": round(ref / step) * step,
                                "width": 1, "step": step, "ttl": 0})
    with urllib.request.urlopen(f"{HUB_URL}/chain?{q}", timeout=60) as r:
        d = json.loads(r.read())
    if not d.get("ok") or not d.get("spot"):
        raise RuntimeError(f"snapshot sin spot: {d.get('error')}")
    return float(d["spot"])


def _ultimo_premarket(ticker: str, fecha_cierre: pd.Timestamp, ref: float = float("nan")) -> float:
    """Último precio posterior al cierre: velas 1 min rth=0; si el histórico no
    responde, snapshot de mercado (otra granja de IBKR)."""
    try:
        if _HUB_HIST_CAIDO[0]:
            raise RuntimeError("histórico HMDS caído")
        m1 = hub_bars(ticker, "1 D", "1 min", rth=0, cache=False, ttl=0)
        m1 = m1[m1.index > fecha_cierre.tz_localize(ET) + pd.Timedelta(hours=16)]
        if len(m1):
            return float(m1["Close"].iloc[-1])
    except Exception as e:
        log(f"  {ticker}: velas 1 min no disponibles ({e}); uso snapshot de mercado")
    return spot_snapshot(ticker, ref)


def overnight_vivo(ticker: str, cierre_ref: float, fecha_cierre: pd.Timestamp, spy_close_ref: float) -> dict:
    """Snapshot actual: pre-market del ticker y de SPY (hub 1 min rth=0) contra el último cierre."""
    ahora = pd.Timestamp.now(tz=ET)
    px_pm = _ultimo_premarket(ticker, fecha_cierre, cierre_ref)
    px_spy = px_pm if ticker == "SPY" else _ultimo_premarket("SPY", fecha_cierre, spy_close_ref)
    pm = math.log(px_pm / cierre_ref) * 100 if not np.isnan(px_pm) else float("nan")
    mkt_on = math.log(px_spy / spy_close_ref) * 100 if not np.isnan(px_spy) else float("nan")
    en_ventana = ahora.date() > fecha_cierre.date() and dt.time(4, 0) <= ahora.time() <= dt.time(9, 29)
    return {"mkt_on": mkt_on, "pm": pm, "px_pm": px_pm, "px_spy": px_spy, "spy_ref": spy_close_ref,
            "fuente_pm": "hub", "hora": ahora.isoformat(timespec="seconds"), "parcial": not en_ventana}


def guardar_snapshot(ticker: str, fecha: pd.Timestamp, s: dict):
    fila = pd.DataFrame([{"fecha": fecha.date(), "ticker": ticker, "mkt_on": round(s["mkt_on"], 4),
                          "pm": round(s["pm"], 4), "hora": s["hora"], "fuente_pm": s["fuente_pm"]}])
    fila.to_csv(SNAPSHOTS, mode="a", header=not os.path.exists(SNAPSHOTS), index=False)


def iv_vivo(ticker: str, spot: float) -> dict | None:
    """IV ATM y skew actuales desde el hub IBKR (/chain), vencimiento viernes 7-14 DTE.
    Devuelve None si el hub no responde o no hay IV."""
    import urllib.parse
    import urllib.request
    hoy = dt.date.today()
    dias = (4 - hoy.weekday()) % 7
    if dias < 7:
        dias += 7
    expiry = (hoy + dt.timedelta(days=dias)).strftime("%Y%m%d")
    step = 1.0 if spot < 100 else (2.5 if spot < 400 else 5.0)
    center = round(spot / step) * step
    filas = {}
    for right in ("C", "P"):
        q = urllib.parse.urlencode({"ticker": ticker, "right": right, "date": expiry,
                                    "center": center, "width": 10, "step": step, "ttl": 60})
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:8791/chain?{q}", timeout=40) as r:
                d = json.loads(r.read())
        except Exception as e:
            log(f"  iv vivo: hub /chain no disponible ({e})")
            return None
        if not d.get("ok"):
            log(f"  iv vivo: {d.get('error')}")
            return None
        filas[right] = [x for x in d.get("rows", []) if x.get("iv_pct")]
        spot_hub = d.get("spot") or spot
    if not filas.get("C") and not filas.get("P"):
        return None
    ivs = []
    for right in ("C", "P"):
        if filas.get(right):
            k = min(filas[right], key=lambda x: abs(x["strike"] - spot_hub))
            ivs.append(k["iv_pct"] / 100)
    p25 = min((x for x in filas.get("P", []) if x.get("delta") is not None), key=lambda x: abs(x["delta"] + 0.25), default=None)
    c25 = min((x for x in filas.get("C", []) if x.get("delta") is not None), key=lambda x: abs(x["delta"] - 0.25), default=None)
    skew = (p25["iv_pct"] - c25["iv_pct"]) / 100 if (p25 and c25) else 0.0
    return {"iv_atm": float(np.mean(ivs)), "skew": float(skew), "expiry": expiry, "spot_hub": spot_hub}


def features_iv(ticker: str, theta_dir: str, iv_dir: str = IV_DIARIA_DIR) -> pd.DataFrame | None:
    """iv_atm y skew por fecha: snapshots de theta_vacuum (15:30 ET) unidos a la
    captura propia diaria de data/raw/iv_diaria (~15:40 ET, misma definición).
    Si una fecha está en las dos, manda la captura propia."""
    theta = _iv_theta_vacuum(ticker, theta_dir)
    ruta = os.path.join(iv_dir, f"{ticker}.csv")
    propia = None
    if os.path.exists(ruta):
        c = pd.read_csv(ruta)
        if len(c):
            propia = c.assign(fecha=pd.to_datetime(c["fecha"])).set_index("fecha")[["iv_atm", "skew"]].sort_index()
    if propia is None:
        return theta
    if theta is None:
        return propia
    return propia.combine_first(theta).sort_index()


def _iv_theta_vacuum(ticker: str, theta_dir: str) -> pd.DataFrame | None:
    """iv_atm y skew por fecha desde los snapshots de opciones de theta_vacuum."""
    fs = sorted(glob.glob(os.path.join(theta_dir, ticker, "*", "*.parquet")))
    if not fs:
        return None
    d = pd.concat([pd.read_parquet(f) for f in fs])
    d["date"] = pd.to_datetime(d["date"])
    d = d[d["iv"].notna() & (d["iv"] > 0)]

    def por_dia(x):
        spot = x["spot"].iloc[0]
        k = x.iloc[(x["strike"] - spot).abs().argsort()[:1]]["strike"].iloc[0]
        c = x[(x["strike"] == k) & (x["right"] == "C")]["iv"].mean()
        p = x[(x["strike"] == k) & (x["right"] == "P")]["iv"].mean()
        pu, ca = x[x["right"] == "P"], x[x["right"] == "C"]
        p25 = pu.iloc[(pu["delta"] + 0.25).abs().argsort()[:1]]["iv"].mean() if len(pu) else np.nan
        c25 = ca.iloc[(ca["delta"] - 0.25).abs().argsort()[:1]]["iv"].mean() if len(ca) else np.nan
        return pd.Series({"iv_atm": np.nanmean([c, p]), "skew": p25 - c25})

    out = d.groupby("date")[["strike", "right", "spot", "iv", "delta"]].apply(por_dia)
    out.index = pd.to_datetime(out.index)
    return out.sort_index()


def features_5m(ticker: str, bars_dir: str, diario: pd.DataFrame) -> pd.DataFrame:
    """ret_ult30m / ret_ult5m por sesión desde bars_5m de IBKR; relleno IBKR 5 mins 60 D vía hub."""
    fs = sorted(glob.glob(os.path.join(bars_dir, "*", "*", f"{ticker}.parquet")))
    partes = []
    if fs:
        b = pd.concat([pd.read_parquet(f) for f in fs])
        b["ts"] = pd.to_datetime(b["timestamp_et"]).dt.tz_convert(ET)
        b["fecha"] = pd.to_datetime(b["session_date"])
        partes.append(b[["fecha", "ts", "close"]])
    try:
        y = hub_bars(ticker, "60 D", "5 mins", rth=1)
        partes.append(pd.DataFrame({"fecha": pd.to_datetime(y.index.date), "ts": y.index, "close": y["Close"].values}))
    except Exception as e:
        log(f"  sin relleno hub 5 mins: {e}")
    if not partes:
        return pd.DataFrame(columns=["ret_ult30m", "ret_ult5m"])
    b = pd.concat(partes)
    b["hm"] = b["ts"].dt.hour * 100 + b["ts"].dt.minute
    piv = b.pivot_table(index="fecha", columns="hm", values="close", aggfunc="first")
    out = pd.DataFrame(index=piv.index)
    for col, h0 in (("ret_ult30m", 1525), ("ret_ult5m", 1550)):
        if 1555 in piv.columns and h0 in piv.columns:
            out[col] = np.log(piv[1555] / piv[h0]) * 100
        else:
            out[col] = np.nan
    return out.dropna(how="all")


# ----------------------------------------------------------------------------
# Features
# ----------------------------------------------------------------------------
def construir_features(df, spy, on, iv, m5) -> pd.DataFrame:
    """Fila t = información al cierre de t (+ overnight de t+1). Target = gap de t+1."""
    o, h, l, c, v = df["Open"], df["High"], df["Low"], df["Close"], df["Volume"]
    r = np.log(c / c.shift(1))
    gap = np.log(o / c.shift(1))
    f = pd.DataFrame(index=df.index)
    for k in range(1, 11):
        f[f"r_{k}"] = r.shift(k - 1)
    for w in (5, 10, 20, 60):
        f[f"vol_{w}"] = r.rolling(w).std()
    for k in range(1, 6):
        f[f"absgap_{k}"] = gap.abs().shift(k - 1)
    f["absgap_m20"] = gap.abs().rolling(20).mean()
    f["gap_m5"] = gap.rolling(5).mean()
    rango = (h - l) / c
    f["rango_1"] = rango
    f["rango_m5"] = rango.rolling(5).mean()
    f["rango_m20"] = rango.rolling(20).mean()
    f["pos_cierre"] = ((c - l) / (h - l).replace(0, np.nan)).fillna(0.5)
    f["vol_rel"] = np.log(v / v.rolling(20).mean().replace(0, np.nan))
    f["ret_5"] = np.log(c / c.shift(5))
    f["ret_20"] = np.log(c / c.shift(20))
    f["dist_max20"] = np.log(c / c.rolling(20).max())
    if spy is not None:
        rs = np.log(spy["Close"] / spy["Close"].shift(1)).reindex(df.index)
        f["spy_r1"] = rs
        f["spy_vol20"] = rs.rolling(20).std()
        f["spy_absgap_m20"] = np.log(spy["Open"] / spy["Close"].shift(1)).abs().rolling(20).mean().reindex(df.index)
    prox = pd.Series(df.index, index=df.index).shift(-1)
    prox = prox.fillna(pd.Series([df.index[-1] + pd.offsets.BDay(1)], index=[df.index[-1]]))
    dow = pd.to_datetime(prox).dt.dayofweek
    for d in range(5):
        f[f"dow_{d}"] = (dow == d).astype(float)

    def enmascarar(cols_vals: dict, disp: pd.Series, nombre_disp: str):
        for k_, s_ in cols_vals.items():
            f[k_] = s_.where(disp == 1, 0.0).fillna(0.0)
        f[nombre_disp] = disp

    if on is not None:
        mkt_on = on["mkt_on"].shift(-1).reindex(df.index)
        pm = on["pm"].shift(-1).reindex(df.index)
        disp = (mkt_on.notna() & pm.notna()).astype(float)
        enmascarar({"mkt_on": mkt_on, "pm": pm, "pm_idio": pm - mkt_on}, disp, "on_disp")
    if iv is not None:
        iv_atm = iv["iv_atm"].reindex(df.index)
        disp = iv_atm.notna().astype(float)
        enmascarar({"iv_atm": iv_atm,
                    "iv_dia": iv_atm / math.sqrt(252) * 100,
                    "iv_rv": (iv_atm / (f["vol_20"] * math.sqrt(252))).clip(0, 5),
                    "iv_chg": iv["iv_atm"].diff().reindex(df.index),
                    "skew": iv["skew"].reindex(df.index)}, disp, "iv_disp")
    if m5 is not None:
        r30 = m5["ret_ult30m"].reindex(df.index)
        disp = r30.notna().astype(float)
        enmascarar({"ret_ult30m": r30, "ret_ult5m": m5["ret_ult5m"].reindex(df.index)}, disp, "m5_disp")
    f["target"] = gap.shift(-1) * 100.0
    f["close_ref"] = c
    f["open_next"] = o.shift(-1)
    f["fecha_next"] = prox.values
    return f


# ----------------------------------------------------------------------------
# MDN
# ----------------------------------------------------------------------------
class MDN(nn.Module):
    def __init__(self, n_in: int, k: int = 3, hidden: int = 64, dropout: float = 0.1):
        super().__init__()
        self.red = nn.Sequential(nn.Linear(n_in, hidden), nn.SiLU(), nn.Dropout(dropout),
                                 nn.Linear(hidden, hidden), nn.SiLU(), nn.Dropout(dropout))
        self.pi, self.mu, self.ls = nn.Linear(hidden, k), nn.Linear(hidden, k), nn.Linear(hidden, k)

    def forward(self, x):
        h = self.red(x)
        return torch.log_softmax(self.pi(h), dim=-1), self.mu(h), nn.functional.softplus(self.ls(h)) + 1e-3


def nll(logpi, mu, sigma, y):
    y = y.unsqueeze(-1)
    logn = -0.5 * ((y - mu) / sigma) ** 2 - torch.log(sigma) - 0.5 * math.log(2 * math.pi)
    return -torch.logsumexp(logpi + logn, dim=-1).mean()


def entrenar(Xtr, ytr, Xva, yva, seed, k, epochs=600, paciencia=60, lr=2e-3, wd=1e-4) -> MDN:
    torch.manual_seed(seed)
    np.random.seed(seed)
    m = MDN(Xtr.shape[1], k=k)
    opt = torch.optim.AdamW(m.parameters(), lr=lr, weight_decay=wd)
    Xtr_t, ytr_t = torch.tensor(Xtr, dtype=torch.float32), torch.tensor(ytr, dtype=torch.float32)
    Xva_t, yva_t = torch.tensor(Xva, dtype=torch.float32), torch.tensor(yva, dtype=torch.float32)
    mejor, mejor_estado, sin_mejora = float("inf"), None, 0
    n, bs = len(Xtr_t), 256
    for _ in range(epochs):
        m.train()
        perm = torch.randperm(n)
        for i in range(0, n, bs):
            idx = perm[i:i + bs]
            opt.zero_grad()
            loss = nll(*m(Xtr_t[idx]), ytr_t[idx])
            loss.backward()
            nn.utils.clip_grad_norm_(m.parameters(), 5.0)
            opt.step()
        m.eval()
        with torch.no_grad():
            lv = nll(*m(Xva_t), yva_t).item()
        if lv < mejor - 1e-4:
            mejor, sin_mejora = lv, 0
            mejor_estado = {k_: v.clone() for k_, v in m.state_dict().items()}
        else:
            sin_mejora += 1
            if sin_mejora >= paciencia:
                break
    m.load_state_dict(mejor_estado)
    m.eval()
    return m


def muestrear(modelos, X, n_por_modelo, factor_sigma, rng) -> np.ndarray:
    Xt = torch.tensor(X, dtype=torch.float32)
    salidas = []
    with torch.no_grad():
        for m in modelos:
            logpi, mu, sigma = m(Xt)
            pi, mu, sigma = logpi.exp().numpy(), mu.numpy(), sigma.numpy() * factor_sigma
            n, k = pi.shape
            comp = np.array([rng.choice(k, size=n_por_modelo, p=pi[i]) for i in range(n)])
            eps = rng.standard_normal((n, n_por_modelo))
            salidas.append(np.take_along_axis(mu, comp, 1) + np.take_along_axis(sigma, comp, 1) * eps)
    return np.concatenate(salidas, axis=1)


class Ensamble:
    def __init__(self, cols, train, val, args, rng):
        self.cols = cols
        self.mu_x = train[cols].mean()
        self.sd_x = train[cols].std().replace(0, 1.0)
        self.modelos = [entrenar(self.esc(train), train["target"].values, self.esc(val),
                                 val["target"].values, seed=s, k=args.k) for s in range(args.semillas)]
        self.n_mc = args.mc // args.semillas
        self.factor = 1.0
        if args.calibrar:
            base = muestrear(self.modelos, self.esc(val), self.n_mc, 1.0, rng)
            yv = val["target"].values
            mejor, mejor_err = 1.0, 9
            for fac in np.linspace(0.7, 1.8, 45):
                cov = np.mean([_dentro(s * fac - s.mean() * (fac - 1), y, 90) for s, y in zip(base, yv)])
                if abs(cov - 0.90) < mejor_err:
                    mejor, mejor_err = fac, abs(cov - 0.90)
            self.factor = float(mejor)

    def esc(self, d):
        return ((d[self.cols] - self.mu_x) / self.sd_x).values.astype(np.float32)

    def muestras(self, d, rng):
        return muestrear(self.modelos, self.esc(d), self.n_mc, self.factor, rng)


# ----------------------------------------------------------------------------
# Métricas y baselines
# ----------------------------------------------------------------------------
def crps_muestras(s, y):
    s = np.sort(s)
    n = len(s)
    i = np.arange(1, n + 1)
    return float(np.mean(np.abs(s - y)) - np.sum((2 * i - n - 1) * s) / (n * n))


def intervalo(s, p):
    if p >= 100:
        return float(s.min()), float(s.max())
    a = (100 - p) / 200.0
    return float(np.quantile(s, a)), float(np.quantile(s, 1 - a))


def _dentro(s, y, p):
    lo, hi = intervalo(s, p)
    return lo <= y <= hi


def resumen_modelo(S, y) -> dict:
    med = np.median(S, axis=1)
    return {
        "cobertura": {str(p): round(float(np.mean([_dentro(s, yy, p) for s, yy in zip(S, y)])), 4)
                      for p in NIVELES if p < 100},
        "crps": round(float(np.mean([crps_muestras(s, yy) for s, yy in zip(S, y)])), 4),
        "ancho_90_pct": round(float(np.mean([intervalo(s, 90)[1] - intervalo(s, 90)[0] for s in S])), 3),
        "acierto_signo": round(float(np.mean(np.sign(med) == np.sign(y))), 3),
        "mae_mediana_pct": round(float(np.mean(np.abs(med - y))), 3),
    }


def baseline_empirico(gaps_hist, n, rng):
    return rng.choice(gaps_hist[-250:], size=n, replace=True)


def baseline_ewma(gaps_hist, n, rng, lam=0.94):
    g = gaps_hist[-750:]
    var = np.var(g[:50])
    sig = np.empty(len(g))
    for i, x in enumerate(g):
        sig[i] = math.sqrt(var)
        var = lam * var + (1 - lam) * x * x
    z = (g - g.mean()) / sig
    return g.mean() + math.sqrt(var) * rng.choice(z[-500:], size=n, replace=True)


# ----------------------------------------------------------------------------
# Núcleo por ticker
# ----------------------------------------------------------------------------
def procesar(ticker, spy, spy_0900, args, rng) -> dict:
    df = descargar(ticker, args.periodo)
    on = overnight_historico(ticker, df, spy, spy_0900) if args.overnight else None
    iv = features_iv(ticker, args.theta_dir, args.iv_dir) if args.iv else None
    m5 = features_5m(ticker, args.bars5m_dir, df) if args.m5 else None
    f = construir_features(df, None if ticker == "SPY" else spy, on, iv, m5)
    cols_all = [c for c in f.columns if c not in AUX]
    cols_sin_on = [c for c in cols_all if c not in COLS_ON]
    cols_base = [c for c in cols_sin_on if c not in COLS_IV + COLS_M5]
    f = f.dropna(subset=cols_all)
    hist = f.dropna(subset=["target"])
    ultima = f.iloc[[-1]].copy()

    vivo = None
    if args.overnight and args.vivo:
        spy_ref = float(spy["Close"].reindex([ultima.index[0]]).iloc[0])
        vivo = overnight_vivo(ticker, float(ultima["close_ref"].iloc[0]), ultima.index[0], spy_ref)
        if not (np.isnan(vivo["mkt_on"]) or np.isnan(vivo["pm"])):
            ultima.loc[:, ["mkt_on", "pm", "pm_idio", "on_disp"]] = [vivo["mkt_on"], vivo["pm"], vivo["pm"] - vivo["mkt_on"], 1.0]
            if not vivo["parcial"]:
                guardar_snapshot(ticker, pd.Timestamp(ultima["fecha_next"].iloc[0]), vivo)
        log(f"  overnight vivo: SPY {vivo['mkt_on']:+.2f}% ({vivo['spy_ref']:.2f} -> {vivo['px_spy']:.2f})  "
            f"PM {vivo['pm']:+.2f}% (hub {vivo['px_pm']})  parcial={vivo['parcial']}")
    elif args.overnight:
        ultima.loc[:, COLS_ON] = 0.0
    ivv = None
    if args.vivo and args.iv and "iv_disp" in ultima.columns and ultima["iv_disp"].iloc[0] == 0.0:
        ivv = iv_vivo(ticker, float(ultima["close_ref"].iloc[0]))
        if ivv:
            iv_prev = float(iv["iv_atm"].iloc[-1]) if iv is not None and len(iv) else ivv["iv_atm"]
            vol20 = float(ultima["vol_20"].iloc[0])
            ultima.loc[:, ["iv_atm", "iv_dia", "iv_rv", "iv_chg", "skew", "iv_disp"]] = [
                ivv["iv_atm"], ivv["iv_atm"] / math.sqrt(252) * 100,
                min(5.0, ivv["iv_atm"] / (vol20 * math.sqrt(252))), ivv["iv_atm"] - iv_prev, ivv["skew"], 1.0]
            log(f"  iv vivo: atm {ivv['iv_atm']:.3f} skew {ivv['skew']:+.3f} (exp {ivv['expiry']})")

    n_test = args.dias_test
    test, trainval = hist.iloc[-n_test:], hist.iloc[:-n_test]
    # validación intercalada en bloques de 10 sesiones (1 de cada 8 bloques):
    # así train y val cubren las mismas épocas y las fuentes locales recientes
    # (overnight, IV, 5m) quedan representadas en el entrenamiento.
    mask_val = (np.arange(len(trainval)) // 10) % 8 == 7
    train, val = trainval[~mask_val], trainval[mask_val]
    # grupos sin cobertura real en train se descartan (evita pesos no aprendidos)
    for grupo, disp_col in (("overnight", "on_disp"), ("iv", "iv_disp"), ("m5", "m5_disp")):
        if disp_col in cols_all and train[disp_col].sum() < 100:
            log(f"  grupo {grupo} con solo {int(train[disp_col].sum())} filas en train (< 100): descartado")
            quitar = {"overnight": COLS_ON, "iv": COLS_IV, "m5": COLS_M5}[grupo]
            cols_all = [c for c in cols_all if c not in quitar]
            cols_sin_on = [c for c in cols_sin_on if c not in quitar]
            cols_base = [c for c in cols_base if c not in quitar]
    disp = {}
    for nombre, col in (("overnight", "on_disp"), ("iv", "iv_disp"), ("m5", "m5_disp")):
        if col in f.columns:
            disp[nombre] = {"train": round(float(train[col].mean()), 3), "test": round(float(test[col].mean()), 3),
                            "hoy": bool(ultima[col].iloc[0] == 1.0)}
    log("  disponibilidad: " + ", ".join(f"{k} train {v['train']:.0%} test {v['test']:.0%} hoy {'sí' if v['hoy'] else 'no'}" for k, v in disp.items()))

    # Sin el dato de pre-market en la fila a predecir (tras el cierre, --pre-cierre,
    # reconstrucción o --vivo fallido) se usa el modelo entrenado sin pre-market:
    # el completo, con el pre-market marcado como ausente, recibe un caso que casi no
    # vio al entrenar y salía con intervalos demasiado estrechos (evaluación del 25-sep-2026).
    hay_on = cols_sin_on != cols_all
    con_premarket = hay_on and float(ultima["on_disp"].iloc[0]) == 1.0
    usar_sin_on = hay_on and not con_premarket
    ens_completo = Ensamble(cols_all, train, val, args, rng) if (not usar_sin_on or args.ablacion) else None
    ens_sin_on = Ensamble(cols_sin_on, train, val, args, rng) if (usar_sin_on or (args.ablacion and hay_on)) else None
    ens = ens_sin_on if usar_sin_on else ens_completo
    modelo_prediccion = "con_premarket" if con_premarket else "sin_premarket"
    log(f"  modelo de la proyección: {'con' if con_premarket else 'sin'} pre-market")
    ablacion = {}
    if args.ablacion:
        if usar_sin_on:
            ablacion["mdn_con_premarket"] = ens_completo
        elif hay_on:
            ablacion["mdn_sin_overnight"] = ens_sin_on
        if cols_base != cols_sin_on:
            ablacion["mdn_solo_cierres"] = Ensamble(cols_base, train, val, args, rng)

    y_test = test["target"].values
    S_test = ens.muestras(test, rng)
    test_sin_on = test.copy()
    test_sin_on.loc[:, [c for c in COLS_ON if c in test.columns]] = 0.0   # como si no hubiera dato de pre-market
    gaps_all = hist["target"].values
    idx0 = len(hist) - n_test
    S_emp = np.array([baseline_empirico(gaps_all[:idx0 + i], args.mc, rng) for i in range(n_test)])
    S_ewm = np.array([baseline_ewma(gaps_all[:idx0 + i], args.mc, rng) for i in range(n_test)])
    bt = {
        "n_dias": int(n_test),
        "desde": str(test["fecha_next"].iloc[0].date()), "hasta": str(test["fecha_next"].iloc[-1].date()),
        "mdn": resumen_modelo(S_test, y_test),
        "modelo_prediccion": modelo_prediccion,
        **{k: resumen_modelo(e.muestras(test, rng), y_test) for k, e in ablacion.items()},
        **({"mdn_con_premarket_sin_dato": resumen_modelo(ens_completo.muestras(test_sin_on, rng), y_test)}
           if (args.ablacion and hay_on and ens_completo is not None) else {}),
        "baseline_empirico_250": resumen_modelo(S_emp, y_test),
        "baseline_ewma_bootstrap": resumen_modelo(S_ewm, y_test),
        "pit_mdn_media": round(float(np.mean([(s < yy).mean() for s, yy in zip(S_test, y_test)])), 3),
        "factor_sigma_conformal": round(ens.factor, 3),
        "muestras_train": int(len(train)), "muestras_val": int(len(val)),
        "disponibilidad": disp,
    }

    detalle = []
    for (_, row), s in zip(test.iterrows(), S_test):
        c0 = float(row["close_ref"])
        lo80, hi80 = intervalo(s, 80)
        lo95, hi95 = intervalo(s, 95)
        d = {"fecha": str(row["fecha_next"].date()), "cierre_prev": round(c0, 2),
             "open_real": round(float(row["open_next"]), 2), "gap_real_pct": round(float(row["target"]), 3),
             "mediana_pred_pct": round(float(np.median(s)), 3),
             "int80": [round(c0 * math.exp(lo80 / 100), 2), round(c0 * math.exp(hi80 / 100), 2)],
             "int95": [round(c0 * math.exp(lo95 / 100), 2), round(c0 * math.exp(hi95 / 100), 2)],
             "en80": bool(lo80 <= row["target"] <= hi80), "en95": bool(lo95 <= row["target"] <= hi95),
             "pit": round(float((s < row["target"]).mean()), 3)}
        for col in ("mkt_on", "pm", "iv_dia", "ret_ult30m"):
            if col in row.index:
                d[col] = round(float(row[col]), 3)
        detalle.append(d)

    S_next = ens.muestras(ultima, rng)[0]
    c0 = float(ultima["close_ref"].iloc[0])
    tabla = [{"prob": p, "gap_min_pct": round(lo, 3), "gap_max_pct": round(hi, 3),
              "open_min": round(c0 * math.exp(lo / 100), 2), "open_max": round(c0 * math.exp(hi / 100), 2),
              "ancho_pct": round(hi - lo, 3)} for p in NIVELES for lo, hi in [intervalo(S_next, p)]]
    unilateral = [{"prob_open_mayor_o_igual": p, "gap_pct": round(q, 3), "open": round(c0 * math.exp(q / 100), 2)}
                  for p in NIVELES if p < 100 for q in [float(np.quantile(S_next, 1 - p / 100))]]
    cuantiles = {str(q): round(float(np.quantile(S_next, q / 100)), 4) for q in range(1, 100)}   # gap % por percentil
    escalera = []
    for p in ESCALERA:
        q = float(np.quantile(S_next, 1 - p / 100))
        px = c0 * math.exp(q / 100)
        escalera.append({"prob_abre_encima": p, "precio": round(px, 2), "vs_cierre_usd": round(px - c0, 2),
                         "vs_cierre_pct": round((px / c0 - 1) * 100, 3)})
    return {
        "ticker": ticker,
        "generado": dt.datetime.now().isoformat(timespec="seconds"),
        "ultimo_cierre_fecha": str(ultima.index[0].date()), "ultimo_cierre": round(c0, 2),
        "apertura_objetivo": str(pd.Timestamp(ultima["fecha_next"].iloc[0]).date()),
        "modo": ("reconstruida" if HASTA is not None else "vivo" if vivo else "pre_cierre" if args.pre_cierre
                 else "cierre_sin_overnight" if args.overnight else "solo_cierres"),
        "reconstruida": HASTA is not None,
        "modelo_prediccion": modelo_prediccion,
        "overnight_usado": ({k: (round(v, 4) if isinstance(v, float) else v) for k, v in vivo.items()} if vivo else None),
        "iv_vivo_usado": ivv,
        "features_hoy": {c: round(float(ultima[c].iloc[0]), 4) for c in COLS_ON + COLS_IV + COLS_M5 if c in ultima.columns},
        "prob_gap_positivo": round(float((S_next > 0).mean()), 3),
        "gap_mediana_pct": round(float(np.median(S_next)), 3),
        "gap_sigma_pct": round(float(S_next.std()), 3),
        "tabla": tabla, "unilateral": unilateral, "escalera": escalera, "cuantiles_gap_pct": cuantiles,
        "backtest": bt, "detalle_test": detalle,
        "config": {"k": args.k, "semillas": args.semillas, "mc": args.mc, "features": cols_all,
                   "fuentes": {"diario": "IBKR hub 1 day", "overnight": "IBKR hub 1 hour rth=0" if on is not None else None,
                               "iv": [args.theta_dir, args.iv_dir] if iv is not None else None,
                               "m5": args.bars5m_dir if m5 is not None else None}},
    }


# ----------------------------------------------------------------------------
# Informe
# ----------------------------------------------------------------------------
NOMBRES = [("mdn", "MDN usado"), ("mdn_con_premarket", "MDN con pre-market"),
           ("mdn_sin_overnight", "MDN sin pre-market"), ("mdn_con_premarket_sin_dato", "Con pre-market, sin dato"),
           ("mdn_solo_cierres", "MDN solo cierres"),
           ("baseline_empirico_250", "Empírico 250 d"), ("baseline_ewma_bootstrap", "EWMA + bootstrap")]


def informe_md(resultados) -> str:
    L = ["# Apertura MDN + Monte Carlo", "",
         f"Generado: {dt.datetime.now():%Y-%m-%d %H:%M} ET. Fuentes: IBKR vía hub (diario 15 Y, 1 hour rth=0 2 Y, 5 mins 60 D, "
         "1 min rth=0 en vivo, /chain), theta_vacuum (IV 15:30), bars_5m IBKR."]
    for r in resultados:
        bt = r["backtest"]
        L += ["", f"## {r['ticker']}  —  cierre {r['ultimo_cierre_fecha']} = {r['ultimo_cierre']}  →  apertura {r['apertura_objetivo']}  (modo: {r['modo']})", ""]
        if r["overnight_usado"]:
            ov = r["overnight_usado"]
            L += [f"Overnight usado: SPY pre-market {ov['mkt_on']:+.2f} % · pre-market {ov['pm']:+.2f} % (hub, {ov['hora']}){' · PARCIAL' if ov['parcial'] else ''}", ""]
        elif r["modo"] == "cierre_sin_overnight":
            L += ["Sin overnight vivo: tabla solo con cierres, IV y 5m. Correr con --vivo a las 9:15 ET para incluir el pre-market.", ""]
        fh = r["features_hoy"]
        L += ["Features locales hoy: " + ", ".join(f"{k}={v:+.3f}" for k, v in fh.items() if not k.endswith("_disp")) +
              " · disponibles: " + ", ".join(f"{k}={'sí' if v else 'no'}" for k, v in fh.items() if k.endswith("_disp")), "",
              f"P(gap > 0) = {r['prob_gap_positivo']:.0%} · mediana gap {r['gap_mediana_pct']:+.2f} % · σ gap {r['gap_sigma_pct']:.2f} %", "",
              "| Prob | Open mín | Open máx | Gap mín % | Gap máx % | Ancho % |", "|---|---|---|---|---|---|"]
        for t in r["tabla"]:
            et = f"{t['prob']}%" if t["prob"] < 100 else "100% (mín/máx sim.)"
            L.append(f"| {et} | {t['open_min']} | {t['open_max']} | {t['gap_min_pct']:+.2f} | {t['gap_max_pct']:+.2f} | {t['ancho_pct']:.2f} |")
        L += ["", "Escalera de precios: probabilidad de que la apertura quede POR ENCIMA de cada precio "
              "(el 50 % es la mediana; cada fila se lee sola, sin rangos).", "",
              "| P(abre por encima) | Precio | vs cierre $ | vs cierre % |", "|---|---|---|---|"]
        for e in r["escalera"]:
            L.append(f"| {e['prob_abre_encima']} % | {e['precio']:.2f} | {e['vs_cierre_usd']:+.2f} | {e['vs_cierre_pct']:+.2f} % |")
        L += ["", f"### Backtest {bt['desde']} → {bt['hasta']} ({bt['n_dias']} sesiones, modelo congelado antes del tramo)", "",
              "Disponibilidad de fuentes: " + " · ".join(f"{k}: train {v['train']:.0%}, test {v['test']:.0%}" for k, v in bt["disponibilidad"].items()), "",
              "| Modelo | CRPS (pp) | Signo | MAE med. | Cob. 60 | Cob. 80 | Cob. 90 | Cob. 95 | Ancho 90 % |", "|---|---|---|---|---|---|---|---|---|"]
        for key, nombre in NOMBRES:
            if key not in bt:
                continue
            m = bt[key]; c = m["cobertura"]
            L.append(f"| {nombre} | {m['crps']:.3f} | {m['acierto_signo']:.0%} | {m['mae_mediana_pct']:.3f} | "
                     f"{c['60']:.0%} | {c['80']:.0%} | {c['90']:.0%} | {c['95']:.0%} | {m['ancho_90_pct']:.2f} |")
        L += ["", f"Factor conformal σ = {bt['factor_sigma_conformal']} · PIT medio = {bt['pit_mdn_media']} (ideal 0.50) · "
              f"train {bt['muestras_train']} / val {bt['muestras_val']} sesiones.", ""]
        extras = [c for c in ("mkt_on", "pm", "iv_dia", "ret_ult30m") if c in r["detalle_test"][0]]
        L += ["| Fecha | Cierre prev | " + "".join(f"{c} | " for c in extras) + "Open real | Gap real % | Med. pred % | Int. 80 % | Int. 95 % | En 80 | En 95 |",
              "|---|---|" + "---|" * len(extras) + "---|---|---|---|---|---|---|"]
        for d in r["detalle_test"]:
            ex = "".join(f"{d[c]:+.2f} | " for c in extras)
            L.append(f"| {d['fecha']} | {d['cierre_prev']} | {ex}{d['open_real']} | {d['gap_real_pct']:+.2f} | {d['mediana_pred_pct']:+.2f} | "
                     f"{d['int80'][0]}–{d['int80'][1]} | {d['int95'][0]}–{d['int95'][1]} | {'✓' if d['en80'] else '✗'} | {'✓' if d['en95'] else '✗'} |")
    L += ["", "Notas: el 100 % es el mínimo y máximo de la simulación, no una garantía. CRPS en puntos porcentuales de gap; menor es mejor. "
          "Signo = acierto de dirección de la mediana. Overnight histórico medido a las 09:00 ET (vela 1h de las 08:00 de IBKR rth=0); en --vivo, al momento de la ejecución. "
          "Las features enmascaradas valen 0 con su *_disp = 0 cuando la fuente no cubre esa fecha. La cobertura de ~21 días tiene granularidad de ~5 pp."]
    return "\n".join(L) + "\n"


def archivar_proyeccion(r: dict) -> str:
    """Copia compacta (sin el detalle día a día del backtest) de cada proyección,
    en proyecciones/<apertura_objetivo>/<TICKER>_<modo>_<generado>.json. Es lo que
    evalúa src/evaluar_aperturas.py; nada se sobrescribe."""
    d = os.path.join(PROY_DIR, r["apertura_objetivo"])
    os.makedirs(d, exist_ok=True)
    marca = r["generado"].replace(":", "").replace("-", "")
    ruta = os.path.join(d, f"{r['ticker']}_{r['modo']}_{marca}.json")
    with open(ruta, "w") as fh:
        json.dump({k: v for k, v in r.items() if k != "detalle_test"}, fh, indent=1, ensure_ascii=False)
    return ruta


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*", default=TICKERS_DEF)
    ap.add_argument("--dias-test", type=int, default=21, help="sesiones del backtest (defecto 21 ≈ 1 mes)")
    ap.add_argument("--periodo", default="15 Y", help="duración IBKR del diario (ej. '15 Y')")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--semillas", type=int, default=5)
    ap.add_argument("--mc", type=int, default=20000)
    ap.add_argument("--vivo", action="store_true", help="usar el pre-market actual (correr 9:00-9:29 ET)")
    ap.add_argument("--pre-cierre", action="store_true",
                    help="correr en sesión (p.ej. 15:50 ET) usando el precio actual como cierre de referencia; "
                         "para decidir la operación cierre→apertura antes de que cierre el mercado")
    ap.add_argument("--sin-overnight", dest="overnight", action="store_false")
    ap.add_argument("--sin-iv", dest="iv", action="store_false")
    ap.add_argument("--sin-m5", dest="m5", action="store_false")
    ap.add_argument("--sin-ablacion", dest="ablacion", action="store_false", help="no entrenar los modelos de comparación")
    ap.add_argument("--sin-calibrar", dest="calibrar", action="store_false")
    ap.add_argument("--sin-informe", action="store_true")
    ap.add_argument("--theta-dir", default=THETA_DIR)
    ap.add_argument("--iv-dir", default=IV_DIARIA_DIR, help="captura propia diaria de IV (capturar_iv.py)")
    ap.add_argument("--hasta", default=None, metavar="YYYY-MM-DD",
                    help="reconstruir la proyección que se habría hecho tras el cierre de esa fecha, "
                         "sin usar ningún dato posterior. Solo archiva: no toca los ficheros vigentes")
    ap.add_argument("--bars5m-dir", default=BARS5M_DIR)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--etiqueta", default="")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    global PRE_CIERRE, HASTA
    PRE_CIERRE = bool(args.pre_cierre)
    if args.hasta:
        if args.vivo or args.pre_cierre:
            ap.error("--hasta no se combina con --vivo ni --pre-cierre")
        HASTA = pd.Timestamp(args.hasta)
    rng = np.random.default_rng(args.seed)
    if not hub_ok():
        log(f"ERROR: el hub IBKR no responde o no está conectado en {HUB_URL}. "
            "Levanta el gateway y ejecuta 'python3 ibkr_hub.py' en sentinel-lite.")
        sys.exit(2)
    spy = descargar("SPY", args.periodo)
    spy_0900 = premarket_0900("SPY") if args.overnight else None
    resultados = []
    for t in [x.upper() for x in args.tickers]:
        log(f"[{t}] MDN k={args.k} ensamble={args.semillas} overnight={args.overnight} iv={args.iv} m5={args.m5} ablacion={args.ablacion}")
        try:
            r = procesar(t, spy, spy_0900, args, rng)
        except Exception as e:
            log(f"[{t}] ERROR: {e!r}. Sigo con el resto.")
            continue
        resultados.append(r)
        if not args.etiqueta:
            archivar_proyeccion(r)
        if HASTA is None:
            with open(os.path.join(OUT_DIR, f"apertura_mdn_{t}{args.etiqueta}.json"), "w") as fh:
                json.dump(r, fh, indent=1, ensure_ascii=False)
        print(f"\n{t}: cierre {r['ultimo_cierre_fecha']} = {r['ultimo_cierre']}  ->  apertura {r['apertura_objetivo']}   "
              f"P(gap>0)={r['prob_gap_positivo']:.0%}  modo={r['modo']}")
        print(f"  {'Prob':>6} {'Open mín':>10} {'Open máx':>10} {'Gap mín%':>9} {'Gap máx%':>9}")
        for row in r["tabla"]:
            print(f"  {row['prob']:>5}% {row['open_min']:>10.2f} {row['open_max']:>10.2f} {row['gap_min_pct']:>+9.2f} {row['gap_max_pct']:>+9.2f}")
        print(f"  {'P(abre encima)':>15} {'Precio':>9} {'vs $':>8} {'vs %':>8}")
        for e in r["escalera"]:
            print(f"  {e['prob_abre_encima']:>14}% {e['precio']:>9.2f} {e['vs_cierre_usd']:>+8.2f} {e['vs_cierre_pct']:>+7.2f}%")
        bt = r["backtest"]
        print(f"  backtest {bt['desde']}→{bt['hasta']} ({bt['n_dias']} d):")
        for key, nombre in NOMBRES:
            if key in bt:
                m = bt[key]
                print(f"    {nombre:18s} CRPS {m['crps']:.3f}  signo {m['acierto_signo']:.0%}  MAE {m['mae_mediana_pct']:.3f}  "
                      f"cob80 {m['cobertura']['80']:.0%}  cob95 {m['cobertura']['95']:.0%}  ancho90 {m['ancho_90_pct']:.2f}")

    if HASTA is not None:
        return
    resumen = [{k: r[k] for k in ("ticker", "ultimo_cierre_fecha", "ultimo_cierre", "apertura_objetivo", "modo", "overnight_usado",
                                  "features_hoy", "prob_gap_positivo", "gap_mediana_pct", "gap_sigma_pct", "tabla", "escalera")}
               | {"crps": {k: r["backtest"][k]["crps"] for k, _ in NOMBRES if k in r["backtest"]}} for r in resultados]
    with open(os.path.join(OUT_DIR, f"apertura_mdn{args.etiqueta}.json"), "w") as fh:
        json.dump({"generado": dt.datetime.now().isoformat(timespec="seconds"), "tickers": resumen}, fh, indent=1, ensure_ascii=False)
    if not args.sin_informe:
        ruta = os.path.join(OUT_DIR, f"apertura_mdn_informe{args.etiqueta}.md")
        with open(ruta, "w") as fh:
            fh.write(informe_md(resultados))
        print(f"\ninforme: {ruta}")


if __name__ == "__main__":
    main()
