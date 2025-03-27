import os
import sqlite3
import logging
import re
from datetime import datetime
import time
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from dotenv import load_dotenv

# Konfiguratsiya
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'default_token')
OWNER_ID = int(os.getenv('OWNER_ID', 0))

# Logging sozlamalari
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    filename='bot.log'
)
logger = logging.getLogger(__name__)

class OffensiveWordManager:
    def __init__(self, db_path='bot_data.db'):
        self.db_path = db_path
        self._create_tables()

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def _create_tables(self):
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS offensive_words (
                        id INTEGER PRIMARY KEY, 
                        word TEXT UNIQUE, 
                        added_at DATETIME
                    )
                ''')
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Jadval yaratishda xatolik: {e}")

    def word_exists(self, word):
        """So'z bazada mavjudligini tekshiradi."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM offensive_words WHERE word = ?", (word.lower().strip(),))
                return cursor.fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"So'z mavjudligini tekshirishda xatolik: {e}")
            return False

    def add_word(self, word):
        try:
            word = word.lower().strip()
            if self.word_exists(word):
                return "exists"  # So'z allaqachon mavjud
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO offensive_words (word, added_at) VALUES (?, ?)",
                    (word, datetime.now())
                )
                conn.commit()
            return "added"  # So'z muvaffaqiyatli qo'shildi
        except sqlite3.Error as e:
            logger.error(f"So'z qo'shishda xatolik: {e}")
            return "error"

    def remove_word(self, word):
        try:
            word = word.lower().strip()
            if not self.word_exists(word):
                return "not_found"  # So'z bazada yo'q
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM offensive_words WHERE word = ?", (word,))
                conn.commit()
            return "removed"  # So'z muvaffaqiyatli o'chirildi
        except sqlite3.Error as e:
            logger.error(f"So'zni o'chirishda xatolik: {e}")
            return "error"

    def get_words(self, limit=None):
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                if limit:
                    cursor.execute("SELECT word FROM offensive_words LIMIT ?", (limit,))
                else:
                    cursor.execute("SELECT word FROM offensive_words")
                return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"So'zlarni olishda xatolik: {e}")
            return []

    def word_count(self):
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM offensive_words")
                return cursor.fetchone()[0]
        except sqlite3.Error as e:
            logger.error(f"So'zlar sonini olishda xatolik: {e}")
            return 0

class TelegramModerator:
    def __init__(self, token):
        self.word_manager = OffensiveWordManager()
        self.token = token
        self.words_per_page = 50

    def _contains_offensive_words(self, text):
        if not text:
            return False
        offensive_words = self.word_manager.get_words()
        text = text.lower()
        for word in offensive_words:
            if re.search(r'\b' + re.escape(word) + r'\b', text):
                return True
        return False

    async def check_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            if not update.message:
                logger.info("Xabar topilmadi (update.message yo'q)")
                return
                
            # Admin va creatorlarni tekshirish
            member = await context.bot.get_chat_member(update.message.chat_id, update.message.from_user.id)
            logger.info(f"Foydalanuvchi statusi: {member.status}")
            if member.status in ['administrator', 'creator', 'left']:
                logger.info("Foydalanuvchi admin yoki creator, tekshiruv o'tkazib yuborildi")
                return
                
            # Matn va captionni olish
            text = update.message.text or update.message.caption or ""
            text = text.lower()
            logger.info(f"Xabar matni yoki caption: {text}")
            
            # Haqoratli so'zlarni tekshirish (matn uchun)
            if self._contains_offensive_words(text):
                await update.message.delete()
                logger.info(f"Haqoratli xabar o'chirildi: {text}")
                return
                
            # Havolalarni tekshirish (YouTube linklaridan tashqari)
            if self._contains_links(text, update.message):
                await update.message.delete()
                logger.info(f"Havolali xabar o'chirildi: {text}")
                return
                
            # APK fayllarni tekshirish va o'chirish
            if update.message.document:
                file_name = update.message.document.file_name or ""
                logger.info(f"Hujjat topildi, fayl nomi: {file_name}")
                if file_name.lower().endswith('.apk'):
                    await update.message.delete()
                    logger.info(f"APK fayl o'chirildi: {file_name}")
                    return
                else:
                    logger.info(f"Fayl .apk emas: {file_name}")
            else:
                logger.info("Xabarda hujjat (document) topilmadi")
                
            # Guruhlangan xabarlarni tekshirish
            if update.message.media_group_id:
                logger.info(f"Guruhlangan xabar aniqlandi, media_group_id: {update.message.media_group_id}")
                has_caption = bool(update.message.caption)
                if has_caption and self._contains_offensive_words(update.message.caption.lower()):
                    # Guruhlangan xabarning barcha qismlarini o'chirish
                    await self.handle_media_group(update, context)
                    return
                else:
                    logger.info("Guruhlangan xabarda haqoratli izoh yo'q, xabar qoldirildi")
                    return
                
            # Oddiy rasm/video + izoh mavjud bo'lsa va izohda haqoratli so'z bo'lsa o'chirish
            has_media = update.message.photo or update.message.video or update.message.document
            has_caption = bool(update.message.caption)
            logger.info(f"Media: {has_media}, Caption: {has_caption}")
            
            if has_media and has_caption:
                if self._contains_offensive_words(update.message.caption.lower()):
                    await update.message.delete()
                    logger.info(f"Media + haqoratli izohli xabar o'chirildi: {update.message.caption}")
                    return
                else:
                    logger.info("Izohda haqoratli so'z yo'q, xabar qoldirildi")
                
        except Exception as e:
            logger.error(f"Xabarni tekshirishda xatolik: {e}")

    async def handle_media_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            media_group_id = update.message.media_group_id
            chat_id = update.message.chat_id
            logger.info(f"Guruhlangan xabarni o'chirish boshlandi, media_group_id: {media_group_id}")

            # Guruhlangan xabarlarni o'chirish uchun qisqa vaqt kutiladi
            await asyncio.sleep(1)  # 1 soniya kutamiz, barcha xabarlar kelishini kutish uchun

            # Chat tarixidan so'nggi xabarlarni olish
            messages = await context.bot.get_chat_history(chat_id=chat_id, limit=10)
            messages_to_delete = []

            for msg in messages:
                if msg.media_group_id == media_group_id:
                    messages_to_delete.append(msg.message_id)

            # Barcha guruhlangan xabarlarni o'chirish
            for message_id in messages_to_delete:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                    logger.info(f"Guruhlangan xabar o'chirildi, message_id: {message_id}")
                except Exception as e:
                    logger.error(f"Guruhlangan xabarni o'chirishda xatolik, message_id: {message_id}, xatolik: {e}")

            logger.info(f"Guruhlangan xabarlar to'liq o'chirildi, media_group_id: {media_group_id}")
        except Exception as e:
            logger.error(f"Guruhlangan xabarni qayta ishlashda xatolik: {e}")

    def _contains_links(self, text, message):
        url_pattern = re.compile(
            r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+|www\.\S+|@[\w]+',
            re.IGNORECASE
        )
        links = url_pattern.findall(text)
        allowed_domains = ['youtube.com', 'youtu.be']
        
        for link in links:
            if any(domain in link for domain in allowed_domains) or link.startswith('@'):
                continue
            return True
            
        entities = message.entities or message.caption_entities or []
        for entity in entities:
            if entity.type in ['url', 'text_link']:
                url = text[entity.offset:entity.offset + entity.length]
                if any(domain in url for domain in allowed_domains):
                    continue
                return True
            elif entity.type == 'mention':
                continue
                
        return False

    async def add_offensive_word(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Iltimos, so'zni kiriting. Masalan: /addword yomon")
            return
        word = " ".join(context.args).lower().strip()
        result = self.word_manager.add_word(word)
        if result == "added":
            await update.message.reply_text(f"✅ '{word}' haqoratli so'zlar ro'yxatiga qo'shildi!")
        elif result == "exists":
            await update.message.reply_text(f"ℹ️ '{word}' bu so'z avval qo'shilgan.")
        elif result == "error":
            await update.message.reply_text(f"❌ '{word}' so'zini qo'shishda xatolik yuz berdi.")

    async def remove_offensive_word(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Iltimos, o'chirish kerak bo'lgan so'zni kiriting. Masalan: /removeword yomon")
            return
        word = " ".join(context.args).lower().strip()
        result = self.word_manager.remove_word(word)
        if result == "removed":
            await update.message.reply_text(f"✅ '{word}' haqoratli so'zlar ro'yxatidan o'chirildi!")
        elif result == "not_found":
            await update.message.reply_text(f"ℹ️ '{word}' so'z ro'yxatda mavjud emas.")
        elif result == "error":
            await update.message.reply_text(f"❌ '{word}' so'zini o'chirishda xatolik yuz berdi.")

    async def show_offensive_words(self, update: Update, context: ContextTypes.DEFAULT_TYPE, page=None):
        logger.info("show_offensive_words funksiyasi chaqirildi")
        offensive_words = self.word_manager.get_words()
        word_count = self.word_manager.word_count()
        logger.info(f"Haqoratli so'zlar soni: {word_count}")

        if not offensive_words:
            await update.message.reply_text("Haqoratli so'zlar ro'yxati bo'sh.")
            return

        total_pages = (word_count + self.words_per_page - 1) // self.words_per_page
        if page is None:
            page = total_pages - 1
        else:
            page = max(0, min(page, total_pages - 1))

        start_idx = page * self.words_per_page
        end_idx = min(start_idx + self.words_per_page, word_count)
        page_words = offensive_words[start_idx:end_idx]

        message = f"Haqoratli so'zlar ro'yxati ({page + 1}/{total_pages} sahifa):\n"
        message += "\n".join(page_words)

        keyboard = []
        if page > 0:
            keyboard.append(InlineKeyboardButton("⏪ Avvalgi", callback_data=f"prev_{page}"))
        if page < total_pages - 1:
            keyboard.append(InlineKeyboardButton("Keyingi ⏩", callback_data=f"next_{page}"))

        reply_markup = InlineKeyboardMarkup([keyboard]) if keyboard else None

        try:
            await update.message.reply_text(message, reply_markup=reply_markup)
            logger.info(f"Sahifa {page + 1}/{total_pages} yuborildi, so'zlar: {len(page_words)}")
        except Exception as e:
            logger.error(f"Xabar yuborishda xatolik: {e}")
            await update.message.reply_text("❌ Ro'yxatni ko'rsatishda xatolik yuz berdi.")

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        data = query.data
        if data.startswith("prev_"):
            current_page = int(data.split("_")[1])
            await self.show_offensive_words(query, context, current_page - 1)
            await query.message.delete()
        elif data.startswith("next_"):
            current_page = int(data.split("_")[1])
            await self.show_offensive_words(query, context, current_page + 1)
            await query.message.delete()

    async def delete_stories_automatically(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            if update.message and hasattr(update.message, 'story') and update.message.story:
                member = await context.bot.get_chat_member(update.message.chat_id, update.message.from_user.id)
                if member.status in ['administrator', 'creator']:
                    return
                await update.message.delete()
                logger.info(f"Story avtomatik o'chirildi: {update.message.message_id}")
        except Exception as e:
            logger.error(f"Hikoyalarni o'chirishda umumiy xatolik: {e}")

def main():
    logging.info("Bot ishga tushirilmoqda...")
    
    try:
        application = Application.builder().token(TOKEN).build()
        moderator = TelegramModerator(TOKEN)

        application.add_handler(CommandHandler('start', lambda update, context: update.message.reply_text("Telegram moderator botiga xush kelibsiz!")))
        application.add_handler(CommandHandler('addword', moderator.add_offensive_word))
        application.add_handler(CommandHandler('removeword', moderator.remove_offensive_word))
        application.add_handler(CommandHandler('showwords', moderator.show_offensive_words))

        application.add_handler(CallbackQueryHandler(moderator.button_handler))
        application.add_handler(MessageHandler(filters.Document.ALL, moderator.check_message))
        application.add_handler(MessageHandler(filters.TEXT, moderator.check_message))
        application.add_handler(MessageHandler(filters.PHOTO, moderator.check_message))
        application.add_handler(MessageHandler(filters.VIDEO, moderator.check_message))
        application.add_handler(MessageHandler(filters.ALL, moderator.delete_stories_automatically))

        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logging.error(f"Botni ishga tushirishda xatolik: {e}")

if __name__ == '__main__':
    main()
