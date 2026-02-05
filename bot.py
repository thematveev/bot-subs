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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, BigInteger
from sqlalchemy.orm import sessionmaker, declarative_base
import aiohttp

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================
# Данные из Render (Environment Variables)
MERCHANT_ACCOUNT = os.getenv('MERCHANT_ACCOUNT', 'test_merch_n1')
MERCHANT_SECRET = os.getenv('MERCHANT_SECRET', 'flk3409refn54t54t*FNJRET')
TG_API_TOKEN = os.getenv('TG_API_TOKEN', '8198828061:AAE-pKTb0lSgJ3E9w1_m29uQyd_KZum9yLc')

CHANNEL_ID = -1003690130785
ADMIN_ID = 367335715

# URL вашего приложения на Render
BASE_WEBHOOK_URL = os.getenv('BASE_WEBHOOK_URL', 'https://bot-subs.onrender.com') 
WEBHOOK_PATH = "/wayforpay/callback"

# Тарифы
TARIFFS = {
    "1_month": {"name": "1 Месяц", "price": 1, "days": 30, "period": "monthly"},
    "3_months": {"name": "3 Месяца", "price": 2, "days": 90, "period": "quarterly"},
    "6_months": {"name": "6 Месяцев", "price": 5, "days": 180, "period": "halfyearly"},
    "12_months": {"name": "1 Год", "price": 9, "days": 365, "period": "yearly"},
}

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

# Инициализация БД
engine = create_engine('sqlite:///bot_database.db', echo=False)
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)

# ==========================================
# WAYFORPAY
# ==========================================
def generate_signature(string_to_sign):
    return hmac.new(
        MERCHANT_SECRET.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.md5
    ).hexdigest()

async def get_payment_url(user_id, tariff_key):
    """
    Генерация ссылки на оплату через API WayForPay
    """
    tariff = TARIFFS[tariff_key]
    order_ref = f"SUB_{user_id}_{int(time.time())}"
    order_date = int(time.time())
    amount = tariff['price']
    product_name = f"Subscription {tariff['name']}"
    
    # 1. Подпись (Purchase)
    sign_list = [
        MERCHANT_ACCOUNT, 
        "t.me/Bot", 
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

    # 2. Payload
    payload = {
        'merchantAccount': MERCHANT_ACCOUNT,
        'merchantAuthType': 'SimpleSignature',
        'merchantDomainName': 't.me/Bot',
        'orderReference': order_ref,
        'orderDate': order_date,
        'amount': amount,
        'currency': 'UAH',
        'orderTimeout': 86400,
        'productName[]': product_name,
        'productPrice[]': amount,
        'productCount[]': 1,
        'clientFirstname': f"ID {user_id}",
        'clientLastname': "User",
        'serviceUrl': BASE_WEBHOOK_URL + WEBHOOK_PATH,
        'merchantSignature': signature
    }
    
    if 'period' in tariff:
        payload['regularMode'] = tariff['period']

    # 3. POST запрос
    async with aiohttp.ClientSession() as session:
        url = "https://secure.wayforpay.com/pay?behavior=offline"
        try:
            async with session.post(url, data=payload) as response:
                resp_text = await response.text()
                logging.info(f"WFP Init: {resp_text}")
                
                try:
                    data = json.loads(resp_text)
                    if "url" in data:
                        return data["url"], order_ref
                    if "reason" in data:
                        logging.error(f"WFP Error: {data['reason']}")
                except:
                    pass
        except Exception as e:
            logging.error(f"HTTP Error: {e}")
            
    return None, None

# ==========================================
# БОТ
# ==========================================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TG_API_TOKEN)
dp = Dispatcher()

def get_tariffs_keyboard():
    keyboard = []
    for key, data in TARIFFS.items():
        keyboard.append([InlineKeyboardButton(
            text=f"{data['name']} - {data['price']} UAH", 
            callback_data=f"buy_{key}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
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
    
    status_text = "❌ Нет активной подписки"
    if user.is_active and user.expiry_date:
        if user.expiry_date > datetime.now():
            status_text = f"✅ Активна до {user.expiry_date.strftime('%d.%m.%Y')}"
            
    session.close()

    await message.answer(
        f"👋 Привет!\nСтатус: {status_text}\n\n"
        "Выберите тариф:",
        reply_markup=get_tariffs_keyboard()
    )

@dp.callback_query(F.data.startswith("buy_"))
async def process_buy(callback: types.CallbackQuery):
    tariff_key = callback.data.split("_", 1)[1]
    payment_url, order_ref = await get_payment_url(callback.from_user.id, tariff_key)
    
    if not payment_url:
        await callback.message.answer("⚠️ Ошибка создания ссылки.")
        await callback.answer()
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)]
    ])
    
    await callback.message.answer(
        f"Тариф: {TARIFFS[tariff_key]['name']}.\n"
        f"Нажмите для оплаты:",
        reply_markup=markup
    )
    await callback.answer()

