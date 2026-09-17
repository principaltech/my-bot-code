import telebot
import os
import re
from flask import Flask
from threading import Thread, Lock
from groq import Groq

# Tokens
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")       # Bot 1: Customer Bot
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")     # Bot 2: Admin Bot

customer_bot = telebot.TeleBot(TELEGRAM_TOKEN)
admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)

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
# IMPORTANT: keyed by a composite "customer_key" (chat_id + sender's user_id),
# NOT by chat_id alone. If the customer bot is used inside a group (any chat
# with a negative Telegram ID), every member shares the same chat.id, so
# keying by chat_id alone collapses all customers into one record. Keying by
# (chat_id, user_id) keeps each person's thread separate even inside a group,
# while still working normally for private 1-on-1 chats.
user_memory = {}
user_last_message_id = {}   # customer_key -> message_id (for reply_to)
customer_chat_id = {}       # customer_key -> chat_id to send messages back to
customer_display = {}       # customer_key -> human-readable label for admin alerts
admin_chat_id = None


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


# Tracks customers currently waiting on a voucher approval, keyed by
# customer_key:
#   {"code_ending": "...", "price": "...", "phone": "...", "package": "...",
#    "proposed_code": "..." or None, "package_key": "..." or None}
pending_approvals = {}

# Package reference (price + data), mirrors the pricing table below.
PACKAGES = {
    "24 HOURS LITE": {"price": "$1.00", "data": "UNLIMITED"},
    "2 DAYS": {"price": "$0.50", "data": "5GB"},
    "7 DAYS": {"price": "$1.00", "data": "12GB"},
    "3 DAYS LITE": {"price": "$2.00", "data": "UNLIMITED"},
    "14 DAYS PRO": {"price": "$5.00", "data": "UNLIMITED"},
    "30 DAYS LITE": {"price": "$10.00", "data": "UNLIMITED"},
    "30 DAYS PRO": {"price": "$20.00", "data": "UNLIMITED"},
}

voucher_inventory = {pkg: [] for pkg in PACKAGES}
_inventory_lock = Lock()

system_rules = """
You are an automated customer care AI assistant for Splash Internet. You MUST follow these rules strictly:

1. LANGUAGE: Support ONLY Shona or English. Match the user's language.
2. PRICING & PACKAGES:
   - 24 HOURS LITE = USD $1.00 = UNLIMITED
   - 2 DAYS = USD $0.50 = 5GB
   - 7 DAYS = USD $1.00 = 12GB
   - 3 DAYS LITE = USD $2.00 = UNLIMITED
   - 14 DAYS PRO = USD $5.00 = UNLIMITED
   - 30 DAYS LITE = USD $10.00 = UNLIMITED
   - 30 DAYS PRO = USD $20.00 = UNLIMITED
3. PAYMENTS & PROOF OF PAYMENT:
   - EcoCash number is 0776248396.
   - You need three things: (a) the customer's phone number, (b) the package, and (c) proof of payment (a confirmation message or the last 4 digits of the transaction/reference).
   - The package can be stated directly by the customer, OR inferred from the amount they paid. If the amount matches EXACTLY ONE package price, treat that as confirmed automatically - do NOT ask the customer to confirm it again, that wastes their time. Just state which package you matched them to and move straight on to rule 4 and rule 5 in the same reply.
   - Only ask a clarifying question about the package if the amount paid matches more than one package price (e.g. $1.00 = both 24 HOURS LITE and 7 DAYS) or matches no known price at all.
   - EXTRACTING THE TRANSACTION REFERENCE: proof-of-payment messages often contain a labelled reference such as "Approval Code: PP260917.1524.T3000820", "Transaction ID: ...", "Ref: ...", or "Confirmation code: ...". When such a label is present, CODE_ENDING MUST be the last 4 characters of that specific code (letters and digits only, ignore punctuation) - e.g. "T3000820" -> "0820". Do NOT substitute the phone number's last 4 digits when a transaction reference is present, even if it looks unfamiliar or contains letters. Only use the phone number's last 4 digits as a last resort when the customer's message contains no transaction/approval/reference code at all.
4. AFTER PAYMENT PROOF IS SUBMITTED:
   - Tell the user to wait 30 seconds while the payment is validated.
5. ADMIN PAYMENT APPROVAL (CRITICAL INSTRUCTION):
   - When a user submits proof of payment, you MUST generate the exact tag [ADMIN_ALERT] followed immediately by a structured line in EXACTLY this format (pipe-separated, one line, then your own short note after a dash):
     [ADMIN_ALERT] CODE_ENDING: <last 4 characters of the transaction/approval reference, or phone last 4 if truly no reference was given> | PRICE: $<amount> | PHONE: <customer phone number> | PACKAGE: <package name> - Admin, please provide a voucher code.
   - Fill in every field. If you used the phone number instead of a transaction reference, say so explicitly in your note.
   - The system automatically checks real voucher stock for you - you do not need to track or remember whether a code is available. Just always send an accurate alert; the backend and admin decide what happens next.
   - NEVER tell the user you forwarded the payment without including this exact [ADMIN_ALERT] structured line.
6. ADMIN REPLIES:
   - You will never need to interpret or forward admin replies yourself - the backend now matches the admin's reply to the correct customer and delivers the voucher (or rejection) directly and automatically. You do not need to output [IGNORE_ADMIN] or handle raw codes.
7. MANDATORY CLOSING WARNING:
   - You MUST include this exact warning at the end of EVERY customer response: "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
8. SCOPE: Only answer about Splash Internet.
"""

