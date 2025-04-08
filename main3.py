import os
import sqlite3
import logging
import re
from datetime import datetime, timedelta
import asyncio
import functools # Декоратор учун

# Янги қўшилган импортлар
import ahocorasick
import telegram # telegram.error.Forbidden учун
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, JobQueue
)
from dotenv import load_dotenv
from telegram.ext.filters import Sticker  # Sticker субмодулини импорт қилиш

# --- Конфигурация ва Logging ---
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'default_token')
OWNER_ID = int(os.getenv('OWNER_ID', 0)) # OWNER_ID энди муҳим

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    filename='bot.log'
)
logger = logging.getLogger(__name__)

# --- OffensiveWordManager синфи ---
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
            if not word: return "error" # Бўш сўзни қўшмаслик
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
        except sqlite3.IntegrityError: # Агар UNIQUE constraint бузилса (пойга ҳолати)
             logger.warning(f"'{word}' so'zini qo'shishda пойга ҳолати ёки UNIQUE хатоси.")
             return "exists" # Эҳтимол, аллақачон мавжуд
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
                    cursor.execute("SELECT word FROM offensive_words ORDER BY added_at DESC LIMIT ?", (limit,)) # Yangilarini ko'rsatish uchun tartiblash
                else:
                    cursor.execute("SELECT word FROM offensive_words ORDER BY added_at DESC") # Yangilarini ko'rsatish uchun tartiblash
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

