import asyncio
import os
import logging
import sqlite3
from datetime import datetime
from typing import List, Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.enums import ParseMode

from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= 1. КОНФИГУРАЦИЯ И СЕКРЕТЫ =================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN:
    raise ValueError("Ошибка: не задана переменная окружения TELEGRAM_TOKEN!")
if not GEMINI_API_KEY:
    raise ValueError("Ошибка: не задана переменная окружения GEMINI_API_KEY!")

DB_NAME = "repair_memory.db"

# Имена/триггеры, на которые реагирует бот
BOT_NAMES = ["тимофей", "ии-прораб", "тимофей,", "тимофей!"]

# Системная инструкция ИИ-прораба
SYSTEM_INSTRUCTION = """
Тебя зовут Тимофей. Ты — опытный, строгий и внимательный ИИ-прораб и инженер технадзора по ремонту квартир.
Тебе доступен контекст последних сообщений из строительного чата (кто что обещал, какие материалы согласовали).
Твоя задача:
1. Отвечать на обращения по имени (Тимофей / ИИ-Прораб).
2. Анализировать входящие документы (PDF, изображения, видео, аудио).
3. Давать четкие, технически грамотные ответы по ремонту и защищать интересы владельца квартиры.
4. Предупреждать о технических ошибках, нарушениях технологий или завышении смет.
5. Ты также эксперт в области однофазной силовой и слаботочной электрики в квартирах, распределительных щитов
6. Ты эксперт в области умных домом, построенных на беспроводных технологиях с использованием протокола zigbee и локального управления с помощью Homeassistant
"""

# Инициализация клиентов
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()


# Простейший веб-сервер для Render Health Check
async def handle_ping(request):
    return web.Response(text="Тимофей работает!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


# ================= 2. БАЗА ДАННЫХ =================
def init_db():
    """Инициализация SQLite базы данных."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_name TEXT,
            message_text TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_chats (
            chat_id INTEGER PRIMARY KEY,
            digest_thread_id INTEGER
        )
    """)
    conn.commit()
    conn.close()


def save_message(chat_id: int, user_name: str, text: str):
    """Сохранение сообщения в историю."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chat_history (chat_id, user_name, message_text) VALUES (?, ?, ?)", (chat_id, user_name, text))
    conn.commit()
    conn.close()


def set_digest_thread(chat_id: int, thread_id: Optional[int]):
    """Фиксация chat_id и thread_id для отправки вечернего дайджеста."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO active_chats (chat_id, digest_thread_id) VALUES (?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET digest_thread_id=excluded.digest_thread_id
    """, (chat_id, thread_id))
    conn.commit()
    conn.close()


def get_recent_history(chat_id: int, limit: int = 20) -> str:
    """Извлечение последних N сообщений из базы данных."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_name, message_text FROM (
            SELECT user_name, message_text, id FROM chat_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?
        ) ORDER BY id ASC
    """, (chat_id, limit))
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return "История пуста."
        
    return "\n".join([f"{user}: {text}" for user, text in rows])


def get_active_chats_for_digest():
    """Получение всех сохраненных чатов и привязанных топиков."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, digest_thread_id FROM active_chats")
    rows = cursor.fetchall()
    conn.close()
    return rows


# ================= 3. СЛУЖБА УВЕДОМЛЕНИЙ И НАПОМИНАНИЙ =================
async def send_daily_summary():
    """Ежедневный вечерний отчет Тимофея в 19:00."""
    chats = get_active_chats_for_digest()
    for cid, tid in chats:
        context_history = get_recent_history(cid, limit=30)
        if context_history == "История пуста.":
            continue

        prompt = (
            f"--- ИСТОРИЯ ЧАТА ЗА ДЕНЬ ---\n"
            f"{context_history}\n"
            f"----------------------------\n\n"
            f"Сформируй краткий и строгий вечерний отчет ИИ-прораба Тимофея по ремонту:\n"
            f"1. Что было сделано/согласовано сегодня?\n"
            f"2. Есть ли незакрытые вопросы или риски?\n"
            f"3. Что запланировано на завтра?"
        )

        try:
            response = gemini_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
                config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)
            )
            report = f"📋 **Вечерний отчет Тимофея ({datetime.now().strftime('%d.%m.%Y')})**\n\n" + response.text
            await bot.send_message(chat_id=cid, text=report, message_thread_id=tid)
        except Exception as e:
            logging.error(f"Ошибка при генерации вечернего отчета: {e}")


