"""Sabzomi backend: business logic. Every function expects to run inside db.tx()."""
import base64, hashlib, hmac, json, os, re, secrets, sqlite3, string, uuid
from cryptography.fernet import Fernet
from db import now, get_settings

SECRET = os.environ.get("SABZOMI_SECRET", "dev-only-secret-change-me")
GATEWAY_SECRET = os.environ.get("SABZOMI_GATEWAY_SECRET", "dev-gateway-secret")
_F = Fernet(base64.urlsafe_b64encode(hashlib.sha256((SECRET + "|field").encode()).digest()))

class ApiError(Exception):
    def __init__(self, msg, status=400, **extra): self.msg, self.status, self.extra = msg, status, extra

def inr(p): return f"₹{p // 100:,}.{p % 100:02d}"

# ---------- security helpers ----------
def hash_pw(pw):
    salt = os.urandom(16)
    return salt.hex() + ":" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()

def check_pw(pw, stored):
    salt, h = stored.split(":")
    got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32).hex()
    return hmac.compare_digest(got, h)

def enc(obj): return _F.encrypt(json.dumps(obj).encode()).decode()
def dec(tok): return json.loads(_F.decrypt(tok.encode()).decode()) if tok else None

def audit(db, actor, action, entity=None, entity_id=None, before=None, after=None, reason=None, ctx=None):
    ctx = ctx or {}
    db.execute("INSERT INTO audit_log(actor_id,action,entity,entity_id,before_json,after_json,reason,ip,user_agent,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (actor, action, entity, str(entity_id) if entity_id is not None else None, json.dumps(before) if before is not None else None,
                json.dumps(after) if after is not None else None, reason, ctx.get("ip"), ctx.get("ua"), now()))

def notify(db, uid, title, body=""):
    db.execute("INSERT INTO notifications(user_id,title,body,created_at) VALUES(?,?,?,?)", (uid, title, body, now()))

def flag(db, user_id, kind, detail):
    db.execute("INSERT INTO risk_flags(user_id,kind,detail,status,created_at) VALUES(?,?,?,'open',?)", (user_id, kind, detail, now()))

# ---------- settings validation ----------
def validate_settings(cfg):
    errs = []
    def ints(*keys):
        for k in keys:
            if not isinstance(cfg.get(k), int) or isinstance(cfg.get(k), bool) or cfg[k] < 0: errs.append(f"{k} must be a non-negative integer")
    ints("grocery_min_paise", "free_delivery_paise", "grocery_fee_paise", "mlm_pool_bp", "max_commission_per_order_paise",
         "min_order_for_commission_paise", "min_personal_purchase_paise", "release_days", "gateway_cost_bp", "min_withdraw_paise",
         "max_withdraw_paise", "withdraw_fee_flat_paise", "withdraw_fee_bp", "daily_withdraw_limit_paise", "max_signups_per_ip_day", "refund_flag_threshold")
    lv = cfg.get("level_bp")
    if not isinstance(lv, list) or not lv or len(lv) > 10 or not all(isinstance(x, int) and 0 <= x <= 10000 for x in lv):
        errs.append("level_bp must be 1-10 integers between 0 and 10000 (basis points of the MLM pool)")
    elif sum(lv) > 10000: errs.append(f"level_bp totals {sum(lv)/100}% which exceeds 100% of the MLM pool")
    if isinstance(cfg.get("mlm_pool_bp"), int) and cfg["mlm_pool_bp"] > 10000: errs.append("mlm_pool_bp cannot exceed 10000")
    if isinstance(cfg.get("min_withdraw_paise"), int) and isinstance(cfg.get("max_withdraw_paise"), int) and cfg["min_withdraw_paise"] > cfg["max_withdraw_paise"]:
        errs.append("min_withdraw_paise cannot exceed max_withdraw_paise")
    of = cfg.get("other_fee_paise")
    if not isinstance(of, dict) or not all(isinstance(v, int) and v >= 0 for v in of.values()): errs.append("other_fee_paise must map category to non-negative paise")
    if errs: raise ApiError("Invalid settings", 422, errors=errs)
    return cfg

