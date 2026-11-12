import asyncio, csv, io, json, math, os, re, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
import pandas as pd
from . import analysts, chair, learn, paypal
from .risk import PRESETS, evaluate
from .vault import Vault
from .universe import COMPANIES, TOP100

def _load_env(path):
    """Read KEY=VALUE lines from .env so settings do not need retyping each session."""
    try:
        for line in open(path, encoding="utf-8-sig"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); v = v.split("#")[0].strip().strip('"')
                if v: os.environ.setdefault(k.strip(), v)
    except FileNotFoundError:
        pass


_load_env(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
V = Vault(os.getenv("COIN_DB", "coin.db"))
L = learn.Learner(V.db, V.lock)
_model_path = os.getenv('COIN_MODEL', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'chair-model.json'))
os.environ.setdefault('COIN_MODEL', _model_path)
if L.W is None and os.path.exists(_model_path): L.load_model(_model_path)
chair.LEARNER = L
@asynccontextmanager
async def lifespan(_app):
    for f in (_auto_close_loop, _exit_monitor, _scanner_loop, _learn_loop):  # background workers
        threading.Thread(target=f, daemon=True).start()
    yield


app = FastAPI(title="Coin Board", lifespan=lifespan)
PRICES: dict = {}
SCAN = dict(running=False, error=None)
VERSION = "2026-11-12-final"
PTICK = [0]  # bumps when a live price changes


def auth(x_token: str = Header(default="")):
    if os.getenv("COIN_TOKEN") and x_token != os.getenv("COIN_TOKEN"):
        raise HTTPException(401, "Missing or wrong x-token")


def fail(e): raise HTTPException(400, str(e))


@app.get("/")
def index(): return FileResponse(os.path.join(os.path.dirname(__file__), "..", "static", "index.html"), headers={"Cache-Control": "no-store"})


@app.get("/api/state")
def state():
    s = V.state; ex = V.exits(); pos = [dict(p, price=PRICES.get(p["symbol"]), **ex.get(p["symbol"], dict(stop=None, target=None))) for p in V.positions()]
    return dict(reasoning=chair.status(s), version=VERSION, scanning=SCAN["running"], scan_status=_scan_status(), state=s, limits=V.limits(), positions=pos, equity=round(V.equity(), 2), events=V.events(),
                paypal=paypal.public_config(), chair=bool(os.getenv("CHAIR_BASE_URL") and os.getenv("CHAIR_MODEL")),
                kronos=bool(os.getenv("KRONOS_REPO")), presets=list(PRESETS))


class Sym(BaseModel): symbol: str


@app.post("/api/analyze", dependencies=[Depends(auth)])
def analyze(b: Sym):
    sym = b.symbol.strip().upper()
    if not sym.replace(".", "").replace("-", "").isalnum() or len(sym) > 10: fail("Enter a valid ticker, like NVDA")
    return _board(sym)


