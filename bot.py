import telebot
import os
import re
from flask import Flask
from threading import Thread
from groq import Groq

# Tokens
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")       # Bot 1: Customer Bot
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")     # Bot 2: Admin Bot
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

customer_bot = telebot.TeleBot(TELEGRAM_TOKEN)
admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)
client = Groq(api_key=GROQ_API_KEY)

# ==========================================
# CENTRAL MEMORY & DATABASE
# ==========================================
user_memory = {}
user_last_message_id = {}
admin_memory = []
admin_chat_id = None

pending_approvals = {}  # Format: {"7873": {"chat_id": 123, "package": "2 DAYS", "phone": "077..."}}

voucher_inventory = {
    "24 HOURS LITE": [],
    "2 DAYS": [],
    "7 DAYS": [],
    "3 DAYS LITE": [],
    "14 DAYS PRO": [],
    "30 DAYS LITE": [],
    "30 DAYS PRO": []
}

# ==========================================
# SYSTEM RULES
# ==========================================
CUSTOMER_SYSTEM_RULES = """
You are a human-like customer care representative for Splash Internet. Follow these rules strictly:
1. IDENTITY RESTRICTION (CRITICAL): 
   - NEVER use the words "bot", "AI", "assistant", "automated", "system", or "virtual" when speaking to customers. 
   - Always speak as a real member of the Splash Internet customer care team.
2. LANGUAGE: Support ONLY Shona or English.
3. PRICING & PACKAGES (STRICT NAMES):
   - 24 HOURS LITE = USD $1.00 = UNLIMITED
   - 2 DAYS = USD $0.50 = 5GB
   - 7 DAYS = USD $1.00 = 12GB
   - 3 DAYS LITE = USD $2.00 = UNLIMITED
   - 14 DAYS PRO = USD $5.00 = UNLIMITED
   - 30 DAYS LITE = USD $10.00 = UNLIMITED
   - 30 DAYS PRO = USD $20.00 = UNLIMITED
4. PAYMENTS & PROOF OF PAYMENT RECOGNITION (CRITICAL):
   - EcoCash number is 0776248396.
   - You MUST instantly recognize valid proof of payment messages matching these exact formats:
     * "Transfer Confirmation: USD 0.50 sent to SPLASH INTERNET. Approval Code: PP260915.0921.T389978733..."
     * "Cashout Confirmation: USD 6.00 sent to ARNOLD MOTO. Approval Code: CO260911.0749.T1889777..."
   - When a user sends proof of payment, extract the amount, match it to the correct package price, and request their contact phone number if they haven't provided it yet.
5. ADMIN PAYMENT APPROVAL:
   - When proof of payment and phone number are received, you MUST request backend approval.
   - You MUST generate this exact tag on a new line:
     [REQUEST_APPROVAL] XXXX | YYYY | ZZZZ
   - Replace XXXX with the last 4 digits of the approval code (e.g., from T389978733, the ending digits or full trailing numbers). Replace YYYY with the exact package name based on the amount paid. Replace ZZZZ with their phone number.
   - EXAMPLE: [REQUEST_APPROVAL] 8733 | 2 DAYS | 0771234567
   - Tell the customer: "Thank you for sending the payment proof. Please stay on the page and wait about 30 seconds while we process your verification."
6. VOUCHER ISSUANCE:
   - If approved, you will receive: "[SYSTEM] PAYMENT APPROVED. Give the user this voucher code: VVVV"
   - Issue the voucher VVVV to the customer professionally.
   - If you receive "[SYSTEM] PAYMENT REJECTED", inform the customer politely.
7. MANDATORY CLOSING WARNING:
   - End EVERY response with: "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
"""

def get_admin_system_rules():
    inventory_str = "\n".join([f"   - {pkg}: {len(codes)} codes available" for pkg, codes in voucher_inventory.items()])
    
    return f"""
You are the Splash Internet Admin Assistant. You converse with "mr cool" (the Admin) to manage network inventory, store vouchers, and process approvals. 

CURRENT VOUCHER INVENTORY IN MEMORY:
{inventory_str}

RULES FOR MANAGING VOUCHERS & APPROVALS (USE EXACT TAGS):
1. STORING CODES: If the admin asks to keep/store codes in memory, output this exact tag on a new line:
   [STORE_VOUCHERS] PACKAGE NAME | code1, code2, code3
   
2. APPROVING (AUTO-DISTRIBUTE): If the admin approves a payment (e.g., "YES 7873"), output:
   [APPROVE] 7873
   
3. APPROVING (MANUAL CODE): If the admin explicitly provides a raw code (e.g., "7873 gets code X1Y2"), output:
   [APPROVE_WITH_CODE] 7873 | X1Y2

4. REJECTING: If the admin rejects a payment (e.g., "NO 7873"), output:
   [REJECT] 7873

CONVERSATION STYLE:
- Chat naturally with the admin. Answer questions about inventory. 
- Include the [TAG] naturally in your text so the backend Python system can execute it.
"""

