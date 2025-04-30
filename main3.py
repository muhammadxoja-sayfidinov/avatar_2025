import os
import sqlite3
import logging
import re
from datetime import datetime, timedelta
import asyncio
import functools
import ahocorasick
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, JobQueue
)
from dotenv import load_dotenv
from telegram.ext.filters import Sticker

# --- Konfiguratsiya va Logging ---
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'default_token')
OWNER_ID = int(os.getenv('OWNER_ID', 0))

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    filename='bot.log'
)
logger = logging.getLogger(__name__)

# --- OffensiveWordManager sinfi ---
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
            if not word:
                return "error"
            if self.word_exists(word):
                return "exists"
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO offensive_words (word, added_at) VALUES (?, ?)",
                    (word, datetime.now())
                )
                conn.commit()
            return "added"
        except sqlite3.IntegrityError:
            logger.warning(f"'{word}' so'zini qo'shishda poyga holati yoki UNIQUE xatosi.")
            return "exists"
        except sqlite3.Error as e:
            logger.error(f"So'z qo'shishda xatolik: {e}")
            return "error"

    def remove_word(self, word):
        try:
            word = word.lower().strip()
            if not self.word_exists(word):
                return "not_found"
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM offensive_words WHERE word = ?", (word,))
                conn.commit()
            return "removed"
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

