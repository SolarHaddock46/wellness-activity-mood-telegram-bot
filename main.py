import asyncio
import logging
import pandas as pd
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, StateFilter
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardMarkup,
                           InlineKeyboardButton, Message, ReplyKeyboardRemove, BotCommand)
import aiosqlite
from dotenv import load_dotenv
import os
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from typing import Any, Awaitable, Callable, Dict
from gigachat import GigaChat
from langchain.llms import GigaChat as LangChainGigaChat
from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate


# Загружаем переменные окружения
load_dotenv()

# Настройки логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
API_TOKEN = os.getenv('API_TOKEN')
GIGACHAT_CREDENTIALS = os.getenv('GIGACHAT_CREDENTIALS')
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
gigachat = GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Инициализация планировщика
scheduler = AsyncIOScheduler()


# FSM
class RegistrationForm(StatesGroup):
    name = State()


class SurveyForm(StatesGroup):
    answering = State()


# Middleware
class RegistrationMiddleware(BaseMiddleware):
    async def __call__(
            self,
            handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
            event: Message,
            data: Dict[str, Any]
    ) -> Any:
        # Пропускаем не-сообщения
        if not isinstance(event, Message):
            return await handler(event, data)

        # Пропускаем не-текстовые сообщения
        if not event.text:
            return await handler(event, data)

        # Проверяем наличие состояния FSM
        state: FSMContext = data.get("state")
        if state:
            current_state = await state.get_state()
            if current_state == "RegistrationForm:name":
                return await handler(event, data)

        # Пропускаем команды регистрации
        if event.text.startswith(('/start', '/register')):
            return await handler(event, data)

        user_id = event.from_user.id

        # Проверяем регистрацию
        async with aiosqlite.connect('users.db') as db:
            async with db.execute(
                    "SELECT name FROM users WHERE user_id = ?",
                    (user_id,)
            ) as cursor:
                user = await cursor.fetchone()

        if not user:
            await event.answer("Пожалуйста, зарегистрируйтесь с помощью команды /register")
            return

        return await handler(event, data)


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
            self,
            handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
            event: Message,
            data: Dict[str, Any]
    ) -> Any:
        # Создаем таблицу для логов, если её нет
        async with aiosqlite.connect('users.db') as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS user_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT,
                    content TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await db.commit()

        # Логируем действие пользователя
        if isinstance(event, Message):
            user_id = event.from_user.id
            action = event.text if event.text else 'non-text action'

            async with aiosqlite.connect('users.db') as db:
                await db.execute(
                    "INSERT INTO user_actions (user_id, action, content, timestamp) VALUES (?, ?, ?, ?)",
                    (user_id, action, str(event), datetime.now())
                )
                await db.commit()

            logger.info(f"User {user_id} performed action: {action}")

        return await handler(event, data)


# Клавиатуры
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Начать опрос")],
        [KeyboardButton(text="Мои результаты")],
        [KeyboardButton(text="Анализ динамики")],
        [KeyboardButton(text="Отправить отзыв")]
    ],
    resize_keyboard=True
)

rating_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text=str(i), callback_data=f"rate:{i}")
         for i in range(-3, 4, 1)]
    ]
)

async def set_commands():
    commands = [
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="register", description="Регистрация"),
    ]
    await bot.set_my_commands(commands)

# Вспомогательный класс для хранения ответов
class UserResponse:
    def __init__(self):
        self.current_question = 0
        self.answers = {}

# FSM для сбора отзывов
class FeedbackForm(StatesGroup):
    waiting_for_feedback = State()

# Хранилище ответов пользователей
user_responses = {}

# Загрузка вопросов
try:
    questions_df = pd.read_csv('questions.csv')
    TOTAL_QUESTIONS = len(questions_df)
except Exception as e:
    logger.error(f"Ошибка при загрузке CSV файла: {e}")
    questions_df = None
    TOTAL_QUESTIONS = 0

# Функции баз данных
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

async def create_feedback_database():
    async with aiosqlite.connect('feedback.db') as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                feedback TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        await db.commit()

