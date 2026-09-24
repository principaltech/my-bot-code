# =============================================================================
#  splash_bot.py  —  STOCK-RESERVATION FIX   (4 edits; everything else unchanged)
# =============================================================================
#  SYMPTOM
#    Admin bot:  "⏰ Reminder: ... No stock code is reserved yet — reply with a
#    voucher code to approve, or 'no' to reject."  ...while /stock shows 9 codes
#    for that very package.
#
#  CAUSE
#    A stock code was reserved only ONCE: at the moment the first [ADMIN_ALERT] was
#    created. If the package had no stock at that instant (or you answered NO to the
#    stored code), the request stayed at "no code reserved" for good - reminders never
#    looked at stock again. Worse, replying YES in that state sent the literal word
#    "YES" to the customer as their voucher code (and burned their payment reference).
#
#    Second cause of the same symptom: "$5 = Unlimited 14d" and "$10 = Unlimited 30d"
#    could never match their own stock lists (mixed-case keys vs. an UPPER-cased
#    lookup), so those two packages were always reported OUT OF STOCK.
#
#  HOW TO APPLY: make the 4 edits below, top to bottom. No new imports are needed.
# =============================================================================


# -----------------------------------------------------------------------------
# EDIT 1 of 4 — REPLACE the existing `normalize_package` function with this.
# -----------------------------------------------------------------------------
_PACKAGE_BY_UPPER = {k.upper(): k for k in PACKAGES}

def normalize_package(text):
    if not text:
        return None
    t = re.sub(r'\s+', ' ', text.strip()).upper()
    # Exact match, case-insensitively (the old `t in PACKAGES` could never match the
    # mixed-case keys "$5 = Unlimited 14d" / "$10 = Unlimited 30d").
    if t in _PACKAGE_BY_UPPER:
        return _PACKAGE_BY_UPPER[t]
    candidates = [k for k in PACKAGES if t in k or k in t]
    return candidates[0] if len(candidates) == 1 else None


# -----------------------------------------------------------------------------
# EDIT 2 of 4 — ADD this whole block at module level, e.g. right AFTER the existing
#               `delete_voucher(...)` function (just above `ADD_CODE_PATTERN = ...`).
# -----------------------------------------------------------------------------
_reserve_lock = Lock()

def resolve_package_key(info):
    """The canonical PACKAGES key for a pending request, or None if it can't be worked out.
    Also heals older records whose package_key was left empty by the normalize_package bug."""
    for candidate in (info.get("package_key"), info.get("package")):
        if candidate:
            key = normalize_package_compact(candidate)
            if key in PACKAGES:
                return key
    return None

def try_reserve_stock_for_pending(customer_key):
    """Reserve one stock code for this customer's open request if it has none yet.
    Returns True only if a code was newly reserved just now. Never touches a request the
    admin already answered NO to (stock_declined) - that means 'I'll supply a code myself'."""
    with _reserve_lock:
        info = pending_approvals.get(customer_key)
        if not info or info.get("proposed_code") or info.get("stock_declined"):
            return False
        pkg_key = resolve_package_key(info)
        if not pkg_key:
            return False
        code = reserve_voucher(pkg_key)
        if not code:
            return False
        info["package_key"] = pkg_key
        info["proposed_code"] = code
        return True

def reply_hint(customer_key):
    """('YES', 'NO') when only one customer is waiting. With several waiting, the same words
    followed by this customer's code ending, so the admin's reply says who it is about."""
    ending = re.sub(r'[^A-Za-z0-9]', '', (pending_approvals.get(customer_key) or {}).get("code_ending") or "")[-7:]
    if len(pending_approvals) > 1 and ending:
        return f"YES {ending}", f"NO {ending}"
    return "YES", "NO"

def strip_customer_ref(text, code_ending):
    """Remove the token an admin used to name a customer (their payment code ending, or the whole
    approval code containing it) so that what is left is just the decision or voucher code:
        'YES 3928909' -> 'YES'    '3928909 no' -> 'no'    '3928909 AB12CD3' -> 'AB12CD3'
    Returns '' if nothing else was typed."""
    end = re.sub(r'[^a-z0-9]', '', (code_ending or "").lower())[-7:]
    text = (text or "").strip()
    if len(end) < 7:
        return text
    kept = []
    for tok in text.split():
        norm = re.sub(r'[^a-z0-9]', '', tok.lower())
        if len(norm) >= 7 and end in norm:
            continue
        kept.append(tok)
    return " ".join(kept).strip()

