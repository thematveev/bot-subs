import logging
import time
import hmac
import hashlib
import json
import asyncio
import os
import csv
import io
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, BigInteger
from sqlalchemy.orm import sessionmaker, declarative_base
import aiohttp

# ==========================================
# ИМПОРТ КОНФИГУРАЦИИ
# ==========================================
# Убедитесь, что config.py лежит рядом и в нем есть MESSAGES
from config import (
    MERCHANT_ACCOUNT, 
    MERCHANT_PASSWORD, 
    MERCHANT_SECRET, 
    TG_API_TOKEN, 
    CHANNEL_ID, 
    ADMIN_ID, 
    TARIFFS, 
    BASE_WEBHOOK_URL, 
    WEBHOOK_PATH,
    MESSAGES
)

# ==========================================
# БАЗА ДАННЫХ
# ==========================================
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    tariff = Column(String, nullable=True)
    start_date = Column(DateTime, nullable=True)
    expiry_date = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=False)
    invite_link = Column(String, nullable=True)
    active_order_ref = Column(String, nullable=True)
    language = Column(String, default="ru")

# Подключение к БД (Postgres для продакшена, SQLite для тестов)
DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL:
    engine = create_engine(DATABASE_URL, echo=False)
else:
    engine = create_engine('sqlite:///bot_database.db', echo=False)

Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)

# ==========================================
# HELPER: ПОЛУЧЕНИЕ ТЕКСТА
# ==========================================
def get_text(lang_code, key, **kwargs):
    """
    Возвращает текст из словаря MESSAGES в config.py.
    Если перевода нет, возвращает дефолтный (ru) или ключ ошибки.
    Поддерживает .format(**kwargs).
    """
    lang_code = lang_code or "ru"
    lang_dict = MESSAGES.get(lang_code, MESSAGES["ru"])
    text = lang_dict.get(key, f"MISSING_{key}")
    
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception as e:
            logging.error(f"Text formatting error: {e}")
            return text
    return text

# ==========================================
# WAYFORPAY API
# ==========================================
def generate_signature(string_to_sign):
    return hmac.new(
        MERCHANT_SECRET.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.md5
    ).hexdigest()

async def get_payment_url(user_id, tariff_key):
    """
    Генерирует ссылку на оплату.
    Включает параметры регулярного платежа, если тариф имеет 'period'.
    """
    tariff = TARIFFS[tariff_key]
    order_ref = f"SUB_{user_id}_{int(time.time())}"
    order_date = int(time.time())
    amount = tariff['price']
    
    # Имя продукта (можно брать локализованное, если нужно)
    product_name = f"Subscription {tariff.get('name_ru', tariff_key)}"
    
    # 1. Формируем строку подписи
    sign_list = [MERCHANT_ACCOUNT, 't.me/lesya_kovalchuk_2026_bot', order_ref, order_date, amount, "EUR", product_name, 1, amount]
    sign_str = ";".join(map(str, sign_list))
    signature = generate_signature(sign_str)

    # 2. Формируем тело запроса
    payload = {
        'merchantAccount': MERCHANT_ACCOUNT,
        'merchantAuthType': 'SimpleSignature',
        'merchantDomainName': 't.me/lesya_kovalchuk_2026_bot',
        'orderReference': order_ref,
        'orderDate': order_date,
        'amount': amount,
        'currency': 'EUR',
        'orderTimeout': 86400,
        'productName[]': product_name,
        'productPrice[]': amount,
        'productCount[]': 1,
        'clientFirstname': f"ID {user_id}",
        'clientLastname': "User",
        'serviceUrl': BASE_WEBHOOK_URL + WEBHOOK_PATH,
        'merchantSignature': signature
    }
    
    # 3. Добавляем параметры регулярного платежа (ПОДПИСКА)
    if 'period' in tariff:
        payload['regularMode'] = tariff['period']      # 'monthly', 'quarterly' и т.д.
        payload['regularOn'] = 1                       # Включить регулярность
        payload['regularBehavior'] = 'preset'          # Предустановленная галочка (нельзя снять)
        # payload['regularCount'] = 12                 # (Опционально) ограничить кол-во списаний

    async with aiohttp.ClientSession() as session:
        try:
            # Используем behavior=offline, чтобы сразу получить URL
            async with session.post("https://secure.wayforpay.com/pay?behavior=offline", data=payload) as response:
                try:
                    data = json.loads(await response.text())
                    if "url" in data: 
                        return data["url"], order_ref
                    logging.error(f"WFP Error: {data}")
                except Exception as e:
                    logging.error(f"WFP Response Parse Error: {e}")
        except Exception as e:
            logging.error(f"HTTP Error: {e}")
            
    return None, None

