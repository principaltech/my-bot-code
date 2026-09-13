import telebot
import google.generativeai as genai
import os
from flask import Flask
from threading import Thread

# Securely grab keys from Render (so they aren't public on GitHub)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Initialize Bot and AI
bot = telebot.TeleBot(TELEGRAM_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')

# Handle incoming Telegram messages
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        # Show 'typing...' status in Telegram
        bot.send_chat_action(message.chat.id, 'typing')
        # Get AI response
        ai_response = model.generate_content(message.text)
        # Send reply
        bot.reply_to(message, ai_response.text)
    except Exception as e:
        bot.reply_to(message, str(e))

# Create a fake web server to satisfy Render's requirements
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is awake and running!"

# Function to keep the bot listening in the background
def run_bot():
    bot.infinity_polling()

# Run both the web server and the bot simultaneously
if __name__ == "__main__":
    Thread(target=run_bot).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
