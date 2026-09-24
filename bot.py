import os
import logging
from datetime import datetime, timedelta, timezone
from threading import Thread

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# ============================================================
# RENDER KEEP-ALIVE WEB SERVER
# ============================================================
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is running and healthy!", 200


@app.route("/health")
def health():
    return "OK", 200


def run_web():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


def keep_alive():
    Thread(target=run_web, daemon=True).start()


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================
def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


BOT_TOKEN = required_env("BOT_TOKEN")
MONGO_URI = required_env("MONGO_URI")
UPI_ID = required_env("UPI_ID")
CONTACT_USERNAME = required_env("CONTACT_USERNAME").lstrip("@")

try:
    ADMIN_ID = int(required_env("ADMIN_ID"))
except ValueError as exc:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID.") from exc

# ============================================================
# TELEGRAM + MONGODB
# ============================================================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
    client.admin.command("ping")
except Exception as exc:
    raise RuntimeError(f"MongoDB connection failed: {exc}") from exc

db = client["sub_management"]
channels_col = db["channels"]
users_col = db["users"]

# Helpful indexes
channels_col.create_index("channel_id", unique=True)
users_col.create_index([("channel_id", 1), ("user_id", 1)], unique=True)
users_col.create_index("expiry")


# ============================================================
# HELPERS
# ============================================================
def format_duration(minutes: int) -> str:
    minutes = int(minutes)

    if minutes < 60:
        return f"{minutes} Min"

    if minutes < 1440:
        hours = minutes // 60
        return f"{hours} Hour" if hours == 1 else f"{hours} Hours"

    days = minutes // 1440
    return f"{days} Day" if days == 1 else f"{days} Days"


def contact_url() -> str:
    return f"https://t.me/{CONTACT_USERNAME}"


def admin_only(user_id: int) -> bool:
    return user_id == ADMIN_ID


# ============================================================
# /START
# ============================================================
@bot.message_handler(commands=["start"])
def start_handler(message):
    user_id = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)

    # User entry through a deep link:
    # https://t.me/BOT_USERNAME?start=CHANNEL_ID
    if len(parts) > 1:
        try:
            ch_id = int(parts[1].strip())
            ch_data = channels_col.find_one({"channel_id": ch_id})

            if ch_data:
                plans = ch_data.get("plans", {})
                markup = InlineKeyboardMarkup()

                for p_time, p_price in plans.items():
                    minutes = int(p_time)
                    label = format_duration(minutes)
                    markup.add(
                        InlineKeyboardButton(
                            f"💳 {label} - ₹{p_price}",
                            callback_data=f"select_{ch_id}_{minutes}"
                        )
                    )

                markup.add(
                    InlineKeyboardButton(
                        "📞 Contact Admin",
                        url=contact_url()
                    )
                )

                bot.send_message(
                    message.chat.id,
                    f"Welcome!\n\n"
                    f"You are joining: *{ch_data.get('name', 'Channel')}*.\n\n"
                    f"Please select a subscription plan below:",
                    reply_markup=markup,
                    parse_mode="Markdown"
                )
                return

        except (ValueError, TypeError) as exc:
            logger.warning("Invalid /start payload: %s", exc)

    # Admin panel
    if admin_only(user_id):
        bot.send_message(
            message.chat.id,
            "✅ Admin Panel Active!\n\n"
            "/add - Add/Edit Channel & Prices\n"
            "/channels - Manage Existing Channels"
        )
    else:
        bot.send_message(
            message.chat.id,
            "Welcome! To join a channel, please use the link provided by the Admin."
        )


# ============================================================
# ADMIN: LIST CHANNELS
# ============================================================
@bot.message_handler(commands=["channels"], func=lambda m: admin_only(m.from_user.id))
def list_channels(message):
    markup = InlineKeyboardMarkup()
    cursor = channels_col.find({"admin_id": ADMIN_ID})
    count = 0

    for ch in cursor:
        markup.add(
            InlineKeyboardButton(
                f"Channel: {ch.get('name', 'Unnamed')}",
                callback_data=f"manage_{ch['channel_id']}"
            )
        )
        count += 1

    markup.add(
        InlineKeyboardButton("➕ Add New Channel", callback_data="add_new")
    )

    if count == 0:
        bot.send_message(
            ADMIN_ID,
            "No channels found. Click below to add one.",
            reply_markup=markup
        )
    else:
        bot.send_message(
            ADMIN_ID,
            "Your Managed Channels:",
            reply_markup=markup
        )


