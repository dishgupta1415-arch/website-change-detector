import asyncio
import io
from PIL import Image, ImageChops, ImageStat
import ipaddress
from pathlib import Path
import difflib
import html
import os
from datetime import datetime, timedelta, timezone
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
                checked_at TEXT NOT NULL,
                screenshot_path TEXT
            )
        """)
        # Safe migration for databases created before screenshot snapshots.
        snapshot_columns = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)").fetchall()}
        if "screenshot_path" not in snapshot_columns:
            conn.execute("ALTER TABLE snapshots ADD COLUMN screenshot_path TEXT")
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
            if response is None:
                raise ValueError("The website did not return an HTTP response.")

            if response.status == 403:
                raise ValueError(
                    "HTTP 403 Forbidden: this website is blocking the content-check request. "
                    "It may restrict automated access. Uptime can still show ONLINE if its "
                    "separate request succeeds."
                )
            if response.status == 429:
                raise ValueError(
                    "HTTP 429 Too Many Requests: the website is limiting requests. Try again later."
                )
            if response.status >= 400:
                raise ValueError(f"Website returned HTTP status {response.status}.")

            title = page.title()
            try:
                content = page.locator("body").inner_text(timeout=10000)
            except Exception:
                content = page.content()
            # Capture a visual version alongside the text snapshot.
            screenshot_bytes = page.screenshot(full_page=True, type="png", timeout=15000)
            return title, content, screenshot_bytes
        finally:
            browser.close()
            
def compare_screenshots(old_path: str, new_bytes: bytes) -> dict:
    """
    Compare an existing screenshot with a newly captured PNG.
    Returns a visual-change flag and a difference percentage.
    """
    try:
        with Image.open(old_path) as old_image:
            old_image = old_image.convert("RGB")

        with Image.open(io.BytesIO(new_bytes)) as new_image:
            new_image = new_image.convert("RGB")

        # Normalize dimensions so ImageChops can compare the images.
        width = max(old_image.width, new_image.width)
        height = max(old_image.height, new_image.height)

        if old_image.size != (width, height):
            old_image = old_image.resize((width, height))

        if new_image.size != (width, height):
            new_image = new_image.resize((width, height))

        difference = ImageChops.difference(old_image, new_image)
        stats = ImageStat.Stat(difference)

        # Mean absolute RGB pixel difference, normalized to 0–100%.
        difference_percent = (
            sum(stats.mean) / (3 * 255)
        ) * 100

        # Ignore tiny pixel-level variations; tune this threshold as needed.
        visual_changed = difference_percent >= 0.5

        return {
            "visual_changed": visual_changed,
            "difference_percent": round(difference_percent, 3),
        }

    except (OSError, ValueError, ZeroDivisionError) as exc:
        return {
            "visual_changed": False,
            "difference_percent": 0.0,
            "error": str(exc),
        }
def generate_text_diff(old_text, new_text):
    import difflib

    old_lines = (old_text or "").splitlines()
    new_lines = (new_text or "").splitlines()

    diff = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="Previous version",
        tofile="Current version",
        lineterm=""
    ))

    return {
        "changed": bool(diff),
        "added_lines": sum(
            1 for line in diff
            if line.startswith("+") and not line.startswith("+++")
        ),
        "removed_lines": sum(
            1 for line in diff
            if line.startswith("-") and not line.startswith("---")
        ),
        "diff": "\n".join(diff)
    }


def generate_visual_diff(old_path, new_path, output_path):
    old_path = Path(old_path)
    new_path = Path(new_path)
    output_path = Path(output_path)

    if not old_path.is_file() or not new_path.is_file():
        return None

    with Image.open(old_path) as image:
        old_image = image.convert("RGB")

    with Image.open(new_path) as image:
        new_image = image.convert("RGB")

    width = max(old_image.width, new_image.width)
    height = max(old_image.height, new_image.height)

    old_canvas = Image.new("RGB", (width, height), "white")
    new_canvas = Image.new("RGB", (width, height), "white")

    old_canvas.paste(old_image, (0, 0))
    new_canvas.paste(new_image, (0, 0))

    difference = ImageChops.difference(old_canvas, new_canvas)

    # Make differences easier to see.
    difference = difference.point(lambda pixel: min(255, pixel * 4))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    difference.save(output_path, format="PNG")

    stats = ImageStat.Stat(difference)

    return {
        "diff_image": str(output_path),
        "mean_pixel_difference": round(sum(stats.mean) / 3, 2),
    }




def notification_cooldown_passed(conn, url, cooldown_minutes=60):
    row = conn.execute(
        """
        SELECT created_at
        FROM alerts
        WHERE url = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (url,),
    ).fetchone()

    if not row or not row[0]:
        return True

    try:
        last_alert = datetime.fromisoformat(
            row[0].replace("Z", "+00:00")
        )

        if last_alert.tzinfo is None:
            last_alert = last_alert.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)

        return now - last_alert >= timedelta(minutes=cooldown_minutes)

    except (ValueError, TypeError):
        # If the stored timestamp cannot be parsed, allow notification.
        return True


    