async def init_db():
    async with aiosqlite.connect('survey.db') as db:
        await db.execute(
            '''CREATE TABLE IF NOT EXISTS survey_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                well_being REAL,
                activity REAL,
                mood REAL,
                analysis TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )'''
        )
        await db.commit()

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Добро пожаловать! Используйте команду /register для регистрации.",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "🔍 <b>Справка по использованию методики САН</b>\n\n"
        "📝 <b>Как пройти опросник:</b>\n"
        "1. Нажмите кнопку 'Начать опрос'\n"
        "2. Оцените свое текущее состояние по шкале от -3 до +3:\n"
        "   • +3: состояние максимально выражено\n"
        "   • 0: нейтральное состояние\n"
        "   • -3: противоположное состояние максимально выражено\n\n"
        "📊 <b>Доступные команды:</b>\n"
        "/start - Начать работу с ботом\n"
        "/register - Регистрация нового пользователя\n"
        "/help - Показать эту справку\n\n"
        "🔘 <b>Доступные действия:</b>\n"
        "  - 'Начать опрос': запуск прохождения опроса\n"
        "  - 'Мои результаты': просмотр истории прохождения форм с результатами и их анализами\n"
        "  - 'Анализ динамики': анализ Вашей истории прохождения опросника нейросетью GigaChat\n\n"
        "📈 <b>Результаты:</b>\n"
        "• После прохождения опроса вы получите оценку:\n"
        "  - Самочувствия\n"
        "  - Активности\n"
        "  - Настроения\n"
        "Также результаты прохождения каждого теста анализируются нейросетью GigaChat для составления выводов и рекомендаций по ним."
        "❓ Если у вас возникли вопросы, воспользуйтесь командой /help"
    )
    await message.answer(help_text, parse_mode="HTML")

@dp.message(Command("register"))
async def cmd_register(message: Message, state: FSMContext):
    user_id = message.from_user.id

    # Проверяем текущее состояние
    current_state = await state.get_state()
    if current_state == "RegistrationForm:name":
        await message.answer("Вы уже начали регистрацию. Пожалуйста, введите ваше имя.")
        return

    async with aiosqlite.connect('users.db') as db:
        async with db.execute(
                "SELECT name FROM users WHERE user_id = ?",
                (user_id,)
        ) as cursor:
            user = await cursor.fetchone()

    if user:
        await message.answer(
            f"Вы уже зарегистрированы как {user[0]}!",
            reply_markup=main_menu
        )
    else:
        await state.set_state(RegistrationForm.name)
        await message.answer(
            "Как вас зовут?",
            reply_markup=ReplyKeyboardRemove()
        )

@dp.message(StateFilter(RegistrationForm.name))
async def process_name(message: Message, state: FSMContext):
    # Игнорируем все команды во время регистрации
    if message.text.startswith('/'):
        return

    name = message.text.strip()
    if len(name) < 2 or len(name) > 50:
        await message.answer("Введите корректное имя (от 2 до 50 символов)")
        return

    user_id = message.from_user.id

    try:
        async with aiosqlite.connect('users.db') as db:
            await db.execute(
                "INSERT INTO users (user_id, name) VALUES (?, ?)",
                (user_id, name)
            )
            await db.commit()

        await state.clear()
        await message.answer(
            f"Приятно познакомиться, {name}!",
            reply_markup=main_menu
        )
        logger.info(f"Новый пользователь зарегистрирован: {user_id} ({name})")

    except Exception as e:
        logger.error(f"Ошибка при регистрации пользователя: {e}")
        await message.answer(
            "Произошла ошибка при регистрации. Пожалуйста, попробуйте еще раз.",
            reply_markup=ReplyKeyboardRemove()
        )

@dp.message(F.text == "Начать опрос")
async def start_survey(message: Message, state: FSMContext):
    if questions_df is not None:
        user_id = message.from_user.id

        # Создаем инструкцию перед началом опроса
        guide_text = (
            "📋 <b>Краткая инструкция:</b>\n\n"
            "• Оцените свое текущее состояние\n"
            "• Используйте шкалу от -3 до +3\n"
            "• Не думайте долго над ответом\n"
            "• Ориентируйтесь на свое первое впечатление\n\n"
            "<b>Шкала оценки:</b>\n"
            "+3 — максимально выражено\n"
            "+2 — сильно выражено\n"
            "+1 — слабо выражено\n"
            " 0 — нейтрально\n"
            "-1 — слабо выражено противоположное\n"
            "-2 — сильно выражено противоположное\n"
            "-3 — максимально выражено противоположное\n\n"
         )

        # Инициализируем данные пользователя
        user_responses[user_id] = UserResponse()
        await state.set_state(SurveyForm.answering)

        # Отправляем инструкцию и первый вопрос
        await message.answer(guide_text, parse_mode="HTML")
        await send_question(message.chat.id, user_id)
    else:
        await message.answer("Извините, в данный момент опрос недоступен. Попробуйте позже.")

