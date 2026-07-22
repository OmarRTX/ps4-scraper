import re
import time
import requests
from bs4AndWait import *  # أو مكتباتك المستخدمة


def check_deal(title, price_text, link):
  # استخراج الرقم من نص السعر
  prices = re.findall(r'\d+', price_text.replace(',', ''))
  if not prices:
    return
  price = int(prices[0])

  # استبعاد التقسيط نهائياً
  forbidden_words = ['تقسيط', 'قسط', 'مقدم', 'أقساط']
  if any(word in title.lower() for word in forbidden_words):
    return

  # تحديد النوع والحد الأقصى للسعر
  title_lower = title.lower()
  is_valid = False

  if 'pro' in title_lower and 4000 <= price <= 12000:
    is_valid = True
  elif 'slim' in title_lower and 4000 <= price <= 9500:
    is_valid = True
  elif ('fat' in title_lower or 'عادي' in title_lower) and 4000 <= price <= 8000:
    is_valid = True
  # لو الجهاز بلايستيشن 4 ومش متصنف بوضوح بس السعر في نطاق الآمان العام
  elif 4000 <= price <= 9000:
    is_valid = True

  if is_valid:
    send_telegram_alert(title, price, link)


def send_telegram_alert(title, price, link):
    # كود الإرسال لتليجرام
    pass
