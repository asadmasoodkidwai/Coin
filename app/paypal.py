"""PayPal sandbox: Orders API v2 funds the vault (the official PayPal button in the browser, capture on the server) and Payouts sends profit out.
Without credentials the app runs in a clearly labelled demo mode. The host is pinned to the PayPal sandbox; the only override allowed is localhost, for tests."""
import os, time, uuid
import httpx

_tok = dict(v=None, exp=0.0)


def _host():
    h = os.getenv("PAYPAL_API_BASE", "")
    return h.rstrip("/") if h.startswith(("http://127.0.0.1", "http://localhost")) else "https://api-m.sandbox.paypal.com"


def configured():
    return bool(os.getenv("PAYPAL_CLIENT_ID") and os.getenv("PAYPAL_SECRET"))


def _mask_email(e):
    return (e.split("@")[0][:2] + "***@" + e.split("@")[1]) if "@" in e else ""


def public_config(full=False):
    """What the browser may know. The secret never leaves the server; the client ID is public by design but only sent when needed."""
    cid, email = os.getenv("PAYPAL_CLIENT_ID", ""), os.getenv("PAYPAL_PAYOUT_EMAIL", "")
    out = dict(configured=configured(), env="sandbox", client_id_masked=(cid[:6] + "..." + cid[-4:]) if len(cid) > 12 else ("set" if cid else ""),
               payout_email=_mask_email(email), payouts_ready=bool(configured() and email))
    if full: out["client_id"] = cid
    return out


def _token():
    if _tok["v"] and time.time() < _tok["exp"]: return _tok["v"]
    r = httpx.post(f"{_host()}/v1/oauth2/token", auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_SECRET"]), data={"grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status(); j = r.json(); _tok.update(v=j["access_token"], exp=time.time() + float(j.get("expires_in", 300)) - 60)
    return _tok["v"]


def _h(extra=None):
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json", **(extra or {})}


def create_order(amount: float, redirect=False):
    """Create the order on the server. The browser button only receives the order id and PayPal handles the approval."""
    if not configured(): return dict(id=f"SIM-{uuid.uuid4().hex[:8]}", approve_url=None, simulated=True)
    body = dict(intent="CAPTURE", purchase_units=[dict(amount=dict(currency_code="USD", value=f"{amount:.2f}"), description="Coin Board vault deposit")])
    if redirect:   # fallback without the button: send the buyer to PayPal and bring them back
        base = os.getenv("APP_BASE_URL", "http://localhost:8000")
        body["payment_source"] = dict(paypal=dict(experience_context=dict(return_url=f"{base}/?paypal=return", cancel_url=f"{base}/?paypal=cancel", user_action="PAY_NOW")))
    r = httpx.post(f"{_host()}/v2/checkout/orders", headers=_h({"PayPal-Request-Id": uuid.uuid4().hex}), timeout=30, json=body)
    r.raise_for_status(); o = r.json()
    return dict(id=o["id"], approve_url=next((l["href"] for l in o.get("links", []) if l["rel"] in ("payer-action", "approve")), None), simulated=False)


def capture_order(order_id: str):
    """Move the money after the buyer approved. Returns the capture details, or None for a demo order."""
    if order_id.startswith("SIM-") or not configured(): return None
    r = httpx.post(f"{_host()}/v2/checkout/orders/{order_id}/capture", headers=_h({"PayPal-Request-Id": order_id}), json={}, timeout=30)
    if r.status_code == 422 and "ORDER_ALREADY_CAPTURED" in r.text:   # captured earlier (for example the page reloaded): read the result instead
        r = httpx.get(f"{_host()}/v2/checkout/orders/{order_id}", headers=_h(), timeout=30)
    r.raise_for_status(); o = r.json()
    if o.get("status") != "COMPLETED": raise ValueError(f"Order not completed: {o.get('status')}")
    cap = o["purchase_units"][0]["payments"]["captures"][0]
    if cap.get("status") != "COMPLETED": raise ValueError(f"PayPal has not completed this payment yet ({cap.get('status')})")
    return dict(amount=float(cap["amount"]["value"]), currency=cap["amount"]["currency_code"], capture_id=cap["id"], payer=(o.get("payer") or {}).get("email_address", ""))


def payout(amount: float):
    email = os.getenv("PAYPAL_PAYOUT_EMAIL")
    if not (configured() and email): return dict(id=f"DEMO-PAYOUT-{uuid.uuid4().hex[:6]}", status="DEMO", recipient=email or "", simulated=True)
    bid = uuid.uuid4().hex
    r = httpx.post(f"{_host()}/v1/payments/payouts", headers=_h(), timeout=30, json=dict(
        sender_batch_header=dict(sender_batch_id=bid, email_subject="Coin Board profit withdrawal"),
        items=[dict(recipient_type="EMAIL", receiver=email, amount=dict(value=f"{amount:.2f}", currency="USD"), sender_item_id=bid)]))
    r.raise_for_status(); h = r.json()["batch_header"]
    return dict(id=h["payout_batch_id"], status=h.get("batch_status", "PENDING"), recipient=email, simulated=False)
