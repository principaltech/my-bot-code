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

# Storage
user_memory = {}
admin_chat_id = None

system_rules = """
You are an automated customer care AI assistant for Splash Internet. You MUST follow these rules strictly:

1. LANGUAGE: Support ONLY Shona or English.
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
   - Request customer phone number and proof of payment in this chat.
4. AFTER PAYMENT PROOF IS SUBMITTED:
   - Tell the user to wait 30 seconds while the payment is validated.
5. ADMIN PAYMENT APPROVAL (CRITICAL INSTRUCTION):
   - ONLY an explicit YES from the admin authorizes a voucher.
   - When a user submits proof of payment, you MUST generate the exact tag [ADMIN_ALERT] to notify the admin.
   - Example format: [ADMIN_ALERT] User submitted proof of payment. Approval code ending: XXXX. Approve payment? YES or NO.
   - NEVER tell the user you forwarded the payment without also including the [ADMIN_ALERT] tag in your response.
6. ADMIN YES/NO RULE & VOUCHERS:
   - You will receive a system message if the admin replies: "[SYSTEM NOTIFICATION - ADMIN REPLIED]: YES XXXX".
   - If YES, you may issue a voucher. If NO, reject it.
7. MANDATORY CLOSING WARNING:
   - You MUST include this exact warning at the end of EVERY customer response: "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
8. SCOPE: Only answer about Splash Internet.
"""

# ==========================================
# BOT 1: CUSTOMER BOT HANDLER
# ==========================================
@customer_bot.message_handler(func=lambda message: True)
def handle_customer_message(message):
    chat_id = message.chat.id
    text = message.text or ""

    if chat_id not in user_memory:
        user_memory[chat_id] = [{"role": "system", "content": system_rules}]
    
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
        
        # Use Regex to extract alerts anywhere in the text, and remove them from the customer's view
        alerts = re.findall(r'\[ADMIN_ALERT\](.*)', ai_reply, re.IGNORECASE)
        clean_reply = re.sub(r'\[ADMIN_ALERT\].*', '', ai_reply, flags=re.IGNORECASE).strip()
        
        if alerts:
            if admin_chat_id:
                for alert in alerts:
                    admin_bot.send_message(admin_chat_id, f"🔔 ADMIN ALERT (Customer ID: {chat_id}):\n{alert.strip()}")
            else:
                # FAILSAFE: If you haven't linked the admin bot yet, it warns you directly in the customer chat
                clean_reply += "\n\n⚠️ SYSTEM NOTIFICATION: The Admin Bot is currently unlinked. (Admin: Please send /start to the Admin bot to reconnect routing)."

        if clean_reply:
            user_memory[chat_id].append({"role": "assistant", "content": ai_reply}) 
            customer_bot.reply_to(message, clean_reply)

        # Cap memory to avoid token limits
        if len(user_memory[chat_id]) > 15:
            user_memory[chat_id] = [user_memory[chat_id][0]] + user_memory[chat_id][-14:]
            
    except Exception as e:
        customer_bot.reply_to(message, f"Error processing request: {str(e)}")


# ==========================================
# BOT 2: ADMIN BOT HANDLER
# ==========================================
@admin_bot.message_handler(func=lambda message: True)
def handle_admin_message(message):
    global admin_chat_id
    admin_chat_id = message.chat.id # Locks onto your admin ID
    text = message.text or ""
    
    # Confirm linkage to the admin
    admin_bot.reply_to(message, f"✅ Admin Link Active! (Your ID: {admin_chat_id})\nCommand received: {text}")

    # Only inject into the AI memory if it looks like an approval or voucher command
    if "YES" in text.upper() or "NO" in text.upper() or "VOUCHER" in text.upper():
        for cid, history in user_memory.items():
            history.append({
                "role": "system", 
                "content": f"[SYSTEM NOTIFICATION - ADMIN REPLIED]: {text}"
            })
            
            # Automatically trigger the AI to tell the customer the good/bad news
            try:
                completion = client.chat.completions.create(
                    model="openai/gpt-oss-120b",
                    messages=history,
                    temperature=1,
                    max_completion_tokens=2048,
                    top_p=1
                )
                ai_reply = completion.choices[0].message.content
                clean_reply = re.sub(r'\[ADMIN_ALERT\].*', '', ai_reply, flags=re.IGNORECASE).strip()
                
                if clean_reply:
                    history.append({"role": "assistant", "content": ai_reply})
                    customer_bot.send_message(cid, clean_reply)
            except Exception as e:
                pass


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
    Thread(target=run_customer_bot).start()
    Thread(target=run_admin_bot).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
