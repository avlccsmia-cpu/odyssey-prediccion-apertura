#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ibkr_hub.py — LA CONEXION FIJA. Un solo cliente IBKR persistente para todo
el sistema de lectura.

Motivo (5-ago-2026): cada ayudante abria y cerraba su propia conexion
(confluencia cid 201, monitor 194, defensivo 95, capturador 91, replay 92...).
Eso daba: latencia de handshake en cada ciclo, el marcador del Gateway
bailando entre connected/disconnected, clientIds retenidos por sockets a
medias (dos incidentes el 5-ago) y, sumadas todas las cadencias, 77 peticiones
historicas por 10 min contra el limite de 60 de IBKR (errores 162
"query cancelled: 1000").

El hub mantiene UNA conexion viva (cid 300), con:
  - reconexion automatica con backoff si el Gateway se reinicia
  - cola serializada: una peticion historica a la vez, espaciadas >= 2s
    (tope duro estructural: 30 por cada 10 min aunque todos pidan a la vez)
  - cache con TTL: dos consumidores pidiendo lo mismo en la misma ventana
    cuestan UNA peticion a IBKR

y expone velas por HTTP local:

  GET 127.0.0.1:8791/health
  GET 127.0.0.1:8791/bars?ticker=SPY&dur=2 D&bar=15 mins[&what=TRADES][&rth=1][&ttl=60]

SOLO LECTURA. Nunca envia ordenes: no importa nada de escritura. El camino de
escritura (ibkr_writer.py) mantiene su conexion propia y separada, a proposito:
un fallo del hub jamas puede dejar una posicion desprotegida.

Uso:
  python3 ibkr_hub.py                  # daemon
  python3 ibkr_hub.py --probar SPY     # cliente de prueba contra el daemon
