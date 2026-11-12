"""The Chair reads every analyst report and decides buy / sell / ignore.
Order of use: the main model (CHAIR_*), a backup model (CHAIR2_*, for example a local model), the learned student, then a fixed rule.
If a model hits its rate limit or fails, it is paused for a while and the next one in the list takes over automatically."""
import json, os, random, re, time
import httpx

SYSTEM = ("You are the Chair of an investment committee for a long-only paper-trading account. Four analysts report scores in -1..1. "
          "Weigh them, say where they disagree, and prefer ignore when evidence is thin or mixed. Read the headlines yourself: treat investigations, scandals, lawsuits, fraud or leadership crises as event risk that can outweigh the scores, and do not recommend buying into an unresolved major scandal. Respond with JSON only: "
          '{"action":"buy|sell|ignore","confidence":0-100,"size_scale":0.25-1,"stop_atr":1-4,"target_atr":1-8,"reasoning":"<=70 words","dissent":"<=30 words","explanation":"150-250 words in plain English with blank lines between paragraphs: the evidence for, the evidence against, the main risks, and what would change your mind"}')
WEIGHTS = {"Market": .3, "Forecast": .3, "Sentiment": .2, "Company": .2}
BACKENDS = [("main", "CHAIR"), ("backup", "CHAIR2")]
COOL, ERR = {}, {}   # model name -> paused until (epoch), last error text
LEARNER = None       # set by main: watches decisions and provides the learned student


def _rules(reports):
    comp = sum(WEIGHTS[r["name"]] * r["score"] * (0.5 + r["confidence"]) for r in reports) / 1.0
    act = "buy" if comp > 0.2 else "sell" if comp < -0.2 else "ignore"
    ev = any(r["data"].get("event_risk") for r in reports)
    if ev and act == "buy": act = "ignore"
    worst = min(reports, key=lambda r: r["score"] * (1 if comp > 0 else -1))
    seats = " ".join(f"The {r['name']} analyst ({r.get('model', 'model')}) scored {r['score']:+.2f}: {r.get('summary', '')}" for r in reports)
    exp = (f"The board combined four analysts into a weighted score of {comp:+.2f}. " + seats + "\n\n" +
           ("Severe negative news is flagged, so the board will not buy. " if ev else "") +
           ("The signals agree enough to act." if act != "ignore" else "The signals are too mixed to justify a trade.") +
           " This summary comes from a fixed rule because no Chair model is available, so it cannot weigh nuance the way a reasoning model can.")
    return dict(explanation=exp, action=act, confidence=round(min(95, 50 + abs(comp) * 60)), size_scale=0.5 if abs(comp) < 0.4 else 0.8, stop_atr=2, target_atr=4,
                reasoning=f"Weighted analyst score is {comp:+.2f}. " + ("Severe negative news is flagged, so no buying. " if ev else "") + ("Signals agree enough to act." if act != "ignore" else "Signals are too mixed to act."),
                dissent=f"{worst['name']} is the least aligned seat", engine="Fixed rule")


def _parse(txt):
    txt = re.sub(r"<think>.*?</think>", "", txt or "", flags=re.S)          # some reasoning models print their thinking first
    v = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
    act = v["action"] if v.get("action") in ("buy", "sell", "ignore") else "ignore"
    clamp = lambda x, lo, hi, d: max(lo, min(hi, float(x))) if isinstance(x, (int, float)) else d
    return dict(action=act, confidence=clamp(v.get("confidence"), 0, 100, 50), size_scale=clamp(v.get("size_scale"), .25, 1, .5),
                stop_atr=clamp(v.get("stop_atr"), 1, 4, 2), target_atr=clamp(v.get("target_atr"), 1, 8, 4),
                reasoning=str(v.get("reasoning", ""))[:500], explanation=str(v.get("explanation", ""))[:2500], dissent=str(v.get("dissent", ""))[:200])


def _llm(base, key, model, brief, timeout):
    r = httpx.post(base.rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {key}"}, timeout=timeout,
                   json=dict(model=model, temperature=0.2, messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(brief)}]))
    r.raise_for_status()
    return _parse(r.json()["choices"][0]["message"].get("content"))


def _explain(e, base, key):
    """Turn an error into (message, seconds to pause this model)."""
    resp = getattr(e, "response", None)
    if resp is None: return f"{type(e).__name__}: {str(e)[:150]}", 120
    code = resp.status_code; msg = f"HTTP {code} from {resp.url}: {resp.text[:300]}"
    if code in (429, 402):                      # rate limit or quota used up: honour the server's retry time
        try: wait = float(resp.headers.get("retry-after", ""))
        except ValueError: wait = 900
        return msg, max(60, min(wait, 86400))
    if code in (401, 403): return msg, 3600
    if code == 404:
        try:
            ids = [x["id"] for x in httpx.get(base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}, timeout=20).json()["data"]]
            msg += " | Models your key can use: " + ", ".join(ids[:25])
        except Exception:
            pass
        return msg, 300
    return msg, 120


WIN_MIN, OBJECT_CONF = 0.45, 60


