"""Payment gateway adapter. SABZOMI_GATEWAY=razorpay (live) or sim (default, development).
Razorpay env: RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET."""
import base64, hashlib, hmac, json, os, urllib.request, uuid
from services import ApiError

def provider(): return os.environ.get("SABZOMI_GATEWAY", "sim")
def _key(): return os.environ.get("RAZORPAY_KEY_ID", "")
def _secret(): return os.environ.get("RAZORPAY_KEY_SECRET", "")
def public_info(): return {"provider": provider(), "key_id": _key() if provider() == "razorpay" else None}

def create_order(amount_paise, receipt):
    """Returns the gateway order id. Called BEFORE opening a DB transaction (network call)."""
    if provider() != "razorpay": return "gw_" + uuid.uuid4().hex[:14]
    auth = base64.b64encode(f"{_key()}:{_secret()}".encode()).decode()
    req = urllib.request.Request("https://api.razorpay.com/v1/orders", method="POST", headers={"Content-Type": "application/json", "Authorization": "Basic " + auth},
                                 data=json.dumps({"amount": amount_paise, "currency": "INR", "receipt": receipt[:40]}).encode())
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return json.loads(r.read())["id"]
    except Exception:
        raise ApiError("Payment gateway is unavailable. Please try again.", 502)

def parse_razorpay(raw, signature):
    """Verify the webhook signature, then normalise to our internal event (or None to ignore)."""
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    good = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest() if secret else None
    if not good or not signature or not hmac.compare_digest(good, signature): raise ApiError("Invalid signature", 401)
    p = json.loads(raw); ent = ((p.get("payload") or {}).get("payment") or {}).get("entity") or {}
    if p.get("event") in ("payment.captured", "order.paid") and ent.get("order_id"):
        return {"event": "payment.captured", "gateway_order_id": ent["order_id"], "gateway_payment_id": ent["id"], "amount_paise": ent["amount"]}
    if p.get("event") == "payment.failed" and ent.get("order_id"): return {"event": "payment.failed", "gateway_order_id": ent["order_id"]}
    return None

def verify_checkout(order_id, payment_id, signature):
    """Razorpay checkout callback signature: HMAC_SHA256(order_id|payment_id, key_secret)."""
    if provider() != "razorpay" or not signature: return False
    good = hmac.new(_secret().encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, signature)