def _board(sym, allow_add=True):
    reports, ind, src = analysts.run_analysts(sym); PRICES[sym] = ind["price"]; PTICK[0] += 1
    held = V.held().get(sym, 0); verdict = chair.decide(sym, reports, held, V.state); eq = V.equity(); lim = V.limits()
    if src != 'demo' and not verdict.get('obs_id'):
        verdict['obs_id'] = L.observe(sym, reports, held, {'symbol':sym,'reports':reports}, verdict,
                                     verdict.get('source','rules'), verdict.get('engine','local'))
    price, act = ind["price"], verdict["action"]; qty = 0; stop = target = None
    if not allow_add and act == "buy" and held:
        verdict["action"] = act = "ignore"; verdict["reasoning"] += " (Already holding it, so not adding.)"
    if act == "buy":
        dist = max(ind["atr"] * verdict["stop_atr"], price * 0.01)
        qty = math.floor(min(eq * lim["max_loss_pct"] / 100 * verdict["size_scale"] / dist, eq * lim["max_pos_pct"] / 100 / price))
        stop, target = round(price - dist, 2), round(price + ind["atr"] * verdict["target_atr"], 2)
    elif act == "sell":
        qty = held
    obs_id = verdict.pop("obs_id", None)
    p = dict(id=uuid.uuid4().hex[:10], symbol=sym, side=act if act != "ignore" else "none", qty=qty, price=round(price, 2),
             stop=stop if stop else round(price * 0.95, 2), target=target, confidence=verdict["confidence"], data_source=src, obs_id=obs_id,
             event_risk=any(r["data"].get("event_risk") for r in reports))
    decision = evaluate(p, lim, V.state, eq, V.held()) if act != "ignore" and qty > 0 else None
    if act != "ignore" and qty == 0 and decision is None:
        verdict["reasoning"] += " (You do not own it, so there is nothing to sell.)" if act == "sell" else " (No shares fit inside your limits.)"; p["side"] = "none"
    out = dict(proposal=p, verdict=verdict, reports=reports, decision=decision, executed=None, ts=time.time())
    V.db.execute("insert into proposals values(?,?)", (p["id"], json.dumps(out))); V.db.commit()
    V.log("board", dict(symbol=sym, action=act, confidence=verdict["confidence"], qty=qty, price=p["price"], reasoning=verdict["reasoning"],
                        dissent=verdict["dissent"], engine=verdict["engine"], second=verdict.get("second_opinion"), data_source=src,
                        firewall=decision["status"] if decision else None, firewall_reason=decision["reason"] if decision else None,
                        failed_checks=[c["label"] for c in decision["checks"] if not c["passed"]] if decision else [],
                        reports=[{k: r[k] for k in ("name", "model", "score", "confidence", "summary")} for r in reports]))
    if decision and decision["status"] == "approved" and V.state["mode"] == "autopilot" and (src != "demo" or os.getenv("COIN_DEMO")):
        out["executed"] = _execute(p)
    return out


def _execute(p):
    try:
        if p["side"] == "buy":
            V.buy(p["symbol"], p["qty"], p["price"]); V.set_exit(p["symbol"], p["stop"], p["target"])
        else:
            V.sell(p["symbol"], p["qty"], p["price"])
    except ValueError as e:
        fail(e)
    return dict(status="filled", simulated=True, symbol=p["symbol"], side=p["side"], qty=p["qty"], price=p["price"])


@app.post("/api/proposals/{pid}/approve", dependencies=[Depends(auth)])
def approve(pid: str):
    row = V.db.execute("select data from proposals where id=?", (pid,)).fetchone()
    if not row: fail("Proposal not found")
    out = json.loads(row[0]); p = out["proposal"]
    if out.get("executed"): fail("Already executed")
    if V.state["mode"] == "observe": fail("Watch mode cannot place orders")
    px = analysts.last_price(p["symbol"]) or p["price"]
    if abs(px / p["price"] - 1) > 0.02: fail("The price has moved more than 2% since the board decided. Ask the board again.")
    p["price"] = round(px, 2)
    d = evaluate(p, V.limits(), V.state, V.equity(), V.held())  # re-checked on current server state
    if d["status"] != "approved": raise HTTPException(409, d["reason"])
    out["executed"] = _execute(p); out["decision"] = d
    if p.get("obs_id"): L.feedback(p["obs_id"], 1)   # you agreed with the board: a positive signal for learning
    V.db.execute("update proposals set data=? where id=?", (json.dumps(out), pid)); V.db.commit()
    return out


class Settings(BaseModel):
    mode: str | None = None; preset: str | None = None; reserve: int | None = None; withdraw: int | None = None
    auto_close: bool | None = None; close_time: str | None = None
    watchlist: str | None = None; scan_enabled: bool | None = None; scan_minutes: int | None = None; scan_top: int | None = None; scan_always: bool | None = None
    student_role: str | None = None; student_advisor_days: int | None = None; student_chair_days: int | None = None; student_check_rate: float | None = None


