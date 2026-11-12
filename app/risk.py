"""Risk Firewall. Runs on the server against server-held state. The Chair cannot override it."""
PRESETS = {
    "cautious": dict(max_pos_pct=10, max_loss_pct=0.5, daily_loss_pct=1.5, min_conf=75),
    "balanced": dict(max_pos_pct=15, max_loss_pct=1.0, daily_loss_pct=3.0, min_conf=68),
    "bold": dict(max_pos_pct=25, max_loss_pct=2.0, daily_loss_pct=6.0, min_conf=60),
}
EXCLUDED = {"GME", "AMC"}


def evaluate(p: dict, limits: dict, st: dict, equity: float, held: dict) -> dict:
    """p: symbol, side, qty, price, stop, confidence. held: {symbol: qty}."""
    buy = p["side"] == "buy"
    notional = p["qty"] * p["price"]
    loss = max(0.0, (p["price"] - p["stop"]) * p["qty"]) if buy else 0.0
    daily_used = max(0.0, -st["day_realized"])
    pos_pct = (notional + held.get(p["symbol"], 0) * p["price"]) / equity * 100 if equity > 0 else 100
    c = [
        ("circuit", "Trading not paused", (not buy) or not st["paused"], "Paused after a large drawdown" if st["paused"] else "Active"),
        ("cash", "Enough cash", (not buy) or notional <= st["cash"], f"${notional:,.2f} order vs ${st['cash']:,.2f} cash"),
        ("size", "Position size", (not buy) or pos_pct <= limits["max_pos_pct"], f"{pos_pct:.1f}% of capital vs {limits['max_pos_pct']}% limit"),
        ("loss", "Loss if stopped out", loss <= equity * limits["max_loss_pct"] / 100, f"${loss:,.2f} vs ${equity * limits['max_loss_pct'] / 100:,.2f} limit"),
        ("daily", "Daily loss budget", daily_used + loss <= equity * limits["daily_loss_pct"] / 100, f"${daily_used + loss:,.2f} worst case vs ${equity * limits['daily_loss_pct'] / 100:,.2f}"),
        ("conf", "Board confidence", (not buy) or p["confidence"] >= limits["min_conf"], f"{p['confidence']:.0f}% vs {limits['min_conf']}% minimum"),
        ("news", "No major negative news", (not buy) or not p.get("event_risk"), "Serious negative headline flagged" if p.get("event_risk") else "None flagged"),
        ("excl", "Allowed asset", (not buy) or p["symbol"] not in EXCLUDED, "Excluded" if p["symbol"] in EXCLUDED else "Allowed"),
        ("shape", "Order makes sense", p["qty"] > 0 and p["price"] > 0 and (p["stop"] < p["price"] if buy else held.get(p["symbol"], 0) >= p["qty"]),
         "Long-only: buys need a stop below price, sells need shares you hold"),
    ]
    checks = [dict(key=k, label=l, passed=bool(ok), detail=d) for k, l, ok, d in c]
    failed = [x["label"] for x in checks if not x["passed"]]
    return dict(status="blocked" if failed else "approved", checks=checks, projected_loss=round(loss, 2),
                reason=("Blocked: " + "; ".join(f"{x['label'].lower()} ({x['detail']})" for x in checks if not x["passed"])) if failed else "Every limit passed.")