# Отображение истории заполнения опросника
@dp.message(F.text == "Мои результаты")
async def show_results(message: Message):
    user_id = message.from_user.id
    try:
        async with aiosqlite.connect('survey.db') as db:
            async with db.execute(
                    """SELECT well_being, activity, mood, analysis, timestamp 
                    FROM survey_results 
                    WHERE user_id = ? 
                    ORDER BY timestamp DESC 
                    LIMIT 5""",
                    (user_id,)
            ) as cursor:
                results = await cursor.fetchall()

        if results:
            response = "Ваши результаты:\n\n"
            for well_being, activity, mood, analysis, timestamp in results:
                response += (
                    f"Дата: {timestamp}\n"
                    f"Самочувствие: {well_being:.1f}\n"
                    f"Активность: {activity:.1f}\n"
                    f"Настроение: {mood:.1f}\n"
                    f"{'=' * 20}\n"
                )
            response += "\nНорма: 5.0-5.5 баллов"
            await message.answer(response)
        else:
            await message.answer("У вас пока нет результатов. Пройдите опрос, чтобы увидеть свои показатели.")
    except Exception as e:
        logger.error(f"Ошибка при получении результатов: {e}")
        await message.answer("Произошла ошибка при получении результатов. Попробуйте позже.")

# Анализ трендов в изменении показателей САН
@dp.message(F.text == "Анализ динамики")
async def cmd_analyze_trends(message: Message):
    user_id = message.from_user.id

    # Проверяем регистрацию пользователя
    async with aiosqlite.connect('users.db') as db:
        async with db.execute(
                "SELECT name FROM users WHERE user_id = ?",
                (user_id,)
        ) as cursor:
            user = await cursor.fetchone()

    if not user:
        await message.answer(
            "Пожалуйста, сначала зарегистрируйтесь с помощью команды /register"
        )
        return

    await analyze_trends_with_gigachat(message.chat.id, user_id)

# Обработчик ответов пользователя при опросе
@dp.callback_query(lambda c: c.data.startswith("rate:"))
async def process_rating(callback_query: types.CallbackQuery, state: FSMContext):
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

# Обработчик состояния для отправки отзыва
@dp.message(F.text.in_({"Отправить отзыв"}))
async def start_feedback(message: Message, state: FSMContext):
    # Запрашиваем отзыв у пользователя
    await message.answer("Пожалуйста, напишите ваш отзыв. Мы будем признательны за ваши комментарии!")
    await state.set_state(FeedbackForm.waiting_for_feedback)

@dp.message(FeedbackForm.waiting_for_feedback)
async def process_feedback(message: Message, state: FSMContext):
    user_id = message.from_user.id
    feedback_text = message.text

    # Попытка сохранить отзыв в базе данных
    try:
        async with aiosqlite.connect('feedback.db') as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    feedback TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            await db.execute(
                "INSERT INTO feedback (user_id, feedback) VALUES (?, ?)",
                (user_id, feedback_text)
            )
            await db.commit()

        await message.answer(
            "Спасибо за ваш отзыв! Мы ценим ваше мнение.",
            reply_markup=main_menu
        )

    except Exception as e:
        logger.error(f"Ошибка при сохранении отзыва: {e}")
        await message.answer(
            "Произошла ошибка при сохранении вашего отзыва. Пожалуйста, попробуйте позже.",
            reply_markup=main_menu
        )

    # Очищаем состояние после обработки отзыва
    await state.clear()

