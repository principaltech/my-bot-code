import telebot
import os
import re
import json
from flask import Flask
from threading import Thread
from groq import Groq


# =========================================================
# CONFIGURATION
# =========================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# Telegram username of the authorized admin.
# Set ADMIN_USERNAME in your environment if the actual
# Telegram username is different.
ADMIN_USERNAME = os.environ.get(
    "ADMIN_USERNAME",
    "mr cool"
).lstrip("@").lower()

# Local temporary state file.
# This allows the bot to remember pending payments and
# unused voucher codes even if the Python process restarts,
# provided the hosting platform keeps its filesystem.
STATE_FILE = "splash_bot_state.json"


# =========================================================
# BOT / GROQ
# =========================================================

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Groq(api_key=GROQ_API_KEY)


# =========================================================
# SPLASH INTERNET SYSTEM RULES
# =========================================================

system_rules = """
You are an automated customer care AI assistant for Splash Internet,
a prepaid Wi-Fi network. You MUST follow these rules strictly.

1. LANGUAGE:
   - Support ONLY Shona or English.
   - Match the user's language precisely.
   - Do not switch languages unless the user does.

2. PRICING:
   The current Splash Internet packages displayed on the Wi-Fi portal are:

   - 24 HOURS LITE — USD $1.00 — UNLIMITED
   - 2 DAYS — USD $0.50 — 5GB
   - 7 DAYS — USD $1.00 — 12GB
   - 3 DAYS LITE — USD $2.00 — UNLIMITED
   - 14 DAYS PRO — USD $5.00 — UNLIMITED
   - 30 DAYS LITE — USD $10.00 — UNLIMITED
   - 30 DAYS PRO — USD $20.00 — UNLIMITED

   PRICING RULES:
   - Do not invent, change, or add package prices.
   - If the user asks for prices, provide the relevant package(s).
   - Users can also check the Splash Internet login screen for the
     currently displayed packages.
   - To purchase, instruct the user to click the package/price they
     want on the login screen and then select SPLASH.
   - If the user asks about one specific package, do not unnecessarily
     provide the entire price list.

3. PACKAGE INFORMATION:
   - 24 HOURS LITE = $1.00 = UNLIMITED
   - 2 DAYS = $0.50 = 5GB
   - 7 DAYS = $1.00 = 12GB
   - 3 DAYS LITE = $2.00 = UNLIMITED
   - 14 DAYS PRO = $5.00 = UNLIMITED
   - 30 DAYS LITE = $10.00 = UNLIMITED
   - 30 DAYS PRO = $20.00 = UNLIMITED

   IMPORTANT:
   - Do not invent differences between LITE and PRO.
   - Both displayed LITE/PRO packages listed above are unlimited.
   - Do not claim a particular speed, priority, device limit, or other
     feature unless explicitly provided by Splash Internet.

4. PAYMENTS & CUSTOMER PHONE NUMBER:
   - EcoCash number: 0776248396.
   - When requesting payment proof, ALWAYS request the customer's
     phone/contact number.
   - The phone number is the customer's general contact number for
     Splash Internet customer support and payment processing.
   - The customer's phone number DOES NOT have to be the same number
     used to make the EcoCash payment.
   - Customers use Splash Internet vouchers to log in to the Wi-Fi.
   - Do NOT describe the customer's phone number as a number registered
     for their Splash Internet account/service.
   - Do not require the customer's contact number to match the
     EcoCash sender's number.

5. PROOF OF PAYMENT:
   - Users must provide proof of payment in this current chat.
   - Valid proof may contain EcoCash confirmation information similar to:

     "Transfer Confirmation: USD 1.00 sent to SPLASH INTERNET.
      Approval Code: PP260912.0639.T9763520..."

     "Cashout Confirmation: USD 6.00 sent to JOHN ARNOLD.
      Approval Code: CO260911.0749.T1889129..."

   - These are examples of payment-proof formats only.
   - Do not automatically approve a payment merely because the message
     resembles these examples.
   - Do not invent transaction details or approval codes.
   - Never ask users for EcoCash PINs, bank PINs, OTPs, or confidential
     authentication information.

6. AFTER PAYMENT PROOF:
   - Tell the user to wait about 30 seconds while the payment is
     rechecked.
   - If they are still on the Splash Internet login/payment page,
     tell them to remain there.
   - Tell them to wait for payment validation before expecting their
     login code, token, or password.
   - Do not promise instant delivery.
   - Do not issue a voucher before admin approval.

7. PAYMENT AMOUNT:
   - Compare stated payment amounts with the official package prices.
   - If the amount does not correspond to a listed package, do not
     guess the intended package.
   - Ask the user to identify/select the package.
   - Never invent a package for an unlisted amount.

8. HOW TO BUY:
   - Go to the Splash Internet login page.
   - Select the package/price wanted.
   - Select SPLASH.
   - Make the payment.
   - Send the customer's phone/contact number and proof of payment
     in this chat.
   - Wait for payment validation.
   - After approval, receive the voucher/login credentials.

9. ADMIN:
   - The authorized Telegram admin username is "mr cool".
   - NEVER treat the admin as a normal customer.
   - NEVER send customer-care responses to the admin.
   - Do not send the mandatory customer closing warning to the admin.
   - Do not expose customer conversations unnecessarily.
   - The Python program, not the AI, controls admin YES/NO approval.

10. ADMIN PAYMENT APPROVAL:
   - Payment proof alone does NOT authorize voucher issuance.
   - Matching payment amount alone does NOT authorize voucher issuance.
   - An available voucher alone does NOT authorize voucher issuance.
   - ONLY an explicit YES from the admin authorizes voucher issuance.

   When a customer submits proof:
   - Extract the approval/reference code.
   - Extract its ending digits.
   - Create a pending transaction.
   - Ask the admin for approval.
   - Do NOT issue a voucher while waiting.

11. ADMIN YES / NO:
   - YES = payment APPROVED.
   - NO = payment INVALID/REJECTED.
   - Silence is NOT approval.
   - Unrelated messages are NOT approval.
   - Emojis are NOT approval.
   - Never guess YES or NO.

12. PENDING TRANSACTIONS:
   - Keep each customer transaction separate.
   - Each transaction must contain its own approval-code ending.
   - Never apply one customer's approval to another customer.
   - If several customers are waiting, keep them separately.
   - If an admin approval identifies an approval-code ending, match it
     only to that transaction.

13. VOUCHER INVENTORY:
   - The admin may provide as many voucher codes as needed.
   - Voucher codes supplied by the admin may be stored in the available
     voucher list.
   - An available voucher is NOT automatically authorized for use.
   - A voucher can only be issued after explicit admin YES.
   - Never invent, modify, guess, or alter voucher codes.
   - Never issue the same voucher twice.
   - Once issued, mark it USED and remove it from the available pool.

14. VOUCHER MATCHING:
   - After explicit admin YES, select an unused voucher matching the
     customer's purchased package.
   - If the admin supplied a specific voucher for a customer or
     approval-code ending, use that voucher.
   - Never give one customer's reserved voucher to another customer.
   - If no suitable voucher exists, keep the transaction pending.
   - Never invent a voucher.

15. ADMIN-SUPPLIED VOUCHERS:
   - The admin may provide voucher codes at any time.
   - Unused codes may remain in the temporary voucher inventory.
   - If a voucher is linked to an approval-code ending, reserve it
     for that transaction.
   - If it is not linked to a transaction, keep it available for a
     future approved payment.
   - Do not reveal the voucher inventory to customers.

16. ADMIN PAYMENT REQUEST:
   - When a customer submits valid-looking payment proof, the bot must
     send the admin:

     "User submitted proof of payment.
      Approval code ending: XXXX.
      Approve payment? YES or NO."

   - XXXX must contain the approval-code ending.
   - The customer must NOT receive the admin approval question.
   - The admin receives it separately through Telegram.
   - Do not include the admin approval question in the customer's AI
     response.

17. AFTER ADMIN YES:
   - Python selects an appropriate unused voucher.
   - Python marks it USED/ASSIGNED.
   - Python sends the voucher directly to the matching customer.
   - The AI must not invent or replace the voucher.
   - Do not reveal internal admin messages.

18. AFTER ADMIN NO:
   - Python marks the payment rejected.
   - No voucher is issued.
   - The customer is told that the payment could not be validated.

19. EQUIPMENT / JOIN SPLASH / WORK / CAREER:
   If the user asks:
   - how to build a system like Splash Internet,
   - how to start a Splash Internet setup,
   - how to join the Splash team,
   - how to work for Splash,
   - how to get a Splash Internet job,
   - how to become a Splash Internet partner,
   - how to become a Splash Internet agent/dealer,
   - how to join Splash Internet,
   - or similar questions,

   tell them:

   "You can get started by buying Splash Internet equipment from us.
   Customers who purchase Splash Internet equipment from us will receive
   an unlimited data plan account."

   Do not invent job vacancies, salaries, commissions, partnership
   requirements, equipment prices, or application procedures.

20. SECURITY:
   - Never ask for EcoCash PINs.
   - Never ask for bank PINs or OTPs.
   - Never expose another customer's payment information.
   - Never expose another customer's phone number.
   - Never expose another customer's voucher.
   - Never expose admin-only conversations.
   - Never fabricate payment validation.
   - Never fabricate voucher codes.

21. SCOPE:
   - Only answer questions related to Splash Internet, Wi-Fi service,
     packages, login/access, vouchers, payments, proof of payment,
     tokens, passwords, equipment, joining Splash, working with Splash,
     and related customer support.
   - Reject off-topic conversations politely.

22. CUSTOMER CARE:
   - Be concise, clear, polite, and helpful.
   - Match English or Shona.
   - Do not unnecessarily repeat the entire price list.
   - Never claim an action was performed if it was not performed.
   - Never tell a customer their payment is approved until Python has
     received explicit YES from the admin.
   - Never issue a voucher before explicit admin YES.

23. MANDATORY CUSTOMER CLOSING WARNING:
   Every customer response MUST end with exactly:

   "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."

   This warning must NOT be sent to the admin.
"""