def parse_admin_alert(alert_text):
    """
    Extract CODE_ENDING / PRICE / PHONE / PACKAGE from a structured
    [ADMIN_ALERT] line. Returns None if the AI didn't follow the format,
    in which case we fall back to forwarding the raw text.
    """
    pattern = re.compile(
        r'CODE_ENDING:\s*(?P<code>[^|]+)\|\s*PRICE:\s*(?P<price>[^|]+)\|\s*PHONE:\s*(?P<phone>[^|]+)\|\s*PACKAGE:\s*(?P<package>[^\n]+)',
        re.IGNORECASE
    )
    m = pattern.search(alert_text)
    if not m:
        return None
    return {
        "code_ending": m.group("code").strip(" -\u2014:"),
        "price": m.group("price").strip(" -\u2014:"),
        "phone": m.group("phone").strip(" -\u2014:"),
        "package": re.split(r'[\u2014-]', m.group("package"))[0].strip(),
    }


# Deterministic safety-net: pull a real transaction/approval reference straight
# out of the customer's own message text, independent of whatever the AI
# decided. If found, this always wins over the AI's CODE_ENDING guess, because
# an LLM occasionally falls back to the phone number even when a proper
# reference was clearly given (e.g. "Approval Code: PP260917.1524.T3000820").
TRANSACTION_REF_PATTERN = re.compile(
    r'(?:approval\s*code|transaction\s*id|trans(?:action)?\s*ref(?:erence)?|confirmation\s*code|ref(?:erence)?\s*(?:no\.?|number)?)\s*[:#]?\s*'
    r'([A-Za-z0-9][A-Za-z0-9.\-]{3,})',
    re.IGNORECASE
)


def extract_code_ending_from_text(text):
    """Return the last 4 alphanumeric characters of a labelled transaction
    reference found in `text`, or None if no such reference is present."""
    if not text:
        return None
    m = TRANSACTION_REF_PATTERN.search(text)
    if not m:
        return None
    raw = re.sub(r'[^A-Za-z0-9]', '', m.group(1))
    if len(raw) >= 4:
        return raw[-4:]
    return None


