import os
import sys
import sqlite3
import joblib
import webbrowser
import re
import secrets
import hashlib
import mimetypes
import socket
import ipaddress
from urllib.parse import urlparse
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
from flask_session import Session
from groq import Groq
from groq._base_client import APIConnectionError
import requests
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# === RESOURCE PATH ===
def resource_path(rel_path):
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base, rel_path)

# === CONFIG & MODELS ===
# Load local environment variables from .env when available.
load_dotenv()

app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static")
)
app.secret_key = os.environ.get("SECRET_KEY", "quantumshade_secret")
app.config["SESSION_TYPE"] = "filesystem"
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB per request
Session(app)

# Load sentiment model - Systemic dependency
try:
    sentiment_model = joblib.load(resource_path("model/sentiment_model.pkl"))
except Exception as e:
    print(f"CRITICAL ERROR: Could not load sentiment model: {e}")
    sys.exit(1)

DB_FILE = "chat.db"
UPLOAD_DIR = resource_path("uploads")
MODEL = "llama-3.1-8b-instant"
ENABLE_HANDOFF = True
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    print("CRITICAL ERROR: GROQ_API_KEY not set in environment.")
    sys.exit(1)
client = Groq(api_key=GROQ_API_KEY)
DEBUG = os.environ.get("XENOBIZ_DEBUG", "0") == "1"
ALLOWED_UPLOAD_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".log", ".xml",
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".pdf"
}

os.makedirs(UPLOAD_DIR, exist_ok=True)

# === DATABASE LOGIC ===
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                last_reported TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS resolution_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL UNIQUE,
                response_mode TEXT DEFAULT "NORMAL",
                disable_speculation INTEGER DEFAULT 0,
                simplify_output INTEGER DEFAULT 0,
                require_verifiable_only INTEGER DEFAULT 0,
                limit_scope INTEGER DEFAULT 0,
                refuse_if_repeated INTEGER DEFAULT 0,
                last_updated TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS issue_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL,
                reported_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS handoff_locks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL UNIQUE,
                locked INTEGER DEFAULT 0,
                last_updated TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS recovery_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code_hash TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL,
                issued_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL,
                revoked INTEGER DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT 'New Chat',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                conversation_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                mime_type TEXT,
                file_size INTEGER,
                extracted_text TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(conversation_id) REFERENCES conversation_threads(id)
            )
        """)

        columns = [r["name"] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()]
        if "user_id" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN user_id INTEGER")
        if "conversation_id" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN conversation_id INTEGER")
        user_columns = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "subscription_status" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT DEFAULT 'free'")

init_db()

# === AUTH LOGIC ===
def normalize_email(email):
    return (email or "").strip().lower()

def hash_token(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def generate_recovery_code():
    raw = secrets.token_hex(4).upper()
    return f"{raw[:4]}-{raw[4:]}"

def get_current_user_id():
    now_iso = datetime.now().isoformat()
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header.split(" ", 1)[1].strip()
        if bearer_token:
            with get_db_connection() as conn:
                row = conn.execute(
                    """
                    SELECT user_id FROM session_tokens
                    WHERE token_hash = ? AND revoked = 0 AND expires_at > ?
                    """,
                    (hash_token(bearer_token), now_iso),
                ).fetchone()
            return int(row["user_id"]) if row else None

    user_id = session.get("user_id")
    token = session.get("auth_token")
    if not user_id or not token:
        return None

    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT id FROM session_tokens
            WHERE user_id = ? AND token_hash = ? AND revoked = 0 AND expires_at > ?
            """,
            (user_id, hash_token(token), now_iso),
        ).fetchone()
    return int(user_id) if row else None