# ---------- wallet ledger ----------
def balance(db, uid):
    return db.execute("SELECT COALESCE(SUM(CASE direction WHEN 'credit' THEN amount_paise ELSE -amount_paise END),0) b FROM ledger WHERE user_id=?", (uid,)).fetchone()["b"]

def post(db, uid, typ, direction, amount, ref_type=None, ref_id=None, desc="", allow_negative=False):
    if not isinstance(amount, int) or amount <= 0: raise ValueError("amount must be positive integer paise")
    if direction == "debit" and not allow_negative and balance(db, uid) < amount: raise ApiError("Insufficient wallet balance", 402)
    txn = "TXN" + secrets.token_hex(8).upper()
    db.execute("INSERT INTO ledger(txn_id,user_id,type,direction,amount_paise,ref_type,ref_id,description,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
               (txn, uid, typ, direction, amount, ref_type, str(ref_id) if ref_id is not None else None, desc, now()))
    return txn

# ---------- users / referral tree ----------
def _code(db, cfg):
    while True:
        c = cfg["referral_prefix"] + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        if not db.execute("SELECT 1 FROM users WHERE referral_code=?", (c,)).fetchone(): return c

def create_user(db, name, email, phone, password, sponsor_code=None, ip=None, role="user"):
    cfg = get_settings(db)
    name, email, phone = (name or "").strip(), (email or "").strip().lower(), (phone or "").strip()
    if len(name) < 2: raise ApiError("Name is required", 422)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email): raise ApiError("Valid email is required", 422)
    if not re.fullmatch(r"[6-9]\d{9}", phone): raise ApiError("Valid 10-digit Indian mobile number is required", 422)
    if len(password or "") < 8: raise ApiError("Password must be at least 8 characters", 422)
    sponsor = None
    if sponsor_code:
        sponsor = db.execute("SELECT * FROM users WHERE referral_code=? AND status='active'", (sponsor_code.strip().upper(),)).fetchone()
        if not sponsor: raise ApiError("Invalid referral code", 422)
    try:
        cur = db.execute("INSERT INTO users(name,email,phone,pw_hash,role,referral_code,sponsor_id,reg_ip,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (name, email, phone, hash_pw(password), role, _code(db, cfg), sponsor["id"] if sponsor else None, ip, now()))
    except sqlite3.IntegrityError:
        raise ApiError("Email or mobile number is already registered", 409)
    uid = cur.lastrowid
    path = (sponsor["path"] if sponsor else "/") + f"{uid}/"
    db.execute("UPDATE users SET account_id=?, path=?, depth=? WHERE id=?", (f"USR{uid:06d}", path, (sponsor["depth"] + 1) if sponsor else 0, uid))
    if ip and db.execute("SELECT COUNT(*) c FROM users WHERE reg_ip=? AND created_at>?", (ip, now() - 86400)).fetchone()["c"] > cfg["max_signups_per_ip_day"]:
        flag(db, uid, "multi_signup_ip", f"More than {cfg['max_signups_per_ip_day']} signups from {ip} in 24h")
    return uid

def upline(db, uid, n):
    out, cur = [], db.execute("SELECT sponsor_id FROM users WHERE id=?", (uid,)).fetchone()["sponsor_id"]
    while cur and len(out) < n:
        u = db.execute("SELECT * FROM users WHERE id=?", (cur,)).fetchone()
        out.append(u); cur = u["sponsor_id"]
    return out

def change_sponsor(db, admin_id, uid, new_sponsor_id, reason, ctx):
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    s = db.execute("SELECT * FROM users WHERE id=?", (new_sponsor_id,)).fetchone() if new_sponsor_id else None
    if not u or (new_sponsor_id and not s): raise ApiError("User not found", 404)
    if not reason: raise ApiError("A reason is required for network changes", 422)
    if s and (s["id"] == uid or f"/{uid}/" in s["path"]): raise ApiError("Self or circular referral is not allowed", 422)
    old_path = u["path"]; new_path = (s["path"] if s else "/") + f"{uid}/"
    for d in db.execute("SELECT id,path FROM users WHERE path LIKE ?", (old_path + "%",)).fetchall():
        p = new_path + d["path"][len(old_path):]
        db.execute("UPDATE users SET path=?, depth=? WHERE id=?", (p, p.count("/") - 2, d["id"]))
    db.execute("UPDATE users SET sponsor_id=? WHERE id=?", (new_sponsor_id, uid))
    audit(db, admin_id, "sponsor_change", "user", uid, {"sponsor_id": u["sponsor_id"]}, {"sponsor_id": new_sponsor_id}, reason, ctx)

def tree(db, uid, depth=3, limit=500):
    seen = [0]
    def walk(row, d):
        node = {"id": row["id"], "account_id": row["account_id"], "name": row["name"], "referral_code": row["referral_code"], "children": []}
        if d < depth:
            for c in db.execute("SELECT * FROM users WHERE sponsor_id=? ORDER BY id", (row["id"],)).fetchall():
                if seen[0] >= limit: break
                seen[0] += 1; node["children"].append(walk(c, d + 1))
        return node
    return walk(db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone(), 0)

# ---------- commission engine ----------
def preview(cfg, margin):
    pool = margin * cfg["mlm_pool_bp"] // 10000
    levels = [{"level": i + 1, "bp": bp, "amount_paise": pool * bp // 10000} for i, bp in enumerate(cfg["level_bp"])]
    dist = sum(l["amount_paise"] for l in levels)
    return {"net_margin_paise": margin, "mlm_pool_paise": pool, "direct_commission_paise": levels[0]["amount_paise"] if levels else 0,
            "levels": levels, "distributed_paise": dist, "unallocated_pool_paise": pool - dist, "company_retained_margin_paise": margin - dist}

def order_margin(items, discount, total, method, cfg):
    rev = costs = 0
    for it in items:
        line = it["unit_price_paise"] * it["qty"]
        rev += line - line * it["tax_bp"] // (10000 + it["tax_bp"])
        costs += (it["cost_paise"] + it["other_cost_paise"]) * it["qty"]
    gw = total * cfg["gateway_cost_bp"] // 10000 if method == "gateway" else 0
    return rev - discount - costs - gw

def generate_commissions(db, order):
    if db.execute("SELECT 1 FROM commission_runs WHERE order_id=?", (order["id"],)).fetchone(): return
    cfg, margin = get_settings(db), order["net_margin_paise"]
    if order["total_paise"] < cfg["min_order_for_commission_paise"]: margin = 0
    p = preview(cfg, max(margin, 0)); dist = 0
    for i, up in enumerate(upline(db, order["user_id"], len(cfg["level_bp"]))):
        amt = p["levels"][i]["amount_paise"]
        if cfg["max_commission_per_order_paise"]: amt = min(amt, cfg["max_commission_per_order_paise"])
        if amt <= 0 or up["status"] != "active": continue
        if cfg["min_personal_purchase_paise"]:
            spent = db.execute("SELECT COALESCE(SUM(total_paise),0) s FROM orders WHERE user_id=? AND status='delivered' AND created_at>?", (up["id"], now() - 30 * 86400)).fetchone()["s"]
            if spent < cfg["min_personal_purchase_paise"]: continue
        db.execute("INSERT INTO commissions(order_id,beneficiary_id,source_user_id,level,amount_paise,status,release_at,created_at) VALUES(?,?,?,?,?,'pending',?,?)",
                   (order["id"], up["id"], order["user_id"], i + 1, amt, now() + cfg["release_days"] * 86400, now())); dist += amt
    db.execute("INSERT INTO commission_runs VALUES(?,?,?,?,?,?,?)", (order["id"], margin, p["mlm_pool_paise"], dist, margin - dist, json.dumps({k: cfg[k] for k in ("mlm_pool_bp", "level_bp")}), now()))

def release_due(db):
    n = 0
    for c in db.execute("SELECT c.* FROM commissions c JOIN orders o ON o.id=c.order_id WHERE c.status='pending' AND c.release_at<=? AND o.status='delivered'", (now(),)).fetchall():
        post(db, c["beneficiary_id"], "commission", "credit", c["amount_paise"], "commission", c["id"], f"Level {c['level']} commission")
        db.execute("UPDATE commissions SET status='available', released_at=? WHERE id=?", (now(), c["id"])); notify(db, c["beneficiary_id"], "Commission available", f"{inr(c['amount_paise'])} added to your wallet"); n += 1
    return n

def reverse_commissions(db, order_id, reason):
    for c in db.execute("SELECT * FROM commissions WHERE order_id=? AND status IN ('pending','approved','available')", (order_id,)).fetchall():
        if c["status"] == "available":
            post(db, c["beneficiary_id"], "commission_reversal", "debit", c["amount_paise"], "commission", c["id"], reason, allow_negative=True)
            if balance(db, c["beneficiary_id"]) < 0: flag(db, c["beneficiary_id"], "negative_balance", f"Clawback left negative balance (order {order_id})")
            db.execute("UPDATE commissions SET status='clawed_back' WHERE id=?", (c["id"],))
        else:
            db.execute("UPDATE commissions SET status='reversed' WHERE id=?", (c["id"],))

# ---------- checkout ----------
def _coupon(db, code, groups):
    if not code: return None, 0
    c = db.execute("SELECT * FROM coupons WHERE code=? AND active=1", (code.strip().upper(),)).fetchone()
    if not c or (c["expires_at"] and c["expires_at"] < now()) or (c["usage_limit"] and c["used"] >= c["usage_limit"]): raise ApiError("Coupon is invalid or expired", 422)
    base = sum(g["subtotal_paise"] for g in groups if c["applies_to"] in ("all", g["type"]))
    if base < c["min_order_paise"]: raise ApiError(f"Coupon needs a minimum of {inr(c['min_order_paise'])}", 422)
    d = base * c["value"] // 10000 if c["kind"] == "percent" else c["value"]
    if c["max_discount_paise"]: d = min(d, c["max_discount_paise"])
    return c["code"], min(d, base)

def build_quote(db, items, pincode, coupon, method, cfg):
    if not isinstance(items, list) or not items: raise ApiError("Cart is empty", 422)
    qty = {}
    for it in items:
        try: pid, q = int(it["product_id"]), int(it["qty"])
        except (KeyError, TypeError, ValueError): raise ApiError("Invalid cart item", 422)
        if not 1 <= q <= 99: raise ApiError("Quantity must be between 1 and 99", 422)
        qty[pid] = qty.get(pid, 0) + q
    pin = db.execute("SELECT * FROM pincodes WHERE pincode=? AND active=1", (str(pincode),)).fetchone()
    if not pin: raise ApiError("We do not deliver to this PIN code yet", 422)
    gs = {"grocery": {"type": "grocery", "items": [], "subtotal_paise": 0, "tax_paise": 0}, "other": {"type": "other", "items": [], "subtotal_paise": 0, "tax_paise": 0}}
    pkg_min = 0
    for pid, q in qty.items():
        p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p: raise ApiError(f"Product {pid} is not available", 422)
        if p["stock"] < q: raise ApiError(f"Only {p['stock']} left of {p['name']}", 422)
        g = gs["grocery" if p["category"] == "Grocery" else "other"]
        line = p["price_paise"] * q
        g["items"].append({"product_id": pid, "name": p["name"], "category": p["category"], "qty": q, "unit_price_paise": p["price_paise"], "cost_paise": p["cost_paise"],
                           "other_cost_paise": p["other_cost_paise"], "tax_bp": p["tax_bp"], "line_paise": line})
        g["subtotal_paise"] += line; g["tax_paise"] += line * p["tax_bp"] // (10000 + p["tax_bp"])
        if p["min_order_paise"]: pkg_min = max(pkg_min, p["min_order_paise"])
    groups = [g for g in gs.values() if g["items"]]
    code, disc = _coupon(db, coupon, groups)
    elig = [g for g in groups if code and db.execute("SELECT applies_to FROM coupons WHERE code=?", (code,)).fetchone()["applies_to"] in ("all", g["type"])]
    base = sum(g["subtotal_paise"] for g in elig); left = disc
    for g in groups:
        g["discount_paise"] = 0
        if g in elig and base:
            g["discount_paise"] = disc * g["subtotal_paise"] // base; left -= g["discount_paise"]
    if elig: elig[0]["discount_paise"] += left
    for g in groups:
        if g["type"] == "grocery":
            need = max(cfg["grocery_min_paise"], pkg_min); g["minimum_paise"] = need
            if g["subtotal_paise"] < need and not cfg["allow_min_override"]:
                raise ApiError(f"Add {inr(need - g['subtotal_paise'])} more to complete your grocery order.", 422, shortfall_paise=need - g["subtotal_paise"], minimum_paise=need)
            g["delivery_paise"] = 0 if g["subtotal_paise"] >= cfg["free_delivery_paise"] else cfg["grocery_fee_paise"]
        else:
            g["delivery_paise"] = sum(cfg["other_fee_paise"].get(c, 0) for c in {i["category"] for i in g["items"]})
    groups[0]["delivery_paise"] += pin["fee_paise"]
    for g in groups: g["total_paise"] = g["subtotal_paise"] - g["discount_paise"] + g["delivery_paise"]
    return {"groups": groups, "coupon": code, "discount_paise": disc, "delivery_paise": sum(g["delivery_paise"] for g in groups), "grand_total_paise": sum(g["total_paise"] for g in groups)}

def public_quote(q):
    return {**q, "groups": [{**g, "items": [{k: v for k, v in i.items() if k not in ("cost_paise", "other_cost_paise")} for i in g["items"]]} for g in q["groups"]]}

def place_order(db, user, items, address, method, coupon, gid=None, expect_total=None):
    cfg = get_settings(db)
    for k in ("name", "phone", "line1", "pincode"):
        if not str((address or {}).get(k, "")).strip(): raise ApiError(f"Address field '{k}' is required", 422)
    if method not in ("wallet", "gateway"): raise ApiError("payment_method must be wallet or gateway", 422)
    q = build_quote(db, items, address["pincode"], coupon, method, cfg)
    if expect_total is not None and q["grand_total_paise"] != expect_total: raise ApiError("Prices changed. Please review your cart.", 409)
    if method == "wallet" and balance(db, user["id"]) < q["grand_total_paise"]: raise ApiError("Insufficient wallet balance", 402, required_paise=q["grand_total_paise"])
    ref, orders = "CK" + secrets.token_hex(6).upper(), []
    for g in q["groups"]:
        no = "SBZ" + secrets.token_hex(5).upper()
        margin = order_margin(g["items"], g["discount_paise"], g["total_paise"], method, cfg)
        cur = db.execute("INSERT INTO orders(order_no,checkout_ref,user_id,group_type,subtotal_paise,discount_paise,tax_paise,delivery_paise,total_paise,net_margin_paise,payment_method,address_json,pincode,coupon,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (no, ref, user["id"], g["type"], g["subtotal_paise"], g["discount_paise"], g["tax_paise"], g["delivery_paise"], g["total_paise"], margin, method, json.dumps(address), str(address["pincode"]), q["coupon"], now()))
        oid = cur.lastrowid
        for i in g["items"]:
            if db.execute("UPDATE products SET stock=stock-? WHERE id=? AND stock>=?", (i["qty"], i["product_id"], i["qty"])).rowcount != 1: raise ApiError(f"{i['name']} just went out of stock", 409)
            db.execute("INSERT INTO order_items(order_id,product_id,name,qty,unit_price_paise,cost_paise,other_cost_paise,tax_bp) VALUES(?,?,?,?,?,?,?,?)",
                       (oid, i["product_id"], i["name"], i["qty"], i["unit_price_paise"], i["cost_paise"], i["other_cost_paise"], i["tax_bp"]))
        event(db, oid, "pending", "Order placed"); orders.append(no)
        if method == "wallet":
            post(db, user["id"], "order_payment", "debit", g["total_paise"], "order", no, f"Payment for {no}")
            db.execute("UPDATE orders SET payment_status='paid', status='confirmed' WHERE id=?", (oid,)); event(db, oid, "confirmed", "Paid from wallet")
    if q["coupon"]: db.execute("UPDATE coupons SET used=used+1 WHERE code=?", (q["coupon"],))
    out = {"checkout_ref": ref, "orders": orders, "quote": public_quote(q)}
    if method == "gateway":
        gid = gid or "gw_" + uuid.uuid4().hex[:14]
        db.execute("INSERT INTO payments(user_id,purpose,ref,amount_paise,gateway_order_id,created_at) VALUES(?,?,?,?,?,?)", (user["id"], "order", ref, q["grand_total_paise"], gid, now()))
        out["payment"] = {"gateway_order_id": gid, "amount_paise": q["grand_total_paise"]}
    return out

def event(db, oid, status, note=""): db.execute("INSERT INTO order_events(order_id,status,note,at) VALUES(?,?,?,?)", (oid, status, note, now()))

# ---------- payments ----------
def sign(raw): return hmac.new(GATEWAY_SECRET.encode(), raw, hashlib.sha256).hexdigest()

def handle_webhook(db, raw, signature):
    if not signature or not hmac.compare_digest(sign(raw), signature): raise ApiError("Invalid signature", 401)
    return apply_event(db, json.loads(raw))

def apply_event(db, ev):
    pay = db.execute("SELECT * FROM payments WHERE gateway_order_id=?", (ev.get("gateway_order_id"),)).fetchone()
    if not pay: raise ApiError("Unknown payment", 404)
    if pay["status"] != "created": return {"status": "already_processed"}
    if ev.get("event") == "payment.failed":
        db.execute("UPDATE payments SET status='failed' WHERE id=?", (pay["id"],)); return {"status": "failed"}
    if ev.get("event") != "payment.captured" or ev.get("amount_paise") != pay["amount_paise"] or not ev.get("gateway_payment_id"): raise ApiError("Payment verification failed", 422)
    db.execute("UPDATE payments SET status='captured', gateway_payment_id=? WHERE id=?", (ev["gateway_payment_id"], pay["id"]))
    if pay["purpose"] == "topup": post(db, pay["user_id"], "wallet_topup", "credit", pay["amount_paise"], "payment", pay["id"], "Wallet top-up"); notify(db, pay["user_id"], "Wallet topped up", f"{inr(pay['amount_paise'])} added to your wallet")
    else:
        for o in db.execute("SELECT * FROM orders WHERE checkout_ref=? AND payment_status='pending'", (pay["ref"],)).fetchall():
            db.execute("UPDATE orders SET payment_status='paid', status='confirmed' WHERE id=?", (o["id"],)); event(db, o["id"], "confirmed", "Gateway payment verified"); notify(db, o["user_id"], "Payment received", f"Order {o['order_no']} is confirmed")
    return {"status": "captured"}

# ---------- order lifecycle ----------
FLOW = {"pending": ["confirmed", "cancelled"], "confirmed": ["processing", "cancelled"], "processing": ["packed", "cancelled"], "packed": ["shipped", "cancelled"],
        "shipped": ["out_for_delivery", "returned"], "out_for_delivery": ["delivered", "returned"], "delivered": ["returned", "refunded"], "returned": ["refunded"], "cancelled": [], "refunded": []}

def _restock(db, oid):
    for i in db.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall(): db.execute("UPDATE products SET stock=stock+? WHERE id=?", (i["qty"], i["product_id"]))

def set_status(db, order, new, note="", actor=None, ctx=None):
    if new not in FLOW[order["status"]]: raise ApiError(f"Cannot move order from {order['status']} to {new}", 409)
    if new == "confirmed" and order["payment_status"] != "paid": raise ApiError("Order is not paid", 409)
    if new == "refunded": return refund(db, order, note or "Refund", actor, ctx)
    db.execute("UPDATE orders SET status=? WHERE id=?", (new, order["id"])); event(db, order["id"], new, note); notify(db, order["user_id"], f"Order {order['order_no']} {new.replace('_', ' ')}", note)
    if new == "delivered":
        db.execute("UPDATE orders SET delivered_at=? WHERE id=?", (now(), order["id"]))
        generate_commissions(db, db.execute("SELECT * FROM orders WHERE id=?", (order["id"],)).fetchone())
    if new == "cancelled":
        _restock(db, order["id"])
        if order["payment_status"] == "paid":
            post(db, order["user_id"], "refund", "credit", order["total_paise"], "order", order["order_no"], f"Refund for cancelled {order['order_no']}")
            db.execute("UPDATE orders SET payment_status='refunded' WHERE id=?", (order["id"],))
        reverse_commissions(db, order["id"], f"Order {order['order_no']} cancelled")
    audit(db, actor, "order_status", "order", order["order_no"], {"status": order["status"]}, {"status": new}, note, ctx)

def refund(db, order, reason, actor=None, ctx=None):
    cfg = get_settings(db)
    if order["payment_status"] == "paid":
        post(db, order["user_id"], "refund", "credit", order["total_paise"], "order", order["order_no"], f"Refund for {order['order_no']}")
    db.execute("UPDATE orders SET payment_status='refunded', status='refunded' WHERE id=?", (order["id"],)); event(db, order["id"], "refunded", reason); notify(db, order["user_id"], f"Order {order['order_no']} refunded", inr(order["total_paise"]))
    _restock(db, order["id"])
    if cfg["clawback_on_refund"]: reverse_commissions(db, order["id"], f"Order {order['order_no']} refunded")
    n = db.execute("SELECT COUNT(*) c FROM orders WHERE user_id=? AND status='refunded' AND created_at>?", (order["user_id"], now() - 30 * 86400)).fetchone()["c"]
    if n >= cfg["refund_flag_threshold"]: flag(db, order["user_id"], "repeated_refunds", f"{n} refunded orders in 30 days")
    audit(db, actor, "order_refund", "order", order["order_no"], None, {"total_paise": order["total_paise"]}, reason, ctx)

# ---------- payout details & withdrawals ----------
def save_payout(db, uid, data):
    out = {}
    if data.get("upi"):
        if not re.fullmatch(r"[\w.\-]{2,}@[A-Za-z]{2,}", data["upi"]): raise ApiError("Invalid UPI ID", 422)
        out["upi"] = data["upi"]
    b = data.get("bank")
    if b:
        if not (re.fullmatch(r"\d{9,18}", str(b.get("account", ""))) and re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", str(b.get("ifsc", "")).upper()) and len(str(b.get("holder", ""))) >= 2):
            raise ApiError("Invalid bank details (holder, 9-18 digit account, IFSC)", 422)
        out["bank"] = {"holder": b["holder"], "account": str(b["account"]), "ifsc": b["ifsc"].upper(), "bank_name": b.get("bank_name", "")}
    if not out: raise ApiError("Provide a UPI ID or bank details", 422)
    cur = dec(db.execute("SELECT payout_enc FROM users WHERE id=?", (uid,)).fetchone()["payout_enc"]) or {}
    db.execute("UPDATE users SET payout_enc=? WHERE id=?", (enc({**cur, **out}), uid))

def mask_payout(p):
    if not p: return None
    o = {}
    if "upi" in p: o["upi"] = p["upi"][:2] + "***@" + p["upi"].split("@")[1]
    if "bank" in p: o["bank"] = {"holder": p["bank"]["holder"], "account": "****" + p["bank"]["account"][-4:], "ifsc": p["bank"]["ifsc"]}
    return o

def request_withdrawal(db, user, amount, method):
    cfg = get_settings(db)
    if not isinstance(amount, int) or isinstance(amount, bool): raise ApiError("amount_paise must be an integer", 422)
    if not cfg["min_withdraw_paise"] <= amount <= cfg["max_withdraw_paise"]: raise ApiError(f"Withdrawal must be between {inr(cfg['min_withdraw_paise'])} and {inr(cfg['max_withdraw_paise'])}", 422)
    p = dec(db.execute("SELECT payout_enc FROM users WHERE id=?", (user["id"],)).fetchone()["payout_enc"])
    if method not in ("upi", "bank") or not p or method not in p: raise ApiError(f"Save your {method} payout details first", 422)
    fee = cfg["withdraw_fee_flat_paise"] + amount * cfg["withdraw_fee_bp"] // 10000
    if fee >= amount: raise ApiError("Amount does not cover the withdrawal fee", 422)
    today = db.execute("SELECT COALESCE(SUM(amount_paise),0) s FROM withdrawals WHERE user_id=? AND created_at>? AND status NOT IN ('rejected','failed','cancelled')", (user["id"], now() - 86400)).fetchone()["s"]
    if today + amount > cfg["daily_withdraw_limit_paise"]: raise ApiError("Daily withdrawal limit exceeded", 422)
    post(db, user["id"], "withdrawal_hold", "debit", amount, "withdrawal", "new", "Withdrawal requested")
    cur = db.execute("INSERT INTO withdrawals(user_id,amount_paise,fee_paise,method,payout_snapshot,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (user["id"], amount, fee, method, json.dumps(mask_payout({method: p[method]})), now(), now()))
    if now() - user["created_at"] < 86400: flag(db, user["id"], "early_withdrawal", "Withdrawal requested within 24h of signup")
    return cur.lastrowid

WD = {"pending": ["approved", "rejected", "cancelled"], "approved": ["processing", "rejected"], "processing": ["paid", "failed"]}

def move_withdrawal(db, wid, new, actor=None, ref=None, note=None, ctx=None, owner=None):
    w = db.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
    if not w or (owner and w["user_id"] != owner): raise ApiError("Withdrawal not found", 404)
    if new not in WD.get(w["status"], []): raise ApiError(f"Cannot move withdrawal from {w['status']} to {new}", 409)
    if new == "paid" and not ref: raise ApiError("Payment reference is required", 422)
    db.execute("UPDATE withdrawals SET status=?, reference=COALESCE(?,reference), admin_note=COALESCE(?,admin_note), updated_at=? WHERE id=?", (new, ref, note, now(), wid))
    if new in ("rejected", "failed", "cancelled"):
        post(db, w["user_id"], "withdrawal_release", "credit", w["amount_paise"], "withdrawal", wid, f"Withdrawal {new}")
    notify(db, w["user_id"], f"Withdrawal {new}", inr(w["amount_paise"]))
    audit(db, actor, "withdrawal_" + new, "withdrawal", wid, {"status": w["status"]}, {"status": new}, note, ctx)

# ---------- maintenance (run by scheduler / admin) ----------
def expire_unpaid(db, minutes=30):
    """Cancel gateway orders that were never paid, returning stock to inventory."""
    n = 0
    for o in db.execute("SELECT * FROM orders WHERE status='pending' AND payment_method='gateway' AND payment_status='pending' AND created_at<?", (now() - minutes * 60,)).fetchall():
        set_status(db, o, "cancelled", "Payment not completed in time")
        db.execute("UPDATE payments SET status='failed' WHERE ref=? AND status='created'", (o["checkout_ref"],)); n += 1
    return n

def run_maintenance(db): return {"released": release_due(db), "expired": expire_unpaid(db)}
