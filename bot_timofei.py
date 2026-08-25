import asyncio
import os
import logging
import sqlite3
import re
import mimetypes
import uuid
import pathlib
import time

from datetime import datetime, timedelta
from typing import List, Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession

from google import genai
from google.genai import types as genai_types
from google.genai.errors import APIError

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
5. Ты также эксперт в области однофазной силовой и слаботочной электрики в квартирах, распределительных щитов.
6. Ты эксперт в области умных домов, построенных на беспроводных технологиях с использованием протокола zigbee и локального управления с помощью Home Assistant.
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


# ================= 3. ВЫЗОВ GEMINI API С ОБРАБОТКОЙ ОШИБОК =================
async def generate_gemini_response(contents, system_instruction: str) -> str:
    """Безопасный вызов Gemini API с обработкой лимитов 429 и асинхронным выполнением."""
    loop = asyncio.get_event_loop()

    def _call_api():
        return gemini_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=contents,
            config=genai_types.GenerateContentConfig(system_instruction=system_instruction)
        )

    try:
        response = await loop.run_in_executor(None, _call_api)
        return response.text
    except APIError as e:
        if e.code == 429:
            logging.warning("Превышен лимит запросов Gemini API (429). Ожидание 10 сек...")
            await asyncio.sleep(10)
            try:
                response = await loop.run_in_executor(None, _call_api)
                return response.text
            except Exception as retry_err:
                logging.error(f"Повторный запрос после 429 завершился ошибкой: {retry_err}")
                return "⚠️ **Лимит запросов к ИИ временно превышен.** Пожалуйста, подождите 1–2 минуты и повторите попытку."
        else:
            raise e


# ================= 4. СЛУЖБА УВЕДОМЛЕНИЙ И НАПОМИНАНИЙ =================
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
            ai_text = await generate_gemini_response(contents=prompt, system_instruction=SYSTEM_INSTRUCTION)
            report = f"📋 **Вечерний отчет Тимофея ({datetime.now().strftime('%d.%m.%Y')})**\n\n" + ai_text
            
            if len(report) <= 4000:
                if tid:
                    await bot.send_message(chat_id=cid, text=report, message_thread_id=tid)
                else:
                    await bot.send_message(chat_id=cid, text=report)
            else:
                chunks = [report[i:i + 4000] for i in range(0, len(report), 4000)]
                for chunk in chunks:
                    if tid:
                        await bot.send_message(chat_id=cid, text=chunk, message_thread_id=tid)
                    else:
                        await bot.send_message(chat_id=cid, text=chunk)
        except Exception as e:
            logging.error(f"Ошибка при генерации вечернего отчета: {e}")

async def scheduled_reminder_task(chat_id: int, reminder_text: str, thread_id: Optional[int] = None):
    """Отправка запланированного напоминания."""
    msg = f"🔔 **Напоминание от Тимофея!**\n\n{reminder_text}"
    await bot.send_message(chat_id=chat_id, text=msg, message_thread_id=thread_id)


# ================= 5. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
async def send_long_message(message: types.Message, text: str, status_msg: types.Message = None):
    """Отправляет длинный текст, разбивая его на части до 4000 символов."""
    MAX_LENGTH = 4000
    
    if len(text) <= MAX_LENGTH:
        if status_msg:
            await status_msg.edit_text(text)
        else:
            await message.reply(text)
        return

    chunks = [text[i:i + MAX_LENGTH] for i in range(0, len(text), MAX_LENGTH)]

    if status_msg:
        await status_msg.edit_text(chunks[0])
    else:
        await message.reply(chunks[0])

    for chunk in chunks[1:]:
        await message.answer(chunk)

async def process_media_file(bot: Bot, file_id: str, original_file_name: str):
    """Безопасная загрузка медиафайла в Gemini API с использованием ASCII-имени."""
    file_ext = os.path.splitext(original_file_name)[1]
    if not file_ext:
        file_ext = ".bin"

    safe_local_name = f"temp_{uuid.uuid4().hex}{file_ext}"
    local_path = os.path.join(".", safe_local_name)

    try:
        file_info = await bot.get_file(file_id)
        
        if file_info.file_size and file_info.file_size > 20 * 1024 * 1024:
            raise ValueError("Размер файла превышает 20 МБ.")

        await bot.download_file(file_info.file_path, local_path)

        mime_type, _ = mimetypes.guess_type(original_file_name)
        if not mime_type:
            mime_type = "application/octet-stream"

        uploaded_file = gemini_client.files.upload(
            file=pathlib.Path(local_path),
            config=genai_types.UploadFileConfig(
                display_name=original_file_name.encode('utf-8', 'ignore').decode('utf-8'),
                mime_type=mime_type
            )
        )
        return uploaded_file

    finally:
        if os.path.exists(local_path):
            os.remove(local_path)


# ================= 6. ОБРАБОТЧИКИ СООБЩЕНИЙ =================

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


@dp.message(Command("set_digest"))
async def handle_set_digest(message: types.Message):
    thread_id = message.message_thread_id
    set_digest_thread(message.chat.id, thread_id)
    await message.reply("📌 **Отлично!** Тимофей зафиксировал этот топик и будет присылать вечерние отчеты в 19:00 прямо сюда.")