async def cancel_wfp_subscription(order_ref):
    """
    Отмена подписки через API (regularApi).
    Требует отдельный MERCHANT_PASSWORD (не Secret Key, хотя иногда совпадают).
    """
    if not order_ref: 
        return False

    payload = {
        "apiVersion": 1,
        "requestType": "REMOVE",
        "merchantAccount": MERCHANT_ACCOUNT,
        "orderReference": order_ref,
        "merchantPassword": MERCHANT_PASSWORD
    }

    url = "https://api.wayforpay.com/regularApi" 

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as response:
                text_response = await response.text()
                logging.info(f"Cancel WFP Response: {text_response}")
                
                try: 
                    data = json.loads(text_response)
                except: 
                    return False

                code = str(data.get("reasonCode"))
                reason = str(data.get("reason")).lower()
                
                # 4100 = OK (Regular API), 1100 = OK (Base API)
                if code == "4100" or reason == "ok" or code == "1100": 
                    return True
                
                logging.error(f"Cancel failed: {code} - {reason}")
                return False
                
        except Exception as e:
            logging.error(f"Cancel API Error: {e}")
            return False

# ==========================================
# БОТ (КЛАВИАТУРЫ)
# ==========================================
def get_language_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
         InlineKeyboardButton(text="🇺🇦 Українська", callback_data="lang_ua")]
    ])

def get_main_keyboard(lang="ru"):
    t = lambda k: get_text(lang, k)
    kb = [
        [KeyboardButton(text=t("btn_profile")), KeyboardButton(text=t("btn_buy"))],
        [KeyboardButton(text=t("btn_support")), KeyboardButton(text=t("btn_change_lang"))]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_tariffs_keyboard(lang="ru"):
    kb = []
    for key, data in TARIFFS.items():
        # Берем название на языке пользователя или дефолтное
        name = data.get(f"name_{lang}", data.get("name_ru", key))
        text = f"{name} - {data['price']} EUR"
        kb.append([InlineKeyboardButton(text=text, callback_data=f"buy_{key}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_profile_keyboard(user_id, lang="ru"):
    t = lambda k: get_text(lang, k)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("btn_cancel_sub"), callback_data="cancel_sub")]
    ])

# ==========================================
# БОТ (ЛОГИКА)
# ==========================================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TG_API_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=message.from_user.id).first()
    
    if not user:
        # Новый пользователь
        user = User(
            telegram_id=message.from_user.id, 
            username=message.from_user.username,
            full_name=message.from_user.full_name,
            language="ru" # Дефолт
        )
        session.add(user)
        session.commit()
        session.close()
        
        # Сразу предлагаем язык
        await message.answer(
            "👋 Welcome! / Вітаємо!\n\n"
            "Please choose your language / Будь ласка, оберіть мову:",
            reply_markup=get_language_keyboard()
        )
    else:
        # Старый пользователь
        lang = user.language
        session.close()
        await message.answer(
            get_text(lang, "welcome"),
            reply_markup=get_main_keyboard(lang)
        )

