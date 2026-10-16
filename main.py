
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
import time
import urllib.request
import urllib.error
import smtplib
import socket
import sqlite3
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

# Always load .env from the same folder as this file.
BASE_DIR = FilePath(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

DB_PATH = BASE_DIR / "websites.db"

init_extra_tables(DB_PATH)

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
            CREATE TABLE IF NOT EXISTS uptime_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                status TEXT NOT NULL,
                http_status INTEGER,
                response_time_ms REAL,
                message TEXT,
                checked_at TEXT NOT NULL
            )
        """)

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
                added_at TEXT NOT NULL
            )
        """)
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


async def perform_check(url: str):
    try:
        clean_url = normalize_url(url)
        title, content = await asyncio.to_thread(fetch_website_sync, clean_url)

        with sqlite3.connect(DB_PATH) as conn:
            previous = conn.execute(
                """
                SELECT title, content FROM snapshots
                WHERE url = ? ORDER BY id DESC LIMIT 1
                """,
                (clean_url,),
            ).fetchone()

            changed = False
            if previous is not None:
                old_title, old_content = previous
                changed = old_title != title or old_content != content

            conn.execute(
                "INSERT OR IGNORE INTO monitored_websites (url, added_at) VALUES (?, ?)",
                (clean_url, now_utc()),
            )
            conn.execute(
                """
                INSERT INTO snapshots (url, title, content, checked_at)
                VALUES (?, ?, ?, ?)
                """,
                (clean_url, title, content, now_utc()),
            )

            message = "Website content has changed since the previous check."
            if changed:
                conn.execute(
                    """
                    INSERT INTO alerts (url, title, message, created_at, is_read)
                    VALUES (?, ?, ?, ?, 0)
                    """,
                    (clean_url, title, message, now_utc()),
                )
            conn.commit()

        if changed:
            await asyncio.to_thread(send_change_email, clean_url, title, message)

        return {
            "success": True,
            "url": clean_url,
            "title": title,
            "changed": changed,
            "message": (
                "Website change detected!"
                if changed
                else "Website checked successfully. No change detected."
            ),
        }
    except Exception as exc:
        return {
            "success": False,
            "url": url,
            "changed": False,
            "message": str(exc),
        }


async def check_all_websites():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT url FROM monitored_websites").fetchall()
    for (url,) in rows:
        await perform_check(url)


# ---------------- App startup/shutdown ----------------