def handle_pending_decision(message, text):
    """The admin's reply about a waiting customer: YES / NO / a voucher code.
    This is the old tail of handle_admin_message, moved here and made stock-aware."""
    target_key = find_target_customer(text)

    if target_key is None:
        if not pending_approvals:
            admin_bot.reply_to(message, "⚠️ No customers are currently waiting for approval.")
        else:
            pending_list = "\n".join(
                f"- {customer_display.get(cid, cid)}: code ending {info.get('code_ending') or '?'}, "
                f"{info.get('package') or '?'}, phone {info.get('phone') or '?'}"
                for cid, info in pending_approvals.items()
            )
            admin_bot.reply_to(
                message,
                "⚠️ Multiple customers are waiting — please include the code ending to say which "
                "one, e.g. YES 3928909:\n\n"
                f"{pending_list}"
            )
        return

    info = pending_approvals.get(target_key, {})
    target_chat_id = customer_chat_id.get(target_key)
    reply_id = user_last_message_id.get(target_key)
    target_label = customer_display.get(target_key, target_key)

    if target_chat_id is None:
        if info.get("proposed_code"):
            return_voucher(info.get("package_key"), info["proposed_code"])
        admin_bot.reply_to(message, f"⚠️ Lost track of chat for {target_label}; they'll need to message again.")
        pending_approvals.pop(target_key, None)
        save_state()
        return

    def send_to_customer(msg_text):
        if reply_id:
            customer_bot.send_message(target_chat_id, msg_text, reply_to_message_id=reply_id)
        else:
            customer_bot.send_message(target_chat_id, msg_text)

    # "YES 3928909" / "3928909 NO" / "3928909 ABC123": drop the code-ending token that named
    # the customer, so what is left is just the decision or the voucher code.
    reply_text = strip_customer_ref(text, info.get("code_ending"))
    decision = reply_text.lower().strip(" .,!")

    # YES while nothing is reserved yet: take a code from stock now (that is what the admin
    # means) instead of falling through and sending the word "YES" as the voucher code.
    if not info.get("proposed_code") and decision in YES_WORDS:
        if not info.get("stock_declined"):
            try_reserve_stock_for_pending(target_key)
        if not info.get("proposed_code"):
            why = ("you put the stored code back earlier" if info.get("stock_declined")
                   else f"there is no stock code for {info.get('package') or 'their package'}")
            admin_bot.reply_to(
                message,
                f"⚠️ I did NOT send anything to {target_label}: {why}. "
                "Reply with the voucher code itself, or 'no' to reject."
            )
            return

    if info.get("proposed_code"):
        if decision in YES_WORDS:
            code = info["proposed_code"]
            package = info.get("package") or "your package"
            voucher_message = (
                f"✅ Your payment has been approved!\n\n"
                f"Package: {package}\n"
                f"Voucher code: {code}\n\n"
                + CLOSING_WARNING
            )
            history = user_memory.setdefault(target_key, [{"role": "system", "content": system_rules}])
            note_voucher_delivered(history, package)
            send_to_customer(voucher_message)

            mark_payment_used(info)
            admin_bot.reply_to(message, f"✅ Delivered stored code '{code}' to {target_label} instantly.")
            pending_approvals.pop(target_key, None)
            save_state()
            return

        elif decision in NO_WORDS:
            return_voucher(info.get("package_key"), info["proposed_code"])
            pending_approvals[target_key]["proposed_code"] = None
            pending_approvals[target_key]["stock_declined"] = True   # don't auto-reserve again
            admin_bot.reply_to(
                message,
                f"↩️ Put that code back in stock. {target_label} is still waiting — "
                "please reply with a different voucher code for them."
            )
            save_state()
            return

        else:
            yes_word, no_word = reply_hint(target_key)
            admin_bot.reply_to(
                message,
                f"❓ {target_label} has a stored code ('{info['proposed_code']}') awaiting your confirmation. "
                f"Reply exactly {yes_word} to send it, or {no_word} to put it back in stock."
            )
            return

    history = user_memory.setdefault(target_key, [{"role": "system", "content": system_rules}])

    if decision in NO_WORDS:
        rejection_message = (
            "❌ Unfortunately we could not verify your payment. "
            "Please double-check your proof of payment and try again, or contact support.\n\n"
            + CLOSING_WARNING
        )
        history.append({"role": "assistant", "content": rejection_message})
        send_to_customer(rejection_message)

        admin_bot.reply_to(message, f"❌ Rejection sent to {target_label}.")
        pending_approvals.pop(target_key, None)
        save_state()
        return

    code = reply_text.strip()

    if len(code.split()) != 1:
        admin_bot.reply_to(
            message,
            f"⚠️ That doesn't look like a single voucher code, so I did NOT send it to {target_label}. "
            "Send only the code (no spaces), or 'no' to reject."
        )
        return

    package = info.get("package") or "your package"
    voucher_message = (
        f"✅ Your payment has been approved!\n\n"
        f"Package: {package}\n"
        f"Voucher code: {code}\n\n"
        + CLOSING_WARNING
    )
    note_voucher_delivered(history, package)
    send_to_customer(voucher_message)

    # A code typed by hand may also be sitting in stock (e.g. copied from /stockfull).
    # Take it out so the same voucher can't be sold twice.
    removed_from = delete_voucher(code)
    mark_payment_used(info)
    admin_bot.reply_to(
        message,
        f"✅ Delivered code '{code}' to {target_label} instantly."
        + (f"\n📦 It was also in stock ({removed_from}) - removed so it can't be reused." if removed_from else "")
    )
    pending_approvals.pop(target_key, None)
    save_state()


