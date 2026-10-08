"""Tiny SMTP sender. Env: SMTP_HOST, SMTP_PORT (465=SSL, 587=STARTTLS), SMTP_USER, SMTP_PASS, SMTP_FROM."""
import logging, os, smtplib, ssl
from email.message import EmailMessage

def configured(): return bool(os.environ.get("SMTP_HOST"))

def send(to, subject, body):
    if not configured():
        logging.warning("SMTP not configured; email to %s skipped", to); return False
    msg = EmailMessage(); msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", ""); msg["To"] = to; msg["Subject"] = subject; msg.set_content(body)
    host, port, user = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "465")), os.environ.get("SMTP_USER")
    try:
        ctx = ssl.create_default_context()
        if port == 465: s = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx)
        else:
            s = smtplib.SMTP(host, port, timeout=15); s.starttls(context=ctx)
        with s:
            if user: s.login(user, os.environ.get("SMTP_PASS", ""))
            s.send_message(msg)
        return True
    except Exception:
        logging.exception("Sending email failed"); return False
