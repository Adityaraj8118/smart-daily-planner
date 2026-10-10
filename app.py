"""
Smart Daily Planner — backend (Flask + SQLite locally / Postgres in production)

- Sign up: name + email + password -> 6-digit OTP emailed -> verified account
- Log in: email (or username) + password
- Tasks are stored per user in the database (DATABASE_URL -> Postgres, else SQLite)
- Web Push: once a day, users get a phone/desktop notification for tasks
  that are due today or overdue
"""

import base64
import hashlib
import json
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_from_directory, session
from py_vapid import Vapid01
from pywebpush import WebPushException, webpush
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PUBLIC_DIR = BASE_DIR / "public"
DB_PATH = DATA_DIR / "planner.db"
VAPID_KEY_PATH = DATA_DIR / "vapid_private.pem"
SECRET_KEY_PATH = DATA_DIR / "secret_key"

load_dotenv(BASE_DIR / ".env")
DATA_DIR.mkdir(exist_ok=True)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")  # HTTPS email API, for hosts that block SMTP
MAIL_FROM = os.getenv("MAIL_FROM", SMTP_USER)
VAPID_CONTACT = os.getenv("VAPID_CONTACT", "mailto:" + (parseaddr(MAIL_FROM)[1] or "admin@example.com"))
EMAIL_ENABLED = bool(BREVO_API_KEY and MAIL_FROM) or bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)
DATABASE_URL = os.getenv("DATABASE_URL", "")
NOTIFY_HOUR = int(os.getenv("NOTIFY_HOUR", "8"))  # local hour after which the daily reminder is sent

OTP_TTL_SECONDS = 10 * 60
OTP_RESEND_COOLDOWN = 60
OTP_MAX_ATTEMPTS = 5
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 15 * 60

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_or_create_secret_key():
    if os.getenv("SECRET_KEY"):
        return os.getenv("SECRET_KEY")
    if not SECRET_KEY_PATH.exists():
        SECRET_KEY_PATH.write_text(secrets.token_hex(32))
    return SECRET_KEY_PATH.read_text().strip()


app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=load_or_create_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

