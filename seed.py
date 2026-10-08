"""First-run setup. Runs automatically at startup when ADMIN_PASSWORD is set (no shell needed); also usable as: python seed.py"""
import os, sys
import db as D, services as S

PRODUCTS = [  # (sku, name, category, sub, unit, price, mrp, cost, other_cost, tax_bp, stock)  money in paise
 ("ATTA5", "Aashirvaad Atta", "Grocery", "Grains & Pulses", "5 kg", 24500, 26500, 21000, 500, 0, 200), ("RICE5", "Basmati Rice", "Grocery", "Grains & Pulses", "5 kg", 29900, 34900, 25500, 500, 0, 200),
 ("OIL1", "Cooking Oil", "Grocery", "Kirana", "1 L", 14900, 16500, 13000, 300, 500, 300), ("TOM1", "Fresh Tomatoes", "Grocery", "Fruits & Vegetables", "1 kg", 4000, 4500, 3000, 300, 0, 100),
 ("BUDS1", "Bluetooth Earbuds", "Electronics", "", "", 129900, 199900, 90000, 3000, 1800, 50), ("TEE1", "Cotton T-Shirt", "Clothing", "", "Men, M", 49900, 79900, 28000, 1500, 500, 80),
 ("VASE1", "Ceramic Vase", "Decor", "", "", 39900, 59900, 21000, 1000, 1200, 40),
 ("PKG1", "Basic Grocery Pack", "Grocery", "Package", "1 month", 149900, 169900, 125000, 2000, 0, 100), ("PKG2", "Family Grocery Pack", "Grocery", "Package", "1 month", 249900, 289900, 205000, 3000, 0, 100),
 ("PKG3", "Premium Grocery Pack", "Grocery", "Package", "1 month", 399900, 459900, 330000, 4000, 0, 100)]

def seed(db):
    """Creates the first admin (and a demo catalog if SEED_SAMPLE=1). Does nothing once an admin exists."""
    with D.tx(db):
        if db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone(): return False
        pw = os.environ.get("ADMIN_PASSWORD")
        if not pw: raise RuntimeError("Set ADMIN_PASSWORD (min 8 characters) to create the first admin account")
        try: S.create_user(db, "Sabzomi Admin", os.environ.get("ADMIN_EMAIL", "admin@sabzomi.com"), os.environ.get("ADMIN_PHONE", "9000000000"), pw, role="admin")
        except S.ApiError as e: raise RuntimeError(f"Could not create admin: {e.msg}")
        if os.environ.get("SEED_SAMPLE", "1") == "1":
            for p in PRODUCTS: db.execute("INSERT OR IGNORE INTO products(sku,name,category,sub,unit,price_paise,mrp_paise,cost_paise,other_cost_paise,tax_bp,stock) VALUES(?,?,?,?,?,?,?,?,?,?,?)", p)
            for pin in ("411001", "400001", "110001", "410401"): db.execute("INSERT OR IGNORE INTO pincodes(pincode) VALUES(?)", (pin,))
    return True

if __name__ == "__main__":
    c = D.connect(); D.init_db(c)
    try: print("Seeded." if seed(c) else "Admin already exists; nothing to do.")
    except RuntimeError as e: sys.exit(str(e))