def issue_session_token(user_id):
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(days=7)).isoformat()
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO session_tokens (user_id, token_hash, expires_at)
            VALUES (?, ?, ?)
            """,
            (user_id, hash_token(token), expires_at),
        )
    session["user_id"] = user_id
    session["auth_token"] = token
    return token, expires_at

def revoke_current_session():
    user_id = session.get("user_id")
    token = session.get("auth_token")
    if user_id and token:
        with get_db_connection() as conn:
            conn.execute(
                """
                UPDATE session_tokens
                SET revoked = 1
                WHERE user_id = ? AND token_hash = ?
                """,
                (user_id, hash_token(token)),
            )
    session.clear()

def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not get_current_user_id():
            if request.path.startswith("/auth/") or request.path.startswith("/chat") or request.path.startswith("/history"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login_page"))
        return view_func(*args, **kwargs)
    return wrapped

def create_user(email, password):
    email = normalize_email(email)
    if not email or "@" not in email:
        return None, "Enter a valid email."
    if not password or len(password) < 8:
        return None, "Password must be at least 8 characters."

    password_hash = generate_password_hash(password)
try:
        with get_db_connection() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                (email, password_hash),
            )
            user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return None, "Email already registered."
    return user_id, None

def generate_and_store_recovery_codes(user_id, count=8):
    codes = [generate_recovery_code() for _ in range(count)]
    with get_db_connection() as conn:
        for code in codes:
            conn.execute(
                "INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, ?)",
                (user_id, hash_token(code)),
            )
    return codes

def verify_login(email, password):
    email = normalize_email(email)
    with get_db_connection() as conn:
        user = conn.execute(
            "SELECT id, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    if not user:
        return None
    if not check_password_hash(user["password_hash"], password or ""):
        return None
    return int(user["id"])

def consume_recovery_code_and_reset_password(email, recovery_code, new_password):
    email = normalize_email(email)
    if not new_password or len(new_password) < 8:
        return False, "New password must be at least 8 characters."
    if not recovery_code:
        return False, "Recovery code is required."

    with get_db_connection() as conn:
        user = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            return False, "Invalid email or recovery code."
        user_id = int(user["id"])
        code_hash = hash_token(recovery_code.strip().upper())
        code_row = conn.execute(
            """
            SELECT id FROM recovery_codes
            WHERE user_id = ? AND code_hash = ? AND used_at IS NULL
            """,
            (user_id, code_hash),
        ).fetchone()
        if not code_row:
            unused_count = conn.execute(
                "SELECT COUNT(*) AS c FROM recovery_codes WHERE user_id = ? AND used_at IS NULL",
                (user_id,),
            ).fetchone()
            if int(unused_count["c"]) <= 0:
                return False, "All recovery codes are used. Manual support is required."
            return False, "Invalid email or recovery code."

        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), user_id),
        )
        conn.execute(
            "UPDATE recovery_codes SET used_at = ? WHERE id = ?",
            (datetime.now().isoformat(), int(code_row["id"])),
        )
    return True, "Password reset successful."

def get_remaining_recovery_codes(user_id):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM recovery_codes WHERE user_id = ? AND used_at IS NULL",
            (user_id,),
        ).fetchone()
    return int(row["c"]) if row else 0

def get_subscription_status(user_id):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT subscription_status FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return (row["subscription_status"] if row else "free") or "free"

def get_upload_limit_for_user(user_id):
    status = get_subscription_status(user_id).lower()
    if status in ("paid", "active", "pro"):
        return None
    return 2

def get_upload_count_for_user(user_id):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM attachments WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row["c"]) if row else 0

def create_conversation_thread(user_id, title="New Chat"):
    safe_title = (title or "New Chat").strip()[:120] or "New Chat"
    now = datetime.now().isoformat()
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO conversation_threads (user_id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, safe_title, now, now),
        )
    return int(cur.lastrowid)

def list_conversation_threads(user_id):
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.title, t.created_at, t.updated_at,
                   (
                     SELECT c.content
                     FROM conversations c
                     WHERE c.user_id = t.user_id AND c.conversation_id = t.id
                     ORDER BY c.id DESC
                     LIMIT 1
                   ) AS last_message
            FROM conversation_threads t
            WHERE t.user_id = ?
            ORDER BY t.updated_at DESC, t.id DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]

def get_conversation_thread(user_id, conversation_id):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id, user_id, title, created_at, updated_at FROM conversation_threads WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
    return dict(row) if row else None

def rename_conversation_thread(user_id, conversation_id, title):
    safe_title = (title or "").strip()[:120]
    if not safe_title:
        return False, "Title is required."
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id FROM conversation_threads WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if not row:
            return False, "Conversation not found."
        conn.execute(
            "UPDATE conversation_threads SET title = ?, updated_at = ? WHERE id = ?",
            (safe_title, datetime.now().isoformat(), conversation_id),
        )
    return True, safe_title

def delete_conversation_thread(user_id, conversation_id):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id FROM conversation_threads WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "DELETE FROM conversations WHERE user_id = ? AND conversation_id = ?",
            (user_id, conversation_id),
        )
        conn.execute(
            "DELETE FROM conversation_threads WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )
    if get_active_conversation_id() == int(conversation_id):
        session.pop("active_conversation_id", None)
    return True

def get_active_conversation_id():
    convo_id = session.get("active_conversation_id")
    return int(convo_id) if convo_id else None

def resolve_conversation_id(user_id, requested_conversation_id=None, create_if_missing=True):
    convo_id = requested_conversation_id or get_active_conversation_id()
    if convo_id:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT id FROM conversation_threads WHERE id = ? AND user_id = ?",
                (convo_id, user_id),
            ).fetchone()
        if row:
            session["active_conversation_id"] = int(convo_id)
            return int(convo_id)

    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id FROM conversation_threads WHERE user_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if row:
        session["active_conversation_id"] = int(row["id"])
        return int(row["id"])

    if create_if_missing:
        new_id = create_conversation_thread(user_id, title="New Chat")
        session["active_conversation_id"] = new_id
        return new_id
    return None

def touch_conversation(conversation_id):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE conversation_threads SET updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), conversation_id),
        )

def maybe_update_conversation_title(conversation_id, role, content):
    if role != "user":
        return
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT title FROM conversation_threads WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if not row:
            return
        current_title = (row["title"] or "").strip().lower()
        if current_title not in ("", "new chat"):
            return
        base = (content or "").strip().replace("\n", " ")
        if not base:
            return
        title = (base[:60] + "...") if len(base) > 60 else base
        conn.execute(
            "UPDATE conversation_threads SET title = ? WHERE id = ?",
            (title, conversation_id),
        )

def is_allowed_upload(filename):
    _, ext = os.path.splitext(filename or "")
    return ext.lower() in ALLOWED_UPLOAD_EXTENSIONS

def build_storage_name(filename):
    cleaned = secure_filename(filename or "file")
    if not cleaned:
        cleaned = "file"
    stem, ext = os.path.splitext(cleaned)
    return f"{stem}_{secrets.token_hex(8)}{ext.lower()}"

def extract_text_from_file(path, mime_type, max_chars=4000):
    text_like = (
        (mime_type or "").startswith("text/")
        or (mime_type or "") in ("application/json", "application/xml")
        or os.path.splitext(path)[1].lower() in (".txt", ".md", ".csv", ".json", ".log", ".xml")
    )
    if not text_like:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(max_chars)
        return content.strip() if content else None
    except Exception:
        return None

def save_attachment_record(user_id, conversation_id, original_name, stored_name, mime_type, file_size, extracted_text):
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO attachments
            (user_id, conversation_id, original_name, stored_name, mime_type, file_size, extracted_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                conversation_id,
                original_name,
                stored_name,
                mime_type,
                file_size,
                extracted_text,
                datetime.now().isoformat(),
            ),
        )
    return int(cur.lastrowid)

def get_attachment(user_id, attachment_id):
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT id, user_id, conversation_id, original_name, stored_name, mime_type, file_size, extracted_text, created_at
            FROM attachments
            WHERE id = ? AND user_id = ?
            """,
            (attachment_id, user_id),
        ).fetchone()
    return dict(row) if row else None

def get_attachments_for_chat(user_id, conversation_id, attachment_ids):
    if not attachment_ids:
        return []
    clean_ids = []
    for a_id in attachment_ids:
        try:
            clean_ids.append(int(a_id))
        except Exception:
            continue
    if not clean_ids:
        return []
    placeholders = ",".join(["?"] * len(clean_ids))
    params = [user_id, conversation_id] + clean_ids
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT id, original_name, mime_type, file_size, extracted_text
            FROM attachments
            WHERE user_id = ? AND conversation_id = ? AND id IN ({placeholders})
            ORDER BY id ASC
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]

