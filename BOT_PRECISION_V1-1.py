import os
import sys
import time
import json
import tempfile
import numpy as np
import talib
import requests
from decimal import Decimal as D, InvalidOperation
from binance.client import Client

# =========================
# PARÁMETROS PRINCIPALES
# =========================
TRADE_SIZE         = D('350')
MAX_POSITIONS      = 5
MIN_VOL_MULT       = D('1.20')
MIN_VOL_BYPASS     = D('0.90')
TP_PCT             = D('0.022')
SL_PCT             = D('0.028')

# Trailing normal
TRAILING_STAGE1    = D('0.011')
TRAILING_STAGE2    = D('0.017')
TRAILING_STAGE3    = D('0.023')
TRAILING_DIST_1    = D('0.007')
TRAILING_DIST_2    = D('0.006')
TRAILING_DIST_3    = D('0.005')

# FIX #4 — Trailing fuerte (setup_fuerte): variables que faltaban completamente
TRAILING_STRONG_STAGE1 = D('0.010')
TRAILING_STRONG_STAGE2 = D('0.016')
TRAILING_STRONG_STAGE3 = D('0.022')
TRAILING_STRONG_DIST_1 = D('0.008')
TRAILING_STRONG_DIST_2 = D('0.007')
TRAILING_STRONG_DIST_3 = D('0.006')

BREAKEVEN_LOCK     = D('0.003')
PROFIT_TIMEOUT_MIN = 120
PROFIT_TIMEOUT_PCT = D('0.006')
TIMEOUT_RSI_MAX    = 52
TIMEOUT_5M_BARS    = 30
TIMEOUT_BTC_SOLO_MIN  = 75
# FIX #7 — Defensa RISK_OFF: 45min → 60min, -0.1% → -0.15% (evita stops por ruido)
TIMEOUT_RISK_OFF_MIN  = 60
DEFENSE_LOSS_BTC_SOLO = D('-0.003')
DEFENSE_LOSS_RISK_OFF = D('-0.0015')

DESACOPLE_PCT      = D('0.008')
TP_BUFFER          = D('0.999')
CHECK_INTERVAL     = 8
COOLDOWN_SEC       = 180
REBOTE_RSI_MAX     = 35
REBOTE_RSI_MIN     = 28
REBOTE_VELAS       = 2
CONT_RSI1H_MIN     = 52
CONT_RSI4H_MIN     = 50
CONT_MIN_VOL       = D('0.85')
CONT_EMA_TOL       = 0.004

# =========================
# MODO SNIPER
# =========================
SNIPER_MODE            = True
SNIPER_ALT_RET_MIN     = D('0.012')
SNIPER_BTC_RET_MAX     = D('0.001')
SNIPER_VOL_MIN         = D('1.80')
SNIPER_RSI15_MIN       = 57
SNIPER_RSI1H_MIN       = 55
SNIPER_MAX_EXT         = 0.012
SNIPER_MAX_DRIFT       = 0.006

POSITIONS_FILE     = "/root/bot_precision_positions.json"
LOG_PREFIX         = "BOT PRECISIÓN V1.7"

WATCHLIST = [
    'SOLUSDT', 'SUIUSDT', 'TAOUSDT', 'AVAXUSDT',
    'CHZUSDT', 'TONUSDT', 'FETUSDT', 'NEARUSDT', 'LINKUSDT',
    'XRPUSDT', 'ETHUSDT', 'ADAUSDT', 'BNBUSDT', 'DOTUSDT',
    'AVAUSDT', 'AAVEUSDT'
]

API_KEY    = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")
TOKEN      = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID")

if not all([API_KEY, API_SECRET, TOKEN, CHAT_ID]):
    print("ERROR: Faltan variables de entorno")
    sys.exit(1)

client = Client(API_KEY, API_SECRET)
_SYMBOL_CACHE = {}

# =========================
# UTILIDADES
# =========================
def tg(msg: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=8
        )
    except Exception:
        pass

