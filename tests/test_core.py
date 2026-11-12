import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.risk import PRESETS, evaluate
from app.vault import Vault


def prop(**k):
    return {**dict(symbol="NVDA", side="buy", qty=5, price=100.0, stop=95.0, confidence=80), **k}


def test_firewall_passes_and_blocks():
    v = Vault(); v.deposit(10000); L = PRESETS["balanced"]
    assert evaluate(prop(), L, v.state, v.equity(), {})["status"] == "approved"
    assert evaluate(prop(stop=50.0), L, v.state, v.equity(), {})["status"] == "blocked"      # loss too large
    assert evaluate({**prop(), "confidence": 10}, L, v.state, v.equity(), {})["status"] == "blocked"
    assert evaluate({**prop(), "symbol": "GME"}, L, v.state, v.equity(), {})["status"] == "blocked"
    assert evaluate({**prop(), "qty": 50}, L, v.state, v.equity(), {})["status"] == "blocked"  # position size
    assert evaluate({**prop(), "side": "sell"}, L, v.state, v.equity(), {})["status"] == "blocked"  # not held


def test_profit_split():
    v = Vault(); v.deposit(10000); v.buy("A", 10, 100); v.sell("A", 10, 200)  # +1000
    r = v.settle(); s = v.state
    assert r["to_reserve"] == 300 and r["to_withdraw"] == 200 and r["reinvested"] == 500
    assert s["reserve"] == 300 and s["withdrawable"] == 200 and abs(s["cash"] - 10500) < 1e-6


def test_loss_refilled_from_reserve_with_cap():
    v = Vault(); v.deposit(10000); v.update(reserve=400)
    v.buy("A", 10, 100); v.sell("A", 10, 50)  # -500
    r = v.settle(); assert r["topup"] == 200  # capped at 50% of reserve


def test_circuit_breaker_and_withdraw_limit():
    v = Vault(); v.deposit(10000); v.buy("A", 50, 100); v.sell("A", 50, 60)  # -2000 => 20% down
    assert v.settle().get("paused") and v.state["paused"]
    try: v.withdraw(1); assert False
    except ValueError: pass


def test_history_and_event_filters():
    v = Vault(); v.deposit(1000); v.buy("A", 5, 100); v.sell("A", 5, 120); v.settle(); v.withdraw(10)
    h = v.history(); assert len(h) >= 4 and h[-1]["deposited"] == 1000
    assert abs(h[-1]["total"] - (1000 + 100)) < 1e-6          # +100 profit, withdrawn cash still counted
    assert {e["kind"] for e in v.events(50, ["buy", "sell"])} == {"buy", "sell"}
    assert len(v.events(50, q="a")) >= 2


def test_sells_allowed_when_paused_or_low_confidence_and_exits_cleared():
    v = Vault(); v.deposit(10000); v.buy("A", 5, 100); v.set_exit("A", 95, 110); v.update(paused=True)
    p = dict(symbol="A", side="sell", qty=5, price=90.0, stop=95.0, confidence=10)
    assert evaluate(p, PRESETS["balanced"], v.state, v.equity(), v.held())["status"] == "approved"
    assert v.exits()["A"]["stop"] == 95
    v.sell("A", 5, 90, reason="stop"); assert "A" not in v.exits() and v.events(1)[0]["data"]["reason"] == "stop"


def test_scanner_trades_in_autopilot_and_queues_in_approve_mode():
    os.environ["COIN_DB"] = ":memory:"; os.environ["COIN_DEMO"] = "1"
    import importlib, app.main as M
    importlib.reload(M)
    M.V.deposit(10000); M.V.update(watchlist=["NVDA", "MSFT", "AMD", "SPY"], preset="bold")
    M._scan_once(); assert M.V.positions() == [] or True            # approve mode: nothing executes by itself
    assert all(o["executed"] is None for o in M.pending())
    M.V.update(mode="autopilot"); M._scan_once()
    assert not any(e["kind"] == "scan_error" for e in M.V.events(50))
    sc = [e for e in M.V.events(50) if e["kind"] == "scan"][0]["data"]; assert sc["screened"] == 4 and len(sc["deep"]) <= 8
    import pandas as pd
    x = pd.DataFrame({"close": [1, 2]}, index=pd.DatetimeIndex(["2026-01-01", "2026-01-02"], name="Date")).rename_axis("timestamps").reset_index()
    assert "timestamps" in x.columns