async def scheduled_reminder_task(chat_id: int, reminder_text: str, thread_id: Optional[int] = None):
    """Отправка запланированного напоминания."""
    msg = f"🔔 **Напоминание от Тимофея!**\n\n{reminder_text}"
    await bot.send_message(chat_id=chat_id, text=msg, message_thread_id=thread_id)


# ================= 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
async def process_media_file(bot: Bot, file_id: str, file_name: str) -> str:
    local_path = f"./temp_{file_id}_{file_name}"
    file_info = await bot.get_file(file_id)
    await bot.download_file(file_info.file_path, local_path)
    
    uploaded_file = gemini_client.files.upload(file=local_path)
    if os.path.exists(local_path):
        os.remove(local_path)
    return uploaded_file


# ================= 5. ОБРАБОТЧИКИ СООБЩЕНИЙ =================

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.reply(
        "🏗️ **ИИ-Прораб Тимофей на связи!**\n\n"
        "Как со мной работать:\n"
        "• Обратитесь по имени: `Тимофей, проверь смету`\n"
        "• `/set_digest` — вызвать в топике «Ход ремонта», чтобы ежедневный отчет в 19:00 приходил именно туда\n"
        "• `/remind HH:MM Текст` — поставить точное напоминание\n"
        "• Пришлите фото, PDF или видео — я их проанализирую!"
    )


# --- Команда фиксации топика для дайджеста ---
@dp.message(Command("set_digest"))
async def handle_set_digest(message: types.Message):
    thread_id = message.message_thread_id
    set_digest_thread(message.chat.id, thread_id)
    await message.reply("📌 **Отлично!** Тимофей зафиксировал этот топик и будет присылать вечерние отчеты в 19:00 прямо сюда.")


# --- Команда постановки напоминаний ---
@dp.message(Command("remind"))
async def handle_remind(message: types.Message):
    args = message.text.replace("/remind", "").strip().split(" ", 1)
    
    if len(args) < 2:
        await message.reply("⚠️ **Неверный формат!** Используйте: `/remind HH:MM Текст напоминания`\nПример: `/remind 15:00 Привезти штукатурку`")
        return

    time_str, reminder_text = args[0], args[1]

    try:
        now = datetime.now()
        target_time = datetime.strptime(time_str, "%H:%M").time()
        run_datetime = datetime.combine(now.date(), target_time)

        if run_datetime <= now:
            run_datetime = run_datetime.replace(day=now.day + 1)

        scheduler.add_job(
            scheduled_reminder_task,
            'date',
            run_date=run_datetime,
            args=[message.chat.id, reminder_text, message.message_thread_id]
        )

        await message.reply(f"⏰ **Напоминание принято!**\nТимофей напомнит: «_{reminder_text}_» в **{run_datetime.strftime('%H:%M %d.%m')}**")

    except ValueError:
        await message.reply("❌ **Ошибка времени!** Укажите время в формате **ЧЧ:ММ** (например, 14:30).")


# --- Команда /ask ---
@dp.message(Command("ask"))
async def handle_ask(message: types.Message):
    user_query = message.text.replace("/ask", "").strip()
    if not user_query:
        await message.reply("Задайте вопрос после команды, например: `/ask Тимофей, что думаешь по поводу этих розеток?`")
        return

    status_msg = await message.reply("⏳ *Тимофей анализирует запрос...*", parse_mode=ParseMode.MARKDOWN)

    save_message(message.chat.id, message.from_user.full_name or "Пользователь", f"[Запрос /ask]: {user_query}")
    context_history = get_recent_history(message.chat.id, limit=20)
    prompt = (
        f"--- ИСТОРИЯ ЧАТА (ПОСЛЕДНИЕ 20 СООБЩЕНИЙ) ---\n"
        f"{context_history}\n"
        f"---------------------------------------------\n\n"
        f"Запрос пользователя: {user_query}"
    )

    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)
        )

        save_message(message.chat.id, "Тимофей (ИИ-Прораб)", response.text)
        await status_msg.edit_text(response.text)

    except Exception as e:
        logging.error(f"Ошибка Gemini API: {e}")
        await status_msg.edit_text("❌ Произошла ошибка при обработке запроса.")