def check_all_websites_uptime():
    with sqlite3.connect(DB_PATH) as conn:
        websites = conn.execute(
            "SELECT url FROM monitored_websites"
        ).fetchall()

    for (url,) in websites:
        # Pichhla status check karo
        with sqlite3.connect(DB_PATH) as conn:
            previous = conn.execute(
                """
                SELECT status
                FROM uptime_checks
                WHERE url = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (url,),
            ).fetchone()

        previous_status = (
            str(previous[0]).upper() if previous else None
        )

        # Purane status names ko bhi support karo
        was_online = previous_status in ("ONLINE", "UP")
        was_offline = previous_status in ("OFFLINE", "DOWN")

        started = time.perf_counter()
        http_status = None

        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "WebsiteChangeDetector/1.0"},
            )

            with urllib.request.urlopen(request, timeout=15) as response:
                http_status = response.status

            status = (
                "ONLINE" if http_status < 400 else "OFFLINE"
            )
            message = "Website responded successfully."

        except urllib.error.HTTPError as error:
            http_status = error.code
            status = (
                "ONLINE" if error.code < 400 else "OFFLINE"
            )
            message = f"Website returned HTTP {error.code}."

        except Exception as error:
            status = "OFFLINE"
            message = f"Website check failed: {error}"

        response_time_ms = round(
            (time.perf_counter() - started) * 1000, 2
        )

        checked_at = datetime.now().astimezone().isoformat()

        # Har check ka result database mein save karo
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO uptime_checks
                (url, status, http_status, response_time_ms,
                 message, checked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    url,
                    status,
                    http_status,
                    response_time_ms,
                    message,
                    checked_at,
                ),
            )

        # OFFLINE hone par ek alert; lagatar OFFLINE par repeat nahi
        if status == "OFFLINE" and not was_offline:
            send_change_email(
                url,
                f"Website DOWN: {url}",
                (
                    "Your monitored website is OFFLINE.\n\n"
                    f"Website: {url}\n"
                    f"HTTP status: {http_status}\n"
                    f"Message: {message}\n"
                    f"Checked at: {checked_at}"
                ),
            )

        # Website wapas ONLINE aaye to recovery email
        elif status == "ONLINE" and was_offline:
            send_change_email(
                url,
                f"Website RECOVERED: {url}",
                (
                    "Your monitored website is ONLINE again.\n\n"
                    f"Website: {url}\n"
                    f"HTTP status: {http_status}\n"
                    f"Response time: {response_time_ms} ms\n"
                    f"Checked at: {checked_at}"
                ),
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    if not scheduler.running:
        scheduler.add_job(
            check_all_websites,
            "interval",
            minutes=5,
            id="website_check_job",
            replace_existing=True,
            max_instances=1,
        )

        scheduler.add_job(
            check_all_websites_uptime,
            "interval",
            minutes=5,
            id="automatic_uptime_check",
            replace_existing=True,
            next_run_time=datetime.now().astimezone(),
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

@app.get("/api/alert-history")
def alert_history(limit: int = 100):
    return {"items": get_alert_history(DB_PATH, limit)}


@app.get("/api/ssl-history")
def ssl_history(limit: int = 100):
    return {"items": get_ssl_history(DB_PATH, limit)}



@app.get("/", response_class=HTMLResponse)
async def dashboard():
    if not HTML_PATH.exists():
        return HTMLResponse(
            "<h1>Website Change Detector</h1>"
            "<p>Please create an index.html file in the project folder.</p>"
        )
    return FileResponse(HTML_PATH)


# Accept common field names so different dashboard versions work.
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

    result = await perform_check(url)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@app.get("/websites")
async def get_websites():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT w.url, w.added_at,
                (SELECT s.checked_at FROM snapshots s
                 WHERE s.url = w.url ORDER BY s.id DESC LIMIT 1) AS last_checked
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


@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/uptime/check")
async def uptime_check(request: Request):
    import time
    import urllib.request
    import urllib.error

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(
        status_code=400,
        detail="Invalid JSON. Please send a website URL."
    )
    url = data.get("url", "").strip()

    if not url:
        raise HTTPException(status_code=400, detail="Website URL is required.")

    try:
        validate_public_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    url = normalize_url(url)
    started = time.perf_counter()
    status = "ONLINE"
    http_status = None
    message = "Website responded successfully."

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "WebsiteUptimeMonitor/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            http_status = response.status
            if http_status >= 500:
                status = "OFFLINE"
                message = f"Server error: HTTP {http_status}"
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        status = "OFFLINE" if exc.code >= 500 else "ONLINE"
        message = f"HTTP status: {exc.code}"
    except Exception as exc:
        status = "OFFLINE"
        message = f"Website unreachable: {exc}"

    response_time = round((time.perf_counter() - started) * 1000, 2)
    checked_at = now_utc()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS uptime_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                status TEXT NOT NULL,
                http_status INTEGER,
                response_time_ms REAL,
                message TEXT,
                checked_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO uptime_checks
            (url, status, http_status, response_time_ms, message, checked_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (url, status, http_status, response_time, message, checked_at))
        conn.execute(
            "INSERT OR IGNORE INTO monitored_websites (url, added_at) VALUES (?, ?)",
            (url, checked_at)
        )
        conn.commit()

    return {
        "url": url,
        "status": status,
        "http_status": http_status,
        "response_time_ms": response_time,
        "message": message,
        "checked_at": checked_at
    }


@app.get("/uptime/status")
async def uptime_status():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT u.url, u.status, u.http_status,
                   u.response_time_ms, u.message, u.checked_at
            FROM uptime_checks u
            INNER JOIN (
                SELECT url, MAX(id) AS latest_id
                FROM uptime_checks GROUP BY url
            ) latest ON u.id = latest.latest_id
            ORDER BY u.checked_at DESC
        """).fetchall()

    return {"websites": [dict(row) for row in rows]}


@app.get("/uptime/history")
async def uptime_history(url: str):
    url = normalize_url(url.strip())
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT url, status, http_status, response_time_ms,
                   message, checked_at
            FROM uptime_checks
            WHERE url = ?
            ORDER BY id DESC LIMIT 50
        """, (url,)).fetchall()

    return {"history": [dict(row) for row in rows]}

@app.post("/test-uptime-alerts")
async def test_uptime_alerts():
    down_sent = await asyncio.to_thread(
        send_change_email,
        "https://example.com/",
        "Website DOWN: TEST",
        (
            "This is a TEST alert only.\n\n"
            "Simulated status: OFFLINE\n"
            "No real website status was changed."
        ),
    )

    recovered_sent = await asyncio.to_thread(
        send_change_email,
        "https://example.com/",
        "Website RECOVERED: TEST",
        (
            "This is a TEST alert only.\n\n"
            "Simulated status: ONLINE\n"
            "No real website status was changed."
        ),
    )

    if not down_sent or not recovered_sent:
        raise HTTPException(
            status_code=500,
            detail="One or both test emails failed. Check the VS Code terminal.",
        )

    return {
        "success": True,
        "message": "Both test emails were sent successfully.",
    }



@app.post("/test-automatic-alert-logic")
async def test_automatic_alert_logic():
    test_url = "https://example.com/"

    down_sent = await asyncio.to_thread(
        send_change_email,
        test_url,
        "TEST - Website DOWN",
        "TEST ONLY: Simulated website OFFLINE alert. "
        "No actual website status was changed.",
    )

    recovered_sent = await asyncio.to_thread(
        send_change_email,
        test_url,
        "TEST - Website RECOVERED",
        "TEST ONLY: Simulated website RECOVERED alert. "
        "No actual website status was changed.",
    )

    if not down_sent or not recovered_sent:
        raise HTTPException(
            status_code=500,
            detail="Test failed. Check the VS Code terminal and .env.",
        )

    return {
        "success": True,
        "down_email_sent": down_sent,
        "recovered_email_sent": recovered_sent,
        "note": "Email delivery tested; scheduler transition logic not yet tested.",
    }
