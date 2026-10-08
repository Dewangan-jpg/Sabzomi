# Put Sabzomi live on GoDaddy (cPanel hosting) — step by step

**What you are uploading:** one package containing the website (frontend) and the server (backend). It runs as a *Python app* inside your GoDaddy cPanel. It does **not** need any extra paid software.

## Step 0 — Check your plan supports Python (2 minutes)
1. Log in to GoDaddy → **My Products** → your **Web Hosting** → **cPanel Admin**.
2. In cPanel, look under **Software** for **"Setup Python App"** (use the search box at the top).
   - **You see it →** continue with Step 1.
   - **You don't see it →** your plan can't run Python. Options: (a) ask GoDaddy support to enable Python/Application Manager or move you to a plan that has it; (b) use the "No Python?" section at the bottom. Do not buy anything until GoDaddy confirms.

## Step 1 — Upload the files
1. cPanel → **File Manager** → go to your **home folder** (`/home/YOURUSERNAME`), NOT `public_html`.
2. Click **+ Folder** → name it `sabzomi`. Open it.
3. **Upload** → choose `sabzomi-godaddy-upload.zip` → when done, select the zip → **Extract**. You should now see `app.py`, `passenger_wsgi.py`, a `static` folder, etc. directly inside `sabzomi`.
   (Important: the app folder must be **outside** `public_html`, so nobody can download your database or secrets.)

## Step 2 — Fill in your settings
1. In `sabzomi`, find **`sabzomi.env.example`** → right-click → **Copy** → name the copy **`sabzomi.env`**.
2. Right-click `sabzomi.env` → **Edit** and replace every value:
   - `SABZOMI_SECRET` and `SABZOMI_GATEWAY_SECRET`: two different long random texts (40+ letters/numbers). Keep them private.
   - `YOUR_CPANEL_USERNAME` (appears twice): your cPanel username (shown top-right in cPanel / File Manager path).
   - `SITE_URL`, `SUPPORT_EMAIL`: your real domain and email.
   - `ADMIN_EMAIL`, `ADMIN_PHONE` (10 digits), `ADMIN_PASSWORD`: your admin login.
   - Razorpay keys: start with **test** keys (Step 6).
   - Email (SMTP): create an email account in cPanel → **Email Accounts** (e.g. `no-reply@yourdomain.com`), then fill `SMTP_USER` / `SMTP_PASS`. If your email is GoDaddy Workspace email instead, use host `smtpout.secureserver.net` (port 465).
3. **Save**.

## Step 3 — Create the Python app
1. cPanel → **Setup Python App** → **Create Application**.
2. Fill:
   - **Python version:** the newest offered (3.10 – 3.12 is ideal; must be 3.9 or higher).
   - **Application root:** `sabzomi`
   - **Application URL:** your domain (leave the path empty to run at the main address, or pick a subdomain like `shop.yourdomain.com`).
   - **Application startup file:** `passenger_wsgi.py`
   - **Application Entry point:** `application`
3. Click **Create**.
4. On the same page, **Configuration files** → add `requirements.txt` → click **Run Pip Install** and wait until it finishes.
5. Click **Restart**.

> If your domain currently shows a GoDaddy placeholder page, delete or rename the `index.html` / `default.html` inside `public_html` first, otherwise it will cover your site.

## Step 4 — First visit
1. Open your domain. You should see the Sabzomi home page.
2. Go to **Login / Register** → log in with `ADMIN_EMAIL` and `ADMIN_PASSWORD` (the admin account was created automatically on first start).
3. **Immediately:** edit `sabzomi.env` and **delete the `ADMIN_PASSWORD` line**, then **Restart** the app in Setup Python App. Then open **Account → Profile** and change your admin password to a new one.

## Step 5 — Set up your store (Account → Admin screens)
1. **Delivery & coupons:** add every PIN code you deliver to (customers cannot order to other PIN codes).
2. **Products:** add your real products (price, MRP, your purchase cost, tax %, stock) and upload a **Photo** for each. For grocery packages, type `Package` in Sub-category.
   Categories: Grocery, Electronics, Clothing, Decor. Grocery orders need ₹999 minimum.
3. **Commission settings:** check the pool % and level %, press **Preview**, then **Save**.
4. Do a dry run: register 3 test accounts (B using A's referral code, C using B's), buy as C, then in **Orders** move the order to **delivered** and look at **Commissions**.