# ==========================================
# УПРАВЛЕНИЕ ДОСТУПОМ
# ==========================================
async def grant_access(user_id, days, tariff_name):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=user_id).first()
    if not user:
        user = User(telegram_id=user_id)
        session.add(user)
    
    # 1. Снимаем бан
    try: await bot.unban_chat_member(CHANNEL_ID, user_id)
    except: pass

    # 2. Даты
    now = datetime.now()
    if user.is_active and user.expiry_date and user.expiry_date > now:
        user.expiry_date += timedelta(days=days)
    else:
        user.start_date = now
        user.expiry_date = now + timedelta(days=days)
    
    user.is_active = True
    user.tariff = tariff_name
    
    # 3. Ссылка
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            name=f"U_{user_id}",
            expire_date=None 
        )
        user.invite_link = invite.invite_link
        
        await bot.send_message(
            user_id,
            f"✅ Подписка активна до {user.expiry_date.strftime('%d.%m.%Y')}!\n"
            f"Ваша ссылка:\n{invite.invite_link}"
        )
    except Exception as e:
        logging.error(f"Invite Error: {e}")
        await bot.send_message(ADMIN_ID, f"Ошибка выдачи ссылки ID {user_id}: {e}")
        
    session.commit()
    session.close()

async def revoke_access(user_id):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=user_id).first()
    
    try:
        # 1. Убиваем ссылку
        if user and user.invite_link:
            try:
                await bot.revoke_chat_invite_link(
                    chat_id=CHANNEL_ID,
                    invite_link=user.invite_link
                )
            except Exception as e:
                logging.warning(f"Revoke error: {e}")

        # 2. Баним (чтобы выгнать и закрыть вход)
        await bot.ban_chat_member(CHANNEL_ID, user_id)
        
        # 3. Чистим базу
        if user:
            user.is_active = False
            user.invite_link = None
            session.commit()
        
        await bot.send_message(user_id, "⛔ Срок подписки истек. Доступ закрыт.")
        
    except Exception as e:
        logging.error(f"Kick Error {user_id}: {e}")
    finally:
        session.close()

# ==========================================
# АДМИНКА
# ==========================================
@dp.message(Command("admin"))
async def cmd_admin_help(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    text = (
        "🛠 **Админка**\n"
        "`/stats` - Статистика\n"
        "`/add ID ДНИ` - Дать доступ\n"
        "`/ban ID` - Забрать доступ\n"
        "`/check ID` - Инфо\n"
        "`/export` - Скачать CSV"
    )
    await message.answer(text, parse_mode="Markdown")

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
        await message.answer("Ошибка. Пример: `/add 12345 30`")

@dp.message(Command("ban"))
async def cmd_manual_ban(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid = int(message.text.split()[1])
        await revoke_access(uid)
        await message.answer(f"🚫 ID {uid} забанен.")
    except:
        await message.answer("Ошибка. Пример: `/ban 12345`")

@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid = int(message.text.split()[1])
        session = SessionLocal()
        u = session.query(User).filter_by(telegram_id=uid).first()
        session.close()
        if not u: 
            await message.answer("Не найден.")
            return
        status = "✅" if u.is_active else "❌"
        link = u.invite_link if u.invite_link else "Нет"
        await message.answer(f"User: {u.full_name}\nStatus: {status}\nExpires: {u.expiry_date}\nLink: {link}")
    except:
        await message.answer("Ошибка. Пример: `/check 12345`")

@dp.message(Command("export"))
async def cmd_export(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    session = SessionLocal()
    users = session.query(User).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "TG_ID", "Username", "Name", "Tariff", "Active", "Start", "End", "Link"])
    
    for u in users:
        writer.writerow([
            u.id, u.telegram_id, u.username, u.full_name, u.tariff, 
            u.is_active, u.start_date, u.expiry_date, u.invite_link
        ])
    
    session.close()
    output.seek(0)
    file_bytes = output.getvalue().encode('utf-8')
    document = types.BufferedInputFile(file_bytes, filename=f"users_{int(time.time())}.csv")
    
    await message.answer_document(document, caption="📂 Экспорт базы пользователей")

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

    resp = {"orderReference": order_ref, "status": "accept", "time": int(time.time())}
    resp['signature'] = generate_signature(f"{order_ref};accept;{resp['time']}")

    if status == 'Approved':
        try:
            uid = int(order_ref.split('_')[1])
            amount = float(data.get('amount', 0))
            
            days = 30
            t_name = "Auto"
            for k, v in TARIFFS.items():
                if abs(v['price'] - amount) < 1.0:
                    days = v['days']
                    t_name = v['name']
                    break
            
            await grant_access(uid, days, t_name)
        except Exception as e:
            logging.error(f"Grant Error: {e}")

    elif status in ['Declined', 'Expired']:
        try:
            uid = int(order_ref.split('_')[1])
            await bot.send_message(uid, "❌ Автоплатеж отклонен.")
        except: pass

    return web.json_response(resp)

async def handle_ping(request):
    return web.Response(text="Bot OK")

# ==========================================
# ЗАПУСК
# ==========================================
async def check_subs_job():
    session = SessionLocal()
    users = session.query(User).filter(User.is_active == True).all()
    now = datetime.now()
    
    for u in users:
        if not u.expiry_date: continue
        left = u.expiry_date - now
        
        if left.days == 3:
            try: await bot.send_message(u.telegram_id, "⏳ Осталось 3 дня.")
            except: pass
        elif left.days == 0 and 0 < left.seconds < 43200:
             try: await bot.send_message(u.telegram_id, "❗ Сегодня списание.")
             except: pass
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