# ---------------------------------------------------------
# Database
# ---------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  username TEXT NOT NULL UNIQUE,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  timezone TEXT,
  last_notified_date TEXT,
  created_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_signups (
  email TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  otp_hash TEXT NOT NULL,
  expires_at BIGINT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_sent_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  deadline TEXT,
  completed INTEGER NOT NULL DEFAULT 0,
  created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
CREATE TABLE IF NOT EXISTS push_subscriptions (
  endpoint TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subscription_json TEXT NOT NULL,
  created_at BIGINT NOT NULL
);
"""


class Database:
    """Runs the same SQL (with ? placeholders) on SQLite locally and on Postgres when DATABASE_URL is set."""

    def __init__(self):
        self.pg = bool(DATABASE_URL)
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row

            self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql, params=()):
        if self.pg:
            sql = sql.replace("?", "%s")
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def connect():
    return Database()


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = connect()
    try:
        if db.pg:
            db.conn.execute(SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"))
        else:
            db.conn.executescript(SCHEMA)
        db.commit()
    finally:
        db.close()


init_db()

# ---------------------------------------------------------
# VAPID keys for Web Push. In production they come from the VAPID_SECRET
# env var (so they survive restarts); locally they're generated into data/.
# ---------------------------------------------------------

P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def load_vapid():
    secret = os.getenv("VAPID_SECRET")
    if secret:
        seed = int.from_bytes(hashlib.sha256(secret.encode()).digest(), "big")
        return Vapid01(private_key=ec.derive_private_key(seed % (P256_ORDER - 1) + 1, ec.SECP256R1()))
    if not VAPID_KEY_PATH.exists():
        v = Vapid01()
        v.generate_keys()
        v.save_key(str(VAPID_KEY_PATH))
    return Vapid01.from_file(str(VAPID_KEY_PATH))


_vapid = load_vapid()
VAPID_PUBLIC_KEY = base64.urlsafe_b64encode(
    _vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
).decode().rstrip("=")

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------


def now():
    return int(time.time())


def error(message, status=400):
    return jsonify({"error": message}), status


def json_body():
    return request.get_json(silent=True) or {}


@app.before_request
def require_json_for_writes():
    # Cross-site forms can't send application/json without a CORS preflight,
    # so this doubles as CSRF protection for the cookie-based session.
    if request.path.startswith("/api/") and request.method in ("POST", "PATCH", "PUT"):
        if not request.is_json:
            return error("Expected JSON.", 415)


def current_user():
    uid = session.get("user_id")
    if uid is None:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return error("Please log in.", 401)
        return fn(user, *args, **kwargs)

    return wrapper


def public_user(user):
    return {"name": user["name"], "username": user["username"], "email": user["email"]}


def start_session(user_id):
    session.clear()
    session.permanent = True
    session["user_id"] = user_id


def make_username(db, name):
    base = re.sub(r"[^a-z]", "", name.lower())[:12] or "user"
    for _ in range(50):
        candidate = f"{base}{secrets.randbelow(9000) + 1000}"
        if not db.execute("SELECT 1 FROM users WHERE username = ?", (candidate,)).fetchone():
            return candidate
    return f"{base}{secrets.token_hex(4)}"


def send_otp_email(email, name, otp):
    if not EMAIL_ENABLED:
        # Dev mode: no email configured, so print the code to the server console.
        print(f"\n[DEV MODE] OTP for {email}: {otp}\n", flush=True)
        return
    subject = f"{otp} is your Smart Daily Planner code"
    text = (
        f"Hi {name},\n\nYour verification code is: {otp}\n\n"
        f"It expires in {OTP_TTL_SECONDS // 60} minutes. "
        "If you didn't try to create an account, you can ignore this email.\n"
    )
    if BREVO_API_KEY:
        sender_name, sender_email = parseaddr(MAIL_FROM)
        res = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "accept": "application/json"},
            json={
                "sender": {"name": sender_name or "Smart Daily Planner", "email": sender_email},
                "to": [{"email": email}],
                "subject": subject,
                "textContent": text,
            },
            timeout=20,
        )
        if not res.ok:
            raise RuntimeError(f"Brevo API {res.status_code}: {res.text[:300]}")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = email
    msg.set_content(text)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)


def issue_otp(db, email, name, password_hash):
    otp = f"{secrets.randbelow(1_000_000):06d}"
    db.execute(
        """INSERT INTO pending_signups (email, name, password_hash, otp_hash, expires_at, attempts, last_sent_at)
           VALUES (?, ?, ?, ?, ?, 0, ?)
           ON CONFLICT(email) DO UPDATE SET name = excluded.name, password_hash = excluded.password_hash,
             otp_hash = excluded.otp_hash, expires_at = excluded.expires_at, attempts = 0,
             last_sent_at = excluded.last_sent_at""",
        (email, name, password_hash, generate_password_hash(otp), now() + OTP_TTL_SECONDS, now()),
    )
    db.commit()
    send_otp_email(email, name, otp)


# In-memory login throttling: { email: (failures, locked_until) }
_login_failures = {}
_login_lock = threading.Lock()


# ---------------------------------------------------------
# Auth API
# ---------------------------------------------------------


@app.post("/api/signup")
def signup():
    data = json_body()
    name = str(data.get("name", "")).strip()[:60]
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    if not name:
        return error("Please enter your name.")
    if not EMAIL_RE.match(email):
        return error("Please enter a valid email address.")
    if len(password) < 8:
        return error("Password must be at least 8 characters.")

    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        return error("An account with this email already exists. Please log in.", 409)

    pending = db.execute("SELECT last_sent_at FROM pending_signups WHERE email = ?", (email,)).fetchone()
    if pending and now() - pending["last_sent_at"] < OTP_RESEND_COOLDOWN:
        wait = OTP_RESEND_COOLDOWN - (now() - pending["last_sent_at"])
        return error(f"Please wait {wait}s before requesting another code.", 429)

    try:
        issue_otp(db, email, name, generate_password_hash(password))
    except Exception as exc:  # SMTP problems
        print("Failed to send OTP email:", exc, flush=True)
        return error("Couldn't send the verification email. Please try again later.", 502)

    return jsonify({"ok": True, "devMode": not EMAIL_ENABLED})


@app.post("/api/resend-otp")
def resend_otp():
    email = str(json_body().get("email", "")).strip().lower()
    db = get_db()
    pending = db.execute("SELECT * FROM pending_signups WHERE email = ?", (email,)).fetchone()
    if not pending:
        return error("Please start sign-up again.", 404)
    if now() - pending["last_sent_at"] < OTP_RESEND_COOLDOWN:
        wait = OTP_RESEND_COOLDOWN - (now() - pending["last_sent_at"])
        return error(f"Please wait {wait}s before requesting another code.", 429)
    try:
        issue_otp(db, email, pending["name"], pending["password_hash"])
    except Exception as exc:
        print("Failed to send OTP email:", exc, flush=True)
        return error("Couldn't send the verification email. Please try again later.", 502)
    return jsonify({"ok": True, "devMode": not EMAIL_ENABLED})


@app.post("/api/verify-otp")
def verify_otp():
    data = json_body()
    email = str(data.get("email", "")).strip().lower()
    otp = str(data.get("otp", "")).strip()

    db = get_db()
    pending = db.execute("SELECT * FROM pending_signups WHERE email = ?", (email,)).fetchone()
    if not pending:
        return error("No pending sign-up for this email. Please sign up again.", 404)
    if now() > pending["expires_at"]:
        return error("This code has expired. Please request a new one.", 410)
    if pending["attempts"] >= OTP_MAX_ATTEMPTS:
        return error("Too many wrong attempts. Please request a new code.", 429)

    if not re.fullmatch(r"\d{6}", otp) or not check_password_hash(pending["otp_hash"], otp):
        db.execute("UPDATE pending_signups SET attempts = attempts + 1 WHERE email = ?", (email,))
        db.commit()
        left = OTP_MAX_ATTEMPTS - pending["attempts"] - 1
        return error(f"Incorrect code. {left} attempt(s) left." if left > 0 else
                     "Too many wrong attempts. Please request a new code.")

    if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        db.execute("DELETE FROM pending_signups WHERE email = ?", (email,))
        db.commit()
        return error("An account with this email already exists. Please log in.", 409)

    username = make_username(db, pending["name"])
    user = db.execute(
        "INSERT INTO users (name, username, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?) RETURNING *",
        (pending["name"], username, email, pending["password_hash"], now()),
    ).fetchone()
    db.execute("DELETE FROM pending_signups WHERE email = ?", (email,))
    db.commit()

    start_session(user["id"])
    return jsonify({"user": public_user(user)})


@app.post("/api/login")
def login():
    data = json_body()
    identifier = str(data.get("identifier", "")).strip().lower()
    password = str(data.get("password", ""))
    if not identifier or not password:
        return error("Please enter your email/username and password.")

    with _login_lock:
        failures, locked_until = _login_failures.get(identifier, (0, 0))
        if locked_until > now():
            return error("Too many failed attempts. Try again in a few minutes.", 429)

    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE email = ? OR lower(username) = ?", (identifier, identifier)
    ).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        with _login_lock:
            failures += 1
            _login_failures[identifier] = (
                (0, now() + LOGIN_LOCK_SECONDS) if failures >= LOGIN_MAX_FAILURES else (failures, 0)
            )
        return error("Incorrect email/username or password.", 401)

    with _login_lock:
        _login_failures.pop(identifier, None)
    start_session(user["id"])
    return jsonify({"user": public_user(user)})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
@login_required
def me(user):
    return jsonify({"user": public_user(user)})


# ---------------------------------------------------------
# Tasks API
# ---------------------------------------------------------


def task_json(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "deadline": row["deadline"],
        "completed": bool(row["completed"]),
        "createdAt": row["created_at"],
    }


def clean_deadline(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValueError
    datetime.strptime(value, "%Y-%m-%d")
    return value


@app.get("/api/tasks")
@login_required
def list_tasks(user):
    rows = get_db().execute("SELECT * FROM tasks WHERE user_id = ?", (user["id"],)).fetchall()
    return jsonify({"tasks": [task_json(r) for r in rows]})


@app.post("/api/tasks")
@login_required
def create_task(user):
    data = json_body()
    title = str(data.get("title", "")).strip()[:200]
    if not title:
        return error("Task title can't be empty.")
    try:
        deadline = clean_deadline(data.get("deadline"))
    except ValueError:
        return error("Invalid deadline date.")
    task_id = secrets.token_hex(12)
    db = get_db()
    db.execute(
        "INSERT INTO tasks (id, user_id, title, deadline, completed, created_at) VALUES (?, ?, ?, ?, 0, ?)",
        (task_id, user["id"], title, deadline, int(time.time() * 1000)),
    )
    db.commit()
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return jsonify({"task": task_json(row)}), 201


@app.post("/api/tasks/import")
@login_required
def import_tasks(user):
    """One-time import of tasks that were saved in the browser before accounts existed."""
    items = json_body().get("tasks")
    if not isinstance(items, list):
        return error("Expected a list of tasks.")
    db = get_db()
    imported = 0
    for t in items[:500]:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title", "")).strip()[:200]
        if not title:
            continue
        try:
            deadline = clean_deadline(t.get("deadline"))
        except ValueError:
            deadline = None
        created = t.get("createdAt") if isinstance(t.get("createdAt"), (int, float)) else time.time() * 1000
        db.execute(
            "INSERT INTO tasks (id, user_id, title, deadline, completed, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (secrets.token_hex(12), user["id"], title, deadline, 1 if t.get("completed") is True else 0, int(created)),
        )
        imported += 1
    db.commit()
    return jsonify({"imported": imported})


@app.patch("/api/tasks/<task_id>")
@login_required
def update_task(user, task_id):
    data = json_body()
    db = get_db()
    row = db.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
    if row is None:
        return error("Task not found.", 404)
    if "completed" in data:
        db.execute("UPDATE tasks SET completed = ? WHERE id = ?", (1 if data["completed"] else 0, task_id))
    db.commit()
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return jsonify({"task": task_json(row)})


@app.delete("/api/tasks/<task_id>")
@login_required
def delete_task(user, task_id):
    db = get_db()
    cur = db.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
    db.commit()
    if cur.rowcount == 0:
        return error("Task not found.", 404)
    return jsonify({"ok": True})


# ---------------------------------------------------------
# Push notifications
# ---------------------------------------------------------


@app.get("/api/push/public-key")
def push_public_key():
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


@app.post("/api/push/subscribe")
@login_required
def push_subscribe(user):
    data = json_body()
    sub = data.get("subscription")
    if not isinstance(sub, dict) or not str(sub.get("endpoint", "")).startswith("https://"):
        return error("Invalid push subscription.")
    tz = str(data.get("timezone", ""))[:64]
    try:
        ZoneInfo(tz)
    except Exception:
        tz = None
    db = get_db()
    db.execute(
        """INSERT INTO push_subscriptions (endpoint, user_id, subscription_json, created_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id,
             subscription_json = excluded.subscription_json""",
        (sub["endpoint"], user["id"], json.dumps(sub), now()),
    )
    if tz:
        db.execute("UPDATE users SET timezone = ? WHERE id = ?", (tz, user["id"]))
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/push/unsubscribe")
@login_required
def push_unsubscribe(user):
    endpoint = str(json_body().get("endpoint", ""))
    db = get_db()
    db.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?", (endpoint, user["id"]))
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/push/test")
@login_required
def push_test(user):
    sent = send_push_to_user(get_db(), user["id"], {
        "title": "Notifications are on ✅",
        "body": "You'll get a daily reminder for tasks due today or overdue.",
        "url": "/",
    })
    if sent == 0:
        return error("No device is subscribed yet.", 404)
    return jsonify({"sent": sent})


def send_push_to_user(db, user_id, payload):
    subs = db.execute("SELECT * FROM push_subscriptions WHERE user_id = ?", (user_id,)).fetchall()
    sent = 0
    for s in subs:
        try:
            webpush(
                subscription_info=json.loads(s["subscription_json"]),
                data=json.dumps(payload),
                vapid_private_key=_vapid,
                vapid_claims={"sub": VAPID_CONTACT},  # fresh dict: webpush mutates it
                ttl=12 * 60 * 60,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):  # subscription expired / unsubscribed
                db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (s["endpoint"],))
                db.commit()
            else:
                print("Push failed:", exc, flush=True)
    return sent


def reminder_payload(tasks, today):
    due_today = [t for t in tasks if t["deadline"] == today]
    overdue = [t for t in tasks if t["deadline"] < today]
    parts = []
    if due_today:
        parts.append(f"{len(due_today)} due today")
    if overdue:
        parts.append(f"{len(overdue)} overdue")
    titles = [t["title"] for t in (due_today + overdue)[:3]]
    body = " • ".join(titles)
    if len(due_today) + len(overdue) > 3:
        body += f" and {len(due_today) + len(overdue) - 3} more"
    return {"title": "Pending tasks: " + ", ".join(parts), "body": body, "url": "/"}


def run_daily_reminders():
    db = connect()
    try:
        users = db.execute(
            "SELECT DISTINCT u.* FROM users u JOIN push_subscriptions p ON p.user_id = u.id"
        ).fetchall()
        for user in users:
            try:
                tz = ZoneInfo(user["timezone"]) if user["timezone"] else timezone.utc
            except Exception:
                tz = timezone.utc
            local_now = datetime.now(tz)
            today = local_now.strftime("%Y-%m-%d")
            if local_now.hour < NOTIFY_HOUR or user["last_notified_date"] == today:
                continue
            tasks = db.execute(
                """SELECT title, deadline FROM tasks
                   WHERE user_id = ? AND completed = 0 AND deadline IS NOT NULL AND deadline <= ?
                   ORDER BY deadline""",
                (user["id"], today),
            ).fetchall()
            if tasks:
                send_push_to_user(db, user["id"], reminder_payload(tasks, today))
            db.execute("UPDATE users SET last_notified_date = ? WHERE id = ?", (today, user["id"]))
            db.commit()
    finally:
        db.close()


def reminder_loop():
    while True:
        try:
            run_daily_reminders()
        except Exception as exc:
            print("Reminder job error:", exc, flush=True)
        time.sleep(5 * 60)


# ---------------------------------------------------------
# Static pages
# ---------------------------------------------------------


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def index_page():
    return send_from_directory(PUBLIC_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    response = send_from_directory(PUBLIC_DIR, filename)
    if filename == "sw.js":
        response.headers["Cache-Control"] = "no-cache"
    return response


# Runs inside the (single) web worker, both with `python app.py` and gunicorn.
if os.getenv("RUN_REMINDERS", "1") == "1":
    threading.Thread(target=reminder_loop, daemon=True).start()

if not EMAIL_ENABLED:
    print("Email is not configured: OTP codes will be printed here (dev mode).", flush=True)

if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")))
