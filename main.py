
from monitoring_extras import (
    send_telegram_message,
    init_extra_tables,
    record_alert,
    get_alert_history,
    get_ssl_history,
    run_ssl_checks,
)

import asyncio
import ipaddress
import os
import smtplib
import socket
import sqlite3
import urllib.request
import urllib.error
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path as FilePath
from urllib.parse import urlparse, urlsplit, urlunsplit

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from playwright.sync_api import sync_playwright

try:
    from monitoring_extras import init_extra_tables, get_alert_history, get_ssl_history, run_ssl_checks
except ImportError:
    init_extra_tables = get_alert_history = get_ssl_history = run_ssl_checks = None

# Always load .env from the same folder as this file.
BASE_DIR = FilePath(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

DB_PATH = BASE_DIR / "websites.db"
HTML_PATH = BASE_DIR / "index.html"
scheduler = AsyncIOScheduler()


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def normalize_url(url: str) -> str:
    """Normalize equivalent root URLs such as example.com and example.com/."""
    raw_url = url.strip()
    parts = urlsplit(raw_url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path

    # Only remove the slash for the website root. Do not change paths like /news/.
    if path == "/":
        path = ""

    return urlunsplit((scheme, netloc, path, parts.query, parts.fragment))


# ---------------- Database ----------------

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                title TEXT,
                content TEXT,
                checked_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS monitored_websites (
                url TEXT PRIMARY KEY,
                added_at TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                interval_minutes INTEGER NOT NULL DEFAULT 60,
                last_checked TEXT,
                last_changed TEXT,
                status TEXT NOT NULL DEFAULT 'UNKNOWN'
            )
        """)
        # Safe migrations for databases created by older versions.
        website_columns = {row[1] for row in conn.execute("PRAGMA table_info(monitored_websites)").fetchall()}
        for column, definition in (
            ('name', "TEXT NOT NULL DEFAULT ''"),
            ('interval_minutes', 'INTEGER NOT NULL DEFAULT 60'),
            ('last_checked', 'TEXT'),
            ('last_changed', 'TEXT'),
            ('status', "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
        ):
            if column not in website_columns:
                conn.execute(f"ALTER TABLE monitored_websites ADD COLUMN {column} {definition}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                title TEXT,
                message TEXT,
                created_at TEXT NOT NULL,
                is_read INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""CREATE TABLE IF NOT EXISTS uptime_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, status TEXT NOT NULL,
            http_status INTEGER, response_time_ms INTEGER, checked_at TEXT NOT NULL, error TEXT
        )""")
        conn.commit()

        # Normalize existing records and merge duplicate monitored website rows.
        website_rows = conn.execute(
            "SELECT url, added_at FROM monitored_websites ORDER BY added_at ASC"
        ).fetchall()
        for old_url, added_at in website_rows:
            canonical_url = normalize_url(old_url)
            if canonical_url != old_url:
                conn.execute("UPDATE snapshots SET url = ? WHERE url = ?", (canonical_url, old_url))
                conn.execute("UPDATE alerts SET url = ? WHERE url = ?", (canonical_url, old_url))
                existing = conn.execute(
                    "SELECT added_at FROM monitored_websites WHERE url = ?", (canonical_url,)
                ).fetchone()
                if existing:
                    earliest = min(existing[0], added_at)
                    conn.execute(
                        "UPDATE monitored_websites SET added_at = ? WHERE url = ?",
                        (earliest, canonical_url),
                    )
                    conn.execute("DELETE FROM monitored_websites WHERE url = ?", (old_url,))
                else:
                    conn.execute(
                        "UPDATE monitored_websites SET url = ? WHERE url = ?",
                        (canonical_url, old_url),
                    )
        conn.commit()


# ---------------- Email ----------------

def send_change_email(url: str, title: str, message: str) -> bool:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    try:
        smtp_port = int(os.getenv("SMTP_PORT", "587").strip())
    except ValueError:
        print("Email not sent: SMTP_PORT must be a number.")
        return False

    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip().replace(" ", "")
    email_to = os.getenv("EMAIL_TO", "").strip()

    if not smtp_user or not smtp_password or not email_to:
        print(
            "Email not sent: SMTP_USER, SMTP_PASSWORD, or EMAIL_TO is missing "
            f"from {BASE_DIR / '.env'}."
        )
        return False

    email = EmailMessage()
    email["Subject"] = f"Website Change Detected: {title or url}"
    email["From"] = smtp_user
    email["To"] = email_to
    email.set_content(
        f"{message}\n\nWebsite: {url}\nTitle: {title or '(no title)'}\n"
        f"Detected at (UTC): {now_utc()}\n"
    )

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.send_message(email)
        print(f"Email notification sent to {email_to}")
        return True
    except Exception as exc:
        # Do not print secret values.
        print(f"Email notification failed: {type(exc).__name__}: {exc}")
        return False


# ---------------- Telegram ----------------
def send_telegram_alert(message: str) -> bool:
    """Send a message using the Telegram Bot API."""
    import json

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("Telegram not sent: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing in .env")
        return False

    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": message[:4000]}).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("ok") is True:
            print("Telegram notification sent successfully.")
            return True
        print("Telegram failed: Telegram API did not accept the message.")
        return False
    except Exception as exc:
        # Do not print the bot token or other secrets.
        print(f"Telegram notification failed: {type(exc).__name__}")
        return False


def send_all_notifications(url: str, title: str, message: str) -> dict:
    """Attempt email and Telegram independently."""
    email_sent = send_change_email(url, title, message)
    telegram_text = (
        f"{title or 'Website Alert'}\n\n{message}\n\n"
        f"Website: {url}\nDetected at (UTC): {now_utc()}"
    )
    telegram_sent = send_telegram_alert(telegram_text)
    print(f"Notification results: email={email_sent}, telegram={telegram_sent}")
    return {"email_sent": email_sent, "telegram_sent": telegram_sent}


# ---------------- URL validation ----------------

def validate_public_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("Please enter a valid website URL.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing login credentials are not allowed.")

    hostname = parsed.hostname
    if hostname.lower() == "localhost":
        raise ValueError("Private or non-public IP addresses are not allowed.")

    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        try:
            results = socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = {ipaddress.ip_address(result[4][0]) for result in results}
        except socket.gaierror:
            raise ValueError("Could not find this website. Check the URL.")

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Private or non-public IP addresses are not allowed.")


# ---------------- Website fetching ----------------
# Synchronous Playwright runs in a worker thread to avoid Windows asyncio subprocess issues.

def fetch_website_sync(url: str):
    validate_public_url(url)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            if response and response.status >= 400:
                raise ValueError(f"Website returned HTTP status {response.status}.")
            title = page.title()
            try:
                content = page.locator("body").inner_text(timeout=10000)
            except Exception:
                content = page.content()
            return title, content
        finally:
            browser.close()


async def perform_check(url: str, name: str | None = None, interval_minutes: int | None = None):
    try:
        clean_url = normalize_url(url)
        validate_public_url(clean_url)
        title, content = await asyncio.to_thread(fetch_website_sync, clean_url)
        checked_at = now_utc()

        with sqlite3.connect(DB_PATH) as conn:
            previous = conn.execute(
                "SELECT title, content FROM snapshots WHERE url = ? ORDER BY id DESC LIMIT 1",
                (clean_url,),
            ).fetchone()
            changed = previous is not None and (previous[0] != title or previous[1] != content)
            is_first_snapshot = previous is None

            conn.execute(
                """INSERT OR IGNORE INTO monitored_websites
                   (url, added_at, name, interval_minutes, status)
                   VALUES (?, ?, ?, ?, 'ONLINE')""",
                (clean_url, checked_at, (name or title or clean_url)[:120],
                 max(15, min(1440, int(interval_minutes or 60)))),
            )
            if name:
                conn.execute("UPDATE monitored_websites SET name = ? WHERE url = ?", (name[:120], clean_url))
            if interval_minutes is not None:
                conn.execute("UPDATE monitored_websites SET interval_minutes = ? WHERE url = ?",
                              (max(15, min(1440, int(interval_minutes))), clean_url))

            # Store only the first version or a genuine content change.
            if is_first_snapshot or changed:
                conn.execute(
                    "INSERT INTO snapshots (url, title, content, checked_at) VALUES (?, ?, ?, ?)",
                    (clean_url, title, content, checked_at),
                )
            message = "Website content has changed since the previous version."
            if changed:
                conn.execute(
                    "INSERT INTO alerts (url, title, message, created_at, is_read) VALUES (?, ?, ?, ?, 0)",
                    (clean_url, title, message, checked_at),
                )
            conn.execute(
                "UPDATE monitored_websites SET last_checked = ?, status = 'ONLINE', "
                "last_changed = CASE WHEN ? THEN ? ELSE last_changed END WHERE url = ?",
                (checked_at, int(changed), checked_at, clean_url),
            )
            conn.commit()

        if changed:
            await asyncio.to_thread(send_all_notifications, clean_url,
                                    title or "Website Change Detected", message)
        return {"success": True, "url": clean_url, "title": title, "changed": changed,
                "message": "Website change detected!" if changed else
                ("First snapshot saved." if is_first_snapshot else "Checked successfully. No change detected.")}
    except Exception as exc:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE monitored_websites SET last_checked = ?, status = 'ERROR' WHERE url = ?",
                             (now_utc(), normalize_url(url)))
                conn.commit()
        except Exception:
            pass
        return {"success": False, "url": url, "changed": False, "message": str(exc)}


