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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, BigInteger
from sqlalchemy.orm import sessionmaker, declarative_base
import aiohttp

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================
MERCHANT_ACCOUNT = os.getenv('MERCHANT_ACCOUNT', 'test_merch_n1')
MERCHANT_SECRET = os.getenv('MERCHANT_SECRET', 'flk3409refn54t54t*FNJRET')
TG_API_TOKEN = os.getenv('TG_API_TOKEN', '8198828061:AAE-pKTb0lSgJ3E9w1_m29uQyd_KZum9yLc')

CHANNEL_ID = -1003690130785
ADMIN_ID = 367335715

BASE_WEBHOOK_URL = os.getenv('BASE_WEBHOOK_URL', 'https://bot-subs.onrender.com') 
WEBHOOK_PATH = "/wayforpay/callback"

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
    active_order_ref = Column(String, nullable=True) # ID заказа для отмены

# Если добавляем новую колонку в существующую SQLite базу, лучше удалить старый файл db
# или использовать миграции. Для простоты теста - удаляем файл вручную если будут ошибки.
engine = create_engine('sqlite:///bot_database.db', echo=False)
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)

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
    """ Генерация ссылки (Purchase) """
    tariff = TARIFFS[tariff_key]
    order_ref = f"SUB_{user_id}_{int(time.time())}"
    order_date = int(time.time())
    amount = tariff['price']
    product_name = f"Subscription {tariff['name']}"
    
    sign_list = [MERCHANT_ACCOUNT, "t.me/Bot", order_ref, order_date, amount, "UAH", product_name, 1, amount]
    signature = generate_signature(";".join(map(str, sign_list)))

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

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://secure.wayforpay.com/pay?behavior=offline", data=payload) as response:
                data = json.loads(await response.text())
                if "url" in data: return data["url"], order_ref
                logging.error(f"WFP Error: {data}")
        except Exception as e:
            logging.error(f"HTTP Error: {e}")
            
    return None, None

async def cancel_wfp_subscription(order_ref):
    """ 
    Отмена регулярного платежа.
    Используется метод REMOVE в regularApi с передачей пароля.
    """
    if not order_ref: return False

    payload = {
        "requestType": "REMOVE",
        "merchantAccount": MERCHANT_ACCOUNT,
        "merchantPassword": MERCHANT_SECRET, # Прямой пароль (Secret Key)
        "orderReference": order_ref
    }

    url = "https://api.wayforpay.com/regularApi" 

    async with aiohttp.ClientSession() as session:
        try:
            # Важно: WayForPay иногда капризен к Content-Type, поэтому json=payload подходит лучше всего
            async with session.post(url, json=payload) as response:
                text_response = await response.text()
                logging.info(f"Cancel WFP Response: {text_response}")
                
                try:
                    data = json.loads(text_response)
                except:
                    logging.error(f"Cancel Failed: Invalid JSON response")
                    return False

                # Проверка успеха
                # Успешный код часто пустой или "Ok" в поле reason
                reason = data.get("reason", "").lower()
                code = str(data.get("reasonCode", ""))
                
                # 4100 - это стандартный код успеха для Regular API
                # 1100 - для основного API
                if code == "4100" or reason == "ok" or code == "1100":
                    return True
                
                logging.error(f"Cancel failed: {code} - {data.get('reason')}")
                return False
                
        except Exception as e:
            logging.error(f"Cancel API Connection Error: {e}")
            return False


