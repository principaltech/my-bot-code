import telebot
import os
import re
import json
import sqlite3
from flask import Flask
from threading import Thread, Lock
from groq import Groq
import librouteros
 
# Tokens
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")       # Bot 1: Customer Bot
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")     # Bot 2: Admin Bot
 
customer_bot = telebot.TeleBot(TELEGRAM_TOKEN)
admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)
 
# ==========================================
# ADMIN AUTHENTICATION
# ==========================================
# Comma-separated Telegram user IDs allowed to act as admin, e.g.
# ADMIN_USER_IDS="123456789,987654321" in your environment.
# Without this set, the admin bot refuses everyone (fail closed, not open) -
# previously *anyone* who found the admin bot and said "hi" became the admin.
_admin_ids_raw = os.environ.get("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = {
    int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip().isdigit()
}
if not ADMIN_USER_IDS:
    print("[WARN] ADMIN_USER_IDS is not set - the admin bot will reject everyone until it is.")
 
 
def is_authorized_admin(message):
    u = message.from_user
    return bool(u) and u.id in ADMIN_USER_IDS
 
# ==========================================
# GROQ MULTI-KEY ROTATION / FALLBACK
# ==========================================
_raw_keys = [
    os.environ.get("GROQ_API_KEY_1"),
    os.environ.get("GROQ_API_KEY_2"),
    os.environ.get("GROQ_API_KEY_3"),
    os.environ.get("GROQ_API_KEY_4"),
    os.environ.get("GROQ_API_KEY_5"),
    os.environ.get("GROQ_API_KEY_6"),
    os.environ.get("GROQ_API_KEY_7"),
    os.environ.get("GROQ_API_KEY"),
]
GROQ_API_KEYS = [k for k in _raw_keys if k]
 
if not GROQ_API_KEYS:
