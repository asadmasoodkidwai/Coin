"""Observe the main Chair, learn from it (and from real outcomes and from you), and earn a bigger role over time.

The "student" is a small model (numpy only) with two parts:
  * an imitator: softmax regression that copies the main Chair's buy / sell / ignore calls from the analysts' scores;
  * an outcome model: logistic regression that estimates how often a buy like this one actually worked out.
Decisions that later made money weigh more in training, ones that lost weigh less, and trades you approve or decline count as feedback.

Roles (stage): apprentice -> advisor -> chair. Time alone never promotes it. It also needs evidence: enough observed decisions, agreement
with the main Chair, and, for the top role, results on past decisions (shadow comparison) at least as good as the main model's."""
import json, time, os
from pathlib import Path
import numpy as np

CLASSES = ["buy", "sell", "ignore"]
FEATS = ["Market score", "Market confidence", "Forecast score", "Forecast confidence", "Sentiment score", "Sentiment confidence",
         "Company score", "Company confidence", "Severe news flagged", "You already own it", "Kronos chance of rising", "Forecast move", "RSI stretch"]
MIN_OBS, MIN_AGREE, HORIZON_DAYS = 40, 0.6, 7
MIN_WIN_N = 25          # labelled buys needed before the outcome model is allowed to speak
WINDOW, MIN_SHADOW = 40, 30   # compare the student with the main model over the latest 40 decisions that have outcomes, need at least 30
TOL = 0.002             # the student may trail the main model by this much per decision and still be promoted


def features(reports, held):
    r = {x["name"]: x for x in reports}
    g = lambda n, k: float(r.get(n, {}).get(k, 0.0) or 0.0)
    fd, md = r.get("Forecast", {}).get("data", {}), r.get("Market", {}).get("data", {})
    return [g("Market", "score"), g("Market", "confidence"), g("Forecast", "score"), g("Forecast", "confidence"), g("Sentiment", "score"),
            g("Sentiment", "confidence"), g("Company", "score"), g("Company", "confidence"),
            1.0 if r.get("Sentiment", {}).get("data", {}).get("event_risk") else 0.0, 1.0 if held else 0.0,
            float(fd.get("p_up", 0.5)) - 0.5, max(-0.1, min(0.1, float(fd.get("expected_return", 0.0)))) * 10, (float(md.get("rsi", 50)) - 50) / 50]


def _softmax(z):
    z = z - z.max(axis=1, keepdims=True); e = np.exp(z); return e / e.sum(axis=1, keepdims=True)


def _fit(X, y, w, l2=0.01, lr=0.5, epochs=600):
    n, d = X.shape; Xb = np.hstack([X, np.ones((n, 1))]); W = np.zeros((d + 1, 3)); Y = np.eye(3)[y]
    for _ in range(epochs):
        G = Xb.T @ ((_softmax(Xb @ W) - Y) * w[:, None]) / w.sum() + l2 * np.vstack([W[:-1], np.zeros((1, 3))])
        W -= lr * G
    return W


def _fit_logit(X, y, l2=0.05, lr=0.5, epochs=500):
    n, d = X.shape; Xb = np.hstack([X, np.ones((n, 1))]); w = np.zeros(d + 1)
    for _ in range(epochs):
        p = 1 / (1 + np.exp(-(Xb @ w))); w -= lr * (Xb.T @ (p - y) / n + l2 * np.append(w[:-1], 0))
    return w


def _good(action, fwd):
    return fwd > 0.01 if action == "buy" else fwd < -0.01 if action == "sell" else abs(fwd) < 0.03


def _score(action, fwd, held):
    """Paper result of one decision: buying earns the move, selling something you hold avoids the move, ignoring earns nothing."""
    return fwd if action == "buy" else (-fwd if action == "sell" and held else 0.0)