@app.post("/api/settings", dependencies=[Depends(auth)])
def settings(b: Settings):
    if b.mode:
        if b.mode not in ("observe", "approve", "autopilot"): fail("Unknown mode")
        V.update(mode=b.mode)
    if b.preset:
        if b.preset not in PRESETS: fail("Unknown risk style")
        V.update(preset=b.preset)
    if b.close_time is not None:
        try: hh, mm = map(int, b.close_time.split(":")); assert 0 <= hh < 24 and 0 <= mm < 60
        except Exception: fail("Close time must look like 16:15")
        V.update(close_time=b.close_time)
    if b.auto_close is not None: V.update(auto_close=b.auto_close)
    if b.watchlist is not None:
        raw = b.watchlist.strip().lower()
        wl = TOP100[:30] if raw == "@top30" else TOP100 if raw == "@top100" else [x.strip().upper() for x in b.watchlist.replace(";", ",").split(",") if x.strip()]
        if not 1 <= len(wl) <= 200 or not all(x.replace(".", "").replace("-", "").isalnum() and len(x) <= 10 for x in wl): fail("Enter 1 to 200 tickers separated by commas")
        V.update(watchlist=wl)
    if b.student_role is not None:
        if b.student_role not in ("auto", "off", "advisor", "chair"): fail("Unknown student role")
        V.update(student_role=b.student_role)
    if b.student_advisor_days is not None: V.update(student_advisor_days=max(1, min(365, b.student_advisor_days)))
    if b.student_chair_days is not None: V.update(student_chair_days=max(1, min(730, b.student_chair_days)))
    if b.student_check_rate is not None: V.update(student_check_rate=max(0.0, min(1.0, b.student_check_rate)))
    if b.scan_top is not None: V.update(scan_top=max(1, min(30, b.scan_top)))
    if b.scan_minutes is not None: V.update(scan_minutes=max(5, min(240, b.scan_minutes)))
    if b.scan_enabled is not None: V.update(scan_enabled=b.scan_enabled)
    if b.scan_always is not None: V.update(scan_always=b.scan_always)
    if b.reserve is not None and b.withdraw is not None:
        if min(b.reserve, b.withdraw) < 0 or b.reserve + b.withdraw > 100: fail("Reserve plus withdraw must be 100 or less")
        V.update(split=dict(reserve=b.reserve, withdraw=b.withdraw, reinvest=100 - b.reserve - b.withdraw))
    return state()


@app.post("/api/settle", dependencies=[Depends(auth)])
def settle():
    V.update(last_close=_today()); return dict(result=V.settle(), **state())


@app.post("/api/resume", dependencies=[Depends(auth)])
def resume(): V.update(paused=False, peak=V.equity()); return state()


class Amount(BaseModel): amount: float


PP_LOCK = threading.Lock()   # one PayPal money movement at a time


def _pp_err(e):
    r = getattr(e, "response", None)
    return f"HTTP {r.status_code}: {r.text[:300]}" if r is not None else str(e)[:300]


def _amount(a):
    if not 1 <= a <= 100000: fail("Add between $1 and $100,000")
    return round(a, 2)


@app.get("/api/paypal/config")
def paypal_config(): return paypal.public_config(full=True)


@app.post("/api/paypal/orders", dependencies=[Depends(auth)])
def paypal_create(b: Amount):
    """Called by the PayPal button. The amount is set here, on the server, and remembered so it can be checked at capture."""
    a = _amount(b.amount)
    if not paypal.configured(): fail("PayPal is not connected. Add your sandbox keys to .env, or use demo funds.")
    try: o = paypal.create_order(a)
    except Exception as e: fail(f"PayPal error: {_pp_err(e)}")
    V.paypal_new(o["id"], a); return dict(id=o["id"])


def _credit_order(order_id):
    """Capture an approved order and credit the vault exactly once, only for an order this app created, and only if PayPal's amount matches."""
    with PP_LOCK:
        row = V.paypal_get(order_id)
        if not row: fail("Unknown PayPal order. Only orders created by this app can be credited.")
        if row["status"] == "credited": return False   # already added (double click or page reload)
        try: cap = paypal.capture_order(order_id)
        except Exception as e: fail(f"PayPal error: {_pp_err(e)}")
        if cap is None: fail("This order cannot be captured")
        if cap["currency"] != "USD" or abs(cap["amount"] - row["amount"]) > 0.005:
            fail("PayPal reported a different amount than the order, so nothing was credited. Check the payment in your PayPal sandbox.")
        return V.paypal_credit(order_id, cap["capture_id"], cap["payer"])