# ==========================================
# BOT 1: CUSTOMER BOT HANDLER
# ==========================================
@customer_bot.message_handler(func=lambda message: True)
def handle_customer_message(message):
    chat_id = message.chat.id
    text = message.text or ""
    
    user_last_message_id[chat_id] = message.message_id

    if chat_id not in user_memory:
        user_memory[chat_id] = [{"role": "system", "content": CUSTOMER_SYSTEM_RULES}]
    
    user_memory[chat_id].append({"role": "user", "content": text})
    customer_bot.send_chat_action(chat_id, 'typing')
    
    try:
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=user_memory[chat_id],
            temperature=1,
            max_completion_tokens=2048,
            top_p=1
        )
        ai_reply = completion.choices[0].message.content
        
        requests = re.findall(r'\[REQUEST_APPROVAL\](.*)', ai_reply, re.IGNORECASE)
        clean_reply = re.sub(r'\[REQUEST_APPROVAL\].*', '', ai_reply, flags=re.IGNORECASE).strip()

        if requests:
            for req in requests:
                parts = [p.strip() for p in req.split('|')]
                ending = parts[0] if len(parts) > 0 else "UNKNOWN"
                package = parts[1].upper() if len(parts) > 1 else "UNKNOWN PACKAGE"
                phone = parts[2] if len(parts) > 2 else "UNKNOWN PHONE"
                
                pending_approvals[ending] = {"chat_id": chat_id, "package": package, "phone": phone}
                
                if admin_chat_id:
                    stock = len(voucher_inventory.get(package, []))
                    if stock > 0:
                        admin_msg = f"🔔 **NEW PAYMENT PROOF**\nApproval Ending: `{ending}`\nPackage: {package}\nPhone: {phone}\n\n✅ You have {stock} codes in memory. Reply **YES {ending}** to auto-distribute."
                    else:
                        admin_msg = f"🔔 **NEW PAYMENT PROOF**\nApproval Ending: `{ending}`\nPackage: {package}\nPhone: {phone}\n\n⚠️ **OUT OF STOCK!** You have 0 codes in memory for this package. Please provide a manual code: `{ending} code XXXX` or store codes first."
                    admin_bot.send_message(admin_chat_id, admin_msg, parse_mode="Markdown")
                else:
                    clean_reply += "\n\n⚠️ **NOTICE:** Our verification team is currently offline. (Admin: Please send /start to your Admin Bot to reconnect routing)."

        if clean_reply:
            user_memory[chat_id].append({"role": "assistant", "content": ai_reply}) 
            customer_bot.reply_to(message, clean_reply)

        if len(user_memory[chat_id]) > 15:
            user_memory[chat_id] = [user_memory[chat_id][0]] + user_memory[chat_id][-14:]
            
    except Exception as e:
        customer_bot.reply_to(message, "Thank you for reaching out. Please give us a moment while we process your request.")