# --- Обработка обычного текста (Фильтр по имени Тимофей + запись в базу) ---
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text_messages(message: types.Message):
    user_name = message.from_user.full_name or "Пользователь"
    user_text = message.text
    text_lower = user_text.lower()

    save_message(message.chat.id, user_name, user_text)

    is_called_by_name = any(name in text_lower for name in BOT_NAMES)

    if is_called_by_name:
        status_msg = await message.reply("⏳ *Тимофей изучает ваш вопрос...*", parse_mode=ParseMode.MARKDOWN)

        context_history = get_recent_history(message.chat.id, limit=20)
        prompt = (
            f"--- ИСТОРИЯ ЧАТА (ПОСЛЕДНИЕ 20 СООБЩЕНИЙ) ---\n"
            f"{context_history}\n"
            f"---------------------------------------------\n\n"
            f"Обращение к тебе ({user_name}): {user_text}"
        )

        try:
            response = gemini_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
                config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)
            )

            save_message(message.chat.id, "Тимофей (ИИ-Прораб)", response.text)
            await status_msg.edit_text(response.text)

        except Exception as e:
            logging.error(f"Ошибка при ответе по имени: {e}")
            await status_msg.edit_text("❌ Извините, не удалось сформировать ответ.")


# --- Обработка медиафайлов ---
@dp.message(F.document | F.photo | F.video | F.voice)
async def handle_media(message: types.Message):
    status_msg = await message.reply("📥 *Тимофей изучает медиафайл...*", parse_mode=ParseMode.MARKDOWN)
    
    uploaded_file = None
    media_type = "файл"

    try:
        if message.document:
            file_id = message.document.file_id
            file_name = message.document.file_name or "document.pdf"
            media_type = f"документ ({file_name})"
        elif message.photo:
            file_id = message.photo[-1].file_id
            file_name = "photo.jpg"
            media_type = "фотографию"
        elif message.video:
            file_id = message.video.file_id
            file_name = message.video.file_name or "video.mp4"
            media_type = "видео"
        elif message.voice:
            file_id = message.voice.file_id
            file_name = "voice.ogg"
            media_type = "голосовое сообщение"

        uploaded_file = await process_media_file(bot, file_id, file_name)

        context_history = get_recent_history(message.chat.id, limit=20)
        user_caption = message.caption if message.caption else "Тимофей, проанализируй этот файл в контексте нашего ремонта."

        prompt = (
            f"--- ИСТОРИЯ ЧАТА (ПОСЛЕДНИЕ 20 СООБЩЕНИЙ) ---\n"
            f"{context_history}\n"
            f"---------------------------------------------\n\n"
            f"Пользователь прислал {media_type}.\n"
            f"Комментарий к файлу: {user_caption}"
        )

        response = gemini_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=[uploaded_file, prompt],
            config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)
        )

        user_name = message.from_user.full_name or "Пользователь"
        save_message(message.chat.id, user_name, f"[Отправил {media_type}]: {user_caption}")
        save_message(message.chat.id, "Тимофей (ИИ-Прораб)", response.text)

        await status_msg.edit_text(response.text)

    except Exception as e:
        logging.error(f"Ошибка при обработке медиафайла: {e}")
        await status_msg.edit_text("❌ Не удалось обработать медиафайл.")


# ================= 6. ТОЧКА ВХОДА =================
async def main():
    init_db()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )

    scheduler.add_job(send_daily_summary, 'cron', hour=19, minute=0)
    scheduler.start()

    await start_dummy_server()

    print("🚀 Бот Тимофей запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