# ==========================================
# БОТ (КЛАВИАТУРЫ)
# ==========================================
def get_main_keyboard():
    # Главное меню внизу экрана
    kb = [
        [KeyboardButton(text="👤 Профиль / Статус"), KeyboardButton(text="💳 Купить подписку")],
        [KeyboardButton(text="🆘 Поддержка")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_tariffs_keyboard():
    # Инлайн кнопки тарифов
    kb = []
    for key, data in TARIFFS.items():
        kb.append([InlineKeyboardButton(text=f"{data['name']} - {data['price']} UAH", callback_data=f"buy_{key}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_profile_keyboard(user_id):
    # Кнопки в профиле
    kb = [
        [InlineKeyboardButton(text="❌ Отменить автопродление", callback_data="cancel_sub")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

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
        user = User(
            telegram_id=message.from_user.id, 
            username=message.from_user.username,
            full_name=message.from_user.full_name
        )
        session.add(user)
        session.commit()
    session.close()

    await message.answer(
        "👋 Добро пожаловать!\nЯ бот для доступа к закрытому каналу.\n\n"
        "Используйте меню ниже для управления.",
        reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "💳 Купить подписку")
async def msg_buy(message: types.Message):
    await message.answer("Выберите тарифный план:", reply_markup=get_tariffs_keyboard())

@dp.message(F.text == "👤 Профиль / Статус")
async def msg_profile(message: types.Message):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=message.from_user.id).first()
    session.close()

    if not user:
        await message.answer("Ошибка: Пользователь не найден.")
        return

    if user.is_active and user.expiry_date and user.expiry_date > datetime.now():
        status = "✅ АКТИВНА"
        date_str = user.expiry_date.strftime('%d.%m.%Y')
        text = (
            f"👤 **Ваш профиль**\n\n"
            f"Статус подписки: {status}\n"
            f"Истекает: {date_str}\n"
            f"Тариф: {user.tariff}\n\n"
            f"🔗 Ваша ссылка: {user.invite_link or 'Нет'}"
        )
        # Показываем кнопку отмены только если активен
        await message.answer(text, parse_mode="Markdown", reply_markup=get_profile_keyboard(user.id))
    else:
        status = "❌ НЕ АКТИВНА"
        await message.answer(
            f"👤 **Ваш профиль**\n\nСтатус: {status}\nДля доступа купите подписку.",
            reply_markup=get_tariffs_keyboard()
        )

@dp.message(F.text == "🆘 Поддержка")
async def msg_support(message: types.Message):
    await message.answer(f"По всем вопросам пишите: @AdminUsername") # Замените на свой контакт

# --- Обработчики Callback ---

@dp.callback_query(F.data.startswith("buy_"))
async def process_buy(callback: types.CallbackQuery):
    tariff_key = callback.data.split("_", 1)[1]
    payment_url, order_ref = await get_payment_url(callback.from_user.id, tariff_key)
    
    if not payment_url:
        await callback.message.answer("⚠️ Ошибка. Попробуйте позже.")
        return

    # Можно сохранить order_ref как временный "attempt", но мы сохраняем только при успехе (webhook)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)]
    ])
    await callback.message.answer(f"Счет создан. Нажмите для оплаты:", reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data == "cancel_sub")
async def process_cancel_sub(callback: types.CallbackQuery):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=callback.from_user.id).first()
    
    if not user or not user.active_order_ref:
        await callback.message.answer("⚠️ У вас нет активной авто-подписки для отмены.")
        session.close()
        await callback.answer()
        return

    # Пытаемся отменить в WayForPay
    success = await cancel_wfp_subscription(user.active_order_ref)
    
    if success:
        user.active_order_ref = None # Стираем ID, чтобы не пытаться снова
        session.commit()
        await callback.message.answer("✅ Автопродление успешно отключено.\nВы сохраните доступ до конца оплаченного периода.")
        # Уведомляем админа
        await bot.send_message(ADMIN_ID, f"ℹ️ Пользователь {user.telegram_id} отключил автопродление.")
    else:
        await callback.message.answer("⚠️ Не удалось отключить автоматически. Пожалуйста, напишите в поддержку.")
    
    session.close()
    await callback.answer()

# ==========================================
# CORE LOGIC
# ==========================================
async def grant_access(user_id, days, tariff_name, order_ref=None):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=user_id).first()
    if not user:
        user = User(telegram_id=user_id)
        session.add(user)
    
    try: await bot.unban_chat_member(CHANNEL_ID, user_id)
    except: pass

    now = datetime.now()
    if user.is_active and user.expiry_date and user.expiry_date > now:
        user.expiry_date += timedelta(days=days)
    else:
        user.start_date = now
        user.expiry_date = now + timedelta(days=days)
    
    user.is_active = True
    user.tariff = tariff_name
    
    # СОХРАНЯЕМ ORDER REF ДЛЯ ОТМЕНЫ
    if order_ref:
        user.active_order_ref = order_ref
    
    try:
        if not user.invite_link:
            invite = await bot.create_chat_invite_link(
                chat_id=CHANNEL_ID, member_limit=1, name=f"U_{user_id}", expire_date=None 
            )
            user.invite_link = invite.invite_link
        
        await bot.send_message(
            user_id,
            f"✅ Подписка продлена до {user.expiry_date.strftime('%d.%m.%Y')}!\n"
            f"Ссылка: {user.invite_link}",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logging.error(f"Invite Error: {e}")
        
    session.commit()
    session.close()

async def revoke_access(user_id):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=user_id).first()
    
    try:
        # 1. Отмена в WayForPay
        if user and user.active_order_ref:
            await cancel_wfp_subscription(user.active_order_ref)
            user.active_order_ref = None # Сброс

        # 2. Убиваем ссылку
        if user and user.invite_link:
            try: await bot.revoke_chat_invite_link(CHANNEL_ID, user.invite_link)
            except: pass

        # 3. Бан
        await bot.ban_chat_member(CHANNEL_ID, user_id)
        
        if user:
            user.is_active = False
            user.invite_link = None
            session.commit()
        
        await bot.send_message(user_id, "⛔ Подписка истекла.")
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
        "`/ban ID` - Забрать доступ + Отмена подписки\n"
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
        await message.answer(f"✅ Доступ выдан ID {uid}")
    except:
        await message.answer("Ошибка. `/add ID DAYS`")

@dp.message(Command("ban"))
async def cmd_manual_ban(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid = int(message.text.split()[1])
        await revoke_access(uid)
        await message.answer(f"🚫 ID {uid} забанен, подписка отменена.")
    except:
        await message.answer("Ошибка. `/ban ID`")

@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid = int(message.text.split()[1])
        session = SessionLocal()
        u = session.query(User).filter_by(telegram_id=uid).first()
        session.close()
        if not u: 
            await message.answer("Нет в базе.")
            return
        status = "✅" if u.is_active else "❌"
        wfp_status = "ВКЛ" if u.active_order_ref else "ВЫКЛ"
        await message.answer(f"User: {u.full_name}\nStatus: {status}\nExpires: {u.expiry_date}\nAutoPay: {wfp_status}")
    except:
        await message.answer("Ошибка.")

@dp.message(Command("export"))
async def cmd_export(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    session = SessionLocal()
    users = session.query(User).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "TG_ID", "Name", "Active", "Expires", "OrderRef"])
    for u in users:
        writer.writerow([u.id, u.telegram_id, u.full_name, u.is_active, u.expiry_date, u.active_order_ref])
    session.close()
    output.seek(0)
    file_bytes = output.getvalue().encode('utf-8')
    document = types.BufferedInputFile(file_bytes, filename=f"users_{int(time.time())}.csv")
    await message.answer_document(document)

# ==========================================
# WEBHOOK
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
            
            # ВАЖНО: Передаем order_ref чтобы запомнить ID подписки
            await grant_access(uid, days, t_name, order_ref)
        except Exception as e:
            logging.error(f"Grant Error: {e}")

    return web.json_response(resp)

async def handle_ping(request):
    return web.Response(text="Bot OK")

# ==========================================
# RUN
# ==========================================
async def check_subs_job():
    session = SessionLocal()
    users = session.query(User).filter(User.is_active == True).all()
    now = datetime.now()
    for u in users:
        if not u.expiry_date: continue
        left = u.expiry_date - now
        if left.days == 3:
            try: await bot.send_message(u.telegram_id, "⏳ 3 дня до оплаты.")
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
