import telebot
import os
import re
import json
import sqlite3
import time
import hmac
from flask import Flask, request, jsonify
from threading import Thread, Lock
from groq import Groq

# Tokens
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")       # Bot 1: Customer Bot
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")     # Bot 2: Admin Bot

customer_bot = telebot.TeleBot(TELEGRAM_TOKEN)
admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)

# ==========================================
# ADMIN AUTHENTICATION
# ==========================================
_admin_ids_raw = os.environ.get("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = {
    int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip().isdigit()
}
if not ADMIN_USER_IDS:
    print("[WARN] ADMIN_USER_IDS is not set - the admin bot will reject everyone until it is.")

def is_authorized_admin(message):
    u = message.from_user
    return bool(u) and u.id in ADMIN_USER_IDS

# ==========================================
# GROQ MULTI-KEY ROTATION / FALLBACK
# ==========================================
_raw_keys = [
    os.environ.get("GROQ_API_KEY_1"),
    os.environ.get("GROQ_API_KEY_2"),
    os.environ.get("GROQ_API_KEY_3"),
    os.environ.get("GROQ_API_KEY_4"),
    os.environ.get("GROQ_API_KEY_5"),
    os.environ.get("GROQ_API_KEY_6"),
    os.environ.get("GROQ_API_KEY_7"),
    os.environ.get("GROQ_API_KEY"),
]
GROQ_API_KEYS = [k for k in _raw_keys if k]

if not GROQ_API_KEYS:
    raise RuntimeError(
        "No Groq API keys found. Set GROQ_API_KEY_1..GROQ_API_KEY_4 (and/or GROQ_API_KEY) in your environment."
    )

groq_clients = [Groq(api_key=key) for key in GROQ_API_KEYS]

_key_index_lock = Lock()
_current_key_index = 0

def _next_key_index():
    global _current_key_index
    with _key_index_lock:
        idx = _current_key_index
        _current_key_index = (_current_key_index + 1) % len(groq_clients)
    return idx

def create_completion(messages, **kwargs):
    start = _next_key_index()
    last_error = None

    for offset in range(len(groq_clients)):
        idx = (start + offset) % len(groq_clients)
        client = groq_clients[idx]
        try:
            return client.chat.completions.create(messages=messages, **kwargs)
        except Exception as e:
            last_error = e
            print(f"[Groq] Key #{idx + 1} failed ({e}); trying next key...")
            continue

    raise last_error

# ==========================================
# STORAGE
# ==========================================
user_memory = {}
user_last_message_id = {}   
customer_chat_id = {}       
customer_display = {}       
customer_last_code = {}     
admin_chat_id = None

# ==========================================
# PERSISTENCE 
# ==========================================
DATABASE_URL = os.environ.get("DATABASE_URL")
_db_lock = Lock()

if DATABASE_URL:
    import psycopg

    def _init_db():
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()

    def _write_state(payload):
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES ('state', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (payload,),
            )
            conn.commit()

    def _read_state():
        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = 'state'").fetchone()
            return row[0] if row else None

    print("[DB] Using Postgres (DATABASE_URL is set).")
else:
    DB_PATH = os.environ.get("BOT_DB_PATH", "splash_bot.db")
    _sqlite = sqlite3.connect(DB_PATH, check_same_thread=False)

    def _init_db():
        _sqlite.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        _sqlite.commit()

    def _write_state(payload):
        _sqlite.execute(
            "INSERT INTO kv (key, value) VALUES ('state', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (payload,),
        )
        _sqlite.commit()

    def _read_state():
        row = _sqlite.execute("SELECT value FROM kv WHERE key = 'state'").fetchone()
        return row[0] if row else None

    print(f"[DB] Using local SQLite at {DB_PATH}.")

_init_db()

def save_state():
    global admin_chat_id
    snapshot = {
        "user_memory": user_memory,
        "pending_approvals": pending_approvals,
        "user_last_message_id": user_last_message_id,
        "customer_chat_id": customer_chat_id,
        "customer_display": customer_display,
        "customer_last_code": customer_last_code,
        "voucher_inventory": voucher_inventory,
        "used_payment_refs": sorted(used_payment_refs),
        "admin_chat_id": admin_chat_id,
        "intergram_tag_to_key": intergram_tag_to_key,
        "registered_proofs": registered_proofs,
    }
    try:
        with _db_lock:
            _write_state(json.dumps(snapshot))
    except Exception as e:
        print(f"[DB] Failed to save state: {e}")

def load_state():
    global admin_chat_id
    try:
        with _db_lock:
            raw = _read_state()
        if not raw:
            print("[DB] No saved state found - starting fresh.")
            return
        snapshot = json.loads(raw)
        user_memory.update(snapshot.get("user_memory", {}))
        pending_approvals.update(snapshot.get("pending_approvals", {}))
        user_last_message_id.update(snapshot.get("user_last_message_id", {}))
        customer_chat_id.update(snapshot.get("customer_chat_id", {}))
        customer_display.update(snapshot.get("customer_display", {}))
        customer_last_code.update(snapshot.get("customer_last_code", {}))
        used_payment_refs.update(snapshot.get("used_payment_refs", []))
        for pkg, codes in snapshot.get("voucher_inventory", {}).items():
            voucher_inventory[pkg] = codes
        intergram_tag_to_key.update(snapshot.get("intergram_tag_to_key", {}))
        registered_proofs.update(snapshot.get("registered_proofs", {}))
        admin_chat_id = snapshot.get("admin_chat_id")
    except Exception as e:
        print(f"[DB] Failed to load state: {e}")

def make_customer_key(message):
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else chat_id
    return f"{chat_id}:{user_id}"

def display_name_for(message):
    u = message.from_user
    if not u:
        return str(message.chat.id)
    name = (u.first_name or "") + (f" {u.last_name}" if u.last_name else "")
    name = name.strip() or (f"@{u.username}" if u.username else str(u.id))
    if u.username:
        return f"{name} (@{u.username})"
    return f"{name} (id {u.id})"

pending_approvals = {}
used_payment_refs = set()

# Maps Intergram's stable per-visitor tag (the short code Intergram prefixes onto
# every forwarded message, e.g. "y6ysyx: Hi") to whichever customer_key currently
# holds that visitor's record. Unlike chat_id, this tag does NOT change when the
# widget session resets, so it's the right thing to key returning-customer
# identity on.
intergram_tag_to_key = {}

admin_awaiting = {}
_admin_state_lock = Lock()

PACKAGES = {
    "$1:UNL:24HRS": {"price": "$1.00", "data": "UNLIMITED"},
    "2 DAYS": {"price": "$0.50", "data": "5GB"},
    "7 DAYS": {"price": "$1.00", "data": "12GB"},
    "$5 = Unlimited 14d": {"price": "$5.00", "data": "UNLIMITED"},
    "$10 = Unlimited 30d": {"price": "$10.00", "data": "UNLIMITED"},
}

voucher_inventory = {pkg: [] for pkg in PACKAGES}
_inventory_lock = Lock()

CLOSING_WARNING = (
    "Do not close this current chat, otherwise you might not receive your login code, "
    "token, or password because the chat ID changes."
)

system_rules = """
You are an automated customer care AI assistant for Splash Internet. You MUST follow these rules strictly:

1. LANGUAGE: Support ONLY Shona or English. Match the user's language.
2. PRICING & PACKAGES:
   - $1:UNL:24HRS = USD $1.00 = UNLIMITED
   - 2 DAYS = USD $0.50 = 5GB
   - 7 DAYS = USD $1.00 = 12GB
   - $5 = Unlimited 14d = USD $5.00 = UNLIMITED
   - $10 = Unlimited 30d = USD $10.00 = UNLIMITED
3. PAYMENTS & PROOF OF PAYMENT:
   - EcoCash number is 0776248396.
   - You need three things: (a) the customer's phone number, (b) the package, and (c) proof of payment.
   - DISTINGUISH REPLIES: If the user is just answering a question about which package they want (e.g. saying "7d", "7 days", "lite"), DO NOT treat it as a payment reference. Only evaluate transaction references when a full payment confirmation block is provided.
   - A VALID TRANSACTION REFERENCE MUST BE AT LEAST 7 CHARACTERS LONG. If a user provides a reference that is less than 7 characters as a payment code, reject it.
   - EXTRACTING THE TRANSACTION REFERENCE: Look for the longest alphanumeric string in the message. CODE_ENDING MUST be the EXACT last 7 characters of that full reference. Ignore punctuation like dots or dashes. NEVER accept or use a code shorter than 7 characters.
4. AFTER PAYMENT PROOF IS SUBMITTED:
   - Tell the user to wait 30 seconds while the payment is validated. That is ALL you say about the outcome.
   - CRITICAL: Do NOT attempt to repeat, quote, or summarize the customer's transaction reference back to them in your conversational reply. The reference must ONLY be output inside the [ADMIN_ALERT] tag.
5. ADMIN PAYMENT APPROVAL (CRITICAL INSTRUCTION):
   - When a user has provided their phone number, package, AND a valid payment reference (minimum 7 chars), generate the exact tag [ADMIN_ALERT] followed immediately by a structured line:
     [ADMIN_ALERT] CODE_ENDING: <exact last 7 characters> | PRICE: $<amount> | PHONE: <customer phone number> | PACKAGE: <package name> - Admin, please provide a voucher code.
   - NEVER generate this alert if the code is under 7 characters.
6. ADMIN REPLIES:
   - You will never need to interpret or forward admin replies yourself. The backend handles this.
7. MANDATORY CLOSING WARNING:
   - You MUST include this exact warning at the end of EVERY customer response: "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
8. SCOPE: Only answer about Splash Internet.
9. NO CODES, NO APPROVALS (ABSOLUTE, OVERRIDES EVERYTHING ELSE):
   - You do NOT have access to any voucher codes, login codes, tokens, usernames or passwords. You must NEVER write, invent, guess or repeat one, in any format.
   - You must NEVER say or imply that a payment "has been approved", "verified", "confirmed" or "successful". You cannot know that.
10. FRESH TRANSACTIONS:
   - If you see a SYSTEM NOTE telling you a new payment reference was submitted, treat it as a completely separate, brand-new transaction. Do NOT reuse a phone number or package mentioned earlier in this chat for that new reference. Ask the customer to (re)confirm both before producing any [ADMIN_ALERT].
11. NEVER FABRICATE A "PAYMENT RECEIVED" REPLY:
   - Only tell the customer their payment was received / to wait 30 seconds if THIS message actually contains a new, valid transaction reference. If the customer's latest message is something else (a package choice, "resend", a greeting, etc.) with no reference in it, do NOT repeat or imply an earlier payment confirmation from chat history - ask them for the actual proof of payment instead.
"""

