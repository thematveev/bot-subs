import os

MERCHANT_ACCOUNT = os.getenv('MERCHANT_ACCOUNT', 'test_merch_n1')
MERCHANT_SECRET = os.getenv('MERCHANT_SECRET', 'flk3409refn54t54t*FNJRET')
MERCHANT_PASSWORD = os.getenv('MERCHANT_PASSWORD', 'flk3409refn54t54t*FNJRET')
TG_API_TOKEN = os.getenv('TG_API_TOKEN', '8198828061:AAE-pKTb0lSgJ3E9w1_m29uQyd_KZum9yLc')

CHANNEL_ID = int(os.getenv('CHANNEL_ID', '-1003690130785'))
ADMIN_ID = int(os.getenv('ADMIN_ID', '367335715'))

BASE_WEBHOOK_URL = os.getenv('BASE_WEBHOOK_URL', 'https://bot.thematveev.xyz') 
WEBHOOK_PATH = "/wayforpay/callback"

TARIFFS = {
    "1_month": {"name": "1 Месяц", "price": 1, "days": 30, "period": "monthly"},
    "3_months": {"name": "3 Месяца", "price": 2, "days": 90, "period": "quarterly"},
    "6_months": {"name": "6 Месяцев", "price": 5, "days": 180, "period": "halfyearly"},
    "12_months": {"name": "1 Год", "price": 9, "days": 365, "period": "yearly"},
}

for i in [MERCHANT_ACCOUNT, MERCHANT_SECRET, MERCHANT_PASSWORD, TG_API_TOKEN, CHANNEL_ID, ADMIN_ID, BASE_WEBHOOK_URL]:
    print(i)