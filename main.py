import asyncio
import logging
import pandas as pd
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
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


# FSM для регистрации
class RegistrationForm(StatesGroup):
    name = State()


# Словарь для хранения ответов пользователя
class UserResponse:
    def __init__(self):
        self.current_question = 0
        self.answers = {}


# Хранилище ответов пользователей
user_responses = {}

# Клавиатура для основного меню
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Начать опрос")],
        [KeyboardButton(text="Мои результаты")]
    ],
    resize_keyboard=True
)

# Клавиатура для оценки состояний
rating_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text=str(i), callback_data=f"rate:{i}")
         for i in range(3, -4, -1)]
    ]
)


async def create_user_database():
    async with aiosqlite.connect('users.db') as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        await db.commit()


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    async with aiosqlite.connect('users.db') as db:
        async with db.execute(
                "SELECT name FROM users WHERE user_id = ?",
                (user_id,)
        ) as cursor:
            user = await cursor.fetchone()

    if user:
        await message.answer(
            f"С возвращением, {user[0]}!",
            reply_markup=main_menu
        )
    else:
        await state.set_state(RegistrationForm.name)
        await message.answer("Добро пожаловать! Как вас зовут?")


@dp.message(StateFilter(RegistrationForm.name))
async def process_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2 or len(name) > 50:
        await message.answer("Введите корректное имя (от 2 до 50 символов)")
        return

    async with aiosqlite.connect('users.db') as db:
        await db.execute(
            "INSERT INTO users (user_id, name) VALUES (?, ?)",
            (message.from_user.id, name)
        )
        await db.commit()

    await state.clear()
    await message.answer(
        f"Приятно познакомиться, {name}!",
        reply_markup=main_menu
    )


@dp.message(F.text == "Начать опрос")
async def start_survey(message: types.Message, state: FSMContext):
    if questions_df is not None:
        user_id = message.from_user.id
        user_responses[user_id] = UserResponse()
        await state.set_state(SurveyForm.answering)
        await send_question(message.chat.id, user_id)
    else:
        await message.answer("Извините, в данный момент опрос недоступен. Попробуйте позже.")


@dp.message(F.text == "Мои результаты")
async def show_results(message: types.Message):
    user_id = message.from_user.id
    try:
        async with aiosqlite.connect('survey.db') as db:
            async with db.execute(
                    """
                    SELECT well_being, activity, mood, timestamp 
                    FROM survey_results 
                    WHERE user_id = ? 
                    ORDER BY timestamp DESC 
                    LIMIT 1
                    """,
                    (user_id,)
            ) as cursor:
                result = await cursor.fetchone()

        if result:
            well_being, activity, mood, timestamp = result
            await message.answer(
                f"Ваши последние результаты:\n"
                f"Дата: {timestamp}\n"
                f"Самочувствие: {well_being:.1f}\n"
                f"Активность: {activity:.1f}\n"
                f"Настроение: {mood:.1f}\n\n"
                f"Норма: 5.0-5.5 баллов"
            )
        else:
            await message.answer("У вас пока нет результатов. Пройдите опрос, чтобы увидеть свои показатели.")
    except Exception as e:
        logger.error(f"Ошибка при получении результатов: {e}")
        await message.answer("Произошла ошибка при получении результатов. Попробуйте позже.")


@dp.callback_query(F.data.startswith("rate:"))
async def process_survey_answer(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    if user_id not in user_responses:
        await callback_query.message.answer("Произошла ошибка. Пожалуйста, начните опрос заново.")
        return

    rating = int(callback_query.data.split(":")[1])
    user_response = user_responses[user_id]
    current_question = questions_df.iloc[user_response.current_question]
    user_response.answers[current_question['number']] = rating
    user_response.current_question += 1

    await callback_query.answer()

    if user_response.current_question < TOTAL_QUESTIONS:
        await send_question(callback_query.message.chat.id, user_id)
    else:
        await process_results(callback_query.message.chat.id, user_id)


async def send_question(chat_id: int, user_id: int):
    user_response = user_responses[user_id]
    if user_response.current_question < TOTAL_QUESTIONS:
        question = questions_df.iloc[user_response.current_question]
        await bot.send_message(
            chat_id=chat_id,
            text=f"Вопрос {user_response.current_question + 1} из {TOTAL_QUESTIONS}\n\n"
                 f"{question['positive']} или {question['negative']}?",
            reply_markup=rating_keyboard
        )


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
        async with aiosqlite.connect('survey.db') as db:
            await db.execute(
                'INSERT INTO survey_results (user_id, well_being, activity, mood) VALUES (?, ?, ?, ?)',
                (user_id, well_being, activity, mood)
            )
            await db.commit()

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
        del user_responses[user_id]


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
    # Создаем базу данных пользователей
    await create_user_database()
    # Инициализируем базу данных для результатов
    await init_db()
    # Запускаем бота
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.error("Бот остановлен!")
