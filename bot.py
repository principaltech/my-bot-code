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

# Tracks pending approvals from customers
pending_approvals = {}  # Format: {"9763": {"chat_id": 123, "package": "24 HOURS LITE", "phone": "077..."}}

# The Database that stores your unused voucher codes
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
You are an automated customer care AI assistant for Splash Internet. Follow these rules strictly:
1. LANGUAGE: Support ONLY Shona or English.
2. PRICING & PACKAGES (STRICT NAMES):
   - 24 HOURS LITE = USD $1.00 = UNLIMITED
   - 2 DAYS = USD $0.50 = 5GB
   - 7 DAYS = USD $1.00 = 12GB
   - 3 DAYS LITE = USD $2.00 = UNLIMITED
   - 14 DAYS PRO = USD $5.00 = UNLIMITED
   - 30 DAYS LITE = USD $10.00 = UNLIMITED
   - 30 DAYS PRO = USD $20.00 = UNLIMITED
3. PAYMENTS: EcoCash number is 0776248396. Request phone number and proof of payment.
4. ADMIN PAYMENT APPROVAL (CRITICAL INSTRUCTION):
   - When a user submits proof of payment, you MUST request approval from the backend system.
   - You MUST generate this exact tag on a new line:
     [REQUEST_APPROVAL] ENDING: XXXX | PACKAGE: YYYY | PHONE: ZZZZ
   - Replace XXXX with the last 4 digits of the approval code. Replace YYYY with the exact package name. Replace ZZZZ with their phone number.
   - NEVER tell the user the payment is approved until you receive a [SYSTEM] tag. Tell them to wait 30 seconds.
5. SYSTEM VOUCHER ISSUANCE:
   - If the admin approves, you will receive: "[SYSTEM] PAYMENT APPROVED. Give the user this voucher code: VVVV"
   - Issue the voucher VVVV to the customer enthusiastically.
   - If you receive "[SYSTEM] PAYMENT REJECTED", inform the customer.
6. MANDATORY CLOSING WARNING:
   - End EVERY response with: "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
"""

def get_admin_system_rules():
    # Dynamically inject current inventory into the Admin's brain
    inventory_str = "\n".join([f"   - {pkg}: {len(codes)} codes available" for pkg, codes in voucher_inventory.items()])
    
    return f"""
You are the Splash Internet Admin Assistant. You converse with "mr cool" (the Admin) to manage the network, store vouchers, and process approvals. You are highly intelligent, communicative, and helpful.

CURRENT VOUCHER INVENTORY IN MEMORY:
{inventory_str}

RULES FOR MANAGING VOUCHERS & APPROVALS (USE EXACT TAGS):
1. STORING CODES: If the admin asks to keep/store/save codes in your memory for a package, extract the codes and output this exact tag on a new line:
   [STORE_VOUCHERS] PACKAGE NAME | code1, code2, code3
   
2. APPROVING (AUTO-DISTRIBUTE): If the admin approves a payment (e.g., "YES 9763"), output:
   [APPROVE] 9763
   (The backend will automatically pull a code from memory and clear it).
   
3. APPROVING (MANUAL CODE): If the admin explicitly provides a raw code for an approval (e.g., "9763 gets code X1Y2"), output:
   [APPROVE_WITH_CODE] 9763 | X1Y2

4. REJECTING: If the admin rejects a payment (e.g., "NO 9763"), output:
   [REJECT] 9763

CONVERSATION STYLE:
- Chat naturally. You can answer questions about how many codes are left based on the inventory above.
- When taking an action, just include the [TAG] naturally in your text so the backend Python system can execute it.
- Package names must exactly match the list.
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
        
        # Intercept Approval Requests
        requests = re.findall(r'\[REQUEST_APPROVAL\]\s*ENDING:\s*(\w+)\s*\|\s*PACKAGE:\s*(.*?)\s*\|\s*PHONE:\s*(.*)', ai_reply, re.IGNORECASE)
        clean_reply = re.sub(r'\[REQUEST_APPROVAL\].*', '', ai_reply, flags=re.IGNORECASE).strip()

        if requests:
            for req in requests:
                ending, package, phone = [r.strip() for r in req]
                package_upper = package.upper()
                
                # Save to pending transactions
                pending_approvals[ending] = {"chat_id": chat_id, "package": package_upper, "phone": phone}
                
                if admin_chat_id:
                    stock = len(voucher_inventory.get(package_upper, []))
                    if stock > 0:
                        admin_msg = f"🔔 **NEW PAYMENT PROOF**\nApproval Ending: `{ending}`\nPackage: {package_upper}\nPhone: {phone}\n\n✅ You have {stock} codes in memory. Reply **YES {ending}** to auto-distribute."
                    else:
                        admin_msg = f"🔔 **NEW PAYMENT PROOF**\nApproval Ending: `{ending}`\nPackage: {package_upper}\nPhone: {phone}\n\n⚠️ **OUT OF STOCK!** You have 0 codes in memory for this package. Please provide a code using: `{ending} code XXXX` or store codes first."
                    admin_bot.send_message(admin_chat_id, admin_msg, parse_mode="Markdown")

        if clean_reply:
            user_memory[chat_id].append({"role": "assistant", "content": ai_reply}) 
            customer_bot.reply_to(message, clean_reply)

        if len(user_memory[chat_id]) > 15:
            user_memory[chat_id] = [user_memory[chat_id][0]] + user_memory[chat_id][-14:]
            
    except Exception as e:
        customer_bot.reply_to(message, f"Error processing request: {str(e)}")