def build_attachment_context(attachments):
    if not attachments:
        return ""
    blocks = []
    for a in attachments:
        name = a.get("original_name", "file")
        mime_type = a.get("mime_type") or "application/octet-stream"
        size = a.get("file_size") or 0
        extracted = a.get("extracted_text")
        if extracted:
            blocks.append(
                f"[Attachment: {name}, type={mime_type}, size={size} bytes]\n"
                f"Extracted text:\n{extracted[:3500]}"
            )
        else:
            blocks.append(
                f"[Attachment: {name}, type={mime_type}, size={size} bytes]\n"
                "No text extraction available; use this as supporting context."
            )
    return "\n\n".join(blocks)

def is_public_hostname(hostname):
    if not hostname:
        return False
    host = hostname.strip().lower()
    if host in ("localhost",):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        ip_text = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except Exception:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True

def is_safe_public_url(url):
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return is_public_hostname(parsed.hostname)

def extract_web_text(html, max_chars=3500):
    if not html:
        return ""
    no_script = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", html)
    no_tags = re.sub(r"(?is)<[^>]+>", " ", no_script)
    clean = re.sub(r"\\s+", " ", no_tags).strip()
    return clean[:max_chars]

def fetch_url_context(url, timeout=8):
    if not is_safe_public_url(url):
        return {"url": url, "ok": False, "error": "Blocked or invalid URL."}
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": "XenoBizBot/1.0 (+context-fetch)"},
        )
    except Exception:
        return {"url": url, "ok": False, "error": "Could not fetch URL."}

    ctype = (resp.headers.get("Content-Type") or "").lower()
    status = int(resp.status_code)
    if status >= 400:
        return {"url": url, "ok": False, "error": f"HTTP {status}."}

    if "text/html" in ctype:
        text = extract_web_text(resp.text or "")
    elif any(x in ctype for x in ("text/plain", "application/json", "application/xml")):
        text = (resp.text or "").strip()[:3500]
    else:
        return {"url": url, "ok": False, "error": f"Unsupported content type: {ctype or 'unknown'}."}

    return {
        "url": resp.url,
        "ok": True,
        "status": status,
        "content_type": ctype,
        "text": text,
    }

def build_url_context(url_inputs):
    if not url_inputs:
        return ""
    unique = []
    seen = set()
    for raw in url_inputs:
        u = (raw or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        unique.append(u)
        if len(unique) >= 3:
            break

    blocks = []
    for u in unique:
        fetched = fetch_url_context(u)
        if fetched.get("ok"):
            blocks.append(
                f"[URL: {fetched['url']}, status={fetched.get('status')}, type={fetched.get('content_type')}]\n"
                f"Extracted content:\n{fetched.get('text', '')}"
            )
        else:
            blocks.append(f"[URL: {u}] {fetched.get('error', 'Unavailable.')}")
    return "\n\n".join(blocks)

# === SYSTEM LOGIC ===
def analyze_sentiment(text):
    prediction = sentiment_model.predict([text])[0]
    probabilities = sentiment_model.predict_proba([text])[0]
    confidence = round(probabilities.max() * 100, 2)
    
    emotion_map = {
        "positive": "Happy 😊",
        "neutral": "Calm 😐",
        "negative": "Frustrated 😠"
    }
    return prediction, emotion_map.get(prediction, "Unknown"), confidence

def resolve_business_role(sentiment, confidence):
    if sentiment == "negative" and confidence > 60:
        return "customer_support_manager"
    elif sentiment == "positive" and confidence > 60:
        return "growth_manager"
    elif sentiment == "neutral":
        return "operations_manager"
    return "strategic_advisor"

ROLE_PROMPTS = {
    "customer_support_manager": "Senior support manager. De-escalate, protect trust, resolve.",
    "growth_manager": "Growth manager. Reinforce value, identify upsells, build loyalty.",
    "operations_manager": "Operations manager. Efficient, direct, frictionless.",
    "strategic_advisor": "Advisor. Clarify decisions, explain trade-offs, suggest next actions."
}

def decision_directive(sentiment, confidence):
    """Step 3: Internal decision logic to force action over scripts."""
    if sentiment == "negative" and confidence > 60:
        return (
            "Decision: Immediate corrective action required.\n"
            "Action: Log issue as high priority, propose a concrete fix, "
            "and communicate next steps clearly."
        )
    elif sentiment == "positive" and confidence > 60:
        return (
            "Decision: Strengthen and extend value.\n"
            "Action: Reinforce what works and identify opportunities "
            "for enhancement or expansion."
        )
    else:
        return (
            "Decision: Maintain stability and optimize.\n"
            "Action: Provide clear explanation and incremental improvement."
        )

ISSUE_TAGS = {
    "safety:hazard": ["dangerous", "unsafe", "hazard", "risk", "shock", "burn"],
    "misinformation:claims": ["not true", "wrong info", "misleading", "false", "misinformation"],
    "boundary:abuse": ["harass", "abuse", "hate", "threat", "illegal", "violence"],
    "usability:power_on": ["power on", "turn on", "start up", "boot", "won't start", "wont start"],
    "usability:setup": ["setup", "install", "installation", "pair", "connect", "onboarding"],
    "usability:controls": ["button", "controls", "navigation", "menu", "settings", "ui", "interface"],
    "performance:speed": ["slow", "lag", "latency", "delay", "freezing"],
    "performance:stability": ["crash", "error", "bug", "glitch", "stuck", "freeze"],
    "performance:cooling": ["hot", "overheat", "overheating", "cooling", "fan"],
    "reliability:battery": ["battery", "charge", "charging", "drain"],
    "hardware:damage": ["broken", "cracked", "damaged", "loose", "bent"],
    "design:ui": ["design", "layout", "look", "theme", "color", "font"],
    "cost:pricing": ["price", "pricing", "cost", "expensive", "cheap", "subscription"],
}

def detect_issue_tag(text):
    text_l = text.lower()
    for tag, keywords in ISSUE_TAGS.items():
        for kw in keywords:
            if kw in text_l:
                return tag
    return "general:other"

def is_complaint(text):
    text_l = text.lower()
    complaint_markers = ["issue", "problem", "difficulty", "can't", "cannot", "won't", "wont", "broken", "not working"]
    return any(m in text_l for m in complaint_markers)

def upsert_issue(tag):
    with get_db_connection() as conn:
        row = conn.execute("SELECT id, count FROM issues WHERE tag = ?", (tag,)).fetchone()
        if row:
            new_count = int(row["count"]) + 1
            conn.execute(
                "UPDATE issues SET count = ?, last_reported = ? WHERE id = ?",
                (new_count, datetime.now().isoformat(), row["id"]),
            )
            conn.execute(
                "INSERT INTO issue_events (tag, reported_at) VALUES (?, ?)",
                (tag, datetime.now().isoformat()),
            )
            return new_count
        else:
            conn.execute(
                "INSERT INTO issues (tag, count, last_reported) VALUES (?, ?, ?)",
                (tag, 1, datetime.now().isoformat()),
            )
            conn.execute(
                "INSERT INTO issue_events (tag, reported_at) VALUES (?, ?)",
                (tag, datetime.now().isoformat()),
            )
            return 1

def escalation_level(count):
    if count >= 5:
        return "escalated"
    if count >= 3:
        return "priority"
    if count >= 2:
        return "noted"
    return "first"

def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None

def get_issue_count_window(tag, hours=24):
    cutoff = datetime.now().timestamp() - (hours * 3600)
    cutoff_iso = datetime.fromtimestamp(cutoff).isoformat()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM issue_events WHERE tag = ? AND reported_at >= ?",
            (tag, cutoff_iso),
        ).fetchone()
    return int(row["c"]) if row else 0

