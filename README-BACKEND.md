# Sabzomi Backend (Flask + SQLite)

Secure API for the Sabzomi grocery + e-commerce + referral platform. All money is stored as **integer paise** (`*_paise`), never floats.

## Run
```bash
pip install -r requirements.txt
export SABZOMI_SECRET="long-random-string" SABZOMI_GATEWAY_SECRET="another-random-string"
export ADMIN_EMAIL=you@sabzomi.com ADMIN_PASSWORD='choose-a-strong-one'
python seed.py          # creates first admin, sample products, PIN codes
export SABZOMI_DEV_PAYMENTS=1   # DEV ONLY: lets the UI fake a gateway payment (never set in production)
python app.py           # open http://localhost:5000 - the storefront is served by this same server   (production: gunicorn "app:create_app()")
python -m unittest discover -s tests -v
```
Env: `SABZOMI_DB` (db path), `SABZOMI_ENV=production` (refuses to start with dev secrets), `CORS_ORIGIN` (your storefront URL).

## What is built
- **Storefront** (`static/index.html`, served at `/`): home, category pages, product detail, packages + build-your-own basket, cart, server-priced checkout, orders (pay now / cancel / return), wallet (add money, UPI + bank payout, withdrawals), referral tree, notifications, support tickets, profile/password, and admin screens (dashboard, withdrawals, commission settings with preview, products with private margins, risk flags, tickets, CSV export).
- **Auth**: register (referral code), login (scrypt, JWT 12h, lockout, rate limits), roles user/admin, password change.
- **Referral tree**: permanent account IDs (`USR000001`), unique referral codes, materialized path, self/circular prevention, audited admin sponsor change.
- **Commission engine**: net margin -> MLM pool (40%) -> level % of pool (20/25/20/15/10, level 1 = direct referrer), all admin-configurable and validated (<=100%). Pending -> available after release period; refund/cancel -> reversed or clawed back. Calculated only on the server.
- **Wallet**: immutable ledger (DB triggers), overdraft protection, admin adjustments with reason. **Withdrawals**: UPI/bank (encrypted at rest, masked), limits/fee/daily cap, funds held then released if rejected.
- **Payments**: Razorpay (orders API, verified webhook `X-Razorpay-Signature`, verified checkout callback `/api/payments/verify`), idempotent crediting, price lock at payment creation, unpaid orders expire after 30 min and restock. Simulated gateway for development.
- **Checkout**: grocery minimum with "Add ₹X more", mixed cart split, delivery rules, PIN codes, coupons, stock control.
- **Orders**: full status flow with events, cancel/return/refund, automatic commission reversal.
- **Admin API**: settings, commission preview, products (private margin calculator), users, sponsor changes, wallet adjustments, orders, returns, withdrawals, commissions, coupons, PIN codes, risk flags, audit log, tickets, CSV exports, maintenance.
- **Notifications** (in-app) on orders, payments, commissions, withdrawals, support replies. **Support tickets** with admin replies.
- **Anti-fraud flags**: many signups per IP, early withdrawal, repeated refunds, negative balance after clawback.

## Go live with Razorpay
1. Create a Razorpay account, get **Key ID / Key Secret** (use *test mode* keys first).
2. Dashboard -> Webhooks: URL `https://YOUR-DOMAIN/api/webhooks/payment`, events `payment.captured`, `order.paid`, `payment.failed`, and a secret; put the same secret in `RAZORPAY_WEBHOOK_SECRET`.
3. Set env vars (see `.env.example`), `SABZOMI_GATEWAY=razorpay`. Do NOT set `SABZOMI_DEV_PAYMENTS`.
4. Put the app behind HTTPS (Caddy/Nginx/your host). `docker compose up -d --build` runs everything (data persists in the `sabzomi-data` volume).
5. Make a test payment in test mode, confirm the wallet/order updates, then switch to live keys.

## Operations
- Background job (every 10 min when `SABZOMI_AUTORUN_JOBS=1`, set in Docker): releases due commissions and expires unpaid orders. Alternative: cron `python maintenance.py`.
- Backups: `python backup.py /backups` (consistent online copy). Schedule daily and copy off-server.
- Tests: `python -m unittest discover -s tests -v` (11 API tests).

## Known limits / next steps
- SQLite = one server. For multiple servers or heavy traffic, migrate to PostgreSQL and use a Redis-backed rate limiter.
- Email/SMS OTP, rank bonuses, subscriptions, inventory history, payouts via gateway API (withdrawals are marked paid manually by admin after paying via your bank/UPI).
- Admin UI for PIN codes, coupons and returns is API-only for now (`PUT /api/admin/pincodes/<pin>`, `PUT /api/admin/coupons/<code>`, `/api/admin/returns`).
- Have a qualified advisor review the commission plan (direct-selling/MLM, GST, TDS rules) before launch.
