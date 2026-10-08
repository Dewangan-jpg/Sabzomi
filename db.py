"""Sabzomi backend: database layer. All money is stored as INTEGER paise (no floats)."""
import json, os, sqlite3, time
from contextlib import contextmanager

def _load_env():
    """Read KEY=VALUE lines from a .env file next to the code (real environment variables win)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("sabzomi.env", ".env"):          # "sabzomi.env" is visible in cPanel File Manager; ".env" also works
        p = os.path.join(here, name)
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))
_load_env()

DB_PATH = os.environ.get("SABZOMI_DB", "sabzomi.db")

# Every business rule below is a default only; admins change them via /api/admin/settings.
DEFAULTS = {
    "currency": "INR",
    "grocery_min_paise": 99900, "allow_min_override": False,
    "free_delivery_paise": 99900, "grocery_fee_paise": 8000,
    "other_fee_paise": {"Electronics": 6000, "Clothing": 5000, "Decor": 5000},
    "mlm_pool_bp": 4000,                      # 40% of net margin -> MLM pool
    "level_bp": [2000, 2500, 2000, 1500, 1000],  # % of POOL per level (level 1 = direct referrer, 20%)
    "max_commission_per_order_paise": 0,      # 0 = no cap
    "min_order_for_commission_paise": 0, "min_personal_purchase_paise": 0,
    "release_days": 7, "clawback_on_refund": True, "gateway_cost_bp": 200,
    "min_withdraw_paise": 50000, "max_withdraw_paise": 5000000,
    "withdraw_fee_flat_paise": 0, "withdraw_fee_bp": 0, "daily_withdraw_limit_paise": 5000000,
    "referral_prefix": "SB", "max_signups_per_ip_day": 3, "refund_flag_threshold": 3, "return_window_days": 7,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, account_id TEXT UNIQUE, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
  phone TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
  status TEXT NOT NULL DEFAULT 'active', referral_code TEXT UNIQUE NOT NULL,
  sponsor_id INTEGER REFERENCES users(id), depth INTEGER NOT NULL DEFAULT 0, path TEXT NOT NULL DEFAULT '/',
  failed_logins INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0,
  reg_ip TEXT, payout_enc TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_users_sponsor ON users(sponsor_id);
CREATE INDEX IF NOT EXISTS ix_users_path ON users(path);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL, sub TEXT DEFAULT '',
  unit TEXT DEFAULT '', price_paise INTEGER NOT NULL CHECK(price_paise>=0), mrp_paise INTEGER NOT NULL,
  cost_paise INTEGER NOT NULL DEFAULT 0, other_cost_paise INTEGER NOT NULL DEFAULT 0, tax_bp INTEGER NOT NULL DEFAULT 0,
  stock INTEGER NOT NULL DEFAULT 0 CHECK(stock>=0), min_order_paise INTEGER, image TEXT, active INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS ix_products_cat ON products(category, sub);
CREATE TABLE IF NOT EXISTS pincodes(pincode TEXT PRIMARY KEY, fee_paise INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS coupons(code TEXT PRIMARY KEY, kind TEXT NOT NULL, value INTEGER NOT NULL,
  min_order_paise INTEGER NOT NULL DEFAULT 0, max_discount_paise INTEGER NOT NULL DEFAULT 0, applies_to TEXT NOT NULL DEFAULT 'all',
  usage_limit INTEGER NOT NULL DEFAULT 0, used INTEGER NOT NULL DEFAULT 0, expires_at REAL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, order_no TEXT UNIQUE NOT NULL, checkout_ref TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
  group_type TEXT NOT NULL, subtotal_paise INTEGER NOT NULL, discount_paise INTEGER NOT NULL DEFAULT 0,
  tax_paise INTEGER NOT NULL DEFAULT 0, delivery_paise INTEGER NOT NULL DEFAULT 0, total_paise INTEGER NOT NULL,
  net_margin_paise INTEGER NOT NULL DEFAULT 0, payment_method TEXT NOT NULL, payment_status TEXT NOT NULL DEFAULT 'pending',
  status TEXT NOT NULL DEFAULT 'pending', address_json TEXT NOT NULL, pincode TEXT NOT NULL, coupon TEXT,
  created_at REAL NOT NULL, delivered_at REAL);
CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(user_id, created_at);
CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), product_id INTEGER NOT NULL,
  name TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty>0), unit_price_paise INTEGER NOT NULL, cost_paise INTEGER NOT NULL,
  other_cost_paise INTEGER NOT NULL, tax_bp INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS order_events(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, status TEXT NOT NULL, note TEXT, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, purpose TEXT NOT NULL, ref TEXT NOT NULL,
  amount_paise INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'created', gateway_order_id TEXT UNIQUE NOT NULL,
  gateway_payment_id TEXT UNIQUE, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY, txn_id TEXT UNIQUE NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id), type TEXT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('credit','debit')), amount_paise INTEGER NOT NULL CHECK(amount_paise>0),
  currency TEXT NOT NULL DEFAULT 'INR', status TEXT NOT NULL DEFAULT 'posted', ref_type TEXT, ref_id TEXT,
  description TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ledger_user ON ledger(user_id, id);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT,'ledger is immutable'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT,'ledger is immutable'); END;
CREATE TABLE IF NOT EXISTS commission_runs(order_id INTEGER PRIMARY KEY, net_margin_paise INTEGER, pool_paise INTEGER,
  distributed_paise INTEGER, company_retained_paise INTEGER, config_json TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS commissions(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, beneficiary_id INTEGER NOT NULL REFERENCES users(id),
  source_user_id INTEGER NOT NULL, level INTEGER NOT NULL, amount_paise INTEGER NOT NULL CHECK(amount_paise>0),
  status TEXT NOT NULL DEFAULT 'pending', release_at REAL NOT NULL, created_at REAL NOT NULL, released_at REAL,
  UNIQUE(order_id, level));
CREATE INDEX IF NOT EXISTS ix_comm_ben ON commissions(beneficiary_id, status);
CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), amount_paise INTEGER NOT NULL,
  fee_paise INTEGER NOT NULL DEFAULT 0, method TEXT NOT NULL, payout_snapshot TEXT, status TEXT NOT NULL DEFAULT 'pending',
  reference TEXT, admin_note TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS returns(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
  reason TEXT, status TEXT NOT NULL DEFAULT 'requested', admin_note TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS risk_flags(id INTEGER PRIMARY KEY, user_id INTEGER, kind TEXT NOT NULL, detail TEXT,
  status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS password_resets(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, token_hash TEXT UNIQUE NOT NULL, expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT, is_read INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_notif_user ON notifications(user_id, is_read);
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, subject TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ticket_messages(id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id), author_id INTEGER NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, actor_id INTEGER, action TEXT NOT NULL, entity TEXT, entity_id TEXT,
  before_json TEXT, after_json TEXT, reason TEXT, ip TEXT, user_agent TEXT, created_at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT,'audit log is immutable'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT,'audit log is immutable'); END;
"""

def connect(path=None):
    folder = os.path.dirname(os.path.abspath(path or DB_PATH)); os.makedirs(folder, exist_ok=True)
    c = sqlite3.connect(path or DB_PATH, isolation_level=None, check_same_thread=False, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db(db):
    db.executescript(SCHEMA)

@contextmanager
def tx(db):
    """One atomic write transaction. Services assume they run inside one."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK"); raise

def get_settings(db):
    row = db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
    cfg = json.loads(json.dumps(DEFAULTS))
    if row: cfg.update(json.loads(row["value"]))
    return cfg

def save_settings(db, cfg):
    db.execute("INSERT INTO settings(key,value) VALUES('config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(cfg),))

def now(): return time.time()