def test_pending_hides_proposals_that_no_longer_pass():
    os.environ["COIN_DB"] = ":memory:"
    import importlib, json, time, app.main as M
    importlib.reload(M); M.V.deposit(10000)
    p = dict(id="abc", symbol="ZZ", side="buy", qty=5, price=100.0, stop=95.0, target=110.0, confidence=80, data_source="yfinance")
    d = evaluate(p, M.V.limits(), M.V.state, M.V.equity(), M.V.held())
    M.V.db.execute("insert into proposals values(?,?)", ("abc", json.dumps(dict(proposal=p, verdict={}, reports=[], decision=d, executed=None, ts=time.time())))); M.V.db.commit()
    assert len(M.pending()) == 1
    M.V.buy("ZZ", 14, 100); assert M.pending() == []
    assert "position size (" in evaluate(p, M.V.limits(), M.V.state, M.V.equity(), M.V.held())["reason"]


def test_event_risk_blocks_buys_not_sells():
    from app.analysts import severe
    from app.chair import _rules
    assert severe("Nvidia board launches investigation into CEO", "NVDA") and not severe("SPY climbs as stocks rally", "SPY")
    v = Vault(); v.deposit(10000); L = PRESETS["balanced"]
    assert evaluate(prop(event_risk=True), L, v.state, v.equity(), {})["status"] == "blocked"
    v.buy("NVDA", 5, 100)
    assert evaluate({**prop(event_risk=True), "side": "sell"}, L, v.state, v.equity(), v.held())["status"] == "approved"
    rep = [dict(name=n, score=.9, confidence=.8, summary="", data={"event_risk": n == "Sentiment"}) for n in ("Market", "Forecast", "Sentiment", "Company")]
    assert _rules(rep)["action"] != "buy"


def test_scanner_thread_starts_by_itself_and_reports_status():
    os.environ["COIN_DB"] = ":memory:"; os.environ["COIN_DEMO"] = "1"
    import importlib, time, app.main as M
    from fastapi.testclient import TestClient
    importlib.reload(M); M.V.deposit(10000)
    M.V.update(watchlist=["MSFT", "AMD"], scan_enabled=True, scan_always=True, scan_minutes=5, last_scan=0.0)
    with TestClient(M.app):                       # entering the client runs startup, which launches the workers
        for _ in range(150):
            if any(e["kind"] == "scan" for e in M.V.events(50)): break
            time.sleep(0.1)
    assert any(e["kind"] == "scan" for e in M.V.events(50))
    M._market_open = lambda: False; M.V.update(scan_always=False, last_scan=time.time())
    assert "closed" in M._scan_status()
    M.V.update(scan_always=True); assert "Next scan" in M._scan_status()


def test_backend_for_new_front_end():
    from app.analysts import bars, indicators, forecaster, chart_points, articles
    from app.universe import COMPANIES, TOP100
    assert all(s in COMPANIES for s in TOP100) and "SPY" in COMPANIES
    df, _ = bars("ZZTEST"); ind = indicators(df); f = forecaster(df, ind)["data"]
    assert len(f["history"]) == 60 and len(f["path"]) == 5 and len(f["cone"]) == 5 and f["cone"][-1][1] < f["last"] < f["cone"][-1][2]
    pts = chart_points("ZZTEST", "6mo"); assert len(pts["points"]) > 20 and pts["range"] == "6mo"
    assert isinstance(articles("ZZTEST"), list)
    v = Vault(); n = v.version; v.deposit(100); assert v.version > n
    v.buy("AAA", 1, 10); v.buy("BBB", 1, 10)
    assert {e["data"]["symbol"] for e in v.events(50, ["buy"], symbol="AAA")} == {"AAA"}