# ============================================================
# ADMIN: ADD CHANNEL
# ============================================================
@bot.message_handler(commands=["add"], func=lambda m: admin_only(m.from_user.id))
def add_channel_start(message):
    msg = bot.send_message(
        ADMIN_ID,
        "Please ensure the bot is an Admin in your channel, then "
        "FORWARD any message from that channel here."
    )
    bot.register_next_step_handler(msg, get_plans)


@bot.callback_query_handler(func=lambda call: call.data == "add_new")
def cb_add_new(call):
    if not admin_only(call.from_user.id):
        bot.answer_callback_query(call.id, "Not authorized.", show_alert=True)
        return

    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        ADMIN_ID,
        "Please FORWARD any message from your channel here."
    )
    bot.register_next_step_handler(msg, get_plans)


def get_plans(message):
    if message.from_user.id != ADMIN_ID:
        return

    if getattr(message, "forward_from_chat", None):
        ch_id = message.forward_from_chat.id
        ch_name = message.forward_from_chat.title or "Unnamed Channel"

        msg = bot.send_message(
            ADMIN_ID,
            f"Channel Detected: *{ch_name}*\n\n"
            "Enter plans in format (Minutes:Price):\n"
            "`Min:Price, Min:Price`\n\n"
            "Example:\n"
            "`1440:99, 43200:199` (1 Day and 30 Days)",
            parse_mode="Markdown"
        )
        bot.register_next_step_handler(
            msg, finalize_channel, ch_id, ch_name
        )
    else:
        bot.send_message(
            ADMIN_ID,
            "❌ Error: Message was not forwarded. Use /add to try again."
        )


def finalize_channel(message, ch_id, ch_name):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        if not message.text:
            raise ValueError("Please send the plans as text.")

        raw_plans = message.text.strip().split(",")
        plans_dict = {}

        for plan in raw_plans:
            plan = plan.strip()

            if ":" not in plan:
                raise ValueError(
                    f"Invalid plan: {plan}\n"
                    "Expected format: Minutes:Price"
                )

            minutes_text, price_text = plan.split(":", 1)
            minutes_text = minutes_text.strip()
            price_text = price_text.strip()

            if not minutes_text.isdigit():
                raise ValueError(
                    f"Minutes must be a number: {minutes_text}"
                )

            if not price_text.isdigit():
                raise ValueError(
                    f"Price must be a number: {price_text}"
                )

            minutes = int(minutes_text)
            price = int(price_text)

            if minutes <= 0:
                raise ValueError("Minutes must be greater than 0.")

            if price <= 0:
                raise ValueError("Price must be greater than 0.")

            plans_dict[str(minutes)] = str(price)

        if not plans_dict:
            raise ValueError("No valid plans found.")

        channels_col.update_one(
            {"channel_id": ch_id},
            {
                "$set": {
                    "name": ch_name,
                    "plans": plans_dict,
                    "admin_id": ADMIN_ID
                }
            },
            upsert=True
        )

        bot_username = bot.get_me().username

        plan_lines = "\n".join(
            f"• {format_duration(int(mins))} → ₹{price}"
            for mins, price in sorted(
                plans_dict.items(), key=lambda x: int(x[0])
            )
        )

        bot.send_message(
            ADMIN_ID,
            "✅ *Setup Successful!*\n\n"
            f"📢 Channel: *{ch_name}*\n\n"
            f"💳 Plans:\n{plan_lines}\n\n"
            "🔗 *Invite Link:*\n"
            f"https://t.me/{bot_username}?start={ch_id}",
            parse_mode="Markdown"
        )

    except ValueError as exc:
        bot.send_message(
            ADMIN_ID,
            "❌ *Invalid plan format.*\n\n"
            f"Reason: {exc}\n\n"
            "Please use:\n"
            "`Minutes:Price, Minutes:Price`\n\n"
            "Example:\n"
            "`1440:99, 43200:199`",
            parse_mode="Markdown"
        )

    except Exception as exc:
        logger.exception("FINALIZE ERROR")
        bot.send_message(
            ADMIN_ID,
            "⚠️ *Bot/System Error*\n\n"
            f"{exc}",
            parse_mode="Markdown"
        )