def d(x, default: str = '0') -> D:
    try:
        return D(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return D(default)

def is_nan(x) -> bool:
    try:
        return bool(np.isnan(float(x)))
    except Exception:
        return True

def atomic_save_json(path: str, data: dict) -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix="botpos_", suffix=".json", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

def save_positions(posiciones_bot: dict) -> None:
    serializable = {}
    for sym, pos in posiciones_bot.items():
        serializable[sym] = {
            "qty":             str(pos["qty"]),
            "precio_entrada":  str(pos["precio_entrada"]),
            "tp":              str(pos["tp"]),
            "sl":              str(pos["sl"]),
            "max_precio":      str(pos.get("max_precio", pos["precio_entrada"])),
            "trailing_activo": pos.get("trailing_activo", False),
            "trailing_stage":  pos.get("trailing_stage", 0),
            # FIX #6 — setup_fuerte no se guardaba: se pierde al reiniciar el bot
            "setup_fuerte":    pos.get("setup_fuerte", False),
            "timestamp":       pos.get("timestamp", int(time.time()))
        }
    atomic_save_json(POSITIONS_FILE, serializable)

def load_positions() -> dict:
    if not os.path.exists(POSITIONS_FILE):
        return {}
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        posiciones = {}
        for sym, pos in raw.items():
            posiciones[sym] = {
                "qty":             d(pos["qty"]),
                "precio_entrada":  d(pos["precio_entrada"]),
                "tp":              d(pos["tp"]),
                "sl":              d(pos["sl"]),
                "max_precio":      d(pos.get("max_precio", pos["precio_entrada"])),
                "trailing_activo": pos.get("trailing_activo", False),
                "trailing_stage":  int(pos.get("trailing_stage", 0)),
                "setup_fuerte":    pos.get("setup_fuerte", False),
                "timestamp":       int(pos.get("timestamp", time.time()))
            }
        return posiciones
    except Exception as e:
        print(f"⚠️ Error cargando posiciones: {e}")
        return {}

def get_data(sym: str, inv: str, lim: int):
    try:
        k = client.get_klines(symbol=sym, interval=inv, limit=lim)
        closes = np.array([float(x[4]) for x in k], dtype=float)
        vols   = np.array([float(x[5]) for x in k], dtype=float)
        highs  = np.array([float(x[2]) for x in k], dtype=float)
        return closes, vols, highs
    except Exception:
        return None, None, None

def get_symbol_info_cached(sym: str):
    if sym not in _SYMBOL_CACHE:
        _SYMBOL_CACHE[sym] = client.get_symbol_info(sym)
    return _SYMBOL_CACHE[sym]

def get_symbol_filters(sym: str) -> dict:
    info = get_symbol_info_cached(sym)
    out = {
        "min_qty":          D('0.00000001'),
        "step_size":        D('0.00000001'),
        "market_min_qty":   None,
        "market_step_size": None,
        "min_notional":     D('10'),
        "quote_precision":  info.get("quoteAssetPrecision", 8),
        "base_precision":   info.get("baseAssetPrecision", 8),
    }
    for f in info.get("filters", []):
        ft = f.get("filterType")
        if ft == "LOT_SIZE":
            out["min_qty"]   = d(f.get("minQty", '0.00000001'))
            out["step_size"] = d(f.get("stepSize", '0.00000001'))
        elif ft == "MARKET_LOT_SIZE":
            out["market_min_qty"]   = d(f.get("minQty", '0.00000001'))
            out["market_step_size"] = d(f.get("stepSize", '0.00000001'))
        elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
            mn = f.get("minNotional") or f.get("notional")
            if mn is not None:
                out["min_notional"] = d(mn, '10')
    return out

def floor_to_step(qty, step) -> D:
    qty = d(qty)
    step = d(step)
    if step <= 0:
        return qty
    return (qty // step) * step

def get_free_balance(asset: str) -> D:
    try:
        bal = client.get_asset_balance(asset=asset)
        if not bal:
            return D('0')
        return d(bal.get("free", '0'))
    except Exception:
        return D('0')

def posiciones_abiertas_exchange():
    abiertas = []
    try:
        balances = client.get_account().get('balances', [])
        free_map = {b['asset']: d(b['free']) + d(b.get('locked', '0')) for b in balances}
        for sym in WATCHLIST:
            asset = sym.replace("USDT", "")
            total = free_map.get(asset, D('0'))
            filters = get_symbol_filters(sym)
            min_qty = filters["market_min_qty"] or filters["min_qty"]
            if total >= min_qty and total > 0:
                abiertas.append(sym)
    except Exception:
        pass
    return abiertas

def precio_promedio_order(order):
    exec_qty  = d(order.get("executedQty", '0'))
    cum_quote = d(order.get("cummulativeQuoteQty", '0'))
    if exec_qty > 0 and cum_quote > 0:
        return cum_quote / exec_qty, exec_qty
    fills = order.get("fills", [])
    if fills:
        total_qty   = D('0')
        total_quote = D('0')
        for f in fills:
            q = d(f.get("qty", '0'))
            p = d(f.get("price", '0'))
            total_qty   += q
            total_quote += q * p
        if total_qty > 0:
            return total_quote / total_qty, total_qty
    return D('0'), D('0')

def detectar_rebote_btc(c_btc, rsi_btc) -> bool:
    try:
        if c_btc is None or len(c_btc) < 10:
            return False
        rsi_series = talib.RSI(c_btc, 14)
        if rsi_series is None or len(rsi_series) < 10:
            return False
        rsi_actual = rsi_series[-1]
        rsi_prev   = min(rsi_series[-10:])
        if is_nan(rsi_actual) or is_nan(rsi_prev):
            return False
        if not (rsi_actual > REBOTE_RSI_MIN and rsi_prev < REBOTE_RSI_MAX):
            return False
        velas_subiendo = all(
            c_btc[-i-1] > c_btc[-i-2]
            for i in range(1, REBOTE_VELAS + 1)
        )
        if not velas_subiendo:
            return False
        minimo_reciente = float(min(c_btc[-6:-1]))
        subida = (float(c_btc[-1]) - minimo_reciente) / minimo_reciente
        if subida < 0.002:
            return False
        return True
    except Exception:
        return False

def clasificar_estado_mercado(c_btc_15m, c_btc_1h):
    try:
        if c_btc_15m is None or c_btc_1h is None:
            return "RISK_OFF"
        if len(c_btc_15m) < 30 or len(c_btc_1h) < 30:
            return "RISK_OFF"
        ema20_15m = talib.EMA(c_btc_15m, 20)[-1]
        rsi15_btc = talib.RSI(c_btc_15m, 14)[-1]
        ema20_1h  = talib.EMA(c_btc_1h, 20)[-1]
        rsi1h_btc = talib.RSI(c_btc_1h, 14)[-1]
        if any(is_nan(x) for x in [ema20_15m, rsi15_btc, ema20_1h, rsi1h_btc]):
            return "RISK_OFF"
        precio_15m = c_btc_15m[-1]
        precio_1h  = c_btc_1h[-1]
        ext_1h = (precio_1h - ema20_1h) / ema20_1h if ema20_1h > 0 else 0.0
        # 1) Riesgo bajo / mercado flojo
        # FIX #8 — Semáforo BTC: límite inferior 48 → 45 (evita bloquear rebotes válidos en 45-47)
        if precio_15m < ema20_15m or rsi15_btc < 45:
            return "RISK_OFF"
        # 2) BTC demasiado caliente
        if rsi1h_btc >= 72 or ext_1h >= 0.025:
            return "EUPHORIA"
        # 3) BTC fuerte pero dominando solo
        if rsi1h_btc >= 66:
            return "BTC_SOLO"
        # 4) Ventana más sana para alts
        return "ALT_WINDOW"
    except Exception:
        return "RISK_OFF"

# =========================
# HELPERS SNIPER
# =========================
def detectar_sniper_alt(c_coin_15m, v_coin_15m, c_btc_15m, rsi_1h_alt, ema99_1h_alt):
    try:
        if c_coin_15m is None or v_coin_15m is None or c_btc_15m is None:
            return False
        if len(c_coin_15m) < 30 or len(v_coin_15m) < 25 or len(c_btc_15m) < 30:
            return False
        ret_alt = (c_coin_15m[-2] - c_coin_15m[-6]) / c_coin_15m[-6]
        ret_btc = (c_btc_15m[-2] - c_btc_15m[-6]) / c_btc_15m[-6]
        rsi15_alt = talib.RSI(c_coin_15m, 14)[-2]
        ema10_alt = talib.EMA(c_coin_15m, 10)[-2]
        ema20_alt = talib.EMA(c_coin_15m, 20)
        precio_alt = c_coin_15m[-2]
        if any(is_nan(x) for x in [rsi15_alt, ema10_alt, precio_alt, rsi_1h_alt, ema99_1h_alt]):
            return False
        if len(ema20_alt) < 4 or any(is_nan(x) for x in [ema20_alt[-2], ema20_alt[-3]]):
            return False
        vol_avg = float(np.mean(v_coin_15m[-20:-2]))
        if vol_avg <= 0:
            return False
        vol_ratio  = float(v_coin_15m[-2]) / vol_avg
        extension  = (precio_alt - ema10_alt) / ema10_alt if ema10_alt > 0 else 1.0
        drift      = abs(c_coin_15m[-1] - precio_alt) / precio_alt if precio_alt > 0 else 1.0
        ema20_up   = ema20_alt[-2] > ema20_alt[-3]
        sobre_ema99 = c_coin_15m[-2] >= float(ema99_1h_alt) * 0.998
        return bool(
            d(ret_alt) >= SNIPER_ALT_RET_MIN and
            d(ret_btc) <= SNIPER_BTC_RET_MAX and
            vol_ratio >= float(SNIPER_VOL_MIN) and
            float(rsi15_alt) >= SNIPER_RSI15_MIN and
            float(rsi_1h_alt) >= SNIPER_RSI1H_MIN and
            precio_alt > ema10_alt and
            ema20_up and
            sobre_ema99 and
            extension < SNIPER_MAX_EXT and
            drift < SNIPER_MAX_DRIFT
        )
    except Exception:
        return False

# =========================
# TRADING
# =========================
def comprar(sym: str):
    try:
        filters      = get_symbol_filters(sym)
        min_notional = filters["min_notional"]
        quote_amount = TRADE_SIZE if TRADE_SIZE >= min_notional else min_notional
        order = client.create_order(
            symbol=sym,
            side='BUY',
            type='MARKET',
            quoteOrderQty=float(quote_amount)
        )
        avg_price, qty = precio_promedio_order(order)
        if avg_price <= 0 or qty <= 0:
            tg(f"❌ Compra inválida {sym}: sin fills válidos")
            return None, None
        return avg_price, qty
    except Exception as e:
        tg(f"❌ Error comprando {sym}: {e}")
        return None, None

def vender(sym: str, qty):
    try:
        asset    = sym.replace("USDT", "")
        free_bal = get_free_balance(asset)
        if free_bal <= 0:
            tg(f"❌ Error vendiendo {sym}: saldo libre 0")
            return None, None
        filters = get_symbol_filters(sym)
        step    = filters["market_step_size"] or filters["step_size"]
        min_qty = filters["market_min_qty"] or filters["min_qty"]
        qty_to_sell = min(d(qty), free_bal)
        qty_adj     = floor_to_step(qty_to_sell, step)
        if qty_adj < min_qty or qty_adj <= 0:
            tg(f"❌ Error vendiendo {sym}: qty {qty_adj} < mínimo {min_qty}")
            return None, None
        order = client.create_order(
            symbol=sym,
            side='SELL',
            type='MARKET',
            quantity=float(qty_adj)
        )
        avg_price, sold_qty = precio_promedio_order(order)
        if avg_price <= 0 or sold_qty <= 0:
            tg(f"❌ Venta inválida {sym}: sin fills válidos")
            return None, None
        return avg_price, sold_qty
    except Exception as e:
        tg(f"❌ Error vendiendo {sym}: {e}")
        return None, None

# =========================
# BOT PRINCIPAL
# =========================
def run_bot() -> None:
    tg(
        f"🎯 {LOG_PREFIX} — TP 2.2% | SL 2.8% | Trailing 3 fases | "
        f"Timeout 120m/+0.6% | Bypass +0.8% | Rebote oversold | MAX 5 POS"
    )
    ultima_senal   = {}
    posiciones_bot = load_positions()
    estado_mercado = "NORMAL"

    # FIX #5 — sniper_habilitado siempre False: ahora se activa correctamente en BTC_SOLO
    sniper_habilitado = SNIPER_MODE

    if posiciones_bot:
        print(f"🔄 Posiciones recuperadas: {list(posiciones_bot.keys())}")
        tg(f"🔄 {LOG_PREFIX}: posiciones recuperadas: {', '.join(posiciones_bot.keys())}")

    while True:
        try:
            changed = False

            # =========================
            # GESTIÓN POSICIONES ABIERTAS
            # =========================
            for sym in list(posiciones_bot.keys()):
                pos = posiciones_bot[sym]
                c, _, _ = get_data(sym, "1m", 3)
                if c is None or len(c) < 2:
                    continue
                precio_actual = d(c[-1])
                entrada       = d(pos['precio_entrada'])
                qty           = d(pos['qty'])
                tp            = d(pos['tp'])
                sl            = d(pos['sl'])
                if entrada <= 0:
                    continue
                pnl_pct          = ((precio_actual - entrada) / entrada) * D('100')
                pnl              = (precio_actual - entrada) / entrada
                max_precio       = d(pos.get('max_precio', entrada))
                trailing_activo  = pos.get('trailing_activo', False)
                stage            = int(pos.get('trailing_stage', 0))
                setup_fuerte_pos = pos.get('setup_fuerte', False)

                stg1 = TRAILING_STRONG_STAGE1 if setup_fuerte_pos else TRAILING_STAGE1
                stg2 = TRAILING_STRONG_STAGE2 if setup_fuerte_pos else TRAILING_STAGE2
                stg3 = TRAILING_STRONG_STAGE3 if setup_fuerte_pos else TRAILING_STAGE3
                dst1 = TRAILING_STRONG_DIST_1 if setup_fuerte_pos else TRAILING_DIST_1
                dst2 = TRAILING_STRONG_DIST_2 if setup_fuerte_pos else TRAILING_DIST_2
                dst3 = TRAILING_STRONG_DIST_3 if setup_fuerte_pos else TRAILING_DIST_3

                if precio_actual > max_precio:
                    posiciones_bot[sym]['max_precio'] = precio_actual
                    max_precio = precio_actual
                    changed = True

                if not trailing_activo and pnl >= stg1:
                    posiciones_bot[sym]['trailing_activo'] = True
                    posiciones_bot[sym]['trailing_stage']  = 1
                    trailing_activo = True
                    stage = 1
                    nuevo_sl = max(
                        entrada * (D('1') + BREAKEVEN_LOCK),
                        max_precio * (D('1') - dst1)
                    )
                    posiciones_bot[sym]['sl'] = nuevo_sl
                    sl = nuevo_sl
                    tg(
                        f"🔒 TRAILING FASE 1 — {sym}\n"
                        f"📈 PnL: +{float(pnl*100):.2f}%\n"
                        f"🛡️ SL: {float(nuevo_sl):.6f}\n"
                        f"🎯 TP: {float(tp):.6f}"
                    )
                    changed = True

                if trailing_activo:
                    # FIX #3 — tg() y changed=True estaban en la misma línea (syntax error)
                    if pnl >= stg3 and stage < 3:
                        stage = 3
                        posiciones_bot[sym]['trailing_stage'] = 3
                        tg(f"🚀 TRAILING FASE 3 — {sym} | +{float(pnl*100):.2f}% | dist 0.5%")
                        changed = True
                    elif pnl >= stg2 and stage < 2:
                        stage = 2
                        posiciones_bot[sym]['trailing_stage'] = 2
                        tg(f"⚡ TRAILING FASE 2 — {sym} | +{float(pnl*100):.2f}% | dist 0.6%")
                        changed = True

                    dist = (
                        dst1 if stage == 1 else
                        dst2 if stage == 2 else
                        dst3
                    )
                    nuevo_sl = max(
                        entrada * (D('1') + BREAKEVEN_LOCK),
                        max_precio * (D('1') - dist)
                    )
                    if nuevo_sl > sl:
                        posiciones_bot[sym]['sl'] = nuevo_sl
                        sl = nuevo_sl
                        changed = True

                tiempo_min = (time.time() - float(pos.get("timestamp", time.time()))) / 60.0
                timeout_min_dinamico = PROFIT_TIMEOUT_MIN
                if estado_mercado == "BTC_SOLO":
                    timeout_min_dinamico = TIMEOUT_BTC_SOLO_MIN
                elif estado_mercado == "RISK_OFF":
                    timeout_min_dinamico = TIMEOUT_RISK_OFF_MIN

                if (
                    not trailing_activo and
                    tiempo_min >= timeout_min_dinamico and
                    pnl >= PROFIT_TIMEOUT_PCT
                ):
                    c5, _, _ = get_data(sym, "5m", TIMEOUT_5M_BARS)
                    enfriado = False
                    rsi5_val = None
                    ema5_val = None
                    if c5 is not None and len(c5) >= 15:
                        rsi5_val = talib.RSI(c5, 14)[-1]
                        ema5_val = talib.EMA(c5, 10)[-1]
                        if not is_nan(rsi5_val) and not is_nan(ema5_val):
                            enfriado = (
                                float(rsi5_val) <= TIMEOUT_RSI_MAX or
                                float(precio_actual) < float(ema5_val)
                            )
                    if enfriado:
                        precio_venta, sold_qty = vender(sym, qty)
                        if precio_venta:
                            motivo = []
                            if rsi5_val is not None and not is_nan(rsi5_val) and float(rsi5_val) <= TIMEOUT_RSI_MAX:
                                motivo.append(f"RSI5m {round(float(rsi5_val), 1)}")
                            if ema5_val is not None and not is_nan(ema5_val) and float(precio_actual) < float(ema5_val):
                                motivo.append("precio<EMA10")
                            tg(
                                f"⏰ PROFIT TIMEOUT PRO — {sym}\n"
                                f"💰 Entrada: {float(entrada):.6f}\n"
                                f"🎯 Salida:  {float(precio_venta):.6f}\n"
                                f"📈 PnL: +{float(pnl*100):.2f}%\n"
                                f"⏱️ Tiempo: {round(tiempo_min)} min\n"
                                f"🔍 Motivo: {' | '.join(motivo)}"
                            )
                            del posiciones_bot[sym]
                            save_positions(posiciones_bot)
                            changed = True
                            continue

                # Defensa automática cuando BTC domina o el mercado se apaga
                defense_loss_limit = None
                if estado_mercado == "BTC_SOLO":
                    defense_loss_limit = DEFENSE_LOSS_BTC_SOLO
                elif estado_mercado == "RISK_OFF":
                    defense_loss_limit = DEFENSE_LOSS_RISK_OFF

                if (
                    not trailing_activo and
                    defense_loss_limit is not None and
                    tiempo_min >= timeout_min_dinamico and
                    pnl <= defense_loss_limit
                ):
                    precio_venta, sold_qty = vender(sym, qty)
                    if precio_venta:
                        resultado = (precio_venta - entrada) * sold_qty
                        tg(
                            f"🛡️ DEFENSA MERCADO — {sym}\n"
                            f"📉 Estado: {estado_mercado}\n"
                            f"💰 Entrada: {float(entrada):.6f}\n"
                            f"🎯 Salida:  {float(precio_venta):.6f}\n"
                            f"📦 Qty: {float(sold_qty):.8f}\n"
                            f"📈 PnL: {float(pnl_pct):.2f}% | ${float(resultado):+.2f}\n"
                            f"⏱️ Tiempo: {round(tiempo_min)} min"
                        )
                        del posiciones_bot[sym]
                        changed = True
                        continue

                if precio_actual >= (tp * TP_BUFFER):
                    precio_venta, sold_qty = vender(sym, qty)
                    if precio_venta:
                        ganancia = (precio_venta - entrada) * sold_qty
                        tg(
                            f"✅ TAKE PROFIT — {sym}\n"
                            f"💰 Entrada: {float(entrada):.6f}\n"
                            f"🎯 Salida:  {float(precio_venta):.6f}\n"
                            f"📦 Qty: {float(sold_qty):.8f}\n"
                            f"📈 PnL: +{float(pnl_pct):.2f}% | +${float(ganancia):.2f}"
                        )
                        del posiciones_bot[sym]
                        changed = True
                elif precio_actual <= sl:
                    precio_venta, sold_qty = vender(sym, qty)
                    if precio_venta:
                        perdida = (entrada - precio_venta) * sold_qty
                        tg(
                            f"🛑 STOP LOSS — {sym}\n"
                            f"💰 Entrada: {float(entrada):.6f}\n"
                            f"❌ Salida:  {float(precio_venta):.6f}\n"
                            f"📦 Qty: {float(sold_qty):.8f}\n"
                            f"📉 PnL: {float(pnl_pct):.2f}% | -${float(perdida):.2f}"
                        )
                        del posiciones_bot[sym]
                        changed = True

            if changed:
                save_positions(posiciones_bot)

            # =========================
            # SEMÁFORO BTC
            # =========================
            c_btc, _, _ = get_data("BTCUSDT", "15m", 30)
            if c_btc is None or len(c_btc) < 25:
                time.sleep(CHECK_INTERVAL)
                continue
            rsi_btc = talib.RSI(c_btc, 14)[-1]
            ema_btc = talib.EMA(c_btc, 20)[-1]
            if is_nan(rsi_btc) or is_nan(ema_btc):
                time.sleep(CHECK_INTERVAL)
                continue

            # FIX #8 — Semáforo BTC: límite inferior 48 → 45
            mercado_ok = bool(c_btc[-1] > ema_btc and 45 < rsi_btc < 78)
            rebote_ok  = detectar_rebote_btc(c_btc, rsi_btc)

            c_btc_1h_em, _, _ = get_data("BTCUSDT", "1h", 50)
            estado_mercado = clasificar_estado_mercado(
                c_btc,
                c_btc_1h_em if c_btc_1h_em is not None else c_btc
            )

            if mercado_ok:
                modo_semaforo = "🟢 ACTIVE"
            elif rebote_ok:
                modo_semaforo = "🟡 REBOTE"
            else:
                modo_semaforo = "🔴 WAIT"

            print(f"{time.strftime('%H:%M:%S')} BTC: {c_btc[-1]:.2f} | RSI: {rsi_btc:.1f} | {modo_semaforo} | Estado:{estado_mercado}")

            slots_libres = MAX_POSITIONS - len(posiciones_bot)
            pos_exchange = posiciones_abiertas_exchange()

            if estado_mercado == "RISK_OFF":
                permitir_nuevas           = False
                max_positions_dinamico    = 0
                drift_limit               = 0.009
                vol_mult_req              = float(MIN_VOL_MULT)
                rsi1h_extremo_dinamico    = 70
                sniper_habilitado         = False
            elif estado_mercado == "BTC_SOLO":
                permitir_nuevas           = True
                max_positions_dinamico    = 2
                drift_limit               = 0.008
                vol_mult_req              = max(float(MIN_VOL_MULT), 1.45)
                rsi1h_extremo_dinamico    = 68
                # FIX #5 — sniper_habilitado se activa correctamente en BTC_SOLO
                sniper_habilitado         = SNIPER_MODE
            elif estado_mercado == "EUPHORIA":
                permitir_nuevas           = False
                max_positions_dinamico    = 0
                drift_limit               = 0.008
                vol_mult_req              = max(float(MIN_VOL_MULT), 1.50)
                rsi1h_extremo_dinamico    = 68
                sniper_habilitado         = False
            else:  # ALT_WINDOW
                permitir_nuevas           = True
                max_positions_dinamico    = MAX_POSITIONS
                drift_limit               = 0.012
                vol_mult_req              = float(MIN_VOL_MULT)
                rsi1h_extremo_dinamico    = 75
                sniper_habilitado         = SNIPER_MODE

            slots_dinamicos_libres = max_positions_dinamico - len(posiciones_bot)

            print(
                f"Posiciones: {len(posiciones_bot)}/{MAX_POSITIONS} | "
                f"Libres reales: {slots_libres} | "
                f"Libres dinámicos: {slots_dinamicos_libres} | "
                f"Exchange: {pos_exchange}"
            )
            # FIX #9 — f-string con salto de línea literal: separado en dos prints
            print(
                f"🌍 Estado mercado: {estado_mercado} | permitir_nuevas={permitir_nuevas} | "
                f"drift<{drift_limit:.3f} | vol>={vol_mult_req:.2f}x | "
                f"RSI1H<={rsi1h_extremo_dinamico} | sniper={sniper_habilitado}"
            )

            if slots_libres <= 0 or slots_dinamicos_libres <= 0 or not permitir_nuevas:
                time.sleep(CHECK_INTERVAL)
                continue

            # =========================
            # ESCANEO
            # =========================
            for coin in WATCHLIST:
                if slots_libres <= 0 or slots_dinamicos_libres <= 0:
                    break
                if coin in posiciones_bot or coin in pos_exchange:
                    continue

                bypass = False
                if not mercado_ok and not rebote_ok:
                    try:
                        c_coin_15m, _, _ = get_data(coin, "15m", 30)
                        if c_coin_15m is not None and len(c_coin_15m) >= 22 and len(c_btc) >= 6:
                            ret_coin  = (c_coin_15m[-2] - c_coin_15m[-6]) / c_coin_15m[-6]
                            ret_btc   = (c_btc[-2]      - c_btc[-6])      / c_btc[-6]
                            ema20_15m = talib.EMA(c_coin_15m, 20)
                            ema20_up  = (
                                len(ema20_15m) >= 3 and
                                not is_nan(ema20_15m[-2]) and
                                not is_nan(ema20_15m[-3]) and
                                ema20_15m[-2] > ema20_15m[-3]
                            )
                            if d(ret_coin) > DESACOPLE_PCT and ret_btc < 0 and ema20_up:
                                bypass = True
                                print(f"⚡ Bypass desacople: {coin} +{ret_coin*100:.2f}% vs BTC {ret_btc*100:.2f}%")
                    except Exception:
                        bypass = False

                if not mercado_ok and not rebote_ok and not bypass:
                    if not sniper_habilitado:
                        continue

                c_alt, v_alt, _ = get_data(coin, "15m", 120)
                if c_alt is None or v_alt is None or len(c_alt) < 100 or len(v_alt) < 20:
                    continue

                rsi_alt      = talib.RSI(c_alt, 14)[-2]
                ema_alt      = talib.EMA(c_alt, 10)[-2]
                ema15_alt    = talib.EMA(c_alt, 15)[-2]
                ema21_alt    = talib.EMA(c_alt, 21)[-2]
                precio_señal = c_alt[-2]

                if any(is_nan(x) for x in [rsi_alt, ema_alt, ema15_alt, ema21_alt, precio_señal]):
                    continue

                drift             = abs(c_alt[-1] - precio_señal) / precio_señal if precio_señal > 0 else 1.0
                no_tarde          = drift < drift_limit

                # FIX #2 — extension y no_sobreextendido estaban en la misma línea (syntax error)
                extension         = (precio_señal - ema_alt) / ema_alt if ema_alt > 0 else 1.0
                no_sobreextendido = extension < 0.023

                # ANTI-FOMO: distancia al máximo reciente 20 velas
                max_15m = float(np.max(c_alt[-20:-1])) if len(c_alt) >= 20 else float(c_alt[-2])
                dist_max = (max_15m - precio_señal) / precio_señal if precio_señal > 0 else 0.0
                if extension < 0.010:
                    dist_min_req = 0.002
                elif extension < 0.015:
                    dist_min_req = 0.003
                else:
                    dist_min_req = 0.004
                no_cerca_max = dist_max >= dist_min_req

                # Cálculo de vol_ratio ANTES de usarlo en no_vol_climax
                vol_avg = float(np.mean(v_alt[-50:-2])) if len(v_alt) > 50 else float(np.mean(v_alt[:-2]))
                if vol_avg <= 0:
                    continue
                vol_ratio = float(v_alt[-2]) / vol_avg

                # FIX #1 — no_vol_climax usaba vol_ratio antes de calcularlo
                # FIX con lógica más defensiva (OR en lugar de AND puro)
                no_vol_climax = not (
                    (vol_ratio >= 6.0 and extension > 0.010) or
                    (vol_ratio >= 5.5 and extension > 0.015)
                )

                tramo    = c_alt[-14:-2]
                rango_pct = (
                    (float(np.max(tramo)) - float(np.min(tramo))) / float(np.min(tramo))
                    if len(tramo) > 0 and float(np.min(tramo)) > 0 else 0.0
                )
                not_flat = rango_pct > 0.012

                vol_min_req = float(MIN_VOL_BYPASS) if bypass else vol_mult_req

                impulso_prev  = 0.0
                tramo_impulso = c_alt[-8:-2]
                if len(tramo_impulso) > 0 and float(np.min(tramo_impulso)) > 0:
                    impulso_prev = (
                        (float(np.max(tramo_impulso)) - float(np.min(tramo_impulso)))
                        / float(np.min(tramo_impulso))
                    )

                pullback_cont = (
                    precio_señal > (ema15_alt * (1 - CONT_EMA_TOL)) and
                    precio_señal > ema21_alt and
                    c_alt[-2] > c_alt[-3]
                )

                # 1H con suficientes velas para EMA99
                c_1h, _, _ = get_data(coin, "1h", 120)
                if c_1h is None or len(c_1h) < 100:
                    continue
                rsi_1h      = talib.RSI(c_1h, 14)[-1]
                rsi_1h_prev = talib.RSI(c_1h, 14)[-2]
                ema_1h      = talib.EMA(c_1h, 10)[-1]
                ema99_1h    = talib.EMA(c_1h, 99)[-1]
                if any(is_nan(x) for x in [rsi_1h, rsi_1h_prev, ema_1h, ema99_1h]):
                    continue

                tendencia_1h_ok  = bool(c_1h[-1] > ema_1h and rsi_1h > 47)
                no_bajista_1h    = bool(c_1h[-1] >= (ema99_1h * 0.997))
                no_rsi1h_extremo = bool(rsi_1h <= rsi1h_extremo_dinamico)
                no_debilidad_1h  = bool(rsi_1h >= (rsi_1h_prev - 2.0))
                rsi_1h_ok        = bool(rsi_1h >= rsi_1h_prev)

                sniper_ok = False
                if sniper_habilitado:
                    c_coin_sniper, v_coin_sniper, _ = get_data(coin, "15m", 40)
                    sniper_ok = detectar_sniper_alt(
                        c_coin_sniper, v_coin_sniper, c_btc,
                        rsi_1h, ema99_1h
                    )
                    if sniper_ok:
                        print(f"🎯 SNIPER detectado: {coin}")

                # 4H
                c_4h, _, _ = get_data(coin, "4h", 30)
                if c_4h is None or len(c_4h) < 26:
                    continue
                rsi_4h   = talib.RSI(c_4h, 14)[-1]
                ema25_4h = talib.EMA(c_4h, 25)[-1]
                if any(is_nan(x) for x in [rsi_4h, ema25_4h]):
                    continue

                if rebote_ok and not mercado_ok:
                    tendencia_4h_ok = bool(((c_4h[-1] > ema25_4h) or (rsi_4h > 50)) and rsi_4h < 72)
                else:
                    tendencia_4h_ok = bool(((c_4h[-1] > ema25_4h) or (rsi_4h > 52)) and rsi_4h < 75)

                ahora       = time.time()
                cooldown_ok = (ahora - ultima_senal.get(coin, 0)) > COOLDOWN_SEC

                setup_breakout_ok = (
                    55 < rsi_alt < 68 and
                    precio_señal > ema_alt and
                    vol_ratio >= vol_min_req and
                    cooldown_ok and
                    tendencia_1h_ok and
                    no_bajista_1h and
                    no_debilidad_1h and
                    rsi_1h_ok and
                    tendencia_4h_ok and
                    no_rsi1h_extremo and
                    not_flat and
                    no_tarde and
                    no_sobreextendido and
                    no_cerca_max and
                    no_vol_climax
                )

                base_cont_vol = max(float(MIN_VOL_MULT) - 0.50, float(CONT_MIN_VOL))
                setup_continuacion_ok = (
                    52 < rsi_alt < 66 and
                    impulso_prev > 0.012 and
                    pullback_cont and
                    vol_ratio >= base_cont_vol and
                    cooldown_ok and
                    tendencia_1h_ok and
                    no_bajista_1h and
                    no_debilidad_1h and
                    tendencia_4h_ok and
                    no_rsi1h_extremo and
                    not_flat and
                    no_tarde and
                    no_sobreextendido and
                    no_cerca_max and
                    no_vol_climax and
                    rsi_1h > float(CONT_RSI1H_MIN) and
                    rsi_4h > float(CONT_RSI4H_MIN)
                )

                setup_fuerte = bool(
                    vol_ratio >= 2.0 and
                    rsi_4h >= 60 and
                    rsi_1h >= rsi_1h_prev
                )

                setup_sniper_ok = bool(
                    sniper_ok and
                    cooldown_ok and
                    no_rsi1h_extremo and
                    no_debilidad_1h and
                    tendencia_4h_ok
                )

                if setup_breakout_ok or setup_continuacion_ok or setup_sniper_ok:
                    precio_compra, qty = comprar(coin)
                    if precio_compra and qty:
                        tp_pct_real = D('0.028') if setup_fuerte else TP_PCT
                        tp_precio   = precio_compra * (D('1') + tp_pct_real)
                        sl_precio   = precio_compra * (D('1') - SL_PCT)
                        posiciones_bot[coin] = {
                            'qty':             qty,
                            'precio_entrada':  precio_compra,
                            'tp':              tp_precio,
                            'sl':              sl_precio,
                            'max_precio':      precio_compra,
                            'trailing_activo': False,
                            'trailing_stage':  0,
                            'setup_fuerte':    setup_fuerte,
                            'timestamp':       int(ahora)
                        }
                        save_positions(posiciones_bot)
                        ultima_senal[coin] = ahora
                        slots_libres           -= 1
                        slots_dinamicos_libres -= 1

                        invertido_real = precio_compra * qty
                        if setup_sniper_ok and not setup_breakout_ok and not setup_continuacion_ok:
                            modo_entrada = "SNIPER"
                        else:
                            modo_entrada = "CONTINUACION" if (setup_continuacion_ok and not setup_breakout_ok) else "BREAKOUT"
                        if rebote_ok and not mercado_ok:
                            modo_entrada += "+REBOTE"
                        if sniper_habilitado and setup_sniper_ok:
                            modo_entrada += "+BTC_SOLO"

                        tg(
                            f"✅ COMPRA EJECUTADA — {coin}\n"
                            f"🧠 Modo: {modo_entrada}\n"
                            f"💰 Precio medio:   {float(precio_compra):.6f}\n"
                            f"📦 Cantidad:       {float(qty):.8f}\n"
                            f"💵 Invertido real: ~${float(invertido_real):.2f}\n"
                            f"🎯 TP: {float(tp_precio):.6f} (+{float(tp_pct_real*100):.1f}%)\n"
                            f"🛑 SL: {float(sl_precio):.6f} (-2.8%)\n"
                            f"📊 RSI15m: {rsi_alt:.1f} | RSI1H: {rsi_1h:.1f} "
                            f"(prev {rsi_1h_prev:.1f}, dir {'UP' if rsi_1h_ok else 'DOWN'}) "
                            f"| RSI4H: {rsi_4h:.1f} | EMA99_1H: {ema99_1h:.4f} | Vol: {vol_ratio:.2f}x\n"
                            f"⚡ Bypass: {'Si' if bypass else 'No'} | "
                            f"Rebote: {'Si' if rebote_ok else 'No'} | "
                            f"Pos: {len(posiciones_bot)}/{MAX_POSITIONS}"
                        )

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"❌ Error principal: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run_bot()