@app.post("/api/paypal/orders/{order_id}/capture", dependencies=[Depends(auth)])
def paypal_capture(order_id: str):
    return dict(credited=_credit_order(order_id), **state())


@app.post("/api/deposit", dependencies=[Depends(auth)])
def deposit(b: Amount):
    """Demo funds when PayPal is not connected; otherwise a redirect-style PayPal order (fallback if the button cannot load)."""
    a = _amount(b.amount)
    if not paypal.configured():
        ref = f"DEMO-{uuid.uuid4().hex[:8]}"; V.deposit(a, ref=ref, demo=True)
        return dict(order=dict(id=ref, approve_url=None, simulated=True), **state())
    try: o = paypal.create_order(a, redirect=True)
    except Exception as e: fail(f"PayPal error: {_pp_err(e)}")
    V.paypal_new(o["id"], a); return dict(order=o, **state())


class Cap(BaseModel): order_id: str


@app.post("/api/deposit/capture", dependencies=[Depends(auth)])
def capture(b: Cap):
    _credit_order(b.order_id); return state()


@app.post("/api/withdraw", dependencies=[Depends(auth)])
def withdraw(b: Amount):
    """Pay out first; only if PayPal accepts is the profit removed from the vault, so a failed payout never loses money."""
    with PP_LOCK:
        if b.amount <= 0 or b.amount > V.state["withdrawable"] + 1e-9: fail("Amount exceeds withdrawable profit")
        try: pay = paypal.payout(round(b.amount, 2))
        except Exception as e: fail(f"PayPal payout failed, nothing was withdrawn: {_pp_err(e)}")
        V.withdraw(b.amount, payout_id=pay["id"], status=pay["status"], recipient=pay["recipient"], simulated=pay["simulated"])
    return dict(payout=pay, **state())


def _ny_now():
    try: return datetime.now(ZoneInfo("America/New_York"))
    except Exception: return datetime.now(timezone(timedelta(hours=-5)))  # fallback if tz data is missing (off by 1h in summer)


def _today(): return _ny_now().strftime("%Y-%m-%d")


@app.get("/api/history")
def history():
    days = [dict(ts=e["ts"], **e["data"]) for e in V.events(500, ["settle"], ascending=True)]
    return dict(points=V.history(), days=days)


def _sym(s): return "".join(ch for ch in s.upper() if ch.isalnum() or ch in ".-")[:10] or None


def _kinds(kind): return [k for k in kind.split(",") if k] if kind else None


@app.get("/api/events")
def events(kind: str = "", q: str = "", limit: int = 50, symbol: str = ""):
    return V.events(min(max(limit, 1), 500), _kinds(kind), q.strip() or None, symbol=_sym(symbol))


@app.get("/api/events.csv")
def events_csv(kind: str = "", q: str = "", symbol: str = ""):
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(["time_utc", "kind", "details"])
    for e in V.events(100000, _kinds(kind), q.strip() or None, ascending=True, symbol=_sym(symbol)):
        w.writerow([datetime.utcfromtimestamp(e["ts"]).isoformat(), e["kind"], json.dumps(e["data"])])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=coin-board-audit.csv"})


def _auto_close_tick():
    s = V.state
    if s['auto_close']:
        now=_ny_now();hh,mm=map(int,s['close_time'].split(':'))
        if now.weekday()<5 and (now.hour,now.minute)>=(hh,mm) and s['last_close']!=_today():
            V.update(last_close=_today());V.log('auto_close',dict(at=now.isoformat()));V.settle()


def _auto_close_loop():
    while True:
        try:
            _auto_close_tick()
        except Exception:
            pass
        time.sleep(30)


def _do_sell(sym, px, why):
    q = V.held().get(sym, 0)
    if q: V.sell(sym, q, px, reason=why)