# ============================================================
# USER: SELECT PLAN / PAYMENT
# ============================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("select_"))
def user_pays(call):
    try:
        _, ch_id_text, mins_text = call.data.split("_", 2)
        ch_id = int(ch_id_text)
        mins = int(mins_text)

        ch_data = channels_col.find_one({"channel_id": ch_id})
        if not ch_data:
            bot.answer_callback_query(
                call.id, "Channel not found.", show_alert=True
            )
            return

        price = ch_data["plans"].get(str(mins))
        if price is None:
            bot.answer_callback_query(
                call.id, "Plan no longer exists.", show_alert=True
            )
            return

        # UPI deep link. requests-compatible QR URL.
        upi_link = (
            f"upi://pay?pa={UPI_ID}"
            f"&am={price}"
            f"&cu=INR"
        )

        from urllib.parse import quote
        qr_url = (
            "https://api.qrserver.com/v1/create-qr-code/"
            f"?size=300x300&data={quote(upi_link, safe='')}"
        )

        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton(
                "✅ I Have Paid",
                callback_data=f"paid_{ch_id}_{mins}"
            )
        )
        markup.add(
            InlineKeyboardButton(
                "📞 Contact Admin",
                url=contact_url()
            )
        )

        bot.answer_callback_query(call.id)

        bot.send_photo(
            call.message.chat.id,
            qr_url,
            caption=(
                f"Plan: {format_duration(mins)}\n"
                f"Price: ₹{price}\n"
                f"UPI ID: `{UPI_ID}`\n\n"
                "Please complete the payment and click "
                "'I Have Paid'."
            ),
            reply_markup=markup,
            parse_mode="Markdown"
        )

    except Exception as exc:
        logger.exception("PAYMENT ERROR")
        bot.answer_callback_query(
            call.id, "Unable to process this plan.", show_alert=True
        )


# ============================================================
# USER: PAYMENT NOTIFICATION
# ============================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("paid_"))
def admin_notify(call):
    try:
        _, ch_id_text, mins_text = call.data.split("_", 2)
        ch_id = int(ch_id_text)
        mins = int(mins_text)

        user = call.from_user
        ch_data = channels_col.find_one({"channel_id": ch_id})

        if not ch_data:
            bot.answer_callback_query(
                call.id, "Channel not found.", show_alert=True
            )
            return

        price = ch_data["plans"].get(str(mins))
        if price is None:
            bot.answer_callback_query(
                call.id, "Plan no longer exists.", show_alert=True
            )
            return

        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton(
                "✅ Approve",
                callback_data=f"app_{user.id}_{ch_id}_{mins}"
            )
        )
        markup.add(
            InlineKeyboardButton(
                "❌ Reject",
                callback_data=f"rej_{user.id}"
            )
        )

        bot.send_message(
            ADMIN_ID,
            "🔔 *Payment Verification Required!*\n\n"
            f"User: {user.first_name}\n"
            f"User ID: `{user.id}`\n"
            f"Channel: {ch_data.get('name', 'Channel')}\n"
            f"Plan: {format_duration(mins)}\n"
            f"Price: ₹{price}",
            reply_markup=markup,
            parse_mode="Markdown"
        )

        u_markup = InlineKeyboardMarkup()
        u_markup.add(
            InlineKeyboardButton(
                "📞 Contact Admin",
                url=contact_url()
            )
        )

        bot.answer_callback_query(
            call.id, "Payment request sent to Admin."
        )
        bot.send_message(
            call.message.chat.id,
            "✅ Your payment request has been sent. "
            "Please wait for Admin approval.",
            reply_markup=u_markup
        )

    except Exception:
        logger.exception("PAYMENT NOTIFICATION ERROR")
        bot.answer_callback_query(
            call.id, "Could not send the request.", show_alert=True
        )


# ============================================================
# ADMIN: APPROVE
# ============================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("app_"))
def approve_now(call):
    if not admin_only(call.from_user.id):
        bot.answer_callback_query(
            call.id, "Not authorized.", show_alert=True
        )
        return

    try:
        _, u_id_text, ch_id_text, mins_text = call.data.split("_", 3)

        u_id = int(u_id_text)
        ch_id = int(ch_id_text)
        mins = int(mins_text)

        ch_data = channels_col.find_one({"channel_id": ch_id})
        if not ch_data:
            raise ValueError("Channel not found in database.")

        if str(mins) not in ch_data.get("plans", {}):
            raise ValueError("Selected plan no longer exists.")

        expiry_datetime = datetime.now(timezone.utc) + timedelta(minutes=mins)
        expiry_ts = int(expiry_datetime.timestamp())

        # Bot must be an administrator with permission to invite users.
        link = bot.create_chat_invite_link(
            ch_id,
            member_limit=1,
            expire_date=expiry_ts
        )

        users_col.update_one(
            {"user_id": u_id, "channel_id": ch_id},
            {
                "$set": {
                    "user_id": u_id,
                    "channel_id": ch_id,
                    "expiry": expiry_ts
                }
            },
            upsert=True
        )

        bot.send_message(
            u_id,
            "🥳 *Payment Approved!*\n\n"
            f"Subscription: {format_duration(mins)}\n\n"
            f"Join Link: {link.invite_link}\n\n"
            f"⚠️ This link/access expires in {format_duration(mins)}.",
            parse_mode="Markdown"
        )

        bot.answer_callback_query(call.id, "Approved.")

        bot.edit_message_text(
            f"✅ Approved user {u_id} for {format_duration(mins)}.",
            call.message.chat.id,
            call.message.message_id
        )

    except Exception as exc:
        logger.exception("APPROVAL ERROR")
        bot.answer_callback_query(
            call.id, "Approval failed. Check Admin logs.", show_alert=True
        )
        bot.send_message(ADMIN_ID, f"❌ Approval Error:\n{exc}")


