import telebot
import os
from flask import Flask
from threading import Thread
from groq import Groq

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Groq(api_key=GROQ_API_KEY)

# 1. In-memory storage to remember conversations (Solves the memory issue)
user_memory = {}

# 2. Dynamically capture the admin's chat ID so we can route messages to them
admin_chat_id = None

system_rules = """
You are an automated customer care AI assistant for Splash Internet, a prepaid Wi-Fi network. You MUST follow these rules strictly:

1. LANGUAGE:
   - Support ONLY Shona or English. Match the user's language precisely. Do not switch languages unless the user does.

2. PRICING & PACKAGES:
   - 24 HOURS LITE = USD $1.00 = UNLIMITED
   - 2 DAYS = USD $0.50 = 5GB
   - 7 DAYS = USD $1.00 = 12GB
   - 3 DAYS LITE = USD $2.00 = UNLIMITED
   - 14 DAYS PRO = USD $5.00 = UNLIMITED
   - 30 DAYS LITE = USD $10.00 = UNLIMITED
   - 30 DAYS PRO = USD $20.00 = UNLIMITED
   - Do not invent, change, or add package prices. If a user asks for prices, instruct them to check the login screen, click their desired package, and select SPLASH. Both LITE and PRO are unlimited. Do not invent differences.

3. PAYMENTS & CUSTOMER PHONE NUMBER:
   - EcoCash number: 0776248396.
   - When requesting payment proof, ALWAYS request the customer's phone number. The phone number DOES NOT have to be the same number used for EcoCash. 

4. PROOF OF PAYMENT:
   - Users must provide proof of payment in this current chat.
   - Valid proof formats look like: 
     * "Transfer Confirmation: USD 1.00 sent to PRINCE CHIMBUNDE. Approval Code: PP260912..."
     * "Cashout Confirmation: USD 6.00 sent to ARNOLD MUCHAENERA-044408. Approval Code: CO260911..."
   - Do not ask for EcoCash PINs, OTPs, or passwords.

5. AFTER PAYMENT PROOF IS SUBMITTED:
   - Tell the user to wait 30 seconds while the payment is rechecked.
   - Tell them to remain on the page and wait for payment validation before expecting their login code/token.

6. ADMIN PAYMENT APPROVAL (CRITICAL INSTRUCTION):
   - Payment proof alone does NOT authorize a voucher. ONLY an explicit YES from the admin authorizes it.
   - You MUST NOT ask the customer for approval. 
   - To securely ask the admin, you MUST include a line in your response starting exactly with [ADMIN_ALERT].
   - Example format:
     [ADMIN_ALERT] User submitted proof of payment. Approval code ending: XXXX. Approve payment? YES or NO.
   - Python will intercept any line starting with [ADMIN_ALERT] and forward it securely to the admin. The customer will not see it.

7. ADMIN YES/NO RULE & VOUCHERS:
   - You will receive a system message injected into your chat memory if the admin replies (e.g., "[SYSTEM NOTIFICATION - ADMIN REPLIED]: YES XXXX").
   - YES = payment APPROVED. You may issue an available unused voucher matching the purchased package. Mark it USED immediately.
   - NO = payment REJECTED. Tell the customer the payment could not be validated.
   - Silence is NOT approval. Do not guess.

8. JOIN SPLASH / EQUIPMENT:
   - If asked how to join, partner, or work with Splash Internet, reply: "You can get started by buying Splash Internet equipment from us. Customers who purchase Splash Internet equipment from us will receive an unlimited data plan account."

9. SECURITY & SCOPE:
   - Only answer questions related to Splash Internet services, vouchers, payments, and equipment. Reject off-topic conversations politely.

10. MANDATORY CLOSING WARNING:
   - You MUST include this exact warning at the end of EVERY customer response: "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
"""

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    global admin_chat_id
    try:
        chat_id = message.chat.id
        first_name = str(message.from_user.first_name or "").lower()
        username = str(message.from_user.username or "").lower()
        text = message.text or ""

        # --- 1. ADMIN IDENTIFICATION & ROUTING ---
        if "mr cool" in first_name or "mr cool" in username:
            admin_chat_id = chat_id # Save admin's ID securely in memory

            # Broadcast admin's reply (like "YES 3520" or "VOUCHER 1234") into active customer memories 
            # so the AI knows the admin approved a specific transaction.
            for cid, history in user_memory.items():
                if cid != chat_id: 
                    history.append({
                        "role": "system", 
                        "content": f"[SYSTEM NOTIFICATION - ADMIN REPLIED]: {text}"
                    })
            
            # Exit immediately so the bot strictly doesn't reply to mr cool
            return 

        # --- 2. CUSTOMER MEMORY MANAGEMENT ---
        if chat_id not in user_memory:
            # Initialize a new conversation with the system rules
            user_memory[chat_id] = [{"role": "system", "content": system_rules}]
        
        # Add the user's latest message to memory
        user_memory[chat_id].append({"role": "user", "content": text})

        bot.send_chat_action(chat_id, 'typing')
        
        # --- 3. GENERATE RESPONSE WITH CONTEXT ---
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=user_memory[chat_id],
            temperature=1,
            max_completion_tokens=2048,
            top_p=1
        )
        
        ai_reply = completion.choices[0].message.content
        
        # --- 4. INTERCEPT ADMIN ALERTS ---
        customer_reply_lines = []
        for line in ai_reply.split('\n'):
            if "[ADMIN_ALERT]" in line:
                alert_message = line.replace("[ADMIN_ALERT]", "").strip()
                # Route the alert strictly to mr cool
                if admin_chat_id:
                    bot.send_message(admin_chat_id, f"🔔 ADMIN ACTION REQUIRED:\n{alert_message}")
            else:
                customer_reply_lines.append(line)
        
        # Reconstruct the safe response for the customer
        safe_customer_reply = "\n".join(customer_reply_lines).strip()
        
        if safe_customer_reply:
            # Save the FULL reply (including the hidden alert) back to memory so the AI 
            # remembers that it already asked for approval and doesn't ask twice.
            user_memory[chat_id].append({"role": "assistant", "content": ai_reply}) 
            bot.reply_to(message, safe_customer_reply)

        # Cap memory at 15 messages to prevent crashing Groq's token limits
        if len(user_memory[chat_id]) > 15:
            user_memory[chat_id] = [user_memory[chat_id][0]] + user_memory[chat_id][-14:]
        
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")

app = Flask(__name__)

@app.route('/')
def home():
    return "Splash Internet Bot is awake with Context Memory!"

def run_bot():
    bot.infinity_polling()

if __name__ == "__main__":
    Thread(target=run_bot).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
