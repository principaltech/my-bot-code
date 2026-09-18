import telebot
import os
import re
import json
import sqlite3
from flask import Flask
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
        "voucher_inventory": voucher_inventory,
        "used_payment_refs": sorted(used_payment_refs),
        "admin_chat_id": admin_chat_id,
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
        used_payment_refs.update(snapshot.get("used_payment_refs", []))
        for pkg, codes in snapshot.get("voucher_inventory", {}).items():
            voucher_inventory[pkg] = codes
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

admin_awaiting = {}
_admin_state_lock = Lock()

PACKAGES = {
    "24 HOURS LITE": {"price": "$1.00", "data": "UNLIMITED"},
    "2 DAYS": {"price": "$0.50", "data": "5GB"},
    "7 DAYS": {"price": "$1.00", "data": "12GB"},
    "14 DAYS PRO": {"price": "$5.00", "data": "UNLIMITED"},
    "30 DAYS LITE": {"price": "$10.00", "data": "UNLIMITED"},
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
   - 24 HOURS LITE = USD $1.00 = UNLIMITED
   - 2 DAYS = USD $0.50 = 5GB
   - 7 DAYS = USD $1.00 = 12GB
   - 14 DAYS PRO = USD $5.00 = UNLIMITED
   - 30 DAYS LITE = USD $10.00 = UNLIMITED
3. PAYMENTS & PROOF OF PAYMENT:
   - EcoCash number is 0776248396.
   - You need three things: (a) the customer's phone number, (b) the package, and (c) proof of payment.
   - A VALID TRANSACTION REFERENCE MUST BE AT LEAST 7 CHARACTERS LONG. If a user provides a reference that is less than 7 characters (for example, a 4-digit code like "0090"), you MUST reject it. Tell them the code is too short and ask for the FULL exact transaction reference or the full confirmation message.
   - EXTRACTING THE TRANSACTION REFERENCE: Look for the longest alphanumeric string in the message. CODE_ENDING MUST be the EXACT last 7 characters of that full reference. Ignore punctuation like dots or dashes. NEVER accept or use a code shorter than 7 characters.
4. AFTER PAYMENT PROOF IS SUBMITTED:
   - Tell the user to wait 30 seconds while the payment is validated. That is ALL you say about the outcome.
   - CRITICAL: Do NOT attempt to repeat, quote, or summarize the customer's transaction reference back to them in your conversational reply. The reference must ONLY be output inside the [ADMIN_ALERT] tag.
5. ADMIN PAYMENT APPROVAL (CRITICAL INSTRUCTION):
   - When a user submits a VALID proof of payment (minimum 7 chars), generate the exact tag [ADMIN_ALERT] followed immediately by a structured line:
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
"""

WAIT_MESSAGE = "Thank you, we have received your payment proof. Please wait about 30 seconds while we validate the payment."
NEED_PROOF_MESSAGE = ("Please send your phone number, the package you want, and your proof of payment "
                      "(the EcoCash confirmation message or the exact transaction reference).")
DUPLICATE_MESSAGE = ("This payment proof has already been processed. If you did not receive your voucher, "
                     "please scroll up in this chat to find it, or contact support.")

FAKE_APPROVAL_RE = re.compile(
    r'(?:payment\s+(?:has\s+been|was|is|is\s+now)\s+(?:approved|verified|confirmed|successful))'
    r'|(?:(?:voucher|login|access|wifi|wi-fi)\s*(?:code|pin)\s*\**\s*[:\-\u2013\u2014=]\s*\**\s*[A-Za-z0-9])'
    r'|(?:\b(?:token|password|username|pin)\s*\**\s*[:=]\s*\**\s*[A-Za-z0-9])',
    re.IGNORECASE
)
SUSPICIOUS_TOKEN_RE = re.compile(r'\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,14}\b')

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

def parse_admin_alert(alert_text, user_text=""):
    pattern = re.compile(
        r'CODE_ENDING:\s*(?P<code>[^|]+)\|\s*PRICE:\s*(?P<price>[^|]+)\|\s*PHONE:\s*(?P<phone>[^|]+)\|\s*PACKAGE:\s*(?P<package>[^\n]+)',
        re.IGNORECASE
    )
    m = pattern.search(alert_text)
    if not m:
        return None
        
    code_ending = m.group("code").strip(" -\u2014:")
    code_alphanum = re.sub(r'[^A-Za-z0-9]', '', code_ending)
    
    # If the LLM hallucinated a bad code because of dots/dashes, let the backend's regex save it
    real_ending = extract_code_ending_from_text(user_text)
    
    if real_ending:
        final_code = real_ending
    elif len(code_alphanum) >= 7:
        final_code = code_alphanum[-7:]
    else:
        # We couldn't salvage 7 characters either way
        return None

    return {
        "code_ending": final_code,
        "price": m.group("price").strip(" -\u2014:"),
        "phone": m.group("phone").strip(" -\u2014:"),
        "package": re.split(r'[\u2014-]', m.group("package"))[0].strip(),
    }

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
    text = message.text.strip()

    user_last_message_id[customer_key] = message.message_id
    customer_chat_id[customer_key] = message.chat.id
    customer_display[customer_key] = display_name_for(message)

    if customer_key not in user_memory:
        user_memory[customer_key] = [{"role": "system", "content": system_rules}]

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
                parsed = parse_admin_alert(alert_text, text)
                
                if parsed:
                    alerts_to_process.append((alert_text, parsed))
                else:
                    # If parsing failed, see if it was because the LLM pushed a short code 
                    # AND the backend's regex fallback also failed to find a valid code
                    m = re.search(r'CODE_ENDING:\s*([^|]+)', alert_text, re.IGNORECASE)
                    if m:
                        raw_c = re.sub(r'[^A-Za-z0-9]', '', m.group(1))
                        if raw_c and len(raw_c) < 7:
                            short_code_detected = True
                            continue # Kill this alert
                    
                    alerts_to_process.append((alert_text, None))

        # HARD BACKEND GUARD: Instantly intercept and override AI if a short code was pushed 
        # without a backend fallback
        if short_code_detected:
            clean_reply = "⚠️ The transaction reference provided is too short. A valid approval code must be at least 7 characters long. Please send the FULL exact transaction reference."
            alerts_to_process = []
        elif not clean_reply and (alerts_to_process or tampered):
            clean_reply = WAIT_MESSAGE if alerts_to_process else NEED_PROOF_MESSAGE

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

                        existing = pending_approvals.get(customer_key)
                        if existing:
                            if fp and payment_fingerprint(existing) == fp:
                                continue
                            if existing.get("proposed_code"):
                                return_voucher(existing.get("package_key"), existing["proposed_code"])

                        pending_approvals[customer_key] = parsed
                        pkg_key = normalize_package(parsed['package'])
                        reserved_code = reserve_voucher(pkg_key)
                        pending_approvals[customer_key]["package_key"] = pkg_key
                        pending_approvals[customer_key]["proposed_code"] = reserved_code

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
                        if customer_key in pending_approvals:
                            continue
                        pending_approvals[customer_key] = {
                            "code_ending": extract_code_ending_from_text(text) or "",
                            "price": "", "phone": "", "package": "",
                            "package_key": None, "proposed_code": None
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
            
            # Keep AI context clean from fake memory loops
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
            "/stock, /stockfull, /stockdetail, /listcodes, /codes, /addcode, /deletecode"
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

def run_customer_bot():
    customer_bot.infinity_polling()

def run_admin_bot():
    admin_bot.infinity_polling()

if __name__ == "__main__":
    load_state()
    print(f"[Groq] Loaded {len(groq_clients)} API key(s) for rotation/fallback.")
    Thread(target=run_customer_bot).start()
    Thread(target=run_admin_bot).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
