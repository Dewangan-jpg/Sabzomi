"""Create the first admin and sample catalog. Usage: ADMIN_EMAIL=.. ADMIN_PASSWORD=.. python seed.py"""
import os, sys
import db as D, services as S
def seed(db):
    with D.tx(db):
        if not db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            email, pw = os.environ.get("ADMIN_EMAIL", "admin@sabzomi.com"), os.environ.get("ADMIN_PASSWORD")
            if not pw: sys.exit("Set ADMIN_PASSWORD (min 8 chars) before seeding")
            S.create_user(db, "Sabzomi Admin", email, os.environ.get("ADMIN_PHONE", "9000000000"), pw, role="admin")
        # (sku, name, category, sub, unit, price, mrp, cost, other_cost, tax_bp, stock)  prices in paise
        P = [("ATTA5", "Aashirvaad Atta", "Grocery", "Grains & Pulses", "5 kg", 24500, 26500, 21000, 500, 0, 200), ("RICE5", "Basmati Rice", "Grocery", "Grains & Pulses", "5 kg", 29900, 34900, 25500, 500, 0, 200),
             ("OIL1", "Cooking Oil", "Grocery", "Kirana", "1 L", 14900, 16500, 13000, 300, 500, 300), ("TOM1", "Fresh Tomatoes", "Grocery", "Fruits & Vegetables", "1 kg", 4000, 4500, 3000, 300, 0, 100),
             ("BUDS1", "Bluetooth Earbuds", "Electronics", "", "", 129900, 199900, 90000, 3000, 1800, 50), ("TEE1", "Cotton T-Shirt", "Clothing", "", "Men, M", 49900, 79900, 28000, 1500, 500, 80),
             ("VASE1", "Ceramic Vase", "Decor", "", "", 39900, 59900, 21000, 1000, 1200, 40)]
        for p in P: db.execute("INSERT OR IGNORE INTO products(sku,name,category,sub,unit,price_paise,mrp_paise,cost_paise,other_cost_paise,tax_bp,stock) VALUES(?,?,?,?,?,?,?,?,?,?,?)", p)
        for pin in ("411001", "400001", "110001", "410401"): db.execute("INSERT OR IGNORE INTO pincodes(pincode) VALUES(?)", (pin,))
if __name__ == "__main__":
    c = D.connect(); D.init_db(c); seed(c); print("Seeded.")