def _second_voice(v, s, pwin):
    """The student as a second voice. It can only make a buy smaller or cancel it; it can never force a trade, and never blocks a sell."""
    info = dict(action=s["action"], confidence=s["confidence"], probs=s.get("probs"), p_win=pwin, effect="agrees")
    if v["action"] == "buy":
        differs = s["action"] != "buy" and s["confidence"] >= OBJECT_CONF
        weak = pwin is not None and pwin < WIN_MIN
        if (differs and weak) or (pwin is not None and pwin < 0.35): v["action"] = "ignore"; info["effect"] = "vetoed"
        elif differs or weak: v["size_scale"] = max(0.25, v["size_scale"] * 0.5); info["effect"] = "halved"
    why = {"agrees": "It agrees with the main model.", "halved": "It had doubts, so the position size was halved.", "vetoed": "It objected strongly, so the buy was cancelled."}[info["effect"]]
    pw = f" In similar past situations, buys worked out about {pwin * 100:.0f}% of the time." if pwin is not None else ""
    v["reasoning"] = (v["reasoning"] + f" Second voice (student): {s['action']} at {s['confidence']:.0f}%. {why}")[:700]
    v["explanation"] = (v.get("explanation", "") + f"\n\nSecond voice: the student model, trained on this system's own past decisions, would {s['action']} here ({s['confidence']:.0f}% sure).{pw} {why}")[:3000]
    v["second_opinion"] = info; return v


def decide(sym, reports, held_qty, cfg=None):
    cfg = cfg or {}
    brief = {"symbol": sym, "shares_held": held_qty, "reports": [{k: r[k] for k in ("name", "score", "confidence", "summary")} for r in reports],
             "headlines": next((r["data"].get("headlines", []) for r in reports if r["name"] == "Sentiment"), [])}
    stage, s = "apprentice", None
    if LEARNER:
        try:
            stage = LEARNER.stage(cfg)["stage"]
            if stage != "off": s = LEARNER.predict(reports, held_qty)
        except Exception:
            stage, s = "apprentice", None
    # Chair role: the student decides when it is sure, and the main model is only asked when it is unsure or for a random spot check
    if stage == "chair" and s and s["confidence"] >= 70 and random.random() >= float(cfg.get("student_check_rate", 0.2)):
        pw = LEARNER.predict_win(reports, held_qty)
        if s["action"] == "buy" and pw is not None and pw < WIN_MIN:
            s.update(action="ignore", reasoning=s["reasoning"] + f" Skipped: similar buys worked out only {pw * 100:.0f}% of the time.")
        s.update(source="student", engine=s["engine"] + " | Chair role: the main model is asked only when the student is unsure, plus spot checks"); return s
    notes, teacher = [], None
    for name, pre in BACKENDS:
        base, model = os.getenv(pre + "_BASE_URL"), os.getenv(pre + "_MODEL")
        if not (base and model): continue
        if COOL.get(name, 0) > time.time():
            notes.append(f"{name} model paused until {time.strftime('%H:%M', time.localtime(COOL[name]))}"); continue
        key = os.getenv(pre + "_API_KEY", "x")
        try:
            v = _llm(base, key, model, brief, float(os.getenv(pre + "_TIMEOUT", "180" if name == "backup" else "60")))
            ERR.pop(name, None); v.update(engine=f"Chair model ({name}): {model}", source=name)
            if LEARNER:
                try: v["obs_id"] = LEARNER.observe(sym, reports, held_qty, brief, v, name, model)   # always learn from the main model's call
                except Exception: pass
            teacher = v; break
        except Exception as e:
            msg, wait = _explain(e, base, key); ERR[name] = msg[:400]; COOL[name] = time.time() + wait; notes.append(f"{name} model failed: {msg}")
    if teacher is not None:
        if s and stage == "advisor":
            try: _second_voice(teacher, s, LEARNER.predict_win(reports, held_qty))
            except Exception: pass
        elif s and stage == "chair" and s["action"] != teacher["action"]:
            teacher["second_opinion"] = dict(action=s["action"], confidence=s["confidence"], probs=s.get("probs"), p_win=None, effect="overruled")
            teacher["reasoning"] = (teacher["reasoning"] + f" The student would have chosen {s['action']}; the main model overruled it.")[:700]
        return teacher
    tail = (" | " + " | ".join(notes)) if notes else ""
    if s is None and LEARNER and stage != "off":
        try: s = LEARNER.predict(reports, held_qty)
        except Exception: s = None
    if s: s.update(source="student", engine=s["engine"] + tail); return s
    out = _rules(reports); out.update(source="rules", engine="Fixed rule" + (tail if notes else " (no Chair model configured)")); return out


def status(cfg=None):
    out = {}
    for name, pre in BACKENDS:
        base, model = os.getenv(pre + "_BASE_URL"), os.getenv(pre + "_MODEL"); cu = COOL.get(name, 0)
        out[name] = dict(configured=bool(base and model), model=model or "", paused_until=cu if cu > time.time() else None, last_error=ERR.get(name))
    out["student"] = dict(LEARNER.stats(), **LEARNER.stage(cfg)) if LEARNER else None
    return out