async def check_all_websites():
    """Check only sites whose own interval has elapsed; scheduler wakes once per minute."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT url, name, interval_minutes, last_checked FROM monitored_websites").fetchall()
    now = datetime.now(timezone.utc)
    for url, name, interval_minutes, last_checked in rows:
        due = not last_checked
        if last_checked:
            try:
                last = datetime.fromisoformat(last_checked.replace('Z', '+00:00'))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                due = (now - last).total_seconds() >= int(interval_minutes or 60) * 60
            except (ValueError, TypeError):
                due = True
        if due:
            await perform_check(url, name=name, interval_minutes=interval_minutes)


# ---------------- App startup/shutdown ----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if init_extra_tables:
        try:
            init_extra_tables(DB_PATH)
        except Exception as exc:
            print(f"Extra table initialization skipped: {type(exc).__name__}")
    if not scheduler.running:
        scheduler.add_job(
            check_all_websites,
            "interval",
            minutes=1,
            id="website_check_job",
            replace_existing=True,
            max_instances=1,
        )
        scheduler.start()
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


app = FastAPI(
    title="Website Change Detector",
    description="Monitor public websites and receive change alerts.",
    lifespan=lifespan,
)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    if not HTML_PATH.exists():
        return HTMLResponse(
            "<h1>Website Change Detector</h1>"
            "<p>Please create an index.html file in the project folder.</p>"
        )
    return FileResponse(HTML_PATH)


# Accept common field names so different dashboard versions work.
@app.post("/websites")
async def add_website(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    url = str(data.get("url", "")).strip()
    name = str(data.get("name", "")).strip()
    try:
        interval = int(data.get("interval_minutes", 60))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Interval must be a number of minutes.")
    if not url:
        raise HTTPException(status_code=400, detail="URL is required.")
    if interval < 15 or interval > 1440:
        raise HTTPException(status_code=400, detail="Interval must be between 15 and 1440 minutes.")
    try:
        validate_public_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    clean_url = normalize_url(url)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""INSERT INTO monitored_websites (url, added_at, name, interval_minutes, status)
                      VALUES (?, ?, ?, ?, 'UNKNOWN')
                      ON CONFLICT(url) DO UPDATE SET name=excluded.name, interval_minutes=excluded.interval_minutes""",
                     (clean_url, now_utc(), name or clean_url, interval))
        conn.commit()
    result = await perform_check(clean_url, name=name or clean_url, interval_minutes=interval)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return {**result, "name": name or result.get("title") or clean_url, "interval_minutes": interval}