WAIT_MESSAGE = "Thank you, we have received your payment proof. Please wait about 30 seconds while we validate the payment."
NEED_PROOF_MESSAGE = ("Please send your phone number, the package you want, and your proof of payment "
                      "(the EcoCash confirmation message or the exact transaction reference).")
DUPLICATE_MESSAGE = ("This payment proof has already been processed. If you did not receive your voucher, "
                     "please scroll up in this chat to find it, or contact support.")

PHONE_PATTERN = re.compile(r'\b(07\d{8}|\+?2637\d{8})\b')

def extract_phone_from_text(text):
    if not text:
        return None
    m = PHONE_PATTERN.search(text)
    return m.group(1) if m else None

FAKE_APPROVAL_RE = re.compile(
    r'(?:payment\s+(?:has\s+been|was|is|is\s+now)\s+(?:approved|verified|confirmed|successful))'
    r'|(?:(?:voucher|login|access|wifi|wi-fi)\s*(?:code|pin)\s*\**\s*[:\-\u2013\u2014=]\s*\**\s*[A-Za-z0-9])'
    r'|(?:\b(?:token|password|username|pin)\s*\**\s*[:=]\s*\**\s*[A-Za-z0-9])',
    re.IGNORECASE
)
SUSPICIOUS_TOKEN_RE = re.compile(r'\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,14}\b')

# Detects the model fabricating a "we received your payment, please wait" style reply
# purely from chat history, with no genuine new proof submitted this turn.
FAKE_RECEIPT_RE = re.compile(
    r'payment\s+of.{0,40}?(?:has\s+been|was)\s+received'
    r'|your\s+payment.{0,60}?received.{0,60}?wait'
    r'|please\s+wait.{0,40}?(?:30\s*seconds|validat\w*\s+the\s+payment)',
    re.IGNORECASE | re.DOTALL
)
NO_GENUINE_PROOF_MESSAGE = (
    "I don't see a new payment proof or transaction reference in your last message. "
    "If you already paid, please resend the full EcoCash confirmation or transaction reference "
    "so we can process it."
)

def strip_fake_approval(reply, known_text=""):
    if not reply:
        return "", False
    known = (known_text or "").lower()
    cut = None
    m = FAKE_APPROVAL_RE.search(reply)
    if m:
        cut = m.start()
    for tm in SUSPICIOUS_TOKEN_RE.finditer(reply):
        if tm.group(0).lower() not in known:
            cut = tm.start() if cut is None else min(cut, tm.start())
            break
    if cut is None:
        return reply, False
    line_start = reply.rfind('\n', 0, cut) + 1
    return reply[:line_start].rstrip(), True

def ensure_closing_warning(text):
    if "do not close this current chat" in text.lower():
        return text
    return f"{text}\n\n{CLOSING_WARNING}"

def payment_fingerprint(info):
    if not info:
        return None
    ending = re.sub(r'[^a-z0-9]', '', (info.get("code_ending") or "").lower())
    if not ending or len(ending) < 7:
        return None
    return ending[-7:]

def mark_payment_used(info):
    fp = payment_fingerprint(info)
    if fp:
        used_payment_refs.add(fp)
        # Once a payment is redeemed, drop any admin-registered proof for it too.
        with _proofs_lock:
            registered_proofs.pop(fp, None)

def note_voucher_delivered(history, package):
    history.append({
        "role": "system",
        "content": (
            f"SYSTEM NOTE: The backend successfully processed the payment for {package} and delivered "
            "the voucher to the customer. THAT TRANSACTION IS COMPLETELY CLOSED. "
            "Do NOT refer to the old payment or old reference code again. "
            "If the customer sends a new message (like a phone number), assume they want to buy a NEW voucher, "
            "and ask them for their NEW proof of payment and package."
        )
    })

TRANSACTION_REF_PATTERN = re.compile(
    r'(?:approval\s*code|transaction\s*id|trans(?:action)?\s*ref(?:erence)?|confirmation\s*code|ref(?:erence)?\s*(?:no\.?|number)?)\s*[:#]?\s*'
    r'([A-Za-z0-9][A-Za-z0-9.\-]{6,})',
    re.IGNORECASE
)

def extract_code_ending_from_text(text):
    if not text:
        return None
    m = TRANSACTION_REF_PATTERN.search(text)
    if not m:
        return None
    raw = re.sub(r'[^A-Za-z0-9]', '', m.group(1))
    if len(raw) >= 7:
        return raw[-7:]
    return None

def parse_admin_alert(alert_text, user_text="", authoritative_code=None):
    pattern = re.compile(
        r'CODE_ENDING:\s*(?P<code>[^|]+)\|\s*PRICE:\s*(?P<price>[^|]+)\|\s*PHONE:\s*(?P<phone>[^|]+)\|\s*PACKAGE:\s*(?P<package>[^\n]+)',
        re.IGNORECASE
    )
    m = pattern.search(alert_text)
    if not m:
        return None
        
    code_ending = m.group("code").strip(" -\u2014:")
    code_alphanum = re.sub(r'[^A-Za-z0-9]', '', code_ending)
    
    real_ending = extract_code_ending_from_text(user_text)

    # Preference order for the code ending:
    # 1. The code we ALREADY confirmed for this customer's open transaction (most trustworthy -
    #    this came from a genuine proof-of-payment message earlier, not from the AI's memory).
    # 2. A reference freshly extracted from the customer's own message this turn.
    # 3. Whatever the AI itself wrote (least trustworthy - it can garble or truncate this).
    clean_authoritative = re.sub(r'[^A-Za-z0-9]', '', (authoritative_code or ''))
    if len(clean_authoritative) >= 7:
        final_code = clean_authoritative[-7:]
    elif real_ending:
        final_code = real_ending
    elif len(code_alphanum) >= 7:
        final_code = code_alphanum[-7:]
    else:
        return None

    return {
        "code_ending": final_code,
        "price": m.group("price").strip(" -\u2014:"),
        "phone": m.group("phone").strip(" -\u2014:"),
        "package": re.split(r'[\u2014-]', m.group("package"))[0].strip(),
    }

def finalize_alert_slots(parsed, customer_key):
    """Cross-checks a parsed [ADMIN_ALERT] payload against the deterministic slot tracker
    (pending_approvals), which is the only source we trust for phone/package. Returns
    ('ready', parsed) once code+phone+package are all confirmed and admin can be alerted, or
    ('missing', message) if the customer still needs to supply phone and/or package."""
    if customer_key in pending_approvals:
        if not pending_approvals[customer_key].get("phone_confirmed"):
            pending_approvals[customer_key]["phone"] = None
        if not pending_approvals[customer_key].get("package_confirmed"):
            pending_approvals[customer_key]["package"] = None
            pending_approvals[customer_key]["package_key"] = None

    current_slot = pending_approvals.get(customer_key, parsed)
    if not current_slot.get("phone") or not current_slot.get("package"):
        missing = []
        if not current_slot.get("phone"):
            missing.append("phone number")
        if not current_slot.get("package"):
            missing.append("package")
        msg = (
            f"I see your payment code ending **{parsed['code_ending']}**. "
            f"To proceed, please also provide your **{' and '.join(missing)}**."
        )
        return "missing", msg

    parsed["phone"] = current_slot.get("phone")
    parsed["package"] = current_slot.get("package")
    customer_last_code[customer_key] = parsed["code_ending"]
    return "ready", parsed

def find_target_customer(admin_text):
    digits_in_text = re.findall(r'[A-Za-z0-9]{7,}', admin_text)
    for cid, info in pending_approvals.items():
        ending = (info.get("code_ending") or "").strip()
        if ending and len(ending) >= 7 and any(ending[-7:] in d or d in ending[-7:] for d in digits_in_text):
            return cid

    if len(pending_approvals) == 1:
        return next(iter(pending_approvals))

    return None

def normalize_package(text):
    if not text:
        return None
    t = re.sub(r'\s+', ' ', text.strip()).upper()
    if t in PACKAGES:
        return t
    candidates = [k for k in PACKAGES if t in k or k in t]
    return candidates[0] if len(candidates) == 1 else None

PACKAGE_ALIASES = {re.sub(r'\s+', '', k.upper()): k for k in PACKAGES}

def normalize_package_compact(text):
    if not text:
        return None
    compact = re.sub(r'\s+', '', text.strip().upper())
    if compact in PACKAGE_ALIASES:
        return PACKAGE_ALIASES[compact]
    return normalize_package(text)