# -----------------------------------------------------------------------------
# EDIT 3 of 4 — REPLACE the existing `maybe_bump_admin_for_pending` function with this.
# -----------------------------------------------------------------------------
def maybe_bump_admin_for_pending(customer_key, text, label):
    """
    Call this for any message that reconciled to an existing, already-alerted,
    unresolved transaction and carries no new information (no ref, no phone,
    no package). We deliberately do NOT require specific wording here -
    "resend", "resubmission", "re-verify", "check again", or literally
    anything else the customer types in that state means the same thing:
    they're still waiting and the admin needs a nudge. If it's been a while
    since we last alerted, send a low-key reminder - never a second full
    "PAYMENT APPROVAL" alert, and never anything implying approval.
    Returns True if it sent a reminder.

    Each nudge also looks at stock again: the first alert only tried to reserve a code
    once, so a request raised while the package was empty would otherwise say
    "no stock code" forever, even after codes were added.
    """
    info = pending_approvals.get(customer_key)
    if not info or not info.get("alert_sent"):
        return False

    now = time.time()
    last = info.get("last_alert_time", 0)
    if now - last < REBUMP_COOLDOWN_SECONDS:
        return False  # already nudged recently, don't spam

    newly_reserved = try_reserve_stock_for_pending(customer_key)
    note = probable_match_note(info.get("code_ending"))

    if admin_chat_id:
        yes_word, no_word = reply_hint(customer_key)
        if info.get("proposed_code"):
            reserved_line = ("A code from stock has just been reserved for them"
                             if newly_reserved else "A stored code is already reserved for them")
            admin_bot.send_message(
                admin_chat_id,
                f"⏰ Reminder: {label} is still waiting on their voucher "
                f"(code ending {info.get('code_ending')}, {info.get('package')}, "
                f"phone {info.get('phone')}). {reserved_line} "
                f"('{info['proposed_code']}') — reply {yes_word} to send it, or {no_word} to hold it."
                + note
            )
        else:
            if info.get("stock_declined"):
                status = "You put the stored code back earlier, so none is reserved"
            elif not resolve_package_key(info):
                status = "I couldn't match this request's package to a stock list, so nothing is reserved"
            else:
                status = "There is no stock code available for this package"
            admin_bot.send_message(
                admin_chat_id,
                f"⏰ Reminder: {label} is still waiting on a decision "
                f"(code ending {info.get('code_ending')}, {info.get('package')}, "
                f"phone {info.get('phone')}). {status} — reply with a voucher code to approve "
                f"(or add stock and reply {yes_word}), or {no_word} to reject."
                + note
            )
    info["last_alert_time"] = now
    save_state()
    return True


# -----------------------------------------------------------------------------
# EDIT 4 of 4 — in `handle_admin_message`, DELETE everything from the line
#
#       target_key = find_target_customer(text)
#
#   down to the end of that function (its last line is `save_state()`, just above the
#   "# SERVER AND MULTI-THREADING" banner), and put this single line in its place
#   (4-space indent, same as the code above it):
#
#       handle_pending_decision(message, text)
#
# -----------------------------------------------------------------------------
