import logging
import time
import hmac
import hashlib
import json
import asyncio
from datetime import datetime, timedelta
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, BigInteger
from sqlalchemy.orm import sessionmaker, declarative_base

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================
MERCHANT_ACCOUNT = 'test_merch_n1'
MERCHANT_SECRET = 'flk3409refn54t54t*FNJRET'
TG_API_TOKEN = '8198828061:AAE-pKTb0lSgJ3E9w1_m29uQyd_KZum9yLc'
CHANNEL_ID = -1003690130785  # Приведен к int
ADMIN_ID = 367335715         # Приведен к int

# АДРЕС ВАШЕГО СЕРВЕРА (ОБЯЗАТЕЛЬНО ИЗМЕНИТЬ ДЛЯ РАБОТЫ WAYFORPAY)
# WayForPay будет слать сюда уведомления. Должен быть HTTPS.
BASE_WEBHOOK_URL = "https://your-ip-or-domain.com" 
WEBHOOK_PATH = "/wayforpay/callback"

# Цены (в валюте мерчанта, UAH)
TARIFFS = {
    "1_month": {"name": "1 Месяц", "price": 100, "days": 30, "period": "monthly"},
    "3_months": {"name": "3 Месяца", "price": 270, "days": 90, "period": "quarterly"}, # Примерный период для API
    "6_months": {"name": "6 Месяцев", "price": 500, "days": 180, "period": "halfyearly"},
    "12_months": {"name": "1 Год", "price": 900, "days": 365, "period": "yearly"},
}

# ==========================================
# БАЗА ДАННЫХ (SQLAlchemy)
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

engine = create_engine('sqlite:///bot_database.db', echo=False)
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)

# ==========================================
# ЛОГИКА WAYFORPAY
# ==========================================
def generate_signature(string_to_sign):
    return hmac.new(
        MERCHANT_SECRET.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.md5
    ).hexdigest()

def get_payment_url(user_id, tariff_key):
    tariff = TARIFFS[tariff_key]
    order_ref = f"SUB_{user_id}_{int(time.time())}"
    order_date = int(time.time())
    amount = tariff['price']
    
    # Формируем данные для подписи (Порядок важен!)
    # merchantAccount;merchantDomainName;orderReference;orderDate;amount;currency;productName;productCount;productPrice
    product_name = f"Subscription {tariff['name']}"
    sign_list = [
        MERCHANT_ACCOUNT, 
        "t.me/BotName", # Domain name
        order_ref, 
        order_date, 
        amount, 
        "UAH",
        product_name, 
        1, 
        amount
    ]
    sign_str = ";".join(map(str, sign_list))
    signature = generate_signature(sign_str)

    # Параметры запроса
    # regularMode управляет подпиской. 
    params = {
        'merchantAccount': MERCHANT_ACCOUNT,
        'merchantAuthType': 'SimpleSignature',
        'merchantDomainName': 't.me/BotName',
        'orderReference': order_ref,
        'orderDate': order_date,
        'amount': amount,
        'currency': 'UAH',
        'orderTimeout': 3600,
        'productName[]': product_name,
        'productPrice[]': amount,
        'productCount[]': 1,
        'clientFirstname': f"ID {user_id}",
        'clientLastname': "User",
        'clientPhone': "380000000000", # Формальный
        'regularMode': tariff['period'], # monthly, etc.
        'serviceUrl': BASE_WEBHOOK_URL + WEBHOOK_PATH,
        'merchantSignature': signature
    }
    
    # Генерируем URL (GET запрос для редиректа пользователя)
    # В реальности лучше делать POST форму, но для простоты Telegram кнопки используем GET ссылку
    base_url = "https://secure.wayforpay.com/pay"
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    return f"{base_url}?{query_string}", order_ref

# ==========================================
# БОТ И ОБРАБОТЧИКИ
# ==========================================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TG_API_TOKEN)
dp = Dispatcher()

