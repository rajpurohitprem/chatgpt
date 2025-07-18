import json
import asyncio
import os
from datetime import datetime
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetHistoryRequest
from tqdm import tqdm
from telegram.constants import ParseMode

# ---------------------- CONFIGURATION ----------------------
CONFIG_FILE = "config.json"
BOT_FILE = "bot.json"
STOP_FLAG_PREFIX = "stop_"
START_FLAG_PREFIX = "start_"
SESSION_FILE_PREFIX = "session_"
SENT_LOG_PREFIX = "sent_"
ERROR_LOG_PREFIX = "error_"

# Ensure data directory exists
os.makedirs("user_data", exist_ok=True)

# ---------------------- UTILITY FUNCTIONS ----------------------

def get_user_file(user_id, prefix):
    return f"user_data/{prefix}{user_id}.json"

def load_config(user_id=None):
    if user_id:
        user_config = get_user_file(user_id, "config_")
        if os.path.exists(user_config):
            with open(user_config) as f:
                return json.load(f)
        return {}
    
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)

def save_config(data, user_id=None):
    if user_id:
        user_config = get_user_file(user_id, "config_")
        with open(user_config, "w") as f:
            json.dump(data, f, indent=2)
    else:
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)

def ensure_config_key(key, value, user_id):
    config = load_config(user_id)
    config[key] = value
    save_config(config, user_id)

def log_error(msg, user_id):
    error_file = get_user_file(user_id, ERROR_LOG_PREFIX)
    with open(error_file, 'a') as f:
        f.write(f"{datetime.now()}: {msg}\n")

# ---------------------- CLONE WORKER CLASS ----------------------

