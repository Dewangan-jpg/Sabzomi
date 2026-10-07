import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ADMIN_PASSWORD"] = "AdminPass123"
import db as D, services as S, seed
from app import create_app

ADDR = {"name": "Rahul", "phone": "9876543210", "line1": "Flat 1", "pincode": "411001"}

class Base(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.app = create_app(self.path); self.c = self.app.test_client()
        k = D.connect(self.path); seed.seed(k); k.close()
        self.admin = self.login("admin@sabzomi.com", "AdminPass123")
        self.n = 0
    def tearDown(self):
        for s in ("", "-wal", "-shm"):
            try: os.remove(self.path + s)
            except OSError: pass
    def call(self, verb, url, tok=None, **j):
        h = {"Authorization": f"Bearer {tok}"} if tok else {}
        r = getattr(self.c, verb)(url, json=j or None, headers=h); return r.status_code, r.get_json()
    def login(self, email, pw="Password123"): return self.call("post", "/api/auth/login", email=email, password=pw)[1]["token"]
    def signup(self, name, ref=None):
        self.n += 1
        s, b = self.call("post", "/api/auth/register", name=name, email=f"{name.lower()}@x.com", phone=f"98{self.n:08d}", password="Password123", referral_code=ref)
        self.assertEqual(s, 201, b); return b["token"], b["user"]
    def fund(self, tok, uid_paise):
        s, b = self.call("post", "/api/wallet/topup", tok, amount_paise=uid_paise); raw = json.dumps({"event": "payment.captured", "gateway_order_id": b["gateway_order_id"], "gateway_payment_id": "pay_" + b["gateway_order_id"], "amount_paise": uid_paise}).encode()
        r = self.c.post("/api/webhooks/payment", data=raw, headers={"X-Signature": S.sign(raw)}); self.assertEqual(r.status_code, 200); return raw
    def pid(self, sku):
        k = D.connect(self.path); v = k.execute("SELECT id FROM products WHERE sku=?", (sku,)).fetchone()[0]; k.close(); return v

class T(Base):
    def test_preview_matches_spec_example(self):
        s, b = self.call("post", "/api/admin/commissions/preview", self.admin, net_margin_paise=20000)
        self.assertEqual((b["mlm_pool_paise"], b["direct_commission_paise"], b["distributed_paise"], b["unallocated_pool_paise"]), (8000, 1600, 7200, 800))
        self.assertEqual([l["amount_paise"] for l in b["levels"]], [1600, 2000, 1600, 1200, 800])

    def test_settings_validation_and_admin_only(self):
        s, b = self.call("put", "/api/admin/settings", self.admin, level_bp=[5000, 4000, 3000]); self.assertEqual(s, 422)
        tok, _ = self.signup("Zed"); self.assertEqual(self.call("get", "/api/admin/settings", tok)[0], 403); self.assertEqual(self.call("get", "/api/admin/settings")[0], 401)

    def test_registration_rules(self):
        self.assertEqual(self.call("post", "/api/auth/register", name="Bad", email="b@x.com", phone="9111111111", password="Password123", referral_code="NOPE")[0], 422)
        self.signup("Amit"); self.assertEqual(self.call("post", "/api/auth/register", name="Amit", email="amit@x.com", phone="9222222222", password="Password123")[0], 409)

    def test_money_flow_commission_release_refund_clawback(self):
        ta, ua = self.signup("Amit"); tb, ub = self.signup("Neha", ua["referral_code"]); tc, uc = self.signup("Rohit", ub["referral_code"])
        self.assertEqual(ua["account_id"], "USR000002")
        atta = self.pid("ATTA5"); tee = self.pid("TEE1")
        # below grocery minimum -> blocked with friendly message
        s, b = self.call("post", "/api/checkout/quote", tc, items=[{"product_id": atta, "qty": 1}], address=ADDR); self.assertEqual(s, 422); self.assertIn("Add ₹", b["error"])
        # unfunded wallet -> 402
        items = [{"product_id": atta, "qty": 5}, {"product_id": tee, "qty": 1}]
        self.assertEqual(self.call("post", "/api/checkout", tc, items=items, address=ADDR, payment_method="wallet")[0], 402)
        self.call("put", "/api/admin/settings", self.admin, release_days=0)
        raw = self.fund(tc, 500000)
        r = self.c.post("/api/webhooks/payment", data=raw, headers={"X-Signature": S.sign(raw)}); self.assertEqual(r.get_json()["status"], "already_processed")
        self.assertEqual(self.c.post("/api/webhooks/payment", data=raw, headers={"X-Signature": "bad"}).status_code, 401)
        self.assertEqual(self.call("get", "/api/wallet", tc)[1]["available_paise"], 500000)
        # mixed cart -> two orders (grocery + other)
        s, b = self.call("post", "/api/checkout", tc, items=items, address=ADDR, payment_method="wallet"); self.assertEqual(s, 201, b); self.assertEqual(len(b["orders"]), 2)
        grocery = next(g for g in b["quote"]["groups"] if g["type"] == "grocery"); self.assertEqual(grocery["delivery_paise"], 0)
        total = b["quote"]["grand_total_paise"]; self.assertEqual(self.call("get", "/api/wallet", tc)[1]["available_paise"], 500000 - total)
        for o in b["orders"]:
            for st in ("processing", "packed", "shipped", "out_for_delivery", "delivered"):
                self.assertEqual(self.call("patch", f"/api/admin/orders/{o}/status", self.admin, status=st)[0], 200)
        gno = next(o for o in b["orders"] if self.call("get", f"/api/orders/{o}", tc)[1]["order"]["group_type"] == "grocery")
        # grocery margin = 5 x (24500-21000-500)=15000 -> pool 6000 -> Neha(L1) 1200, Amit(L2) 1500
        cb = self.call("get", "/api/commissions", tb)[1]["commissions"]; ca = self.call("get", "/api/commissions", ta)[1]["commissions"]
        gcb = [c for c in cb if c["order_no"] == gno][0]; gca = [c for c in ca if c["order_no"] == gno][0]
        self.assertEqual((gcb["amount_paise"], gcb["level"], gca["amount_paise"], gca["level"]), (1200, 1, 1500, 2))
        self.assertEqual(self.call("get", "/api/wallet", tb)[1]["available_paise"], 0)           # pending is not spendable
        self.assertEqual(self.call("get", "/api/wallet", tb)[1]["pending_commission_paise"], gcb["amount_paise"] + sum(c["amount_paise"] for c in cb if c["order_no"] != gno))
        self.call("post", "/api/admin/commissions/release", self.admin)
        avail = self.call("get", "/api/wallet", tb)[1]["available_paise"]; self.assertGreaterEqual(avail, 1200)
        # refund grocery order -> commissions clawed back
        self.assertEqual(self.call("patch", f"/api/admin/orders/{gno}/status", self.admin, status="refunded")[0], 200)
        self.assertEqual(self.call("get", "/api/wallet", tb)[1]["available_paise"], avail - 1200)
        self.assertIn("clawed_back", [c["status"] for c in self.call("get", "/api/commissions", tb)[1]["commissions"] if c["order_no"] == gno])

    def test_withdrawals(self):
        t, u = self.signup("Amit"); self.fund(t, 300000)
        self.assertEqual(self.call("post", "/api/withdrawals", t, amount_paise=100000, method="upi")[0], 422)  # no payout saved
        self.assertEqual(self.call("put", "/api/me/payout", t, upi="amit@okaxis")[0], 200)
        self.assertEqual(self.call("post", "/api/withdrawals", t, amount_paise=100, method="upi")[0], 422)     # below minimum
        self.assertEqual(self.call("post", "/api/withdrawals", t, amount_paise=9000000, method="upi")[0], 422)
        s, b = self.call("post", "/api/withdrawals", t, amount_paise=100000, method="upi"); self.assertEqual(s, 201)
        w = self.call("get", "/api/wallet", t)[1]; self.assertEqual((w["available_paise"], w["withdrawals_in_process_paise"], w["current_balance_paise"]), (200000, 100000, 300000))
        self.assertEqual(self.call("post", f"/api/admin/withdrawals/{b['id']}", self.admin, status="rejected", note="test")[0], 200)
        self.assertEqual(self.call("get", "/api/wallet", t)[1]["available_paise"], 300000)
        s, b = self.call("post", "/api/withdrawals", t, amount_paise=50000, method="upi")
        for st, extra in (("approved", {}), ("processing", {}), ("paid", {"reference": "UTR123"})): self.assertEqual(self.call("post", f"/api/admin/withdrawals/{b['id']}", self.admin, status=st, **extra)[0], 200)
        self.assertEqual(self.call("get", "/api/wallet", t)[1]["total_withdrawn_paise"], 50000)
        self.assertEqual(self.call("get", "/api/me", t)[1]["payout"]["upi"], "am***@okaxis")                      # masked
        self.assertTrue(any(f["kind"] == "early_withdrawal" for f in self.call("get", "/api/admin/risk-flags", self.admin)[1]["flags"]))

    def test_ledger_and_audit_are_immutable(self):
        t, u = self.signup("Amit"); self.fund(t, 20000); k = D.connect(self.path)
        with self.assertRaises(Exception): k.execute("UPDATE ledger SET amount_paise=1")
        with self.assertRaises(Exception): k.execute("DELETE FROM ledger")
        self.call("put", "/api/admin/settings", self.admin, release_days=3)
        with self.assertRaises(Exception): k.execute("DELETE FROM audit_log")
        self.assertTrue(self.call("get", "/api/admin/audit-log", self.admin)[1]["entries"])

    def test_network_integrity_and_privacy(self):
        ta, ua = self.signup("Amit"); tb, ub = self.signup("Neha", ua["referral_code"]); tc, uc = self.signup("Rohit", ub["referral_code"])
        s, b = self.call("post", f"/api/admin/users/{ua['id']}/sponsor", self.admin, sponsor_id=uc["id"], reason="x"); self.assertEqual(s, 422)   # circular
        self.assertEqual(self.call("post", f"/api/admin/users/{uc['id']}/sponsor", self.admin, sponsor_id=ua["id"])[0], 422)                      # reason required
        self.assertEqual(self.call("post", f"/api/admin/users/{uc['id']}/sponsor", self.admin, sponsor_id=ua["id"], reason="Support ticket 42")[0], 200)
        self.assertEqual(self.call("get", "/api/referrals/stats", ta)[1]["direct"], 2)
        p = self.call("get", "/api/products")[1]["products"][0]; self.assertNotIn("cost_paise", p)
        self.assertIn("internal_admin_only", self.call("get", "/api/admin/products", self.admin)[1]["products"][0])

if __name__ == "__main__": unittest.main()