@dp.message(Command("remind"))
async def handle_remind(message: types.Message):
    payload = message.text.replace("/remind", "").strip()
    
    if not payload:
        await message.reply(
            "⚠️ **Неверный формат!** Используйте один из вариантов:\n\n"
            "• `/remind 30m Текст` — через 30 минут (или `2h` — через 2 часа)\n"
            "• `/remind 15:30 Текст` — в указанное время сегодня\n"
            "• `/remind 25.08 10:00 Текст` — в конкретную дату и время"
        )
        return

    parts = payload.split(" ", 1)
    time_arg = parts[0].strip()
    reminder_text = parts[1].strip() if len(parts) > 1 else "Без описания"

    now = datetime.now()
    run_datetime = None

    try:
        rel_match = re.match(r"^(\d+)([mhмч])$", time_arg.lower())
        if rel_match:
            amount = int(rel_match.group(1))
            unit = rel_match.group(2)
            if unit in ['m', 'м']:
                run_datetime = now + timedelta(minutes=amount)
            elif unit in ['h', 'ч']:
                run_datetime = now + timedelta(hours=amount)

        elif ":" in time_arg and len(time_arg.split(":")) == 2:
            target_time = datetime.strptime(time_arg, "%H:%M").time()
            run_datetime = datetime.combine(now.date(), target_time)
            if run_datetime <= now:
                run_datetime += timedelta(days=1)

        elif len(parts) > 1 and "." in time_arg:
            full_date_str = f"{time_arg} {parts[1].split(' ')[0]}"
            reminder_text = " ".join(parts[1].split(' ')[1:])
            run_datetime = datetime.strptime(full_date_str, "%d.%m %H:%M").replace(year=now.year)
            if run_datetime <= now:
                run_datetime = run_datetime.replace(year=now.year + 1)

        if not run_datetime:
            raise ValueError("Нераспознанный формат времени")

        scheduler.add_job(
            scheduled_reminder_task,
            'date',
            run_date=run_datetime,
            args=[message.chat.id, reminder_text, message.message_thread_id]
        )

        formatted_time = run_datetime.strftime("%H:%M (%d.%m)")
        await message.reply(
            f"⏰ **Напоминание принято!**\n"
            f"Тимофей напомнит: «_{reminder_text}_»\n"
            f"📅 **Время срабатывания:** {formatted_time}"
        )

    except Exception as e:
        logging.error(f"Ошибка разбора /remind: {e}")
        await message.reply(
            "❌ **Ошибка в формате времени!**\n"
            "Примеры использования:\n"
            "• `/remind 45m Проверить затирку`\n"
            "• `/remind 18:00 Встретить доставку`\n"
            "• `/remind 28.08 12:00 Приедет замерщик`"
        )


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
        response_text = await generate_gemini_response(contents=prompt, system_instruction=SYSTEM_INSTRUCTION)
        save_message(message.chat.id, "Тимофей (ИИ-Прораб)", response_text)
        await send_long_message(message, response_text, status_msg)

    except Exception as e:
        logging.error(f"Ошибка Gemini API: {e}")
        await status_msg.edit_text("❌ Произошла ошибка при обработке запроса.")


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
            response_text = await generate_gemini_response(contents=prompt, system_instruction=SYSTEM_INSTRUCTION)
            save_message(message.chat.id, "Тимофей (ИИ-Прораб)", response_text)
            await send_long_message(message, response_text, status_msg)

        except Exception as e:
            logging.error(f"Ошибка при ответе по имени: {e}")
            await status_msg.edit_text("❌ Извините, не удалось сформировать ответ.")


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
        else:
            await status_msg.edit_text("❌ Неподдерживаемый тип файла.")
            return

        uploaded_file = await process_media_file(bot, file_id, file_name)

        context_history = get_recent_history(message.chat.id, limit=5)
        user_caption = message.caption if message.caption else "Тимофей, проанализируй этот файл в контексте нашего ремонта."

        prompt = (
            f"--- ИСТОРИЯ ЧАТА (ПОСЛЕДНИЕ 5 СООБЩЕНИЙ) ---\n"
            f"{context_history}\n"
            f"---------------------------------------------\n\n"
            f"Пользователь прислал {media_type}.\n"
            f"Комментарий к файлу: {user_caption}"
        )

        response_text = await generate_gemini_response(
            contents=[uploaded_file, prompt],
            system_instruction=SYSTEM_INSTRUCTION
        )

        user_name = message.from_user.full_name or "Пользователь"
        save_message(message.chat.id, user_name, f"[Отправил {media_type}]: {user_caption}")
        save_message(message.chat.id, "Тимофей (ИИ-Прораб)", response_text)

        await send_long_message(message, response_text, status_msg)

    except ValueError as ve:
        logging.warning(f"Ошибка размера файла: {ve}")
        await status_msg.edit_text(f"⚠️ **Ошибка загрузки:** {ve}\nОтправьте документ размером менее 20 МБ.")
    except Exception as e:
        logging.error(f"Ошибка при обработке медиафайла: {e}", exc_info=True)
        await status_msg.edit_text("❌ Не удалось обработать медиафайл. Проверьте размер и формат (поддерживаются PDF, JPG, PNG, MP4, MP3, OGG).")


# ================= 7. ТОЧКА ВХОДА =================
async def main():
    init_db()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )

    scheduler.add_job(send_daily_summary, 'cron', hour=19, minute=0)
    scheduler.start()

    await start_dummy_server()

    session = AiohttpSession(timeout=120)
    custom_bot = Bot(token=TELEGRAM_TOKEN, session=session)

    print("🚀 Бот Тимофей запущен!")
    
    await custom_bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(custom_bot)

if __name__ == "__main__":
    asyncio.run(main())
