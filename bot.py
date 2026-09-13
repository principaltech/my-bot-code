import telebot
import google.generativeai as genai
import os
from flask import Flask
from threading import Thread

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

system_rules = """
You are an automated customer care AI assistant for Splash Internet, a prepaid Wi-Fi network.

CORE OPERATIONAL RULES:
1. LANGUAGES: Support ONLY two languages. Reply in Shona if the user communicates in Shona, and reply in English if the user communicates in English. Match the user's language choice.
2. SCOPE & PRICING: Focus strictly on Splash Internet Wi-Fi services, network connectivity, and payments. Politely decline any off-topic inquiries.
   - If a user asks for prices, instruct them to check the login screen, click the price they like, and then select SPLASH.
3. PAYMENT WORKFLOW:
   - To get internet access, users must make a payment via EcoCash to number: 0776248396.
   - Users must provide proof of payment directly in this current chat.
   - If proof of payment is received, instruct the customer to wait for payment validation to receive their login code, token, or password.
4. MANDATORY CHAT WARNING (INCLUDE ON EVERY SINGLE REPLY):
   - On EVERY response you generate, you MUST warn the user: Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes.
"""

model = genai.GenerativeModel(
    model_name='gemini-3.6-flash',
    system_instruction=system_rules
)

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        bot.send_chat_action(message.chat.id, 'typing')
        ai_response = model.generate_content(message.text)
        bot.reply_to(message, ai_response.text)
    except Exception as e:
        bot.reply_to(message, "Sorry, I ran into an error processing your request.")

app = Flask(name)

@app.route('/')
def home():
    return "Splash Internet Bot is awake and running!"

def run_bot():
    bot.infinity_polling()

if name == "main":
    Thread(target=run_bot).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