def get_memory_statement(tag, total_count):
    if not tag or not total_count or total_count < 2:
        return None
    count_24h = get_issue_count_window(tag, hours=24)
    if count_24h >= 2:
        return "This issue has come up multiple times in the last 24 hours."
    return "You have raised this issue before."

def should_emit_memory_line(memory_line):
    if not memory_line:
        return False
    last = get_last_assistant_message()
    if not last:
        return True
    return memory_line not in last

def analyze_issue_state(tag, count, escalation):
    """Rule-based state classification (24h thresholds + severity + escalation)."""
    if not tag or count is None:
        return "IGNORABLE"
    count_24h = get_issue_count_window(tag, hours=24)

    severity_boost = 0
    if tag.startswith("safety:") or tag.startswith("misinformation:") or tag.startswith("boundary:"):
        severity_boost = 1

    # Base state by frequency within 24h
    if count_24h >= 5:
        state = "CRITICAL"
    elif count_24h >= 3:
        state = "ACTION_REQUIRED"
    elif count_24h >= 2:
        state = "MONITOR"
    else:
        state = "IGNORABLE"

    # Escalation floor (from Step 4)
    if escalation in ("escalated", "priority") and state == "MONITOR":
        state = "ACTION_REQUIRED"
    if escalation == "escalated" and state != "CRITICAL":
        state = "CRITICAL"

    # Severity boost
    if severity_boost:
        if state == "IGNORABLE":
            state = "MONITOR"
        elif state == "MONITOR":
            state = "ACTION_REQUIRED"
        elif state == "ACTION_REQUIRED":
            state = "CRITICAL"

    return state

def resolution_policies(issue_state, tag):
    """Map issue state to internal behavior flags."""
    # Naming map for Step 5 docs:
    # disable_speculation = NO_SPECULATION
    # require_verifiable_only = VERIFY_BEFORE_ASSERT
    flags = {
        "response_mode": "NORMAL",
        "disable_speculation": 0,
        "simplify_output": 0,
        "require_verifiable_only": 0,
        "limit_scope": 0,
        "refuse_if_repeated": 0,
    }
    if issue_state == "MONITOR":
        flags["simplify_output"] = 1
    elif issue_state == "ACTION_REQUIRED":
        flags["response_mode"] = "STRICT"
        flags["disable_speculation"] = 1
        flags["simplify_output"] = 1
        flags["limit_scope"] = 1
    elif issue_state == "CRITICAL":
        flags["response_mode"] = "STRICT"
        flags["disable_speculation"] = 1
        flags["simplify_output"] = 1
        flags["require_verifiable_only"] = 1
        flags["limit_scope"] = 1
        flags["refuse_if_repeated"] = 1
    return flags

def upsert_resolution_flags(tag, flags):
    with get_db_connection() as conn:
        row = conn.execute("SELECT id FROM resolution_flags WHERE tag = ?", (tag,)).fetchone()
        if row:
            conn.execute(
                """
                UPDATE resolution_flags
                SET response_mode = ?, disable_speculation = ?, simplify_output = ?,
                    require_verifiable_only = ?, limit_scope = ?, refuse_if_repeated = ?, last_updated = ?
                WHERE id = ?
                """,
                (
                    flags["response_mode"],
                    flags["disable_speculation"],
                    flags["simplify_output"],
                    flags["require_verifiable_only"],
                    flags["limit_scope"],
                    flags["refuse_if_repeated"],
                    datetime.now().isoformat(),
                    row["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO resolution_flags
                (tag, response_mode, disable_speculation, simplify_output, require_verifiable_only,
                 limit_scope, refuse_if_repeated, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tag,
                    flags["response_mode"],
                    flags["disable_speculation"],
                    flags["simplify_output"],
                    flags["require_verifiable_only"],
                    flags["limit_scope"],
                    flags["refuse_if_repeated"],
                    datetime.now().isoformat(),
                ),
            )

def get_resolution_flags(tag):
    if not tag:
        return None
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT response_mode, disable_speculation, simplify_output, require_verifiable_only, "
            "limit_scope, refuse_if_repeated "
            "FROM resolution_flags WHERE tag = ?",
            (tag,),
        ).fetchone()
    return dict(row) if row else None

def save_message(role, content, user_id=None, conversation_id=None):
    active_user_id = user_id or get_current_user_id()
    active_conversation_id = conversation_id or get_active_conversation_id()
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO conversations (role, content, timestamp, user_id, conversation_id) VALUES (?, ?, ?, ?, ?)",
            (role, content, datetime.now().isoformat(), active_user_id, active_conversation_id)
        )
    if active_conversation_id:
        maybe_update_conversation_title(active_conversation_id, role, content)
        touch_conversation(active_conversation_id)