def find_target_customer(admin_text):
    """
    Deterministically match an admin reply to the customer it's for,
    instead of asking the AI to guess across every open chat.
    """
    digits_in_text = re.findall(r'[A-Za-z0-9]{3,}', admin_text)
    for cid, info in pending_approvals.items():
        ending = (info.get("code_ending") or "").strip()
        if ending and any(ending in d or d in ending for d in digits_in_text):
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


def reserve_voucher(package_key):
    if not package_key:
        return None
    with _inventory_lock:
        codes = voucher_inventory.get(package_key)
        if codes:
            return codes.pop(0)
    return None


def return_voucher(package_key, code):
    if not package_key or not code:
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


ADD_CODE_PATTERN = re.compile(
    r'^\s*add\s+code\s+(?P<code>\S+)\s+for\s+(?P<package>.+?)\s*$',
    re.IGNORECASE
)

DELETE_CODE_PATTERN = re.compile(
    r'^\s*delete\s+code\s+(?P<code>\S+)(?:\s+from\s+(?P<package>.+?))?\s*$',
    re.IGNORECASE
)

YES_WORDS = {"yes", "y", "approve", "approved", "ok", "okay", "confirm", "confirmed"}
NO_WORDS = {"no", "n", "reject", "rejected", "cancel", "deny", "denied"}

SERVICE_MESSAGE_PATTERNS = re.compile(
    r'\b(has left|has joined|joined the group|left the group|was removed|removed from the group|'
    r'added to the group|pinned a message|changed the group|changed the chat photo)\b',
    re.IGNORECASE
)


def is_service_message(text):
    return bool(SERVICE_MESSAGE_PATTERNS.search(text or ""))


# ==========================================
# BOT 1: CUSTOMER BOT HANDLER
# ==========================================
@customer_bot.message_handler(func=lambda message: True)
def handle_customer_message(message):
    if message.content_type != 'text' or not (message.text or "").strip():
        return
    if is_service_message(message.text):
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
            temperature=1,
            max_completion_tokens=2048,
            top_p=1
        )
        ai_reply = completion.choices[0].message.content

        alerts = re.findall(r'\[ADMIN_ALERT\](.*)', ai_reply, re.IGNORECASE)
        clean_reply = re.sub(r'\[ADMIN_ALERT\].*', '', ai_reply, flags=re.IGNORECASE).strip()

        if alerts:
            if admin_chat_id:
                for alert in alerts:
                    alert_text = alert.strip()
                    parsed = parse_admin_alert(alert_text)
                    if parsed:
                        # Deterministic override: if the customer's own message
                        # contains a labelled transaction/approval reference,
                        # trust that over whatever the AI put in CODE_ENDING.
                        real_ending = extract_code_ending_from_text(text)
                        if real_ending:
                            parsed["code_ending"] = real_ending

                        pending_approvals[customer_key] = parsed
                        pkg_key = normalize_package(parsed['package'])
                        reserved_code = reserve_voucher(pkg_key)
                        pending_approvals[customer_key]["package_key"] = pkg_key
                        pending_approvals[customer_key]["proposed_code"] = reserved_code

                        label = customer_display.get(customer_key, str(customer_key))

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
                        label = customer_display.get(customer_key, str(customer_key))
                        pending_approvals[customer_key] = {
                            "code_ending": extract_code_ending_from_text(text) or "",
                            "price": "", "phone": "", "package": "",
                            "package_key": None, "proposed_code": None
                        }
                        admin_msg = f"🔔 ADMIN ALERT (Customer: {label}):\n{alert_text}"
                    admin_bot.send_message(admin_chat_id, admin_msg)
            else:
                clean_reply += "\n\n⚠️ SYSTEM NOTIFICATION: The Admin Bot is currently unlinked. (Admin: Please send /start to the Admin bot to reconnect routing)."

        if clean_reply:
            user_memory[customer_key].append({"role": "assistant", "content": ai_reply})
            customer_bot.reply_to(message, clean_reply)

        if len(user_memory[customer_key]) > 15:
            user_memory[customer_key] = [user_memory[customer_key][0]] + user_memory[customer_key][-14:]

    except Exception as e:
        customer_bot.reply_to(message, f"Error processing request: {str(e)}")


