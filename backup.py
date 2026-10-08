"""Consistent online backup of the SQLite database:  python backup.py backups/   (schedule daily, copy off-server)"""
import os, sqlite3, sys, time
import db as D
dest = os.path.join(sys.argv[1] if len(sys.argv) > 1 else "backups", time.strftime("sabzomi-%Y%m%d-%H%M%S.db")); os.makedirs(os.path.dirname(dest), exist_ok=True)
src = sqlite3.connect(D.DB_PATH); dst = sqlite3.connect(dest); src.backup(dst); dst.close(); src.close(); print("Backup written:", dest)