@dp.message()
async def handle_invalid_input(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state != FeedbackForm.waiting_for_feedback:
        await message.answer(
            "Извините, я не совсем поняла, что вы хотите. 🤔",
            reply_markup=main_menu
        )

# Отправка вопроса пользователю
async def send_question(chat_id: int, user_id: int):
    user_response = user_responses[user_id]
    if user_response.current_question < TOTAL_QUESTIONS:
        question = questions_df.iloc[user_response.current_question]
        await bot.send_message(
            chat_id=chat_id,
            text=f"Вопрос {user_response.current_question + 1} из {TOTAL_QUESTIONS}\n\n"
                 f"{question['negative']} или {question['positive']}?",
            reply_markup=rating_keyboard
        )

# Анализ результатов единичного заполнения опросника
async def analyze_results_with_gigachat(well_being: float, activity: float, mood: float) -> str:
    prompt = PromptTemplate(
        input_variables=["well_being", "activity", "mood"],
        template="""Ты - опытный психолог-аналитик, специализирующийся на оценке психоэмоционального состояния. Используй следующие данные опросника САН:

        Самочувствие: {well_being}
        Активность: {activity}
        Настроение: {mood}
                
        Задачи:
        1. Проанализируй взаимосвязь между показателями
        2. Определи возможные причины текущего состояния
        3. Оцени риски при данной комбинации показателей
        4. Предложи персонализированные рекомендации
        
        При анализе учитывай:
        - Критические отклонения от нормы
        - Дисбаланс между показателями
        - Возможные физиологические и психологические факторы
        
        
        Формат ответа:
        1. Краткая интерпретация результатов (2-3 предложения)
        2. Выявленные паттерны и взаимосвязи
        3. Потенциальные риски
        4. Конкретные рекомендации по улучшению каждого показателя
        5. Общий план действий
        
        Структура ответа должна четко следовать формату. **Текст форматировать не следует.**
        Избегай категоричных суждений и учитывай индивидуальный контекст.
        Формулируй свой ответ так, как если бы ты рассказывал эту интерпретацию в живом разговоре с заполнившим анкету, обращайся к нему на Вы.
        **Не используй в своем ответе форматирование markdown**, для визуального разделения секций сообщения лучше использовать подходящие по контексту эмодзи."""
    )

    chain = LLMChain(
        llm=LangChainGigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False),
        prompt=prompt
    )

    result = await chain.arun({
        "well_being": well_being,
        "activity": activity,
        "mood": mood
    })

    return result

# Расчет значений результатов
async def process_results(chat_id: int, user_id: int):
    user_response = user_responses[user_id]

    well_being = sum(user_response.answers[i] for i in range(1, 31) if i in [1, 2, 7, 8, 13, 14, 19, 20, 25, 26])
    activity = sum(user_response.answers[i] for i in range(1, 31) if i in [3, 4, 9, 10, 15, 16, 21, 22, 27, 28])
    mood = sum(user_response.answers[i] for i in range(1, 31) if i in [5, 6, 11, 12, 17, 18, 23, 24, 29, 30])

    well_being = (well_being + 30) / 10
    activity = (activity + 30) / 10
    mood = (mood + 30) / 10

    try:
        # Сначала сохраняем базовые результаты
        async with aiosqlite.connect('survey.db') as db:
            await db.execute(
                '''INSERT INTO survey_results 
                (user_id, well_being, activity, mood) 
                VALUES (?, ?, ?, ?)''',
                (user_id, well_being, activity, mood)
            )
            await db.commit()

        logger.info(f"Сохранены базовые результаты для пользователя {user_id}")

        # Затем пытаемся получить анализ
        try:
            analysis = await analyze_results_with_gigachat(well_being, activity, mood)

            # Обновляем запись, добавляя анализ
            async with aiosqlite.connect('survey.db') as db:
                await db.execute(
                    '''UPDATE survey_results 
                    SET analysis = ? 
                    WHERE id = (
                        SELECT id 
                        FROM survey_results 
                        WHERE user_id = ? 
                        AND analysis IS NULL 
                        ORDER BY timestamp DESC 
                        LIMIT 1
                    )''',
                    (analysis, user_id)
                )
                await db.commit()

            logger.info(f"Добавлен анализ для пользователя {user_id}")

            message_text = (f"Результаты опроса:\n"
                            f"Самочувствие: {well_being:.1f}\n"
                            f"Активность: {activity:.1f}\n"
                            f"Настроение: {mood:.1f}\n\n"
                            f"Норма: 5.0-5.5 баллов\n\n"
                            f"Анализ результатов:\n{analysis}")
        except Exception as e:
            logger.error(f"Ошибка при получении анализа: {e}")
            message_text = (f"Результаты опроса:\n"
                            f"Самочувствие: {well_being:.1f}\n"
                            f"Активность: {activity:.1f}\n"
                            f"Настроение: {mood:.1f}\n\n"
                            f"Норма: 5.0-5.5 баллов")

        await bot.send_message(
            chat_id=chat_id,
            text=message_text,
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

# Функция анализа трендов в последних 5 заполнениях
async def analyze_trends_with_gigachat(chat_id: int, user_id: int, num_last_results: int = 5) -> None:
    try:
        # Получаем последние результаты из БД
        async with aiosqlite.connect('survey.db') as db:
            async with db.execute(
                '''SELECT well_being, activity, mood, timestamp 
                FROM survey_results 
                WHERE user_id = ? 
                ORDER BY timestamp DESC 
                LIMIT ?''',
                (user_id, num_last_results)
            ) as cursor:
                results = await cursor.fetchall()

        if not results:
            await bot.send_message(
                chat_id=chat_id,
                text="Недостаточно данных для анализа трендов. Необходимо пройти несколько измерений."
            )
            return

        # Форматируем данные для анализа
        formatted_data = []
        for well_being, activity, mood, timestamp in results:
            formatted_data.append(
                f"Дата: {timestamp}\n"
                f"Самочувствие: {well_being:.1f}\n"
                f"Активность: {activity:.1f}\n"
                f"Настроение: {mood:.1f}\n"
            )

        # Создаем промпт для GigaChat
        prompt = PromptTemplate(
            input_variables=["measurements"],
            template="""Ты - опытный психолог-аналитик. Проанализируй изменения в показателях САН (самочувствие, активность, настроение) за последние измерения.

            Данные измерений (от новых к старым):
            {measurements}
            
            Задачи анализа:
            1. Определи тренды изменения каждого показателя
            2. Оцени стабильность показателей
            3. Выяви возможные причины изменений
            4. Определи потенциальные риски при текущей динамике
            5. Предложи рекомендации с учетом наблюдаемых изменений
            
            При анализе обрати внимание на:
            - Резкие изменения показателей
            - Устойчивые тренды
            - Взаимосвязь изменений разных показателей
            - Цикличность изменений
            
            Сформулируй анализ так, как если бы ты объяснял динамику показателей при личной консультации. Обращайся к пользователю на Вы.
            При этом не пиши никаких приветственных сообщений или введений, переходи сразу к сути."""
        )

        # Получаем анализ от GigaChat
        chain = LLMChain(
            llm=LangChainGigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False),
            prompt=prompt
        )

        analysis = await chain.arun({
            "measurements": "\n---\n".join(formatted_data)
        })

        # Отправляем результат анализа
        await bot.send_message(
            chat_id=chat_id,
            text=f"Анализ динамики показателей САН за последние {len(results)} измерений:\n\n{analysis}"
        )

    except Exception as e:
        logger.error(f"Ошибка при анализе трендов: {e}")
        await bot.send_message(
            chat_id=chat_id,
            text="Произошла ошибка при анализе трендов. Пожалуйста, попробуйте позже."
        )