# ==========================================
# BOT 2: ADMIN BOT HANDLER
# ==========================================
@admin_bot.message_handler(func=lambda message: True)
def handle_admin_message(message):
    global admin_chat_id, admin_memory
    admin_chat_id = message.chat.id 
    text = message.text or ""

    if text.lower() in ['/start', 'hello', 'hi', 'link']:
        admin_bot.reply_to(message, "✅ Splash Admin AI Online! I am ready to manage your voucher memory and approvals.")
        admin_memory = []
        return

    admin_bot.send_chat_action(admin_chat_id, 'typing')

    # Ensure Admin memory starts with the dynamically updated inventory rules
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
        
        # 1. STORE VOUCHERS
        stores = re.findall(r'\[STORE_VOUCHERS\]\s*(.*?)\s*\|\s*(.*)', ai_reply, re.IGNORECASE)
        for pkg, codes_str in stores:
            pkg = pkg.strip().upper()
            codes = [c.strip() for c in codes_str.split(',') if c.strip()]
            if pkg in voucher_inventory:
                voucher_inventory[pkg].extend(codes)
                admin_bot.send_message(admin_chat_id, f"💾 **MEMORY UPDATED:** Safely stored {len(codes)} codes into `{pkg}`.", parse_mode="Markdown")

        # 2. APPROVE (AUTO-DISTRIBUTE FROM MEMORY)
        approves = re.findall(r'\[APPROVE\]\s*(\w+)', ai_reply, re.IGNORECASE)
        for ending in approves:
            if ending in pending_approvals:
                pending = pending_approvals[ending]
                pkg = pending["package"]
                cid = pending["chat_id"]
                
                if len(voucher_inventory.get(pkg, [])) > 0:
                    # Pop the code (Clears it from memory so it can't be used again)
                    assigned_code = voucher_inventory[pkg].pop(0)
                    
                    # Notify Customer AI to deliver it
                    user_memory[cid].append({"role": "system", "content": f"[SYSTEM] PAYMENT APPROVED. Give the user this voucher code: {assigned_code}"})
                    trigger_customer_ai_delivery(cid)
                    
                    admin_bot.send_message(admin_chat_id, f"✅ **AUTO-DELIVERED:** Code `{assigned_code}` sent to customer. It has been cleared from memory. ({len(voucher_inventory[pkg])} left in `{pkg}`)", parse_mode="Markdown")
                    del pending_approvals[ending]
                else:
                    admin_bot.send_message(admin_chat_id, f"⚠️ **FAILED:** You told me to approve {ending}, but I have 0 codes in memory for `{pkg}`! Please provide a manual code.", parse_mode="Markdown")

        # 3. APPROVE WITH MANUAL CODE
        manuals = re.findall(r'\[APPROVE_WITH_CODE\]\s*(\w+)\s*\|\s*(.*)', ai_reply, re.IGNORECASE)
        for ending, code in manuals:
            if ending in pending_approvals:
                cid = pending_approvals[ending]["chat_id"]
                code = code.strip()
                
                # Notify Customer AI to deliver it
                user_memory[cid].append({"role": "system", "content": f"[SYSTEM] PAYMENT APPROVED. Give the user this voucher code: {code}"})
                trigger_customer_ai_delivery(cid)
                
                admin_bot.send_message(admin_chat_id, f"✅ **DELIVERED:** Manual code `{code}` sent to customer.", parse_mode="Markdown")
                del pending_approvals[ending]

        # 4. REJECT
        rejects = re.findall(r'\[REJECT\]\s*(\w+)', ai_reply, re.IGNORECASE)
        for ending in rejects:
            if ending in pending_approvals:
                cid = pending_approvals[ending]["chat_id"]
                user_memory[cid].append({"role": "system", "content": f"[SYSTEM] PAYMENT REJECTED. Inform the customer."})
                trigger_customer_ai_delivery(cid)
                admin_bot.send_message(admin_chat_id, f"🚫 **REJECTED:** Customer notified.")
                del pending_approvals[ending]

        # Clean AI reply of tags before showing to Admin
        clean_reply = re.sub(r'\[.*?\](.*)', '', ai_reply).strip()
        if clean_reply:
            admin_bot.reply_to(message, clean_reply)

        if len(admin_memory) > 15:
            admin_memory = [admin_memory[0]] + admin_memory[-14:]

    except Exception as e:
        admin_bot.reply_to(message, f"Admin AI Error: {str(e)}")


def trigger_customer_ai_delivery(chat_id):
    """Forces the Customer Bot to generate and send the final voucher response based on the newly injected [SYSTEM] tag."""
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
        
        # Highlight original proof message
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
