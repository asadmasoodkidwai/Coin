"""Board analysts. Each uses an existing model when installed, otherwise a clearly labelled baseline."""
import math, os, re, sys, time
import numpy as np, pandas as pd


def bars(sym):
    try:
        import yfinance as yf
        df = yf.Ticker(sym).history(period="1y", interval="1d").rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        df.index = df.index.tz_localize(None)
        if len(df) > 80: return df, "yfinance"
    except Exception:
        pass
    rng = np.random.default_rng(abs(hash(sym)) % 2**32)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=250)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.014, len(idx))))
    df = pd.DataFrame(dict(open=close * 0.998, high=close * 1.01, low=close * 0.99, close=close, volume=rng.integers(1e6, 5e6, len(idx))), index=idx)
    return df, "demo"


def _rep(name, model, score, conf, summary, **data):
    return dict(name=name, model=model, score=round(float(max(-1, min(1, score))), 3), confidence=round(float(conf), 2), summary=summary, data=data)


def indicators(df):
    c = df["close"]; d = c.diff()
    rsi = 100 - 100 / (1 + d.clip(lower=0).rolling(14).mean() / ((-d.clip(upper=0)).rolling(14).mean() + 1e-9))
    tr = pd.concat([df["high"] - df["low"], (df["high"] - c.shift()).abs(), (df["low"] - c.shift()).abs()], axis=1).max(axis=1)
    return dict(price=float(c.iloc[-1]), sma20=float(c.rolling(20).mean().iloc[-1]), sma50=float(c.rolling(50).mean().iloc[-1]),
                rsi=float(rsi.iloc[-1]), atr=float(tr.rolling(14).mean().iloc[-1]), vol=float(c.pct_change().tail(20).std()))


def market_analyst(ind):
    s = 0.6 * math.tanh((ind["price"] / ind["sma50"] - 1) * 10) + 0.4 * math.tanh((ind["sma20"] / ind["sma50"] - 1) * 15)
    if ind["rsi"] > 75: s -= 0.25
    if ind["rsi"] < 25: s += 0.25
    t = "above" if ind["price"] > ind["sma50"] else "below"
    return _rep("Market", "Technical indicators (SMA, RSI, ATR)", s, 0.6, f"Price is {t} its 50-day average; RSI {ind['rsi']:.0f}.", **ind)


_kronos = None