@app.post("/api/positions/{sym}/sell", dependencies=[Depends(auth)])
def sell_position(sym: str):
    sym = sym.upper()
    if not V.held().get(sym): fail(f"You do not own {sym}")
    px = analysts.last_price(sym) or PRICES.get(sym)
    if not px: fail("No current price available. Ask the board about this ticker first.")
    _do_sell(sym, px, "manual"); return state()


def _exit_tick():
    ex=V.exits()
    for pos in V.positions():
        px=analysts.last_price(pos['symbol'])
        if not px:continue
        if PRICES.get(pos['symbol'])!=px:PTICK[0]+=1
        PRICES[pos['symbol']]=px;e=ex.get(pos['symbol'])
        if e and e['stop'] and px<=e['stop']:_do_sell(pos['symbol'],px,'stop')
        elif e and e['target'] and px>=e['target']:_do_sell(pos['symbol'],px,'target')


def _exit_monitor():
    """Every minute: sell any position whose price has reached its stop or target. Protective, so always on."""
    while True:
        try:
            _exit_tick()
        except Exception:
            pass
        time.sleep(60)


def _market_open():
    n = _ny_now()
    return n.weekday() < 5 and (9, 30) <= (n.hour, n.minute) < (16, 0)


def _scan_once():
    """Run the Board over the whole watchlist. In autopilot, approved trades execute; otherwise they wait for approval."""
    if SCAN["running"]: return
    SCAN["running"] = True
    try:
        s = V.state; live = not os.getenv("COIN_DEMO")

        def screen(sym):  # cheap first pass: price data and indicators only
            try:
                df, src = analysts.bars(sym)
                if src == "demo" and live: return None  # never rank on fake data
                return sym, analysts.market_analyst(analysts.indicators(df))["score"]
            except Exception as e:
                V.log("scan_error", dict(symbol=sym, error=str(e)[:200])); return None
        with ThreadPoolExecutor(8) as ex:
            ranked = sorted((r for r in ex.map(screen, s["watchlist"]) if r), key=lambda r: -r[1])
        deep = list(dict.fromkeys(list(V.held()) + [r[0] for r in ranked[:s["scan_top"]]]))
        V.log("scan", dict(screened=len(ranked), deep=deep))
        for sym in deep:  # full board only for what you hold plus the best candidates
            try: _board(sym, allow_add=False)
            except Exception as e: V.log("scan_error", dict(symbol=sym, error=str(e)[:200]))
    finally:
        SCAN["running"] = False; V.update(last_scan=time.time())


def _scanner_tick():
    s=V.state
    if s['scan_enabled'] and s['mode']!='observe' and (s['scan_always'] or _market_open()) and time.time()-s['last_scan']>s['scan_minutes']*60:
        _scan_once()
    SCAN['error']=None