# --- Клавиатуры ---
def get_tariffs_keyboard():
    keyboard = []
    for key, data in TARIFFS.items():
        keyboard.append([InlineKeyboardButton(
            text=f"{data['name']} - {data['price']} UAH", 
            callback_data=f"buy_{key}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# --- Команды ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # Регистрируем пользователя в БД если нет
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=message.from_user.id).first()
    if not user:
        user = User(
            telegram_id=message.from_user.id, 
            username=message.from_user.username,
            full_name=message.from_user.full_name
        )
        session.add(user)
        session.commit()
    session.close()

    await message.answer(
        "Привет! Это бот для доступа к закрытому каналу.\n"
        "Выберите тарифный план для оформления подписки:",
        reply_markup=get_tariffs_keyboard()
    )

@dp.callback_query(F.data.startswith("buy_"))
async def process_buy(callback: types.CallbackQuery):
    tariff_key = callback.data.split("_", 1)[1]
    payment_url, order_ref = get_payment_url(callback.from_user.id, tariff_key)
    
    # Сохраняем попытку в БД (опционально можно сохранять order_ref)
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)]
    ])
    
    await callback.message.answer(
        f"Вы выбрали тариф: {TARIFFS[tariff_key]['name']}.\n"
        f"Нажмите кнопку ниже для оплаты.",
        reply_markup=markup
    )
    await callback.answer()

# --- Админка ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    session = SessionLocal()
    users = session.query(User).all()
    active = sum(1 for u in users if u.is_active)
    
    text = f"👥 Всего пользователей: {len(users)}\n" \
           f"✅ Активных подписок: {active}\n\n" \
           f"Команды:\n" \
           f"/add ID DAYS - Выдать доступ вручную"
    
    await message.answer(text)
    session.close()

@dp.message(Command("add"))
async def cmd_admin_add(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        _, target_id, days = message.text.split()
        days = int(days)
        target_id = int(target_id)
        
        await grant_access(target_id, days, "Manual_Admin")
        await message.answer(f"Пользователь {target_id} добавлен на {days} дней.")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

# ==========================================
# ФУНКЦИИ УПРАВЛЕНИЯ ДОСТУПОМ
# ==========================================
async def grant_access(user_id, days, tariff_name):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=user_id).first()
    
    # Если юзера нет в базе (например, оплатил, но не жал старт - редкость)
    if not user:
        user = User(telegram_id=user_id)
        session.add(user)

    # Обновляем даты
    now = datetime.now()
    if user.is_active and user.expiry_date and user.expiry_date > now:
        user.expiry_date += timedelta(days=days) # Продлеваем
    else:
        user.start_date = now
        user.expiry_date = now + timedelta(days=days)
    
    user.is_active = True
    user.tariff = tariff_name
    
    # Генерируем ссылку (одноразовая)
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            name=f"User_{user_id}",
            expire_date=None # Ссылка не истекает по времени, но истекает после 1 входа
        )
        user.invite_link = invite.invite_link
        
        await bot.send_message(
            user_id,
            f"✅ Оплата успешна! Подписка продлена до {user.expiry_date.strftime('%d.%m.%Y')}.\n\n"
            f"Ваша ссылка для входа:\n{invite.invite_link}"
        )
    except Exception as e:
        logging.error(f"Не удалось создать ссылку: {e}")
        await bot.send_message(ADMIN_ID, f"Ошибка создания ссылки для {user_id}: {e}")

    session.commit()
    session.close()

async def revoke_access(user_id):
    try:
        # Кикаем из канала
        await bot.ban_chat_member(CHANNEL_ID, user_id)
        # Сразу разбаниваем, чтобы мог вернуться при оплате
        await bot.unban_chat_member(CHANNEL_ID, user_id)
        
        session = SessionLocal()
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if user:
            user.is_active = False
            # Можно также аннулировать ссылку через edit_chat_invite_link, если нужно
            session.commit()
        session.close()
        
        await bot.send_message(user_id, "⛔ Ваша подписка истекла. Доступ закрыт.")
    except Exception as e:
        logging.error(f"Ошибка при кике {user_id}: {e}")