# Informal / lenient package matching specifically for raw customer messages. Handles:
#  - a package name embedded with no whitespace inside a longer message, e.g. the message
#    starting with "7days: Transfer Confirmation: ..." (normalize_package's substring check
#    requires the space in "7 DAYS", which "7days" without a space never satisfies)
#  - informal shorthand like "7d", "24h", "2 days"
PACKAGE_DURATION_MAP = {
    ("24", "h"): "$1:UNL:24HRS",
    ("2", "d"): "2 DAYS",
    ("7", "d"): "7 DAYS",
    ("14", "d"): "$5 = Unlimited 14d",
    ("30", "d"): "$10 = Unlimited 30d",
}
DURATION_RE = re.compile(r'\b(\d{1,2})\s*-?\s*(days?|d|hours?|hrs?|h)\b', re.IGNORECASE)

def extract_package_from_duration(text):
    if not text:
        return None
    m = DURATION_RE.search(text)
    if not m:
        return None
    num = m.group(1)
    unit = "h" if m.group(2).lower().startswith("h") else "d"
    return PACKAGE_DURATION_MAP.get((num, unit))

# Price-based inference: if the EcoCash amount in the proof-of-payment text matches
# the price of exactly ONE package, we can safely fill the package slot ourselves
# instead of making the customer type it. If the amount is shared by more than one
# package (e.g. two packages both cost $1.00), it stays ambiguous and we still ask -
# guessing wrong would sell the customer the wrong voucher.
PRICE_TO_PACKAGES = {}
for _pkg, _info in PACKAGES.items():
    _amt = _info["price"].replace("$", "").strip()
    PRICE_TO_PACKAGES.setdefault(_amt, []).append(_pkg)

AMOUNT_RE = re.compile(r'(?:USD|\$)\s*(\d+(?:\.\d{1,2})?)', re.IGNORECASE)
BALANCE_CONTEXT_RE = re.compile(r'balance', re.IGNORECASE)

def extract_package_from_price(text):
    if not text:
        return None
    candidates = []
    for m in AMOUNT_RE.finditer(text):
        start, end = m.span()
        window = text[max(0, start - 20):min(len(text), end + 20)]
        if BALANCE_CONTEXT_RE.search(window):
            continue  # skip "New balance: USD X.XX" - that's not the payment amount
        candidates.append(m.group(1))
    if not candidates:
        return None
    amount = candidates[0]  # the transaction amount is virtually always the first non-balance figure
    try:
        normalized = f"{float(amount):.2f}"
    except ValueError:
        return None
    matched = PRICE_TO_PACKAGES.get(normalized, [])
    return matched[0] if len(matched) == 1 else None

def extract_customer_package(text):
    if not text:
        return None
    pkg = normalize_package(text)
    if pkg:
        return pkg
    compact = re.sub(r'\s+', '', text.strip().upper())
    compact_matches = {p for alias, p in PACKAGE_ALIASES.items() if alias in compact}
    if len(compact_matches) == 1:
        return next(iter(compact_matches))
    pkg = extract_package_from_duration(text)
    if pkg:
        return pkg
    return extract_package_from_price(text)

def reserve_voucher(package_key):
    if not package_key:
        return None
    with _inventory_lock:
        codes = voucher_inventory.get(package_key)
        if codes:
            return codes.pop(0)
    return None

def return_voucher(package_key, code):
    if not package_key or code is None:
        return
    with _inventory_lock:
        voucher_inventory.setdefault(package_key, []).insert(0, code)

def delete_voucher(code, package_key=None):
    with _inventory_lock:
        search_keys = [package_key] if package_key else list(voucher_inventory.keys())
        for pkg in search_keys:
            codes = voucher_inventory.get(pkg, [])
            for i, c in enumerate(codes):
                if c.lower() == code.lower():
                    codes.pop(i)
                    return pkg
    return None

ADD_CODE_PATTERN = re.compile(r'^\s*add\s+code\s+(?P<code>\S+)\s+for\s+(?P<package>.+?)\s*$', re.IGNORECASE)
DELETE_CODE_PATTERN = re.compile(r'^\s*delete\s+code\s+(?P<code>\S+)(?:\s+from\s+(?P<package>.+?))?\s*$', re.IGNORECASE)
ADD_CODE_SHORT_PATTERN = re.compile(r'^\s*(?P<code>\S+)\s+for\s+(?P<package>.+?)\s*$', re.IGNORECASE)
DELETE_CODE_SHORT_PATTERN = re.compile(r'^\s*(?P<code>\S+)(?:\s+from\s+(?P<package>.+?))?\s*$', re.IGNORECASE)
BULK_ADD_ENTRY_PATTERN = re.compile(r'CODE:\s*(?P<code>\S+)[ \t]*\r?\n[ \t]*(?P<pkgline>[^\r\n]+)', re.IGNORECASE)

def parse_bulk_add_entries(text):
    entries = []
    for m in BULK_ADD_ENTRY_PATTERN.finditer(text):
        code = m.group("code").strip()
        pkgline = m.group("pkgline").strip()
        fields = [f.strip() for f in pkgline.split(':') if f.strip()]
        raw_package = fields[-1] if fields else pkgline
        pkg_key = normalize_package_compact(raw_package)
        entries.append((code, pkg_key, raw_package))
    return entries

YES_WORDS = {"yes", "y", "approve", "approved", "ok", "okay", "confirm", "confirmed"}
NO_WORDS = {"no", "n", "reject", "rejected", "cancel", "deny", "denied"}

SERVICE_MESSAGE_PATTERNS = re.compile(
    r'\b(has left|has joined|joined the group|left the group|was removed|removed from the group|'
    r'added to the group|pinned a message|changed the group|changed the chat photo)\b',
    re.IGNORECASE
)

def is_service_message(text):
    return bool(SERVICE_MESSAGE_PATTERNS.search(text or ""))

def cancel_pending_for_customer(customer_key, reason="left the chat"):
    info = pending_approvals.pop(customer_key, None)
    if not info:
        return False
    if info.get("proposed_code"):
        return_voucher(info.get("package_key"), info["proposed_code"])

    label = customer_display.get(customer_key, customer_key)
    if admin_chat_id:
        admin_bot.send_message(
            admin_chat_id,
            f"⚠️ {label} {reason} before their payment was approved. "
            "Their pending approval request has been canceled"
            + (" and the reserved voucher code was returned to stock." if info.get("proposed_code") else ".")
        )
    save_state()
    return True


# ==========================================================================
# PRE-REGISTERED PROOF OF PAYMENT  ->  AUTO-APPROVAL
#
# How it works:
#   1. You paste the real EcoCash confirmation into the ADMIN bot:
#         Transfer Confirmation: USD 1.00 from MICHAEL KUSUBA.
#         Approval Code: PP260919.2324.T3345746
#         New balance: USD 15.63.
#      The bot stores: amount (1.00), full reference, and its last 7 chars.
#   2. When a customer sends proof, the bot auto-approves ONLY if
#         - last 7 chars of the approval code match a registered proof, AND
#         - the amount they sent equals the registered amount, AND
#         - (if they sent the full reference) the full reference matches, AND
#         - the package's price equals the paid amount, AND
#         - a stocked voucher exists for that package.
#      Anything else falls through to the normal AI + admin YES/NO flow.
#   3. After the voucher is delivered, the registered proof is DELETED and
#      the reference is burned in used_payment_refs (replay protection).
# ==========================================================================

AUTO_APPROVE_ENABLED = os.environ.get("AUTO_APPROVE_ENABLED", "1") != "0"
# Set AUTO_APPROVE_REQUIRE_PHONE=1 if you also want the customer's phone number
# before auto-sending (by default price + reference + package are enough).
AUTO_APPROVE_REQUIRE_PHONE = os.environ.get("AUTO_APPROVE_REQUIRE_PHONE", "0") == "1"

# fingerprint (last 7 alphanumerics, lowercase) -> {"amount", "full_ref", "sender", "registered_at"}
registered_proofs = {}
_proofs_lock = Lock()

PROOF_MARKER_RE = re.compile(r'approval\s*code|transfer\s*confirmation', re.IGNORECASE)