# --- ВЫБОР ЯЗЫКА ---
@dp.callback_query(F.data.startswith("lang_"))
async def process_lang_select(callback: types.CallbackQuery):
    lang_code = callback.data.split("_")[1] # ru или ua
    
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=callback.from_user.id).first()
    if user:
        user.language = lang_code
        session.commit()
    session.close()

    t = lambda k: get_text(lang_code, k)
    
    # Удаляем кнопки языка и показываем меню
    await callback.message.delete() 
    await callback.message.answer(
        t("language_selected"),
        reply_markup=get_main_keyboard(lang_code)
    )
    await callback.message.answer(t("welcome"))
    await callback.answer()

@dp.message(F.text.in_({"🇷🇺/🇺🇦 Сменить язык", "🇷🇺/🇺🇦 Змінити мову"}))
async def msg_change_lang(message: types.Message):
    await message.answer(
        "Choose language / Оберіть мову:",
        reply_markup=get_language_keyboard()
    )

# --- ПОКУПКА ---
@dp.message(F.text.in_({"💳 Купить подписку", "💳 Придбати підписку"}))
async def msg_buy(message: types.Message):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=message.from_user.id).first()
    lang = user.language if user else "ru"
    session.close()
    
    await message.answer(
        get_text(lang, "choose_tariff"), 
        reply_markup=get_tariffs_keyboard(lang)
    )

# --- ПРОФИЛЬ ---
@dp.message(F.text.in_({"👤 Профиль", "👤 Профіль"}))
async def msg_profile(message: types.Message):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=message.from_user.id).first()
    
    if not user:
        session.close()
        return
    
    lang = user.language or "ru"
    t = lambda k: get_text(lang, k)

    if user.is_active and user.expiry_date and user.expiry_date > datetime.now():
        status = t("profile_active")
        date_str = user.expiry_date.strftime('%d.%m.%Y')
        
        # Получаем красивое название тарифа
        tariff_display = user.tariff 
        # (Опционально можно найти в TARIFFS и взять name_ru/ua, но user.tariff хранит то, что купили)

        text = (
            f"{t('profile_header')}\n\n"
            f"{t('status')}: {status}\n"
            f"{t('expires')}: {date_str}\n"
            f"{t('tariff')}: {tariff_display}\n\n"
            f"🔗 {t('link')}: {user.invite_link or '...'}"
        )
        
        await message.answer(
            text, 
            parse_mode="HTML", 
            reply_markup=get_profile_keyboard(user.id, lang)
        )
    else:
        status = t("profile_inactive")
        text = f"{t('profile_header')}\n\n{t('status')}: {status}"
        await message.answer(
            text, 
            parse_mode="HTML", 
            reply_markup=get_tariffs_keyboard(lang)
        )
    
    session.close()

# --- ПОДДЕРЖКА ---
@dp.message(F.text.in_({"🆘 Поддержка", "🆘 Підтримка"}))
async def msg_support(message: types.Message):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=message.from_user.id).first()
    lang = user.language if user else "ru"
    session.close()
    
    await message.answer(get_text(lang, "support_text"))

# --- ОБРАБОТЧИКИ CALLBACK (ПОКУПКА / ОТМЕНА) ---
@dp.callback_query(F.data.startswith("buy_"))
async def process_buy(callback: types.CallbackQuery):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=callback.from_user.id).first()
    lang = user.language if user else "ru"
    session.close()

    tariff_key = callback.data.split("_", 1)[1]
    payment_url, order_ref = await get_payment_url(callback.from_user.id, tariff_key)
    
    if not payment_url:
        await callback.message.answer("⚠️ Error / Помилка")
        await callback.answer()
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=get_text(lang, "btn_pay"), 
            url=payment_url
        )]
    ])
    await callback.message.answer(
        get_text(lang, "invoice_created"), 
        reply_markup=markup
    )
    await callback.answer()

