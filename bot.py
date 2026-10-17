
import os
import json
import time
import logging
import threading
import requests
import telebot

from decimal import Decimal
from telebot import types

# ==================== الإعدادات ====================

BOT_TOKEN = '8466831620:AAHBBM_kwIw2eOYWMBfs3zE1gomwRbl_a0s'

STATE_FILE = "bot_data.json"
PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
CHECK_INTERVAL = 1

COINS = {
    "GRAMUSDT": "Gram (GRAM)",
    "BTCUSDT": "Bitcoin (BTC)",
    "ETHUSDT": "Ethereum (ETH)",
    "BNBUSDT": "BNB",
    "SOLUSDT": "Solana (SOL)",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

if not BOT_TOKEN or BOT_TOKEN == "ضع_توكن_البوت_هنا":
    raise ValueError("ضع توكن البوت الصحيح في BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
DATA_LOCK = threading.RLock()

# ==================== حفظ البيانات ====================

def load_data():
    if not os.path.exists(STATE_FILE):
        return {"selected": {}, "channels": {}}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        data.setdefault("selected", {})
        data.setdefault("channels", {})
        return data

    except (OSError, json.JSONDecodeError):
        logging.exception("تعذر قراءة ملف البيانات")
        return {"selected": {}, "channels": {}}


DATA = load_data()


def save_data():
    with DATA_LOCK:
        temp_file = STATE_FILE + ".tmp"

        with open(temp_file, "w", encoding="utf-8") as file:
            json.dump(DATA, file, ensure_ascii=False, indent=2)

        os.replace(temp_file, STATE_FILE)


# ==================== اختيار العملة ====================

def coin_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    buttons = [
        types.InlineKeyboardButton(
            text=name,
            callback_data="coin:" + symbol
        )
        for symbol, name in COINS.items()
    ]

    keyboard.add(*buttons)
    return keyboard


@bot.message_handler(commands=["start", "change"])
def start_handler(message):
    if message.chat.type != "private":
        return

    bot.send_message(
        message.chat.id,
        "👋 مرحبًا بك في بوت مراقبة أسعار العملات.\n\n"
        "اختر العملة التي تريد مراقبة سعرها في قناتك:",
        reply_markup=coin_keyboard()
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("coin:")
)
def choose_coin(call):
    user_id = str(call.from_user.id)
    symbol = call.data.split(":", 1)[1]

    if symbol not in COINS:
        bot.answer_callback_query(
            call.id,
            "العملة غير موجودة."
        )
        return

    with DATA_LOCK:
        DATA["selected"][user_id] = symbol

        # تحديث العملة في قنوات هذا المستخدم
        for channel in DATA["channels"].values():
            if str(channel.get("owner_id")) == user_id:
                if channel.get("symbol") != symbol:
                    channel["symbol"] = symbol
                    channel["last_price"] = None

        save_data()

    bot.answer_callback_query(call.id, "تم اختيار العملة ✅")

    bot.send_message(
        call.message.chat.id,
        f"✅ اخترت: {COINS[symbol]}\n\n"
        "الخطوة التالية:\n"
        "1. أضف البوت إلى قناتك.\n"
        "2. اجعله مشرفًا.\n"
        "3. امنحه صلاحية نشر الرسائل.\n\n"
        "سيبدأ مراقبة السعر، ولن ينشر تحديثًا "
        "إلا عندما يلاحظ تغير السعر."
    )


# ==================== اكتشاف القنوات ====================

@bot.my_chat_member_handler()
def channel_member_update(update):
    if update.chat.type != "channel":
        return

    chat_id = str(update.chat.id)
    new_member = update.new_chat_member
    status = new_member.status

    # إذا أزيل البوت من القناة
    if status in ("left", "kicked"):
        with DATA_LOCK:
            DATA["channels"].pop(chat_id, None)
            save_data()

        return

    # لا يبدأ التتبع إلا عندما يصبح البوت مشرفًا
    if status != "administrator":
        return

    can_post = getattr(new_member, "can_post_messages", False)

    if not can_post:
        if update.from_user:
            try:
                bot.send_message(
                    update.from_user.id,
                    "⚠️ اجعل البوت مشرفًا في القناة "
                    "مع تفعيل صلاحية نشر الرسائل."
                )
            except Exception:
                logging.exception("تعذر إرسال تنبيه الصلاحيات")
        return

    if not update.from_user:
        return

    owner_id = str(update.from_user.id)

    with DATA_LOCK:
        selected_symbol = DATA["selected"].get(owner_id)
        existing = DATA["channels"].get(chat_id, {})

        old_symbol = existing.get("symbol")

        if old_symbol != selected_symbol:
            last_price = None
        else:
            last_price = existing.get("last_price")

        DATA["channels"][chat_id] = {
            "owner_id": owner_id,
            "symbol": selected_symbol,
            "last_price": last_price,
        }

        save_data()

    try:
        if selected_symbol:
            bot.send_message(
                int(owner_id),
                f"✅ تم إعداد القناة لمراقبة "
                f"{COINS[selected_symbol]}."
            )
        else:
            bot.send_message(
                int(owner_id),
                "تم اكتشاف القناة. افتح البوت واختر العملة "
                "حتى يبدأ التتبع."
            )
    except Exception:
        logging.exception("تعذر إرسال تأكيد إلى المالك")


# ==================== تنسيق الأسعار ====================

def format_price(price):
    value = format(Decimal(str(price)), "f")

    if "." in value:
        value = value.rstrip("0").rstrip(".")

    return value


# ==================== فحص الأسعار كل ثانية ====================

def price_monitor():
    session = requests.Session()

    while True:
        cycle_start = time.monotonic()

        try:
            with DATA_LOCK:
                channels_snapshot = {
                    chat_id: dict(channel)
                    for chat_id, channel in DATA["channels"].items()
                    if channel.get("symbol") in COINS
                }

            symbols = sorted({
                channel["symbol"]
                for channel in channels_snapshot.values()
            })

            if not symbols:
                time.sleep(CHECK_INTERVAL)
                continue

            # طلب واحد يشمل جميع العملات التي تتم مراقبتها
            response = session.get(
                PRICE_URL,
                params={"symbols": json.dumps(symbols)},
                timeout=5
            )
            response.raise_for_status()

            result = response.json()

            prices = {
                item["symbol"]: item["price"]
                for item in result
            }

            messages = []
            dirty = False

            with DATA_LOCK:
                for chat_id, snapshot in channels_snapshot.items():
                    channel = DATA["channels"].get(chat_id)

                    if not channel:
                        continue

                    symbol = channel.get("symbol")

                    if symbol not in prices:
                        continue

                    new_price = prices[symbol]
                    old_price = channel.get("last_price")

                    # أول سعر يُحفظ للمقارنة دون نشر رسالة
                    if old_price is None:
                        channel["last_price"] = new_price
                        dirty = True
                        continue

                    # لا تنشر إذا لم يتغير السعر
                    if Decimal(new_price) == Decimal(old_price):
                        continue

                    old_value = Decimal(old_price)
                    new_value = Decimal(new_price)

                    if new_value > old_value:
                        emoji = "📈🟢"
                        direction = "ارتفع"
                    else:
                        emoji = "📉🔴"
                        direction = "انخفض"

                    coin_name = COINS[symbol]

                    message_text = (
                        f"{emoji} <b>{coin_name}</b>\n\n"
                        f"السعر {direction}!\n"
                        f"السعر السابق: "
                        f"<code>{format_price(old_price)}</code> USDT\n"
                        f"السعر الحالي: "
                        f"<code>{format_price(new_price)}</code> USDT"
                    )

                    messages.append((chat_id, message_text))

                    # حفظ آخر سعر حتى لا يتكرر نشره
                    channel["last_price"] = new_price
                    dirty = True

                if dirty:
                    save_data()

            # إرسال التحديثات إلى القنوات المعنية
            for chat_id, message_text in messages:
                try:
                    bot.send_message(
                        int(chat_id),
                        message_text,
                        disable_web_page_preview=True
                    )
                except Exception:
                    logging.exception(
                        "تعذر نشر تحديث في القناة %s",
                        chat_id
                    )

        except requests.RequestException:
            logging.exception("خطأ في الاتصال بمصدر الأسعار")

        except Exception:
            logging.exception("حدث خطأ أثناء مراقبة الأسعار")

        # المحافظة على دورة فحص مدتها ثانية تقريبًا
        elapsed = time.monotonic() - cycle_start
        time.sleep(max(0, CHECK_INTERVAL - elapsed))


# ==================== عرض حالة البوت ====================

@bot.message_handler(commands=["status"])
def status_handler(message):
    if message.chat.type != "private":
        return

    user_id = str(message.from_user.id)

    with DATA_LOCK:
        selected = DATA["selected"].get(user_id)

        channels = [
            (chat_id, channel)
            for chat_id, channel in DATA["channels"].items()
            if str(channel.get("owner_id")) == user_id
        ]

    if selected:
        text = f"العملة المختارة: {COINS[selected]}\n"
    else:
        text = "لم تختر عملة بعد.\n"

    text += f"\nعدد القنوات المرتبطة: {len(channels)}"

    for chat_id, channel in channels:
        symbol = channel.get("symbol")
        name = COINS.get(symbol, "لم تُحدد")

        text += f"\n\nالقناة: {chat_id}\nالعملة: {name}"

    bot.send_message(message.chat.id, text)


# ==================== تشغيل البوت ====================

if __name__ == "__main__":
    monitor_thread = threading.Thread(
        target=price_monitor,
        daemon=True
    )
    monitor_thread.start()

    logging.info("بدأ تشغيل بوت مراقبة الأسعار")

    bot.infinity_polling(
        skip_pending=False,
        allowed_updates=[
            "message",
            "callback_query",
            "my_chat_member"
        ]
    )
