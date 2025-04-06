import os
import sqlite3
import logging
import re
from datetime import datetime, timedelta
import asyncio
import functools # Декоратор учун

# Янги қўшилган импортлар
import ahocorasick
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, JobQueue
)
from dotenv import load_dotenv
from telegram.ext.filters import Sticker  # Sticker субмодулини импорт қилиш

# --- Конфигурация ва Logging (ўзгаришсиз) ---
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'default_token')
OWNER_ID = int(os.getenv('OWNER_ID', 0)) # OWNER_ID энди муҳим

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    filename='bot.log'
)
logger = logging.getLogger(__name__)

# --- OffensiveWordManager синфи (ўзгаришсиз) ---
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

# --- TelegramModerator синфи (ўзгартирилган) ---
class TelegramModerator:
    def __init__(self, token, application: Application): # Application'ни қабул қиламиз
        self.word_manager = OffensiveWordManager()
        self.token = token
        self.application = application # JobQueue учун сақлаймиз
        self.words_per_page = 50
        self.A = ahocorasick.Automaton() # Aho-Corasick Automaton
        self._rebuild_automaton() # Бошланишида қурамиз

        # Рухсат этилмаган контент учун Regex'лар
        self.link_pattern = re.compile(
             # Оддий URLлар, www. билан бошланадиганлар (YouTube'дан ташқари)
             r'http[s]?://(?!.*(?:youtube\.com/watch|youtu\.be/|youtube\.com/shorts|m\.youtube\.com))[a-zA-Z0-9./?=&-_%]+' +
             r'|' +
             r'www\.(?!.*(?:youtube\.com/watch|youtu\.be/|youtube\.com/shorts|m\.youtube\.com))[a-zA-Z0-9./?=&-_%]+',
            re.IGNORECASE
        )
        # Камида 5 белгили username'лар (@ билан)
        self.mention_pattern = re.compile(r'@[\w]{5,}')
        # Медиа гуруҳларни сақлаш учун луғат (key: media_group_id)
        self.media_group_cache = {} # Эски bot_data ўрнига синф атрибути


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
        text = text.lower()
        # Automaton'дан фойдаланиб текшириш
        for end_index, found_word in self.A.iter(text):
             # Тўлиқ сўз мослигини текшириш (масалан, 'ass' 'massage' ичида топилмаслиги учун)
             start_index = end_index - len(found_word) + 1
             # Сўз бошида ёки олдида бўшлиқ/пунктуация борми?
             is_start_boundary = start_index == 0 or not text[start_index - 1].isalnum()
             # Сўз охирида ёки кейинида бўшлиқ/пунктуация борми?
             is_end_boundary = end_index == len(text) - 1 or not text[end_index + 1].isalnum()

             if is_start_boundary and is_end_boundary:
                logger.info(f"Ҳақоратли сўз топилди: '{found_word}' матнда: '{text[:50]}...'")
                return True # Биринчи топилган сўздаёқ тўхтаймиз
        return False

    def _contains_disallowed_content(self, text, message: Update.message):
        """Рухсат этилмаган ҳаволалар ёки mention'ларни текширади."""
        if not text: return None # Агар матн бўлмаса, бузмаймиз

        text_lower = text.lower()

        # Рухсат этилмаган ҳаволаларни қидириш
        if self.link_pattern.search(text_lower):
            logger.info(f"Рухсат этилмаган ҳавола топилди: {text[:50]}...")
            return "link"

        # Mention'ларни қидириш (@username)
        if self.mention_pattern.search(text_lower):
             # Ўзини-ўзи mention қилишни ёки ботни mention қилишни ўтказиб юбориш (ихтиёрий)
             # if message.from_user and f"@{message.from_user.username}" in text: pass
             # bot_username = context.bot.username # Буни олиш керак бўлади
             # if f"@{bot_username}" in text: pass
            logger.info(f"Mention топилди: {text[:50]}...")
            return "mention"

        # Message Entities орқали текшириш (text_link учун)
        entities = message.entities or message.caption_entities or []
        allowed_domains = ['youtube.com/watch', 'youtu.be/', 'youtube.com/shorts', 'm.youtube.com'] # Рухсат этилган доменлар

        for entity in entities:
            if entity.type == 'url': # Оддий URL http://...
                url = text[entity.offset : entity.offset + entity.length].lower()
                if not any(domain in url for domain in allowed_domains):
                    logger.info(f"Entity орқали рухсат этилмаган URL топилди: {url}")
                    return "link"
            elif entity.type == 'text_link': # Гиперҳавола [text](url)
                 url = entity.url.lower()
                 if not any(domain in url for domain in allowed_domains):
                    logger.info(f"Entity орқали рухсат этилмаган text_link топилди: {url}")
                    return "link"
            # elif entity.type == 'mention': # @username entity турини ҳам текшириш мумкин
            #     logger.info(f"Entity орқали mention топилди: {text[entity.offset : entity.offset + entity.length]}")
            #     return "mention" # Юқоридаги regex билан бирга ишлаши мумкин

        return None # Ҳеч нарса топилмади

    async def check_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message:
            # logger.info("Xabar topilmadi (update.message yo'q)")
            return

        chat_id = update.message.chat_id
        user_id = update.message.from_user.id
        message_id = update.message.message_id
        media_group_id = update.message.media_group_id

        try:
            # Админ ва creatorларни текшириш (гуруҳларда)
            if update.message.chat.type != Chat.PRIVATE:
                member = await context.bot.get_chat_member(chat_id, user_id)
                # logger.info(f"Foydalanuvchi statusi ({user_id} in {chat_id}): {member.status}")
                if member.status in ['administrator', 'creator','left']:
                    # logger.info("Foydalanuvchi admin yoki creator, tekshiruv o'tkazib yuborildi")
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
                    except Exception as e:
                        logger.error(f"Xabarni (ID: {message_id}) o'chirishda xatolik: {e}")
                return # Текширувни тугатамиз, чунки сабаб топилди

            # Агар медиа гуруҳ бўлса, лекин юқоридаги текширувларда ўчириш сабаби топилмаса ҳам,
            # уни кейинчалик текшириш учун рўйхатга қўшамиз (бошқа қисмда сабаб бўлиши мумкин)
            elif media_group_id:
                 await self.schedule_media_group_check(context, chat_id, media_group_id, message_id, delete_required=False)


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
            # 3 сониядан кейин process_media_group ни ишга туширамиз
            context.job_queue.run_once(
                self.process_media_group,
                when=timedelta(seconds=2), # 3 сония кутамиз
                data={'chat_id': chat_id, 'media_group_id': media_group_id},
                name=job_name
            )
            group_data['job_scheduled'] = True
            logger.info(f"Media guruh ({media_group_id}) uchun tekshirish {job_name} номи билан режалаштирилди.")
        # else:
            # logger.debug(f"Media guruh ({media_group_id}) uchun tekshirish allaqachon rejalashtirilgan.")


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
                deleted_count = 0
                for msg_id in message_ids_to_delete:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                        deleted_count += 1
                        await asyncio.sleep(0.1) # Кичик танаффус (flood wait олдини олиш учун)
                    except Exception as e:
                        # Агар хабар аллақачон ўчирилган бўлса ёки бошқа хато
                        logger.error(f"Media guruh хабарини (ID: {msg_id}) ўчиришда хато: {e}")
                logger.info(f"Media guruh ({media_group_id}) дан {deleted_count}/{len(message_ids_to_delete)} хабар ўчирилди.")
            else:
                logger.info(f"Media guruh ({media_group_id}) uchun o'chirish талаб қилинмаган.")

            # Гуруҳни кэшдан тозалаш
            del self.media_group_cache[media_group_id]
            logger.info(f"Media guruh ({media_group_id}) кэшдан тозаланди.")
        else:
            logger.warning(f"process_media_group чақирилди, лекин media guruh ({media_group_id}) кэшда топилмади.")


    @owner_only # Фақат owner учун
    async def add_offensive_word(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Iltimos, so'zni kiriting. Masalan: /addword yomon")
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
            await update.message.reply_text("Iltimos, o'chirish kerak bo'lgan so'zni kiriting. Masalan: /removeword yomon")
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
    async def show_offensive_words(self, update: Update, context: ContextTypes.DEFAULT_TYPE, page=None):
        # Бу функцияни энди update.message орқали чақирамиз, query орқали эмас
        message_or_query = update.callback_query.message if update.callback_query else update.message

        logger.info("show_offensive_words funksiyasi chaqirildi")
        offensive_words = self.word_manager.get_words()
        word_count = len(offensive_words) # Базага қайта сўров жўнатмаймиз
        logger.info(f"Haqoratli so'zlar soni: {word_count}")

        if not offensive_words:
            await message_or_query.reply_text("Haqoratli so'zlar ro'yxati bo'sh.")
            return

        total_pages = (word_count + self.words_per_page - 1) // self.words_per_page
        if page is None:
            # Агар page берилмаса ва бу callback query бўлмаса, охирги саҳифани кўрсатамиз
             if not update.callback_query:
                 page = max(0, total_pages - 1)
             else:
                  page = 0 # Callback'да page аниқ келади, бу ҳолат кузатилмаслиги керак
        else:
             page = max(0, min(page, total_pages - 1)) # Саҳифа чегараларини текшириш

        start_idx = page * self.words_per_page
        end_idx = min(start_idx + self.words_per_page, word_count)
        page_words = offensive_words[start_idx:end_idx]

        message_text = f"Haqoratli so'zlar ro'yxati ({page + 1}/{total_pages} sahifa, Jami: {word_count}):\n"
        if page_words:
             message_text += "```\n" # Код блокида чиройли кўриниши учун
             message_text += "\n".join(page_words)
             message_text += "\n```"
        else:
             message_text += "_Bu sahifada so'zlar yo'q._"


        keyboard_buttons = []
        row = []
        if page > 0:
            row.append(InlineKeyboardButton("⏪ Avvalgi", callback_data=f"prev_{page-1}")) # Тўғриланган page
        if page < total_pages - 1:
            row.append(InlineKeyboardButton("Keyingi ⏩", callback_data=f"next_{page+1}")) # Тўғриланган page
        if row:
             keyboard_buttons.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_buttons) if keyboard_buttons else None

        try:
            # Агар бу callback query бўлса, хабарни таҳрирлаймиз
            if update.callback_query:
                 await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode='MarkdownV2')
                 logger.info(f"Sahifa {page + 1}/{total_pages} таҳрирланди.")
            # Агар бу /showwords буйруғи бўлса, янги хабар юборамиз
            else:
                 await message_or_query.reply_text(message_text, reply_markup=reply_markup, parse_mode='MarkdownV2')
                 logger.info(f"Sahifa {page + 1}/{total_pages} юборилди.")
        except Exception as e:
            logger.error(f"Ro'yxatni ko'rsatish/tahrirlashda xatolik: {e}")
            # Агар Markdown хатоси бўлса, оддий текстда юбориб кўрамиз
            try:
                 fallback_text = f"Haqoratli so'zlar ro'yxati ({page + 1}/{total_pages} sahifa):\n" + "\n".join(page_words)
                 if update.callback_query:
                     await update.callback_query.edit_message_text(fallback_text, reply_markup=reply_markup)
                 else:
                     await message_or_query.reply_text(fallback_text, reply_markup=reply_markup)
            except Exception as fallback_e:
                 logger.error(f"Оддий текстда ҳам юборишда хатолик: {fallback_e}")
                 await message_or_query.reply_text("❌ Ro'yxatni ko'rsatishда хатолик юз берди.")


    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer() # Тугма босилганлигини тасдиқлаш

        data = query.data
        logger.info(f"Callback query олинди: {data}")

        try:
            action, page_str = data.split("_")
            current_page = int(page_str)

            if action == "prev":
                await self.show_offensive_words(update, context, page=current_page) # Янги page'ни берамиз
            elif action == "next":
                await self.show_offensive_words(update, context, page=current_page) # Янги page'ни берамиз
            # Эски хабарни ўчирмаймиз, edit_message_text орқали таҳрирлаймиз
        except Exception as e:
             logger.error(f"Callback query'ни ({data}) қайта ишлашда хато: {e}", exc_info=True)


    async def delete_stories_automatically(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Бу функция асосан ўзгаришсиз қолади, лекин check_message'дан олдин ишламаслиги керак
        # Агар story хабарини алоҳида ушламоқчи бўлсак, алоҳида handler керак
        # Ҳозирги ҳолатда filters.ALL охирида тургани маъқул
        try:
            # Story хабари эканлигини текширишнинг ишончли усули керак бўлиши мумкин
            # Бу ерда update.message.story мавжудлиги тахмин қилинмоқда
            if update.message and hasattr(update.message, 'story') and update.message.story:
                user_id = update.message.from_user.id
                chat_id = update.message.chat_id
                message_id = update.message.message_id

                # Админ ва creator'ларни текшириш (гуруҳларда)
                if update.message.chat.type != Chat.PRIVATE:
                     member = await context.bot.get_chat_member(chat_id, user_id)
                     if member.status in ['administrator', 'creator','left']:
                         return # Админларникини ўчирмаймиз

                await update.message.delete()
                logger.info(f"Story (ID: {message_id}) avtomatik o'chirildi. User ID: {user_id}")
        except AttributeError:
             pass # 'story' атрибути бўлмаса, индамаймиз
        except Exception as e:
            logger.error(f"Hikoyalarni o'chirishда умумий xatolik: {e}")

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
         user_id = update.effective_user.id
         if update.effective_chat.type == Chat.PRIVATE and user_id != OWNER_ID:
              await update.message.reply_text("Салом! Мен гуруҳ модераториман. Мени гуруҳга қўшинг ва админлик ҳуқуқини беринг.")
         elif update.effective_chat.type == Chat.PRIVATE and user_id == OWNER_ID:
              await update.message.reply_text(f"Салом, хўжайин! Мен ишлашга тайёрман.\n"
                                              f"Мавжуд буйруқлар:\n"
                                              f"/addword [сўз] - ҳақоратли сўз қўшиш\n"
                                              f"/removeword [сўз] - ҳақоратли сўзни ўчириш\n"
                                              f"/showwords - ҳақоратли сўзлар рўйхати")
         else: # Гуруҳда /start ёзилса
              await update.message.reply_text("Салом! Мен гуруҳингизни ҳақоратли сўзлар, рухсатсиз ҳаволалар ва APK файллардан тозалашга ёрдам бераман.")


def main():
    logging.info("Бот ишга туширилмоқда...")

    try:
        # Application'ни JobQueue билан яратамиз
        application = Application.builder().token(TOKEN).job_queue(JobQueue()).build()

        # Moderator'га application'ни узатамиз
        moderator = TelegramModerator(TOKEN, application)

        # Буйруқ Handler'лари (owner_only декоратори билан)
        application.add_handler(CommandHandler('start', moderator.start_command))
        application.add_handler(CommandHandler('addword', moderator.add_offensive_word))
        application.add_handler(CommandHandler('removeword', moderator.remove_offensive_word))
        application.add_handler(CommandHandler('showwords', moderator.show_offensive_words))

        # Callback Query Handler (пагинация учун)
        application.add_handler(CallbackQueryHandler(moderator.button_handler))

        # Message Handler'лар (асосий текширув учун)
        # Энг кенг тарқалган фильтрларни юқорига қўямиз
        message_filters = (
    filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO | filters.AUDIO |
    filters.VOICE | filters.VIDEO_NOTE | filters.Sticker.ALL | filters.CONTACT |  # filters.STICKER ўрнига
    filters.LOCATION | filters.VENUE | filters.POLL | filters.Document.ALL
) & (~filters.UpdateType.EDITED_MESSAGE) & (~filters.StatusUpdate.WEB_APP_DATA)

        application.add_handler(MessageHandler(message_filters, moderator.check_message))

        # Story'ларни ўчириш учун алоҳида handler (аниқроқ фильтр билан)
        # Агар library 'story' учун алоҳида фильтр тақдим этса, шуни ишлатган маъқул
        # Ҳозирча эски усул қолади, лекин filters.ALL охирида туриши керак
        # application.add_handler(MessageHandler(filters.STORY???, moderator.delete_stories_automatically)) # Агар шундай фильтр бўлса
        # filters.ALL энг охирида қолади (агар юқоридагилар ишламаса)
        # application.add_handler(MessageHandler(filters.ALL, moderator.delete_stories_automatically))


        logging.info("Бот polling режимида ишга тушди.")
        application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES) # Барча турдаги update'ларни қабул қилиш

    except Exception as e:
        logging.critical(f"Ботни ишга туширишда критик хатолик: {e}", exc_info=True)

if __name__ == '__main__':
    main()