@dp.callback_query(F.data == "cancel_sub")
async def process_cancel_sub(callback: types.CallbackQuery):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=callback.from_user.id).first()
    
    if not user:
        session.close()
        await callback.answer()
        return
    
    lang = user.language or "ru"
    
    if not user.active_order_ref:
        await callback.message.answer(get_text(lang, "no_active_sub"))
        session.close()
        await callback.answer()
        return

    # Отправляем запрос на отмену
    success = await cancel_wfp_subscription(user.active_order_ref)
    
    if success:
        user.active_order_ref = None
        session.commit()
        await callback.message.answer(get_text(lang, "sub_cancelled"))
        
        # Лог админу
        try: 
            await bot.send_message(ADMIN_ID, f"ℹ️ User {user.telegram_id} cancelled sub")
        except: 
            pass
    else:
        await callback.message.answer(get_text(lang, "sub_cancel_fail"))
    
    session.close()
    await callback.answer()

# ==========================================
# CORE LOGIC (ВЫДАЧА / ОТЗЫВ ДОСТУПА)
# ==========================================
async def grant_access(user_id, days, tariff_name, order_ref=None):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=user_id).first()
    
    if not user:
        user = User(telegram_id=user_id, language="ru")
        session.add(user)
        session.flush() # Получить ID, но не коммитить пока
    
    lang = user.language or "ru"

    # Разбаниваем
    try: 
        await bot.unban_chat_member(CHANNEL_ID, user_id)
    except: 
        pass

    # Считаем дату
    now = datetime.now()
    if user.is_active and user.expiry_date and user.expiry_date > now:
        user.expiry_date += timedelta(days=days)
    else:
        user.start_date = now
        user.expiry_date = now + timedelta(days=days)
    
    user.is_active = True
    user.tariff = tariff_name
    
    if order_ref:
        user.active_order_ref = order_ref
    
    # Генерируем ссылку
    try:
        if not user.invite_link:
            invite = await bot.create_chat_invite_link(
                chat_id=CHANNEL_ID, 
                member_limit=1, 
                name=f"U_{user_id}", 
                expire_date=None
            )
            user.invite_link = invite.invite_link
        
        # Шлем сообщение юзеру
        msg = get_text(
            lang, 
            "sub_extended",
            date=user.expiry_date.strftime('%d.%m.%Y'),
            link=user.invite_link
        )
        await bot.send_message(
            user_id, 
            msg, 
            reply_markup=get_main_keyboard(lang)
        )
    except Exception as e:
        logging.error(f"Invite Error: {e}")
        
    session.commit()
    session.close()

async def revoke_access(user_id):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=user_id).first()
    
    if not user:
        session.close()
        return
    
    lang = user.language or "ru"

    try:
        # 1. Отмена в WFP
        if user.active_order_ref:
            await cancel_wfp_subscription(user.active_order_ref)
            user.active_order_ref = None
        
        # 2. Отзыв ссылки
        if user.invite_link:
            try: await bot.revoke_chat_invite_link(CHANNEL_ID, user.invite_link)
            except: pass
        
        # 3. Бан
        await bot.ban_chat_member(CHANNEL_ID, user_id)
        
        # 4. БД
        user.is_active = False
        user.invite_link = None
        session.commit()
        
        # 5. Уведомление
        await bot.send_message(user_id, get_text(lang, "access_revoked"))
        
    except Exception as e:
        logging.error(f"Revoke Error {user_id}: {e}")
    finally:
        session.close()