# ==========================================
# BOT 2: ADMIN BOT HANDLER
# ==========================================
@admin_bot.message_handler(func=lambda message: True)
def handle_admin_message(message):
    global admin_chat_id

    if message.content_type != 'text' or not (message.text or "").strip():
        return
    if is_service_message(message.text):
        return

    admin_chat_id = message.chat.id
    text = message.text.strip()

    if text.lower() in ['/start', 'hello', 'hi', 'link']:
        admin_bot.reply_to(message, f"✅ Admin Link Active! (Your ID: {admin_chat_id})\nReady to receive and route vouchers.")
        return

    if text.strip().lower() in ['stock', '/stock', 'inventory', '/inventory']:
        lines = [
            f"- {pkg} ({info['price']}, {info['data']}): {len(voucher_inventory.get(pkg, []))} code(s)"
            for pkg, info in PACKAGES.items()
        ]
        admin_bot.reply_to(
            message,
            "📦 Voucher stock:\n" + "\n".join(lines) +
            "\n\nSend 'stock full' to see the actual codes."
        )
        return

    if text.strip().lower() in ['stock full', 'stock detail', 'list codes', '/codes']:
        lines = []
        for pkg, info in PACKAGES.items():
            codes = voucher_inventory.get(pkg, [])
            codes_str = ", ".join(codes) if codes else "(none)"
            lines.append(f"- {pkg} ({info['price']}, {info['data']}): {codes_str}")
        admin_bot.reply_to(message, "📦 Voucher stock (detailed):\n" + "\n".join(lines))
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
        admin_bot.reply_to(message, f"⚠️ Lost track of chat for {target_label}; they'll need to message again.")
        pending_approvals.pop(target_key, None)
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
                "Do not close this current chat, otherwise you might not receive your login code, "
                "token, or password because the chat ID changes."
            )
            history = user_memory.setdefault(target_key, [{"role": "system", "content": system_rules}])
            history.append({"role": "assistant", "content": voucher_message})
            send_to_customer(voucher_message)

            admin_bot.reply_to(message, f"✅ Delivered stored code '{code}' to {target_label} instantly.")
            pending_approvals.pop(target_key, None)
            return

        elif decision in NO_WORDS:
            return_voucher(info.get("package_key"), info["proposed_code"])
            pending_approvals[target_key]["proposed_code"] = None
            admin_bot.reply_to(
                message,
                f"↩️ Put that code back in stock. {target_label} is still waiting — "
                "please reply with a different voucher code for them."
            )
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
            "Do not close this current chat, otherwise you might not receive your login code, "
            "token, or password because the chat ID changes."
        )
        history.append({"role": "assistant", "content": rejection_message})
        send_to_customer(rejection_message)

        admin_bot.reply_to(message, f"❌ Rejection sent to {target_label}.")
        pending_approvals.pop(target_key, None)
        return

    code = text.strip()
    package = info.get("package") or "your package"
    voucher_message = (
        f"✅ Your payment has been approved!\n\n"
        f"Package: {package}\n"
        f"Voucher code: {code}\n\n"
        "Do not close this current chat, otherwise you might not receive your login code, "
        "token, or password because the chat ID changes."
    )
    history.append({"role": "assistant", "content": voucher_message})
    send_to_customer(voucher_message)

    admin_bot.reply_to(message, f"✅ Delivered code '{code}' to {target_label} instantly.")
    pending_approvals.pop(target_key, None)


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
    print(f"[Groq] Loaded {len(groq_clients)} API key(s) for rotation/fallback.")
    Thread(target=run_customer_bot).start()
    Thread(target=run_admin_bot).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