# ==========================================
# WEB SERVER (HANDLER ДЛЯ WAYFORPAY)
# ==========================================
async def handle_wayforpay_webhook(request):
    try:
        data = await request.json() # Или request.post() в зависимости от того, как шлет WFP
    except:
        # WFP иногда шлет как form-data, иногда как json raw body
        # Для надежности читаем текст
        text = await request.text()
        data = json.loads(text)

    # Логируем входящий запрос (для отладки)
    logging.info(f"Webhook data: {data}")

    # Проверка обязательных полей
    if 'orderReference' not in data or 'transactionStatus' not in data:
        return web.Response(status=400)

    order_ref = data['orderReference'] # SUB_USERID_TIME
    status = data['transactionStatus']
    
    # Извлекаем ID юзера из orderReference
    try:
        user_id = int(order_ref.split('_')[1])
    except:
        return web.Response(text="Bad order ref", status=400)

    response_data = {
        "orderReference": order_ref,
        "status": "accept",
        "time": int(time.time()),
        "signature": ""
    }

    if status == 'Approved':
        # Вычисляем срок (в реальном проекте парсим productPrice или ищем заказ в БД)
        # Упрощение: если сумма 100 -> 30 дней, 270 -> 90 дней и т.д.
        amount = float(data.get('amount', 0))
        days = 30
        tariff_name = "Unknown"
        
        for k, v in TARIFFS.items():
            if abs(v['price'] - amount) < 1.0: # Сравнение float
                days = v['days']
                tariff_name = v['name']
                break
        
        await grant_access(user_id, days, tariff_name)

    elif status == 'Declined' or status == 'Expired':
        # Неудачное автосписание
        await bot.send_message(user_id, "❌ Не удалось продлить подписку. Пожалуйста, проверьте карту и оплатите вручную.")
        # Планировщик сам кикнет его, когда дата expiry_date пройдет окончательно

    # Формируем ответ для WFP
    sign_str = ";".join(map(str, [order_ref, "accept", response_data['time']]))
    response_data['signature'] = generate_signature(sign_str)
    
    return web.json_response(response_data)

# ==========================================
# ПЛАНИРОВЩИК (SCHEDULER)
# ==========================================
async def check_subscriptions():
    session = SessionLocal()
    users = session.query(User).filter(User.is_active == True).all()
    now = datetime.now()
    
    for user in users:
        if not user.expiry_date:
            continue
            
        time_left = user.expiry_date - now
        days_left = time_left.days
        
        if days_left == 3:
            try:
                await bot.send_message(user.telegram_id, "⏳ Ваша подписка истекает через 3 дня.")
            except: pass
            
        elif days_left == 0 and 0 < time_left.seconds < 43200: # Утром в день окончания
             try:
                await bot.send_message(user.telegram_id, "❗ Подписка истекает сегодня. Ожидайте автосписания.")
             except: pass
             
        elif time_left.total_seconds() < 0:
            # Срок вышел
            await revoke_access(user.telegram_id)
            
    session.close()

# ==========================================
# ЗАПУСК
# ==========================================
async def on_startup(app):
    # Настройка Webhook для бота (если бы использовали webhook метод телеграма, но мы используем polling для бота)
    # Здесь мы запускаем только планировщик
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_subscriptions, 'interval', hours=12)
    scheduler.start()
    
    # Запускаем поллинг бота в фоновой задаче
    asyncio.create_task(dp.start_polling(bot))

def main():
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_wayforpay_webhook)
    app.on_startup.append(on_startup)
    
    # Получаем порт от Render, по умолчанию 10000 (стандарт Render)
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, host='0.0.0.0', port=port)



if __name__ == '__main__':
    main()
