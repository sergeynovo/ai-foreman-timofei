import asyncio
import os
import logging
import sqlite3
from typing import List, Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.enums import ParseMode

from google import genai
from google.genai import types as genai_types

# ================= 1. КОНФИГУРАЦИЯ И СЕКРЕТЫ =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN:
    raise ValueError("Ошибка: не задана переменная окружения TELEGRAM_TOKEN!")
if not GEMINI_API_KEY:
    raise ValueError("Ошибка: не задана переменная окружения GEMINI_API_KEY!")

DB_NAME = "repair_memory.db"

# Имена/триггеры, на которые репликой реагирует бот
BOT_NAMES = ["тимофей", "ии-прораб", "тимофей,", "тимофей!"]

# Системная инструкция ИИ-прораба с именем
SYSTEM_INSTRUCTION = """
Тебя зовут Тимофей. Ты — опытный, строгий и внимательный ИИ-прораб и инженер технадзора по ремонту квартир.
Тебе доступен контекст последних сообщений из строительного чата (кто что обещал, какие материалы согласовали).
Твоя задача:
1. Отвечать на обращения по имени (Тимофей / ИИ-Прораб).
2. Анализировать входящие документы (PDF, изображения, видео, аудио).
3. Давать четкие, технически грамотные ответы по ремонту и защищать интересы владельца квартиры.
4. Предупреждать о технических ошибках, нарушениях технологий или завышении смет.
"""

# Инициализация клиентов
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


# ================= 2. БАЗА ДАННЫХ (Память на 20 сообщений) =================
def init_db():
    """Инициализация SQLite базы данных."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_name TEXT,
            message_text TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def save_message(user_name: str, text: str):
    """Сохранение сообщения в историю."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chat_history (user_name, message_text) VALUES (?, ?)", (user_name, text))
    conn.commit()
    conn.close()


def get_recent_history(limit: int = 20) -> str:
    """Извлечение последних N сообщений из базы данных."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_name, message_text FROM (
            SELECT user_name, message_text, id FROM chat_history ORDER BY id DESC LIMIT ?
        ) ORDER BY id ASC
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return "История пуста."
        
    formatted = "\n".join([f"{user}: {text}" for user, text in rows])
    return formatted


# ================= 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
async def process_media_file(bot: Bot, file_id: str, file_name: str) -> str:
    """Скачивание файла локально и загрузка в Gemini File API."""
    local_path = f"./temp_{file_id}_{file_name}"
    file_info = await bot.get_file(file_id)
    await bot.download_file(file_info.file_path, local_path)
    
    uploaded_file = gemini_client.files.upload(file=local_path)
    
    if os.path.exists(local_path):
        os.remove(local_path)
        
    return uploaded_file


# ================= 4. ОБРАБОТЧИКИ СООБЩЕНИЙ =================

# --- Команда /start ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.reply(
        "🏗️ **ИИ-Прораб Тимофей на связи!**\n\n"
        "Как со мной работать:\n"
        "• Просто обратитесь ко мне по имени в тексте: `Тимофей, проверь смету`\n"
        "• Задайте вопрос через команду `/ask <текст>`\n"
        "• Пришлите фото, PDF, видео или голосовое сообщение — я проанализирую его!"
    )


# --- Обработка обычного текста (Фильтр по имени Тимофей + запись в базу) ---
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text_messages(message: types.Message):
    user_name = message.from_user.full_name or "Пользователь"
    user_text = message.text
    text_lower = user_text.lower()

    # Сначала сохраняем любое текстовое сообщение в историю для контекста
    save_message(user_name, user_text)

    # Проверяем, есть ли обращение к боту по имени
    is_called_by_name = any(name in text_lower for name in BOT_NAMES)

    if is_called_by_name:
        status_msg = await message.reply("⏳ *Тимофей изучает ваш вопрос...*", parse_mode=ParseMode.MARKDOWN)

        context_history = get_recent_history(limit=20)
        prompt = (
            f"--- ИСТОРИЯ ЧАТА (ПОСЛЕДНИЕ 20 СООБЩЕНИЙ) ---\n"
            f"{context_history}\n"
            f"---------------------------------------------\n\n"
            f"Обращение к тебе ({user_name}): {user_text}"
        )

        try:
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)
            )

            # Сохраняем ответ Тимофейа в историю
            save_message("Тимофей (ИИ-Прораб)", response.text)

            await status_msg.edit_text(response.text)

        except Exception as e:
            logging.error(f"Ошибка при ответе по имени: {e}")
            await status_msg.edit_text("❌ Извините, не удалось сформировать ответ.")


# --- Команда /ask ---
@dp.message(Command("ask"))
async def handle_ask(message: types.Message):
    user_query = message.text.replace("/ask", "").strip()
    if not user_query:
        await message.reply("Задайте вопрос после команды, например: `/ask Тимофей, что думаешь по поводу этих розеток?`")
        return

    status_msg = await message.reply("⏳ *Тимофей анализирует запрос...*", parse_mode=ParseMode.MARKDOWN)

    context_history = get_recent_history(limit=20)
    prompt = (
        f"--- ИСТОРИЯ ЧАТА (ПОСЛЕДНИЕ 20 СООБЩЕНИЙ) ---\n"
        f"{context_history}\n"
        f"---------------------------------------------\n\n"
        f"Запрос пользователя: {user_query}"
    )

    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)
        )

        user_name = message.from_user.full_name or "Пользователь"
        save_message(user_name, f"[Запрос /ask]: {user_query}")
        save_message("Тимофей (ИИ-Прораб)", response.text)

        await status_msg.edit_text(response.text)

    except Exception as e:
        logging.error(f"Ошибка Gemini API: {e}")
        await status_msg.edit_text("❌ Произошла ошибка при обработке запроса.")


# --- Обработка медиафайлов: Документы (PDF), Фотографии, Видео и Голосовые ---
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

        context_history = get_recent_history(limit=20)
        user_caption = message.caption if message.caption else "Тимофей, проанализируй этот файл в контексте нашего ремонта."

        prompt = (
            f"--- ИСТОРИЯ ЧАТА (ПОСЛЕДНИЕ 20 СООБЩЕНИЙ) ---\n"
            f"{context_history}\n"
            f"---------------------------------------------\n\n"
            f"Пользователь прислал {media_type}.\n"
            f"Комментарий к файлу: {user_caption}"
        )

        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[uploaded_file, prompt],
            config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)
        )

        user_name = message.from_user.full_name or "Пользователь"
        save_message(user_name, f"[Отправил {media_type}]: {user_caption}")
        save_message("Тимофей (ИИ-Прораб)", response.text)

        await status_msg.edit_text(response.text)

    except Exception as e:
        logging.error(f"Ошибка при обработке медиафайла: {e}")
        await status_msg.edit_text("❌ Не удалось обработать медиафайл.")


# ================= 5. ТОЧКА ВХОДА =================
async def main():
    init_db()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )
    print("🚀 Бот Тимофей с реагированием на имя запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