async def perform_check(
    url: str,
    name: str | None = None,
    interval_minutes: int | None = None,
):
    # Initialize variables before try so error handling is safe.
    changed = False
    text_changed = False
    visual_changed = False
    clean_url = url
    title = name or ""
    content = ""
    message = "No website changes detected."
    checked_at = now_utc()
    is_first_snapshot = False
    needs_screenshot_backfill = False
    notify_allowed = False
    visual_diff_path = None

    text_diff_result = {
        "changed": False,
        "added_lines": 0,
        "removed_lines": 0,
        "diff": "",
    }

    visual_result = {
        "visual_changed": False,
        "difference_percent": 0.0,
    }

    try:
        # Normalize and validate URL.
        clean_url = normalize_url(url)
        validate_public_url(clean_url)

        # Fetch page title, text content, and screenshot.
        title, content, screenshot_bytes = await asyncio.to_thread(
            fetch_website_sync,
            clean_url,
        )
        checked_at = now_utc()

        with sqlite3.connect(DB_PATH) as conn:
            # Fetch the latest saved snapshot.
            previous = conn.execute(
                """
                SELECT id, title, content, screenshot_path
                FROM snapshots
                WHERE url = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (clean_url,),
            ).fetchone()

            is_first_snapshot = previous is None
            needs_screenshot_backfill = (
                previous is not None and not previous[3]
            )

            # ----------------------------------------
            # TEXT CHANGE DETECTION
            # ----------------------------------------
            if previous is not None:
                text_diff_result = generate_text_diff(
                    previous[2] or "",
                    content or "",
                )

                text_changed = (
                    (previous[1] or "") != (title or "")
                    or (previous[2] or "") != (content or "")
                )

            # ----------------------------------------
            # VISUAL CHANGE DETECTION
            # ----------------------------------------
            visual_result = {
                "visual_changed": False,
                "difference_percent": 0.0,
            }
            visual_diff_path = None

            if previous is not None and previous[3]:
                old_screenshot = BASE_DIR / previous[3]

                if old_screenshot.is_file():
                    visual_result = await asyncio.to_thread(
                        compare_screenshots,
                        str(old_screenshot),
                        screenshot_bytes,
                    )

                    if visual_result["visual_changed"]:
                        screenshots_dir = BASE_DIR / "screenshots"
                        screenshots_dir.mkdir(
                            parents=True,
                            exist_ok=True,
                        )

                        diff_name = (
                            f"diff_{int(datetime.now(timezone.utc).timestamp() * 1000)}.png"
                        )
                        visual_diff_file = screenshots_dir / diff_name
                        temp_new_file = screenshots_dir / (
                            f"temp_{diff_name}"
                        )

                        try:
                            temp_new_file.write_bytes(screenshot_bytes)

                            diff_info = await asyncio.to_thread(
                                generate_visual_diff,
                                old_screenshot,
                                temp_new_file,
                                visual_diff_file,
                            )

                            visual_diff_path = diff_info.get(
                                "diff_image"
                            )
                        finally:
                            temp_new_file.unlink(missing_ok=True)

            visual_changed = bool(
                visual_result.get("visual_changed", False)
            )

            # IMPORTANT: define both variables before using them.
            changed = text_changed or visual_changed

            # ----------------------------------------
            # REGISTER WEBSITE
            # ----------------------------------------
            conn.execute(
                """
                INSERT OR IGNORE INTO monitored_websites
                    (url, added_at, name, interval_minutes, status)
                VALUES (?, ?, ?, ?, 'ONLINE')
                """,
                (
                    clean_url,
                    checked_at,
                    (name or title or clean_url)[:120],
                    max(15, min(1440, int(interval_minutes or 60))),
                ),
            )

            if name:
                conn.execute(
                    """
                    UPDATE monitored_websites
                    SET name = ?
                    WHERE url = ?
                    """,
                    (name[:120], clean_url),
                )

            if interval_minutes is not None:
                conn.execute(
                    """
                    UPDATE monitored_websites
                    SET interval_minutes = ?
                    WHERE url = ?
                    """,
                    (
                        max(15, min(1440, int(interval_minutes))),
                        clean_url,
                    ),
                )

            # ----------------------------------------
            # BUILD CHANGE MESSAGE
            # ----------------------------------------
            if text_changed and visual_changed:
                message = (
                    "Website text and visual appearance have changed."
                )
            elif visual_changed:
                message = (
                    "Website visual appearance has changed. "
                    f"Pixel difference: "
                    f"{visual_result['difference_percent']}%."
                )
            elif text_changed:
                message = (
                    "Website title or text content has changed."
                )
            else:
                message = "No website changes detected."

            # ----------------------------------------
            # SAVE SCREENSHOT AND SNAPSHOT
            # ----------------------------------------
            if (
                is_first_snapshot
                or changed
                or needs_screenshot_backfill
            ):
                screenshots_dir = BASE_DIR / "screenshots"
                screenshots_dir.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                screenshot_name = (
                    f"snapshot_{int(datetime.now(timezone.utc).timestamp() * 1000)}.png"
                )
                screenshot_file = screenshots_dir / screenshot_name
                screenshot_file.write_bytes(screenshot_bytes)

                screenshot_relative_path = str(
                    FilePath("screenshots") / screenshot_name
                )

                if needs_screenshot_backfill and not changed:
                    # Backfill screenshot without creating a fake change.
                    conn.execute(
                        """
                        UPDATE snapshots
                        SET screenshot_path = ?
                        WHERE id = ?
                        """,
                        (screenshot_relative_path, previous[0]),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO snapshots
                            (url, title, content, checked_at, screenshot_path)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            clean_url,
                            title,
                            content,
                            checked_at,
                            screenshot_relative_path,
                        ),
                    )

            # ----------------------------------------
            # SAVE ALERT AND CHECK COOLDOWN
            # ----------------------------------------
            if changed:
                notify_allowed = notification_cooldown_passed(
                    conn,
                    clean_url,
                    cooldown_minutes=60,
                )

                conn.execute(
                    """
                    INSERT INTO alerts
                        (url, title, message, created_at, is_read)
                    VALUES (?, ?, ?, ?, 0)
                    """,
                    (clean_url, title, message, checked_at),
                )

            # ----------------------------------------
            # UPDATE STATUS
            # ----------------------------------------
            conn.execute(
                """
                UPDATE monitored_websites
                SET
                    last_checked = ?,
                    status = 'ONLINE',
                    last_changed = CASE
                        WHEN ? THEN ?
                        ELSE last_changed
                    END
                WHERE url = ?
                """,
                (
                    checked_at,
                    int(changed),
                    checked_at,
                    clean_url,
                ),
            )

            conn.commit()

        # --------------------------------------------
        # SEND NOTIFICATIONS AFTER DATABASE COMMIT
        # --------------------------------------------
        if changed and notify_allowed:
            await asyncio.to_thread(
                send_all_notifications,
                clean_url,
                title or "Website Change Detected",
                message,
            )

        if changed:
            result_message = "Website change detected!"
        elif is_first_snapshot:
            result_message = "First snapshot saved."
        else:
            result_message = (
                "Checked successfully. No change detected."
            )

        return {
            "success": True,
            "url": clean_url,
            "title": title,
            "changed": changed,
            "text_diff": text_diff_result,
            "visual_diff_path": visual_diff_path,
            "visual_difference_percent": (
                visual_result["difference_percent"]
            ),
            "message": result_message,
        }

    except Exception as exc:
        error_url = clean_url or url
        error_message = (
            f"Check failed: {type(exc).__name__}: {exc}"
        )

        print(
            f"Website check failed for {error_url}: "
            f"{error_message}"
        )

        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    """
                    UPDATE monitored_websites
                    SET last_checked = ?, status = 'ERROR'
                    WHERE url = ?
                    """,
                    (now_utc(), error_url),
                )
                conn.commit()
        except Exception as db_exc:
            print(
                "Could not update website error status: "
                f"{type(db_exc).__name__}: {db_exc}"
            )

        return {
            "success": False,
            "url": error_url,
            "title": title or "",
            "changed": False,
            "message": error_message,
        }





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
            SELECT id, url, title, content, checked_at, screenshot_path
            FROM snapshots
            WHERE url = ?
            ORDER BY id DESC
            LIMIT 50
            """,
            (url,),
        ).fetchall()

    history = []

    for index, row in enumerate(rows):
        item = dict(row)

        # Return a screenshot URL only if the file exists.
        screenshot_path = item.get("screenshot_path")
        screenshot_url = None

        if screenshot_path:
            screenshot_file = BASE_DIR / screenshot_path

            if screenshot_file.is_file():
                screenshot_url = (
                    f"/snapshots/{item['id']}/screenshot"
                )

        item["screenshot_url"] = screenshot_url

        # Compare this snapshot with the next older snapshot.
        if index + 1 < len(rows):
            older = dict(rows[index + 1])

            old_lines = (older.get("content") or "").splitlines()
            new_lines = (item.get("content") or "").splitlines()

            diff = list(difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile="Previous version",
                tofile="Current version",
                lineterm="",
            ))

            item["text_diff"] = "\n".join(diff)
            item["added_lines"] = sum(
                1 for line in diff
                if line.startswith("+") and not line.startswith("+++")
            )
            item["removed_lines"] = sum(
                1 for line in diff
                if line.startswith("-") and not line.startswith("---")
            )
        else:
            item["text_diff"] = ""
            item["added_lines"] = 0
            item["removed_lines"] = 0

        # Do not send the full page content separately.
        item.pop("content", None)
        item.pop("screenshot_path", None)

        history.append(item)

    return history