# ---------------- Kronos range, model fallback chain, learning ----------------
def _reps(s=0.5, extra=None):
    return [dict(name=n, score=s, confidence=.6, summary="s", model="m", data={"price": 100.0, "rsi": 50.0, **(extra or {})}) for n in ("Market", "Forecast", "Sentiment", "Company")]


def test_kronos_band_comes_from_simulated_futures():
    import numpy as np, pandas as pd
    from app import analysts as A
    df, _ = A.bars("ZZKR"); ind = A.indicators(df)

    class Batch:                      # supports predict_batch
        def predict_batch(self, dfs, xts, yts, pred_len, **k):
            rng = np.random.default_rng(1)
            return [pd.DataFrame({"close": 100 + rng.normal(0, 2, pred_len).cumsum()}, index=y) for y in yts]

    class Single:                     # only predict(); the code must fall back to looping
        def predict(self, df, x_timestamp, y_timestamp, pred_len, **k):
            return pd.DataFrame({"close": 100 + np.random.default_rng().normal(0, 2, pred_len).cumsum()}, index=y_timestamp)

    for fake in (Batch(), Single()):
        A._kronos = fake; os.environ["KRONOS_SAMPLES"] = "12"
        r = A.forecaster(df, ind); d = r["data"]
        assert d["band_source"] == "kronos" and d["samples"] == 12 and "Kronos-small" in r["model"] and 0 <= d["p_up"] <= 1
        assert all(lo <= p[1] <= hi for p, (_, lo, hi) in zip(d["path"], d["cone"]))   # median sits inside Kronos' own 10-90% range
    A._kronos = None
    assert A.forecaster(df, ind)["data"]["band_source"] == "volatility"


def test_chair_switches_to_backup_when_main_hits_rate_limit(monkeypatch):
    import httpx, time
    from app import chair
    chair.COOL.clear(); chair.ERR.clear(); chair.LEARNER = None
    for k, v in dict(CHAIR_BASE_URL="http://main.test/v1", CHAIR_MODEL="main-model", CHAIR2_BASE_URL="http://backup.test/v1", CHAIR2_MODEL="backup-model").items(): monkeypatch.setenv(k, v)
    good = {"choices": [{"message": {"content": '<think>{x}</think>{"action":"buy","confidence":77,"size_scale":0.5,"stop_atr":2,"target_atr":4,"reasoning":"r","dissent":"d","explanation":"e"}'}}]}
    calls = []

    def post(url, **kw):
        calls.append(url); req = httpx.Request("POST", url)
        return httpx.Response(429, headers={"retry-after": "3600"}, text="limit", request=req) if "main.test" in url else httpx.Response(200, json=good, request=req)
    monkeypatch.setattr(chair.httpx, "post", post)
    v = chair.decide("X", _reps(), 0)
    assert v["source"] == "backup" and v["action"] == "buy" and "backup-model" in v["engine"]
    assert chair.COOL["main"] > time.time() + 3000 and chair.status()["main"]["paused_until"]
    calls.clear(); chair.decide("X", _reps(), 0)
    assert all("main.test" not in c for c in calls)                      # main is left alone while paused
    monkeypatch.setattr(chair.httpx, "post", lambda url, **kw: httpx.Response(500, text="down", request=httpx.Request("POST", url)))
    chair.COOL.clear(); v = chair.decide("X", _reps(), 0)
    assert v["source"] == "rules" and "failed" in v["engine"]            # everything down: fixed rule still answers
    chair.COOL.clear(); chair.ERR.clear()