class Learner:
    def __init__(self, db, lock):
        self.db, self.lock = db, lock
        self.db.executescript("""create table if not exists observations(id integer primary key autoincrement, ts real, symbol text, price real,
            feats text, action text, conf real, source text, model text, brief text, resp text, fwd_ret real, labeled_ts real, user integer);
            create table if not exists student(id integer primary key, data text);""")
        cols = {r[1] for r in self.db.execute("pragma table_info(observations)")}   # upgrade older databases in place
        for c, t in (("shadow_action", "text"), ("shadow_conf", "real")):
            if c not in cols: self.db.execute(f"alter table observations add column {c} {t}")
        self.db.commit()
        self.W, self.Wwin, self.meta = None, None, {}
        row = self.db.execute("select data from student where id=1").fetchone()
        if row:
            d = json.loads(row[0]); self.W = np.array(d.pop("W")); ww = d.pop("Wwin", None); self.Wwin = np.array(ww) if ww else None; self.meta = d

    # ---------- observing ----------
    def _probs(self, reports, held):
        if self.W is None: return None
        x = np.array(features(reports, held)); return _softmax((np.append(x, 1.0) @ self.W)[None])[0]

    def observe(self, sym, reports, held, brief, v, source, model):
        price = float(next((r["data"].get("price") for r in reports if r["name"] == "Market"), 0) or 0)
        resp = {k: v.get(k) for k in ("action", "confidence", "size_scale", "stop_atr", "target_atr", "reasoning", "dissent", "explanation")}
        p = self._probs(reports, held)   # what the student would have said at this moment (shadow mode), to compare fairly later
        sa = None if p is None else (CLASSES[int(p.argmax())] if p.max() >= 0.5 else "ignore"); sc = None if p is None else float(p.max()) * 100
        with self.lock:
            cur = self.db.execute("insert into observations(ts,symbol,price,feats,action,conf,source,model,brief,resp,shadow_action,shadow_conf) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (time.time(), sym, price, json.dumps(features(reports, held)), v["action"], v.get("confidence", 0), source, model, json.dumps(brief), json.dumps(resp), sa, sc))
            self.db.commit(); return cur.lastrowid

    def feedback(self, obs_id, val):
        with self.lock: self.db.execute("update observations set user=? where id=?", (val, obs_id)); self.db.commit()

    def label_outcomes(self, price_after):
        """Fill in what happened HORIZON_DAYS later. price_after(symbol, ts) returns a price or None."""
        with self.lock:
            rows = self.db.execute("select id,symbol,ts,price from observations where fwd_ret is null and price>0 and ts<? limit 200", (time.time() - HORIZON_DAYS * 86400,)).fetchall()
        n = 0
        for i, sym, ts, price in rows:
            p = price_after(sym, ts)
            if p:
                with self.lock: self.db.execute("update observations set fwd_ret=?, labeled_ts=? where id=?", (p / price - 1, time.time(), i)); self.db.commit()
                n += 1
        return n

    # ---------- training ----------
    def _data(self):
        raw = self.db.execute("select feats,action,fwd_ret,user,source from observations order by ts").fetchall()
        rows=[]
        for feats,action,fwd,user,source in raw:
            if source in ('student','rules'):
                if fwd is None: continue  # do not teach the model its own unverified predictions
                held=json.loads(feats)[9]>0
                action='buy' if fwd>.01 else 'sell' if fwd<-.01 and held else 'ignore'
            rows.append((feats,action,fwd,user))
        X = np.array([json.loads(r[0]) for r in rows]); y = np.array([CLASSES.index(r[1]) for r in rows]); w = np.ones(len(rows))
        for i, (_, a, f, u) in enumerate(rows):
            if f is not None:
                r = f if a == "buy" else -f if a == "sell" else 0.0
                w[i] = float(np.clip(1 + 10 * r, 0.2, 2.0)) if a != "ignore" else (0.8 if abs(f) > 0.05 else 1.0)
            if u == 1: w[i] *= 1.5
            elif u == -1: w[i] *= 0.5
        return X, y, w

    def train(self):
        with self.lock:
            X, y, w = self._data(); n = len(y)
            if n < 10: return dict(ok=False, reason=f"Need at least 10 observed decisions, have {n}.")
            agree = None
            if n >= 30:
                cut = int(n * 0.8); W = _fit(X[:cut], y[:cut], w[:cut])
                agree = float(((np.hstack([X[cut:], np.ones((n - cut, 1))]) @ W).argmax(1) == y[cut:]).mean())
            self.W = _fit(X, y, w)
            buys = self.db.execute("select feats,fwd_ret from observations where action='buy' and fwd_ret is not null").fetchall()
            self.Wwin = _fit_logit(np.array([json.loads(r[0]) for r in buys]), np.array([1.0 if r[1] > 0.01 else 0.0 for r in buys])) if len(buys) >= 15 else None
            self.meta = dict(n_obs=n, agreement=None if self.meta.get('historical') else agree, trained_at=time.time(), n_win=len(buys),
                             historical=self.meta.get('historical', False), validation=self.meta.get('validation'),
                             n_labeled=self.db.execute('select count(*) from observations where fwd_ret is not null').fetchone()[0],
                             n_seen=self.db.execute('select count(*) from observations').fetchone()[0])
            self.db.execute("insert or replace into student values(1,?)", (json.dumps(dict(W=self.W.tolist(), Wwin=None if self.Wwin is None else self.Wwin.tolist(), **self.meta)),)); self.db.commit()
            self.save_model(os.getenv('COIN_MODEL', 'chair-model.json'))
            return dict(ok=True, **self.meta)

    def save_model(self, path):
        """Portable, versioned parameters; compatible with any instance using this feature schema."""
        if self.W is None: raise ValueError('No trained model')
        payload = dict(self.meta, schema_version=1, features=FEATS, classes=CLASSES,
                       W=self.W.tolist(), Wwin=None if self.Wwin is None else self.Wwin.tolist())
        target=Path(path); target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_suffix('.tmp');temp.write_text(json.dumps(payload,indent=2));temp.replace(target)
        return payload

    def load_model(self, path):
        d=json.loads(Path(path).read_text())
        if d.get('schema_version')!=1 or d.get('features')!=FEATS or d.get('classes')!=CLASSES:
            raise ValueError('Incompatible Chair model schema')
        W=np.asarray(d['W'],dtype=float)
        ww=d.get('Wwin'); Wwin=None if ww is None else np.asarray(ww,dtype=float)
        if W.shape!=(len(FEATS)+1,3) or not np.isfinite(W).all(): raise ValueError('Invalid model weights')
        if Wwin is not None and (Wwin.shape!=(len(FEATS)+1,) or not np.isfinite(Wwin).all()): raise ValueError('Invalid outcome weights')
        with self.lock:
            self.W,self.Wwin=W,Wwin
            self.meta={k:v for k,v in d.items() if k not in ('W','Wwin','features','classes','schema_version')}
            self.db.execute('insert or replace into student values(1,?)',(json.dumps(dict(self.meta,W=W.tolist(),Wwin=ww)),));self.db.commit()
        return self.meta

    # ---------- using it ----------
    def _ready(self):
        m = self.meta
        return self.W is not None and m.get("n_obs", 0) >= MIN_OBS and (m.get('historical') or m.get("agreement") is None or m["agreement"] >= MIN_AGREE)

    def predict(self, reports, held):
        if not self._ready(): return None
        x = np.array(features(reports, held)); p = self._probs(reports, held); k = int(p.argmax())
        contrib = x * (self.W[:-1, k] - self.W[:-1].mean(axis=1)); top = np.argsort(-np.abs(contrib))[:3]
        drivers = ", ".join(f"{FEATS[i]} ({'for' if contrib[i] > 0 else 'against'})" for i in top)
        act = CLASSES[k] if p[k] >= 0.5 else "ignore"; n, ag = self.meta["n_obs"], self.meta.get("agreement")
        historical=self.meta.get('historical',False)
        origin='historical outcomes and observed decisions' if historical else "earlier decisions of the main Chair"
        return dict(action=act, confidence=min(85, round(float(p.max()) * 100)), size_scale=0.5, stop_atr=2, target_atr=4, probs={c: float(q) for c, q in zip(CLASSES, p)},
                    reasoning=f"The local student model leans {act} ({p.max() * 100:.0f}%). Main drivers: {drivers}.",
                    dissent="Historical price patterns may fail in new regimes; this model cannot interpret new headlines.",
                    explanation=("A small model trained on " + str(n) + " examples of " + origin + " made this call. "
                                 f"It gives buy {p[0] * 100:.0f}%, sell {p[1] * 100:.0f}%, ignore {p[2] * 100:.0f}%.\n\n"
                                 f"The strongest influences were: {drivers}. Probabilities are uncalibrated model scores. It cannot read news headlines or reason about new situations, so the safety checks and the severe-news gate matter even more when it decides."),
                    engine=f"Local outcome model ({n} examples)" if historical else f"Student model (learned from {n} decisions" + (f", agrees with the main Chair {ag * 100:.0f}% of the time)" if ag is not None else ")"))

    def predict_win(self, reports, held):
        """Chance that a buy like this one made money in the past, or None until there are enough labelled buys."""
        if self.Wwin is None or self.meta.get("n_win", 0) < MIN_WIN_N: return None
        x = np.append(np.array(features(reports, held)), 1.0); return float(1 / (1 + np.exp(-(x @ self.Wwin))))

    # ---------- evidence and roles ----------
    def shadow(self):
        rows = self.db.execute("select action,shadow_action,fwd_ret,feats from observations where fwd_ret is not null and shadow_action is not null and source in ('main','backup') order by ts desc limit ?", (WINDOW,)).fetchall()
        if not rows: return dict(n=0, teacher_mean=0.0, student_mean=0.0)
        held = [json.loads(r[3])[9] > 0 for r in rows]
        return dict(n=len(rows), teacher_mean=float(np.mean([_score(r[0], r[2], h) for r, h in zip(rows, held)])), student_mean=float(np.mean([_score(r[1], r[2], h) for r, h in zip(rows, held)])))

    def stats(self):
        q = lambda s: self.db.execute(s).fetchone()[0]
        first = q("select min(ts) from observations where source!='historical'")
        return dict(observations=q("select count(*) from observations"), with_outcomes=q("select count(*) from observations where fwd_ret is not null"),
                    eligible=q("select count(*) from observations where source not in ('student','rules') or fwd_ret is not null"),
                    with_feedback=q("select count(*) from observations where user is not null"), trained=self.W is not None, n_trained=self.meta.get("n_obs", 0),
                    agreement=self.meta.get("agreement"), trained_at=self.meta.get("trained_at"), ready=self._ready(), needed=MIN_OBS,
                    age_days=(time.time() - first) / 86400 if first else 0.0, shadow=self.shadow(), n_win=self.meta.get("n_win", 0),
                    win_ready=self.Wwin is not None and self.meta.get("n_win", 0) >= MIN_WIN_N,
                    historical=self.meta.get('historical',False), validation=self.meta.get('validation'),
                    labeled_since_training=q('select count(*) from observations where fwd_ret is not null')-self.meta.get('n_labeled',0))

    def stage(self, cfg):
        """apprentice / advisor / chair / off, with what is still missing for the next step."""
        cfg = cfg or {}; role = cfg.get("student_role", "auto"); st = self.stats(); need = []
        adv, chd = float(cfg.get("student_advisor_days", 14)), float(cfg.get("student_chair_days", 60))
        if role == "off": return dict(stage="off", need=["Turned off in settings"])
        if not st["ready"]:
            if st["observations"] < MIN_OBS: need.append(f"{MIN_OBS - st['observations']} more observed decisions")
            if not st["trained"]: need.append("a first training run (automatic, or press Train)")
            elif st["agreement"] is not None and st["agreement"] < MIN_AGREE: need.append(f"{MIN_AGREE * 100:.0f}% agreement with the main model (now {st['agreement'] * 100:.0f}%)")
            return dict(stage="apprentice", need=need)
        if role in ("advisor", "chair"): return dict(stage=role, need=[], forced=True)
        if st["age_days"] < adv: return dict(stage="apprentice", need=[f"{adv - st['age_days']:.0f} more days of watching (second voice after {adv:.0f} days)"])
        sh = st["shadow"]
        if st["age_days"] < chd: need.append(f"{chd - st['age_days']:.0f} more days (decider after {chd:.0f} days)")
        if sh["n"] < MIN_SHADOW: need.append(f"{MIN_SHADOW - sh['n']} more decisions with known outcomes to compare against the main model")
        elif sh["student_mean"] < sh["teacher_mean"] - TOL: need.append("it must do at least as well as the main model on past decisions (it currently trails)")
        return dict(stage="advisor" if need else "chair", need=need)

    def export_jsonl(self, system, good_only=False):
        out = []
        for brief, resp, action, fwd, ts, sym in self.db.execute("select brief,resp,action,fwd_ret,ts,symbol from observations order by ts"):
            if good_only and (fwd is None or not _good(action, fwd)): continue
            out.append(json.dumps(dict(messages=[dict(role="system", content=system), dict(role="user", content=brief), dict(role="assistant", content=resp)],
                                       meta=dict(symbol=sym, ts=ts, action=action, forward_return=fwd))))
        return "\n".join(out) + ("\n" if out else "")