## Step 6 — Turn on real payments (Razorpay)
1. Create a Razorpay account and complete their KYC. Copy **Key ID** and **Key Secret** (test mode first) into `sabzomi.env`.
2. Razorpay dashboard → **Settings → Webhooks → Add**:
   - URL: `https://YOURDOMAIN/api/webhooks/payment`
   - Events: `payment.captured`, `order.paid`, `payment.failed`
   - Secret: a text of your choice → put the same text in `RAZORPAY_WEBHOOK_SECRET`.
3. Restart the app. Make a test payment with Razorpay test cards/UPI. Wallet / order must update by itself.
4. When happy, replace the test keys with **live** keys and restart.

## Step 7 — HTTPS (padlock)
1. cPanel → **SSL/TLS Status** → run **AutoSSL** for your domain (or install the SSL certificate included with / bought for your plan).
2. `FORCE_HTTPS=1` in `sabzomi.env` (already in the template) makes the site redirect to https.

## Step 8 — Background job (important)
Commissions are released and unpaid orders are cancelled by a small job.
1. cPanel → **Cron Jobs** → add a job **every 10 minutes** (`*/10 * * * *`) with this command (replace `YOURUSERNAME`, and `3.11` with the Python version you chose):
```
/home/YOURUSERNAME/virtualenv/sabzomi/3.11/bin/python /home/YOURUSERNAME/sabzomi/maintenance.py
```
   (Setup Python App shows the exact virtualenv command at the top of your app page — copy the path from there.)
2. You can also press **Run maintenance** in the Admin panel any time.

## Step 9 — Backups
- Daily: cPanel → **Backup** (download a full backup), and keep a copy of `/home/YOURUSERNAME/sabzomi_data` (database + product photos).
- Optional job: `.../python /home/YOURUSERNAME/sabzomi/backup.py /home/YOURUSERNAME/sabzomi_backups`

## Go-live checklist
- [ ] Python app running, https padlock shows
- [ ] `ADMIN_PASSWORD` line removed, admin password changed
- [ ] Live Razorpay keys + webhook working (small real payment tested and refunded)
- [ ] Password-reset email arrives (use "Forgot password" with a test account)
- [ ] PIN codes, products with photos, commission settings done
- [ ] Cron job added
- [ ] Terms / Privacy / Refunds / Earnings pages reviewed by a lawyer for your business details (direct-selling / MLM, GST and TDS rules)
- [ ] `SABZOMI_DEV_PAYMENTS` is NOT in sabzomi.env

## Troubleshooting
| Problem | Fix |
|---|---|
| "Internal Server Error" / blank page | Setup Python App → open the log file shown there; usually a wrong value in `sabzomi.env` (message says which). Fix, then Restart. |
| Message "Edit sabzomi.env: set SABZOMI_SECRET…" | You still have a `CHANGE-ME` value. Replace it. |
| Pip install fails | Choose a newer Python version in the app settings and run Pip Install again. |
| Changes not showing | Restart the app (Passenger caches code). Also hard-refresh the browser (Ctrl+F5). |
| Cannot log in as admin | Admin is only created if the `ADMIN_*` lines were present on the first start. Check the phone has 10 digits and the password is 8+ characters, then Restart. |
| Payments don't update | Check the webhook URL/secret in Razorpay dashboard; Razorpay shows delivery attempts and errors there. |
| Browser says "redirected you too many times" | Set `FORCE_HTTPS=0` in `sabzomi.env`, Restart, and instead force https in cPanel → **Domains** (Force HTTPS Redirect toggle). |
| No emails | Check `SMTP_*` values; try port 587 if 465 fails. |

## No Python on your plan?
- **Keep your GoDaddy domain, host the app elsewhere.** Services like PythonAnywhere, Render or Railway can run this same package; you then point your GoDaddy domain's DNS to them (they show the exact records). 
- **GoDaddy VPS** also works: install Docker and run `docker compose up -d --build` using the included `Dockerfile` and `docker-compose.yml`.
- The `sabzomi-frontend.zip` alone is only the website screens; it needs the server above to work and can be pointed at it by editing `window.SABZOMI_API` in `index.html`.