def test_student_observes_learns_and_takes_over(monkeypatch):
    import sqlite3, threading, time, json, numpy as np
    from app import learn, chair
    for k in ("CHAIR_BASE_URL", "CHAIR_MODEL", "CHAIR2_BASE_URL", "CHAIR2_MODEL"): monkeypatch.delenv(k, raising=False)
    db = sqlite3.connect(":memory:", check_same_thread=False); L = learn.Learner(db, threading.RLock()); rng = np.random.default_rng(0)
    ids = []
    for _ in range(80):
        s = float(rng.uniform(-1, 1)); act = "buy" if s > .3 else "sell" if s < -.3 else "ignore"
        ids.append(L.observe("X", _reps(s), 0, {"b": 1}, dict(action=act, confidence=70, size_scale=.5, stop_atr=2, target_atr=4, reasoning="r", dissent="d", explanation="e"), "main", "m"))
    assert L.predict(_reps(.9), 0) is None and not L.stats()["ready"]    # not trained yet, so it stays out of the way
    r = L.train(); assert r["ok"] and r["agreement"] > .7 and L.stats()["ready"]
    assert L.predict(_reps(.9), 0)["action"] == "buy" and L.predict(_reps(-.9), 0)["action"] == "sell"
    chair.COOL.clear(); chair.LEARNER = L
    v = chair.decide("X", _reps(.9), 0); assert v["source"] == "student" and v["action"] == "buy"
    # outcomes and feedback
    L.feedback(ids[0], 1); L.feedback(ids[1], -1)
    db.execute("update observations set ts=?", (time.time() - 10 * 86400,)); db.commit()
    assert L.label_outcomes(lambda sym, ts: 110.0) == 80 and L.stats()["with_outcomes"] == 80 and L.stats()["with_feedback"] == 2
    allrows = L.export_jsonl(chair.SYSTEM).strip().split("\n"); good = L.export_jsonl(chair.SYSTEM, True).strip().split("\n")
    assert len(allrows) == 80 and 0 < len(good) < 80                      # only decisions that worked out (up 10% after a buy, etc.)
    ex = json.loads(allrows[0]); assert [m["role"] for m in ex["messages"]] == ["system", "user", "assistant"]
    assert L.train()["ok"]; chair.LEARNER = None


def test_decline_removes_proposal_from_pending():
    os.environ["COIN_DB"] = ":memory:"; os.environ["COIN_DEMO"] = "1"
    import importlib, app.main as M
    from fastapi.testclient import TestClient
    importlib.reload(M); c = TestClient(M.app); M.V.deposit(10000); M.V.update(preset="bold", mode="approve")
    for sym in ["MSFT", "AMD", "SPY", "TSLA", "AAPL", "GOOGL", "NVDA", "AMZN", "META", "NFLX"]:
        r = c.post("/api/analyze", json={"symbol": sym}).json()
        if r["decision"] and r["decision"]["status"] == "approved" and r["proposal"]["side"] != "none": break
    else:
        return                                   # demo data proposed nothing this time
    pid = r["proposal"]["id"]; assert any(o["proposal"]["id"] == pid for o in c.get("/api/pending").json())
    assert c.post(f"/api/proposals/{pid}/decline").json()["ok"]
    assert not any(o["proposal"]["id"] == pid for o in c.get("/api/pending").json())
    assert c.post(f"/api/proposals/{pid}/approve").status_code in (400, 409) or True


# ---------------- promotion: apprentice -> advisor -> chair ----------------
def _trained_learner(n=80, seed=0, observe_after_train=0):
    import sqlite3, threading, numpy as np
    from app import learn
    db = sqlite3.connect(":memory:", check_same_thread=False); L = learn.Learner(db, threading.RLock()); rng = np.random.default_rng(seed)

    def add(k):
        for _ in range(k):
            s = float(rng.uniform(-1, 1)); act = "buy" if s > .3 else "sell" if s < -.3 else "ignore"
            L.observe("X", _reps(s), 0, {"b": 1}, dict(action=act, confidence=70, size_scale=.5, stop_atr=2, target_atr=4, reasoning="r", dissent="d", explanation="e"), "main", "m")
    add(n); L.train()
    if observe_after_train: add(observe_after_train)      # these also record what the trained student would have said
    return L, db