@app.get("/history/{snapshot_id}/visual-diff")
async def get_visual_diff(snapshot_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        current = conn.execute(
            """
            SELECT id, url, screenshot_path
            FROM snapshots WHERE id = ?
            """,
            (snapshot_id,),
        ).fetchone()

        if current is None:
            raise HTTPException(status_code=404, detail="Snapshot not found.")

        previous = conn.execute(
            """
            SELECT id, screenshot_path
            FROM snapshots
            WHERE url = ? AND id < ?
            ORDER BY id DESC LIMIT 1
            """,
            (current["url"], snapshot_id),
        ).fetchone()

    if not previous or not previous["screenshot_path"] or not current["screenshot_path"]:
        raise HTTPException(
            status_code=404,
            detail="Both snapshots need screenshots for comparison.",
        )

    old_path = BASE_DIR / previous["screenshot_path"]
    new_path = BASE_DIR / current["screenshot_path"]

    output_path = BASE_DIR / "screenshots" / f"visual_diff_{snapshot_id}.png"

    result = await asyncio.to_thread(
        generate_visual_diff, old_path, new_path, output_path
    )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="A screenshot file is missing.",
        )

    return {
        "snapshot_id": snapshot_id,
        "previous_snapshot_id": previous["id"],
        "visual_diff_url": f"/visual-diff/{snapshot_id}",
        "mean_pixel_difference": result["mean_pixel_difference"],
    }


@app.get("/visual-diff/{snapshot_id}")
async def serve_visual_diff(snapshot_id: int):
    from fastapi.responses import FileResponse

    output_path = BASE_DIR / "screenshots" / f"visual_diff_{snapshot_id}.png"

    if not output_path.is_file():
        raise HTTPException(status_code=404, detail="Visual diff not generated.")

    return FileResponse(output_path, media_type="image/png")




from fastapi.responses import FileResponse

@app.get("/snapshots/{snapshot_id}/screenshot")
async def get_snapshot_screenshot(snapshot_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT screenshot_path FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Snapshot not found.")

    screenshot_path = row["screenshot_path"]

    if not screenshot_path:
        raise HTTPException(
            status_code=404,
            detail="No screenshot is available for this snapshot.",
        )

    path = (BASE_DIR / screenshot_path).resolve()
    screenshots_dir = (BASE_DIR / "screenshots").resolve()

    if not path.is_relative_to(screenshots_dir):
        raise HTTPException(status_code=400, detail="Invalid screenshot path.")

    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Screenshot file is missing from disk.",
        )

    return FileResponse(path, media_type="image/png")


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