# ==========================================
# АДМИНКА
# ==========================================
@dp.message(Command("admin"))
async def cmd_admin_help(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    text = (
        "🛠 <b>Админка</b>\n\n"
        "/stats - Статистика\n"
        "/add ID DAYS - Выдать доступ\n"
        "/ban ID - Забрать доступ\n"
        "/check ID - Проверить юзера\n"
        "/export - Скачать CSV"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    session = SessionLocal()
    total = session.query(User).count()
    active = session.query(User).filter(User.is_active == True).count()
    session.close()
    await message.answer(f"📊 Всего: {total} | Активных: {active}")

@dp.message(Command("add"))
async def cmd_manual_add(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        _, uid, d = message.text.split()
        await grant_access(int(uid), int(d), "Manual_Admin")
        await message.answer(f"✅ Доступ выдан ID {uid} на {d} дней")
    except:
        await message.answer("Ошибка. Пример: /add 123456789 30")

@dp.message(Command("ban"))
async def cmd_manual_ban(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid = int(message.text.split()[1])
        await revoke_access(uid)
        await message.answer(f"🚫 ID {uid} забанен")
    except:
        await message.answer("Ошибка. Пример: /ban 123456789")

@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid = int(message.text.split()[1])
        session = SessionLocal()
        u = session.query(User).filter_by(telegram_id=uid).first()
        session.close()
        
        if not u: 
            await message.answer("Нет в базе")
            return
        
        status = "✅ Активен" if u.is_active else "❌ Не активен"
        wfp = "ВКЛ" if u.active_order_ref else "ВЫКЛ"
        lang = u.language or "ru"
        
        await message.answer(
            f"👤 {u.full_name}\n"
            f"Статус: {status}\n"
            f"Истекает: {u.expiry_date}\n"
            f"Автосписание: {wfp}\n"
            f"Язык: {lang}"
        )
    except:
        await message.answer("Ошибка")

@dp.message(Command("export"))
async def cmd_export(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    session = SessionLocal()
    users = session.query(User).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "TG_ID", "Name", "Active", "Expires", "Tariff", "Lang"])
    
    for u in users:
        writer.writerow([
            u.id, u.telegram_id, u.full_name, u.is_active, 
            u.expiry_date, u.tariff, u.language
        ])
    
    session.close()
    output.seek(0)
    doc = BufferedInputFile(
        output.getvalue().encode('utf-8'), 
        filename=f"users_{int(time.time())}.csv"
    )
    await message.answer_document(doc)

# ==========================================
# WEBHOOK HANDLER
# ==========================================
async def handle_wayforpay_webhook(request):
    try:
        data = await request.json()
    except:
        try: data = json.loads(await request.text())
        except: return web.Response(status=400)

    logging.info(f"Webhook: {data}")
    order_ref = data.get('orderReference')
    status = data.get('transactionStatus')
    
    if not order_ref: return web.Response(status=400)

    # Ответ для WFP
    resp = {
        "orderReference": order_ref, 
        "status": "accept", 
        "time": int(time.time())
    }
    resp['signature'] = generate_signature(f"{order_ref};accept;{resp['time']}")

    if status == 'Approved':
        try:
            uid = int(order_ref.split('_')[1])
            amount = float(data.get('amount', 0))
            
            # Определяем тариф
            days = 30
            t_name = "Auto"
            for k, v in TARIFFS.items():
                if abs(v['price'] - amount) < 1.0:
                    days = v['days']
                    t_name = v.get("name_ru", k)
                    break
            
            await grant_access(uid, days, t_name, order_ref)
        except Exception as e:
            logging.error(f"Grant Error: {e}")

    return web.json_response(resp)

async def handle_ping(request):
    return web.Response(text="Bot OK")

# ==========================================
# STARTUP
# ==========================================
async def check_subs_job():
    session = SessionLocal()
    users = session.query(User).filter(User.is_active == True).all()
    now = datetime.now()
    
    for u in users:
        if not u.expiry_date: continue
        left = u.expiry_date - now
        
        # Напоминание
        if left.days == 3:
            try: 
                msg = get_text(u.language, "reminder_3days") # Добавить в config
                if "MISSING" in msg: msg = "⏳ 3 дня до окончания подписки"
                await bot.send_message(u.telegram_id, msg)
            except: pass
        
        # Отзыв
        elif left.total_seconds() < 0:
            await revoke_access(u.telegram_id)
            
    session.close()

async def on_startup(app):
    sched = AsyncIOScheduler()
    sched.add_job(check_subs_job, 'interval', hours=12)
    sched.start()
    asyncio.create_task(dp.start_polling(bot))

def main():
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_wayforpay_webhook)
    app.router.add_get('/', handle_ping)
    app.on_startup.append(on_startup)
    
    port = int(os.environ.get("PORT", 8080))
    web.run_app(app, host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()
