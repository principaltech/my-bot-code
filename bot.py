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
    os.environ.get("GROQ_API_KEY_8"),
    os.environ.get("GROQ_API_KEY_9"),
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
customer_last_phone = {}    # customer_key -> last Python-VERIFIED phone number (persists across
                             # turns even before a payment reference exists, so a legitimately
                             # provided phone isn't forgotten by the time proof of payment arrives)
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
        "customer_last_phone": customer_last_phone,
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
        customer_last_phone.update(snapshot.get("customer_last_phone", {}))
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

# Maps Intergram's per-visitor tag (the short label Intergram prefixes onto every
# forwarded message, e.g. "y6ysyx: Hi" - everything up to the first ":" is the
# tag, everything after it is the actual customer message) to whichever
# customer_key last used that tag.
#
# CONFIRMED: this tag changes together with chat_id whenever the customer
# leaves/reopens the widget chat - it behaves like chat_id itself, not like a
# persistent visitor id. So it can't reliably identify a returning customer
# across a session reset (a new session means a brand-new, never-seen tag).
# Kept only as a harmless, best-effort fallback - see reconcile_returning_customer()
# below, which tries the phone number FIRST because that's the signal that
# actually survives a reset.
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
   - EcoCash number is 0776248396. Customers can also click the price of a package on the login portal and process the payment to Splash there.
   - You only need proof of payment. The backend detects the package from the amount and does NOT need a phone number unless the payment takes long.
   - PHONE NUMBER FORMAT: A valid Zimbabwean mobile number is exactly 10 digits starting with 071, 077, 078, or 079 (e.g. 0776248396), or the same number in +263 format (e.g. +263776248396). This is a SEPARATE rule from the transaction reference length below - do not confuse the two. If the customer's phone number is missing digits, has the wrong prefix, or is otherwise not in this format, ask them to resend it correctly. NEVER treat a 7-digit string as a valid phone number.
   - DISTINGUISH REPLIES: If the user is just answering a question about which package they want (e.g. saying "7d", "7 days", "lite"), DO NOT treat it as a payment reference. Only evaluate transaction references when a full payment confirmation block is provided.
   - A VALID TRANSACTION REFERENCE MUST BE AT LEAST 7 CHARACTERS LONG. If a user provides a reference that is less than 7 characters as a payment code, reject it.
   - EXTRACTING THE TRANSACTION REFERENCE: Look for the longest alphanumeric string in the message. CODE_ENDING MUST be the EXACT last 7 characters of that full reference. Ignore punctuation like dots or dashes. NEVER accept or use a code shorter than 7 characters.
4. AFTER PAYMENT PROOF IS SUBMITTED:
   - Tell the user to wait 35 seconds while the payment is validated. That is ALL you say about the outcome.
   - CRITICAL: Do NOT attempt to repeat, quote, or summarize the customer's transaction reference back to them in your conversational reply. The reference must ONLY be output inside the [ADMIN_ALERT] tag.
5. ADMIN PAYMENT APPROVAL (CRITICAL INSTRUCTION):
   - When a user has provided a valid payment reference (minimum 7 chars), generate the exact tag [ADMIN_ALERT] followed immediately by a structured line:
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
12. PROOF OF PAYMENT FORMAT (ALWAYS RECOMMEND THIS):
   - Whenever the customer wants to buy, asks how to pay, or has not yet sent proof, tell them to pay via EcoCash and then paste the FULL proof of payment in ONE message, using exactly this format:
     Paste Full Proof of payment:
   - Tell them to paste the ENTIRE EcoCash confirmation message (the whole "Transfer Confirmation: ... Approval Code: ... New balance: ..." text), NOT just the last digits of the approval code. The backend reads the full approval code and the amount from it, detects the package from the amount, and compares it with our records.
   - If the customer sends only part of the code, politely ask them to paste the FULL proof of payment.
   - Do NOT ask for a phone number or package up front.
13. WHERE TO PAY:
   - If the customer asks for the EcoCash number, where to pay, or how to pay, tell them to process their EcoCash payment to 0776248396, OR alternatively to click the price of the package they want on the login portal and pay to Splash. Then tell them to paste the FULL proof of payment. Use 0776248396 in any phone number examples.
14. PHONE NUMBER TIMING (DO NOT IMPROVISE THIS):
   - NEVER ask the customer for their phone number on your own initiative. The backend decides exactly when a phone number is actually needed and will tell you via an explicit SYSTEM NOTE when that's the case. If you don't see such a note, do not bring up the phone number at all, even if you think it would help speed things along.
   - Likewise, never tell the customer "you'd like package X" or otherwise confirm a package choice based on a vague or partial message (like a stray data amount or a cut-off sentence). If you are not certain which package they mean, ask them to reply with just the package name.
"""

# ==========================================
# BUSINESS PAYMENT NUMBER + "WHERE DO I PAY?" AUTO-REPLY
# ==========================================
BUSINESS_NUMBER = "0776248396"
BUSINESS_NUMBER_INTL = "+263776248396"

PAYMENT_INFO_MESSAGE = (
    f"To pay, send your EcoCash payment to {BUSINESS_NUMBER} (Splash Internet).\n\n"
    "Or, even simpler: on the login portal, click the price of the package you want "
    "and process the payment to Splash from there.\n\n"
    "After paying, paste the FULL proof of payment (the entire EcoCash confirmation "
    "message) here using this format:\n\n"
    "Paste Full Proof of payment:"
)

PAY_WHERE_RE = re.compile(
    r"eco\s*-?\s*cash\s*(number|no\b|num)"
    r"|(what|which|whats|what's)\s*(is\s*)?(the\s*|your\s*|ur\s*)?(number|no\b)"
    r"|where\s*(do|can|should|must|to)\s*(i\s*|we\s*)?(pay|send|deposit)"
    r"|how\s*(do|can|to|should)\s*(i\s*|we\s*)?pay"
    r"|payment\s*(number|details|info|method)"
    r"|send\s*(money|payment)\s*to"
    r"|(number|namba)\s*(to|yekubhadhara|for)\s*(pay|payment)?",
    re.IGNORECASE
)

def asks_where_to_pay(text):
    if not text or PROOF_MARKER_RE.search(text):
        return False
    return bool(PAY_WHERE_RE.search(text))

WAIT_MESSAGE = "Thank you, we have received your payment proof. Please wait about 30 seconds while we validate the payment."
ONE_MESSAGE_FORMAT = "Paste Full Proof of payment:"
NEED_PROOF_MESSAGE = (
    "Please send everything in ONE message, using this format:\n\n"
    + ONE_MESSAGE_FORMAT +
    "\n\nPaste the ENTIRE EcoCash confirmation message (from \"Transfer Confirmation\" to the end), "
    "not just part of the approval code."
)
NEED_FULL_PROOF_MESSAGE = (
    "I received a code, but I need the FULL proof of payment. Please paste the ENTIRE EcoCash "
    "confirmation message (from \"Transfer Confirmation\" to the end), not just part of the approval code.\n\n"
    "Use this format:\n\n"
    + ONE_MESSAGE_FORMAT
)
DUPLICATE_MESSAGE = ("This payment proof has already been processed. If you did not receive your voucher, "
                     "please scroll up in this chat to find it, or contact support.")

def build_welcome_message():
    lines = [f"- {pkg}: {info['price']} = {info['data']}" for pkg, info in PACKAGES.items()]
    return (
        "👋 Welcome to Splash Internet!\n\n"
        "Packages:\n" + "\n".join(lines) + "\n\n"
        f"Pay via EcoCash to {BUSINESS_NUMBER}, or simply click the price of your package on the "
        "login portal and pay to Splash. Then paste the ENTIRE EcoCash confirmation message "
        "(from \"Transfer Confirmation\" to the end) here. Your package is detected automatically "
        "from the amount you paid."
    )

PHONE_PATTERN = re.compile(r'\b(0(?:71|77|78|79)\d{7}|\+?263(?:71|77|78|79)\d{7})\b')

def extract_phone_from_text(text):
    if not text:
        return None
    for m in PHONE_PATTERN.finditer(text):
        if normalize_phone(m.group(1)) == normalize_phone(BUSINESS_NUMBER):
            continue  # that's OUR EcoCash number, not the customer's
        return m.group(1)
    return None

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
    "I don't see a new payment proof in your last message. If you already paid, please send "
    "everything in ONE message, using this format:\n\n"
    + ONE_MESSAGE_FORMAT +
    "\n\nPaste the ENTIRE EcoCash confirmation message, not just part of the approval code."
)

# Detects the model confirming/echoing a phone number back to the customer (e.g. "phone
# number **392-8906**", "for phone number 0771234567") anywhere within ~40 characters of
# the words "phone number". Used to catch the model treating an invalid or unverified
# string (like a bare 7-digit input) as if it were a captured phone number, when Python's
# own PHONE_PATTERN extraction found nothing valid.
PHONE_LABEL_RE = re.compile(r'phone\s*number', re.IGNORECASE)
CLAIMED_DIGIT_RUN_RE = re.compile(r'\+?\d[\d\s\-\u2010\u2011\u2012\u2013\u2014\u2015.]{5,}\d')

def reply_claims_phone_number(reply_text):
    if not reply_text:
        return False
    for m in PHONE_LABEL_RE.finditer(reply_text):
        window = reply_text[m.end():m.end() + 40]
        if CLAIMED_DIGIT_RUN_RE.search(window):
            return True
    return False

INVALID_PHONE_MESSAGE = (
    "I want to double-check your phone number before we go further. Please resend your "
    "Zimbabwean mobile number so I can confirm it - it must be exactly 10 digits starting "
    "with 071, 077, 078, or 079 (e.g. 0776248396), or the same number in +263 format "
    "(e.g. +263776248396)."
)

def normalize_phone(num):
    """Canonical local 10-digit form (e.g. '0771234567') for comparing two phone numbers
    regardless of whether they were written with a leading 0 or +263. Returns None if the
    value isn't a well-formed 10-digit Zimbabwean number once normalized."""
    if not num:
        return None
    digits = re.sub(r'\D', '', num)
    if digits.startswith('263') and len(digits) == 12:
        digits = '0' + digits[3:]
    return digits if len(digits) == 10 else None

