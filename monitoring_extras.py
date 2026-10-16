"""Add-on helpers for the existing FastAPI website monitor."""
import os, ssl, socket, json, urllib.parse, urllib.request, sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

def send_telegram_message(message: str) -> bool:
    token=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    chat_id=os.getenv("TELEGRAM_CHAT_ID","").strip()
    if not token or not chat_id:
        print("Telegram not configured; skipping alert.")
        return False
    url=f"https://api.telegram.org/bot{token}/sendMessage"
    data=urllib.parse.urlencode({"chat_id":chat_id,"text":message[:4000]}).encode()
    try:
        req=urllib.request.Request(url,data=data,method="POST")
        with urllib.request.urlopen(req,timeout=10) as r:
            result=json.loads(r.read().decode())
        if not result.get("ok"): print("Telegram API error:",result.get("description"))
        return bool(result.get("ok"))
    except Exception as e:
        print("Telegram send failed:",e); return False

def init_extra_tables(DB_PATH):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS alert_history(
          id INTEGER PRIMARY KEY AUTOINCREMENT,url TEXT NOT NULL,alert_type TEXT NOT NULL,
          channel TEXT NOT NULL,success INTEGER NOT NULL,message TEXT,created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ssl_checks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,url TEXT NOT NULL,status TEXT NOT NULL,
          days_left INTEGER,expires_at TEXT,message TEXT,checked_at TEXT NOT NULL)""")
        c.commit()

def record_alert(DB_PATH,url,alert_type,channel,success,message=""):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT INTO alert_history(url,alert_type,channel,success,message,created_at) VALUES(?,?,?,?,?,?)",
          (url,alert_type,channel,int(bool(success)),message,datetime.now().astimezone().isoformat()))
        c.commit()

def get_alert_history(DB_PATH,limit=100):
    limit=max(1,min(int(limit),500))
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory=sqlite3.Row
        return [dict(x) for x in c.execute("SELECT * FROM alert_history ORDER BY id DESC LIMIT ?",(limit,))]

def get_ssl_history(DB_PATH,limit=100):
    limit=max(1,min(int(limit),500))
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory=sqlite3.Row
        return [dict(x) for x in c.execute("SELECT * FROM ssl_checks ORDER BY id DESC LIMIT ?",(limit,))]

def check_ssl_certificate(url,warning_days=30):
    p=urlparse(url)
    if p.scheme.lower()!="https" or not p.hostname:
        return {"url":url,"status":"SKIPPED","days_left":None,"message":"Not HTTPS"}
    try:
        ctx=ssl.create_default_context()
        with socket.create_connection((p.hostname,p.port or 443),timeout=10) as sock:
            with ctx.wrap_socket(sock,server_hostname=p.hostname) as tls: cert=tls.getpeercert()
        expiry=datetime.strptime(cert["notAfter"],"%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days=(expiry-datetime.now(timezone.utc)).days
        status="EXPIRED" if days<0 else "WARNING" if days<=warning_days else "OK"
        return {"url":url,"status":status,"days_left":days,"expires_at":expiry.isoformat(),
                "message":f"Certificate expires in {days} day(s)."}
    except Exception as e:
        return {"url":url,"status":"ERROR","days_left":None,"message":str(e)}

def run_ssl_checks(DB_PATH,send_change_email,telegram_sender=None,warning_days=30):
    with sqlite3.connect(DB_PATH) as c:
        urls=[r[0] for r in c.execute("SELECT url FROM monitored_websites WHERE lower(url) LIKE 'https://%'")]
    for url in urls:
        result=check_ssl_certificate(url,warning_days)
        now=datetime.now().astimezone().isoformat()
        with sqlite3.connect(DB_PATH) as c:
            c.execute("INSERT INTO ssl_checks(url,status,days_left,expires_at,message,checked_at) VALUES(?,?,?,?,?,?)",
              (url,result["status"],result.get("days_left"),result.get("expires_at"),result["message"],now))
            c.commit()
        if result["status"] in ("WARNING","EXPIRED"):
            body=f"SSL {result['status']}\\nWebsite: {url}\\nDays remaining: {result.get('days_left')}\\nExpiry: {result.get('expires_at')}\\nChecked: {now}"
            try: send_change_email(url,f"SSL {result['status']}: {url}",body)
            except Exception as e: print("SSL email failed:",e)
            if telegram_sender: telegram_sender("🔐 "+body)
