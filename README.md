# Sabzomi Backend (Flask + SQLite)

Secure API for the Sabzomi grocery + e-commerce + referral platform. All money is stored as **integer paise** (`*_paise`), never floats.

## Run
```bash
pip install -r requirements.txt
export SABZOMI_SECRET="long-random-string" SABZOMI_GATEWAY_SECRET="another-random-string"
export ADMIN_EMAIL=you@sabzomi.com ADMIN_PASSWORD='choose-a-strong-one'
python seed.py          # creates first admin, sample products, PIN codes
python app.py           # http://localhost:5000   (production: gunicorn "app:create_app()")
python -m unittest discover -s tests -v
```
Env: `SABZOMI_DB` (db path), `SABZOMI_ENV=production` (refuses to start with dev secrets), `CORS_ORIGIN` (your storefront URL).

## What is built
- **Auth**: register (with referral code), login (scrypt hashes, JWT 12h, 5-fail lockout), roles user/admin.
- **Referral tree**: permanent account IDs (`USR000001`), unique referral codes, materialized path for fast downline queries, self/circular prevention, admin sponsor change (reason required, audited, subtree re-pathed).
- **Commission engine** (`services.preview/generate_commissions`): net margin -> MLM pool (default 40%) -> per-level % of pool (default 20/25/20/15/10, level 1 = direct referrer). Fully configurable; saving settings is rejected if levels exceed 100%. Never computed on the frontend. Statuses used: pending -> available (after release period), reversed, clawed_back, cancelled.
- **Wallet**: immutable ledger (DB triggers block UPDATE/DELETE), no balance column, overdraft protection, top-up credited only after HMAC-verified webhook (idempotent), admin adjustments with mandatory reason.
- **Withdrawals**: UPI/bank details encrypted at rest and masked in responses; limits, fee, daily cap; amount held in ledger on request, released if rejected/failed/cancelled; pending commissions can never be withdrawn.
- **Checkout**: server-side totals; grocery minimum with "Add ₹X more" message; mixed cart split into grocery + other orders; configurable delivery rules; PIN code serviceability; coupons; stock decrement under transaction.
- **Orders**: full status flow, events/tracking, cancel (auto refund + restock), returns/refunds (window configurable), automatic commission reversal/clawback.
- **Admin API**: settings, commission preview/simulator, products with private margin calculator (`internal_admin_only`), users, orders, returns, withdrawals, commissions, coupons, PIN codes, risk flags, audit log, dashboard.
- **Anti-fraud flags**: many signups per IP, early withdrawal, repeated refunds, negative balance after clawback.

## API cheat sheet
Public: `GET /api/products`, `POST /api/auth/register|login`, `POST /api/webhooks/payment`
User (Bearer token): `/api/me`, `/api/dashboard`, `/api/checkout/quote`, `/api/checkout`, `/api/orders`, `/api/wallet`, `/api/wallet/topup`, `/api/wallet/transactions`, `/api/withdrawals`, `/api/referrals/tree|stats`, `/api/commissions`, `PUT /api/me/payout`
Admin: everything under `/api/admin/...`

Payment webhook body: `{"event":"payment.captured","gateway_order_id":"..","gateway_payment_id":"..","amount_paise":N}` with header `X-Signature` = HMAC-SHA256(body, SABZOMI_GATEWAY_SECRET). Replace `sign()` with your gateway's real verification (Razorpay etc.) before going live.

## Not built yet / before going live
- Real payment-gateway integration, payouts API, SMS/email OTP, notifications, support tickets, CMS, subscriptions, inventory history, data export, rank bonuses.
- Move to PostgreSQL for multi-server scale; add rate limiting (e.g. Flask-Limiter + Redis), HTTPS, backups, a scheduled job calling `release_due` daily, and KYC/legal review of the commission plan (direct-selling / MLM and GST/TDS rules vary and need a qualified advisor).
