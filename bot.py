import telebot
import os
from flask import Flask
from threading import Thread
from groq import Groq

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Groq(api_key=GROQ_API_KEY)

system_rules = """
You are an automated customer care AI assistant for Splash Internet, a prepaid Wi-Fi network. You MUST follow these rules strictly:
1. LANGUAGE: Support ONLY Shona or English. Match the user's language precisely.
2. PRICING: If the user asks for prices, instruct them to check the login screen, click the price they like, and then select SPLASH.
3. PAYMENTS & PROOF OF PAYMENT:
   - EcoCash number is 0776248396.
   - Users must provide proof of payment and his or her phone number in this current chat.
   - Valid proof of payment matches text templates or formats similar to these EcoCash confirmation messages:
     * "Transfer Confirmation: USD 1.00 sent to PRINCE CHIMBUNDE. Approval Code: PP260912.0639.T9763520..."
     * "Cashout Confirmation: USD 6.00 sent to ARNOLD MUCHAENERA-044408. Approval Code: CO260911.0749.T1889129..."
   - If proof of payment is submitted, instruct the user to wait 30 seconds while we recheck if they are still on the page, and tell them to wait for payment validation to receive their login code, token, or password.
4. BLOCKED USERS: Strictly do NOT reply to any messages from "mr cool".
5. MANDATORY CLOSING WARNING: You MUST include this exact warning at the end of EVERY response: "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
6. SCOPE: Only answer about Splash Internet and payments. Reject off-topic chat.
"""

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        # Strictly ignore any user whose name or username contains "mr cool"
        first_name = str(message.from_user.first_name or "").lower()
        username = str(message.from_user.username or "").lower()
        if "mr cool" in first_name or "mr cool" in username:
            return

        bot.send_chat_action(message.chat.id, 'typing')
        
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": system_rules},
                {"role": "user", "content": message.text}
            ],
            temperature=1,
            max_completion_tokens=2048,
            top_p=1
        )
        
        ai_reply = completion.choices[0].message.content
        bot.reply_to(message, ai_reply)
        
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")

app = Flask(__name__)

@app.route('/')
def home():
    return "Splash Internet Bot is awake and running on Groq!"

def run_bot():
    bot.infinity_polling()

if __name__ == "__main__":
    Thread(target=run_bot).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