def reply_contains_mismatched_phone(reply_text, known_phone):
    """Phrasing-INDEPENDENT check: scans the whole reply for anything shaped like a real,
    well-formed Zimbabwean number (via the same strict PHONE_PATTERN used for extraction),
    regardless of whether the words "phone number" appear anywhere near it. If a match is
    found that doesn't equal the one number we've actually verified for this customer (or
    nothing has been verified yet), that's the model asserting a phone number with zero
    Python-side backing - exactly the failure mode that a label-only check can't catch when
    the model phrases it as e.g. "package for 0776543324" instead of "phone number: ...".
    Skips numbers that are clearly just illustrative examples (preceded by "e.g."/"example")."""
    if not reply_text:
        return False
    known_norm = normalize_phone(known_phone)
    for m in PHONE_PATTERN.finditer(reply_text):
        prefix = reply_text[max(0, m.start() - 15):m.start()].lower()
        if 'e.g' in prefix or 'example' in prefix:
            continue
        if normalize_phone(m.group(1)) == normalize_phone(BUSINESS_NUMBER):
            continue  # our own payment number is always allowed
        if normalize_phone(m.group(1)) != known_norm:
            return True
    return False

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

# Fallback for the one-message format when the customer types ONLY the code, e.g.
#   Proof of payment: PP260919.2324.T3345746      or      Proof of payment: 3345746
# The value must be a single token alone on its line (so "Proof of payment: Transfer
# Confirmation: ..." is never mistaken for a reference) and contain at least 4 digits.
PROOF_FIELD_REF_PATTERN = re.compile(
    r'proof\s*of\s*payment\s*[:\-]\s*([A-Za-z0-9][A-Za-z0-9.\-]{6,})[ \t]*$',
    re.IGNORECASE | re.MULTILINE
)

