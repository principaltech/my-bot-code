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
user_last_message_id = {}  # NEW: Tracks the user's last message ID for highlighting replies
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
   - When a user submits proof of payment, check your memory. If you DO NOT have a stored, unused voucher for their package, you must ask the admin to provide one.
   - You MUST generate the exact tag [ADMIN_ALERT] to notify the admin secretly.
   - Example format: [ADMIN_ALERT] User submitted proof of payment. Approval code ending: XXXX. Package: YYYY. Admin, please provide a voucher code for this package, or reply NO to reject.
   - If you DO have vouchers stored already, just ask: Approve payment? YES or NO.
   - NEVER tell the user you forwarded the payment without including the [ADMIN_ALERT] tag.
6. ADMIN REPLIES & VOUCHERS:
   - You will receive a system message if the admin replies: "[SYSTEM NOTIFICATION - ADMIN REPLIED]: <message>".
   - If the admin replies with a voucher code, consider the payment APPROVED. Issue the voucher to the user and mark it USED.
   - If the admin replies NO, reject the payment.
   - IMPORTANT: If the admin's reply is meant for a DIFFERENT customer's approval code, ignore it completely and output only the exact word: [IGNORE_ADMIN]
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
    
    # Save the ID of the user's message so the bot can "highlight/reply" to it later
    user_last_message_id[chat_id] = message.message_id

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
        
        alerts = re.findall(r'\[ADMIN_ALERT\](.*)', ai_reply, re.IGNORECASE)
        clean_reply = re.sub(r'\[ADMIN_ALERT\].*', '', ai_reply, flags=re.IGNORECASE).strip()
        
        if alerts:
            if admin_chat_id:
                for alert in alerts:
                    admin_bot.send_message(admin_chat_id, f"🔔 ADMIN ALERT (Customer ID: {chat_id}):\n{alert.strip()}")
            else:
                clean_reply += "\n\n⚠️ SYSTEM NOTIFICATION: The Admin Bot is currently unlinked. (Admin: Please send /start to the Admin bot to reconnect routing)."

        if clean_reply:
            user_memory[chat_id].append({"role": "assistant", "content": ai_reply}) 
            # Highlight/Reply directly to the customer's message
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
    global admin_chat_id
    admin_chat_id = message.chat.id 
    text = message.text or ""
    
    admin_bot.reply_to(message, f"✅ Admin Link Active! Command processed: {text}")

    # Inject the Admin's command silently into all active customer memories
    for cid, history in user_memory.items():
        history.append({
            "role": "system", 
            "content": f"[SYSTEM NOTIFICATION - ADMIN REPLIED]: {text}"
        })
        
        try:
            completion = client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=history,
                temperature=1,
                max_completion_tokens=2048,
                top_p=1
            )
            ai_reply = completion.choices[0].message.content
            
            # If the AI realizes this admin message belongs to a different customer, it will say [IGNORE_ADMIN]
            if "[IGNORE_ADMIN]" in ai_reply:
                # Remove the irrelevant system notification from this customer's memory so it doesn't confuse them later
                history.pop()
                continue
            
            clean_reply = re.sub(r'\[ADMIN_ALERT\].*', '', ai_reply, flags=re.IGNORECASE).strip()
            
            if clean_reply:
                history.append({"role": "assistant", "content": ai_reply})
                
                # Retrieve the customer's last message ID to highlight it in the reply
                reply_id = user_last_message_id.get(cid)
                if reply_id:
                    customer_bot.send_message(cid, clean_reply, reply_to_message_id=reply_id)
                else:
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