def _load_kronos():
    global _kronos
    repo = os.getenv("KRONOS_REPO")
    if _kronos is None and repo:
        sys.path.insert(0, repo)
        from model import Kronos, KronosTokenizer, KronosPredictor
        tok = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
        try:
            import torch
            dev = os.getenv("KRONOS_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
        except Exception:
            dev = "cpu"
        _kronos = KronosPredictor(Kronos.from_pretrained("NeoQuasar/Kronos-small"), tok, device=dev, max_context=512)
    return _kronos


def _ms(t): return int(pd.Timestamp(t).timestamp() * 1000)


def _kronos_samples(p, x, xt, fut, horizon, n):
    """n independent single-sample futures. Kronos averages samples inside one call, so a spread needs separate samples."""
    cols = ["open", "high", "low", "close", "volume"]; yt = pd.Series(fut)
    try:
        outs = p.predict_batch([x[cols]] * n, [xt] * n, [yt] * n, pred_len=horizon, T=1.0, top_p=0.9, sample_count=1, verbose=False)
    except Exception:
        outs = [p.predict(df=x[cols], x_timestamp=xt, y_timestamp=yt, pred_len=horizon, T=1.0, top_p=0.9, sample_count=1, verbose=False) for _ in range(n)]
    return np.array([[float(v) for v in o["close"].values] for o in outs])


def forecaster(df, ind, horizon=5):
    c = df["close"]; last = float(c.iloc[-1])
    fut = pd.bdate_range(df.index[-1] + pd.Timedelta(days=1), periods=horizon)
    hist = [[_ms(t), round(float(v), 2)] for t, v in c.tail(60).items()]
    vol_cone = [[_ms(t), round(last * (1 - ind["vol"] * math.sqrt(i + 1)), 2), round(last * (1 + ind["vol"] * math.sqrt(i + 1)), 2)] for i, t in enumerate(fut)]
    mk = lambda path, cone, **extra: dict(history=hist, path=[[_ms(t), round(float(v), 2)] for t, v in zip(fut, path)], cone=cone, last=round(last, 2), **extra)
    try:
        p = _load_kronos()
        if p is not None:
            x = df.tail(400).rename_axis("timestamps").reset_index()
            n = max(2, int(os.getenv("KRONOS_SAMPLES", "10")))
            S = _kronos_samples(p, x, x["timestamps"], fut, horizon, n)                 # n simulated futures
            med, lo, hi = np.median(S, axis=0), np.percentile(S, 10, axis=0), np.percentile(S, 90, axis=0)
            ret = float(med[-1] / last - 1); p_up = float((S[:, -1] > last).mean())
            cone = [[_ms(t), round(float(a), 2), round(float(b), 2)] for t, a, b in zip(fut, lo, hi)]
            return _rep("Forecast", f"Kronos-small ({n} simulated futures)", math.tanh(ret / (ind["vol"] * math.sqrt(horizon) + 1e-9)), 0.35 + 0.3 * abs(p_up - 0.5) * 2,
                        f"Kronos simulated {n} futures: median {ret * 100:+.1f}% over {horizon} trading days, and {int(round(p_up * n))} of {n} end higher.",
                        **mk(med, cone, expected_return=ret, p_up=p_up, band_source="kronos", samples=n))
    except Exception as e:
        err = str(e)[:80]
    else:
        err = "Kronos not configured"
    slope = np.polyfit(range(20), c.tail(20).values, 1)[0]
    closes = [last + slope * (i + 1) for i in range(horizon)]; ret = closes[-1] / last - 1
    return _rep("Forecast", f"Linear-trend baseline ({err})", math.tanh(ret / (ind["vol"] * math.sqrt(horizon) + 1e-9)), 0.3,
                f"Baseline trend implies {ret * 100:+.1f}% over {horizon} days. Enable Kronos for the real model.",
                **mk(closes, vol_cone, expected_return=ret, band_source="volatility"))


_POS = ("beat", "surge", "growth", "record", "upgrade", "profit", "strong", "gain", "rally")
_NEG = ("miss", "plunge", "lawsuit", "downgrade", "loss", "weak", "probe", "recall", "fall", "cut")
_finbert = None


_SEV = ["investigat", "probe", "scandal", "espionage", "spy", "spies", "spying", "fraud", "lawsuit", "sues", "indict", "subpoena",
        "resign", "recall", "bankrupt", "antitrust", "smuggl", "sanction", "delist", "guilty", "whistleblower", "misconduct"]


def severe(title, sym=""):
    """True if a headline mentions an investigation, scandal, fraud, lawsuit and similar event risk."""
    t = title.lower()
    return any(re.search(r"\b" + x, t) for x in _SEV if not (x.startswith("spy") or x == "spies") or sym.upper() != "SPY")


def articles(sym):
    """Recent news, newest first: title, publisher, link, short summary, age in hours."""
    try:
        import yfinance as yf
        from datetime import datetime, timezone
        out = []
        for n in yf.Ticker(sym).news[:20]:
            c = n.get("content") or {}
            t = n.get("title") or c.get("title")
            if not t: continue
            age, ts = None, c.get("pubDate") or c.get("displayTime")
            try:
                if ts: age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 3600
                elif n.get("providerPublishTime"): age = (time.time() - n["providerPublishTime"]) / 3600
            except Exception:
                pass
            url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url") or n.get("link")
            out.append(dict(title=t, publisher=(c.get("provider") or {}).get("displayName") or n.get("publisher") or "", url=url,
                            summary=re.sub(r"<[^>]+>", "", c.get("summary") or c.get("description") or "")[:220], age_h=age))
        out.sort(key=lambda a: (a["age_h"] is None, a["age_h"] or 0))
        return out[:12]
    except Exception:
        return []


def sentiment_analyst(sym):
    global _finbert
    arts = articles(sym); titles = [a["title"] for a in arts]
    if not arts:
        return _rep("Sentiment", "No headlines available", 0, 0.1, "No recent headlines found.", headlines=[], articles=[], event_risk=False)
    try:
        if _finbert is None:
            from transformers import pipeline
            _finbert = pipeline("text-classification", model="ProsusAI/finbert", top_k=None)
        sc = []
        for r in _finbert(titles):
            m = {x["label"].lower(): x["score"] for x in r}; sc.append(m.get("positive", 0) - m.get("negative", 0))
        model = "FinBERT (ProsusAI/finbert)"
    except Exception:
        sc = [sum(w in h.lower() for w in _POS) - sum(w in h.lower() for w in _NEG) for h in titles]; model = "Keyword baseline (install FinBERT)"
    w = [1 / (1 + (a["age_h"] if a["age_h"] is not None else 48) / 24) for a in arts]   # recent news counts more
    score = math.tanh(float(np.average(sc, weights=w)))
    for a, s in zip(arts, sc):
        a["tone"] = round(float(max(-1, min(1, s))), 2)
        a["severe"] = bool((a["age_h"] is None or a["age_h"] < 72) and severe(a["title"], sym))
    flags = [a["title"] for a in arts if a["severe"]]
    if flags: score = min(score, -0.5)
    tone = "positive" if score > 0.1 else "negative" if score < -0.1 else "neutral"
    note = f" Severe news flagged: {flags[0][:100]}" if flags else ""
    return _rep("Sentiment", model, score, 0.7 if flags else 0.5, f"Across {len(arts)} recent headlines the tone is {tone}.{note}",
                headlines=titles[:8], articles=arts, event_risk=bool(flags), flagged=flags[:3])


def company_analyst(sym):
    try:
        import yfinance as yf
        i = yf.Ticker(sym).info
        m, g, pe = i.get("profitMargins"), i.get("revenueGrowth"), i.get("trailingPE")
        if m is None and g is None: raise ValueError
        s = math.tanh(((m or 0) - 0.08) * 4) * 0.5 + math.tanh((g or 0) * 3) * 0.4 - (0.2 if pe and pe > 45 else 0)
        return _rep("Company", "Fundamentals screen (margins, growth, valuation)", s, 0.5,
                    f"Profit margin {((m or 0) * 100):.0f}%, revenue growth {((g or 0) * 100):.0f}%, P/E {round(pe, 1) if pe else 'n/a'}.", sector=i.get("sector"))
    except Exception:
        return _rep("Company", "Unavailable", 0, 0.1, "Fundamentals not available for this symbol.")


def run_analysts(sym):
    df, src = bars(sym); ind = indicators(df)
    return [market_analyst(ind), forecaster(df, ind), sentiment_analyst(sym), company_analyst(sym)], ind, src


def last_price(sym):
    """Latest traded price, or None if market data is unreachable."""
    try:
        import yfinance as yf
        p = yf.Ticker(sym).fast_info["last_price"]
        return float(p) if p else None
    except Exception:
        return None


RANGES = {"1d": ("1d", "5m"), "5d": ("5d", "15m"), "1mo": ("1mo", "1d"), "6mo": ("6mo", "1d"), "1y": ("1y", "1d")}
_CH = {}


def chart_points(sym, rng="1d"):
    """Price series for the live chart: [[epoch_ms, close], ...]. Cached for 10 seconds."""
    rng = rng if rng in RANGES else "1d"; key = (sym, rng); hit = _CH.get(key)
    if hit and time.time() - hit[0] < 10: return hit[1]
    try:
        import yfinance as yf
        df = yf.Ticker(sym).history(period=RANGES[rng][0], interval=RANGES[rng][1])
        if len(df) < 2: raise ValueError("no data")
        out = dict(points=[[_ms(t), round(float(v), 2)] for t, v in df["Close"].dropna().items()], source="yfinance")
    except Exception:
        df, src = bars(sym); n = dict(zip(RANGES, (30, 30, 22, 126, 250)))[rng]
        out = dict(points=[[_ms(t), round(float(v), 2)] for t, v in df["close"].tail(n).items()], source=src)
    out.update(symbol=sym, range=rng); _CH[key] = (time.time(), out)
    return out