# --- Owner ID tekshiruvchi dekorator ---
def owner_only(func):
    @functools.wraps(func)
    async def wrapped(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        chat_type = update.effective_chat.type
        if chat_type == Chat.PRIVATE and user_id != OWNER_ID:
            logger.warning(f"Ruxsatsiz foydalanuvchi (ID: {user_id}) {func.__name__} buyrug'ini ishlatmoqda.")
            await update.message.reply_text("Kechirasiz, bu buyruq faqat bot egasi uchun mo‘ljallangan.")
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapped

# --- TelegramModerator sinfi ---
class TelegramModerator:
    def __init__(self, token, application: Application):
        self.word_manager = OffensiveWordManager()
        self.token = token
        self.application = application
        self.words_per_page = 50
        self.A = ahocorasick.Automaton()
        self._rebuild_automaton()
        self.link_pattern = re.compile(
            r'http[s]?://(?!.*(?:youtube\.com/watch|youtu\.be/|youtube\.com/shorts|m\.youtube\.com))[a-zA-Z0-9./?=&-_%]+' +
            r'|' +
            r'www\.(?!.*(?:youtube\.com/watch|youtu\.be/|youtube\.com/shorts|m\.youtube\.com))[a-zA-Z0-9./?=&-_%]+',
            re.IGNORECASE
        )
        self.mention_pattern = re.compile(r'@[\w]{5,}')
        self.media_group_cache = {}

    def _rebuild_automaton(self):
        logger.info("Aho-Corasick automaton'ni qayta qurish...")
        self.A = ahocorasick.Automaton()
        offensive_words = self.word_manager.get_words()
        for word in offensive_words:
            if word:
                self.A.add_word(word.strip(), word.strip())
        if offensive_words:
            self.A.make_automaton()
            logger.info(f"Automaton {len(offensive_words)} so‘z bilan qurildi.")
        else:
            logger.info("Haqoratli so‘zlar ro‘yxati bo‘sh, automaton qurilmadi.")

    def _contains_offensive_words(self, text):
        if not text or not self.A.kind == ahocorasick.AHOCORASICK:
            return False
        text = text.lower()
        for end_index, found_word in self.A.iter(text):
            start_index = end_index - len(found_word) + 1
            is_start_boundary = start_index == 0 or not text[start_index - 1].isalnum()
            is_end_boundary = end_index == len(text) - 1 or not text[end_index + 1].isalnum()
            if is_start_boundary and is_end_boundary:
                logger.info(f"Haqoratli so‘z topildi: '{found_word}' matnda: '{text[:50]}...'")
                return True
        return False

    def _contains_disallowed_content(self, text, message):
        if not text:
            return None
        text_lower = text.lower()
        if self.link_pattern.search(text_lower):
            logger.info(f"Ruxsatsiz havola topildi: {text[:50]}...")
            return "link"
        if self.mention_pattern.search(text_lower):
            logger.info(f"Mention topildi: {text[:50]}...")
            return "mention"
        entities = message.entities or message.caption_entities or []
        allowed_domains = ['youtube.com/watch', 'youtu.be/', 'youtube.com/shorts', 'm.youtube.com']
        for entity in entities:
            if entity.type == 'url':
                url = text[entity.offset: entity.offset + entity.length].lower()
                if not any(domain in url for domain in allowed_domains):
                    logger.info(f"Entity orqali ruxsatsiz URL topildi: {url}")
                    return "link"
            elif entity.type == 'text_link':
                url = entity.url.lower()
                if not any(domain in url for domain in allowed_domains):
                    logger.info(f"Entity orqali ruxsatsiz text_link topildi: {url}")
                    return "link"
        return None

    async def check_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message:
            return

        chat_id = update.message.chat_id
        user_id = update.message.from_user.id
        message_id = update.message.message_id
        media_group_id = update.message.media_group_id

        try:
            # Guruhlarda admin va creatorlarni tekshirish
            if update.message.chat.type != Chat.PRIVATE:
                member = await context.bot.get_chat_member(chat_id, user_id)
                if member.status in ['administrator', 'creator','left']:
                    logger.info(f"Foydalanuvchi admin yoki creator (ID: {user_id}), tekshiruv o'tkazib yuborildi.")
                    return

            # Story’larni tekshirish
            if update.message.story:
                logger.info(f"Story aniqlandi: Chat ID: {chat_id}, User ID: {user_id}, Story ID: {update.message.story.id}")
                delete_reason = "story"
                try:
                    await update.message.delete()
                    logger.info(f"Story (ID: {update.message.story.id}) muvaffaqiyatli o'chirildi.")
                except Exception as e:
                    logger.error(f"Story’ni o'chirishda xatolik (ID: {update.message.story.id}): {e}")
                return

            # Matn va caption’ni olish
            text_content = update.message.text or update.message.caption or ""
            delete_reason = None

            # Haqoratli so‘zlarni tekshirish
            if self._contains_offensive_words(text_content):
                delete_reason = "haqoratli so'z"

            # Ruxsatsiz kontentni tekshirish
            if not delete_reason:
                disallowed_type = self._contains_disallowed_content(text_content, update.message)
                if disallowed_type == "link":
                    delete_reason = "ruxsatsiz havola"
                elif disallowed_type == "mention":
                    delete_reason = "mention (@username)"

            # APK fayllarni tekshirish
            if not delete_reason and update.message.document:
                file_name = update.message.document.file_name or ""
                if file_name.lower().endswith('.apk'):
                    delete_reason = "APK fayl"
                    logger.info(f"APK fayl aniqlandi: {file_name}")

            # Agar o‘chirish uchun sabab topilsa
            if delete_reason:
                logger.info(f"Xabarni o'chirish sababi: '{delete_reason}'. User ID: {user_id}, Chat ID: {chat_id}, Msg ID: {message_id}")
                if media_group_id:
                    await self.schedule_media_group_check(context, chat_id, media_group_id, message_id, delete_required=True)
                else:
                    try:
                        await update.message.delete()
                        logger.info(f"Xabar (ID: {message_id}) muvaffaqiyatli o'chirildi ({delete_reason}).")
                    except Exception as e:
                        logger.error(f"Xabarni (ID: {message_id}) o'chirishda xatolik: {e}")
                return

            # Media guruh bo‘lsa, keyinchalik tekshirish uchun ro‘yxatga qo‘shamiz
            elif media_group_id:
                await self.schedule_media_group_check(context, chat_id, media_group_id, message_id, delete_required=False)

        except Exception as e:
            logger.error(f"Xabarni tekshirishda umumiy xatolik (Msg ID: {message_id}): {e}", exc_info=True)

    async def schedule_media_group_check(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, media_group_id: str, message_id: int, delete_required: bool):
        job_name = f"media_group_{chat_id}_{media_group_id}"
        if media_group_id not in self.media_group_cache:
            self.media_group_cache[media_group_id] = {
                'chat_id': chat_id,
                'message_ids': set(),
                'delete_required': False,
                'job_scheduled': False
            }
            logger.info(f"Yangi media guruh keshi yaratildi: {media_group_id}")

        group_data = self.media_group_cache[media_group_id]
        group_data['message_ids'].add(message_id)
        if delete_required:
            group_data['delete_required'] = True
            logger.info(f"Media guruh ({media_group_id}) uchun o'chirish bayrog'i o'rnatildi.")

        if not group_data['job_scheduled']:
            context.job_queue.run_once(
                self.process_media_group,
                when=timedelta(seconds=2),
                data={'chat_id': chat_id, 'media_group_id': media_group_id},
                name=job_name
            )
            group_data['job_scheduled'] = True
            logger.info(f"Media guruh ({media_group_id}) uchun tekshirish {job_name} nomi bilan rejalashtirildi.")

    async def process_media_group(self, context: ContextTypes.DEFAULT_TYPE):
        job_data = context.job.data
        chat_id = job_data['chat_id']
        media_group_id = job_data['media_group_id']
        logger.info(f"Media guruh ({media_group_id}) ni qayta ishlash boshlandi.")

        if media_group_id in self.media_group_cache:
            group_data = self.media_group_cache[media_group_id]
            message_ids_to_delete = list(group_data['message_ids'])

            if group_data['delete_required']:
                logger.warning(f"Media guruh ({media_group_id}) o‘chirilmoqda. Xabarlar: {message_ids_to_delete}")
                deleted_count = 0
                for msg_id in message_ids_to_delete:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                        deleted_count += 1
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        logger.error(f"Media guruh xabarini (ID: {msg_id}) o‘chirishda xato: {e}")
                logger.info(f"Media guruh ({media_group_id}) dan {deleted_count}/{len(message_ids_to_delete)} xabar o‘chirildi.")
            else:
                logger.info(f"Media guruh ({media_group_id}) uchun o'chirish talab qilinmagan.")

            del self.media_group_cache[media_group_id]
            logger.info(f"Media guruh ({media_group_id}) keshdan tozalandi.")
        else:
            logger.warning(f"process_media_group chaqirildi, lekin media guruh ({media_group_id}) keshda topilmadi.")

    @owner_only
    async def add_offensive_word(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Iltimos, so'zni kiriting. Masalan: /addword yomon")
            return
        word = " ".join(context.args).lower().strip()
        result = self.word_manager.add_word(word)
        if result == "added":
            await update.message.reply_text(f"✅ '{word}' haqoratli so'zlar ro'yxatiga qo'shildi!")
            self._rebuild_automaton()
        elif result == "exists":
            await update.message.reply_text(f"ℹ️ '{word}' bu so'z avval qo'shilgan.")
        elif result == "error":
            await update.message.reply_text(f"❌ '{word}' so'zini qo'shishda xatolik yuz berdi yoki so'z bo'sh.")

    @owner_only
    async def remove_offensive_word(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Iltimos, o'chirish kerak bo'lgan so'zni kiriting. Masalan: /removeword yomon")
            return
        word = " ".join(context.args).lower().strip()
        result = self.word_manager.remove_word(word)
        if result == "removed":
            await update.message.reply_text(f"✅ '{word}' haqoratli so'zlar ro'yxatidan o'chirildi!")
            self._rebuild_automaton()
        elif result == "not_found":
            await update.message.reply_text(f"ℹ️ '{word}' so'z ro'yxatda mavjud emas.")
        elif result == "error":
            await update.message.reply_text(f"❌ '{word}' so'zini o'chirishda xatolik yuz berdi.")

    @owner_only
    async def show_offensive_words(self, update: Update, context: ContextTypes.DEFAULT_TYPE, page=None):
        message_or_query = update.callback_query.message if update.callback_query else update.message
        logger.info("show_offensive_words funksiyasi chaqirildi")
        offensive_words = self.word_manager.get_words()
        word_count = len(offensive_words)

        if not offensive_words:
            await message_or_query.reply_text("Haqoratli so'zlar ro'yxati bo'sh.")
            return

        total_pages = (word_count + self.words_per_page - 1) // self.words_per_page
        if page is None:
            if not update.callback_query:
                page = max(0, total_pages - 1)
            else:
                page = 0
        else:
            page = max(0, min(page, total_pages - 1))

        start_idx = page * self.words_per_page
        end_idx = min(start_idx + self.words_per_page, word_count)
        page_words = offensive_words[start_idx:end_idx]

        message_text = f"Haqoratli so'zlar ro'yxati ({page + 1}/{total_pages} sahifa, Jami: {word_count}):\n"
        if page_words:
            message_text += "```\n" + "\n".join(page_words) + "\n```"
        else:
            message_text += "_Bu sahifada so'zlar yo'q._"

        keyboard_buttons = []
        row = []
        if page > 0:
            row.append(InlineKeyboardButton("⏪ Avvalgi", callback_data=f"prev_{page-1}"))
        if page < total_pages - 1:
            row.append(InlineKeyboardButton("Keyingi ⏩", callback_data=f"next_{page+1}"))
        if row:
            keyboard_buttons.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_buttons) if keyboard_buttons else None

        try:
            if update.callback_query:
                await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode='MarkdownV2')
                logger.info(f"Sahifa {page + 1}/{total_pages} tahrirlandi.")
            else:
                await message_or_query.reply_text(message_text, reply_markup=reply_markup, parse_mode='MarkdownV2')
                logger.info(f"Sahifa {page + 1}/{total_pages} yuborildi.")
        except Exception as e:
            logger.error(f"Ro'yxatni ko'rsatish/tahrirlashda xatolik: {e}")
            fallback_text = f"Haqoratli so'zlar ro'yxati ({page + 1}/{total_pages} sahifa):\n" + "\n".join(page_words)
            try:
                if update.callback_query:
                    await update.callback_query.edit_message_text(fallback_text, reply_markup=reply_markup)
                else:
                    await message_or_query.reply_text(fallback_text, reply_markup=reply_markup)
            except Exception as fallback_e:
                logger.error(f"Oddiy tekstda ham yuborishda xatolik: {fallback_e}")
                await message_or_query.reply_text("❌ Ro'yxatni ko'rsatishda xatolik yuz berdi.")

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        logger.info(f"Callback query olindi: {data}")

        try:
            action, page_str = data.split("_")
            current_page = int(page_str)
            if action in ["prev", "next"]:
                await self.show_offensive_words(update, context, page=current_page)
        except Exception as e:
            logger.error(f"Callback query'ni ({data}) qayta ishlashda xato: {e}", exc_info=True)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if update.effective_chat.type == Chat.PRIVATE and user_id != OWNER_ID:
            await update.message.reply_text("Salom! Men guruh moderatoriman. Meni guruhga qo‘shing va adminlik huquqini bering.")
        elif update.effective_chat.type == Chat.PRIVATE and user_id == OWNER_ID:
            await update.message.reply_text(
                f"Salom, xo‘jayin! Men ishlashga tayyorman.\n"
                f"Mavjud buyruqlar:\n"
                f"/addword [so‘z] - haqoratli so‘z qo‘shish\n"
                f"/removeword [so‘z] - haqoratli so‘zni o‘chirish\n"
                f"/showwords - haqoratli so‘zlar ro‘yxati"
            )
        else:
            await update.message.reply_text(
                "Salom! Men guruhingizni haqoratli so‘zlar, ruxsatsiz havolalar va APK fayllardan tozalashga yordam beraman."
            )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}", exc_info=context.error)
    if update and update.effective_chat:
        try:
            await update.effective_chat.send_message("Botda xatolik yuz berdi. Iltimos, keyinroq urinib ko‘ring.")
        except Exception as e:
            logger.error(f"Xato xabarini yuborishda xatolik: {e}")

