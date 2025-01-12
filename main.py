import asyncio
import logging
import pandas as pd
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton)
import aiosqlite
from dotenv import load_dotenv
import os
from datetime import datetime

# Загружаем переменные окружения из .env файла
load_dotenv()

# Настройки логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
API_TOKEN = os.getenv('API_TOKEN')
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Загрузка вопросов из CSV
try:
    questions_df = pd.read_csv('questions.csv')
    TOTAL_QUESTIONS = len(questions_df)
except Exception as e:
    logger.error(f"Ошибка при загрузке CSV файла: {e}")
    questions_df = None
    TOTAL_QUESTIONS = 0


# FSM для опроса
class SurveyForm(StatesGroup):
    answering = State()


# Словарь для хранения ответов пользователя
class UserResponse:
    def __init__(self):
        self.current_question = 0
        self.answers = {}


user_responses = {}

# Меню (обновленное)
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Начать опрос")],
        [KeyboardButton(text="Моя история")]
    ],
    resize_keyboard=True
)

# Клавиатура для оценки состояний
rating_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="+3", callback_data="rate:3")],
        [InlineKeyboardButton(text="+2", callback_data="rate:2")],
        [InlineKeyboardButton(text="+1", callback_data="rate:1")],
        [InlineKeyboardButton(text="0", callback_data="rate:0")],
        [InlineKeyboardButton(text="-1", callback_data="rate:-1")],
        [InlineKeyboardButton(text="-2", callback_data="rate:-2")],
        [InlineKeyboardButton(text="-3", callback_data="rate:-3")],
    ]
)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Давайте начнем опрос по методике 'Ситуация-Активность-Настроение'.\n"
        "В опросе вам будет предложено оценить различные состояния по шкале от -3 до +3, где:\n"
        "+3 - состояние наиболее типично\n"
        "+2 - состояние довольно типично\n"
        "+1 - состояние встречается чаще, чем противоположное\n"
        "0 - трудно сказать\n"
        "-1 - противоположное состояние встречается чаще\n"
        "-2 - противоположное состояние довольно типично\n"
        "-3 - противоположное состояние наиболее типично",
        reply_markup=main_menu
    )


@dp.message(F.text == "Начать опрос")
async def start_survey(message: types.Message, state: FSMContext):
    if not questions_df is None:
        user_id = message.from_user.id
        user_responses[user_id] = UserResponse()
        await state.set_state(SurveyForm.answering)
        await send_question(message.chat.id, user_id)
    else:
        await message.answer("Извините, в данный момент опрос недоступен. Попробуйте позже.")


@dp.message(F.text == "Моя история")
async def show_history(message: types.Message):
    user_id = message.from_user.id
    try:
        async with aiosqlite.connect('survey.db') as db:
            db.row_factory = aiosqlite.Row  # Это позволит обращаться к столбцам по именам
            async with db.execute(
                    '''SELECT well_being, activity, mood, timestamp 
                       FROM survey_results 
                       WHERE user_id = ? 
                       ORDER BY timestamp DESC 
                       LIMIT 5''',
                    (user_id,)
            ) as cursor:
                results = await cursor.fetchall()

        if not results:
            await message.answer("У вас пока нет истории прохождения опросов.", reply_markup=main_menu)
            return

        # Формируем сообщение с историей
        history_message = "Ваша история последних 5 опросов:\n\n"
        for i, result in enumerate(results, 1):
            timestamp = datetime.strptime(result['timestamp'], '%Y-%m-%d %H:%M:%S')
            formatted_date = timestamp.strftime('%d.%m.%Y %H:%M')
            history_message += (
                f"{i}. Дата: {formatted_date}\n"
                f"   Самочувствие: {result['well_being']:.1f}\n"
                f"   Активность: {result['activity']:.1f}\n"
                f"   Настроение: {result['mood']:.1f}\n"
                f"   {'=' * 20}\n"
            )

        await message.answer(history_message, reply_markup=main_menu)

    except Exception as e:
        logger.error(f"Ошибка при получении истории: {e}")
        await message.answer(
            "Произошла ошибка при получении истории. Пожалуйста, попробуйте позже.",
            reply_markup=main_menu
        )


async def send_question(chat_id, user_id):
    user_response = user_responses[user_id]
    if user_response.current_question < TOTAL_QUESTIONS:
        question = questions_df.iloc[user_response.current_question]
        await bot.send_message(
            chat_id=chat_id,
            text=f"{question['positive']} или {question['negative']}?",
            reply_markup=rating_keyboard
        )
    else:
        await process_results(chat_id, user_id)


@dp.callback_query(SurveyForm.answering)
async def process_answer(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    rating = int(callback_query.data.split(":")[1])

    user_response = user_responses[user_id]
    question = questions_df.iloc[user_response.current_question]

    # Сохраняем ответ
    user_response.answers[question['number']] = rating
    user_response.current_question += 1

    await callback_query.answer()

    if user_response.current_question < TOTAL_QUESTIONS:
        await send_question(callback_query.message.chat.id, user_id)
    else:
        await state.clear()
        await process_results(callback_query.message.chat.id, user_id)


async def process_results(chat_id, user_id):
    user_response = user_responses[user_id]

    # Подсчет результатов по категориям
    well_being = sum(user_response.answers[i] for i in range(1, 31) if i in [1, 2, 7, 8, 13, 14, 19, 20, 25, 26])
    activity = sum(user_response.answers[i] for i in range(1, 31) if i in [3, 4, 9, 10, 15, 16, 21, 22, 27, 28])
    mood = sum(user_response.answers[i] for i in range(1, 31) if i in [5, 6, 11, 12, 17, 18, 23, 24, 29, 30])

    # Преобразование в 7-балльную шкалу
    well_being = (well_being + 30) / 10
    activity = (activity + 30) / 10
    mood = (mood + 30) / 10

    try:
        # Сохранение результатов в БД
        async with aiosqlite.connect('survey.db') as db:
            await db.execute(
                'INSERT INTO survey_results (user_id, well_being, activity, mood) VALUES (?, ?, ?, ?)',
                (user_id, well_being, activity, mood)
            )
            await db.commit()

        # Отправка результатов пользователю
        await bot.send_message(
            chat_id=chat_id,
            text=f"Результаты опроса:\n"
                 f"Самочувствие: {well_being:.1f}\n"
                 f"Активность: {activity:.1f}\n"
                 f"Настроение: {mood:.1f}\n\n"
                 f"Норма: 5.0-5.5 баллов",
            reply_markup=main_menu
        )
    except Exception as e:
        logger.error(f"Ошибка при сохранении результатов: {e}")
        await bot.send_message(
            chat_id=chat_id,
            text="Произошла ошибка при сохранении результатов. Пожалуйста, попробуйте позже.",
            reply_markup=main_menu
        )
    finally:
        # Очистка данных пользователя
        del user_responses[user_id]


# Инициализация базы данных
async def init_db():
    try:
        async with aiosqlite.connect('survey.db') as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS survey_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    well_being REAL,
                    activity REAL,
                    mood REAL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Ошибка при инициализации базы данных: {e}")


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.error("Бот остановлен!")