# --- Owner ID текширувчи декоратор ---
def owner_only(func):
    @functools.wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        chat_type = update.effective_chat.type

        # Агар шахсий чат бўлса ва фойдаланувчи owner бўлмаса
        if chat_type == Chat.PRIVATE and user_id != OWNER_ID:
            logger.warning(f"Ruxsatsiz foydalanuvchi (ID: {user_id}) {func.__name__} buyrug'ini ishlatishга urinмоқда.")
            await update.message.reply_text("Кечирасиз, бу буйруқ фақат бот эгаси учун мўлжалланган.")
            return
        # Агар гуруҳ ёки owner бўлса, асл функцияни чақирамиз
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- TelegramModerator синфи ---
class TelegramModerator:
    def __init__(self, token, application: Application): # Application'ни қабул қиламиз
        self.word_manager = OffensiveWordManager()
        self.token = token
        self.application = application # JobQueue учун сақлаймиз
        self.words_per_page = 50
        self.A = ahocorasick.Automaton() # Aho-Corasick Automaton
        self._rebuild_automaton() # Бошланишида қурамиз

        # Рухсат этилмаган контент учун Regex'лар
        # YouTube ва googleusercontent.com доменларини истисно қилиш
        self.link_pattern = re.compile(
            r'http[s]?://(?!.*(?:youtube\.com/watch|youtu\.be/|youtube\.com/shorts|m\.youtube\.com|googleusercontent\.com))[a-zA-Z0-9./?=&-_%]+' +
            r'|' +
            r'www\.(?!.*(?:youtube\.com/watch|youtu\.be/|youtube\.com/shorts|m\.youtube\.com|googleusercontent\.com))[a-zA-Z0-9./?=&-_%]+',
            re.IGNORECASE
        )
        # Камида 5 белгили username'лар (@ билан)
        self.mention_pattern = re.compile(r'@[\w]{5,}')
        # Медиа гуруҳларни сақлаш учун луғат (key: media_group_id)
        self.media_group_cache = {} # Синф атрибути

        # Рухсат этилган доменлар рўйхати (text_link учун)
        self.allowed_domains = ['youtube.com', 'youtu.be', 'googleusercontent.com']


    def _rebuild_automaton(self):
        """Aho-Corasick Automaton'ни қайта қуради."""
        logger.info("Aho-Corasick automaton'ни қайта қуриш...")
        self.A = ahocorasick.Automaton()
        offensive_words = self.word_manager.get_words()
        for word in offensive_words:
            if word: # Бўш сўзларни қўшмаслик
                self.A.add_word(word.strip(), word.strip()) # (keyword, value)
        if offensive_words:
            self.A.make_automaton()
            logger.info(f"Automaton {len(offensive_words)} сўз билан қурилди.")
        else:
            logger.info("Ҳақоратли сўзлар рўйхати бўш, automaton қурилмади.")


    def _contains_offensive_words(self, text):
        """Aho-Corasick орқали ҳақоратли сўзларни текширади."""
        if not text or not self.A.kind == ahocorasick.AHOCORASICK: # Агар automaton қурилмаган бўлса
            return False
        text_lower = text.lower()
        # Automaton'дан фойдаланиб текшириш
        for end_index, found_word in self.A.iter(text_lower):
            # Тўлиқ сўз мослигини текшириш
            start_index = end_index - len(found_word) + 1
            is_start_boundary = start_index == 0 or not text_lower[start_index - 1].isalnum()
            is_end_boundary = end_index == len(text_lower) - 1 or not text_lower[end_index + 1].isalnum()

            if is_start_boundary and is_end_boundary:
                logger.info(f"Ҳақоратли сўз топилди: '{found_word}' матнда: '{text[:50]}...'")
                return True # Биринчи топилган сўздаёқ тўхтаймиз
        return False

    def _contains_disallowed_content(self, text, message: Update.message):
        """Рухсат этилмаган ҳаволалар ёки mention'ларни текширади."""
        if not text: return None # Агар матн бўлмаса

        # Regex орқали текшириш (оддий URL ва www.)
        if self.link_pattern.search(text):
            logger.info(f"Regex орқали рухсат этилмаган ҳавола топилди: {text[:50]}...")
            return "link"

        # Regex орқали mention текшириш (@username)
        if self.mention_pattern.search(text):
            logger.info(f"Mention топилди: {text[:50]}...")
            return "mention"

        # Message Entities орқали текшириш (URL ва text_link учун)
        entities = message.entities or message.caption_entities or []
        for entity in entities:
            url_to_check = None
            if entity.type == 'url': # Оддий URL http://...
                url_to_check = text[entity.offset : entity.offset + entity.length]
            elif entity.type == 'text_link': # Гиперҳавола [text](url)
                 url_to_check = entity.url

            if url_to_check:
                url_lower = url_to_check.lower()
                # Биронта рухсат этилган домен билан бошланадими ёки ўз ичига оладими?
                is_allowed = False
                for domain in self.allowed_domains:
                    # Текширувни яхшилаш: 'google.com' 'notgoogle.com'га мос тушмаслиги учун
                    if f"//{domain}" in url_lower or f"www.{domain}" in url_lower:
                         is_allowed = True
                         break
                if not is_allowed:
                    logger.info(f"Entity ({entity.type}) орқали рухсат этилмаган ҳавола топилди: {url_to_check}")
                    return "link"

            # Mention'ларни entity орқали текшириш (ихтиёрий, regex билан биргаликда)
            # elif entity.type == 'mention':
            #     mention_text = text[entity.offset : entity.offset + entity.length]
            #     if len(mention_text) >= 6: # @ + камида 5 белги
            #         logger.info(f"Entity орқали mention топилди: {mention_text}")
            #         return "mention"

        return None # Ҳеч нарса топилмади

    async def check_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.from_user: # Агар хабар ёки фойдаланувчи бўлмаса
            return

        chat_id = update.message.chat_id
        user_id = update.message.from_user.id
        message_id = update.message.message_id
        media_group_id = update.message.media_group_id

        # OWNER_ID ни ҳар доим ўтказиб юбориш (гуруҳда ҳам)
        if user_id == OWNER_ID:
            # logger.debug(f"Owner (ID: {user_id}) хабари текширилмади.")
            return

        try:
            # Админ ва creatorларни текшириш (гуруҳларда)
            if update.message.chat.type != Chat.PRIVATE:
                member = await context.bot.get_chat_member(chat_id, user_id)
                if member.status in ['administrator', 'creator','left']:
                    # logger.debug(f"Admin/Creator (ID: {user_id}) хабари текширилмади.")
                    return

            # Матн ва caption'ни олиш
            text_content = update.message.text or update.message.caption or ""
            delete_reason = None # Ўчириш сабаби

            # 1. Ҳақоратли сўзларни текшириш
            if self._contains_offensive_words(text_content):
                delete_reason = "haqoratli so'z"

            # 2. Рухсат этилмаган контентни текшириш (агар ҳақорат топилмаган бўлса)
            if not delete_reason:
                 disallowed_type = self._contains_disallowed_content(text_content, update.message)
                 if disallowed_type == "link":
                     delete_reason = "ruxsatsiz havola"
                 elif disallowed_type == "mention":
                     delete_reason = "mention (@username)"

            # 3. APK файлларни текшириш (агар олдинги сабаблар бўлмаса)
            if not delete_reason and update.message.document:
                file_name = update.message.document.file_name or ""
                if file_name.lower().endswith('.apk'):
                    delete_reason = "APK fayl"
                    logger.info(f"APK fayl aniqlandi: {file_name}")

            # Агар ўчириш учун сабаб топилган бўлса
            if delete_reason:
                logger.info(f"Xabarni o'chirish сабаби: '{delete_reason}'. User ID: {user_id}, Chat ID: {chat_id}, Msg ID: {message_id}")

                if media_group_id:
                    # Медиа гуруҳга тегишли бўлса, кэшга белги қўямиз
                    await self.schedule_media_group_check(context, chat_id, media_group_id, message_id, delete_required=True)
                else:
                    # Оддий хабар бўлса, дарҳол ўчирамиз
                    try:
                        await update.message.delete()
                        logger.info(f"Xabar (ID: {message_id}) muvaffaqiyatli o'chirildi ({delete_reason}).")
                    except telegram.error.Forbidden as e:
                        logger.error(f"Xabarni (ID: {message_id}) o'chirish uchun ruxsat yo'q: {e}")
                    except Exception as e:
                        logger.error(f"Xabarni (ID: {message_id}) o'chirishda xatolik: {e}")
                return # Текширувни тугатамиз, чунки сабаб топилди

            # Агар медиа гуруҳ бўлса, лекин юқоридаги текширувларда ўчириш сабаби топилмаса ҳам,
            # уни кейинчалик текшириш учун рўйхатга қўшамиз (бошқа қисмда сабаб бўлиши мумкин)
            elif media_group_id:
                 await self.schedule_media_group_check(context, chat_id, media_group_id, message_id, delete_required=False)

        except telegram.error.BadRequest as e:
             if "member not found" in str(e).lower():
                 logger.warning(f"Foydalanuvchi (ID: {user_id}) chat ({chat_id}) a'zosi emas. Tekshirish o'tkazib yuborildi.")
             else:
                 logger.error(f"Xabarni tekshirishda BadRequest xatoligi (Msg ID: {message_id}): {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Xabarni tekshirishda umumiy xatolik (Msg ID: {message_id}): {e}", exc_info=True)

    async def schedule_media_group_check(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, media_group_id: str, message_id: int, delete_required: bool):
        """Медиа гуруҳни текширишни режалаштиради ва хабар IDларини сақлайди."""
        job_name = f"media_group_{chat_id}_{media_group_id}"

        # Кэшда шу гуруҳ ҳақида ёзув борми?
        if media_group_id not in self.media_group_cache:
            self.media_group_cache[media_group_id] = {
                'chat_id': chat_id,
                'message_ids': set(), # Такрорланмаслиги учун set
                'delete_required': False, # Ҳозирча ўчириш шарт эмас
                'job_scheduled': False
            }
            logger.info(f"Yangi media guruh kэши яратилди: {media_group_id}")

        group_data = self.media_group_cache[media_group_id]
        group_data['message_ids'].add(message_id) # Хабар IDсини қўшамиз

        # Агар шу хабар сабабли ёки олдинроқ ўчириш кераклиги аниқланган бўлса
        if delete_required:
            group_data['delete_required'] = True
            logger.info(f"Media guruh ({media_group_id}) uchun o'chirish bayrog'i o'rnatildi.")

        # Агар текшириш ҳали режалаштирилмаган бўлса
        if not group_data['job_scheduled']:
            # 2 сониядан кейин process_media_group ни ишга туширамиз (олдин 3 эди)
            context.job_queue.run_once(
                self.process_media_group,
                when=timedelta(seconds=2), # 2 сония кутамиз
                data={'chat_id': chat_id, 'media_group_id': media_group_id},
                name=job_name
            )
            group_data['job_scheduled'] = True
            logger.info(f"Media guruh ({media_group_id}) uchun tekshirish {job_name} номи билан режалаштирилди.")


    async def process_media_group(self, context: ContextTypes.DEFAULT_TYPE):
        """Режалаштирилган вазифа: Медиа гуруҳ хабарларини ўчиради (агар керак бўлса)."""
        job_data = context.job.data
        chat_id = job_data['chat_id']
        media_group_id = job_data['media_group_id']

        logger.info(f"Media guruh ({media_group_id}) ни қайта ишлаш бошланди.")

        if media_group_id in self.media_group_cache:
            group_data = self.media_group_cache[media_group_id]
            message_ids_to_delete = list(group_data['message_ids']) # Ўчириш учун рўйхат

            if group_data['delete_required']:
                logger.warning(f"Media guruh ({media_group_id}) ўчирилмоқда. Хабарлар: {message_ids_to_delete}")
                try:
                    # Бир нечта хабарни бирданига ўчиришга ҳаракат қилиш
                    await context.bot.delete_messages(chat_id=chat_id, message_ids=message_ids_to_delete)
                    logger.info(f"Media guruh ({media_group_id}) дан {len(message_ids_to_delete)} хабар delete_messages орқали ўчирилди.")
                except telegram.error.BadRequest as e:
                     # Агар delete_messages ишламаса (эски хабарлар, бошқа сабаб), яккама-якка ўчирамиз
                     logger.warning(f"delete_messages хато берди ({e}), хабарларни яккама-якка ўчиришга ҳаракат қилинади.")
                     deleted_count = 0
                     for msg_id in message_ids_to_delete:
                         try:
                             await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                             deleted_count += 1
                             await asyncio.sleep(0.1) # Кичик танаффус
                         except Exception as single_e:
                             logger.error(f"Media guruh хабарини (ID: {msg_id}) яккама-якка ўчиришда хато: {single_e}")
                     logger.info(f"Media guruh ({media_group_id}) дан {deleted_count}/{len(message_ids_to_delete)} хабар яккама-якка ўчирилди.")
                except telegram.error.Forbidden as e:
                     logger.error(f"Media guruh ({media_group_id}) хабарларини ўчириш учун рухсат йўқ: {e}")
                except Exception as e:
                     logger.error(f"Media guruh ({media_group_id}) хабарларини ўчиришда кутилмаган хатолик: {e}")
            else:
                logger.info(f"Media guruh ({media_group_id}) uchun o'chirish талаб қилинмаган.")

            # Гуруҳни кэшдан тозалаш
            # Агар cache'да ҳали ҳам мавжуд бўлса (пойга ҳолати бўлиши мумкин)
            if media_group_id in self.media_group_cache:
                del self.media_group_cache[media_group_id]
                logger.info(f"Media guruh ({media_group_id}) кэшдан тозаланди.")
        else:
            logger.warning(f"process_media_group чақирилди, лекин media guruh ({media_group_id}) кэшда топилмади.")


    @owner_only # Фақат owner учун
    async def add_offensive_word(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Iltimos, so'zni kiriting. Masalan: `/addword yomon so'z`", parse_mode='MarkdownV2')
            return
        word = " ".join(context.args).lower().strip()
        result = self.word_manager.add_word(word)
        if result == "added":
            await update.message.reply_text(f"✅ '{word}' haqoratli so'zlar ro'yxatiga qo'shildi!")
            self._rebuild_automaton() # Automaton'ни янгилаймиз
        elif result == "exists":
            await update.message.reply_text(f"ℹ️ '{word}' bu so'z avval qo'shilgan.")
        elif result == "error":
            await update.message.reply_text(f"❌ '{word}' so'zini qo'shishda xatolik yuz berdi yoki so'z bo'sh.")

    @owner_only # Фақат owner учун
    async def remove_offensive_word(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Iltimos, o'chirish kerak bo'lgan so'zni kiriting. Masalan: `/removeword yomon so'z`", parse_mode='MarkdownV2')
            return
        word = " ".join(context.args).lower().strip()
        result = self.word_manager.remove_word(word)
        if result == "removed":
            await update.message.reply_text(f"✅ '{word}' haqoratli so'zlar ro'yxatidan o'chirildi!")
            self._rebuild_automaton() # Automaton'ни янгилаймиз
        elif result == "not_found":
            await update.message.reply_text(f"ℹ️ '{word}' so'z ro'yxatda mavjud emas.")
        elif result == "error":
            await update.message.reply_text(f"❌ '{word}' so'zini o'chirishda xatolik yuz berdi.")

    @owner_only # Фақат owner учун (шахсий чатда ишлайди)
    async def show_offensive_words(self, update: Update, context: ContextTypes.DEFAULT_TYPE, page=0): # Стандарт page=0
        message_or_query = update.callback_query.message if update.callback_query else update.message
        is_callback = update.callback_query is not None

        logger.info(f"show_offensive_words chaqirildi (page={page}, is_callback={is_callback})")
        offensive_words = self.word_manager.get_words() # Янгилари биринчи келади
        word_count = len(offensive_words)
        logger.info(f"Haqoratli so'zlar soni: {word_count}")

        if not offensive_words:
            text = "Haqoratli so'zlar ro'yxati bo'sh."
            if is_callback:
                await update.callback_query.edit_message_text(text)
            else:
                await message_or_query.reply_text(text)
            return

        total_pages = (word_count + self.words_per_page - 1) // self.words_per_page
        current_page = max(0, min(page, total_pages - 1)) # Саҳифа чегараларини текшириш

        start_idx = current_page * self.words_per_page
        end_idx = min(start_idx + self.words_per_page, word_count)
        page_words = offensive_words[start_idx:end_idx]

        # MarkdownV2 учун махсус белгиларни escape қилиш
        def escape_md(text):
             escape_chars = r'_*[]()~`>#+-=|{}.!'
             return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

        message_text = f"*Haqoratli so'zlar ro'yxati* ({current_page + 1}/{total_pages} sahifa, Jami: {word_count}):\n"
        if page_words:
            # Сўзларни рақамлаб чиқариш
            message_text += "```\n"
            for i, word in enumerate(page_words, start=start_idx + 1):
                 # Ҳар бир сўзни escape қилиш шарт эмас, чунки улар код блоки ичида
                 message_text += f"{i}. {word}\n"
            message_text += "```"
        else:
             # Бу ҳолат юз бермаслиги керак, лекин ҳар эҳтимолга қарши
            message_text += "_Bu sahifada so'zlar yo'q\\._"


        keyboard_buttons = []
        row = []
        if current_page > 0:
            row.append(InlineKeyboardButton("⏪ Avvalgi", callback_data=f"prev_{current_page-1}"))
        if current_page < total_pages - 1:
            row.append(InlineKeyboardButton("Keyingi ⏩", callback_data=f"next_{current_page+1}"))
        if row:
            keyboard_buttons.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_buttons) if keyboard_buttons else None

        try:
            if is_callback:
                 # Агар матн ва тугмалар ўзгармаган бўлса, хато бермаслиги учун текшириш
                 if update.callback_query.message.text != message_text or update.callback_query.message.reply_markup != reply_markup:
                     await update.callback_query.edit_message_text(
                         message_text,
                         reply_markup=reply_markup,
                         parse_mode='MarkdownV2'
                     )
                     logger.info(f"Sahifa {current_page + 1}/{total_pages} таҳрирланди.")
                 else:
                      await update.callback_query.answer("Sahifa o'zgarmadi.") # Фойдаланувчига билдириш
            else:
                await message_or_query.reply_text(
                    message_text,
                    reply_markup=reply_markup,
                    parse_mode='MarkdownV2'
                )
                logger.info(f"Sahifa {current_page + 1}/{total_pages} юборилди.")
        except telegram.error.BadRequest as e:
             if "Message is not modified" in str(e):
                 logger.info("Хабар ўзгармаганлиги сабабли таҳрирланмади.")
                 await update.callback_query.answer("Sahifa allaqachon ko'rsatilgan.")
             else:
                 logger.error(f"Ro'yxatni ko'rsatish/tahrirlashda BadRequest хатоси: {e}")
                 await message_or_query.reply_text("❌ Ro'yxatni ko'rsatishда хатолик юз берди (BadRequest).")
        except Exception as e:
            logger.error(f"Ro'yxatni ko'rsatish/tahrirlashda кутилмаган хатолик: {e}", exc_info=True)
            # Markdown хатоси бўлса, оддий текстда юбориб кўриш қийин, чунки escape керак
            await message_or_query.reply_text("❌ Ro'yxatni ko'rsatishда хатолик юз берди.")


    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        # Тугма босилганлигини тасдиқлаш (жавоб бериш шарт)
        await query.answer()

        data = query.data
        logger.info(f"Callback query олинди: {data}")

        try:
            action, page_str = data.split("_")
            page = int(page_str)

            if action in ["prev", "next"]:
                 # Айнан шу функцияни қайта чақирамиз, page аргументи билан
                 await self.show_offensive_words(update, context, page=page)
        except Exception as e:
             logger.error(f"Callback query'ни ({data}) қайта ишлашда хато: {e}", exc_info=True)


    # ЯНГИ ФУНКЦИЯ: Ҳикояларни ушлаш ва ўчириш учун
    async def handle_story(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.story or not update.story.from_user: # Ҳикоя ёки юборувчи йўқ бўлса
            logger.debug("Story update received but story or user info is missing.")
            return

        story = update.story
        user_id = story.from_user.id
        chat_id = story.chat_id  # Ҳикоя қаерда кўринса (гуруҳ/канал/профил)
        story_id = story.id      # Ҳикоянинг IDси

        # OWNER_ID нинг ҳикояларини ўчирмаслик
        if user_id == OWNER_ID:
            # logger.debug(f"Owner's story (ID: {story_id}) ignored.")
            return

        logger.info(f"Story received in chat {chat_id} from user {user_id}. Story ID: {story_id}")

        try:
            # Бот фақат гуруҳлардаги (GROUP, SUPERGROUP) ҳикояларни ўчириши керак
            chat = await context.bot.get_chat(chat_id)
            if chat.type not in [Chat.GROUP, Chat.SUPERGROUP]:
                logger.debug(f"Ignoring story {story_id} in non-group chat ({chat_id}, type: {chat.type}).")
                return

             # Гуруҳдаги админ/creator'ларни текширамиз
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status in ['administrator', 'creator','left']:
                logger.info(f"User {user_id} is admin/creator in chat {chat_id}, not deleting story {story_id}.")
                return

            # Ҳикояни ўчириш
            # delete_story методи message_id параметрини кутади, унга story.id берилади
            deleted = await context.bot.delete_story(chat_id=chat_id, message_id=story_id)
            if deleted:
                logger.info(f"Story (ID: {story_id}) deleted successfully from chat {chat_id} (sent by user {user_id}).")
            else:
                # API False қайтариши мумкин (масалан, аллақачон ўчирилган бўлса)
                logger.warning(f"Attempted to delete story (ID: {story_id}) from chat {chat_id}, but API returned False (possibly already deleted?).")

        except telegram.error.Forbidden as e:
             logger.error(f"Permission error deleting story (ID: {story_id}) in chat {chat_id}. Does the bot have 'delete messages' permission? Error: {e}")
        except telegram.error.BadRequest as e:
             logger.error(f"Bad request deleting story (ID: {story_id}) in chat {chat_id}. Maybe story expired or already deleted? Error: {e}")
        except Exception as e:
            logger.error(f"Failed to process/delete story (ID: {story_id}) in chat {chat_id}: {e}", exc_info=True)


    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if update.effective_chat.type == Chat.PRIVATE and user_id != OWNER_ID:
            await update.message.reply_text("Салом! Мен гуруҳ модераториман. Мени гуруҳга қўшинг ва хабарларни ўчириш ҳуқуқини беринг.")
        elif update.effective_chat.type == Chat.PRIVATE and user_id == OWNER_ID:
             # MarkdownV2 учун буйруқларни escape қилиш
             add_cmd = escape_md("/addword [сўз]")
             remove_cmd = escape_md("/removeword [сўз]")
             show_cmd = escape_md("/showwords")
             await update.message.reply_text(f"Салом, хўжайин\\! Мен ишлашга тайёрман\\.\n"
                                             f"*Мавжуд буйруқлар:*\n"
                                             f"{add_cmd} \\- ҳақоратли сўз қўшиш\n"
                                             f"{remove_cmd} \\- ҳақоратли сўзни ўчириш\n"
                                             f"{show_cmd} \\- ҳақоратли сўзлар рўйхати",
                                             parse_mode='MarkdownV2')
        else: # Гуруҳда /start ёзилса
             await update.message.reply_text("Салом! Мен гуруҳингизни ҳақоратли сўзлар, рухсатсиз ҳаволалар, mention'лар, APK файллар ва ҳикоялардан тозалашга ёрдам бераман. Ишлашим учун 'Delete Messages' рухсатини беришни унутманг.")


def main():
    logging.info("Бот ишга туширилмоқда...")
    if not TOKEN or TOKEN == 'default_token':
        logging.critical("TELEGRAM_BOT_TOKEN .env файлида топилмади ёки нотўғри!")
        return
    if OWNER_ID == 0:
         logging.warning("OWNER_ID .env файлида топилмади ёки 0 га тенг! Баъзи буйруқлар ишламайди.")


    try:
        # Application'ни JobQueue билан яратамиз
        application = Application.builder().token(TOKEN).job_queue(JobQueue()).build()

        # Moderator'га application'ни узатамиз
        moderator = TelegramModerator(TOKEN, application)

        # --- Handler'ларни қўшиш ---

        # 1. Буйруқ Handler'лари (фақат owner учун - декоратор текширади)
        application.add_handler(CommandHandler('start', moderator.start_command))
        application.add_handler(CommandHandler('addword', moderator.add_offensive_word))
        application.add_handler(CommandHandler('removeword', moderator.remove_offensive_word))
        application.add_handler(CommandHandler('showwords', moderator.show_offensive_words))

        # 2. Callback Query Handler (пагинация тугмалари учун)
        application.add_handler(CallbackQueryHandler(moderator.button_handler))

        # 3. Асосий хабар Handler (текст, медиа, apk ва ҳ.к.)
        #    ~filters.COMMAND - буйруқларни бу handler ушламаслиги учун
        #    ~filters.UpdateType.EDITED - таҳрирланган хабарларни ушламаслик
        #    ~filters.StatusUpdate - сервис хабарларини ушламаслик (joined, left,...)
        message_filters = (
            filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO | filters.AUDIO |
            filters.VOICE | filters.VIDEO_NOTE | filters.Sticker.ALL | filters.CONTACT |
            filters.LOCATION | filters.VENUE | filters.POLL | filters.Document.ALL
        ) & (~filters.COMMAND) & (~filters.UpdateType.EDITED) & (~filters.StatusUpdate.ALL)

        # Бу handler'ни check_message функциясига боғлаймиз
        application.add_handler(MessageHandler(message_filters, moderator.check_message))

        # 4. Ҳикоя (Story) Handler
        #    Бу handler фақат ҳикояларни ушлайди ва handle_story функциясига юборади
        application.add_handler(MessageHandler(filters.UpdateType.STORY, moderator.handle_story))

        # --- Ботни ишга тушириш ---
        logging.info("Бот polling режимида ишга тушди.")
        # Барча турдаги update'ларни қабул қилиш (бу handler'лар орқали филтрланади)
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

    except Exception as e:
        logging.critical(f"Ботни ишга туширишда критик хатолик: {e}", exc_info=True)

if __name__ == '__main__':
    main()