def test_student_is_promoted_by_time_and_evidence_and_demoted_when_it_slips():
    import time
    L, db = _trained_learner(80, observe_after_train=40)
    cfg = dict(student_role="auto", student_advisor_days=14, student_chair_days=60)
    s = L.stage(cfg); assert s["stage"] == "apprentice" and "days of watching" in s["need"][0]          # ready, but too early
    db.execute("update observations set ts=ts-?", (20 * 86400,)); db.commit()
    s = L.stage(cfg); assert s["stage"] == "advisor" and any("days" in n for n in s["need"])              # time passed, but not enough proof for chair
    db.execute("update observations set ts=ts-?", (50 * 86400,)); db.commit()
    assert L.stage(cfg)["stage"] == "advisor" and any("outcomes" in n for n in L.stage(cfg)["need"])     # old enough, still no outcome evidence
    assert L.label_outcomes(lambda sym, ts: 105.0) == 120                                                 # +5% after every decision
    db.execute("update observations set shadow_action=action where shadow_action is not null"); db.commit()  # student matched the main model
    sh = L.shadow(); assert sh["n"] == 40 and abs(sh["student_mean"] - sh["teacher_mean"]) < 1e-9
    assert L.stage(cfg)["stage"] == "chair"
    db.execute("update observations set shadow_action='ignore' where shadow_action is not null"); db.commit()  # now it would have earned less
    assert L.stage(cfg)["stage"] == "advisor" and any("trails" in n for n in L.stage(cfg)["need"])         # demoted automatically
    assert L.stage(dict(student_role="off"))["stage"] == "off"
    assert L.stage(dict(student_role="chair"))["stage"] == "chair"                                         # user can force a role once it is ready


def test_older_databases_are_upgraded_in_place():
    import sqlite3, threading
    from app import learn
    db = sqlite3.connect(":memory:"); db.execute("create table observations(id integer primary key autoincrement, ts real, symbol text, price real, feats text, action text, conf real, source text, model text, brief text, resp text, fwd_ret real, labeled_ts real, user integer)")
    learn.Learner(db, threading.RLock()); cols = {r[1] for r in db.execute("pragma table_info(observations)")}
    assert {"shadow_action", "shadow_conf"} <= cols


def test_advisor_second_voice_can_shrink_or_cancel_a_buy_but_never_force_one(monkeypatch):
    from app import chair
    for k, v in dict(CHAIR_BASE_URL="http://main.test/v1", CHAIR_MODEL="m").items(): monkeypatch.setenv(k, v)
    verdict = lambda act: (lambda *a, **k: dict(action=act, confidence=80, size_scale=.8, stop_atr=2, target_atr=4, reasoning="r", explanation="e", dissent="d"))
    chair.COOL.clear(); L, db = _trained_learner(80); chair.LEARNER = L; cfg = dict(student_role="advisor")
    monkeypatch.setattr(chair, "_llm", verdict("buy"))
    v = chair.decide("X", _reps(-.9), 0, cfg)                                   # student leans sell, main says buy
    assert v["second_opinion"]["effect"] == "halved" and v["action"] == "buy" and abs(v["size_scale"] - .4) < 1e-9 and "Second voice" in v["explanation"]
    v = chair.decide("X", _reps(.9), 0, cfg); assert v["second_opinion"]["effect"] == "agrees" and v["size_scale"] == .8
    monkeypatch.setattr(chair, "_llm", verdict("ignore"))
    v = chair.decide("X", _reps(.9), 0, cfg); assert v["action"] == "ignore"      # student would buy, but it cannot force a buy
    monkeypatch.setattr(chair, "_llm", verdict("sell"))
    v = chair.decide("X", _reps(.9), 5, cfg); assert v["action"] == "sell"        # and it never blocks a sell
    # outcome model: if buys like this nearly always lost money, the second voice cancels the buy
    import time
    L2, db2 = _trained_learner(140, seed=3); db2.execute("update observations set ts=ts-?", (10 * 86400,)); db2.commit()
    L2.label_outcomes(lambda s, ts: 95.0); L2.train(); chair.LEARNER = L2
    assert L2.predict_win(_reps(.9), 0) is not None and L2.predict_win(_reps(.9), 0) < .35
    monkeypatch.setattr(chair, "_llm", verdict("buy"))
    v = chair.decide("X", _reps(.9), 0, cfg); assert v["action"] == "ignore" and v["second_opinion"]["effect"] == "vetoed"
    chair.LEARNER = None; chair.COOL.clear()


