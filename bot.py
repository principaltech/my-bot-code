import telebot
import google.generativeai as genai
import os
from flask import Flask
from threading import Thread

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

model = genai.GenerativeModel(model_name='gemini-2.5-flash')

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        bot.send_chat_action(message.chat.id, 'typing')
        
        # Wrap the rules directly with the user's message for strict compliance
        strict_prompt = f"""
        [SYSTEM INSTRUCTIONS - YOU MUST STRICTLY FOLLOW THESE ON EVERY REPLY]
        - You are an automated customer care AI for Splash Internet, a prepaid Wi-Fi network.
        - LANGUAGE: Support ONLY Shona or English. Match the user's language precisely.
        - PRICING: If the user asks for prices, instruct them to check the login screen, click the price they like, and then select SPLASH.
        - PAYMENTS: EcoCash number is 0776248396. Users must provide proof of payment in this chat. If proof is received, tell them to wait for validation to get their login code/token/password.
        - MANDATORY CLOSING WARNING: You MUST include this exact warning at the end of EVERY response: "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
        - SCOPE: Only answer about Splash Internet and payments. Reject off-topic chat.

        [USER MESSAGE]: {message.text}
        """
        
        ai_response = model.generate_content(strict_prompt)
        bot.reply_to(message, ai_response.text)
        
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")

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