def get_conversation_history(limit=10, user_id=None, conversation_id=None):
    active_user_id = user_id or get_current_user_id()
    active_conversation_id = conversation_id or get_active_conversation_id()
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT role, content
            FROM conversations
            WHERE user_id = ? AND conversation_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (active_user_id, active_conversation_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def sanitize_reply(text):
    """Remove labeled sections and markdown so replies stay plain."""
    if not text:
        return text
    text = text.replace("**", "").replace("*", "")
    text = re.sub(r"(?i)\\bI\\s+classify\\b", "This appears", text)
    text = re.sub(r"(?i)\\bI\\s+would\\s+classify\\b", "This appears", text)
    text = re.sub(r"(?i)\\bThis\\s+is\\s+a\\s+\\w+\\s+problem\\b", "This appears to be an issue", text)
    text = re.sub(
        r"(?im)^(classification|decision|action|acknowledgement|next step|next steps|"
        r"role clarification|identity verification|verification|authentication|"
        r"authorization|interaction mode|functionality|flags|state|directive|tag)\\s*:\\s*",
        "",
        text,
    )
    text = re.sub(r"(?m)^[\\-•]\\s+", "", text)
    return text.strip()

def split_sentences(text):
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]

def get_last_user_message():
    user_id = get_current_user_id()
    conversation_id = get_active_conversation_id()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT content FROM conversations WHERE role = 'user' AND user_id = ? AND conversation_id = ? ORDER BY id DESC LIMIT 1 OFFSET 1",
            (user_id, conversation_id),
        ).fetchone()
    return row["content"] if row else None

def get_last_assistant_message():
    user_id = get_current_user_id()
    conversation_id = get_active_conversation_id()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT content FROM conversations WHERE role = 'assistant' AND user_id = ? AND conversation_id = ? ORDER BY id DESC LIMIT 1",
            (user_id, conversation_id),
        ).fetchone()
    return row["content"] if row else None

def detect_contact_info(text):
    if not text:
        return False
    email = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}", text)
    phone = re.search(r"(\\+?\\d[\\d\\s().-]{7,}\\d)", text)
    return bool(email or phone)

def get_handoff_lock(tag):
    if not tag:
        return False
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT locked FROM handoff_locks WHERE tag = ?",
            (tag,),
        ).fetchone()
    return bool(row["locked"]) if row else False

def set_handoff_lock(tag, locked=True):
    if not tag:
        return
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id FROM handoff_locks WHERE tag = ?",
            (tag,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE handoff_locks SET locked = ?, last_updated = ? WHERE id = ?",
                (1 if locked else 0, datetime.now().isoformat(), row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO handoff_locks (tag, locked, last_updated) VALUES (?, ?, ?)",
                (tag, 1 if locked else 0, datetime.now().isoformat()),
            )

def max_turns_reached(tag, threshold=12):
    if not tag:
        return False
    count_24h = get_issue_count_window(tag, hours=24)
    return count_24h >= threshold

def log_runtime_event(issue_tag, issue_state, convo_state, directive, handoff_triggered):
    print(
        f"[runtime] tag={issue_tag} state={issue_state} convo={convo_state} "
        f"directive={directive} handoff={handoff_triggered}"
    )

def get_previous_issue_tag():
    prev = get_last_user_message()
    return detect_issue_tag(prev) if prev else None

def get_recent_user_messages(limit=3):
    user_id = get_current_user_id()
    conversation_id = get_active_conversation_id()
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT content FROM conversations WHERE role = 'user' AND user_id = ? AND conversation_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, conversation_id, limit),
        ).fetchall()
    return [r["content"] for r in rows]

def normalize_text(text):
    return re.sub(r"[^a-z0-9\\s]", "", text.lower()).strip()

def jaccard_similarity(a, b):
    a_set = set(normalize_text(a).split())
    b_set = set(normalize_text(b).split())
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / len(a_set | b_set)

def has_new_info(current, previous):
    if not previous:
        return True
    if re.search(r"\\d", current):
        return True
    cur_set = set(normalize_text(current).split())
    prev_set = set(normalize_text(previous).split())
    return len(cur_set - prev_set) >= 2

def is_resolution_message(text):
    t = text.lower()
    markers = ["resolved", "works now", "fixed", "issue is gone", "problem solved"]
    return any(m in t for m in markers)

def is_vague_reply(text):
    t = text.lower().strip()
    vague = ["still", "same", "bad", "not good", "no", "idk", "dont know", "nothing", "whatever"]
    return t in vague or len(t) <= 3

def get_conversation_state(user_message, issue_tag, issue_count, issue_state):
    if issue_tag and issue_tag.startswith("boundary:"):
        return "ABUSIVE"
    if is_resolution_message(user_message) and not is_complaint(user_message):
        if issue_state in ("ACTION_REQUIRED", "CRITICAL") or issue_state is None:
            return "RESOLUTION_CHECK"
        return "RESOLVED"

    prev_tag = get_previous_issue_tag()
    topic_changed = prev_tag and issue_tag and prev_tag != issue_tag
    if topic_changed:
        return "TOPIC_SHIFT"

    recent = get_recent_user_messages(limit=2)
    prev = recent[0] if recent else None
    repeat_score = jaccard_similarity(user_message, prev) if prev else 0.0
    new_info = has_new_info(user_message, prev)

    if repeat_score >= 0.85 or (issue_count and issue_count >= 3 and repeat_score >= 0.7):
        return "REPEATING"
    if not new_info and (is_vague_reply(user_message) or repeat_score >= 0.6):
        return "STALLED"
    return "PROGRESSING"

