"""Server-side ledger: trading cash, reserve, withdrawable profit, positions, event log (SQLite)."""
import json, sqlite3, threading, time
from .risk import PRESETS

DEFAULT = dict(deposited=0.0, cash=0.0, reserve=0.0, withdrawable=0.0, day_realized=0.0, peak=0.0, paused=False,
               mode="approve", preset="balanced", split=dict(reserve=30, reinvest=50, withdraw=20),
               topup_cap_pct=50, max_drawdown_pct=15, withdrawn=0.0, auto_close=False, close_time="16:15", last_close="", watchlist=["NVDA", "MSFT", "AAPL", "AMD", "GOOGL", "AMZN", "TSLA", "SPY"], scan_enabled=False, scan_minutes=30, last_scan=0.0, scan_top=8, scan_always=False, student_role="auto", student_advisor_days=14, student_chair_days=60, student_check_rate=0.2)


class Vault:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.version = 0  # bumps on every write so the live stream knows something changed
        self.db.executescript("""create table if not exists kv(k text primary key, v text);
        create table if not exists positions(symbol text primary key, qty real, avg real);
        create table if not exists events(ts real, kind text, data text);
        create table if not exists proposals(id text primary key, data text);
        create table if not exists exits(symbol text primary key, stop real, target real);
        create table if not exists paypal_orders(order_id text primary key, amount real, status text, created real, capture_id text, payer text);
        create table if not exists snapshots(ts real, cash real, reserve real, withdrawable real, pos real, deposited real, withdrawn real);""")
        if not self.db.execute("select 1 from kv where k='state'").fetchone():
            self._save(dict(DEFAULT))

    def _save(self, s):
        self.db.execute("insert or replace into kv values('state',?)", (json.dumps(s),)); self.db.commit(); self.version += 1

    @property
    def state(self):
        return {**DEFAULT, **json.loads(self.db.execute("select v from kv where k='state'").fetchone()[0])}

    def held(self):
        return {r[0]: r[1] for r in self.db.execute("select symbol,qty from positions")}

    def positions(self):
        return [dict(symbol=r[0], qty=r[1], avg=r[2]) for r in self.db.execute("select symbol,qty,avg from positions")]

    def equity(self):
        return self.state["cash"] + sum(p["qty"] * p["avg"] for p in self.positions())

    def limits(self):
        return PRESETS[self.state["preset"]]

    def log(self, kind, data):
        self.db.execute("insert into events values(?,?,?)", (time.time(), kind, json.dumps(data)))
        if kind != "board": self._snap()
        self.db.commit(); self.version += 1

    def _snap(self):
        s = self.state; pos = sum(p["qty"] * p["avg"] for p in self.positions())
        self.db.execute("insert into snapshots values(?,?,?,?,?,?,?)",
                        (time.time(), s["cash"], s["reserve"], s["withdrawable"], pos, s["deposited"], s["withdrawn"]))

    def history(self):
        return [dict(ts=r[0], cash=r[1], reserve=r[2], withdrawable=r[3], positions=r[4], deposited=r[5],
                     total=r[1] + r[2] + r[3] + r[4] + r[6]) for r in
                self.db.execute("select ts,cash,reserve,withdrawable,pos,deposited,withdrawn from snapshots order by ts")]

    def events(self, n=30, kinds=None, q=None, ascending=False, symbol=None):
        sql, args, w = "select ts,kind,data from events", [], []
        if kinds: w.append("kind in (%s)" % ",".join("?" * len(kinds))); args += kinds
        if q: w.append("upper(data) like ?"); args.append(f"%{q.upper()}%")
        if symbol: w.append("data like ?"); args.append('%"symbol": "' + symbol + '"%')
        if w: sql += " where " + " and ".join(w)
        sql += " order by ts %s limit ?" % ("asc" if ascending else "desc"); args.append(n)
        return [dict(ts=r[0], kind=r[1], data=json.loads(r[2])) for r in self.db.execute(sql, args)]

    def update(self, **kw):
        with self.lock:
            s = self.state; s.update(kw); self._save(s)

    def deposit(self, amount, ref="manual", **extra):
        with self.lock:
            s = self.state; s["cash"] += amount; s["deposited"] += amount
            s["peak"] = max(s["peak"], self.equity() + amount)
            self._save(s); self.log("deposit", dict(amount=amount, ref=ref, **extra))

    def withdraw(self, amount, **extra):
        with self.lock:
            s = self.state
            if amount <= 0 or amount > s["withdrawable"] + 1e-9:
                raise ValueError("Amount exceeds withdrawable profit")
            s["withdrawable"] -= amount; s["withdrawn"] += amount; self._save(s); self.log("withdraw", dict(amount=amount, **extra))

    def buy(self, symbol, qty, price):
        with self.lock:
            s = self.state; cost = qty * price
            if cost > s["cash"] + 1e-9: raise ValueError("Not enough cash")
            row = self.db.execute("select qty,avg from positions where symbol=?", (symbol,)).fetchone()
            q0, a0 = row if row else (0, 0)
            self.db.execute("insert or replace into positions values(?,?,?)", (symbol, q0 + qty, (q0 * a0 + cost) / (q0 + qty)))
            s["cash"] -= cost; self._save(s); self.log("buy", dict(symbol=symbol, qty=qty, price=price))

    def set_exit(self, symbol, stop, target):
        self.db.execute("insert or replace into exits values(?,?,?)", (symbol, stop, target)); self.db.commit(); self.version += 1

    def exits(self):
        return {r[0]: dict(stop=r[1], target=r[2]) for r in self.db.execute("select symbol,stop,target from exits")}

    def sell(self, symbol, qty, price, reason="board"):
        with self.lock:
            row = self.db.execute("select qty,avg from positions where symbol=?", (symbol,)).fetchone()
            if not row or qty > row[0] + 1e-9: raise ValueError("Not enough shares")
            s = self.state; pnl = (price - row[1]) * qty; left = row[0] - qty
            if left <= 1e-9:
                self.db.execute("delete from positions where symbol=?", (symbol,)); self.db.execute("delete from exits where symbol=?", (symbol,))
            else: self.db.execute("update positions set qty=? where symbol=?", (left, symbol))
            s["cash"] += qty * price; s["day_realized"] += pnl; self._save(s)
            self.log("sell", dict(symbol=symbol, qty=qty, price=price, pnl=round(pnl, 2), reason=reason))

    def settle(self):
        """Close the day: split profit into reserve / withdrawable / reinvested, or refill from reserve after a loss."""
        with self.lock:
            s = self.state; pnl = s["day_realized"]; sp = s["split"]
            out = dict(pnl=round(pnl, 2), to_reserve=0, to_withdraw=0, topup=0)
            if pnl > 0:
                r = min(pnl * sp["reserve"] / 100, s["cash"])
                w = min(pnl * sp["withdraw"] / 100, max(0, s["cash"] - r))
                s["cash"] -= r + w; s["reserve"] += r; s["withdrawable"] += w
                out.update(to_reserve=round(r, 2), to_withdraw=round(w, 2), reinvested=round(pnl - r - w, 2))
            elif pnl < 0:
                t = min(-pnl, s["reserve"] * s["topup_cap_pct"] / 100)
                s["reserve"] -= t; s["cash"] += t; out["topup"] = round(t, 2)
            s["day_realized"] = 0.0
            eq = s["cash"] + sum(p["qty"] * p["avg"] for p in self.positions())
            s["peak"] = max(s["peak"], eq)
            if s["peak"] > 0 and eq < s["peak"] * (1 - s["max_drawdown_pct"] / 100):
                s["paused"] = True; out["paused"] = True
            self._save(s); self.log("settle", out); return out

    # ---- PayPal orders: only orders this app created can be credited, and each is credited once ----
    def paypal_new(self, order_id, amount):
        with self.lock:
            self.db.execute("insert or replace into paypal_orders values(?,?,?,?,null,null)", (order_id, amount, "created", time.time())); self.db.commit()

    def paypal_get(self, order_id):
        r = self.db.execute("select order_id,amount,status,capture_id from paypal_orders where order_id=?", (order_id,)).fetchone()
        return dict(order_id=r[0], amount=r[1], status=r[2], capture_id=r[3]) if r else None

    def paypal_credit(self, order_id, capture_id, payer):
        with self.lock:
            row = self.paypal_get(order_id)
            if not row or row["status"] == "credited": return False
            self.db.execute("update paypal_orders set status='credited', capture_id=?, payer=? where order_id=?", (capture_id, payer, order_id))
            self.deposit(row["amount"], ref=order_id, capture_id=capture_id, payer=payer, paypal=True)
            return True