@app.post("/check")
async def check_website(request: Request):
    data = {}
    content_type = request.headers.get("content-type", "").lower()

    try:
        if "application/json" in content_type:
            parsed = await request.json()
            if isinstance(parsed, dict):
                data = parsed
            elif isinstance(parsed, str):
                data = {"url": parsed}
        elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            form = await request.form()
            data = dict(form)
        else:
            raw = (await request.body()).decode("utf-8", errors="ignore").strip()
            if raw:
                try:
                    parsed = __import__("json").loads(raw)
                    data = parsed if isinstance(parsed, dict) else {"url": parsed}
                except Exception:
                    data = {"url": raw}

    except Exception:
        data = {}

    url = ""
    for key in ("url", "website_url", "website", "site", "link"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            url = normalize_url(value)
            break

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Please provide a website URL in the 'url' field, for example: https://example.com/",
        )

    name = data.get("name") if isinstance(data.get("name"), str) else None
    interval = data.get("interval_minutes")
    result = await perform_check(url, name=name, interval_minutes=interval)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@app.get("/websites")
async def get_websites():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT w.url, w.added_at, w.name, w.interval_minutes, w.last_checked,
                   w.last_changed, w.status,
                (SELECT s.checked_at FROM snapshots s
                 WHERE s.url = w.url ORDER BY s.id DESC LIMIT 1) AS last_version_at
            FROM monitored_websites w
            ORDER BY w.added_at DESC
        """).fetchall()
    return [dict(row) for row in rows]


@app.get("/history")
async def get_history(url: str):
    if not url.strip():
        raise HTTPException(status_code=400, detail="URL is required.")
    url = normalize_url(url)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, url, title, content, checked_at
            FROM snapshots WHERE url = ? ORDER BY id DESC LIMIT 50
            """,
            (url,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/alerts")
async def get_alerts(unread_only: bool = False):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if unread_only:
            rows = conn.execute("""
                SELECT id, url, title, message, created_at, is_read
                FROM alerts WHERE is_read = 0 ORDER BY id DESC
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, url, title, message, created_at, is_read
                FROM alerts ORDER BY id DESC LIMIT 100
            """).fetchall()
    return {"alerts": [dict(row) for row in rows]}


@app.post("/alerts/{alert_id}/read")
async def mark_alert_read(alert_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "UPDATE alerts SET is_read = 1 WHERE id = ?",
            (alert_id,),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Alert not found.")
    return {"success": True, "message": "Alert marked as read."}


@app.post("/test-change")
async def test_change():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO alerts (url, title, message, created_at, is_read)
            VALUES (?, ?, ?, ?, 0)
            """,
            (
                "TEST://website-change-detector",
                "Website Change Detector Test",
                "Test successful! A simulated website content change was detected.",
                now_utc(),
            ),
        )
        conn.commit()
    return {"success": True, "changed": True, "message": "Test alert created successfully!"}


@app.post("/test-email")
async def test_email():
    sent = await asyncio.to_thread(
        send_change_email,
        "https://example.com/",
        "Website Change Detector Test Email",
        "This is a test email from Website Change Detector.",
    )
    if not sent:
        raise HTTPException(
            status_code=500,
            detail="Email could not be sent. Check VS Code terminal logs and .env settings.",
        )
    return {"success": True, "message": "Test email sent successfully."}


@app.post("/test-telegram")
async def test_telegram():
    sent = await asyncio.to_thread(
        send_telegram_alert,
        "Website Change Detector\n\nTEST ALERT: Telegram notifications are working.",
    )
    if not sent:
        raise HTTPException(
            status_code=500,
            detail="Telegram failed. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env and the server terminal.",
        )
    return {"success": True, "telegram_sent": True, "message": "Telegram test message sent successfully."}


@app.post("/test-notifications")
async def test_notifications():
    results = await asyncio.to_thread(
        send_all_notifications,
        "https://example.com/",
        "Website Change Detector - Test",
        "This is a test notification. No actual website change occurred.",
    )
    if not all(results.values()):
        raise HTTPException(status_code=500, detail={"success": False, **results})
    return {"success": True, **results, "message": "Email and Telegram tests passed."}


@app.post("/uptime/check")
async def uptime_check(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    url = normalize_url(str(data.get("url", "")).strip())
    if not url:
        raise HTTPException(status_code=400, detail="URL is required.")
    try:
        validate_public_url(url)
        started = __import__("time").monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": "WebsiteChangeDetector/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                code = response.getcode()
        except urllib.error.HTTPError as exc:
            code = exc.code
        elapsed = int((__import__("time").monotonic() - started) * 1000)
        status = "ONLINE" if code < 500 else "OFFLINE"
        err = None
    except Exception as exc:
        code, elapsed, status, err = None, None, "UNKNOWN", str(exc)
    checked = now_utc()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO uptime_checks (url,status,http_status,response_time_ms,checked_at,error) VALUES (?,?,?,?,?,?)",
                     (url, status, code, elapsed, checked, err))
        conn.execute("INSERT OR IGNORE INTO monitored_websites (url,added_at,name,interval_minutes,status) VALUES (?,?,?,60,?)",
                     (url, checked, url, status))
        if status in ("ONLINE", "OFFLINE"):
            conn.execute("UPDATE monitored_websites SET status=? WHERE url=?", (status, url))
        conn.commit()
    return {"success": status != "UNKNOWN", "url": url, "status": status,
            "http_status": code, "response_time_ms": elapsed, "checked_at": checked, "error": err}


@app.get("/uptime/status")
async def uptime_status():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""SELECT w.url, w.name, COALESCE((SELECT u.status FROM uptime_checks u
            WHERE u.url=w.url ORDER BY u.id DESC LIMIT 1), w.status, 'UNKNOWN') AS status,
            (SELECT u.http_status FROM uptime_checks u WHERE u.url=w.url ORDER BY u.id DESC LIMIT 1) AS http_status,
            (SELECT u.response_time_ms FROM uptime_checks u WHERE u.url=w.url ORDER BY u.id DESC LIMIT 1) AS response_time_ms,
            (SELECT u.checked_at FROM uptime_checks u WHERE u.url=w.url ORDER BY u.id DESC LIMIT 1) AS checked_at
            FROM monitored_websites w ORDER BY w.added_at DESC""").fetchall()
    return [dict(row) for row in rows]


@app.get("/uptime/history")
async def uptime_history(url: str):
    url = normalize_url(url)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id,url,status,http_status,response_time_ms,checked_at,error FROM uptime_checks WHERE url=? ORDER BY id DESC LIMIT 100", (url,)).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/alert-history")
async def api_alert_history(limit: int = 100):
    if get_alert_history:
        return get_alert_history(DB_PATH, max(1, min(limit, 500)))
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id,url,title,message,created_at,is_read FROM alerts ORDER BY id DESC LIMIT ?",
                            (max(1, min(limit, 500)),)).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/ssl-history")
async def api_ssl_history(limit: int = 100):
    if get_ssl_history:
        return get_ssl_history(DB_PATH, max(1, min(limit, 500)))
    return {"available": False, "message": "SSL history helper is not configured."}


@app.post("/api/ssl-check")
async def api_ssl_check():
    if not run_ssl_checks:
        raise HTTPException(status_code=501, detail="SSL check helper is not configured.")
    result = await asyncio.to_thread(run_ssl_checks, DB_PATH)
    return {"success": True, "result": result}


@app.get("/health")
async def health():
    return {"status": "ok"}