def conversation_directive(state):
    mapping = {
        "PROGRESSING": "ALLOW_CONTINUE",
        "STALLED": "FORCE_CLARIFY",
        "REPEATING": "LIMIT_RESPONSES",
        "ABUSIVE": "LIMIT_RESPONSES",
        "RESOLVED": "CLOSE_THREAD",
        "TOPIC_SHIFT": "SOFT_CLARIFY",
        "RESOLUTION_CHECK": "CONFIRM_RESOLUTION",
    }
    return mapping.get(state, "ALLOW_CONTINUE")

def handoff_directive(issue_tag, issue_state, issue_count, convo_state):
    if convo_state == "RESOLVED":
        return "NO_HANDOFF"
    if issue_state == "CRITICAL":
        return "HANDOFF_REQUIRED"
    if max_turns_reached(issue_tag, threshold=12):
        return "HANDOFF_REQUIRED"
    if issue_tag and (issue_tag.startswith("safety:") or issue_tag.startswith("boundary:")):
        if issue_count and issue_count >= 2:
            return "HANDOFF_REQUIRED"
    return "NO_HANDOFF"

def get_clarifying_question(tag):
    if not tag:
        return "What specific problem are you seeing right now?"
    if tag.startswith("usability:power_on"):
        return "What happens when you try to power it on?"
    if tag.startswith("performance:cooling"):
        return "When does it overheat and how long into use?"
    if tag.startswith("performance:speed"):
        return "What action feels slow or laggy?"
    if tag.startswith("reliability:battery"):
        return "How long does the battery last before it drops?"
    if tag.startswith("cost:pricing"):
        return "Which price point or plan is the issue?"
    return "What is the most concrete detail you can share about the issue?"

def enforce_response(draft, flags, user_message, issue_tag, issue_count, convo_directive=None, handoff=None):
    """Step 6: Enforce behavior flags on the draft reply."""
    if not draft:
        return draft

    sentences = split_sentences(draft)

    # REFUSE_IF_REPEATED
    if flags and flags.get("refuse_if_repeated"):
        last_user = get_last_user_message()
        if last_user and last_user.strip().lower() == user_message.strip().lower():
            return "I need one new detail to proceed. " + get_clarifying_question(issue_tag)

    # NO_SPECULATION
    if flags and flags.get("disable_speculation"):
        speculation_pattern = re.compile(
            r"\\b(will|going to|next week|next month|tomorrow|soon|"
            r"in \\d+ (day|days|week|weeks|month|months)|"
            r"schedule|timeline|release|deploy|fix)\\b",
            re.IGNORECASE,
        )
        sentences = [s for s in sentences if not speculation_pattern.search(s)]

    # VERIFY_BEFORE_ASSERT
    if flags and flags.get("require_verifiable_only"):
        verified = []
        for s in sentences:
            if "?" in s:
                verified.append(s)
                continue
            if re.search(r"\\b(you|your)\\b", s, re.IGNORECASE):
                verified.append(s)
        sentences = verified

    # LIMIT_SCOPE
    if flags and flags.get("limit_scope"):
        sentences = sentences[:2]

    # SIMPLIFY_OUTPUT
    if flags and flags.get("simplify_output"):
        simplified = []
        for s in sentences:
            if len(s) <= 140:
                simplified.append(s)
        sentences = simplified if simplified else sentences[:1]

    # Step 7: Memory-weighted response (provable from DB)
    memory_line = get_memory_statement(issue_tag, issue_count)
    if should_emit_memory_line(memory_line):
        sentences = [memory_line] + sentences

    final_text = " ".join(sentences).strip()

    # Step 8: Conversation progress control
    if convo_directive == "FORCE_CLARIFY":
        final_text = get_clarifying_question(issue_tag)
    elif convo_directive == "SOFT_CLARIFY":
        final_text = "I can switch topics. Please confirm the new issue and share one specific detail."
    elif convo_directive == "CONFIRM_RESOLUTION":
        final_text = "If this is resolved, please confirm. If not, share one specific remaining symptom."
    elif convo_directive == "LIMIT_RESPONSES":
        final_text = "I need one new detail to move forward. " + get_clarifying_question(issue_tag)
    elif convo_directive == "CLOSE_THREAD":
        final_text = "Understood. If this comes up again, share the exact symptom and I will pick it up."

    # Step 9: Exit / Handoff Logic (override)
    if handoff == "HANDOFF_REQUIRED":
        final_text = "This requires human review. Please provide a contact method and a concise summary of the issue."

    if not final_text:
        final_text = get_clarifying_question(issue_tag)
    return final_text

# === ROUTES ===
@app.route("/login", methods=["GET"])
def login_page():
    if get_current_user_id():
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/auth/signup", methods=["POST"])
def auth_signup():
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email"))
    password = data.get("password", "")
    user_id, err = create_user(email, password)
    if err:
        return jsonify({"error": err}), 400

    recovery_codes = generate_and_store_recovery_codes(user_id, count=8)
    token, expires_at = issue_session_token(user_id)
    return jsonify(
        {
            "ok": True,
            "email": email,
            "token": token,
            "token_expires_at": expires_at,
            "recovery_codes": recovery_codes,
        }
    ), 201

@app.route("/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email"))
    password = data.get("password", "")
    user_id = verify_login(email, password)
    if not user_id:
        return jsonify({"error": "Invalid email or password."}), 401

    token, expires_at = issue_session_token(user_id)
    return jsonify(
        {
            "ok": True,
            "email": email,
            "token": token,
            "token_expires_at": expires_at,
            "remaining_recovery_codes": get_remaining_recovery_codes(user_id),
        }
    )

@app.route("/auth/reset-password", methods=["POST"])
def auth_reset_password():
    data = request.get_json(silent=True) or {}
    email = data.get("email", "")
    recovery_code = data.get("recovery_code", "")
    new_password = data.get("new_password", "")

    ok, message = consume_recovery_code_and_reset_password(email, recovery_code, new_password)
    if not ok:
        return jsonify({"error": message}), 400
    return jsonify({"ok": True, "message": message})