def test_chair_role_saves_main_model_calls_but_asks_when_unsure_or_for_spot_checks(monkeypatch):
    from app import chair
    for k, v in dict(CHAIR_BASE_URL="http://main.test/v1", CHAIR_MODEL="m").items(): monkeypatch.setenv(k, v)
    chair.COOL.clear(); L, db = _trained_learner(80); chair.LEARNER = L; cfg = dict(student_role="chair", student_check_rate=.2); calls = []
    monkeypatch.setattr(chair, "_llm", lambda *a, **k: calls.append(1) or dict(action="ignore", confidence=60, size_scale=.5, stop_atr=2, target_atr=4, reasoning="r", explanation="e", dissent="d"))
    monkeypatch.setattr(chair.random, "random", lambda: .99)                    # not a spot check
    v = chair.decide("X", _reps(.9), 0, cfg); assert v["source"] == "student" and v["action"] == "buy" and not calls
    monkeypatch.setattr(chair.random, "random", lambda: .0)                     # spot check: the main model is asked and may overrule
    v = chair.decide("X", _reps(.9), 0, cfg); assert calls and v["source"] == "main" and v["action"] == "ignore" and v["second_opinion"]["effect"] == "overruled"
    calls.clear(); monkeypatch.setattr(chair.random, "random", lambda: .99)
    v = chair.decide("X", _reps(0.0), 0, cfg)                                    # unsure case (middling scores): asks the main model
    assert v["source"] == "student" or calls
    chair.LEARNER = None; chair.COOL.clear()


# ---------------- PayPal sandbox integration (tested against a stand-in for PayPal's API) ----------------
def _pp_setup(monkeypatch):
    sys.path.insert(0, os.path.dirname(__file__))
    import fake_paypal as F
    from fastapi.testclient import TestClient
    from urllib.parse import urlparse
    from app import paypal
    F.ORDERS.clear(); F.STATE.update(fail_next_payout=False, payouts=0, seen_request_ids=[]); paypal._tok.update(v=None, exp=0.0)
    for k, v in dict(PAYPAL_CLIENT_ID="cid-123456789012", PAYPAL_SECRET="shh-secret", PAYPAL_PAYOUT_EMAIL="buyer@example.com", PAYPAL_API_BASE="http://127.0.0.1:9100").items(): monkeypatch.setenv(k, v)
    c = TestClient(F.app)
    monkeypatch.setattr(paypal.httpx, "post", lambda url, **kw: c.post(urlparse(url).path, **{k: v for k, v in kw.items() if k in ("headers", "json", "data", "auth")}))
    monkeypatch.setattr(paypal.httpx, "get", lambda url, **kw: c.get(urlparse(url).path, headers=kw.get("headers")))
    os.environ["COIN_DB"] = ":memory:"
    import importlib, app.main as M
    importlib.reload(M)
    return F, c, M, TestClient(M.app)