def normalize_ref(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def proof_fingerprint(ref):
    cleaned = normalize_ref(ref)
    return cleaned[-7:] if len(cleaned) >= 7 else None


def extract_full_reference_from_text(text):
    """Full approval code with punctuation stripped, e.g. 'PP2609192324T3345746'."""
    if not text:
        return None
    m = TRANSACTION_REF_PATTERN.search(text)
    if not m:
        return None
    raw = re.sub(r'[^A-Za-z0-9]', '', m.group(1))
    return raw if len(raw) >= 7 else None


def extract_payment_amount(text):
    """The transferred amount as '1.00' - skips 'New balance: USD 15.63' style figures."""
    if not text:
        return None
    for m in AMOUNT_RE.finditer(text):
        start, end = m.span()
        window = text[max(0, start - 20):min(len(text), end + 20)]
        if BALANCE_CONTEXT_RE.search(window):
            continue
        try:
            return f"{float(m.group(1)):.2f}"
        except ValueError:
            return None
    return None


def parse_proof_blocks(text):
    """Parses one OR several pasted confirmations (split on 'Transfer Confirmation')."""
    proofs = []
    for block in re.split(r'(?=Transfer\s+Confirmation)', text, flags=re.IGNORECASE):
        block = block.strip()
        if not block:
            continue
        full_ref = extract_full_reference_from_text(block)
        amount = extract_payment_amount(block)
        if not full_ref or not amount:
            continue
        sender_m = re.search(r'\bfrom\s+([^\n.]+)', block, re.IGNORECASE)
        proofs.append({
            "amount": amount,
            "full_ref": full_ref,
            "sender": sender_m.group(1).strip() if sender_m else "",
            "registered_at": time.time(),
        })
    return proofs


def try_auto_approve(customer_key):
    """Returns True only if a voucher was delivered. Returns False (touching nothing)
    whenever anything doesn't line up, so the normal manual flow takes over."""
    if not AUTO_APPROVE_ENABLED:
        return False
    info = pending_approvals.get(customer_key)
    if not info:
        return False

    fp = proof_fingerprint(info.get("code_ending"))
    if not fp or fp in used_payment_refs:
        return False
    with _proofs_lock:
        proof = registered_proofs.get(fp)
    if not proof:
        return False  # admin never registered this payment -> manual flow

    # --- verification: price ---------------------------------------------
    if info.get("claimed_amount") != proof["amount"]:
        print(f"[AUTO] {customer_key}: amount mismatch (claimed {info.get('claimed_amount')} vs "
              f"registered {proof['amount']}) - manual flow.")
        return False

    # --- verification: full reference, when the customer supplied it -------
    claimed_ref = normalize_ref(info.get("claimed_ref"))
    reg_ref = normalize_ref(proof.get("full_ref"))
    if len(claimed_ref) > 7 and len(reg_ref) > 7 and claimed_ref != reg_ref:
        print(f"[AUTO] {customer_key}: full reference mismatch - manual flow.")
        return False

    if AUTO_APPROVE_REQUIRE_PHONE and not info.get("phone"):
        return False

    # --- verification: package must exist and cost exactly what was paid ---
    pkg_key = info.get("package_key")
    if not pkg_key:
        matches = PRICE_TO_PACKAGES.get(proof["amount"], [])
        pkg_key = matches[0] if len(matches) == 1 else None  # ambiguous -> ask customer
    if not pkg_key or pkg_key not in PACKAGES:
        return False
    try:
        pkg_price = f"{float(PACKAGES[pkg_key]['price'].replace('$', '')):.2f}"
    except ValueError:
        return False
    if pkg_price != proof["amount"]:
        print(f"[AUTO] {customer_key}: paid {proof['amount']} but chose {pkg_key} ({pkg_price}) - manual flow.")
        return False

    chat_id = customer_chat_id.get(customer_key)
    if chat_id is None:
        return False

    # --- get a voucher (reuse one already reserved for this customer) -----
    reserved_here = not info.get("proposed_code")
    code = info.get("proposed_code") or reserve_voucher(pkg_key)
    if not code:
        return False  # out of stock -> normal flow asks admin for a code

    # --- atomically claim the proof so two customers can't both redeem it -
    with _proofs_lock:
        claimed = registered_proofs.pop(fp, None)
    if not claimed:
        if reserved_here:
            return_voucher(pkg_key, code)
        return False

    voucher_message = (
        f"✅ Your payment has been approved!\n\n"
        f"Package: {pkg_key}\n"
        f"Voucher code: {code}\n\n"
        + CLOSING_WARNING
    )
    try:
        reply_id = user_last_message_id.get(customer_key)
        if reply_id:
            customer_bot.send_message(chat_id, voucher_message, reply_to_message_id=reply_id)
        else:
            customer_bot.send_message(chat_id, voucher_message)
    except Exception as e:
        print(f"[AUTO] Delivery failed for {customer_key}: {e} - rolling back.")
        with _proofs_lock:
            registered_proofs[fp] = claimed
        if reserved_here:
            return_voucher(pkg_key, code)
        return False

    # --- success: close the transaction, burn the reference, drop the proof
    history = user_memory.setdefault(customer_key, [{"role": "system", "content": system_rules}])
    note_voucher_delivered(history, pkg_key)
    mark_payment_used(info)                      # also removes the registered proof
    pending_approvals.pop(customer_key, None)

    label = customer_display.get(customer_key, str(customer_key))
    if admin_chat_id:
        try:
            admin_bot.send_message(
                admin_chat_id,
                f"🤖 AUTO-APPROVED\n"
                f"Customer: {label}\n"
                f"Ref ending: {fp}  |  Paid: ${proof['amount']}"
                + (f" (from {proof['sender']})" if proof.get("sender") else "") + "\n"
                f"Package: {pkg_key}\n"
                f"Voucher sent: {code}\n"
                f"The registered proof was removed from memory."
            )
        except Exception as e:
            print(f"[AUTO] Could not notify admin: {e}")
    save_state()
    return True


# ---- admin-bot side ---------------------------------------------------------

def register_proofs_from_admin(text):
    proofs = parse_proof_blocks(text)
    if not proofs:
        return ("⚠️ That looks like a payment confirmation, but I couldn't read both an amount "
                "(e.g. USD 1.00) and an 'Approval Code'. Please paste the full confirmation as received.")
    lines = []
    for p in proofs:
        fp = proof_fingerprint(p["full_ref"])
        if fp in used_payment_refs:
            lines.append(f"⚠️ Ref ending {fp} was already redeemed - NOT registered.")
            continue
        with _proofs_lock:
            replaced = fp in registered_proofs
            registered_proofs[fp] = p

        # A customer may already be waiting on this exact payment - serve them now.
        served = []
        for ckey, cinfo in list(pending_approvals.items()):
            if proof_fingerprint(cinfo.get("code_ending")) == fp and try_auto_approve(ckey):
                served.append(customer_display.get(ckey, str(ckey)))

        who = f" from {p['sender']}" if p["sender"] else ""
        line = f"✅ {'Updated' if replaced else 'Registered'}: ${p['amount']}{who}, ref ending {fp}."
        if served:
            line += f" A waiting customer ({', '.join(served)}) was auto-approved right away."
        else:
            line += " Will auto-approve when a customer sends a matching proof."
        lines.append(line)
    return "\n".join(lines)


def list_proofs_text():
    with _proofs_lock:
        items = list(registered_proofs.items())
    if not items:
        return "📭 No registered proofs waiting."
    lines = [f"- ref ending {fp}: ${p['amount']}" + (f" from {p['sender']}" if p.get("sender") else "")
             for fp, p in items]
    return "🧾 Registered proofs (auto-approve on match):\n" + "\n".join(lines) + \
           "\n\nRemove one with /delproof <last 7 chars of the approval code>"


def handle_proof_admin_message(text):
    """Returns a reply string if this admin message was proof-related, else None."""
    low = text.lower().strip()
    if low in ('/proofs', 'proofs'):
        return list_proofs_text()
    if low.startswith('/delproof'):
        fp = proof_fingerprint(text[len('/delproof'):].strip())
        if not fp:
            return "Usage: /delproof <last 7 (or all) characters of the approval code>"
        with _proofs_lock:
            removed = registered_proofs.pop(fp, None)
        return f"🗑️ Removed proof ending {fp}." if removed else f"⚠️ No registered proof ending {fp}."
    if PROOF_MARKER_RE.search(text):
        return register_proofs_from_admin(text)
    return None

# ==========================================
# RECONCILIATION: reconnect returning customers across Intergram chat-id changes
# ==========================================
#
# Intergram gives each browser/widget session a NEW Telegram chat_id, so
# make_customer_key() (chat_id:user_id) treats a returning customer as a total
# stranger even though their earlier payment proof is still sitting, unresolved,
# in pending_approvals under their OLD key.
#
# The reliable fix: Intergram prefixes every forwarded message with a short,
# STABLE per-visitor tag, e.g. "y6ysyx: Hi" - that tag does NOT change when the
# chat_id does, so it's the correct identity key, not chat_id and not phone
# number. Phone-number matching (and the single-pending fallback) is kept as a
# secondary safety net for the rare message that doesn't carry a tag.

REBUMP_COOLDOWN_SECONDS = 15  # just enough to absorb accidental double-sends/webhook retries,
                               # not to make an impatient customer wait for a re-alert

FOLLOWUP_STATUS_RE = re.compile(
    r'\b(where.?s?\s+my\s+code|any\s+update|did\s+you\s+(get|receive)|status|'
    r'still\s+waiting|already\s+paid|already\s+sent|didn.?t\s+get|'
    r'haven.?t\s+received|resend|re-?send|send\s+(it|the\s+code|my\s+code)|'
    r'waiting\s+for\s+(my\s+)?code|hello|hi|helo)\b',
    re.IGNORECASE
)

# ASSUMPTION based on the two examples you gave ("cdgrmv:", "y6ysyx:"): the tag
# is exactly 6 lowercase letters/digits. If your Intergram config uses a
# different length or includes uppercase, adjust {6} and the character class
# below to match - check a few real forwarded messages to confirm the format.
INTERGRAM_TAG_RE = re.compile(r'^([a-z0-9]{6}):\s?(.*)$', re.IGNORECASE | re.DOTALL)

def extract_intergram_tag(raw_text):
    """
    Splits an Intergram-forwarded message into (tag, message_without_tag).
    Returns (None, raw_text) if no tag is present (e.g. a message typed
    directly by an admin, or an unrelated format) so callers can fall back
    to the phone-number heuristics safely.
    """
    if not raw_text:
        return None, raw_text
    m = INTERGRAM_TAG_RE.match(raw_text.strip())
    if not m:
        return None, raw_text
    return m.group(1), m.group(2).strip()

def find_pending_by_phone(phone):
    if not phone:
        return None
    for key, info in pending_approvals.items():
        if info.get("phone") == phone:
            return key
    return None

def _repoint_customer_routing(message, key):
    """Re-point delivery coordinates (chat id, reply-to message id, display
    name) at the customer's CURRENT session, while keeping the same record
    key - so the existing pending_approvals entry (and its history with the
    admin) stays intact instead of us creating a second, duplicate request
    for the same payment."""
    customer_chat_id[key] = message.chat.id
    user_last_message_id[key] = message.message_id
    customer_display[key] = display_name_for(message)

    if key in user_memory:
        user_memory[key].append({
            "role": "system",
            "content": (
                "SYSTEM NOTE: The customer re-opened the chat (their widget session "
                "restarted and got a new chat id, which is normal for this platform). "
                "This is the SAME person and the SAME still-open transaction as before "
                "in this history - do not ask them to resubmit payment proof or treat "
                "this as a new purchase."
            )
        })
    print(f"[RECONCILE] Reattached returning customer -> existing record {key}")

def reconcile_returning_customer(message, chat_customer_key, raw_text):
    """
    Called BEFORE we touch customer_chat_id/user_memory for this message.
    Works out which customer_key should be treated as canonical for this
    message, and strips any Intergram tag off the text so downstream regexes
    (phone/reference/package extraction) see clean customer text.

    Priority, strongest signal first:
      1. Intergram's stable per-visitor tag - trusted on its own, even with
         zero other info in the message (fixes "hi" / "resend" / "still
         waiting" with nothing else in it).
      2. A phone number in the message matching an open pending approval.
      3. Exactly ONE open, already-alerted transaction system-wide and this
         message reads like a status follow-up (last-resort, single-customer
         only - never guessed when more than one customer is waiting, since a
         wrong guess would leak/misroute someone else's transaction).

    Returns (canonical_key, cleaned_text).
    """
    tag, cleaned_text = extract_intergram_tag(raw_text)

    if tag:
        known_key = intergram_tag_to_key.get(tag)
        if known_key and known_key != chat_customer_key and (
            known_key in pending_approvals or known_key in user_memory
        ):
            _repoint_customer_routing(message, known_key)
            save_state()
            return known_key, cleaned_text
        # First time we've seen this tag, or it already matches this chat -
        # remember the mapping for next time.
        intergram_tag_to_key[tag] = chat_customer_key
        return chat_customer_key, cleaned_text

    # No tag on this message - fall back to the older heuristics.
    if chat_customer_key in pending_approvals or chat_customer_key in user_memory:
        return chat_customer_key, cleaned_text

    phone = extract_phone_from_text(cleaned_text)
    old_key = find_pending_by_phone(phone) if phone else None

    if not old_key and len(pending_approvals) == 1 and FOLLOWUP_STATUS_RE.search(cleaned_text or ""):
        only_key, only_info = next(iter(pending_approvals.items()))
        if only_info.get("alert_sent"):
            old_key = only_key

    if not old_key or old_key == chat_customer_key:
        return chat_customer_key, cleaned_text

    _repoint_customer_routing(message, old_key)
    save_state()
    return old_key, cleaned_text


def maybe_bump_admin_for_pending(customer_key, text, label):
    """
    Call this for any message that reconciled to an existing, already-alerted,
    unresolved transaction and carries no new information (no ref, no phone,
    no package). We deliberately do NOT require specific wording here -
    "resend", "resubmission", "re-verify", "check again", or literally
    anything else the customer types in that state means the same thing:
    they're still waiting and the admin needs a nudge. If it's been a while
    since we last alerted, send a low-key reminder - never a second full
    "PAYMENT APPROVAL" alert, and never anything implying approval.
    Returns True if it sent a reminder.
    """
    info = pending_approvals.get(customer_key)
    if not info or not info.get("alert_sent"):
        return False

    now = time.time()
    last = info.get("last_alert_time", 0)
    if now - last < REBUMP_COOLDOWN_SECONDS:
        return False  # already nudged recently, don't spam

    if admin_chat_id:
        if info.get("proposed_code"):
            admin_bot.send_message(
                admin_chat_id,
                f"⏰ Reminder: {label} is still waiting on their voucher "
                f"(code ending {info.get('code_ending')}, {info.get('package')}, "
                f"phone {info.get('phone')}). A stored code is already reserved for them "
                f"('{info['proposed_code']}') — reply YES to send it, or NO to hold it."
            )
        else:
            admin_bot.send_message(
                admin_chat_id,
                f"⏰ Reminder: {label} is still waiting on a decision "
                f"(code ending {info.get('code_ending')}, {info.get('package')}, "
                f"phone {info.get('phone')}). No stock code is reserved yet — reply with a "
                f"voucher code to approve, or 'no' to reject."
            )
    info["last_alert_time"] = now
    save_state()
    return True

# ==========================================
# BOT 1: CUSTOMER BOT HANDLER
# ==========================================
@customer_bot.message_handler(func=lambda message: True)
def handle_customer_message(message):
    if message.content_type != 'text' or not (message.text or "").strip():
        return
    if is_service_message(message.text):
        cancel_pending_for_customer(make_customer_key(message))
        return

    customer_key = make_customer_key(message)
    raw_text = message.text.strip()

    # Reattach returning customers whose Intergram chat_id changed since their
    # last message - primarily via Intergram's own stable per-visitor tag,
    # with phone-number matching as a fallback. Also strips the tag off the
    # text so extraction regexes below see clean customer text.
    customer_key, text = reconcile_returning_customer(message, customer_key, raw_text)

    user_last_message_id[customer_key] = message.message_id
    customer_chat_id[customer_key] = message.chat.id
    customer_display[customer_key] = display_name_for(message)

    # 1. Deterministic Python Pre-Extraction & Slot Management
    extracted_ref = extract_code_ending_from_text(text)
    extracted_phone = extract_phone_from_text(text)
    extracted_pkg = extract_customer_package(text)

    # Make sure conversation memory exists before we possibly inject a system note below.
    if customer_key not in user_memory:
        user_memory[customer_key] = [{"role": "system", "content": system_rules}]

    # If this is just a status follow-up on an already-open, already-alerted
    # transaction (no fresh reference this turn), answer from real state and
    # skip the LLM entirely - guarantees no fabricated status, and only bumps
    # the admin at most once per cooldown window instead of on every message.
    if not extracted_ref:
        label = customer_display.get(customer_key, customer_key)
        pending_info = pending_approvals.get(customer_key)
        # Covers BOTH sub-states of an open, already-alerted transaction:
        #   - a stock code is reserved and we're waiting on admin's YES/NO, or
        #   - nothing is in stock and we're waiting on admin to supply a code.
        # Either way: no LLM call (so nothing can be improvised), and the admin
        # gets bumped (subject to cooldown) instead of silently hearing nothing.
        if pending_info and pending_info.get("alert_sent"):
            # If the admin has registered this payment's proof since the customer
            # first sent it, a simple follow-up ("resend", "hi") can now complete it.
            if try_auto_approve(customer_key):
                return
            maybe_bump_admin_for_pending(customer_key, text, label)
            reply_text = ensure_closing_warning(
                "Your payment is still with our team for verification — we haven't forgotten you. "
                "You'll get your voucher code the moment it's approved."
            )
            user_memory[customer_key].append({"role": "assistant", "content": reply_text})
            customer_bot.reply_to(message, reply_text)
            save_state()
            return

    if extracted_ref:
        customer_last_code[customer_key] = extracted_ref
        current_pending = pending_approvals.get(customer_key)
        # If approval code changed or is new, force reset phone/package slots
        if not current_pending or current_pending.get("code_ending") != extracted_ref:
            is_repeat_customer = current_pending is not None
            pending_approvals[customer_key] = {
                "code_ending": extracted_ref,
                "price": "$1.00",
                "phone": extracted_phone,     # Will be None if not in same msg, forcing prompt
                "package": extracted_pkg,     # Will be None if not in same msg, forcing prompt
                "package_key": extracted_pkg,
                "proposed_code": None,
                "alert_sent": False,
                # These flags track whether phone/package were confirmed by OUR extraction
                # (not by the AI's memory of a previous, unrelated transaction).
                "phone_confirmed": bool(extracted_phone),
                "package_confirmed": bool(extracted_pkg),
                # What the CUSTOMER claims they paid / the full reference they quoted.
                # Used to verify against an admin-registered proof for auto-approval.
                "claimed_amount": extract_payment_amount(text),
                "claimed_ref": extract_full_reference_from_text(text),
            }
            if is_repeat_customer:
                # Tell the model explicitly not to reuse stale slot values from earlier
                # in the chat history for this brand-new payment reference.
                user_memory[customer_key].append({
                    "role": "system",
                    "content": (
                        "SYSTEM NOTE: The customer just submitted a NEW payment reference, different "
                        "from any earlier one in this chat. Treat this as a brand-new, separate "
                        "transaction. Do NOT reuse the phone number or package the customer mentioned "
                        "earlier in this conversation for this new reference, even though it is visible "
                        "above in the chat history. You must ask them to (re)confirm both the phone "
                        "number and the package for THIS payment before producing any [ADMIN_ALERT]."
                    )
                })
        else:
            if extract_payment_amount(text):
                current_pending["claimed_amount"] = extract_payment_amount(text)
                current_pending["claimed_ref"] = extract_full_reference_from_text(text)
            if extracted_phone:
                current_pending["phone"] = extracted_phone
                current_pending["phone_confirmed"] = True
            if extracted_pkg:
                current_pending["package"] = extracted_pkg
                current_pending["package_key"] = extracted_pkg
                current_pending["package_confirmed"] = True
    else:
        # Update existing slot state with phone or package if sent separately
        if customer_key in pending_approvals:
            if extracted_phone:
                pending_approvals[customer_key]["phone"] = extracted_phone
                pending_approvals[customer_key]["phone_confirmed"] = True
            if extracted_pkg:
                pending_approvals[customer_key]["package"] = extracted_pkg
                pending_approvals[customer_key]["package_key"] = extracted_pkg
                pending_approvals[customer_key]["package_confirmed"] = True

    # 2. AUTO-APPROVAL: if the admin pre-registered this exact payment proof (amount +
    #    approval code match) and a matching voucher is in stock, deliver it right now,
    #    skip the AI entirely, and delete the registered proof. Anything that doesn't
    #    line up returns False and the normal AI + admin approval flow continues below.
    if try_auto_approve(customer_key):
        return

    user_memory[customer_key].append({"role": "user", "content": text})
    customer_bot.send_chat_action(message.chat.id, 'typing')

    try:
        completion = create_completion(
            user_memory[customer_key],
            model="openai/gpt-oss-120b",
            temperature=0.3,   
            max_completion_tokens=2048,
            top_p=1
        )
        ai_reply = completion.choices[0].message.content or ""

        alerts = re.findall(r'\[ADMIN_ALERT\](.*)', ai_reply, re.IGNORECASE)
        clean_reply = re.sub(r'\[ADMIN_ALERT\].*', '', ai_reply, flags=re.IGNORECASE).strip()

        known_text = "\n".join(
            m["content"] for m in user_memory[customer_key] if m["role"] == "user"
        )
        clean_reply, tampered = strip_fake_approval(clean_reply, known_text)
        if tampered:
            print(f"[GUARD] Blocked a fabricated approval/voucher in AI reply for {customer_key}.")

        alerts_to_process = []
        short_code_detected = False

        if alerts:
            for alert_text in alerts:
                alert_text = alert_text.strip()
                authoritative_code = (
                    (pending_approvals.get(customer_key) or {}).get("code_ending")
                    or customer_last_code.get(customer_key)
                )
                clean_authoritative = re.sub(r'[^A-Za-z0-9]', '', authoritative_code or '')
                parsed = parse_admin_alert(alert_text, text, authoritative_code=authoritative_code)

                if not parsed and len(clean_authoritative) >= 7:
                    # The AI's [ADMIN_ALERT] didn't match our strict pipe-delimited format at
                    # all (missing/garbled fields), but we ALREADY have a verified code for this
                    # customer's open transaction. Salvage rather than reject as "too short" -
                    # pull whatever PRICE/PHONE/PACKAGE we can from the alert text, falling back
                    # to what we've already tracked ourselves.
                    active = pending_approvals.get(customer_key, {})

                    def _salvage_field(field_name, default=None):
                        fm = re.search(rf'{field_name}:\s*([^|\n]+)', alert_text, re.IGNORECASE)
                        return fm.group(1).strip(" -\u2014:") if fm else default

                    parsed = {
                        "code_ending": clean_authoritative[-7:],
                        "price": _salvage_field("PRICE", active.get("price") or "$1.00"),
                        "phone": _salvage_field("PHONE", active.get("phone")),
                        "package": _salvage_field("PACKAGE", active.get("package")),
                    }

                if parsed:
                    status, result = finalize_alert_slots(parsed, customer_key)
                    if status == "missing":
                        clean_reply = result
                        alerts_to_process = []  # Suppress admin alert until slots are filled
                    else:
                        alerts_to_process.append((alert_text, result))
                else:
                    m = re.search(r'CODE_ENDING:\s*([^|]+)', alert_text, re.IGNORECASE)
                    if m:
                        raw_c = re.sub(r'[^A-Za-z0-9]', '', m.group(1))
                        if raw_c and len(raw_c) < 7:
                            short_code_detected = True
                            continue 
                    
                    alerts_to_process.append((alert_text, None))

        is_likely_ref_attempt = any(kw in text.lower() for kw in ["pp", "code", "ref", "trans", "sent to"]) or len(re.sub(r'[^0-9]', '', text)) >= 4

        if short_code_detected and is_likely_ref_attempt:
            clean_reply = "⚠️ The transaction reference provided is too short. A valid approval code must be at least 7 characters long. Please send the FULL exact transaction reference."
            alerts_to_process = []
        elif short_code_detected and not is_likely_ref_attempt:
            alerts_to_process = []
        elif not clean_reply and (alerts_to_process or tampered):
            clean_reply = WAIT_MESSAGE if alerts_to_process else NEED_PROOF_MESSAGE

        # GUARD + Templating Enforcement.
        #
        # The failure mode we're guarding against is specifically: the model claiming a NEW
        # payment was JUST received/is now being validated, when nothing genuine happened this
        # turn (no reference extracted, no admin alert generated). That is fabrication, built
        # from the model recalling an old exchange in chat history.
        #
        # That is NOT the same as the model simply referencing an EXISTING, still-open pending
        # transaction to ask for missing info ("I see your code ending X, please also confirm
        # your package") - that's a legitimate, honest continuation, and blocking it breaks
        # ordinary slot-filling conversations. So a mention of "code ending" is only suspect when
        # there's no currently-open pending transaction for this customer AND nothing genuine
        # happened this specific turn.
        active_pending = pending_approvals.get(customer_key)
        has_open_pending = bool(active_pending)
        genuine_receipt_this_turn = bool(extracted_ref) or bool(alerts_to_process)
        implies_new_receipt = bool(clean_reply) and bool(FAKE_RECEIPT_RE.search(clean_reply))
        mentions_code_reference = bool(clean_reply) and (
            "code ending" in clean_reply.lower() or "approval code" in clean_reply.lower()
        )

        if implies_new_receipt and not genuine_receipt_this_turn:
            print(f"[GUARD] Blocked a reply falsely claiming a NEW payment was just received for "
                  f"{customer_key} (no new proof extracted and no admin alert generated this turn).")
            clean_reply = NO_GENUINE_PROOF_MESSAGE
        elif mentions_code_reference and not (genuine_receipt_this_turn or has_open_pending):
            print(f"[GUARD] Blocked a reply referencing a code/approval reference for {customer_key} "
                  "with no open pending transaction and no genuine proof this turn.")
            clean_reply = NO_GENUINE_PROOF_MESSAGE
        elif mentions_code_reference:
            # Genuine case (fresh this turn, or honestly continuing an already-open transaction):
            # enforce Python's exact verified code value rather than whatever the model wrote.
            true_ending = (active_pending or {}).get("code_ending") or customer_last_code.get(customer_key)
            if true_ending:
                clean_reply = re.sub(
                    r'(Approval Code ending\s*\**\s*)[A-Za-z0-9]+(\s*\**)',
                    rf'\1**{true_ending}**\2',
                    clean_reply,
                    flags=re.IGNORECASE
                )

        duplicate_notice = False

        if alerts_to_process:
            if admin_chat_id:
                for alert_text, parsed in alerts_to_process:
                    label = customer_display.get(customer_key, str(customer_key))

                    if parsed:
                        fp = payment_fingerprint(parsed)

                        if fp and fp in used_payment_refs:
                            duplicate_notice = True
                            admin_bot.send_message(
                                admin_chat_id,
                                f"⚠️ DUPLICATE payment proof from {label} "
                                f"(code ending {parsed['code_ending']}, {parsed['price']}, phone {parsed['phone']}). "
                                "A voucher was already issued for this proof, so no new request was created."
                            )
                            continue

                        existing = pending_approvals.get(customer_key, {})
                        if existing.get("alert_sent"):
                            continue # Already alerted

                        if existing.get("proposed_code"):
                            return_voucher(existing.get("package_key"), existing["proposed_code"])

                        pending_approvals[customer_key] = parsed
                        # Carry over what the customer claimed, so a proof the admin registers
                        # LATER can still be verified against it and auto-approve this customer.
                        pending_approvals[customer_key]["claimed_amount"] = existing.get("claimed_amount")
                        pending_approvals[customer_key]["claimed_ref"] = existing.get("claimed_ref")
                        pkg_key = normalize_package(parsed['package'])
                        reserved_code = reserve_voucher(pkg_key)
                        pending_approvals[customer_key]["package_key"] = pkg_key
                        pending_approvals[customer_key]["proposed_code"] = reserved_code
                        pending_approvals[customer_key]["alert_sent"] = True
                        pending_approvals[customer_key]["phone_confirmed"] = True
                        pending_approvals[customer_key]["package_confirmed"] = True
                        pending_approvals[customer_key]["last_alert_time"] = time.time()

                        if reserved_code:
                            admin_msg = (
                                f"🔔 PAYMENT APPROVAL — STOCK CODE AVAILABLE\n"
                                f"Customer: {label}\n"
                                f"Code ending: {parsed['code_ending']}\n"
                                f"Price: {parsed['price']}\n"
                                f"Phone: {parsed['phone']}\n"
                                f"Package: {parsed['package']}\n\n"
                                f"Stored voucher found: {reserved_code}\n"
                                f"Reply YES to send it to the customer, or NO to hold it and provide a different code."
                            )
                        else:
                            admin_msg = (
                                f"🔔 NEW PAYMENT APPROVAL REQUEST — OUT OF STOCK\n"
                                f"Customer: {label}\n"
                                f"Code ending: {parsed['code_ending']}\n"
                                f"Price: {parsed['price']}\n"
                                f"Phone: {parsed['phone']}\n"
                                f"Package: {parsed['package']}\n\n"
                                f"No stored code for this package. Reply with a new voucher code to approve, or 'no' to reject."
                            )
                    else:
                        if customer_key in pending_approvals and pending_approvals[customer_key].get("alert_sent"):
                            continue
                        pending_approvals[customer_key] = {
                            "code_ending": extract_code_ending_from_text(text) or "",
                            "price": "", "phone": "", "package": "",
                            "package_key": None, "proposed_code": None, "alert_sent": True,
                            "last_alert_time": time.time(),
                        }
                        admin_msg = f"🔔 ADMIN ALERT (Customer: {label}):\n{alert_text}"
                    admin_bot.send_message(admin_chat_id, admin_msg)
            else:
                clean_reply += "\n\n⚠️ SYSTEM NOTIFICATION: The Admin Bot is currently unlinked. (Admin: Please send /start to the Admin bot to reconnect routing)."

        if duplicate_notice:
            clean_reply = DUPLICATE_MESSAGE

        if clean_reply:
            clean_reply = ensure_closing_warning(clean_reply)
            history_entry = clean_reply
            
            if alerts_to_process and not duplicate_notice:
                history_entry += "".join(f"\n[ADMIN_ALERT]{a[0]}" for a in alerts_to_process)
            user_memory[customer_key].append({"role": "assistant", "content": history_entry})
            customer_bot.reply_to(message, clean_reply)

        if len(user_memory[customer_key]) > 15:
            user_memory[customer_key] = [user_memory[customer_key][0]] + user_memory[customer_key][-14:]

        save_state()

    except Exception as e:
        customer_bot.reply_to(message, f"Error processing request: {str(e)}")

@customer_bot.message_handler(content_types=['left_chat_member'])
def handle_customer_left(message):
    left_user = message.left_chat_member
    if not left_user:
        return
    customer_key = f"{message.chat.id}:{left_user.id}"
    cancel_pending_for_customer(customer_key)


# ==========================================
# BOT 2: ADMIN BOT HANDLER
# ==========================================
INVISIBLE_CHARS_RE = re.compile(r'[\u200e\u200f\u200b\u200c\u200d\u2060\ufeff\u202a-\u202e]')

STOCK_COMMANDS = {'stock', '/stock', 'inventory', '/inventory'}
STOCK_FULL_COMMANDS = {
    'stock full', 'stock detail', 'list codes',
    '/stockfull', '/stockdetail', '/orstockdetail', '/listcodes', '/codes',
}

def clean_admin_text(raw):
    text = INVISIBLE_CHARS_RE.sub('', raw or '').strip()
    return re.sub(r'^(/\w+)@\w+', r'\1', text)

@admin_bot.message_handler(func=lambda message: True)
def handle_admin_message(message):
    global admin_chat_id

    if message.content_type != 'text' or not (message.text or "").strip():
        return
    if is_service_message(message.text):
        return

    if not is_authorized_admin(message):
        admin_bot.reply_to(message, "🚫 You are not authorized to use this bot.")
        return

    admin_chat_id = message.chat.id
    text = clean_admin_text(message.text)
    if not text:
        return

    if text.lower() in ['/start', 'hello', 'hi', 'link']:
        admin_bot.reply_to(message, f"✅ Admin Link Active! (Your ID: {admin_chat_id})\nReady to receive and route vouchers.")
        save_state()
        return

    if text.lower() in ['/addcode', 'addcode']:
        with _admin_state_lock:
            admin_awaiting[admin_chat_id] = 'add_code'
        admin_bot.reply_to(
            message,
            "✏️ Type a code to be added, followed by its package:\n<code> for <package>\n\nExample: 6786gfr for 7 DAYS"
        )
        return

    if text.lower() in ['/deletecode', 'deletecode']:
        with _admin_state_lock:
            admin_awaiting[admin_chat_id] = 'delete_code'
        admin_bot.reply_to(
            message,
            "✏️ Type a code to be deleted.\nOptionally add the package: <code> from <package>"
        )
        return

    # Pre-registered proofs of payment (auto-approval). This MUST run before the bulk-add
    # check below, because a pasted "Approval Code: XXX\nNew balance: ..." would otherwise be
    # mistaken for a "CODE: xxx / <package>" voucher-add entry.
    proof_reply = handle_proof_admin_message(text)
    if proof_reply is not None:
        admin_bot.reply_to(message, proof_reply)
        save_state()
        return

    awaiting = None
    with _admin_state_lock:
        if text.startswith('/'):
            admin_awaiting.pop(admin_chat_id, None)
        else:
            awaiting = admin_awaiting.pop(admin_chat_id, None)

    if awaiting == 'add_code':
        m = ADD_CODE_SHORT_PATTERN.match(text)
        if not m:
            admin_bot.reply_to(
                message,
                "⚠️ Didn't recognize that format. Please send it as:\n<code> for <package>"
            )
            return
        code = m.group("code").strip()
        pkg_input = m.group("package").strip()
        pkg_key = normalize_package(pkg_input)
        if pkg_key:
            with _inventory_lock:
                voucher_inventory.setdefault(pkg_key, []).append(code)
            admin_bot.reply_to(
                message,
                f"✅ Added to stock:\n{code} → {pkg_key} ({PACKAGES[pkg_key]['price']}, {PACKAGES[pkg_key]['data']})"
            )
            save_state()
        else:
            admin_bot.reply_to(
                message,
                f"⚠️ Could not match package '{pkg_input}'.\n\nKnown packages: " + ", ".join(PACKAGES.keys())
            )
        return

    if awaiting == 'delete_code':
        m = DELETE_CODE_SHORT_PATTERN.match(text)
        if not m:
            admin_bot.reply_to(message, "⚠️ Didn't recognize that. Please send just the code, e.g.: A1B2C3")
            return
        code = m.group("code").strip()
        pkg_input = m.group("package")
        pkg_key = normalize_package(pkg_input.strip()) if pkg_input else None
        found_pkg = delete_voucher(code, pkg_key)
        if found_pkg:
            admin_bot.reply_to(message, f"🗑️ Removed from stock:\n{code} (was in {found_pkg})")
            save_state()
        else:
            admin_bot.reply_to(message, f"⚠️ Not found in stock (already used, wrong package, or typo): {code}")
        return

    if text.lower() in STOCK_COMMANDS:
        lines = [
            f"- {pkg} ({info['price']}, {info['data']}): {len(voucher_inventory.get(pkg, []))} code(s)"
            for pkg, info in PACKAGES.items()
        ]
        admin_bot.reply_to(
            message,
            "📦 Voucher stock:\n" + "\n".join(lines) +
            "\n\nSend /stockfull to see the actual codes."
        )
        return

    if text.lower() in STOCK_FULL_COMMANDS:
        lines = []
        for pkg, info in PACKAGES.items():
            codes = voucher_inventory.get(pkg, [])
            codes_str = ", ".join(codes) if codes else "(none)"
            lines.append(f"- {pkg} ({info['price']}, {info['data']}): {codes_str}")
        admin_bot.reply_to(message, "📦 Voucher stock (detailed):\n" + "\n".join(lines))
        return

    bulk_add_entries = parse_bulk_add_entries(text)
    if bulk_add_entries:
        added, failed = [], []
        for code, pkg_key, raw_package in bulk_add_entries:
            if pkg_key:
                with _inventory_lock:
                    voucher_inventory.setdefault(pkg_key, []).append(code)
                added.append(f"{code} → {pkg_key} ({PACKAGES[pkg_key]['price']}, {PACKAGES[pkg_key]['data']})")
            else:
                failed.append(f"{code} (unrecognized package '{raw_package}')")

        reply_parts = []
        if added:
            reply_parts.append(f"✅ Added {len(added)} code(s) to stock:\n" + "\n".join(added))
        if failed:
            reply_parts.append(
                "⚠️ Could not match package for:\n" + "\n".join(failed) +
                "\n\nKnown packages: " + ", ".join(PACKAGES.keys())
            )
        admin_bot.reply_to(message, "\n\n".join(reply_parts))
        save_state()
        return

    delete_matches = [m for m in (DELETE_CODE_PATTERN.match(line) for line in text.splitlines()) if m]
    if delete_matches:
        removed, not_found = [], []
        for m in delete_matches:
            code = m.group("code").strip()
            pkg_input = m.group("package")
            pkg_key = normalize_package(pkg_input.strip()) if pkg_input else None
            found_pkg = delete_voucher(code, pkg_key)
            if found_pkg:
                removed.append(f"{code} (was in {found_pkg})")
            else:
                not_found.append(code)

        reply_parts = []
        if removed:
            reply_parts.append("🗑️ Removed from stock:\n" + "\n".join(removed))
        if not_found:
            reply_parts.append(
                "⚠️ Not found in stock (already used, wrong package, or typo):\n" + "\n".join(not_found)
            )
        admin_bot.reply_to(message, "\n\n".join(reply_parts))
        save_state()
        return

    add_matches = [m for m in (ADD_CODE_PATTERN.match(line) for line in text.splitlines()) if m]
    if add_matches:
        added, failed = [], []
        for m in add_matches:
            code = m.group("code").strip()
            pkg_input = m.group("package").strip()
            pkg_key = normalize_package(pkg_input)
            if pkg_key:
                with _inventory_lock:
                    voucher_inventory.setdefault(pkg_key, []).append(code)
                added.append(f"{code} → {pkg_key} ({PACKAGES[pkg_key]['price']}, {PACKAGES[pkg_key]['data']})")
            else:
                failed.append(f"{code} (unrecognized package '{pkg_input}')")

        reply_parts = []
        if added:
            reply_parts.append("✅ Added to stock:\n" + "\n".join(added))
        if failed:
            reply_parts.append(
                "⚠️ Could not match package for:\n" + "\n".join(failed) +
                "\n\nKnown packages: " + ", ".join(PACKAGES.keys())
            )
        admin_bot.reply_to(message, "\n\n".join(reply_parts))
        save_state()
        return

    if text.startswith('/'):
        admin_bot.reply_to(
            message,
            "⚠️ Unknown command. Available:\n"
            "/stock, /stockfull, /stockdetail, /listcodes, /codes, /addcode, /deletecode, "
            "/proofs, /delproof"
        )
        return

    target_key = find_target_customer(text)

    if target_key is None:
        if not pending_approvals:
            admin_bot.reply_to(message, "⚠️ No customers are currently waiting for approval.")
        else:
            pending_list = "\n".join(
                f"- {customer_display.get(cid, cid)}: code ending {info.get('code_ending') or '?'}, "
                f"{info.get('package') or '?'}, phone {info.get('phone') or '?'}"
                for cid, info in pending_approvals.items()
            )
            admin_bot.reply_to(
                message,
                "⚠️ Multiple customers are waiting — please include the code ending to specify which one:\n\n"
                f"{pending_list}"
            )
        return

    info = pending_approvals.get(target_key, {})
    target_chat_id = customer_chat_id.get(target_key)
    reply_id = user_last_message_id.get(target_key)
    target_label = customer_display.get(target_key, target_key)

    if target_chat_id is None:
        if info.get("proposed_code"):
            return_voucher(info.get("package_key"), info["proposed_code"])
        admin_bot.reply_to(message, f"⚠️ Lost track of chat for {target_label}; they'll need to message again.")
        pending_approvals.pop(target_key, None)
        save_state()
        return

    def send_to_customer(msg_text):
        if reply_id:
            customer_bot.send_message(target_chat_id, msg_text, reply_to_message_id=reply_id)
        else:
            customer_bot.send_message(target_chat_id, msg_text)

    if info.get("proposed_code"):
        decision = text.strip().lower()

        if decision in YES_WORDS:
            code = info["proposed_code"]
            package = info.get("package") or "your package"
            voucher_message = (
                f"✅ Your payment has been approved!\n\n"
                f"Package: {package}\n"
                f"Voucher code: {code}\n\n"
                + CLOSING_WARNING
            )
            history = user_memory.setdefault(target_key, [{"role": "system", "content": system_rules}])
            note_voucher_delivered(history, package)
            send_to_customer(voucher_message)

            mark_payment_used(info)
            admin_bot.reply_to(message, f"✅ Delivered stored code '{code}' to {target_label} instantly.")
            pending_approvals.pop(target_key, None)
            save_state()
            return

        elif decision in NO_WORDS:
            return_voucher(info.get("package_key"), info["proposed_code"])
            pending_approvals[target_key]["proposed_code"] = None
            admin_bot.reply_to(
                message,
                f"↩️ Put that code back in stock. {target_label} is still waiting — "
                "please reply with a different voucher code for them."
            )
            save_state()
            return

        else:
            admin_bot.reply_to(
                message,
                f"❓ {target_label} has a stored code ('{info['proposed_code']}') awaiting your confirmation. "
                "Reply exactly YES to send it, or NO to put it back in stock."
            )
            return

    decision = text.strip().lower()
    history = user_memory.setdefault(target_key, [{"role": "system", "content": system_rules}])

    if decision in NO_WORDS:
        rejection_message = (
            "❌ Unfortunately we could not verify your payment. "
            "Please double-check your proof of payment and try again, or contact support.\n\n"
            + CLOSING_WARNING
        )
        history.append({"role": "assistant", "content": rejection_message})
        send_to_customer(rejection_message)

        admin_bot.reply_to(message, f"❌ Rejection sent to {target_label}.")
        pending_approvals.pop(target_key, None)
        save_state()
        return

    code = text.strip()

    if len(code.split()) != 1:
        admin_bot.reply_to(
            message,
            f"⚠️ That doesn't look like a single voucher code, so I did NOT send it to {target_label}. "
            "Send only the code (no spaces), or 'no' to reject."
        )
        return

    package = info.get("package") or "your package"
    voucher_message = (
        f"✅ Your payment has been approved!\n\n"
        f"Package: {package}\n"
        f"Voucher code: {code}\n\n"
        + CLOSING_WARNING
    )
    note_voucher_delivered(history, package)
    send_to_customer(voucher_message)

    mark_payment_used(info)
    admin_bot.reply_to(message, f"✅ Delivered code '{code}' to {target_label} instantly.")
    pending_approvals.pop(target_key, None)
    save_state()


# ==========================================
# SERVER AND MULTI-THREADING
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Dual-Bot System Running!"

# ------------------------------------------
# SMS WEBHOOK: your phone forwards each EcoCash confirmation SMS here, and the proof is
# registered automatically (exactly as if you had pasted it into the admin bot).
#
# Phone app (webhook-smsforwarder) sends:  POST /sms   JSON {"sender","message","sim","device"}
# with the header:                         X-Webhook-Secret: <SMS_WEBHOOK_SECRET>
#
# Env vars:
#   SMS_WEBHOOK_SECRET   (required - the endpoint stays disabled until this is set)
#   SMS_ALLOWED_SENDERS  (optional, comma-separated, e.g. "EcoCash" - extra sender filter)
# ------------------------------------------
SMS_WEBHOOK_SECRET = os.environ.get("SMS_WEBHOOK_SECRET", "").strip()
SMS_ALLOWED_SENDERS = [
    re.sub(r'[^a-z0-9]', '', s.lower())
    for s in os.environ.get("SMS_ALLOWED_SENDERS", "").split(",") if s.strip()
]

def _sms_secret_ok(req):
    if not SMS_WEBHOOK_SECRET:
        return False
    supplied = (req.headers.get("X-Webhook-Secret") or "").strip()
    if not supplied:
        auth = (req.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), SMS_WEBHOOK_SECRET.encode("utf-8"))