def _scanner_loop():
    while True:
        try:
            _scanner_tick()
        except Exception as e:
            SCAN["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        time.sleep(30)


@app.post("/api/scan", dependencies=[Depends(auth)])
def scan_now():
    if SCAN["running"]: fail("A scan is already running")
    threading.Thread(target=_scan_once, daemon=True).start(); return state()


@app.get("/api/pending")
def pending():
    seen, out = set(), []
    for (d,) in V.db.execute("select data from proposals order by rowid desc limit 200"):
        o = json.loads(d); p = o["proposal"]
        if o.get("executed") or o.get("declined") or p["side"] == "none" or not o.get("decision") or o["decision"]["status"] != "approved": continue
        if time.time() - o.get("ts", 0) > 6 * 3600 or p["symbol"] in seen: continue
        if evaluate(p, V.limits(), V.state, V.equity(), V.held())["status"] != "approved": continue  # no longer passes today's limits
        seen.add(p["symbol"]); out.append(o)
    return out[:10]


def _next_open():
    n = _ny_now(); d = n.replace(hour=9, minute=30, second=0, microsecond=0)
    if n >= d: d += timedelta(days=1)
    while d.weekday() >= 5: d += timedelta(days=1)
    return d.astimezone()  # shown in the computer's own time zone


def _scan_status():
    s = V.state
    if SCAN["running"]: return "Scanning now..."
    if SCAN["error"]: return "Scanner error: " + SCAN["error"]
    if not s["scan_enabled"]: return "Auto scan is off. Tick the box to turn it on."
    if s["mode"] == "observe": return "Paused: the mode is Just watch. Choose another mode."
    if not s["scan_always"] and not _market_open():
        return f"Waiting: US markets are closed. Opens {_next_open():%a %H:%M} your time. Tick 'also scan when closed' to scan now."
    due = (s["last_scan"] or 0) + s["scan_minutes"] * 60
    return "Scanning soon." if due <= time.time() else f"Next scan about {datetime.fromtimestamp(due):%H:%M}."


@app.get("/api/stocks")
def stocks(): return [dict(symbol=k, name=v) for k, v in sorted(COMPANIES.items(), key=lambda kv: kv[1].lower())]


@app.get("/api/chart/{sym}")
def chart(sym: str, rng: str = Query("1d", alias="range")):
    s = _sym(sym)
    if not s: fail("Enter a valid ticker")
    return analysts.chart_points(s, rng)


@app.get("/api/stream")
async def stream():
    """Server-sent events: tells the browser whenever anything changes, including automated trades and scans."""
    async def gen():
        last, idle = None, 0
        while True:
            cur = (V.version, SCAN["running"], PTICK[0])
            if cur != last:
                last, idle = cur, 0
                yield "data: " + json.dumps(dict(v=V.version)) + "\n\n"
            else:
                idle += 1
                if idle >= 30: idle = 0; yield ": ping\n\n"
            await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/proposals/{pid}/decline", dependencies=[Depends(auth)])
def decline(pid: str):
    row = V.db.execute("select data from proposals where id=?", (pid,)).fetchone()
    if not row: fail("Proposal not found")
    out = json.loads(row[0])
    if out.get("executed"): fail("Already executed")
    out["declined"] = True
    V.db.execute("update proposals set data=? where id=?", (json.dumps(out), pid)); V.db.commit(); V.version += 1
    if out["proposal"].get("obs_id"): L.feedback(out["proposal"]["obs_id"], -1)   # a negative signal for learning
    return dict(ok=True)


@app.post("/api/learning/train", dependencies=[Depends(auth)])
def learning_train():
    r = L.train(); V.version += 1
    return dict(train=r, **state())


@app.get('/api/learning/model', dependencies=[Depends(auth)])
def learning_model():
    if L.W is None: raise HTTPException(404, 'No trained model yet')
    with V.lock:
        payload=L.save_model(_model_path)
    return Response(json.dumps(payload,indent=2),media_type='application/json',
                    headers={'Content-Disposition':'attachment; filename=chair-model.json'})


@app.get("/api/learning/export")
def learning_export(good: int = 0):
    """Every decision of the main model as chat-style training examples (JSONL), optionally only those that worked out."""
    return Response(L.export_jsonl(chair.SYSTEM, bool(good)), media_type="application/x-ndjson",
                    headers={"Content-Disposition": "attachment; filename=coin-board-training-data.jsonl"})


def _fwd_price(sym, ts):
    df, src = analysts.bars(sym)
    if src == "demo": return None   # never learn from fake prices
    later = df[df.index >= pd.Timestamp(ts, unit="s") + pd.Timedelta(days=learn.HORIZON_DAYS)]
    return float(later["close"].iloc[0]) if len(later) else None


def _learning_tick():
    n=L.label_outcomes(_fwd_price);s=L.stats()
    new_observations=s['eligible']-s['n_trained']
    if s['observations']>=20 and (not s['trained'] or new_observations>=20 or s['labeled_since_training']>=10):
        result=L.train()
        if result['ok']:n+=1
    if n:V.version+=1


def _learn_loop():
    """Check matured outcomes every five minutes; retrain on new evidence, never on unverified self-labels."""
    while True:
        try:
            _learning_tick()
        except Exception:
            pass
        time.sleep(300)