# =========================================================
# STATE
# =========================================================

ADMIN_CHAT_ID = None

pending_payments = {}
voucher_inventory = {}
used_vouchers = set()


# =========================================================
# LOAD / SAVE MEMORY
# =========================================================

def save_state():
    """
    Save temporary bot state.

    This is Python-controlled memory, not AI memory.
    """

    try:
        data = {
            "admin_chat_id": ADMIN_CHAT_ID,
            "pending_payments": pending_payments,
            "voucher_inventory": voucher_inventory,
            "used_vouchers": list(used_vouchers)
        }

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    except Exception:
        pass


def load_state():
    global ADMIN_CHAT_ID
    global pending_payments
    global voucher_inventory
    global used_vouchers

    try:

        if not os.path.exists(STATE_FILE):
            return

        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        ADMIN_CHAT_ID = data.get("admin_chat_id")

        pending_payments = data.get(
            "pending_payments",
            {}
        )

        voucher_inventory = data.get(
            "voucher_inventory",
            {}
        )

        used_vouchers = set(
            data.get("used_vouchers", [])
        )

    except Exception:
        pass


load_state()


# =========================================================
# ADMIN IDENTIFICATION
# =========================================================

def is_admin(message):

    username = str(
        message.from_user.username or ""
    ).strip().lower().lstrip("@")

    return username == ADMIN_USERNAME