# ============================================================
# ADMIN: REJECT
# ============================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("rej_"))
def reject_payment(call):
    if not admin_only(call.from_user.id):
        bot.answer_callback_query(
            call.id, "Not authorized.", show_alert=True
        )
        return

    try:
        _, u_id_text = call.data.split("_", 1)
        u_id = int(u_id_text)

        bot.send_message(
            u_id,
            "❌ Your payment request was rejected by the Admin.\n\n"
            "If you believe this is an error, please contact the Admin."
        )

        bot.answer_callback_query(call.id, "Payment rejected.")

        bot.edit_message_text(
            f"❌ Payment rejected for user {u_id}.",
            call.message.chat.id,
            call.message.message_id
        )

    except Exception as exc:
        logger.exception("REJECTION ERROR")
        bot.answer_callback_query(
            call.id, "Rejection failed.", show_alert=True
        )


# ============================================================
# ADMIN: MANAGE CHANNEL
# ============================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("manage_"))
def manage_ch(call):
    if not admin_only(call.from_user.id):
        bot.answer_callback_query(
            call.id, "Not authorized.", show_alert=True
        )
        return

    try:
        ch_id = int(call.data.split("_", 1)[1])
        ch_data = channels_col.find_one(
            {"channel_id": ch_id, "admin_id": ADMIN_ID}
        )

        if not ch_data:
            bot.answer_callback_query(
                call.id, "Channel not found.", show_alert=True
            )
            return

        bot_username = bot.get_me().username
        link = f"https://t.me/{bot_username}?start={ch_id}"

        bot.answer_callback_query(call.id)

        bot.edit_message_text(
            f"Settings for: *{ch_data['name']}*\n\n"
            f"Your Link: `{link}`\n\n"
            "To edit prices, use /add and forward a message "
            "from this channel again.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown"
        )

    except Exception as exc:
        logger.exception("MANAGE CHANNEL ERROR")
        bot.answer_callback_query(
            call.id, "Unable to open channel settings.", show_alert=True
        )


# ============================================================
# AUTOMATIC EXPIRY / KICK
# ============================================================
def kick_expired_users():
    now = int(datetime.now(timezone.utc).timestamp())

    try:
        expired_users = list(
            users_col.find({"expiry": {"$lte": now}})
        )
    except Exception:
        logger.exception("Could not query expired users.")
        return

    for user in expired_users:
        user_id = user.get("user_id")
        channel_id = user.get("channel_id")

        if not user_id or not channel_id:
            users_col.delete_one({"_id": user["_id"]})
            continue

        try:
            # Ban + immediate unban removes the user while allowing future rejoin.
            bot.ban_chat_member(channel_id, user_id)
            bot.unban_chat_member(channel_id, user_id)

            bot_username = bot.get_me().username
            rejoin_url = (
                f"https://t.me/{bot_username}?start={channel_id}"
            )

            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton(
                    "🔄 Re-join / Renew",
                    url=rejoin_url
                )
            )

            bot.send_message(
                user_id,
                "⚠️ Your subscription has expired.\n\n"
                "To join again or renew, click the button below:",
                reply_markup=markup
            )

            users_col.delete_one({"_id": user["_id"]})

        except Exception as exc:
            logger.warning(
                "Expiry processing failed for user=%s channel=%s: %s",
                user_id, channel_id, exc
            )


# ============================================================
# STARTUP
# ============================================================
def main():
    keep_alive()

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        kick_expired_users,
        "interval",
        minutes=1,
        max_instances=1,
        coalesce=True
    )
    scheduler.start()

    try:
        bot.remove_webhook()
        logger.info("Bot is starting...")
        bot.infinity_polling(
            timeout=30,
            long_polling_timeout=30,
            skip_pending=True
        )
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