@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    revoke_current_session()
    return jsonify({"ok": True})

@app.route("/auth/me", methods=["GET"])
def auth_me():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"authenticated": False}), 401

    active_conversation_id = resolve_conversation_id(user_id, create_if_missing=True)
    with get_db_connection() as conn:
        user = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    limit = get_upload_limit_for_user(user_id)
    used = get_upload_count_for_user(user_id)
    return jsonify(
        {
            "authenticated": True,
            "user_id": user_id,
            "email": user["email"] if user else None,
            "remaining_recovery_codes": get_remaining_recovery_codes(user_id),
            "active_conversation_id": active_conversation_id,
            "subscription_status": get_subscription_status(user_id),
            "upload_limit": limit,
            "uploads_used": used,
            "uploads_remaining": None if limit is None else max(limit - used, 0),
        }
    )

@app.route("/conversations", methods=["GET"])
@login_required
def conversations_list():
    user_id = get_current_user_id()
    resolve_conversation_id(user_id, create_if_missing=False)
    return jsonify(list_conversation_threads(user_id))

@app.route("/conversations", methods=["POST"])
@login_required
def conversations_create():
    user_id = get_current_user_id()
    data = request.get_json(silent=True) or {}
    title = data.get("title", "New Chat")
    conversation_id = create_conversation_thread(user_id, title=title)
    session["active_conversation_id"] = conversation_id
    return jsonify({"id": conversation_id, "title": title}), 201

@app.route("/conversations/<int:conversation_id>", methods=["PATCH"])
@login_required
def conversations_rename(conversation_id):
    user_id = get_current_user_id()
    data = request.get_json(silent=True) or {}
    ok, result = rename_conversation_thread(user_id, conversation_id, data.get("title", ""))
    if not ok:
        status = 400 if result == "Title is required." else 404
        return jsonify({"error": result}), status
    return jsonify({"id": conversation_id, "title": result})

@app.route("/conversations/<int:conversation_id>", methods=["DELETE"])
@login_required
def conversations_delete(conversation_id):
    user_id = get_current_user_id()
    deleted = delete_conversation_thread(user_id, conversation_id)
    if not deleted:
        return jsonify({"error": "Conversation not found."}), 404
    active_id = resolve_conversation_id(user_id, create_if_missing=False)
    return jsonify({"ok": True, "active_conversation_id": active_id})

@app.route("/upload", methods=["POST"])
@login_required
def upload_file():
    user_id = get_current_user_id()
    requested_conversation_id = request.form.get("conversation_id", type=int)
    conversation_id = resolve_conversation_id(user_id, requested_conversation_id, create_if_missing=True)

    limit = get_upload_limit_for_user(user_id)
    used = get_upload_count_for_user(user_id)
    if limit is not None and used >= limit:
        return jsonify(
            {
                "error": "Free plan limit reached (2 uploads). Upgrade to upload more files.",
                "code": "UPLOAD_LIMIT_REACHED",
                "upload_limit": limit,
                "uploads_used": used,
                "uploads_remaining": 0,
                "required_plan": "paid",
            }
        ), 402

    incoming = request.files.get("file")
    if not incoming or not incoming.filename:
        return jsonify({"error": "No file provided."}), 400
    if not is_allowed_upload(incoming.filename):
        return jsonify({"error": "File type not allowed."}), 400

    stored_name = build_storage_name(incoming.filename)
    stored_path = os.path.join(UPLOAD_DIR, stored_name)
    incoming.save(stored_path)

    mime_type = incoming.mimetype or mimetypes.guess_type(incoming.filename)[0] or "application/octet-stream"
    file_size = os.path.getsize(stored_path)
    extracted_text = extract_text_from_file(stored_path, mime_type)
    attachment_id = save_attachment_record(
        user_id=user_id,
        conversation_id=conversation_id,
        original_name=incoming.filename,
        stored_name=stored_name,
        mime_type=mime_type,
        file_size=file_size,
        extracted_text=extracted_text,
    )
    return jsonify(
        {
            "id": attachment_id,
            "conversation_id": conversation_id,
            "original_name": incoming.filename,
            "mime_type": mime_type,
            "file_size": file_size,
            "has_text": bool(extracted_text),
            "upload_limit": limit,
            "uploads_used": used + 1,
            "uploads_remaining": None if limit is None else max(limit - (used + 1), 0),
        }
    ), 201

@app.route("/attachments/<int:attachment_id>", methods=["GET"])
@login_required
def attachment_download(attachment_id):
    user_id = get_current_user_id()
    attachment = get_attachment(user_id, attachment_id)
    if not attachment:
        return jsonify({"error": "Attachment not found."}), 404
    path = os.path.join(UPLOAD_DIR, attachment["stored_name"])
    if not os.path.exists(path):
        return jsonify({"error": "Attachment file missing."}), 404
    return send_file(path, mimetype=attachment.get("mime_type") or "application/octet-stream")

