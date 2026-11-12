"""A stand-in for PayPal's sandbox API, used ONLY by the tests (this is not PayPal and never used by the app itself).
It mimics the Orders v2 and Payouts endpoints this app calls, following PayPal's documented behaviour, including refusing to
capture an order the buyer has not approved and refusing to capture twice."""
import itertools
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(); ORDERS = {}; N = itertools.count(1); STATE = dict(fail_next_payout=False, payouts=0, seen_request_ids=[])


def _auth(a):
    if a != "Bearer fake-token": raise HTTPException(401, "bad token")


@app.post("/v1/oauth2/token")
async def token(): return {"access_token": "fake-token", "expires_in": 3600, "token_type": "Bearer"}


@app.post("/v2/checkout/orders")
async def create(req: Request, authorization: str = Header(None), paypal_request_id: str = Header(None)):
    _auth(authorization); b = await req.json(); pu = b["purchase_units"][0]; oid = f"ORD-{next(N):04d}"
    assert b["intent"] == "CAPTURE" and pu["amount"]["currency_code"] == "USD" and paypal_request_id
    ORDERS[oid] = dict(amount=pu["amount"]["value"], currency="USD", approved=False, capture=None)
    return {"id": oid, "status": "CREATED", "links": [{"rel": "payer-action", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={oid}"}]}


def _full(oid):
    o = ORDERS[oid]
    return {"id": oid, "status": "COMPLETED", "payer": {"email_address": "buyer@example.com"},
            "purchase_units": [{"payments": {"captures": [{"id": o["capture"], "status": "COMPLETED", "amount": {"value": o["amount"], "currency_code": o["currency"]}}]}}]}


@app.post("/v2/checkout/orders/{oid}/capture")
async def capture(oid: str, authorization: str = Header(None), paypal_request_id: str = Header(None)):
    _auth(authorization); o = ORDERS.get(oid)
    if not o: return JSONResponse({"name": "RESOURCE_NOT_FOUND"}, 404)
    STATE["seen_request_ids"].append(paypal_request_id)
    if o["capture"]: return JSONResponse({"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]}, 422)
    if not o["approved"]: return JSONResponse({"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED"}]}, 422)
    o["capture"] = f"CAP-{oid[-4:]}"; return _full(oid)


@app.get("/v2/checkout/orders/{oid}")
async def get_order(oid: str, authorization: str = Header(None)):
    _auth(authorization); o = ORDERS.get(oid)
    if not o: return JSONResponse({"name": "RESOURCE_NOT_FOUND"}, 404)
    return _full(oid) if o["capture"] else {"id": oid, "status": "APPROVED" if o["approved"] else "CREATED"}


@app.post("/fake/approve/{oid}")        # test hook: stands in for the buyer logging in and approving in the PayPal popup
async def approve(oid: str): ORDERS[oid]["approved"] = True; return {"ok": True}


@app.post("/fake/fail-next-payout")
async def fail_next(): STATE["fail_next_payout"] = True; return {"ok": True}


@app.post("/v1/payments/payouts")
async def payouts(req: Request, authorization: str = Header(None)):
    _auth(authorization); b = await req.json(); it = b["items"][0]
    assert it["recipient_type"] == "EMAIL" and it["amount"]["currency"] == "USD" and b["sender_batch_header"]["sender_batch_id"]
    if STATE["fail_next_payout"]: STATE["fail_next_payout"] = False; return JSONResponse({"name": "INSUFFICIENT_FUNDS"}, 400)
    STATE["payouts"] += 1; return {"batch_header": {"payout_batch_id": f"PAYOUT-{STATE['payouts']:04d}", "batch_status": "PENDING"}}