"""
import json, os, re, sys, threading, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

HOST_HTTP = "127.0.0.1"
PORT_HTTP = int(os.environ.get("IBKR_HUB_PORT", "8791"))
IB_HOST   = os.environ.get("IBKR_HOST", "127.0.0.1")
IB_PORT   = int(os.environ.get("IBKR_PORT", os.environ.get("IB_PORT", "4001")))
CID_BASE  = int(os.environ.get("IBKR_HUB_CID", "300"))

ESPACIADO_SEG   = float(os.environ.get("IBKR_HUB_ESPACIADO", "2.0"))
TTL_DEFECTO_SEG = int(os.environ.get("IBKR_HUB_TTL", "60"))
TIMEOUT_REQ_SEG = 30.0

# --- 19-ago-2026: log con hora, rotacion y estado en disco -------------------
# Nace de una investigacion que no se pudo cerrar: el log tenia 89 eventos 1100
# ("Connectivity between IBKR and TWS has been lost") y solo 10 recuperaciones,
# pero NINGUNA linea llevaba hora, asi que no habia forma de correlacionar las
# caidas con nada. Ademas el fichero habia crecido a 1,7 MB de reintentos y el
# hub podia pasarse horas reintentando en silencio sin que nada lo notara.
LOGDIR      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
ESTADO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "hub_estado.json")
LOG_MAX_MB  = float(os.environ.get("IBKR_HUB_LOG_MAX_MB", "10"))
_LOG_LOCK   = threading.Lock()


def log(msg):
    """Igual que el print de antes, pero con hora ET e ISO. Sin hora no se
    diagnostica: es la leccion del 19-ago."""
    ahora = datetime.now(timezone.utc)
    print(f"[{ahora.strftime('%Y-%m-%d %H:%M:%S')}Z] [hub] {msg}", flush=True)


def rotar_log_si_toca():
    """El log crecio a 1,7 MB de 'handshake fallido'. Se rota por tamano y se
    guarda UNA copia: mas historia no aporta, y menos deja sin contexto."""
    ruta = os.path.join(LOGDIR, "ibkr_hub.log")
    try:
        if os.path.exists(ruta) and os.path.getsize(ruta) > LOG_MAX_MB * 1024 * 1024:
            os.replace(ruta, ruta + ".1")
            log(f"log rotado por tamano (> {LOG_MAX_MB} MB); anterior en ibkr_hub.log.1")
    except Exception:
        pass


def escribir_estado(conectado, cid=None, motivo=None, desde_intentos=0):
    """Deja el estado en data/hub_estado.json para que el panel pueda pintarlo.
    Antes el hub reintentaba en silencio y te enterabas por una nota al pie del
    panel 19 minutos tarde."""
    try:
        os.makedirs(os.path.dirname(ESTADO_PATH), exist_ok=True)
        with _LOG_LOCK:
            tmp = ESTADO_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "conectado": bool(conectado),
                    "cid": cid,
                    "motivo": motivo,
                    "intentos_fallidos_seguidos": desde_intentos,
                    "nota": ("conectado" if conectado else
                             "SIN CONEXION con el Gateway. Las cadenas, la cartera y la "
                             "confluencia NO son legibles. Un resultado vacio significa "
                             "'no se sabe', no 'no hay nada'."),
                }, f, ensure_ascii=False, indent=1)
            os.replace(tmp, ESTADO_PATH)
    except Exception:
        pass


BARSIZES = {"1 min", "5 mins", "15 mins", "30 mins", "1 hour", "1 day"}
RE_DUR   = re.compile(r"^\d{1,4} [SDWMY]$")
RE_TKR   = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")

# Codigos informativos del Gateway que no son errores de peticion
COD_INFO = {2104, 2106, 2107, 2158, 2119, 2100, 2150}
# Codigos que significan "conexion perdida": disparan la reconexion.
# 5-ago noche: 1100 y 2110 SALEN de esta lista — son avisos transitorios que
# IBKR suele resolver solo (1102 "restaurado" llega segundos despues), y
# tratarlos como caida provoco 52 reconexiones en una tarde, perdiendo las
# peticiones en vuelo. La caida real la detecta connectionClosed() o 502/504.
COD_CAIDA = {1300, 502, 504}
# Transitorios que solo se anotan (si la conexion murio de verdad, ibapi
# dispara connectionClosed y el vigilante actua):
COD_TRANSITORIO = {1100, 1102, 2110}


class Hub(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.listo = threading.Event()          # nextValidId recibido
        self._eventos = {}                      # reqId -> Event
        self._barras = {}                       # reqId -> list
        self._errores = {}                      # reqId -> str
        self.stats = {"conexiones": 0, "peticiones_ibkr": 0, "cache_hits": 0,
                      "errores": 0, "ultima_peticion": None, "conectado_desde": None,
                      "cadenas": 0, "cadenas_cache": 0}
        # --- estado de cadenas de opciones (port de chain_batch.py, 5-ago-2026) ---
        self._cd_evt = {}        # reqId -> Event (contract details)
        self._stk_conid = {}     # reqId -> conId
        self._opt_conids = {}    # reqId -> {strike: conId}
        self._par_evt = {}       # reqId -> Event (secdef params)
        self._expirations = {}   # reqId -> set()
        self._snap_end = set()   # 19-ago: reqIds cuyo snapshot ya cerro IBKR
        self._rows = {}          # reqId -> dict de quote
        self._spots = {}         # reqId -> {"last":..,"close":..}

    def nextValidId(self, orderId):
        self.listo.set()

    def connectionClosed(self):
        self.listo.clear()

    def historicalData(self, reqId, bar):
        self._barras.setdefault(reqId, []).append(bar)

    def historicalDataEnd(self, reqId, start, end):
        ev = self._eventos.get(reqId)
        if ev: ev.set()

    # --- callbacks de cadenas ---
    def contractDetails(self, reqId, d):
        c = d.contract
        if c.secType == "STK":
            self._stk_conid[reqId] = c.conId
        elif c.secType == "OPT":
            self._opt_conids.setdefault(reqId, {})[float(c.strike)] = c.conId

    def contractDetailsEnd(self, reqId):
        ev = self._cd_evt.get(reqId)
        if ev: ev.set()

    def securityDefinitionOptionParameter(self, reqId, exchange, underlyingConId,
                                          tradingClass, multiplier, expirations, strikes):
        if exchange == "SMART":
            self._expirations.setdefault(reqId, set()).update(expirations)

    def securityDefinitionOptionParameterEnd(self, reqId):
        ev = self._par_evt.get(reqId)
        if ev: ev.set()

    def tickPrice(self, reqId, tt, price, attrib):
        if reqId in self._spots:
            if tt in (4, 68) and price > 0:
                self._spots[reqId]["last"] = price
            elif tt in (9, 72) and price > 0:
                self._spots[reqId]["close"] = price
        if reqId in self._rows:
            d = self._rows[reqId]
            if tt in (1, 66):
                d["bid"] = price if price > 0 else None
            elif tt in (2, 67):
                d["ask"] = price if price > 0 else None
            elif tt in (4, 68) and price > 0:
                d["last"] = price
            elif tt in (9, 72) and price > 0:
                d["close"] = price

    def tickOptionComputation(self, reqId, tt, ta, iv, delta, optP, pv,
                              gamma, vega, theta, undP, *a):
        if reqId in self._rows:
            d = self._rows[reqId]
            if (tt == 13) or (d.get("greek_src") != 13):
                if iv is not None and 0 < iv < 5:
                    d["iv"] = iv
                if delta is not None and -1.5 < delta < 1.5:
                    d["delta"] = delta
                if theta is not None and abs(theta) < 100:
                    d["theta"] = theta
                d["greek_src"] = tt

    def tickSnapshotEnd(self, reqId):
        # 19-ago-2026: con snapshot=True IBKR cierra la peticion por si mismo.
        # Se apunta para no cancelar lo ya cerrado (error 300 en el Gateway).
        self._snap_end.add(reqId)

    def error(self, reqId, code, msg, advancedOrderRejectJson=""):
        if code in COD_INFO:
            return
        # errores terminales de un request de cadena: liberar su espera
        if code in (200, 321, 354, 10168):
            for tabla in (self._cd_evt, self._par_evt):
                ev = tabla.get(reqId)
                if ev:
                    ev.set()
                    return
        if code in COD_TRANSITORIO:
            log(f"transitorio ibkr {code}: {msg[:80]}")
            return
        if code in COD_CAIDA:
            log(f"conexion perdida ({code}): {msg[:100]}")
            self.listo.clear()
            return
        if reqId in self._eventos:
            self._errores[reqId] = f"{code}: {msg[:160]}"
            self._eventos[reqId].set()
        else:
            log(f"aviso ibkr {code}: {msg[:120]}")


HUB = Hub()
_LOCK_PETICION = threading.Lock()   # UNA peticion historica a la vez
_ULTIMA_PETICION = [0.0]
_CACHE = {}                          # clave -> (epoch, payload)
_CACHE_LOCK = threading.Lock()
_REQ_ID = [1000]


def vigilante_conexion():
    """Mantiene la conexion viva. Backoff 5s->60s. Rota el cid si esta retenido."""
    intento = 0
    fallos_seguidos = 0
    motivo = None
    while True:
        if HUB.listo.is_set():
            intento = 0
            time.sleep(3)
            continue
        cid = CID_BASE + (intento % 5)
        try:
            try:
                if HUB.isConnected():
                    HUB.disconnect()
                    time.sleep(1.0)
            except Exception:
                pass
            HUB.reset()
            log(f"conectando a {IB_HOST}:{IB_PORT} cid {cid}...")
            HUB.connect(IB_HOST, IB_PORT, clientId=cid)
            threading.Thread(target=HUB.run, daemon=True).start()
            if HUB.listo.wait(timeout=12):
                HUB.stats["conexiones"] += 1
                HUB.stats["conectado_desde"] = datetime.now(timezone.utc).isoformat()
                HUB.reqMarketDataType(2)   # frozen: vivo si abierto, ultima sesion si no
                log(f"CONECTADO cid {cid}")
                escribir_estado(True, cid)
                if fallos_seguidos:
                    log(f"recuperado tras {fallos_seguidos} intento(s) fallido(s)")
                fallos_seguidos = 0
                continue
            log(f"handshake fallido con cid {cid}")
            motivo = f"handshake fallido con cid {cid}"
        except Exception as e:
            log(f"error conectando: {e}")
            motivo = f"error conectando: {e}"
        intento += 1
        fallos_seguidos += 1
        escribir_estado(False, cid, motivo, fallos_seguidos)
        # 19-ago: aviso ESCALADO. Antes reintentaba en silencio para siempre
        # (7.850 fallos en un log que nadie abria). Ahora canta cada 12 fallos
        # -unos 12 min con el backoff a tope- y dice que hay que mirar.
        if fallos_seguidos in (3, 12) or (fallos_seguidos > 12 and fallos_seguidos % 30 == 0):
            log(f"*** ATENCION: {fallos_seguidos} intentos seguidos sin conectar. "
                f"Mira si el Gateway pide RE-LOGIN: no se recupera solo, espera un clic. "
                f"Mientras tanto cadenas, cartera y confluencia estan CIEGAS.")
        rotar_log_si_toca()
        espera = min(60, 5 * max(1, intento))
        time.sleep(espera)


def pedir_barras(ticker, dur, barsize, what="TRADES", rth=1, ttl=None, sec="STK"):
    """Serializada, espaciada y con cache. Devuelve (payload, error).
    sec: STK (defecto) o IND para indices como VIX (5-ago-2026)."""
    ttl = TTL_DEFECTO_SEG if ttl is None else max(0, int(ttl))
    clave = (ticker, dur, barsize, what, int(rth), sec)

    with _CACHE_LOCK:
        hit = _CACHE.get(clave)
        if hit and time.time() - hit[0] < ttl:
            HUB.stats["cache_hits"] += 1
            return hit[1], None

    if not HUB.listo.is_set():
        return None, "hub sin conexion a IBKR (reconectando)"

    with _LOCK_PETICION:
        # re-mirar la cache: otro hilo pudo traer lo mismo mientras esperabamos
        with _CACHE_LOCK:
            hit = _CACHE.get(clave)
            if hit and time.time() - hit[0] < ttl:
                HUB.stats["cache_hits"] += 1
                return hit[1], None

        desde_ultima = time.time() - _ULTIMA_PETICION[0]
        if desde_ultima < ESPACIADO_SEG:
            time.sleep(ESPACIADO_SEG - desde_ultima)

        _REQ_ID[0] += 1
        rid = _REQ_ID[0]
        ev = threading.Event()
        HUB._eventos[rid] = ev
        HUB._barras[rid] = []
        HUB._errores.pop(rid, None)

        c = Contract()
        c.symbol, c.currency = ticker, "USD"
        c.secType = sec
        c.exchange = "CBOE" if sec == "IND" else "SMART"

        _ULTIMA_PETICION[0] = time.time()
        HUB.stats["peticiones_ibkr"] += 1
        HUB.stats["ultima_peticion"] = f"{ticker} {dur} {barsize}"
        HUB.reqHistoricalData(rid, c, "", dur, barsize, what, int(rth), 2, False, [])

        ok = ev.wait(timeout=TIMEOUT_REQ_SEG)
        HUB._eventos.pop(rid, None)
        if not ok:
            try: HUB.cancelHistoricalData(rid)
            except Exception: pass
            HUB.stats["errores"] += 1
            return None, f"timeout {TIMEOUT_REQ_SEG:.0f}s sin respuesta"
        if rid in HUB._errores:
            HUB.stats["errores"] += 1
            return None, HUB._errores.pop(rid)

        barras = HUB._barras.pop(rid, [])
        payload = {"ok": True, "ticker": ticker, "dur": dur, "bar": barsize,
                   "what": what, "rth": int(rth), "n": len(barras),
                   "generado": datetime.now(timezone.utc).isoformat(),
                   "bars": [{"t": b.date, "o": b.open, "h": b.high, "l": b.low,
                             "c": b.close, "v": int(getattr(b, "volume", 0) or 0)}
                            for b in barras]}
        with _CACHE_LOCK:
            _CACHE[clave] = (time.time(), payload)
            if len(_CACHE) > 200:   # poda simple
                mas_viejo = min(_CACHE, key=lambda k: _CACHE[k][0])
                _CACHE.pop(mas_viejo, None)
        return payload, None


# ------------------------------------------------------------------
# Cadenas de opciones sobre la conexion fija (port de chain_batch.py)
# ------------------------------------------------------------------
_LOCK_CADENA = threading.Lock()      # un escaneo de cadena a la vez
_META = {}                           # ticker -> {day, stk_conid, expirations, conids}
_CACHE_CADENA = {}                   # clave -> (epoch, payload)
TTL_CADENA_SEG = float(os.environ.get("IBKR_HUB_TTL_CADENA", "12"))
ESPERA_SNAPSHOT = float(os.environ.get("IBKR_HUB_ESPERA_SNAPSHOT", "6"))


def _hoy():
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _meta_ticker(t):
    ent = _META.get(t)
    if not isinstance(ent, dict) or ent.get("day") != _hoy():
        ent = {"day": _hoy(), "stk_conid": None, "expirations": [], "conids": {}}
        _META[t] = ent
    return ent


def _nuevo_rid():
    _REQ_ID[0] += 1
    return _REQ_ID[0]


def _resolver_stock(t, timeout=6):
    rid = _nuevo_rid()
    HUB._cd_evt[rid] = threading.Event()
    c = Contract()
    c.symbol, c.secType, c.exchange, c.currency = t, "STK", "SMART", "USD"
    HUB.reqContractDetails(rid, c)
    HUB._cd_evt[rid].wait(timeout=timeout)
    HUB._cd_evt.pop(rid, None)
    return HUB._stk_conid.pop(rid, None)


def _resolver_vencimientos(t, stk_conid, timeout=8):
    rid = _nuevo_rid()
    HUB._par_evt[rid] = threading.Event()
    HUB.reqSecDefOptParams(rid, t, "", "STK", stk_conid)
    HUB._par_evt[rid].wait(timeout=timeout)
    HUB._par_evt.pop(rid, None)
    return sorted(HUB._expirations.pop(rid, set()))


def _resolver_conids(t, expiry, right, timeout=25):
    rid = _nuevo_rid()
    HUB._cd_evt[rid] = threading.Event()
    q = Contract()
    q.symbol, q.secType, q.exchange, q.currency = t, "OPT", "SMART", "USD"
    q.lastTradeDateOrContractMonth = expiry
    q.right = right
    HUB.reqContractDetails(rid, q)
    HUB._cd_evt[rid].wait(timeout=timeout)
    HUB._cd_evt.pop(rid, None)
    return HUB._opt_conids.pop(rid, {})


def _vencimiento_cercano(expirations, want):
    if not expirations:
        return None
    try:
        w = datetime.strptime(str(want), "%Y%m%d").date()
    except Exception:
        return None
    hoy = datetime.now(timezone.utc).date()
    best, best_key = None, None
    for e in expirations:
        try:
            d = datetime.strptime(str(e)[:8], "%Y%m%d").date()
        except Exception:
            continue
        if d < hoy:
            continue
        key = (abs((d - w).days), 0 if d >= w else 1)
        if best_key is None or key < best_key:
            best, best_key = str(e)[:8], key
    return best


def pedir_cadena(ticker, right, date, center, width=3, step=1.0, ttl=None):
    """Cadena de opciones por la conexion fija. (payload, error)."""
    ttl = TTL_CADENA_SEG if ttl is None else max(0.0, float(ttl))
    if not HUB.listo.is_set():
        return None, "hub sin conexion a IBKR (reconectando)"

    with _LOCK_CADENA:
        # 1. metadatos con cache diaria
        ent = _meta_ticker(ticker)
        if not ent.get("stk_conid"):
            ent["stk_conid"] = _resolver_stock(ticker)
        if not ent.get("stk_conid"):
            return None, f"{ticker} no resuelto en IBKR"
        if not ent.get("expirations"):
            ent["expirations"] = _resolver_vencimientos(ticker, ent["stk_conid"])
        expiry = _vencimiento_cercano(ent.get("expirations"), date)
        if not expiry:
            return None, "sin vencimientos disponibles"

        clave_conids = f"{expiry}:{right}"
        conids = ent["conids"].get(clave_conids)
        if not conids:
            got = _resolver_conids(ticker, expiry, right)
            conids = {f"{k:g}": v for k, v in got.items()}
            if conids:
                ent["conids"][clave_conids] = conids
        if not conids:
            return None, "sin contratos para ese vencimiento"

        # 2. strikes pedidos -> strikes reales de la parrilla
        grid = sorted(float(k) for k in conids.keys())
        wanted = [float(center) + i * float(step)
                  for i in range(-int(width), int(width) + 1)]
        picked, seen = [], set()
        for w in wanted:
            sk = min(grid, key=lambda g: abs(g - w))
            if sk not in seen:
                seen.add(sk)
                picked.append((sk, conids[f"{sk:g}"]))

        # 3. cache de cotizaciones POR STRIKES RESUELTOS: dos centros distintos
        #    que caen en la misma parrilla comparten entrada (el panel repregunta
        #    con el spot moviendose; los strikes casi nunca cambian)
        clave = (ticker, right, expiry, tuple(sk for sk, _ in picked))
        hit = _CACHE_CADENA.get(clave)
        if hit and time.time() - hit[0] < ttl:
            HUB.stats["cadenas_cache"] += 1
            return hit[1], None

        HUB.stats["cadenas"] += 1

        # 4. snapshots en paralelo: spot + todos los strikes
        rid_spot = _nuevo_rid()
        HUB._spots[rid_spot] = {}
        stk = Contract()
        stk.symbol, stk.secType, stk.exchange, stk.currency = ticker, "STK", "SMART", "USD"
        HUB.reqMktData(rid_spot, stk, "", True, False, [])

        fila_rid = []
        for sk, conid in picked:
            rid = _nuevo_rid()
            HUB._rows[rid] = {"bid": None, "ask": None, "last": None, "close": None,
                              "iv": None, "delta": None, "theta": None}
            c = Contract()
            c.conId = conid
            c.exchange = "SMART"
            # 19-ago-2026: snapshot=True. Antes streaming+cancel = churn de
            # suscripciones (subscribe/unsubscribe continuo visto en el log del
            # Gateway) que consumia lineas y precedia a los cortes 1100.
            HUB.reqMktData(rid, c, "", True, False, [])
            fila_rid.append((sk, rid))

        # 5. salida temprana: bid+ask en todos + gracia corta para griegas
        tope = time.monotonic() + max(1.0, ESPERA_SNAPSHOT)
        quotes_ok = None
        while time.monotonic() < tope:
            faltan_q = sum(1 for _, rid in fila_rid
                           if HUB._rows[rid]["bid"] is None or HUB._rows[rid]["ask"] is None)
            faltan_g = sum(1 for _, rid in fila_rid if HUB._rows[rid]["delta"] is None)
            if faltan_q == 0:
                if quotes_ok is None:
                    quotes_ok = time.monotonic()
                if faltan_g == 0 or time.monotonic() - quotes_ok > 1.5:
                    break
            if fila_rid and all(rid in HUB._snap_end for _, rid in fila_rid):
                break   # IBKR cerro todos los snapshots: no llegara nada mas
            time.sleep(0.2)

        for _, rid in fila_rid:
            if rid in HUB._snap_end:
                HUB._snap_end.discard(rid)
                continue   # ya cerrado por IBKR; cancelarlo daria error 300
            try: HUB.cancelMktData(rid)
            except Exception: pass

        sp = HUB._spots.pop(rid_spot, {})
        spot = sp.get("last") or sp.get("close")

        # 6. armar filas
        filas = []
        for sk, rid in fila_rid:
            d = HUB._rows.pop(rid, {})
            bid, ask = d.get("bid"), d.get("ask")
            mid = None
            if bid is not None and ask is not None and ask >= bid >= 0 and ask > 0:
                mid = round((bid + ask) / 2, 3)
            elif d.get("close"):
                mid = d["close"]
            spread = round(ask - bid, 3) if (bid is not None and ask is not None
                                             and ask >= bid >= 0) else None
            spread_pct = round(spread / mid * 100, 2) if (spread is not None and mid) else None
            money = None
            if spot:
                if right == "C":
                    money = "ITM" if sk < spot else ("ATM" if abs(sk - spot) < 0.5 else "OTM")
                else:
                    money = "ITM" if sk > spot else ("ATM" if abs(sk - spot) < 0.5 else "OTM")
            filas.append({"strike": sk, "bid": bid, "ask": ask, "mid": mid,
                          "spread": spread, "spread_pct": spread_pct,
                          "delta": round(d["delta"], 3) if d.get("delta") is not None else None,
                          "theta": round(d["theta"], 3) if d.get("theta") is not None else None,
                          "iv_pct": round(d["iv"] * 100, 1) if d.get("iv") is not None else None,
                          "moneyness": money})
        filas.sort(key=lambda r: r["strike"])

        try:
            dte = (datetime.strptime(expiry, "%Y%m%d").date()
                   - datetime.now(timezone.utc).date()).days
        except Exception:
            dte = None

        payload = {"ok": True, "ticker": ticker, "right": right,
                   "requested_date": str(date), "expiry": expiry, "dte": dte,
                   "spot": spot, "rows": filas,
                   "generado": datetime.now(timezone.utc).isoformat()}
        _CACHE_CADENA[clave] = (time.time(), payload)
        if len(_CACHE_CADENA) > 100:
            _CACHE_CADENA.pop(min(_CACHE_CADENA, key=lambda k: _CACHE_CADENA[k][0]), None)
        return payload, None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass    # silencio: el log util ya sale por print

    def _json(self, obj, code=200):
        cuerpo = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

        if u.path == "/health":
            self._json({"ok": True, "servicio": "ibkr_hub",
                        "conectado": HUB.listo.is_set(),
                        "cache": len(_CACHE), "stats": HUB.stats})
            return

        if u.path == "/bars":
            ticker = (q.get("ticker") or "").upper().strip()
            dur = q.get("dur", "2 D").strip()
            barsize = q.get("bar", "15 mins").strip()
            what = q.get("what", "TRADES").strip().upper()
            # los indices no tienen TRADES: para IND el dato util es MIDPOINT
            if (q.get("sec") or "").upper() == "IND" and "what" not in q:
                what = "MIDPOINT"
            rth = q.get("rth", "1")
            ttl = q.get("ttl")
            if not RE_TKR.match(ticker):
                self._json({"ok": False, "error": f"ticker invalido: {ticker!r}"}, 400); return
            if not RE_DUR.match(dur):
                self._json({"ok": False, "error": f"dur invalida: {dur!r} (ej: '2 D')"}, 400); return
            if barsize not in BARSIZES:
                self._json({"ok": False, "error": f"bar invalido: {barsize!r} {sorted(BARSIZES)}"}, 400); return
            sec = (q.get("sec") or "STK").upper()
            if sec not in ("STK", "IND"):
                self._json({"ok": False, "error": f"sec invalido: {sec!r} (STK|IND)"}, 400); return
            payload, err = pedir_barras(ticker, dur, barsize, what,
                                        1 if rth not in ("0", "false") else 0,
                                        None if ttl is None else ttl, sec)
            if err:
                self._json({"ok": False, "error": err, "ticker": ticker}, 502); return
            self._json(payload)
            return

        if u.path == "/chain":
            ticker = (q.get("ticker") or "").upper().strip()
            right = (q.get("right") or q.get("direction") or "C").upper().strip()[:1]
            date = (q.get("date") or "").strip()
            center = q.get("center")
            if not RE_TKR.match(ticker):
                self._json({"ok": False, "error": f"ticker invalido: {ticker!r}"}, 400); return
            if right not in ("C", "P"):
                self._json({"ok": False, "error": "right debe ser C o P"}, 400); return
            if not re.match(r"^\d{8}$", date):
                self._json({"ok": False, "error": f"date debe ser YYYYMMDD, no {date!r}"}, 400); return
            try:
                center = float(center)
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "center numerico requerido"}, 400); return
            try:
                width = max(0, min(10, int(q.get("width", 3))))
                step = float(q.get("step", 1) or 1)
                ttl = q.get("ttl")
            except ValueError:
                self._json({"ok": False, "error": "width/step invalidos"}, 400); return
            payload, err = pedir_cadena(ticker, right, date, center, width, step,
                                        None if ttl is None else ttl)
            if err:
                self._json({"ok": False, "error": err, "ticker": ticker}, 502); return
            self._json(payload)
            return

        self._json({"ok": False, "error": "ruta desconocida (usa /health, /bars o /chain)"}, 404)


def probar(ticker):
    base = f"http://{HOST_HTTP}:{PORT_HTTP}"
    for ruta in (f"/health", f"/bars?ticker={ticker}&dur=1 D&bar=15 mins"):
        url = base + urllib.parse.quote(ruta, safe="/?&=")
        t0 = time.time()
        try:
            with urllib.request.urlopen(url, timeout=45) as r:
                d = json.loads(r.read())
        except Exception as e:
            print(f"{ruta}\n  ERROR: {e}"); continue
        ms = (time.time() - t0) * 1000
        if "bars" in d:
            resumen = f"n={d['n']} ultima={d['bars'][-1]['t'] if d['bars'] else '—'} c={d['bars'][-1]['c'] if d['bars'] else '—'}"
        else:
            resumen = json.dumps(d, ensure_ascii=False)[:200]
        print(f"{ruta}\n  {ms:.0f} ms · {resumen}")
    # segunda llamada igual: debe salir de cache en ~0 ms
    t0 = time.time()
    url = base + urllib.parse.quote(f"/bars?ticker={ticker}&dur=1 D&bar=15 mins", safe="/?&=")
    with urllib.request.urlopen(url, timeout=45) as r:
        json.loads(r.read())
    print(f"repeticion (cache): {(time.time()-t0)*1000:.0f} ms")


def main():
    if "--probar" in sys.argv:
        i = sys.argv.index("--probar")
        probar(sys.argv[i + 1].upper() if len(sys.argv) > i + 1 else "SPY")
        return 0
    # 19-ago: el estado "arrancando" se escribe ANTES de lanzar el vigilante.
    # Al reves habia una carrera: el vigilante conectaba y escribia
    # conectado=true, y main lo pisaba con "arrancando" — hub_estado.json
    # decia False con el hub conectado (visto en el arranque de las 14:38).
    rotar_log_si_toca()
    escribir_estado(False, None, "arrancando", 0)
    threading.Thread(target=vigilante_conexion, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST_HTTP, PORT_HTTP), Handler)
    log(f"sirviendo en http://{HOST_HTTP}:{PORT_HTTP}  (IBKR {IB_HOST}:{IB_PORT}, cid base {CID_BASE})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("detenido")
    return 0


if __name__ == "__main__":
    sys.exit(main())