@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/chat", methods=["POST"])
@login_required
def chat():
    data = request.get_json() or {}
    user_id = get_current_user_id()
    requested_conversation_id = data.get("conversation_id")
    conversation_id = resolve_conversation_id(user_id, requested_conversation_id, create_if_missing=True)
    attachment_ids = data.get("attachment_ids") or []
    url_inputs = data.get("url_inputs") or []
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"reply": "⚠️ Please type something."})

    attachments = get_attachments_for_chat(user_id, conversation_id, attachment_ids)
    attachment_context = build_attachment_context(attachments)
    url_context = build_url_context(url_inputs)
    final_user_message = user_message
    if attachment_context or url_context:
        extra_blocks = []
        if attachment_context:
            extra_blocks.append("Additional uploaded context (files/images):\n" + attachment_context)
        if url_context:
            extra_blocks.append("Additional URL context:\n" + url_context)
        final_user_message = (
            f"{user_message}\n\n"
            + "\n\n".join(extra_blocks)
        )

    # 1. Sentiment & Decision Analysis
    sentiment, emotion, confidence = analyze_sentiment(user_message)
    business_role = resolve_business_role(sentiment, confidence)
    decision = decision_directive(sentiment, confidence)

    # 1b. Issue Memory (Step 4)
    issue_tag = None
    issue_count = None
    issue_level = None
    issue_state = None
    behavior_flags = None
    convo_state = None
    convo_directive = None
    handoff_state = None
    if (sentiment == "negative" and confidence >= 50) or is_complaint(user_message):
        issue_tag = detect_issue_tag(user_message)
        issue_count = upsert_issue(issue_tag)
        issue_level = escalation_level(issue_count)

        # Step 5: Resolution Engine
        issue_state = analyze_issue_state(issue_tag, issue_count, issue_level)
        behavior_flags = resolution_policies(issue_state, issue_tag)
        upsert_resolution_flags(issue_tag, behavior_flags)

    # Step 8: Conversation Progress Control
    convo_state = get_conversation_state(user_message, issue_tag, issue_count, issue_state)
    convo_directive = conversation_directive(convo_state)
    handoff_state = handoff_directive(issue_tag, issue_state, issue_count, convo_state)

    active_tag = issue_tag or get_previous_issue_tag()
    if ENABLE_HANDOFF and active_tag and get_handoff_lock(active_tag):
        # Require contact info to unlock
        bot_reply = "This requires human review. Please provide a contact method and a concise summary of the issue."
        if detect_contact_info(user_message):
            set_handoff_lock(active_tag, False)
            bot_reply = "Thanks, contact received. A human will follow up."
        save_message("user", final_user_message, user_id=user_id, conversation_id=conversation_id)
        save_message("assistant", bot_reply, user_id=user_id, conversation_id=conversation_id)
        return jsonify({"reply": bot_reply, "conversation_id": conversation_id})

    if ENABLE_HANDOFF and handoff_state == "HANDOFF_REQUIRED" and issue_tag:
        set_handoff_lock(issue_tag, True)

    log_runtime_event(issue_tag, issue_state, convo_state, convo_directive, handoff_state == "HANDOFF_REQUIRED")
    
    # 2. Persist User Message
    save_message("user", final_user_message, user_id=user_id, conversation_id=conversation_id)

    # 3. Construct Authoritative System Instruction
    system_instruction = f"""
You are XenoBiz AI, a Business Manager AI acting as a {business_role.replace('_', ' ').title()}.

Your role is to manage product feedback, customer input, and operational concerns
with clarity, authority, and forward movement.

User context:
- Sentiment: {sentiment}
- Emotion: {emotion}
- Confidence: {confidence}%

DIRECTIVE:
{decision}

Role Focus: {ROLE_PROMPTS.get(business_role)}

Operating rules:
1. Acknowledge feedback once, briefly.
2. Classify the issue (usability, safety, performance, cost, experience, etc.).
3. State a clear business decision or next action.
4. Close the loop or schedule a follow-up.

Reality constraints (must follow):
- Do NOT claim actions are already done unless the user explicitly confirmed them.
- Do NOT invent timelines, meetings, data collection, or technical implementations.
- If more info is needed, ask one concise, specific question.
- Keep responses short (2–6 sentences) unless asked for details.
- Only introduce technical detail if the user asks for it.
- Do NOT use labeled sections like "Next Step:", "Role Clarification:", "Acknowledgement:", or bullet lists unless the user asks.
- No markdown formatting (no bold/italics, no headings). Use plain sentences only.
- Do NOT list identity/verification/authorization/interaction mode or capabilities unless the user explicitly asks for details.
- If the user asks "who are you" or similar, answer in one short sentence and move to the user's issue.

Behavior constraints:
- Decide internally before responding.
- Do NOT repeat questions already asked.
- Do NOT loop or stall the conversation.
- Do NOT apologize excessively.
- Do NOT offer refunds unless explicitly appropriate.
- Each response must move the situation forward.
- If escalation is noted/priority/escalated, adjust tone:
  - first: calm acknowledgment + single next step
  - noted (2): firm commitment to address
  - priority (3-4): ownership + concrete next action
  - escalated (5+): ownership + next action + ETA only if real; otherwise promise ETA after review

Tone:
- Calm, professional, decisive.
- No emotional mirroring.
- No customer-support scripts.

You speak as a responsible business operator, not a chatbot.
"""

    # 4. Context Assembly
    messages = [{"role": "system", "content": system_instruction}]
    
    # Add historical context from DB
    messages.extend(get_conversation_history(limit=10, user_id=user_id, conversation_id=conversation_id))
    
    # Ensure current message is present
    if not messages or messages[-1]["content"] != final_user_message:
        messages.append({"role": "user", "content": final_user_message})

    # 5. Inference
    try:
        completion = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.85
        )
        bot_reply = completion.choices[0].message.content
    except APIConnectionError:
        bot_reply = "Service is temporarily unavailable. Please try again."
    except Exception:
        bot_reply = "Service is temporarily unavailable. Please try again."

    # 6. Enforcement Layer
    flags_to_use = behavior_flags
    bot_reply = enforce_response(bot_reply, flags_to_use, user_message, issue_tag, issue_count, convo_directive, handoff_state)

    # 7. Sanitize Response
    bot_reply = sanitize_reply(bot_reply)

    # 8. Persist Response
    save_message("assistant", bot_reply, user_id=user_id, conversation_id=conversation_id)
    return jsonify({"reply": bot_reply, "conversation_id": conversation_id})

@app.route("/history", methods=["GET"])
@login_required
def history():
    user_id = get_current_user_id()
    requested_conversation_id = request.args.get("conversation_id", type=int)
    conversation_id = resolve_conversation_id(user_id, requested_conversation_id, create_if_missing=True)
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, role, content, timestamp, conversation_id
            FROM conversations
            WHERE user_id = ? AND conversation_id = ?
            ORDER BY id ASC
            """,
            (user_id, conversation_id),
        ).fetchall()
    return jsonify([dict(r) for r in rows])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    if DEBUG:
        webbrowser.open(f"http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=DEBUG)


        response = client.chat.completions.create(...)
        return response
    except Exception as e:
        print(f"Error occurred: {e}")  # Log the actual error
        raise  # Re-raise the exception to avoid hiding the actual error