# ==========================================
# BOT 2: ADMIN BOT HANDLER
# ==========================================
@admin_bot.message_handler(func=lambda message: True)
def handle_admin_message(message):
    global admin_chat_id, admin_memory
    admin_chat_id = message.chat.id 
    text = message.text or ""

    if text.lower() in ['/start', 'hello', 'hi', 'link']:
        admin_bot.reply_to(message, "✅ Splash Admin Link Active! Connection restored. Ready to route approvals.")
        admin_memory = []
        return

    admin_bot.send_chat_action(admin_chat_id, 'typing')

    if not admin_memory or admin_memory[0]["role"] == "system":
        admin_memory = [{"role": "system", "content": get_admin_system_rules()}] + admin_memory[1:]

    admin_memory.append({"role": "user", "content": text})

    try:
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=admin_memory,
            temperature=1,
            max_completion_tokens=2048,
            top_p=1
        )
        ai_reply = completion.choices[0].message.content
        admin_memory.append({"role": "assistant", "content": ai_reply})

        # --- PROCESS AI COMMAND TAGS ---
        
        stores = re.findall(r'\[STORE_VOUCHERS\]\s*(.*?)\s*\|\s*(.*)', ai_reply, re.IGNORECASE)
        for pkg, codes_str in stores:
            pkg = pkg.strip().upper()
            codes = [c.strip() for c in codes_str.split(',') if c.strip()]
            if pkg in voucher_inventory:
                voucher_inventory[pkg].extend(codes)
                admin_bot.send_message(admin_chat_id, f"💾 **MEMORY UPDATED:** Safely stored {len(codes)} codes into `{pkg}`.", parse_mode="Markdown")
            else:
                admin_bot.send_message(admin_chat_id, f"⚠️ Warning: Could not find package `{pkg}`. Code(s) not saved.")

        approves = re.findall(r'\[APPROVE\]\s*(\w+)', ai_reply, re.IGNORECASE)
        for ending in approves:
            if ending in pending_approvals:
                pending = pending_approvals[ending]
                pkg = pending["package"]
                cid = pending["chat_id"]
                
                if len(voucher_inventory.get(pkg, [])) > 0:
                    assigned_code = voucher_inventory[pkg].pop(0)
                    user_memory[cid].append({"role": "system", "content": f"[SYSTEM] PAYMENT APPROVED. Give the user this voucher code: {assigned_code}"})
                    trigger_customer_ai_delivery(cid)
                    admin_bot.send_message(admin_chat_id, f"✅ **AUTO-DELIVERED:** Code `{assigned_code}` sent. ({len(voucher_inventory[pkg])} left in `{pkg}`)", parse_mode="Markdown")
                    del pending_approvals[ending]
                else:
                    admin_bot.send_message(admin_chat_id, f"⚠️ **FAILED:** 0 codes in memory for `{pkg}`! Please provide a manual code.", parse_mode="Markdown")

        manuals = re.findall(r'\[APPROVE_WITH_CODE\]\s*(\w+)\s*\|\s*(.*)', ai_reply, re.IGNORECASE)
        for ending, code in manuals:
            if ending in pending_approvals:
                cid = pending_approvals[ending]["chat_id"]
                code = code.strip()
                user_memory[cid].append({"role": "system", "content": f"[SYSTEM] PAYMENT APPROVED. Give the user this voucher code: {code}"})
                trigger_customer_ai_delivery(cid)
                admin_bot.send_message(admin_chat_id, f"✅ **DELIVERED:** Manual code `{code}` sent to customer.", parse_mode="Markdown")
                del pending_approvals[ending]

        rejects = re.findall(r'\[REJECT\]\s*(\w+)', ai_reply, re.IGNORECASE)
        for ending in rejects:
            if ending in pending_approvals:
                cid = pending_approvals[ending]["chat_id"]
                user_memory[cid].append({"role": "system", "content": f"[SYSTEM] PAYMENT REJECTED. Inform the customer."})
                trigger_customer_ai_delivery(cid)
                admin_bot.send_message(admin_chat_id, f"🚫 **REJECTED:** Customer notified.")
                del pending_approvals[ending]

        clean_reply = re.sub(r'\[.*?\](.*)', '', ai_reply).strip()
        if clean_reply:
            admin_bot.reply_to(message, clean_reply)

        if len(admin_memory) > 15:
            admin_memory = [admin_memory[0]] + admin_memory[-14:]

    except Exception as e:
        admin_bot.reply_to(message, f"Admin Error: {str(e)}")


def trigger_customer_ai_delivery(chat_id):
    try:
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=user_memory[chat_id],
            temperature=1,
            max_completion_tokens=2048,
            top_p=1
        )
        ai_reply = completion.choices[0].message.content
        user_memory[chat_id].append({"role": "assistant", "content": ai_reply})
        
        reply_id = user_last_message_id.get(chat_id)
        if reply_id:
            customer_bot.send_message(chat_id, ai_reply, reply_to_message_id=reply_id)
        else:
            customer_bot.send_message(chat_id, ai_reply)
    except:
        pass

# ==========================================
# SERVER AND MULTI-THREADING
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Splash Dual-Bot Intelligence Running!"

def run_customer_bot():
    customer_bot.infinity_polling()

def run_admin_bot():
    admin_bot.infinity_polling()

if __name__ == "__main__":
    Thread(target=run_customer_bot).start()
    Thread(target=run_admin_bot).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