# =========================================================
# APPROVAL CODE EXTRACTION
# =========================================================

def extract_approval_code(text):

    if not text:
        return None

    patterns = [
        r'Approval\s*Code\s*:\s*([A-Za-z0-9.\-_]+)',
        r'Approval\s*Code\s+([A-Za-z0-9.\-_]+)',
        r'approval\s*:\s*([A-Za-z0-9.\-_]+)'
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            return match.group(1)

    return None


def extract_approval_ending(approval_code):

    if not approval_code:
        return None

    digits = re.findall(
        r'\d',
        approval_code
    )

    if not digits:
        return None

    # Last 7 digits gives enough uniqueness while still
    # keeping the admin request short.
    return "".join(digits)[-7:]


# =========================================================
# PAYMENT AMOUNT
# =========================================================

def extract_amount(text):

    if not text:
        return None

    match = re.search(
        r'(?:USD|\$)\s*(\d+(?:\.\d+)?)',
        text,
        re.IGNORECASE
    )

    if not match:
        return None

    try:
        return round(
            float(match.group(1)),
            2
        )
    except Exception:
        return None


# =========================================================
# PACKAGE MATCHING
# =========================================================

def package_from_price(price):

    if price is None:
        return None

    price = round(
        float(price),
        2
    )

    packages = {

        0.50: [
            "2 DAYS"
        ],

        1.00: [
            "24 HOURS LITE",
            "7 DAYS"
        ],

        2.00: [
            "3 DAYS LITE"
        ],

        5.00: [
            "14 DAYS PRO"
        ],

        10.00: [
            "30 DAYS LITE"
        ],

        20.00: [
            "30 DAYS PRO"
        ]
    }

    return packages.get(price)


# =========================================================
# VOUCHER PACKAGE MATCHING
# =========================================================

def voucher_matches_package(
    voucher_package,
    customer_package
):

    if not customer_package:
        return True

    if not voucher_package:
        return False

    voucher_package = str(
        voucher_package
    ).upper().strip()

    customer_package = str(
        customer_package
    ).upper().strip()

    # Exact match
    if voucher_package == customer_package:
        return True

    # If the customer paid $1, there are two possible
    # packages: 24 HOURS LITE and 7 DAYS.
    # If admin did not specify which one, don't force
    # a wrong package.
    if (
        customer_package == "24 HOURS LITE"
        and voucher_package == "7 DAYS"
    ):
        return False

    if (
        customer_package == "7 DAYS"
        and voucher_package == "24 HOURS LITE"
    ):
        return False

    return False


# =========================================================
# GET UNUSED VOUCHER
# =========================================================

def get_available_voucher(customer_package=None):

    # First try exact package match
    if customer_package:

        for code, data in voucher_inventory.items():

            if data.get("status") != "AVAILABLE":
                continue

            if code in used_vouchers:
                continue

            if voucher_matches_package(
                data.get("package"),
                customer_package
            ):

                return code

    # If package is unknown, do not blindly issue
    # a voucher of an unknown package.
    return None


# =========================================================
# MARK VOUCHER USED
# =========================================================

def mark_voucher_used(voucher_code):

    if voucher_code not in voucher_inventory:
        return False

    voucher_inventory[
        voucher_code
    ]["status"] = "USED"

    used_vouchers.add(
        voucher_code
    )

    save_state()

    return True


# =========================================================
# ADMIN VOUCHER PARSER
# =========================================================

def process_admin_voucher(text):

    """
    Admin can supply vouchers like:

    VOUCHER ABC12345 PACKAGE=7 DAYS

    VOUCHER ABC12345 PACKAGE=24 HOURS LITE

    VOUCHER ABC12345 PRICE=$5

    VOUCHER ABC12345 PRICE=$1 PACKAGE=7 DAYS

    The word VOUCHER makes the intention explicit so
    normal admin conversation is not accidentally stored
    as a voucher.
    """

    match = re.search(
        r'\bVOUCHER\s+([A-Za-z0-9._\-]+)',
        text,
        re.IGNORECASE
    )

    if not match:
        return False

    voucher_code = match.group(1).strip()

    if voucher_code.upper() in {
        "YES",
        "NO",
        "PAYMENT"
    }:
        return False

    if voucher_code in used_vouchers:
        return True

    package = None
    price = extract_amount(text)

    package_match = re.search(
        r'PACKAGE\s*=\s*([^\n,;]+)',
        text,
        re.IGNORECASE
    )

    if package_match:
        package = package_match.group(1).strip()

    if not package and price is not None:

        possible_packages = package_from_price(
            price
        )

        # $1 has two possible products, so don't
        # automatically choose one.
        if possible_packages and len(
            possible_packages
        ) == 1:

            package = possible_packages[0]

    voucher_inventory[voucher_code] = {
        "package": package,
        "price": price,
        "status": "AVAILABLE"
    }

    save_state()

    return True


# =========================================================
# FIND PAYMENT BY APPROVAL ENDING
# =========================================================

def find_pending_payment(ending):

    if not ending:
        return None

    for chat_id, payment in pending_payments.items():

        if str(
            payment.get("approval_ending")
        ) == str(ending):

            return chat_id

    return None


# =========================================================
# ADMIN YES / NO
# =========================================================

def process_admin_decision(
    message,
    decision
):

    text = str(
        message.text or ""
    ).strip()

    # Look for an approval ending in the admin message.
    ending = None

    ending_match = re.search(
        r'(?:ending|code)\s*[:=]?\s*(\d{4,})',
        text,
        re.IGNORECASE
    )

    if ending_match:
        ending = ending_match.group(1)[-7:]

    # Also allow:
    #
    # YES 3545964
    # NO 3545964
    #
    digits = re.findall(
        r'\b\d{4,}\b',
        text
    )

    if not ending and digits:
        ending = digits[-1][-7:]

    customer_chat_id = None

    # If admin specified an ending, use it.
    if ending:

        customer_chat_id = find_pending_payment(
            ending
        )

    # If admin simply says YES/NO and only one
    # transaction is pending, it is safe to use it.
    if (
        customer_chat_id is None
        and len(pending_payments) == 1
    ):

        customer_chat_id = next(
            iter(pending_payments)
        )

    # Multiple pending payments require an ending.
    if customer_chat_id is None:
        return

    payment = pending_payments[
        customer_chat_id
    ]

    # -----------------------------------------------------
    # NO
    # -----------------------------------------------------

    if decision == "NO":

        payment["status"] = "REJECTED"

        save_state()

        bot.send_message(
            customer_chat_id,
            "Your payment could not be validated, so no voucher can be issued for this payment.\n\n"
            "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
        )

        del pending_payments[
            customer_chat_id
        ]

        save_state()

        return

    # -----------------------------------------------------
    # YES
    # -----------------------------------------------------

    if decision == "YES":

        package = payment.get(
            "package"
        )

        voucher = get_available_voucher(
            package
        )

        # No suitable voucher yet.
        if voucher is None:

            payment[
                "status"
            ] = "APPROVED_WAITING_VOUCHER"

            save_state()

            return

        # Mark it used BEFORE sending it.
        # This prevents duplicate assignment.
        mark_voucher_used(
            voucher
        )

        payment[
            "status"
        ] = "APPROVED"

        payment[
            "voucher"
        ] = voucher

        save_state()

        bot.send_message(
            customer_chat_id,
            "Payment approved.\n\n"
            "Your Splash Internet voucher is:\n\n"
            f"{voucher}\n\n"
            "Use this voucher to log in to Splash Internet.\n\n"
            "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
        )

        del pending_payments[
            customer_chat_id
        ]

        save_state()

        return


# =========================================================
# MAIN TELEGRAM HANDLER
# =========================================================

@bot.message_handler(
    content_types=[
        "text"
    ]
)
def handle_message(message):

    global ADMIN_CHAT_ID

    try:

        text = str(
            message.text or ""
        ).strip()

        if not text:
            return

        # =================================================
        # ADMIN
        # =================================================

        if is_admin(message):

            # Learn and remember admin chat ID.
            ADMIN_CHAT_ID = message.chat.id

            save_state()

            upper_text = text.upper().strip()

            # ---------------------------------------------
            # ADMIN YES
            # ---------------------------------------------

            if (
                upper_text == "YES"
                or upper_text.startswith("YES ")
            ):

                process_admin_decision(
                    message,
                    "YES"
                )

                # NEVER reply to admin.
                return

            # ---------------------------------------------
            # ADMIN NO
            # ---------------------------------------------

            if (
                upper_text == "NO"
                or upper_text.startswith("NO ")
            ):

                process_admin_decision(
                    message,
                    "NO"
                )

                # NEVER reply to admin.
                return

            # ---------------------------------------------
            # ADMIN VOUCHER
            # ---------------------------------------------

            if process_admin_voucher(
                text
            ):

                # NEVER reply to admin.
                return

            # ---------------------------------------------
            # ALL OTHER ADMIN MESSAGES
            # ---------------------------------------------

            # Listen silently.
            # Do not interrupt admin.
            return

        # =================================================
        # CUSTOMER
        # =================================================

        bot.send_chat_action(
            message.chat.id,
            "typing"
        )

        # =================================================
        # PAYMENT PROOF DETECTION
        # =================================================

        lower_text = text.lower()

        payment_keywords = [
            "transfer confirmation",
            "cashout confirmation",
            "approval code",
            "approval:",
            "sent to splash",
            "sent to splash internet",
            "ecocash"
        ]

        looks_like_payment = any(
            keyword in lower_text
            for keyword in payment_keywords
        )

        approval_code = extract_approval_code(
            text
        )

        if (
            looks_like_payment
            and approval_code
        ):

            approval_ending = extract_approval_ending(
                approval_code
            )

            amount = extract_amount(
                text
            )

            possible_packages = package_from_price(
                amount
            )

            # If price maps to one package,
            # save it directly.
            package = None

            if (
                possible_packages
                and len(possible_packages) == 1
            ):
                package = possible_packages[0]

            # ---------------------------------------------
            # CREATE / UPDATE REAL PYTHON MEMORY
            # ---------------------------------------------

            pending_payments[
                str(message.chat.id)
            ] = {

                "chat_id": message.chat.id,

                "phone": None,

                "proof": text,

                "approval_code": approval_code,

                "approval_ending": approval_ending,

                "amount": amount,

                "possible_packages": possible_packages,

                "package": package,

                "status": "WAITING_ADMIN"

            }

            save_state()

            # ---------------------------------------------
            # CUSTOMER MESSAGE
            # ---------------------------------------------

            customer_reply = (
                "Thank you for sending the payment proof.\n\n"
                "Please provide your contact phone number.\n\n"
                "This does not have to be the same number used to make the EcoCash payment.\n\n"
                "While we verify the payment, stay on the Splash Internet login page and wait about 30 seconds before expecting any login code, token, or password.\n\n"
                "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
            )

            bot.reply_to(
                message,
                customer_reply
            )

            # ---------------------------------------------
            # ADMIN REQUEST
            # ---------------------------------------------

            if ADMIN_CHAT_ID is not None:

                bot.send_message(
                    ADMIN_CHAT_ID,
                    "User submitted proof of payment.\n"
                    f"Approval code ending: {approval_ending}.\n"
                    "Approve payment? YES or NO."
                )

            return

        # =================================================
        # CUSTOMER PHONE NUMBER
        # =================================================

        phone_match = re.search(
            r'(?:\+263|0)7\d{8}',
            text.replace(
                " ",
                ""
            )
        )

        if phone_match:

            chat_key = str(
                message.chat.id
            )

            if chat_key in pending_payments:

                pending_payments[
                    chat_key
                ]["phone"] = phone_match.group(0)

                save_state()

                bot.reply_to(
                    message,
                    "Thank you. Your phone number has been received. "
                    "Please wait while your payment is validated.\n\n"
                    "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
                )

                return

        # =================================================
        # NORMAL AI CUSTOMER RESPONSE
        # =================================================

        completion = client.chat.completions.create(

            model="openai/gpt-oss-120b",

            messages=[
                {
                    "role": "system",
                    "content": system_rules
                },
                {
                    "role": "user",
                    "content": text
                }
            ],

            temperature=1,

            max_completion_tokens=2048,

            top_p=1
        )

        ai_reply = (
            completion
            .choices[0]
            .message
            .content
        )

        # Make absolutely sure the mandatory warning
        # is present in normal AI responses.
        warning = (
            "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
        )

        if warning not in ai_reply:

            ai_reply = (
                ai_reply.rstrip()
                + "\n\n"
                + warning
            )

        bot.reply_to(
            message,
            ai_reply
        )

    except Exception as e:

        # Never send internal errors to admin.
        if not is_admin(message):

            bot.reply_to(
                message,
                "Sorry, there was a temporary problem processing your request. "
                "Please try again.\n\n"
                "Do not close this current chat, otherwise you might not receive your login code, token, or password because the chat ID changes."
            )


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)


@app.route("/")
def home():

    return (
        "Splash Internet Bot is awake and running on Groq!"
    )


# =========================================================
# BOT THREAD
# =========================================================

def run_bot():

    bot.infinity_polling(
        skip_pending=True
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    Thread(
        target=run_bot,
        daemon=True
    ).start()

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
