#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hub_client.py — cliente minimo del ibkr_hub para los consumidores de velas.

Devuelve objetos Bar con los MISMOS atributos que las barras de ibapi
(date, open, high, low, close, volume), de modo que migrar un consumidor
es cambiar su funcion de peticion, no su logica.

    from hub_client import barras
    bars, err = barras("SPY", dur="2 D", barsize="15 mins")
    if err: ...        # el hub no responde o IBKR devolvio error
    bars[-1].close     # igual que antes
"""
import json, urllib.parse, urllib.request

HUB_URL = "http://127.0.0.1:8791"


class Bar:
    __slots__ = ("date", "open", "high", "low", "close", "volume")

    def __init__(self, d):
        self.date = d["t"]
        self.open = d["o"]
        self.high = d["h"]
        self.low = d["l"]
        self.close = d["c"]
        self.volume = d["v"]


def barras(ticker, dur="2 D", barsize="15 mins", what="TRADES",
           rth=1, ttl=None, timeout=40.0):
    """(lista_de_Bar, None) o (None, mensaje_de_error). Nunca lanza."""
    q = {"ticker": ticker, "dur": dur, "bar": barsize, "what": what, "rth": rth}
    if ttl is not None:
        q["ttl"] = ttl
    url = HUB_URL + "/bars?" + urllib.parse.urlencode(q)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            d = json.loads(r.read())
    except Exception as e:
        return None, f"hub inalcanzable en {HUB_URL}: {e}"
    if not d.get("ok"):
        return None, d.get("error", "error desconocido del hub")
    return [Bar(b) for b in d.get("bars", [])], None


def hub_vivo(timeout=3.0):
    """True si el hub responde y tiene conexion IBKR."""
    try:
        with urllib.request.urlopen(HUB_URL + "/health", timeout=timeout) as r:
            d = json.loads(r.read())
        return bool(d.get("ok") and d.get("conectado"))
    except Exception:
        return False