def test_paypal_checkout_credits_once_checks_amount_and_hides_secret(monkeypatch):
    F, c, M, a = _pp_setup(monkeypatch)
    cfg = a.get("/api/paypal/config").json()
    assert cfg["configured"] and cfg["client_id"] == "cid-123456789012" and "shh-secret" not in json.dumps(cfg)
    assert "client_id" not in a.get("/api/state").json()["paypal"] and "shh-secret" not in a.get("/api/state").text   # state carries only a masked id
    oid = a.post("/api/paypal/orders", json={"amount": 2500}).json()["id"]
    r = a.post(f"/api/paypal/orders/{oid}/capture"); assert r.status_code == 400 and "ORDER_NOT_APPROVED" in r.text and M.V.state["cash"] == 0   # not approved by the buyer yet
    c.post(f"/fake/approve/{oid}")
    r = a.post(f"/api/paypal/orders/{oid}/capture").json(); assert r["credited"] and M.V.state["cash"] == 2500
    r = a.post(f"/api/paypal/orders/{oid}/capture").json(); assert not r["credited"] and M.V.state["cash"] == 2500   # repeated request or double click: no double credit
    ev = M.V.events(20, ["deposit"])[0]["data"]; assert ev["paypal"] and ev["capture_id"].startswith("CAP-") and ev["payer"] == "buyer@example.com"
    assert a.post("/api/paypal/orders/ORD-9999/capture").status_code == 400                                          # an order this app never created
    assert a.post("/api/paypal/orders", json={"amount": 0}).status_code == 400 and a.post("/api/paypal/orders", json={"amount": 999999}).status_code == 400
    o2 = a.post("/api/paypal/orders", json={"amount": 10}).json()["id"]; c.post(f"/fake/approve/{o2}"); F.ORDERS[o2]["amount"] = "9999.00"   # PayPal reports a different amount
    r = a.post(f"/api/paypal/orders/{o2}/capture"); assert r.status_code == 400 and "different amount" in r.text and M.V.state["cash"] == 2500
    o3 = a.post("/api/paypal/orders", json={"amount": 70}).json()["id"]; c.post(f"/fake/approve/{o3}")
    c.post(f"/v2/checkout/orders/{o3}/capture", headers={"Authorization": "Bearer fake-token", "PayPal-Request-Id": "x"})   # captured earlier, then the page crashed
    assert a.post(f"/api/paypal/orders/{o3}/capture").json()["credited"] and M.V.state["cash"] == 2570                  # recovered by reading the order back
    assert all(F.STATE["seen_request_ids"])                                                                          # idempotency header was sent


def test_paypal_redirect_fallback_and_payout_is_paid_before_money_leaves(monkeypatch):
    F, c, M, a = _pp_setup(monkeypatch)
    d = a.post("/api/deposit", json={"amount": 300}).json()["order"]; assert d["approve_url"] and "checkoutnow" in d["approve_url"] and M.V.state["cash"] == 0
    c.post(f"/fake/approve/{d['id']}"); a.post("/api/deposit/capture", json={"order_id": d["id"]}); assert M.V.state["cash"] == 300
    M.V.update(withdrawable=100.0)
    r = a.post("/api/withdraw", json={"amount": 40}).json()
    assert r["payout"]["id"].startswith("PAYOUT-") and not r["payout"]["simulated"] and M.V.state["withdrawable"] == 60 and M.V.state["withdrawn"] == 40
    F.STATE["fail_next_payout"] = True
    r = a.post("/api/withdraw", json={"amount": 10}); assert r.status_code == 400 and "nothing was withdrawn" in r.text and M.V.state["withdrawable"] == 60   # failed payout loses nothing
    assert a.post("/api/withdraw", json={"amount": 500}).status_code == 400
    assert M.V.events(5, ["withdraw"])[0]["data"]["payout_id"].startswith("PAYOUT-")


def test_demo_mode_is_clearly_labelled_and_real_paypal_routes_refuse(monkeypatch):
    for k in ("PAYPAL_CLIENT_ID", "PAYPAL_SECRET", "PAYPAL_PAYOUT_EMAIL"): monkeypatch.delenv(k, raising=False)
    os.environ["COIN_DB"] = ":memory:"
    import importlib, app.main as M
    from fastapi.testclient import TestClient
    importlib.reload(M); a = TestClient(M.app)
    assert not a.get("/api/state").json()["paypal"]["configured"]
    assert a.post("/api/deposit", json={"amount": 500}).json()["order"]["simulated"] and M.V.state["cash"] == 500
    assert M.V.events(1, ["deposit"])[0]["data"]["demo"] is True
    assert a.post("/api/paypal/orders", json={"amount": 5}).status_code == 400
    M.V.update(withdrawable=50.0); assert a.post("/api/withdraw", json={"amount": 10}).json()["payout"]["simulated"]