# Отправка напоминаний всем зарегистрированным пользователям
async def send_reminder():
    try:
        async with aiosqlite.connect('users.db') as db:
            async with db.execute("SELECT user_id, name FROM users") as cursor:
                users = await cursor.fetchall()

        for user_id, name in users:
            try:
                async with aiosqlite.connect('survey.db') as db:
                    async with db.execute(
                            """SELECT timestamp 
                            FROM survey_results 
                            WHERE user_id = ? 
                            ORDER BY timestamp DESC 
                            LIMIT 1""",
                            (user_id,)
                    ) as cursor:
                        last_survey = await cursor.fetchone()

                if not last_survey:
                    message_text = f"{name}, вы еще ни разу не проходили опрос САН. Предлагаю сделать это сейчас!"
                else:
                    message_text = f"{name}, пришло время снова пройти опрос САН!"

                await bot.send_message(
                    chat_id=user_id,
                    text=message_text,
                    reply_markup=main_menu
                )
                logger.info(f"Отправлено напоминание пользователю {user_id}")

            except Exception as e:
                logger.error(f"Ошибка при отправке напоминания пользователю {user_id}: {e}")
                continue

    except Exception as e:
        logger.error(f"Ошибка при отправке напоминаний: {e}")


# Запуск бота
async def main():
    # Создаем базы данных
    await create_user_database()
    await init_db()
    await create_feedback_database()

    # Регистрируем middleware
    dp.message.middleware.register(RegistrationMiddleware())
    dp.message.middleware.register(LoggingMiddleware())

    # Настраиваем периодическое напоминание
    reminder_interval = int(os.getenv('REMINDER_INTERVAL', '60'))
    scheduler.add_job(send_reminder, 'interval', minutes=reminder_interval)
    scheduler.start()

    # Запускаем бота
    await set_commands()
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.error("Бот остановлен!")
        scheduler.shutdown()