class CloneWorker:
    def __init__(self, user_id):
        self.user_id = user_id
        self.client = None
        self.is_cloning = False
        self.current_status = "Idle"
        self.last_update = None
        self.progress = (0, 0)  # (current, total)
        
    async def initialize_client(self):
        config = load_config(self.user_id)
        if not all(k in config for k in ["api_id", "api_hash", "phone"]):
            return False
            
        self.client = TelegramClient(
            get_user_file(self.user_id, SESSION_FILE_PREFIX),
            config["api_id"],
            config["api_hash"]
        )
        await self.client.connect()
        return True
        
    async def update_status(self, message, app=None, chat_id=None):
        self.current_status = message
        self.last_update = datetime.now().strftime("%H:%M:%S")
        
        if app and chat_id:
            try:
                status_file = get_user_file(self.user_id, "status_")
                status_data = {
                    "message": message,
                    "timestamp": self.last_update,
                    "progress": self.progress
                }
                with open(status_file, 'w') as f:
                    json.dump(status_data, f)
                    
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"🔄 {message}\n"
                         f"⏰ Last Update: {self.last_update}\n"
                         f"📊 Progress: {self.progress[0]}/{self.progress[1]}"
                )
            except Exception as e:
                log_error(f"Status update failed: {str(e)}", self.user_id)
    
    async def clone_messages(self, app, chat_id, start_id=None, end_id=None):
        if self.is_cloning:
            await self.update_status("❌ Already running a clone operation", app, chat_id)
            return
            
        self.is_cloning = True
        config = load_config(self.user_id)
        
        if not await self.initialize_client():
            await self.update_status("❌ Client initialization failed", app, chat_id)
            self.is_cloning = False
            return
            
        if not all(k in config for k in ["source_channel_id", "target_channel_id"]):
            await self.update_status("❌ Missing source or target channel", app, chat_id)
            self.is_cloning = False
            return
            
        try:
            def normalize_channel_id(cid):
                cid = str(cid)
                return int(cid) if cid.startswith("-100") else int("-100" + cid)

            src_entity = await self.client.get_entity(normalize_channel_id(config["source_channel_id"]))
            tgt_entity = await self.client.get_entity(normalize_channel_id(config["target_channel_id"]))
        except Exception as e:
            await self.update_status(f"❌ Channel access failed: {str(e)}", app, chat_id)
            self.is_cloning = False
            return

        # Message collection
        await self.update_status("📂 Collecting messages...", app, chat_id)
        all_messages = []
        offset_id = 0
        limit = 100
        
        while True:
            stop_file = get_user_file(self.user_id, STOP_FLAG_PREFIX)
            if os.path.exists(stop_file):
                os.remove(stop_file)
                await self.update_status("⛔ Stopped by user request", app, chat_id)
                break

            try:
                history = await self.client(GetHistoryRequest(
                    peer=src_entity,
                    offset_id=offset_id,
                    offset_date=None,
                    add_offset=0,
                    limit=limit,
                    max_id=0,
                    min_id=0,
                    hash=0
                ))
                if not history.messages:
                    break
                    
                all_messages.extend(history.messages)
                offset_id = history.messages[-1].id
                
                if len(all_messages) % 100 == 0:
                    await self.update_status(f"📥 Collected {len(all_messages)} messages", app, chat_id)
                    
            except Exception as e:
                await self.update_status(f"❌ Collection error: {str(e)}", app, chat_id)
                break

        all_messages.reverse()
        if start_id and end_id:
            all_messages = [msg for msg in all_messages if start_id <= msg.id <= end_id]

        total_messages = len(all_messages)
        self.progress = (0, total_messages)
        processed = 0
        
        await self.update_status(f"📊 Ready to clone {total_messages} messages", app, chat_id)

        # Cloning process
        sent_log = get_user_file(self.user_id, SENT_LOG_PREFIX)
        progress_interval = max(1, total_messages // 10)  # Update progress 10 times
        
        for msg in all_messages:
            stop_file = get_user_file(self.user_id, STOP_FLAG_PREFIX)
            if os.path.exists(stop_file):
                await self.update_status(f"⛔ Stopped ({processed}/{total_messages} done)", app, chat_id)
                os.remove(stop_file)
                break

            try:
                if msg.media:
                    file_path = await self.client.download_media(msg)
                    await self.client.send_file(tgt_entity, file_path, caption=msg.text or msg.message or "")
                    if os.path.exists(file_path):
                        os.remove(file_path)
                elif msg.text or msg.message:
                    await self.client.send_message(tgt_entity, msg.text or msg.message)

                with open(sent_log, "a") as f:
                    f.write(f"{msg.id}\n")

                processed += 1
                self.progress = (processed, total_messages)
                
                if processed % progress_interval == 0 or processed == total_messages:
                    percent = (processed / total_messages) * 100
                    await self.update_status(
                        f"⏳ Cloning: {percent:.1f}% complete\n"
                        f"({processed}/{total_messages} messages)",
                        app,
                        chat_id
                    )

                await asyncio.sleep(0.5)  # Rate limiting

            except Exception as e:
                log_error(f"Message {msg.id} failed: {str(e)}", self.user_id)
                if processed % 1 == 0:
                    await self.update_status(f"⚠️ Error on message {msg.id} (continuing)", app, chat_id)

        # Final status
        completion_msg = f"✅ Completed: {processed}/{total_messages} messages"
        stop_file = get_user_file(self.user_id, STOP_FLAG_PREFIX)
        if os.path.exists(stop_file):
            completion_msg = f"⏹️ Stopped early: {processed}/{total_messages}"
            os.remove(stop_file)
            
        await self.update_status(completion_msg, app, chat_id)
        self.is_cloning = False
        await self.client.disconnect()

# ---------------------- BOT HANDLERS ----------------------

# Reply Keyboards
def main_menu():
    return ReplyKeyboardMarkup([
        ["User Config", "Source/Target"],
        ["Start Mission"],
        ["Mission Status"],
    ], resize_keyboard=True)

def user_config_menu():
    return ReplyKeyboardMarkup([
        ["Api ID", "Api Hash", "Phone No."],
        ["Login", "Logout","Show Config"],
        ["⬅ Back", "skip"],
    ], resize_keyboard=True)

def source_target_menu():
    return ReplyKeyboardMarkup([
        [KeyboardButton("Select Source Channel", request_chat=KeyboardButtonRequestChat(request_id=1, chat_is_channel=True))],
        [KeyboardButton("Select Target Channel", request_chat=KeyboardButtonRequestChat(request_id=2, chat_is_channel=True))],
        ["⬅ Back"]
    ], resize_keyboard=True)

def mission_menu():
    return ReplyKeyboardMarkup([
        ["Full Clone", "Range Clone"],
        ["Stop","Resume Clone", "skip"],
        ["Mission Status"],
    ], resize_keyboard=True)

# States
(
    MAIN_MENU,
    MISSION_STATUS,
    USER_CONFIG,
    WAITING_FOR_API_ID,
    WAITING_FOR_API_HASH,
    WAITING_FOR_PHONE,
    WAITING_FOR_CODE,
    SOURCE_TARGET,
    MISSION,
    WAITING_FOR_RANGE_START,
    WAITING_FOR_RANGE_END
) = range(11)

# User session management
user_sessions = {}  # user_id: CloneWorker

def get_user_session(user_id):
    if user_id not in user_sessions:
        user_sessions[user_id] = CloneWorker(user_id)
    return user_sessions[user_id]

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data.clear()
    await update.message.reply_text("Welcome! Use buttons to configure and start cloning.", reply_markup=main_menu())
    return MAIN_MENU

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Main Menu:", reply_markup=main_menu())
    return MAIN_MENU

async def check_start_command(update: Update, text: str):
    if text.lower() == "/start":
        await start(update, ContextTypes.DEFAULT_TYPE)
        return True
    return False
    
async def user_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    await update.message.reply_text("User Config:", reply_markup=user_config_menu())
    return USER_CONFIG

async def show_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        config = load_config(user_id)
        
        # Hide sensitive info
        safe_config = config.copy()
        if 'api_hash' in safe_config:
            safe_config['api_hash'] = f"{safe_config['api_hash'][:4]}...{safe_config['api_hash'][-4:]}"
        if 'phone' in safe_config:
            safe_config['phone'] = f"{safe_config['phone'][:3]}...{safe_config['phone'][-2:]}"
        
        formatted_config = json.dumps(safe_config, indent=2)
        
        await update.message.reply_text(
            f"<b>Current Configuration:</b>\n"
            f"<pre>{formatted_config}</pre>\n"
            f"<i>Sensitive fields are partially hidden</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=user_config_menu()
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error loading config: {str(e)}",
            reply_markup=user_config_menu()
        )
    return USER_CONFIG
    
async def source_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    await update.message.reply_text("Select source or target channel.", reply_markup=source_target_menu())
    return SOURCE_TARGET

async def start_mission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    await update.message.reply_text("Choose clone mode:", reply_markup=mission_menu())
    return MISSION

async def request_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    await update.message.reply_text("Please send your API ID:")
    return WAITING_FOR_API_ID

async def request_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    await update.message.reply_text("Please send your API Hash:")
    return WAITING_FOR_API_HASH

async def request_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    user_id = update.effective_user.id
    config = load_config(user_id)
    current = config.get("phone", "Not Set")
    await update.message.reply_text(f"Current number: `{current}`\nSend new phone number or type 'skip' to keep.", parse_mode="Markdown")
    return WAITING_FOR_PHONE

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    user_id = update.effective_user.id
    config = load_config(user_id)
    if not all(k in config for k in ("api_id", "api_hash", "phone")):
        await update.message.reply_text("Please configure API ID, Hash, and Phone first.")
        return USER_CONFIG
    
    try:
        client = TelegramClient(get_user_file(user_id, SESSION_FILE_PREFIX), config["api_id"], config["api_hash"])
        await client.connect()
        
        if not await client.is_user_authorized():
            sent_code = await client.send_code_request(config["phone"])
            context.user_data["client"] = client
            context.user_data["phone"] = config["phone"]
            context.user_data["phone_code_hash"] = sent_code.phone_code_hash
            
            await update.message.reply_text(
                "📲 Code sent. Please reply with the code in format: 1 2 3 4 5\n"
                "(Enter the numbers separated by spaces)"
            )
            return WAITING_FOR_CODE
        
        await update.message.reply_text("✅ Already logged in.", reply_markup=main_menu())
        await client.disconnect()
        return MAIN_MENU
    except Exception as e:
        await update.message.reply_text(f"❌ Login failed: {str(e)}")
        return USER_CONFIG

async def verify_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    code = update.message.text.replace(" ", "")  # Remove spaces from "1 2 3 4 5" format
    if not code.isdigit() or len(code) != 5:
        await update.message.reply_text("❌ Invalid code format. Please send 5 digits (e.g., '1 2 3 4 5')")
        return WAITING_FOR_CODE
    
    try:
        client = context.user_data["client"]
        await client.sign_in(
            phone=context.user_data["phone"],
            code=code,
            phone_code_hash=context.user_data["phone_code_hash"]
        )
        await update.message.reply_text("✅ Login successful!", reply_markup=main_menu())
        await client.disconnect()
        return MAIN_MENU
    except Exception as e:
        await update.message.reply_text(f"❌ Verification failed: {str(e)}\nPlease try again:")
        return WAITING_FOR_CODE
        
async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    user_id = update.effective_user.id
    session_file = get_user_file(user_id, SESSION_FILE_PREFIX)
    if os.path.exists(session_file):
        os.remove(session_file)
        await update.message.reply_text("🔒 Logged out and session removed.")
    else:
        await update.message.reply_text("No session to remove.")
    return USER_CONFIG

async def save_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    text = update.message.text
    user_id = update.effective_user.id
    
    if text.lower() == "skip":
        await update.message.reply_text("No changes made.", reply_markup=main_menu())
        return MAIN_MENU
    
    try:
        ensure_config_key("api_id", int(text), user_id)
        await update.message.reply_text("✅ API ID saved.", reply_markup=user_config_menu())
        return USER_CONFIG
    except ValueError:
        await update.message.reply_text("❌ Invalid API ID. Must be a number.")
        return WAITING_FOR_API_ID

async def save_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    text = update.message.text
    user_id = update.effective_user.id
    
    if text.lower() == "skip":
        await update.message.reply_text("No changes made.", reply_markup=main_menu())
        return MAIN_MENU
    
    if len(text) == 32 and all(c in '0123456789abcdef' for c in text.lower()):
        ensure_config_key("api_hash", text, user_id)
        await update.message.reply_text("✅ API Hash saved!", reply_markup=main_menu())
        return MAIN_MENU
    else:
        await update.message.reply_text(
            "❌ Invalid API Hash!\n"
            "Must be 32-character hexadecimal string\n"
            "Example: 1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p",
            reply_markup=user_config_menu()
        )
        return WAITING_FOR_API_HASH

async def save_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    text = update.message.text
    user_id = update.effective_user.id
    
    if text.lower() == "skip":
        await update.message.reply_text("No changes made.", reply_markup=main_menu())
        return MAIN_MENU
    
    if text.startswith('+') and text[1:].isdigit() and len(text) >= 8:
        ensure_config_key("phone", text, user_id)
        await update.message.reply_text("✅ Phone number saved!", reply_markup=main_menu())
        return USER_CONFIG
    else:
        await update.message.reply_text(
            "❌ Invalid phone number!\n"
            "Must be in international format with country code\n"
            "Example: +123456789012",
            reply_markup=user_config_menu()
        )
        return WAITING_FOR_PHONE

async def request_range_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    await update.message.reply_text("Send start message ID:")
    return WAITING_FOR_RANGE_START

async def set_range_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    text = update.message.text
    if text.lower() == "skip":
        await update.message.reply_text("No changes made.", reply_markup=main_menu())
        return MAIN_MENU
    
    try:
        context.user_data["range_start"] = int(text)
        await update.message.reply_text("Now send end message ID:")
        return WAITING_FOR_RANGE_END
    except ValueError:
        await update.message.reply_text("❌ Invalid message ID. Must be a number.")
        return WAITING_FOR_RANGE_START

async def set_range_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    text = update.message.text
    user_id = update.effective_user.id
    
    if text.lower() == "skip":
        await update.message.reply_text("No changes made.", reply_markup=main_menu())
        return MAIN_MENU
    
    try:
        start_id = context.user_data["range_start"]
        end_id = int(text)
        
        # Create start flag
        with open(get_user_file(user_id, START_FLAG_PREFIX), 'w') as f:
            pass
            
        worker = get_user_session(user_id)
        asyncio.create_task(worker.clone_messages(
            app=context.application,
            chat_id=update.effective_chat.id,
            start_id=start_id,
            end_id=end_id
        ))
        
        await update.message.reply_text(f"📥 Range clone started for messages {start_id} to {end_id}.", reply_markup=mission_menu())
        return MISSION
    except ValueError:
        await update.message.reply_text("❌ Invalid message ID. Must be a number.")
        return WAITING_FOR_RANGE_END

async def full_clone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    user_id = update.effective_user.id
    
    # Create start flag
    with open(get_user_file(user_id, START_FLAG_PREFIX), 'w') as f:
        pass
        
    # Remove stop flag if exists
    stop_file = get_user_file(user_id, STOP_FLAG_PREFIX)
    if os.path.exists(stop_file):
        os.remove(stop_file)
        
    worker = get_user_session(user_id)
    asyncio.create_task(worker.clone_messages(
        app=context.application,
        chat_id=update.effective_chat.id
    ))
    
    await update.message.reply_text("📥 Full clone started...", reply_markup=mission_menu())
    return MISSION
    
async def mission_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    user_id = update.effective_user.id
    worker = get_user_session(user_id)
    
    status_file = get_user_file(user_id, "status_")
    if os.path.exists(status_file):
        with open(status_file) as f:
            status_data = json.load(f)
        await update.message.reply_text(
            f"📊 Mission Status:\n"
            f"{status_data.get('message', 'No status available')}\n"
            f"⏰ Last Update: {status_data.get('timestamp', 'Unknown')}\n"
            f"📊 Progress: {status_data.get('progress', (0, 0))[0]}/{status_data.get('progress', (0, 0))[1]}",
            reply_markup=mission_menu()
        )
    else:
        await update.message.reply_text("No active mission found.", reply_markup=mission_menu())
    return MISSION

async def stop_clone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    user_id = update.effective_user.id
    
    # Create stop flag
    with open(get_user_file(user_id, STOP_FLAG_PREFIX), "w") as f:
        f.write("stop")
        
    await update.message.reply_text("⛔ Clone stop requested.", reply_markup=mission_menu())
    return MISSION
    
async def resume_clone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    user_id = update.effective_user.id
    
    sent_log = get_user_file(user_id, SENT_LOG_PREFIX)
    if not os.path.exists(sent_log):
        await update.message.reply_text("⚠️ No previous clone operation to resume", reply_markup=mission_menu())
        return MISSION
    
    try:
        # Get last message ID from sent log
        with open(sent_log) as f:
            lines = f.readlines()
            if not lines:
                await update.message.reply_text("⚠️ No sent messages found in log", reply_markup=mission_menu())
                return MISSION
                
            last_id = int(lines[-1].strip())
            
        # Create start flag
        with open(get_user_file(user_id, START_FLAG_PREFIX), 'w') as f:
            pass
            
        # Remove stop flag if exists
        stop_file = get_user_file(user_id, STOP_FLAG_PREFIX)
        if os.path.exists(stop_file):
            os.remove(stop_file)
            
        worker = get_user_session(user_id)
        asyncio.create_task(worker.clone_messages(
            app=context.application,
            chat_id=update.effective_chat.id,
            start_id=last_id + 1  # Start from next message
        ))
        
        await update.message.reply_text(f"🔄 Resuming clone from message {last_id + 1}", reply_markup=mission_menu())
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to resume: {str(e)}", reply_markup=mission_menu())
    
    return MISSION
    
async def chat_shared_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_start_command(update, update.message.text):
        return MAIN_MENU
    shared = update.message.chat_shared
    user_id = update.effective_user.id
    
    if not shared:
        return SOURCE_TARGET
    
    if shared.request_id == 1:
        ensure_config_key("source_channel_id", shared.chat_id, user_id)
        await update.message.reply_text(f"✅ Source channel set: `{shared.chat_id}`", parse_mode="Markdown")
    elif shared.request_id == 2:
        ensure_config_key("target_channel_id", shared.chat_id, user_id)
        await update.message.reply_text(f"✅ Target channel set: `{shared.chat_id}`", parse_mode="Markdown")
    
    return SOURCE_TARGET

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    print(f"Error: {error}")
    if update and update.message:
        user_id = update.effective_user.id
        log_error(str(error), user_id)
        await update.message.reply_text(f"⚠️ Error occurred: {error}")

# ---------------------- MAIN ----------------------
def main():
    # Load bot token
    with open(BOT_FILE) as f:
        BOT_TOKEN = json.load(f)["bot_token"]

    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation Handler
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^User Config$"), user_config),
            MessageHandler(filters.Regex("^Source/Target$"), source_target),
            MessageHandler(filters.Regex("^Start Mission$"), start_mission),
            MessageHandler(filters.Regex("^Mission Status$"), mission_status),
            CommandHandler("start", start),
        ],
        states={
            MAIN_MENU: [
                MessageHandler(filters.Regex("^User Config$"), user_config),
                MessageHandler(filters.Regex("^Source/Target$"), source_target),
                MessageHandler(filters.Regex("^Start Mission$"), start_mission),
                MessageHandler(filters.Regex("^Mission Status$"), mission_status),
                CommandHandler("start", start),
            ],
            
            MISSION_STATUS: [
                MessageHandler(filters.Regex("^User Config$"), user_config),
                MessageHandler(filters.Regex("^Source/Target$"), source_target),
                MessageHandler(filters.Regex("^Start Mission$"), start_mission),
                MessageHandler(filters.Regex("^Mission Status$"), mission_status),
                MessageHandler(filters.Regex("^Full Clone$"), full_clone),
                MessageHandler(filters.Regex("^Range Clone$"), request_range_start),
                MessageHandler(filters.Regex("^Resume Clone$"), resume_clone),
                MessageHandler(filters.Regex("^Stop$"), stop_clone),
                MessageHandler(filters.Regex("^⬅ Back$"), back_to_main),
                CommandHandler("start", start),
            ],
            
            USER_CONFIG: [
                MessageHandler(filters.Regex("^Api ID$"), request_api_id),
                MessageHandler(filters.Regex("^Api Hash$"), request_api_hash),
                MessageHandler(filters.Regex("^Phone No\.$"), request_phone),
                MessageHandler(filters.Regex("^Login$"), login),
                MessageHandler(filters.Regex("^Logout$"), logout),
                MessageHandler(filters.Regex("^Show Config$"), show_config),
                MessageHandler(filters.Regex("^⬅ Back$"), back_to_main),
                CommandHandler("start", start),
            ],
            WAITING_FOR_API_ID: [
                CommandHandler("start", start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_api_id),
            ],
            WAITING_FOR_API_HASH: [
                CommandHandler("start", start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_api_hash),
            ],
            WAITING_FOR_PHONE: [
                CommandHandler("start", start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_phone),
            ],
            WAITING_FOR_CODE: [
                CommandHandler("start", start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, verify_code),
            ],
            SOURCE_TARGET: [
                CommandHandler("start", start),
                MessageHandler(filters.StatusUpdate.CHAT_SHARED, chat_shared_handler),
                MessageHandler(filters.Regex("^⬅ Back$"), back_to_main),
            ],
            MISSION: [
                MessageHandler(filters.Regex("^Full Clone$"), full_clone),
                MessageHandler(filters.Regex("^Range Clone$"), request_range_start),
                MessageHandler(filters.Regex("^Resume Clone$"), resume_clone),
                MessageHandler(filters.Regex("^Stop$"), stop_clone),
                MessageHandler(filters.Regex("^⬅ Back$"), back_to_main),
                CommandHandler("start", start),
            ],
            WAITING_FOR_RANGE_START: [
                CommandHandler("start", start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_range_start),
            ],
            WAITING_FOR_RANGE_END: [
                CommandHandler("start", start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_range_end),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^⬅ Back$"), back_to_main),
            MessageHandler(filters.Regex("^skip$"), back_to_main),
        ],
    )

    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    
    print("🤖 Bot is running and ready")
    app.run_polling()

if __name__ == "__main__":
    main()
