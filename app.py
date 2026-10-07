"""Sabzomi backend API (Flask). Run: python app.py   |   Money fields are integer paise (*_paise)."""
import json, os, time
from functools import wraps
import jwt
from flask import Flask, g, jsonify, request
import db as D
import services as S
from services import ApiError

def create_app(db_path=None):
    app = Flask(__name__)
    path = db_path or D.DB_PATH
    boot = D.connect(path); D.init_db(boot); boot.close()
    if os.environ.get("SABZOMI_ENV") == "production" and (S.SECRET.startswith("dev-") or S.GATEWAY_SECRET.startswith("dev-")):
        raise RuntimeError("Set SABZOMI_SECRET and SABZOMI_GATEWAY_SECRET in production")

    def conn():
        if "db" not in g: g.db = D.connect(path)
        return g.db
    @app.teardown_appcontext
    def _close(_):
        c = g.pop("db", None)
        if c: c.close()
    @app.after_request
    def _hdr(r):
        r.headers.update({"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
        o = os.environ.get("CORS_ORIGIN")
        if o: r.headers.update({"Access-Control-Allow-Origin": o, "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS"})
        return r
    @app.errorhandler(ApiError)
    def _api(e): return jsonify(error=e.msg, **e.extra), e.status
    @app.errorhandler(404)
    def _404(e): return jsonify(error="Not found"), 404
    @app.errorhandler(405)
    def _405(e): return jsonify(error="Method not allowed"), 405
    @app.errorhandler(Exception)
    def _500(e):
        app.logger.exception(e); return jsonify(error="Something went wrong. Please try again."), 500

    ctx = lambda: {"ip": request.remote_addr, "ua": request.headers.get("User-Agent", "")[:200]}
    body = lambda: request.get_json(silent=True) or {}
    W = lambda: D.tx(conn())
    def auth(admin=False):
        def deco(f):
            @wraps(f)
            def inner(*a, **k):
                h = request.headers.get("Authorization", "")
                try: claims = jwt.decode(h[7:], S.SECRET, algorithms=["HS256"]) if h.startswith("Bearer ") else None
                except jwt.PyJWTError: claims = None
                u = conn().execute("SELECT * FROM users WHERE id=?", (claims["sub"],)).fetchone() if claims else None
                if not u or u["status"] != "active": raise ApiError("Authentication required", 401)
                if admin and u["role"] != "admin": raise ApiError("Admin access required", 403)
                g.user = u; return f(*a, **k)
            return inner
        return deco
    def page():
        return min(int(request.args.get("limit", 50)), 100), max(int(request.args.get("offset", 0)), 0)
    def rows(sql, args=()): return [dict(r) for r in conn().execute(sql, args).fetchall()]
    def me_view(u):
        return {k: u[k] for k in ("id", "account_id", "name", "email", "phone", "referral_code", "status", "role", "depth", "created_at")}

    # ---------- auth ----------
    @app.get("/api/health")
    def health(): return jsonify(ok=True)

    @app.post("/api/auth/register")
    def register():
        b = body()
        with W() as db:
            uid = S.create_user(db, b.get("name"), b.get("email"), b.get("phone"), b.get("password"), b.get("referral_code"), request.remote_addr)
        return _token(uid), 201

    def _token(uid):
        u = conn().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        t = jwt.encode({"sub": u["id"], "exp": int(time.time()) + 12 * 3600}, S.SECRET, algorithm="HS256")
        return jsonify(token=t, user=me_view(u))

    @app.post("/api/auth/login")
    def login():
        b = body(); db = conn()
        u = db.execute("SELECT * FROM users WHERE email=?", ((b.get("email") or "").strip().lower(),)).fetchone()
        if u and u["locked_until"] > time.time(): raise ApiError("Too many attempts. Try again later.", 429)
        ok = bool(u) and S.check_pw(b.get("password") or "", u["pw_hash"])
        if u:
            with W() as t:
                if ok: t.execute("UPDATE users SET failed_logins=0 WHERE id=?", (u["id"],))
                else:
                    n = u["failed_logins"] + 1
                    t.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (0 if n >= 5 else n, time.time() + 900 if n >= 5 else 0, u["id"]))
        if not ok or u["status"] != "active": raise ApiError("Invalid email or password", 401)
        return _token(u["id"])

    @app.get("/api/me")
    @auth()
    def me():
        return jsonify(user=me_view(g.user), payout=S.mask_payout(S.dec(g.user["payout_enc"])), referral_link=f"/register?ref={g.user['referral_code']}")

    @app.put("/api/me/payout")
    @auth()
    def payout():
        with W() as db: S.save_payout(db, g.user["id"], body())
        return jsonify(ok=True)

    # ---------- catalog (customers never see cost/margin) ----------
    PUB = "id,sku,name,category,sub,unit,price_paise,mrp_paise,stock,image"
    @app.get("/api/products")
    def products():
        lim, off = page(); sql, a = f"SELECT {PUB} FROM products WHERE active=1", []
        for col in ("category", "sub"):
            if request.args.get(col): sql += f" AND {col}=?"; a.append(request.args[col])
        if request.args.get("q"): sql += " AND name LIKE ?"; a.append(f"%{request.args['q']}%")
        return jsonify(products=rows(sql + " ORDER BY id LIMIT ? OFFSET ?", a + [lim, off]))

    @app.get("/api/products/<int:pid>")
    def product(pid):
        r = conn().execute(f"SELECT {PUB} FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not r: raise ApiError("Product not found", 404)
        return jsonify(dict(r))

    # ---------- checkout / orders ----------
    @app.post("/api/checkout/quote")
    @auth()
    def quote():
        b = body(); q = S.build_quote(conn(), b.get("items"), (b.get("address") or {}).get("pincode", b.get("pincode")), b.get("coupon"), b.get("payment_method", "wallet"), D.get_settings(conn()))
        return jsonify(S.public_quote(q))

    @app.post("/api/checkout")
    @auth()
    def checkout():
        b = body()
        with W() as db: out = S.place_order(db, g.user, b.get("items"), b.get("address"), b.get("payment_method"), b.get("coupon"))
        return jsonify(out), 201

    def own_order(no):
        o = conn().execute("SELECT * FROM orders WHERE order_no=?", (no,)).fetchone()
        if not o or (o["user_id"] != g.user["id"] and g.user["role"] != "admin"): raise ApiError("Order not found", 404)
        return o
    @app.get("/api/orders")
    @auth()
    def orders():
        lim, off = page()
        return jsonify(orders=rows("SELECT order_no,group_type,subtotal_paise,discount_paise,delivery_paise,total_paise,payment_status,status,created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (g.user["id"], lim, off)))

    @app.get("/api/orders/<no>")
    @auth()
    def order(no):
        o = dict(own_order(no)); o.pop("net_margin_paise")
        if g.user["role"] == "admin": o["net_margin_paise"] = own_order(no)["net_margin_paise"]
        items = rows("SELECT product_id,name,qty,unit_price_paise FROM order_items WHERE order_id=?", (o["id"],))
        return jsonify(order=o, items=items, events=rows("SELECT status,note,at FROM order_events WHERE order_id=? ORDER BY id", (o["id"],)))

    @app.post("/api/orders/<no>/cancel")
    @auth()
    def cancel(no):
        o = own_order(no)
        if o["status"] not in ("pending", "confirmed"): raise ApiError("This order can no longer be cancelled", 409)
        with W() as db: S.set_status(db, o, "cancelled", "Cancelled by customer", g.user["id"], ctx())
        return jsonify(ok=True)

    @app.post("/api/orders/<no>/returns")
    @auth()
    def ask_return(no):
        o, b, cfg = own_order(no), body(), D.get_settings(conn())
        if o["status"] != "delivered": raise ApiError("Only delivered orders can be returned", 409)
        if time.time() - (o["delivered_at"] or 0) > cfg["return_window_days"] * 86400: raise ApiError("Return window has closed", 409)
        if b.get("kind") not in ("return", "refund", "replacement"): raise ApiError("kind must be return, refund or replacement", 422)
        with W() as db:
            if db.execute("SELECT 1 FROM returns WHERE order_id=? AND status IN ('requested','approved')", (o["id"],)).fetchone(): raise ApiError("A request is already open", 409)
            db.execute("INSERT INTO returns(order_id,user_id,kind,reason,created_at) VALUES(?,?,?,?,?)", (o["id"], g.user["id"], b["kind"], (b.get("reason") or "")[:500], time.time()))
        return jsonify(ok=True), 201

    # ---------- wallet & payments ----------
    def wallet_summary(uid):
        db = conn(); q = lambda sql, *a: db.execute(sql, a).fetchone()[0] or 0
        avail = S.balance(db, uid); held = q("SELECT SUM(amount_paise) FROM withdrawals WHERE user_id=? AND status IN ('pending','approved','processing')", uid)
        led = lambda t, d="credit": q("SELECT SUM(amount_paise) FROM ledger WHERE user_id=? AND type=? AND direction=?", uid, t, d)
        return {"available_paise": avail, "current_balance_paise": avail + held, "pending_commission_paise": q("SELECT SUM(amount_paise) FROM commissions WHERE beneficiary_id=? AND status IN ('pending','approved')", uid),
                "withdrawals_in_process_paise": held, "total_commissions_paise": led("commission") - led("commission_reversal", "debit"), "total_added_paise": led("wallet_topup"),
                "total_withdrawn_paise": q("SELECT SUM(amount_paise) FROM withdrawals WHERE user_id=? AND status='paid'", uid), "total_refunds_adjustments_paise": led("refund") + led("admin_credit") - led("admin_debit", "debit")}
    @app.get("/api/wallet")
    @auth()
    def wallet(): return jsonify(wallet_summary(g.user["id"]))

    @app.get("/api/wallet/transactions")
    @auth()
    def txns():
        lim, off = page()
        return jsonify(transactions=rows("SELECT txn_id,type,direction,amount_paise,currency,status,ref_type,ref_id,description,created_at FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (g.user["id"], lim, off)))

    @app.post("/api/wallet/topup")
    @auth()
    def topup():
        amt = body().get("amount_paise")
        if not isinstance(amt, int) or isinstance(amt, bool) or not 10000 <= amt <= 5000000: raise ApiError("Amount must be between ₹100 and ₹50,000", 422)
        gid = "gw_" + os.urandom(7).hex()
        with W() as db: db.execute("INSERT INTO payments(user_id,purpose,ref,amount_paise,gateway_order_id,created_at) VALUES(?,?,?,?,?,?)", (g.user["id"], "topup", "wallet", amt, gid, time.time()))
        return jsonify(gateway_order_id=gid, amount_paise=amt), 201

    @app.post("/api/webhooks/payment")
    def webhook():
        raw = request.get_data()
        with W() as db: return jsonify(S.handle_webhook(db, raw, request.headers.get("X-Signature")))

    # ---------- withdrawals ----------
    @app.post("/api/withdrawals")
    @auth()
    def withdraw():
        b = body()
        with W() as db: wid = S.request_withdrawal(db, g.user, b.get("amount_paise"), b.get("method"))
        return jsonify(id=wid, status="pending"), 201

    @app.get("/api/withdrawals")
    @auth()
    def my_withdrawals(): return jsonify(withdrawals=rows("SELECT id,amount_paise,fee_paise,method,payout_snapshot,status,reference,created_at FROM withdrawals WHERE user_id=? ORDER BY id DESC", (g.user["id"],)))

    @app.post("/api/withdrawals/<int:wid>/cancel")
    @auth()
    def cancel_wd(wid):
        with W() as db: S.move_withdrawal(db, wid, "cancelled", g.user["id"], ctx=ctx(), owner=g.user["id"])
        return jsonify(ok=True)

    # ---------- referral ----------
    @app.get("/api/referrals/tree")
    @auth()
    def ref_tree(): return jsonify(S.tree(conn(), g.user["id"], min(int(request.args.get("depth", 3)), 5)))

    @app.get("/api/referrals/stats")
    @auth()
    def ref_stats():
        u = g.user; by = rows("SELECT depth-? AS level, COUNT(*) members FROM users WHERE path LIKE ? AND id!=? GROUP BY depth ORDER BY depth", (u["depth"], u["path"] + "%", u["id"]))
        act = conn().execute("SELECT COUNT(*) FROM users WHERE path LIKE ? AND id!=? AND status='active'", (u["path"] + "%", u["id"])).fetchone()[0]
        return jsonify(direct=sum(r["members"] for r in by if r["level"] == 1), total_downline=sum(r["members"] for r in by), active_downline=act, levels=by)

    @app.get("/api/commissions")
    @auth()
    def my_commissions():
        lim, off = page()
        return jsonify(commissions=rows("SELECT c.id,c.level,c.amount_paise,c.status,c.release_at,c.created_at,o.order_no FROM commissions c JOIN orders o ON o.id=c.order_id WHERE c.beneficiary_id=? ORDER BY c.id DESC LIMIT ? OFFSET ?", (g.user["id"], lim, off)))

    @app.get("/api/dashboard")
    @auth()
    def dashboard():
        u = g.user
        return jsonify(user=me_view(u), wallet=wallet_summary(u["id"]), recent_orders=rows("SELECT order_no,total_paise,status,created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5", (u["id"],)),
                       downline=conn().execute("SELECT COUNT(*) FROM users WHERE path LIKE ? AND id!=?", (u["path"] + "%", u["id"])).fetchone()[0])

    # ================= ADMIN =================
    A = lambda: auth(admin=True)
    @app.get("/api/admin/settings")
    @A()
    def get_cfg(): return jsonify(D.get_settings(conn()))

    @app.put("/api/admin/settings")
    @A()
    def put_cfg():
        with W() as db:
            old = D.get_settings(db); new = S.validate_settings({**old, **body()})
            D.save_settings(db, new); S.audit(db, g.user["id"], "settings_update", "settings", "config", old, new, body().get("_reason"), ctx())
        return jsonify(new)

    @app.post("/api/admin/commissions/preview")
    @A()
    def comm_preview():
        b = body(); cfg = S.validate_settings({**D.get_settings(conn()), **{k: v for k, v in b.items() if k in ("mlm_pool_bp", "level_bp")}})
        m = b.get("net_margin_paise")
        if not isinstance(m, int) or m < 0: raise ApiError("net_margin_paise must be a non-negative integer", 422)
        return jsonify(S.preview(cfg, m))

    @app.post("/api/admin/commissions/release")
    @A()
    def release():
        with W() as db: n = S.release_due(db); S.audit(db, g.user["id"], "commission_release", None, None, None, {"released": n}, None, ctx())
        return jsonify(released=n)

    @app.get("/api/admin/commissions")
    @A()
    def adm_comm():
        lim, off = page()
        return jsonify(commissions=rows("SELECT c.*,o.order_no,b.account_id beneficiary FROM commissions c JOIN orders o ON o.id=c.order_id JOIN users b ON b.id=c.beneficiary_id ORDER BY c.id DESC LIMIT ? OFFSET ?", (lim, off)),
                       runs=rows("SELECT * FROM commission_runs ORDER BY order_id DESC LIMIT 20"))

    @app.post("/api/admin/commissions/<int:cid>/cancel")
    @A()
    def cancel_comm(cid):
        with W() as db:
            c = db.execute("SELECT * FROM commissions WHERE id=?", (cid,)).fetchone()
            if not c or c["status"] not in ("pending", "approved"): raise ApiError("Only pending commissions can be cancelled", 409)
            db.execute("UPDATE commissions SET status='cancelled' WHERE id=?", (cid,)); S.audit(db, g.user["id"], "commission_cancel", "commission", cid, {"status": c["status"]}, {"status": "cancelled"}, body().get("reason"), ctx())
        return jsonify(ok=True)

    PF = ("sku", "name", "category", "sub", "unit", "price_paise", "mrp_paise", "cost_paise", "other_cost_paise", "tax_bp", "stock", "min_order_paise", "image", "active")
    def prod_admin(p):
        cfg = D.get_settings(conn()); p = dict(p); tax = p["price_paise"] * p["tax_bp"] // (10000 + p["tax_bp"])
        margin = p["price_paise"] - tax - p["cost_paise"] - p["other_cost_paise"]
        return {**p, "internal_admin_only": {"net_margin_paise": margin, **S.preview(cfg, max(margin, 0))}}
    @app.get("/api/admin/products")
    @A()
    def adm_products(): return jsonify(products=[prod_admin(r) for r in conn().execute("SELECT * FROM products ORDER BY id").fetchall()])

    @app.post("/api/admin/products")
    @A()
    def add_product():
        b = body()
        for k in ("sku", "name", "category", "price_paise", "mrp_paise"):
            if b.get(k) in (None, ""): raise ApiError(f"{k} is required", 422)
        for k in PF:
            if k.endswith(("_paise", "_bp")) or k == "stock":
                if k in b and (not isinstance(b[k], int) or isinstance(b[k], bool) or b[k] < 0): raise ApiError(f"{k} must be a non-negative integer", 422)
        try:
            with W() as db:
                cols = [k for k in PF if k in b]
                cur = db.execute(f"INSERT INTO products({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [b[k] for k in cols])
                S.audit(db, g.user["id"], "product_create", "product", cur.lastrowid, None, b, None, ctx())
        except Exception as e:
            if "UNIQUE" in str(e): raise ApiError("SKU already exists", 409)
            raise
        return jsonify(prod_admin(conn().execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone())), 201

    @app.patch("/api/admin/products/<int:pid>")
    @A()
    def edit_product(pid):
        b = body(); cols = [k for k in PF if k in b]
        with W() as db:
            old = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not old: raise ApiError("Product not found", 404)
            if cols: db.execute(f"UPDATE products SET {','.join(k + '=?' for k in cols)} WHERE id=?", [b[k] for k in cols] + [pid])
            S.audit(db, g.user["id"], "product_update", "product", pid, dict(old), b, None, ctx())
        return jsonify(prod_admin(conn().execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()))

    @app.get("/api/admin/users")
    @A()
    def adm_users():
        lim, off = page(); q = f"%{request.args.get('q', '')}%"
        return jsonify(users=rows("SELECT id,account_id,name,email,phone,role,status,referral_code,sponsor_id,depth,created_at FROM users WHERE name LIKE ? OR email LIKE ? OR account_id LIKE ? OR referral_code LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?", (q, q, q, q, lim, off)))

    @app.patch("/api/admin/users/<int:uid>/status")
    @A()
    def user_status(uid):
        b = body()
        if b.get("status") not in ("active", "suspended", "blocked"): raise ApiError("status must be active, suspended or blocked", 422)
        with W() as db:
            u = db.execute("SELECT status FROM users WHERE id=?", (uid,)).fetchone()
            if not u: raise ApiError("User not found", 404)
            db.execute("UPDATE users SET status=? WHERE id=?", (b["status"], uid)); S.audit(db, g.user["id"], "user_status", "user", uid, {"status": u["status"]}, {"status": b["status"]}, b.get("reason"), ctx())
        return jsonify(ok=True)

    @app.post("/api/admin/users/<int:uid>/sponsor")
    @A()
    def sponsor(uid):
        b = body()
        with W() as db: S.change_sponsor(db, g.user["id"], uid, b.get("sponsor_id"), b.get("reason"), ctx())
        return jsonify(ok=True)

    @app.post("/api/admin/users/<int:uid>/wallet-adjust")
    @A()
    def wallet_adjust(uid):
        b = body()
        if b.get("direction") not in ("credit", "debit") or not b.get("reason"): raise ApiError("direction (credit/debit) and a reason are required", 422)
        with W() as db:
            txn = S.post(db, uid, "admin_" + b["direction"], b["direction"], b.get("amount_paise"), "admin", g.user["id"], b["reason"])
            S.audit(db, g.user["id"], "wallet_adjust", "user", uid, None, {"txn": txn, "amount_paise": b["amount_paise"], "direction": b["direction"]}, b["reason"], ctx())
        return jsonify(txn_id=txn)

    @app.get("/api/admin/orders")
    @A()
    def adm_orders():
        lim, off = page(); st = request.args.get("status")
        return jsonify(orders=rows("SELECT * FROM orders" + (" WHERE status=?" if st else "") + " ORDER BY id DESC LIMIT ? OFFSET ?", ([st] if st else []) + [lim, off]))

    @app.patch("/api/admin/orders/<no>/status")
    @A()
    def order_status(no):
        b = body()
        with W() as db:
            o = db.execute("SELECT * FROM orders WHERE order_no=?", (no,)).fetchone()
            if not o: raise ApiError("Order not found", 404)
            S.set_status(db, o, b.get("status"), b.get("note", ""), g.user["id"], ctx())
        return jsonify(ok=True)

    @app.get("/api/admin/returns")
    @A()
    def adm_returns(): return jsonify(returns=rows("SELECT r.*,o.order_no FROM returns r JOIN orders o ON o.id=r.order_id ORDER BY r.id DESC LIMIT 100"))

    @app.post("/api/admin/returns/<int:rid>")
    @A()
    def process_return(rid):
        b = body(); act = b.get("action")
        with W() as db:
            r = db.execute("SELECT * FROM returns WHERE id=?", (rid,)).fetchone()
            if not r or act not in ("approve", "reject", "refund"): raise ApiError("Return not found or invalid action", 404 if not r else 422)
            if r["status"] not in ("requested", "approved"): raise ApiError("Return is already closed", 409)
            if act == "refund":
                o = db.execute("SELECT * FROM orders WHERE id=?", (r["order_id"],)).fetchone()
                if o["status"] == "delivered": S.set_status(db, o, "returned", "Return received", g.user["id"], ctx()); o = db.execute("SELECT * FROM orders WHERE id=?", (r["order_id"],)).fetchone()
                S.set_status(db, o, "refunded", b.get("note", "Return refund"), g.user["id"], ctx())
            db.execute("UPDATE returns SET status=?, admin_note=? WHERE id=?", ({"approve": "approved", "reject": "rejected", "refund": "refunded"}[act], b.get("note"), rid))
        return jsonify(ok=True)

    @app.get("/api/admin/withdrawals")
    @A()
    def adm_wd():
        st = request.args.get("status")
        return jsonify(withdrawals=rows("SELECT w.*,u.account_id FROM withdrawals w JOIN users u ON u.id=w.user_id" + (" WHERE w.status=?" if st else "") + " ORDER BY w.id DESC LIMIT 100", [st] if st else []))

    @app.post("/api/admin/withdrawals/<int:wid>")
    @A()
    def process_wd(wid):
        b = body()
        with W() as db: S.move_withdrawal(db, wid, b.get("status"), g.user["id"], b.get("reference"), b.get("note"), ctx())
        return jsonify(ok=True)

    @app.get("/api/admin/withdrawals/<int:wid>/payout-details")
    @A()
    def wd_details(wid):
        with W() as db:
            w = db.execute("SELECT w.*,u.payout_enc FROM withdrawals w JOIN users u ON u.id=w.user_id WHERE w.id=?", (wid,)).fetchone()
            if not w: raise ApiError("Withdrawal not found", 404)
            S.audit(db, g.user["id"], "payout_details_viewed", "withdrawal", wid, None, None, None, ctx())
        return jsonify(S.dec(w["payout_enc"]) or {})

    @app.get("/api/admin/risk-flags")
    @A()
    def flags(): return jsonify(flags=rows("SELECT * FROM risk_flags ORDER BY id DESC LIMIT 100"))

    @app.post("/api/admin/risk-flags/<int:fid>/resolve")
    @A()
    def resolve_flag(fid):
        with W() as db: db.execute("UPDATE risk_flags SET status='reviewed' WHERE id=?", (fid,)); S.audit(db, g.user["id"], "risk_flag_reviewed", "risk_flag", fid, None, None, body().get("note"), ctx())
        return jsonify(ok=True)

    @app.get("/api/admin/audit-log")
    @A()
    def audit_log():
        lim, off = page(); return jsonify(entries=rows("SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?", (lim, off)))

    @app.put("/api/admin/pincodes/<pin>")
    @A()
    def put_pin(pin):
        b = body()
        with W() as db: db.execute("INSERT INTO pincodes(pincode,fee_paise,active) VALUES(?,?,?) ON CONFLICT(pincode) DO UPDATE SET fee_paise=excluded.fee_paise, active=excluded.active", (pin, int(b.get("fee_paise", 0)), int(b.get("active", 1))))
        return jsonify(ok=True)

    @app.put("/api/admin/coupons/<code>")
    @A()
    def put_coupon(code):
        b = body()
        if b.get("kind") not in ("percent", "flat") or not isinstance(b.get("value"), int): raise ApiError("kind (percent|flat) and integer value required (percent uses basis points)", 422)
        with W() as db:
            db.execute("INSERT INTO coupons(code,kind,value,min_order_paise,max_discount_paise,applies_to,usage_limit,expires_at,active) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET kind=excluded.kind,value=excluded.value,min_order_paise=excluded.min_order_paise,max_discount_paise=excluded.max_discount_paise,applies_to=excluded.applies_to,usage_limit=excluded.usage_limit,expires_at=excluded.expires_at,active=excluded.active",
                       (code.upper(), b["kind"], b["value"], b.get("min_order_paise", 0), b.get("max_discount_paise", 0), b.get("applies_to", "all"), b.get("usage_limit", 0), b.get("expires_at"), int(b.get("active", 1))))
        return jsonify(ok=True)

    @app.get("/api/admin/dashboard")
    @A()
    def adm_dash():
        q = lambda sql, *a: conn().execute(sql, a).fetchone()[0] or 0
        return jsonify(total_users=q("SELECT COUNT(*) FROM users WHERE role='user'"), total_orders=q("SELECT COUNT(*) FROM orders"),
                       total_sales_paise=q("SELECT SUM(total_paise) FROM orders WHERE payment_status='paid'"), total_commission_paise=q("SELECT SUM(distributed_paise) FROM commission_runs"),
                       pending_withdrawals=q("SELECT COUNT(*) FROM withdrawals WHERE status IN ('pending','approved','processing')"), open_risk_flags=q("SELECT COUNT(*) FROM risk_flags WHERE status='open'"),
                       sales_last_7_days=rows("SELECT date(created_at,'unixepoch') day, SUM(total_paise) total_paise, COUNT(*) orders FROM orders WHERE payment_status='paid' AND created_at>? GROUP BY day ORDER BY day", (time.time() - 7 * 86400,)))
    return app

if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000))) 
    app = create_app()