def extract_code_ending_from_text(text):
    """Returns ONLY the last 7 alphanumeric characters of the approval code - that is the
    only part of a reference the whole system ever uses."""
    if not text:
        return None
    m = TRANSACTION_REF_PATTERN.search(text)
    if not m:
        m = PROOF_FIELD_REF_PATTERN.search(text)
    if not m:
        return None
    raw = re.sub(r'[^A-Za-z0-9]', '', m.group(1))
    if len(raw) >= 7 and sum(ch.isdigit() for ch in raw) >= 4:
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
        field_lines = "\n".join(
            "Phone number:" if item == "phone number" else "Package:" for item in missing
        )
        msg = (
            f"I see your payment code ending **{parsed['code_ending']}**. "
            f"To proceed, please also provide your **{' and '.join(missing)}**.\n\n"
            f"Reply in this format:\n{field_lines}"
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

def _field_value(text, *labels):
    """Value of a 'Label: value' line (used for the Phone number / Package / ... format)."""
    for label in labels:
        m = re.search(rf'^[ \t]*{label}[ \t]*[:\-][ \t]*(.+)$', text or "", re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1).strip()
    return None

def extract_phone_from_message(text):
    val = _field_value(text, r'phone(?:\s*(?:number|no\.?))?', r'mobile(?:\s*number)?', r'whatsapp')
    if val:
        # tolerate "0771 234 567", "077-123-4567", "+263 77 123 4567"
        phone = extract_phone_from_text(re.sub(r'[\s\-().]', '', val))
        if phone:
            return phone
    return extract_phone_from_text(text)

def extract_package_from_message(text):
    val = _field_value(text, r'package', r'bundle', r'plan')
    if val:
        pkg = extract_customer_package(val)
        if pkg:
            return pkg
    return extract_customer_package(text)

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

# ==========================================
# BULK DELETE: delete ALL stocked codes for a whole package in one shot
# (additive feature - does not touch the existing single-code /deletecode flow)
# ==========================================

def _package_slug(pkg_key):
    """Telegram command tokens only allow [a-zA-Z0-9_], so package names like
    '$1:UNL:24HRS' or '$5 = Unlimited 14d' get squashed into a plain alnum slug,
    e.g. '1unl24hrs', '5unlimited14d'. Slugs are derived automatically from
    PACKAGES so this never needs to be kept in sync by hand."""
    return re.sub(r'[^a-z0-9]', '', pkg_key.lower())

PACKAGE_SLUGS = {_package_slug(pkg): pkg for pkg in PACKAGES}

def delete_all_codes_for_package(pkg_key):
    with _inventory_lock:
        removed = list(voucher_inventory.get(pkg_key, []))
        voucher_inventory[pkg_key] = []
    return removed

def list_delcodelist_text():
    lines = []
    for pkg in PACKAGES:
        slug = _package_slug(pkg)
        count = len(voucher_inventory.get(pkg, []))
        lines.append(f"/delcodelist_{slug}  —  {pkg} ({count} code(s) in stock)")
    return ("🗑️ Delete ALL codes for a package at once.\n"
            "Tap a line below to wipe every stocked code for that package:\n\n"
            + "\n".join(lines))

DELCODELIST_RE = re.compile(r'^/delcodelist(?:[_\s]+(\S+))?\s*$', re.IGNORECASE)

def handle_delcodelist_admin_message(text):
    """Returns a reply string if this admin message was a bulk-delete-by-package
    command (/delcodelist or /delcodelist_<slug>), else None so the caller falls
    through to the rest of the normal admin handling, completely untouched."""
    m = DELCODELIST_RE.match(text.strip())
    if not m:
        return None

    arg = m.group(1)
    if not arg:
        return list_delcodelist_text()  # bare "/delcodelist" -> tappable list

    slug = re.sub(r'[^a-z0-9]', '', arg.lower())
    pkg_key = PACKAGE_SLUGS.get(slug) or normalize_package_compact(arg)
    if not pkg_key:
        return (f"⚠️ Unrecognized package '{arg}'.\n\n" + list_delcodelist_text())

    removed = delete_all_codes_for_package(pkg_key)
    if not removed:
        return (f"📭 No codes were in stock for {pkg_key} - nothing to delete.\n\n"
                + list_delcodelist_text())

    return (f"🗑️ Removed {len(removed)} code(s) from {pkg_key}:\n" + ", ".join(removed)
            + "\n\n" + list_delcodelist_text())

YES_WORDS = {"yes", "y", "approve", "approved", "ok", "okay", "confirm", "confirmed"}

# ==========================================
# FULL MEMORY / DATABASE RESET
# (additive, destructive admin-only command - guarded behind an explicit confirm step)
# ==========================================

MEMRESET_RE = re.compile(r'^/memreset(?:[_\s]+(\S+))?\s*$', re.IGNORECASE)
MEMRESET_CONFIRM_TOKEN = "confirm"

def perform_full_memory_reset():
    """Wipes every in-memory store and immediately persists the empty state,
    so the wipe survives a restart instead of being repopulated from the DB
    on next boot."""
    user_memory.clear()
    pending_approvals.clear()
    user_last_message_id.clear()
    customer_chat_id.clear()
    customer_display.clear()
    customer_last_code.clear()
    used_payment_refs.clear()
    intergram_tag_to_key.clear()
    with _proofs_lock:
        registered_proofs.clear()
    with _inventory_lock:
        for pkg in voucher_inventory:
            voucher_inventory[pkg] = []
    with _admin_state_lock:
        admin_awaiting.clear()
    save_state()

def handle_memreset_admin_message(text):
    """Returns a reply string if this admin message was a /memreset command,
    else None so the caller falls through to the rest of the normal admin
    handling, completely untouched."""
    m = MEMRESET_RE.match(text.strip())
    if not m:
        return None

    arg = (m.group(1) or "").strip().lower()
    if arg != MEMRESET_CONFIRM_TOKEN:
        pending_count = len(pending_approvals)
        stock_count = sum(len(c) for c in voucher_inventory.values())
        proof_count = len(registered_proofs)
        return (
            "⚠️ This will PERMANENTLY WIPE the ENTIRE database:\n"
            "- All customer chat histories/memory\n"
            f"- All pending approvals ({pending_count})\n"
            "- All customer identity/reconnect mappings (Intergram tags, chat ids)\n"
            "- Used-payment replay protection (old refs could technically be reused)\n"
            f"- All registered pre-approved proofs of payment ({proof_count})\n"
            f"- ALL voucher stock ({stock_count} code(s) across all packages)\n\n"
            "This CANNOT be undone. Unresolved customer transactions and stocked "
            "voucher codes will be lost.\n\n"
            "To proceed anyway, send:\n/memreset_confirm"
        )

    perform_full_memory_reset()
    return ("✅ Full reset complete. Chat histories, pending approvals, voucher stock, "
            "registered proofs, and identity mappings have all been wiped.")
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
#      The bot stores the amount (1.00) and the FULL approval code
#      (PP260919.2324.T3345746). Its last 7 characters (3345746) are the lookup key
#      shown in messages. The sender name is just a label.
#   2. The customer must paste the FULL proof of payment. The bot auto-approves ONLY if
#         - the last 7 chars of their approval code find a registered proof, AND
#         - their FULL approval code equals the stored one, AND
#         - the amount in their proof equals the registered amount, AND
#         - the chosen package costs exactly that amount, AND
#         - a stocked voucher exists for that package.
#      Anything else falls through to the normal AI + admin YES/NO flow.
#   3. After the voucher is delivered, the registered proof is DELETED and
#      the reference is burned in used_payment_refs (replay protection).
# ==========================================================================

AUTO_APPROVE_ENABLED = os.environ.get("AUTO_APPROVE_ENABLED", "1") != "0"
# Set AUTO_APPROVE_REQUIRE_PHONE=1 if you also want the customer's phone number
# before auto-sending (by default price + reference + package are enough).
AUTO_APPROVE_REQUIRE_PHONE = os.environ.get("AUTO_APPROVE_REQUIRE_PHONE", "0") == "1"

# fingerprint (last 7 alphanumerics, lowercase) -> {"amount", "full_ref", "ref_ending", "sender", "registered_at"}
registered_proofs = {}
_proofs_lock = Lock()

PROOF_MARKER_RE = re.compile(r'approval\s*code|transfer\s*confirmation', re.IGNORECASE)


def normalize_ref(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def proof_fingerprint(ref):
    cleaned = normalize_ref(ref)
    return cleaned[-7:] if len(cleaned) >= 7 else None


def extract_full_reference_from_text(text):
    """The FULL approval code with punctuation stripped, e.g. 'PP2609191817T1705184'."""
    if not text:
        return None
    m = TRANSACTION_REF_PATTERN.search(text)
    if not m:
        return None
    raw = re.sub(r'[^A-Za-z0-9]', '', m.group(1))
    return raw if len(raw) >= 7 else None


def extract_core_reference(text):
    """The STABLE trailing segment of an approval code - e.g. 'T2514748' out of
    'PP260926.0931.T2514748' - which is what auto-approval should actually compare on.

    EcoCash-style approval codes are commonly formatted as PP<date>.<time>.T<digits>.
    The 'PP<date>.<time>' portion reflects when THAT PARTICULAR SMS notification was
    generated, and can legitimately differ by a few minutes between the sender's and
    receiver's copies of the exact same transfer (e.g. 'PP260926.0925.T2514748' vs
    'PP260926.0931.T2514748') - it is NOT a stable transaction attribute. The trailing
    '.T<digits>' segment is the part that stays identical between both copies, so that's
    the segment used for matching (not the whole code).

    Splits on '.' or '-' and takes the last segment. Falls back to the last 7 alphanumeric
    characters of the whole reference if there's no separator to split on."""
    if not text:
        return None
    m = TRANSACTION_REF_PATTERN.search(text)
    if not m:
        return None
    raw = m.group(1).strip(" -\u2014.")
    seg = re.split(r'[.\-]', raw)[-1]
    core = re.sub(r'[^A-Za-z0-9]', '', seg)
    if len(core) >= 4:
        return core.upper()
    whole = re.sub(r'[^A-Za-z0-9]', '', raw)
    return whole[-7:].upper() if len(whole) >= 7 else None


def proof_is_incomplete(pending, text):
    """True when the customer gave a code but NOT the full proof of payment.
    A full proof = the FULL approval code plus either a USD amount or the confirmation text
    itself (so a proof paid in another currency, e.g. 'ZWG 25.00', is still accepted and goes
    to the admin for a manual decision instead of being asked for again forever)."""
    now_ref = normalize_ref(extract_full_reference_from_text(text))
    has_amount_or_heading = bool(extract_payment_amount(text)) or bool(
        re.search(r'confirmation', text or "", re.IGNORECASE))
    if len(now_ref) > 7 and has_amount_or_heading:
        return False
    # ...or the full proof was already pasted earlier in this same transaction.
    earlier_ok = (len(normalize_ref((pending or {}).get("claimed_ref"))) > 7
                  and bool((pending or {}).get("claimed_amount")))
    return not earlier_ok


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


def _party_text(p):
    """' from NAME' / ' to NAME' display label for a registered proof (or '')."""
    if not p.get("sender"):
        return ""
    return f" {p.get('direction') or 'from'} {p['sender']}"


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
        # The receiver's SMS says "USD 1.00 from NAME", the sender's says "USD 0.50 sent to NAME".
        # Either way the name is just a display label - it is never used for matching.
        party_m = re.search(r'\b(from|sent\s+to)\s+([^\n.]+)', block, re.IGNORECASE)
        proofs.append({
            "amount": amount,
            "full_ref": full_ref,                        # kept for display/back-compat, not for matching
            "core_ref": extract_core_reference(block),   # stable "T<digits>"-style segment - compared against the customer's proof
            "ref_ending": normalize_ref(full_ref)[-7:],  # lookup key / what admin messages show
            "sender": party_m.group(2).strip() if party_m else "",
            "direction": "to" if (party_m and party_m.group(1).lower().startswith("sent")) else "from",
            "registered_at": time.time(),
        })
    return proofs


def find_probable_match(code_ending):
    """Given a customer's code ending, look up a registered proof with the same
    fingerprint. This is advisory ONLY - it is never used to auto-approve or
    auto-send a voucher. It exists purely to save the admin a lookup when
    deciding whether to reply YES to a pending request."""
    fp = proof_fingerprint(code_ending)
    if not fp:
        return None, None
    with _proofs_lock:
        return fp, registered_proofs.get(fp)


def diagnose_mismatch(pending_info, proof):
    """Returns a short, specific, human-readable reason a registered proof's fingerprint
    matched but try_auto_approve() still refused to auto-send - so the admin doesn't have
    to go dig through server logs to find out why. Checked in the same order
    try_auto_approve() itself checks them."""
    pending_info = pending_info or {}

    claimed_ref = normalize_ref(pending_info.get("claimed_ref"))
    claimed_amount = pending_info.get("claimed_amount")
    if len(claimed_ref) <= 7 or not claimed_amount:
        return "customer has not pasted the FULL proof of payment yet (only a partial reference)"

    claimed_core = (pending_info.get("claimed_core") or "").upper()
    reg_core = (proof.get("core_ref") or "").upper()
    if reg_core and claimed_core and claimed_core != reg_core:
        return (f"full reference differs beyond the shared last 7 characters "
                f"(customer's: ...{claimed_core}, registered: ...{reg_core})")

    reg_amount = proof.get("amount")
    if claimed_amount != reg_amount:
        return f"amount differs (customer's proof: ${claimed_amount}, registered: ${reg_amount})"

    return "reference/amount could not be fully verified"


def probable_match_note(code_ending, pending_info=None):
    """One-line note to append to an admin alert/reminder if a registered proof
    shares this code ending's fingerprint. Returns '' if there's no candidate.
    When pending_info (the customer's pending_approvals record) is supplied, the
    note names the SPECIFIC reason auto-approval didn't fire instead of a generic
    "full code or amount didn't line up" - see diagnose_mismatch()."""
    fp, proof = find_probable_match(code_ending)
    if not proof:
        return ""
    reason = diagnose_mismatch(pending_info, proof)
    return (
        f"\n\n💡 Possible match on file: ${proof['amount']}{_party_text(proof)}, "
        f"ref ending {fp} (registered {int(time.time() - proof['registered_at'])}s ago). "
        f"This did NOT auto-approve ({reason}) - please compare "
        "against what the customer sent before replying YES.\n"
        f"🗑️ Not a match? Tap to remove it: /matchreject_{fp}"
    )


# --- Bare, unlabeled digit-only messages (e.g. a customer just sending "3928909" with no
# "Approval Code:" / "Ref:" label at all) are NOT treated as a confirmed transaction
# reference - extract_code_ending_from_text still requires a label for that, and nothing
# here changes that. This is a SEPARATE, strictly advisory feature: if such a bare number
# happens to share a fingerprint with something the admin already registered, we give the
# admin an early heads-up so they can follow up proactively. It never creates a
# pending_approvals entry, never sets alert_sent, and never triggers try_auto_approve - the
# customer still has to paste the FULL proof of payment for anything to actually happen.
BARE_DIGIT_RE = re.compile(r'^[\s.\-:,]*(\d{7,15})[\s.\-:,]*$')
BARE_DIGIT_NOTIFY_COOLDOWN_SECONDS = 60
_bare_digit_last_notify = {}

def extract_bare_digit_candidate(text):
    if not text:
        return None
    m = BARE_DIGIT_RE.match(text.strip())
    return m.group(1) if m else None

def maybe_notify_admin_of_bare_digit_match(customer_key, bare_digits, label):
    """Read-only, advisory ONLY. Returns True if a heads-up was sent."""
    fp, proof = find_probable_match(bare_digits)
    if not proof:
        return False  # nothing registered with this fingerprint - nothing to say
    now = time.time()
    if now - _bare_digit_last_notify.get(customer_key, 0) < BARE_DIGIT_NOTIFY_COOLDOWN_SECONDS:
        return False  # already flagged recently, don't spam the admin
    _bare_digit_last_notify[customer_key] = now
    if admin_chat_id:
        try:
            admin_bot.send_message(
                admin_chat_id,
                f"👀 Heads up: {label} sent a bare number ending in {fp}, which matches a "
                f"registered proof on file (${proof['amount']}{_party_text(proof)}).\n"
                "They have NOT sent the full proof of payment yet, so nothing has been "
                "approved or reserved - purely an early FYI in case you want to follow up.\n"
                f"🗑️ Not a match? /matchreject_{fp}"
            )
        except Exception as e:
            print(f"[BARE-DIGIT] Could not notify admin: {e}")
    return True


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

    # --- verification: the customer must have pasted the FULL proof of payment ---
    claimed_ref = normalize_ref(info.get("claimed_ref"))
    claimed_amount = info.get("claimed_amount")
    if len(claimed_ref) <= 7 or not claimed_amount:
        return False  # partial proof (e.g. only the last digits) is never auto-approved

    # --- verification: the CORE transaction-id segment must match ---------------
    # Compares on the stable "T<digits>"-style trailing segment of the approval code
    # (e.g. "T2514748" out of "PP260926.0931.T2514748"), NOT the whole code. The
    # leading "PP<date>.<time>" portion is a per-notification timestamp that can
    # legitimately differ by a few minutes between the sender's and receiver's SMS for
    # the exact same transfer, so requiring a byte-for-byte match on the whole string
    # was rejecting genuine matches (Prince flagged this). The last-7-characters
    # fingerprint is only used above to find the candidate record in the first place;
    # this is the extra check on top of that.
    claimed_core = (info.get("claimed_core") or "").upper()
    reg_core = (proof.get("core_ref") or "").upper()
    if reg_core and claimed_core and claimed_core != reg_core:
        print(f"[AUTO] {customer_key}: core reference mismatch ({claimed_core} vs "
              f"{reg_core}, same last-7) - manual flow.")
        return False

    # --- verification: price ------------------------------------------------
    if claimed_amount != proof["amount"]:
        print(f"[AUTO] {customer_key}: amount mismatch (claimed {claimed_amount} vs "
              f"registered {proof['amount']}) - manual flow.")
        return False

    if AUTO_APPROVE_REQUIRE_PHONE and not info.get("phone"):
        return False

    # --- verification: package must exist and cost exactly what was paid ---
    #
    # A customer must NEVER be handed a package that costs MORE than what they actually
    # paid (that's the business extending free credit / the customer "owing" the
    # difference), and never LESS either (that would shortchange them). So if their
    # requested package's price doesn't exactly equal what the registered proof says they
    # paid, we don't deliver it as-is. Instead: if the amount they paid maps to exactly ONE
    # package (PRICE_TO_PACKAGES), we reassign to THAT package automatically - the customer
    # still gets a real voucher immediately, it's just guaranteed to be the one that
    # actually matches what they paid, rather than what they happened to type. If the paid
    # amount is ambiguous (shared by more than one package) or matches nothing at all, we
    # still refuse to guess and fall back to manual review, exactly as before.
    pkg_key = info.get("package_key")
    reassigned_from = None
    price_matches_requested = False
    if pkg_key and pkg_key in PACKAGES:
        try:
            requested_price = f"{float(PACKAGES[pkg_key]['price'].replace('$', '')):.2f}"
            price_matches_requested = (requested_price == proof["amount"])
        except ValueError:
            price_matches_requested = False

    if not price_matches_requested:
        matches = PRICE_TO_PACKAGES.get(proof["amount"], [])
        matched_pkg = matches[0] if len(matches) == 1 else None
        if not matched_pkg:
            print(f"[AUTO] {customer_key}: paid {proof['amount']} but chose "
                  f"{pkg_key or '(none)'} - no unambiguous package matches that amount - manual flow.")
            return False
        if pkg_key and pkg_key != matched_pkg:
            reassigned_from = pkg_key
        pkg_key = matched_pkg

    if not pkg_key or pkg_key not in PACKAGES:
        return False

    chat_id = customer_chat_id.get(customer_key)
    if chat_id is None:
        return False

    # If a voucher was already reserved for the customer's ORIGINAL (mismatched) package
    # choice, it belongs to a different package's stock now that we're reassigning - return
    # it rather than handing it out under the wrong package.
    if reassigned_from and info.get("proposed_code"):
        return_voucher(info.get("package_key"), info["proposed_code"])
        info["proposed_code"] = None

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
        + (f"Note: your payment of ${proof['amount']} matches the {pkg_key} package, not the "
           f"{reassigned_from} package you originally asked for - we've issued the package "
           f"that matches what you actually paid.\n\n" if reassigned_from else "")
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
                + (f" ({_party_text(proof).strip()})" if proof.get("sender") else "") + "\n"
                f"Package: {pkg_key}"
                + (f" (customer asked for {reassigned_from}; reassigned to match the amount paid)"
                   if reassigned_from else "") + "\n"
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
        fp = proof_fingerprint(p["ref_ending"])
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

        who = _party_text(p)
        line = f"✅ {'Updated' if replaced else 'Registered'}: ${p['amount']}{who}, ref ending {fp}."
        if served:
            line += f" A waiting customer ({', '.join(served)}) was auto-approved right away."
        else:
            line += (" Will auto-approve when a customer sends a matching proof."
                     f"\n🗑️ Wrong one? Tap to remove: /delproof_{fp}")
        lines.append(line)
    return "\n".join(lines)


def list_proofs_text():
    with _proofs_lock:
        items = list(registered_proofs.items())
    if not items:
        return "📭 No registered proofs waiting."
    # Telegram only makes the command token tappable, and a space ends it - so the ref is
    # joined with an underscore (/delproof_3877005): one tap sends the whole thing.
    lines = [f"/delproof_{fp}  —  ${p['amount']}{_party_text(p)}" for fp, p in items]
    return ("🧾 Registered proofs (auto-approve on match).\n"
            "Tap a line to DELETE that proof:\n\n" + "\n".join(lines))


def handle_proof_admin_message(text):
    """Returns a reply string if this admin message was proof-related, else None."""
    low = text.lower().strip()
    if low in ('/proofs', 'proofs'):
        return list_proofs_text()

    # /delproof_<fp> and /matchreject_<fp> are aliases - both delete a registered
    # proof by its fingerprint. matchreject exists so the tappable link in a
    # "possible match" suggestion note reads honestly (rejecting a suggested
    # match, not browsing/deleting from the full /proofs list).
    m = re.match(r'^/(?:delproof|matchreject)(?:[_\s]+(\S+))?\s*$', text.strip(), re.IGNORECASE)
    if m:
        arg = m.group(1)
        if not arg:
            return list_proofs_text()          # bare "/delproof" -> tappable list
        fp = proof_fingerprint(arg)
        if not fp:
            return ("⚠️ That isn't a valid reference (need at least 7 characters).\n\n"
                    + list_proofs_text())
        with _proofs_lock:
            removed = registered_proofs.pop(fp, None)
        if not removed:
            return f"⚠️ No registered proof ending {fp} (already used or deleted).\n\n" + list_proofs_text()
        reply = f"🗑️ Removed proof ending {fp} (${removed.get('amount', '?')}{_party_text(removed)})."
        with _proofs_lock:
            remaining = bool(registered_proofs)
        return reply + ("\n\n" + list_proofs_text() if remaining else "\n\nNo registered proofs left.")

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
# Intergram prefixes every forwarded message with a short per-visitor tag, e.g.
# "y6ysyx: Hi" - everything up to the first ":" is the tag/username, everything
# after it is the actual customer message.
#
# Per Prince: this tag is emitted at the very start of every forwarded message,
# up to the first ":", and it changes together with chat_id whenever the
# customer leaves and reopens the widget chat - i.e. it tracks the SAME session
# boundary as chat_id itself. Because of that it is checked FIRST below (it's
# the most specific signal we have for "this is the same visitor session"), with
# phone number as the fallback when no tag match is found - phone is what
# actually survives a genuine session reset, since a brand-new session always
# produces a brand-new, never-before-seen tag.
#
# extract_intergram_tag() below splits every incoming message into (tag,
# message_without_tag) so downstream regexes (phone/reference/package
# extraction) always see clean customer text either way.

REBUMP_COOLDOWN_SECONDS = 15  # just enough to absorb accidental double-sends/webhook retries,
                               # not to make an impatient customer wait for a re-alert

FOLLOWUP_STATUS_RE = re.compile(
    r'\b(where.?s?\s+my\s+code|any\s+update|did\s+you\s+(get|receive)|status|'
    r'still\s+waiting|already\s+paid|already\s+sent|didn.?t\s+get|'
    r'haven.?t\s+received|resend|re-?send|send\s+(it|the\s+code|my\s+code)|'
    r'waiting\s+for\s+(my\s+)?code|hello|hi|helo)\b',
    re.IGNORECASE
)

# ASSUMPTION based on the two examples you gave ("cdgrmv:", "y6ysyx:", "tzb3bi:"): the tag
# is exactly 6 lowercase letters/digits, followed by ":" and then the message. If your
# Intergram config uses a different length or includes uppercase, adjust {6} and the
# character class below to match - check a few real forwarded messages to confirm the format.
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
      1. This exact chat_customer_key already has an open record - nothing to
         reconcile.
      2. Intergram's per-visitor tag (e.g. "tzb3bi:") matching a key we've
         already seen that tag paired with. Checked FIRST per Prince's
         instruction - the tag is the most specific same-session signal we
         have. NOTE: it changes together with chat_id on a genuine session
         reset, so it can only ever match within a session we've already
         recorded the tag for (e.g. a retried/duplicate webhook delivery, or
         a message arriving before we'd finished routing an earlier one) -
         it will NOT bridge an actual reset. Phone (below) is what bridges
         a real reset.
      3. A phone number in the message matching an open pending approval -
         the signal that actually survives a widget/session reset, since a
         new session always produces a brand-new, never-seen tag.
      4. Exactly ONE open, already-alerted transaction system-wide and this
         message reads like a status follow-up (last-resort, single-customer
         only - never guessed when more than one customer is waiting, since a
         wrong guess would leak/misroute someone else's transaction).

    Returns (canonical_key, cleaned_text).
    """
    tag, cleaned_text = extract_intergram_tag(raw_text)

    if chat_customer_key in pending_approvals or chat_customer_key in user_memory:
        if tag:
            intergram_tag_to_key[tag] = chat_customer_key
        return chat_customer_key, cleaned_text

    old_key = None

    # Priority 1: Intergram tag match.
    if tag:
        known_key = intergram_tag_to_key.get(tag)
        if known_key and known_key != chat_customer_key and (
            known_key in pending_approvals or known_key in user_memory
        ):
            old_key = known_key

    # Priority 2: phone number match (survives a genuine session reset, unlike the tag).
    if not old_key:
        phone = extract_phone_from_text(cleaned_text)
        old_key = find_pending_by_phone(phone) if phone else None

    # Priority 3: single open + already-alerted transaction, follow-up wording.
    if not old_key and len(pending_approvals) == 1 and FOLLOWUP_STATUS_RE.search(cleaned_text or ""):
        only_key, only_info = next(iter(pending_approvals.items()))
        if only_info.get("alert_sent"):
            old_key = only_key

    if tag:
        # Remember this tag -> key pairing regardless of whether it helped just
        # now; costs nothing and might help on some future message.
        intergram_tag_to_key[tag] = chat_customer_key

    if not old_key or old_key == chat_customer_key:
        return chat_customer_key, cleaned_text

    _repoint_customer_routing(message, old_key)
    save_state()
    return old_key, cleaned_text


# ------------------------------------------
# STOCK RESERVATION FOR WAITING REQUESTS
# ------------------------------------------
def _pending_package_key(info):
    pk = info.get("package_key")
    if pk in PACKAGES:
        return pk
    return normalize_package_compact(info.get("package")) if info.get("package") else None

def reserve_stock_for_waiting(only_package=None, notify=True):
    """Give every already-alerted request that has NO reserved code a code from stock, if
    any is available now. Call after stock is added, and before reminders/decisions.
    Returns the number of requests that got a code."""
    assigned = 0
    for ckey, info in list(pending_approvals.items()):
        if not info.get("alert_sent") or info.get("proposed_code"):
            continue
        pk = _pending_package_key(info)
        if not pk or (only_package and pk != only_package):
            continue
        code = reserve_voucher(pk)
        if not code:
            continue
        info["proposed_code"] = code
        info["package_key"] = pk
        assigned += 1
        if notify and admin_chat_id:
            try:
                admin_bot.send_message(
                    admin_chat_id,
                    f"📦 Stock is now available for {customer_display.get(ckey, ckey)} "
                    f"(code ending {info.get('code_ending')}, {pk}).\n"
                    f"Stored voucher reserved: {code}\n"
                    "Reply YES to send it to the customer, or NO to hold it and provide a different code."
                    + probable_match_note(info.get("code_ending"), info)
                )
            except Exception as e:
                print(f"[STOCK] Could not notify admin: {e}")
    if assigned:
        save_state()
    return assigned


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

    # Stock may have been added since the original alert - reserve it first.
    if not info.get("proposed_code") and reserve_stock_for_waiting(_pending_package_key(info)):
        info["last_alert_time"] = now
        return True   # the helper already sent the "stock is now available" message

    note = probable_match_note(info.get("code_ending"), info)

    if admin_chat_id:
        if info.get("proposed_code"):
            admin_bot.send_message(
                admin_chat_id,
                f"⏰ Reminder: {label} is still waiting on their voucher "
                f"(code ending {info.get('code_ending')}, {info.get('package')}, "
                f"phone {info.get('phone')}). A stored code is already reserved for them "
                f"('{info['proposed_code']}') — reply YES to send it, or NO to hold it."
                + note
            )
        else:
            admin_bot.send_message(
                admin_chat_id,
                f"⏰ Reminder: {label} is still waiting on a decision "
                f"(code ending {info.get('code_ending')}, {info.get('package')}, "
                f"phone {info.get('phone')}). No stock code is reserved yet — reply with a "
                f"voucher code to approve, or 'no' to reject."
                + note
            )
    info["last_alert_time"] = now
    save_state()
    return True


# ==========================================================================
# SMART PAYMENT FAST-PATH (no phone/package interrogation)
# ==========================================================================
PHONE_ASK_AFTER_SECONDS = int(os.environ.get("PHONE_ASK_AFTER_SECONDS", "180"))  # 0 = never ask proactively

FRUSTRATION_RE = re.compile(
    r"(where.?s?\s+my|still\s+waiting|how\s+long|taking\s+(so\s+|too\s+)?long|too\s+long|"
    r"waiting\s+(for\s+)?(so\s+|too\s+)?long|hurry|urgent|any\s+update|not\s+yet|"
    r"\?{2,}|!{2,}|scam|fake|cheat|refund|useless|wasting)",
    re.IGNORECASE
)

_REF_TOKEN_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9.\-]{6,}')

def extract_registered_bare_ref(text):
    """Customer pasted just the last digits / bare code (no label). Only trusted when its
    last 7 characters match a proof the admin (or SMS webhook) already registered."""
    if not text:
        return None
    for tok in _REF_TOKEN_RE.findall(text):
        if PHONE_PATTERN.fullmatch(tok):
            continue  # a phone number, not a reference
        if sum(ch.isdigit() for ch in normalize_ref(tok)) < 4:
            continue
        fp = proof_fingerprint(tok)
        if fp:
            with _proofs_lock:
                if fp in registered_proofs:
                    return fp
    return None

def _match_candidate_by_data(text, candidates):
    """When the price alone is ambiguous between 2+ packages, try to settle it using the
    DATA amount/type the customer mentioned (e.g. '12gb', '5 gb', 'unlimited') - but ONLY
    within the already price-filtered candidate list, so this can never assign a package
    the customer didn't actually pay for. Returns a package key only when exactly one
    candidate's data field is referenced in the text; otherwise None (=> still ask)."""
    if not text or not candidates:
        return None
    t = text.lower()
    matches = []
    for pkg in candidates:
        data = PACKAGES[pkg]["data"].lower()  # e.g. "unlimited", "5gb", "12gb"
        if data == "unlimited":
            if re.search(r'\bunlimit', t):
                matches.append(pkg)
        else:
            gb_num = re.sub(r'[^0-9]', '', data)
            if gb_num and re.search(rf'\b{gb_num}\s*gb\b', t):
                matches.append(pkg)
    return matches[0] if len(matches) == 1 else None

def choose_package(amount, explicit_pkg, raw_text=None):
    """Package from the amount paid. Ambiguous amounts use the customer's own choice if it is
    one of the candidates, then try matching the data amount/type they mentioned (e.g. '12gb'),
    otherwise None (=> ask)."""
    candidates = PRICE_TO_PACKAGES.get(amount or "", [])
    if len(candidates) == 1:
        return candidates[0], candidates
    if len(candidates) > 1:
        if explicit_pkg in candidates:
            return explicit_pkg, candidates
        data_match = _match_candidate_by_data(raw_text, candidates)
        if data_match:
            return data_match, candidates
        return None, candidates
    return (explicit_pkg if explicit_pkg in PACKAGES else None), list(PACKAGES)

def package_question(amount, candidates):
    opts = "\n".join(f"- {p} ({PACKAGES[p]['data']})" for p in candidates)
    if amount and len(candidates) < len(PACKAGES):
        head = f"Your payment of ${amount} matches more than one package:"
    else:
        head = "Which package would you like?"
    return (f"{head}\n{opts}\n\nPlease reply with just the package name "
            "(or simply the amount of data, e.g. '12GB' or 'unlimited').")

def _send_reply(message, customer_key, out_text, user_text=None):
    out_text = ensure_closing_warning(out_text)
    hist = user_memory.setdefault(customer_key, [{"role": "system", "content": system_rules}])
    if user_text is not None:
        hist.append({"role": "user", "content": user_text})
    hist.append({"role": "assistant", "content": out_text})
    if len(hist) > 15:
        user_memory[customer_key] = [hist[0]] + hist[-14:]
    customer_bot.reply_to(message, out_text)
    save_state()

def needs_phone(info, text):
    if info.get("phone"):
        return False
    t0 = info.get("alert_time") or info.get("last_alert_time") or time.time()
    waited_long = PHONE_ASK_AFTER_SECONDS > 0 and (time.time() - t0) >= PHONE_ASK_AFTER_SECONDS
    return waited_long or bool(FRUSTRATION_RE.search(text or ""))

def reply_pending_status(message, customer_key, text):
    """Customer is following up on an already-alerted transaction. No LLM involved."""
    info = pending_approvals.get(customer_key)
    if not info or not info.get("alert_sent"):
        return False
    if try_auto_approve(customer_key):   # proof may have been registered since
        return True
    label = customer_display.get(customer_key, customer_key)

    phone = extract_phone_from_message(text)
    if phone:
        info["phone"] = phone
        info["phone_confirmed"] = True
        customer_last_phone[customer_key] = phone
        if admin_chat_id:
            try:
                admin_bot.send_message(
                    admin_chat_id,
                    f"📞 {label} provided their phone number: {phone}\n"
                    f"(code ending {info.get('code_ending')}, {info.get('package')})"
                )
            except Exception as e:
                print(f"[PHONE] Could not notify admin: {e}")
        _send_reply(message, customer_key,
                    "Thank you - we've passed your phone number to our team. "
                    "You'll get your voucher as soon as the payment is approved.", text)
        return True

    maybe_bump_admin_for_pending(customer_key, text, label)
    reply = ("Your payment is still with our team for verification — we haven't forgotten you. "
             "You'll get your voucher code the moment it's approved.")
    if needs_phone(info, text):
        info["phone_asked"] = time.time()
        reply += ("\n\nTo help us speed this up, please also send your phone number "
                  "(e.g. 0776248396).")
    _send_reply(message, customer_key, reply, text)
    return True

def alert_admin_for_pending(message, customer_key, pkg, amount, proof, partial, text):
    info = pending_approvals[customer_key]
    if not admin_chat_id:
        _send_reply(message, customer_key,
                    "⚠️ SYSTEM NOTIFICATION: The Admin Bot is currently unlinked. "
                    "(Admin: Please send /start to the Admin bot to reconnect routing).", text)
        return True

    label = customer_display.get(customer_key, str(customer_key))
    if info.get("proposed_code"):
        return_voucher(info.get("package_key"), info["proposed_code"])
    reserved = reserve_voucher(pkg)
    now = time.time()
    info.update({
        "package": pkg, "package_key": pkg,
        "price": f"${amount}" if amount else PACKAGES[pkg]["price"],
        "proposed_code": reserved, "alert_sent": True,
        "package_confirmed": True, "alert_time": now, "last_alert_time": now,
    })

    ending = info["code_ending"]
    fp = proof_fingerprint(ending)
    if proof and partial:
        basis = (f"✅ Matches registered proof: ${proof['amount']}{_party_text(proof)}, ref ending {fp}.\n"
                 "Customer sent ONLY the last digits (not the full proof).\n")
        note = f"\n🗑️ Not a match? /matchreject_{fp}"
    elif proof:
        basis, note = "", probable_match_note(ending, info)
    else:
        basis, note = "⚠️ No registered proof on file for this reference - verify manually.\n", ""

    if reserved:
        head = "🔔 PAYMENT APPROVAL — STOCK CODE AVAILABLE"
        tail = (f"Stored voucher found: {reserved}\n"
                "Reply YES to send it to the customer, or NO to hold it and provide a different code.")
    else:
        head = "🔔 NEW PAYMENT APPROVAL REQUEST — OUT OF STOCK"
        tail = "No stored code for this package. Reply with a new voucher code to approve, or 'no' to reject."

    phone_line = info.get("phone") or ("requested from customer" if not proof else "not provided")
    admin_msg = (f"{head}\nCustomer: {label}\n{basis}Code ending: {ending}\n"
                 f"Price: {info['price']}\nPhone: {phone_line}\n"
                 f"Package: {pkg}\n\n{tail}{note}")
    try:
        admin_bot.send_message(admin_chat_id, admin_msg)
    except Exception as e:
        print(f"[ALERT] Could not notify admin: {e}")

    reply = WAIT_MESSAGE
    # No saved proof to verify against -> ask for the phone number right away
    # (unless they already included a valid one in their message).
    if not proof and not info.get("phone"):
        info["phone_asked"] = time.time()   # stops the watchdog from asking a second time
        reply += ("\n\nSince we're verifying this payment manually, please also send your "
                  "phone number (e.g. 0776248396).")
    _send_reply(message, customer_key, reply, text)
    return True

def advance_pending(message, customer_key, text):
    """Deterministic payment handling. Returns True if a reply was sent."""
    info = pending_approvals.get(customer_key)
    if not info:
        return False
    if info.get("alert_sent"):
        return reply_pending_status(message, customer_key, text)

    fp = proof_fingerprint(info.get("code_ending"))
    if not fp:
        return False

    if fp in used_payment_refs:
        pending_approvals.pop(customer_key, None)
        if admin_chat_id:
            try:
                admin_bot.send_message(
                    admin_chat_id,
                    f"⚠️ DUPLICATE payment proof from {customer_display.get(customer_key, customer_key)} "
                    f"(ref ending {fp}). A voucher was already issued for it.")
            except Exception:
                pass
        _send_reply(message, customer_key, DUPLICATE_MESSAGE, text)
        return True

    if try_auto_approve(customer_key):      # full proof + matching saved proof -> done
        return True

    with _proofs_lock:
        proof = registered_proofs.get(fp)
    partial = proof_is_incomplete(info, text)

    if partial and not proof:
        _send_reply(message, customer_key, NEED_FULL_PROOF_MESSAGE, text)
        return True

    amount = proof["amount"] if proof else info.get("claimed_amount")
    pkg, candidates = choose_package(amount, info.get("package_key"), text)
    if not pkg:
        info["package"] = None
        info["package_key"] = None
        _send_reply(message, customer_key, package_question(amount, candidates), text)
        return True

    return alert_admin_for_pending(message, customer_key, pkg, amount, proof, partial, text)

def run_phone_watchdog():
    """If approval drags on, ask for the phone number once, without waiting for the customer."""
    while True:
        time.sleep(20)
        try:
            if PHONE_ASK_AFTER_SECONDS <= 0:
                continue
            now = time.time()
            changed = False
            for key, info in list(pending_approvals.items()):
                if not info.get("alert_sent") or info.get("phone") or info.get("phone_asked"):
                    continue
                t0 = info.get("alert_time") or info.get("last_alert_time")
                chat_id = customer_chat_id.get(key)
                if not t0 or now - t0 < PHONE_ASK_AFTER_SECONDS or chat_id is None:
                    continue
                info["phone_asked"] = now
                changed = True
                msg = ensure_closing_warning(
                    "Sorry for the wait - your payment is still being verified. To help us speed "
                    "things up, please send your phone number (e.g. 0776248396).")
                try:
                    customer_bot.send_message(chat_id, msg)
                    user_memory.setdefault(key, [{"role": "system", "content": system_rules}]) \
                               .append({"role": "assistant", "content": msg})
                except Exception as e:
                    print(f"[WATCHDOG] Could not message {key}: {e}")
            if changed:
                save_state()
        except Exception as e:
            print(f"[WATCHDOG] Error: {e}")


# ==========================================
# CUSTOMER BOT HANDLER
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
    if not extracted_ref:
        extracted_ref = extract_registered_bare_ref(text)   # last 7 digits matching a saved proof
    extracted_phone = extract_phone_from_message(text)
    extracted_pkg = extract_package_from_message(text)

    # Advisory-only: a bare digit-only message (no label at all, so it is NOT a confirmed
    # reference) may still be worth flagging to the admin if it happens to match something
    # already registered. This never creates pending_approvals state and never approves
    # anything - the customer conversation below proceeds completely unaffected.
    if not extracted_ref and not extracted_phone and not extracted_pkg:
        bare_digits = extract_bare_digit_candidate(text)
        if bare_digits:
            maybe_notify_admin_of_bare_digit_match(
                customer_key, bare_digits, customer_display.get(customer_key, customer_key)
            )

    # Remember any genuinely-extracted phone number immediately, even if no payment
    # reference has arrived yet - otherwise a phone given in an earlier, separate message
    # is invisible to the reply-guard below by the time the customer later sends proof.
    if extracted_phone:
        customer_last_phone[customer_key] = extracted_phone

    # Make sure conversation memory exists before we possibly inject a system note below.
    if customer_key not in user_memory:
        user_memory[customer_key] = [{"role": "system", "content": system_rules}]

    # "Where do I pay / what's the EcoCash number?" -> fixed, deterministic answer (no AI).
    # Never fires when the message contains a real proof (Approval Code / Transfer Confirmation).
    if not extracted_ref and asks_where_to_pay(text):
        _send_reply(message, customer_key, PAYMENT_INFO_MESSAGE, text)
        return

    # If this is just a status follow-up on an already-open, already-alerted
    # transaction (no fresh reference this turn), answer from real state and
    # skip the LLM entirely - guarantees no fabricated status, bumps the admin at
    # most once per cooldown window, and asks for a phone number if the wait drags on.
    if not extracted_ref:
        if reply_pending_status(message, customer_key, text):
            return

    # Deterministic welcome: always shows the one-message format (no AI involved).
    if text.strip().lower() in ("/start", "start"):
        welcome = ensure_closing_warning(build_welcome_message())
        user_memory[customer_key].append({"role": "assistant", "content": welcome})
        customer_bot.reply_to(message, welcome)
        save_state()
        return

    if extracted_ref:
        customer_last_code[customer_key] = extracted_ref
        current_pending = pending_approvals.get(customer_key)
        # If approval code changed or is new, force reset phone/package slots
        if not current_pending or current_pending.get("code_ending") != extracted_ref:
            if current_pending and current_pending.get("proposed_code"):
                return_voucher(current_pending.get("package_key"), current_pending["proposed_code"])
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
                # The amount and FULL approval code shown in the CUSTOMER's pasted proof -
                # checked against the admin-registered proof for auto-approval. (The last 7
                # characters, code_ending, are what identifies a payment everywhere else.)
                "claimed_amount": extract_payment_amount(text),
                "claimed_ref": extract_full_reference_from_text(text),
                "claimed_core": extract_core_reference(text),
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
            if extract_full_reference_from_text(text):
                current_pending["claimed_ref"] = extract_full_reference_from_text(text)
            if extract_core_reference(text):
                current_pending["claimed_core"] = extract_core_reference(text)
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

    # Payment fast-path: auto-approve on full proof, or alert admin (package chosen by price).
    # Also run this whenever the customer already has an open, not-yet-alerted payment request
    # waiting on a missing slot (almost always just the package) - even if THIS message didn't
    # parse into a phone/package/ref on its own. Without this, an unrecognized reply (a stray
    # "12GB valid fo" cut short, or Intergram auto-forwarding the visitor's display name as its
    # own message) falls through to the free-form AI, which can go off-script - e.g. asking for
    # a phone number that isn't actually needed yet, or "confirming" a package it never actually
    # saved to state, forcing the customer to repeat themselves next turn.
    awaiting_slot = (
        customer_key in pending_approvals
        and not pending_approvals[customer_key].get("alert_sent")
    )
    if extracted_ref or extracted_pkg or extracted_phone or awaiting_slot:
        if advance_pending(message, customer_key, text):
            return

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

        # HARD GATE (Python-enforced, not prompt-enforced): an [ADMIN_ALERT] is only ever
        # honored when this customer has a DETERMINISTICALLY verified transaction to point
        # to - either a reference our own regex extracted THIS turn, or an already-open
        # pending_approvals record created from a genuine extraction on an earlier turn.
        # If neither exists, the alert (and any code/phone/package inside it) is pure model
        # output with zero Python-side evidence behind it, so it's discarded outright rather
        # than parsed. This closes the gap where a bare, unlabeled string with no "approval
        # code"/"transaction id" label (e.g. a customer pasting only "3928906") could
        # otherwise let a hallucinated ADMIN_ALERT reach the admin or get compared against
        # registered_proofs, without ever passing through extract_code_ending_from_text.
        has_genuine_basis = bool(extracted_ref) or bool(pending_approvals.get(customer_key))

        if alerts and not has_genuine_basis:
            print(f"[GUARD] Discarded {len(alerts)} AI-generated [ADMIN_ALERT] block(s) for "
                  f"{customer_key}: no genuine extracted reference or open pending transaction "
                  "this turn - refusing to trust model-only code/phone/package.")
            alerts = []

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
            clean_reply = "⚠️ That approval code is too short. Please paste the FULL proof of payment - the entire EcoCash confirmation message."
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
        # Same principle, applied to phone numbers: the model is only allowed to confirm/echo
        # a phone number back to the customer if Python's own regex actually extracted a valid
        # one THIS turn, or an earlier turn already gave us one for this customer. Two checks:
        # (1) a label-based scan ("phone number" + a nearby digit run, however malformed) -
        #     catches things like the model inventing "phone number 392-8906" out of a bare
        #     7-digit input; and (2) a phrasing-INDEPENDENT scan for anything shaped like a
        #     real, well-formed number anywhere in the reply, compared against the one number
        #     we've actually verified - catches the model omitting the label entirely (e.g.
        #     "package for 0776543324"), which (1) alone cannot detect.
        known_genuine_phone = (
            extracted_phone
            or (active_pending or {}).get("phone")
            or customer_last_phone.get(customer_key)
        )
        claims_phone_label = reply_claims_phone_number(clean_reply) and not known_genuine_phone
        claims_mismatched_phone = reply_contains_mismatched_phone(clean_reply, known_genuine_phone)

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

        if claims_phone_label or claims_mismatched_phone:
            print(f"[GUARD] Blocked a reply that echoed/confirmed a phone number for {customer_key} "
                  "with no matching Python-verified phone this turn or on file - likely an "
                  "invalid or hallucinated number.")
            clean_reply = INVALID_PHONE_MESSAGE

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
                        pending_approvals[customer_key]["claimed_core"] = existing.get("claimed_core")
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
                                + probable_match_note(parsed['code_ending'], pending_approvals.get(customer_key))
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
                                + probable_match_note(parsed['code_ending'], pending_approvals.get(customer_key))
                            )
                    else:
                        if customer_key in pending_approvals and pending_approvals[customer_key].get("alert_sent"):
                            continue
                        fallback_ending = extract_code_ending_from_text(text) or ""
                        pending_approvals[customer_key] = {
                            "code_ending": fallback_ending,
                            "price": "", "phone": "", "package": "",
                            "package_key": None, "proposed_code": None, "alert_sent": True,
                            "last_alert_time": time.time(),
                        }
                        admin_msg = (f"🔔 ADMIN ALERT (Customer: {label}):\n{alert_text}"
                                      + probable_match_note(fallback_ending, pending_approvals.get(customer_key)))
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

    # Bulk delete-by-package (additive feature, does not affect /deletecode above).
    delcodelist_reply = handle_delcodelist_admin_message(text)
    if delcodelist_reply is not None:
        admin_bot.reply_to(message, delcodelist_reply)
        save_state()
        return

    # Full database/memory reset (additive, destructive, confirm-guarded).
    memreset_reply = handle_memreset_admin_message(text)
    if memreset_reply is not None:
        admin_bot.reply_to(message, memreset_reply)
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
            reserve_stock_for_waiting(pkg_key)
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
        reserve_stock_for_waiting()
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
        reserve_stock_for_waiting()
        return

    if text.startswith('/'):
        admin_bot.reply_to(
            message,
            "⚠️ Unknown command. Available:\n"
            "/stock, /stockfull, /stockdetail, /listcodes, /codes, /addcode, /deletecode, "
            "/proofs, /delproof, /matchreject"
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

    # Admin says YES but nothing was reserved (request was raised while out of stock):
    # take a code from stock now instead of sending the word "yes" as a voucher.
    if not info.get("proposed_code") and text.strip().lower() in YES_WORDS:
        pk = _pending_package_key(info)
        code_now = reserve_voucher(pk) if pk else None
        if not code_now:
            admin_bot.reply_to(
                message,
                f"⚠️ Still no stock for {pk or 'that package'}. Send a voucher code to approve "
                "this customer, or 'no' to reject."
            )
            return
        info["proposed_code"] = code_now
        info["package_key"] = pk

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
                "If this wasn't a real payment, tap the 🗑️ link above (or send /delproof to see all)."
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
    print("[VERSION] splash_bot.py — smart payment fast-path (price-based package, no up-front phone) + auto stock reservation + YES-on-unreserved fix + Intergram reconnect (tag-first, phone-fallback) + pre-registered proof auto-approval + where-to-pay auto-reply + specific mismatch reasons in admin notes", flush=True)
    load_state()
    print(f"[Groq] Loaded {len(groq_clients)} API key(s) for rotation/fallback.", flush=True)
    print(f"[AUTO] Auto-approval {'ENABLED' if AUTO_APPROVE_ENABLED else 'DISABLED'} "
          f"({len(registered_proofs)} registered proof(s) loaded).", flush=True)
    print(f"[SMS] Webhook /sms {'ENABLED' if SMS_WEBHOOK_SECRET else 'DISABLED (set SMS_WEBHOOK_SECRET to enable)'}.", flush=True)
    Thread(target=run_customer_bot).start()
    Thread(target=run_admin_bot).start()
    Thread(target=run_phone_watchdog, daemon=True).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
