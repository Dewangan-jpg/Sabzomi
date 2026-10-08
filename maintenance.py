"""Cron entry point:  */10 * * * * cd /app && python maintenance.py"""
import db as D, services as S
c = D.connect(); D.init_db(c)
with D.tx(c): print(S.run_maintenance(c))
