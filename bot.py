import telebot
import os
from flask import Flask
from threading import Thread
from groq import Groq

# Pull both bot tokens from Render Environment
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")       # Bot 1: For Customers
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")     # Bot 2: For You (Admin)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

customer_bot = telebot.TeleBot(TELEGRAM_TOKEN)
admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)
client = Groq(api_key=GROQ_API_KEY)

# Storage
user_memory = {}
admin_chat_id = None

system_rules = """
You are an automated customer care AI assistant for Splash Internet, a prepaid Wi-Fi network. You MUST follow these rules strictly:

1. LANGUAGE: Support ONLY Shona or English. Match the user's language precisely.
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
   - To secretly ask the admin, generate a line starting exactly with [ADMIN_ALERT].
   - Example: [ADMIN_ALERT] User submitted proof of payment. Approval code ending: XXXX. Approve payment? YES or NO.
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
        
        customer_reply_lines = []
        for line in ai_reply.split('\n'):
            if "[ADMIN_ALERT]" in line:
                alert_message = line.replace("[ADMIN_ALERT]", "").strip()
                # Send this alert strictly to the 2nd Admin Bot
                if admin_chat_id:
                    admin_bot.send_message(admin_chat_id, f"🔔 ADMIN ALERT (Customer ID: {chat_id}):\n{alert_message}")
            else:
                customer_reply_lines.append(line)
        
        safe_customer_reply = "\n".join(customer_reply_lines).strip()
        
        if safe_customer_reply:
            user_memory[chat_id].append({"role": "assistant", "content": ai_reply}) 
            customer_bot.reply_to(message, safe_customer_reply)

        if len(user_memory[chat_id]) > 15:
            user_memory[chat_id] = [user_memory[chat_id][0]] + user_memory[chat_id][-14:]
            
    except Exception as e:
        customer_bot.reply_to(message, f"Error processing your request.")


# ==========================================
# BOT 2: ADMIN BOT HANDLER (For mr cool)
# ==========================================
@admin_bot.message_handler(func=lambda message: True)
def handle_admin_message(message):
    global admin_chat_id
    admin_chat_id = message.chat.id # Saves your admin chat ID when you message the second bot
    text = message.text or ""
    
    # Acknowledge receipt to the admin
    admin_bot.reply_to(message, f"✅ Admin command received: {text}")

    # Inject the Admin's command silently into all active customer memories
    for cid, history in user_memory.items():
        history.append({
            "role": "system", 
            "content": f"[SYSTEM NOTIFICATION - ADMIN REPLIED]: {text}"
        })
        
        # Proactively trigger the AI to tell the customer their payment was approved
        try:
            completion = client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=history,
                temperature=1,
                max_completion_tokens=2048,
                top_p=1
            )
            ai_reply = completion.choices[0].message.content
            
            # Ensure no alerts leak back to the customer here
            clean_reply = "\n".join([line for line in ai_reply.split('\n') if "[ADMIN_ALERT]" not in line]).strip()
            
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
    return "Dual-Bot Splash Internet System is Awake!"

def run_customer_bot():
    customer_bot.infinity_polling()

def run_admin_bot():
    admin_bot.infinity_polling()

if __name__ == "__main__":
    # Start both bots in separate threads so they run at the exact same time
    Thread(target=run_customer_bot).start()
    Thread(target=run_admin_bot).start()
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