@app.route('/sms', methods=['GET', 'POST'])
def sms_webhook():
    if request.method == 'GET':
        return "SMS webhook is up (POST only).", 200
    if not SMS_WEBHOOK_SECRET:
        return jsonify({"status": "disabled"}), 503
    if not _sms_secret_ok(request):
        return jsonify({"status": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    sender = str(data.get("sender") or "").strip()
    body = str(data.get("message") or "").strip()
    if not body:
        return jsonify({"status": "ignored", "reason": "empty message"}), 200

    if SMS_ALLOWED_SENDERS:
        sender_norm = re.sub(r'[^a-z0-9]', '', sender.lower())
        if not any(allowed in sender_norm for allowed in SMS_ALLOWED_SENDERS):
            print(f"[SMS] Ignored message from unlisted sender '{sender}'.")
            return jsonify({"status": "ignored", "reason": "sender not allowed"}), 200

    # Only EcoCash-style confirmations (with an "Approval Code") are ever registered.
    if not PROOF_MARKER_RE.search(body):
        return jsonify({"status": "ignored", "reason": "not a payment confirmation"}), 200

    result = register_proofs_from_admin(body)
    save_state()
    print(f"[SMS] Processed confirmation from '{sender}'.")
    if admin_chat_id:
        try:
            admin_bot.send_message(
                admin_chat_id,
                f"📲 EcoCash SMS received (sender: {sender or 'unknown'})\n{result}\n\n"
                "If this wasn't a real payment, remove it with /delproof <last 7 chars>."
            )
        except Exception as e:
            print(f"[SMS] Could not notify admin: {e}")
    return jsonify({"status": "processed"}), 200

def run_customer_bot():
    # Watchdog: pyTelegramBotAPI's infinity_polling() is *supposed* to retry forever on any
    # exception, but a 409 Conflict from getUpdates (e.g. during a Render rolling deploy, where
    # the old instance briefly overlaps with the new one) can escape that internal retry and
    # kill this thread silently - Flask keeps answering health checks, so the service looks
    # "up" on Render, but nothing is actually polling Telegram anymore. This outer loop makes
    # sure polling always restarts instead of dying quietly.
    while True:
        try:
            customer_bot.remove_webhook()  # clears any stray webhook that could also cause 409s
        except Exception as e:
            print(f"[Customer Bot] remove_webhook failed (continuing anyway): {e}")
        try:
            print("[Customer Bot] Starting polling...")
            customer_bot.infinity_polling(timeout=20, long_polling_timeout=20)
        except Exception as e:
            print(f"[Customer Bot] Polling crashed: {e}")
        print("[Customer Bot] Polling stopped/crashed - restarting in 5s...")
        time.sleep(5)

def run_admin_bot():
    while True:
        try:
            admin_bot.remove_webhook()
        except Exception as e:
            print(f"[Admin Bot] remove_webhook failed (continuing anyway): {e}")
        try:
            print("[Admin Bot] Starting polling...")
            admin_bot.infinity_polling(timeout=20, long_polling_timeout=20)
        except Exception as e:
            print(f"[Admin Bot] Polling crashed: {e}")
        print("[Admin Bot] Polling stopped/crashed - restarting in 5s...")
        time.sleep(5)

if __name__ == "__main__":
    import sys
    print("[VERSION] splash_bot.py — lenient package matching + open-pending guard fix + Intergram reconnect + pre-registered proof auto-approval", flush=True)
    load_state()
    print(f"[Groq] Loaded {len(groq_clients)} API key(s) for rotation/fallback.", flush=True)
    print(f"[AUTO] Auto-approval {'ENABLED' if AUTO_APPROVE_ENABLED else 'DISABLED'} "
          f"({len(registered_proofs)} registered proof(s) loaded).", flush=True)
    print(f"[SMS] Webhook /sms {'ENABLED' if SMS_WEBHOOK_SECRET else 'DISABLED (set SMS_WEBHOOK_SECRET to enable)'}.", flush=True)
    Thread(target=run_customer_bot).start()
    Thread(target=run_admin_bot).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