# main funksiyasida:
def main():
    logging.info("Bot ishga tushirilmoqda...")
    try:
        application = Application.builder().token(TOKEN).job_queue(JobQueue()).build()
        moderator = TelegramModerator(TOKEN, application)

        application.add_handler(CommandHandler('start', moderator.start_command))
        application.add_handler(CommandHandler('addword', moderator.add_offensive_word))
        application.add_handler(CommandHandler('removeword', moderator.remove_offensive_word))
        application.add_handler(CommandHandler('showwords', moderator.show_offensive_words))
        application.add_handler(CallbackQueryHandler(moderator.button_handler))

        message_filters = (
            filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO | filters.AUDIO |
            filters.VOICE | filters.VIDEO_NOTE | filters.Sticker.ALL | filters.CONTACT |
            filters.LOCATION | filters.VENUE | filters.POLL | filters.Document.ALL | filters.STORY
        ) & (~filters.UpdateType.EDITED) & (~filters.StatusUpdate.ALL)
        application.add_handler(MessageHandler(message_filters, moderator.check_message))

        # Xato handlerini qo‘shish
        application.add_error_handler(error_handler)

        logging.info("Bot polling rejimida ishga tushdi.")
        application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logging.critical(f"Botni ishga tushirishda kritik xatolik: {e}", exc_info=True)
if __name__ == '__main__':
    main()
