from splinter import Browser
from bs4 import BeautifulSoup
import time
import re
import requests

TELEGRAM_TOKEN = "8964464444:AAHiz68AbrDRI3LeYa-8yUsBUo0bdbtZBLU"
CHAT_ID = "1219916834"

def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Telegram error: {e}")

def check_if_deal(title, price_num):
    title_lower = title.lower()
    if price_num < 4000:
        return False
    if 'pro' in title_lower or 'برو' in title_lower:
        return price_num <= 12000
    elif 'slim' in title_lower or 'سليم' in title_lower:
        return price_num <= 9000
    else:
        return price_num <= 8000

print("Starting browser session for 24/7 monitoring...")
from selenium import webdriver
options = webdriver.ChromeOptions()
options.add_argument("--headless")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")

browser = Browser('chrome', options=options)
url = "https://www.facebook.com/marketplace/104088052961201/search?minPrice=4000&maxPrice=12000&query=%D8%A8%D9%84%D8%A7%D9%8A%D8%B3%D8%AA%D9%8A%D8%B4%D9%86%204&exact=false"

# قائمة لحفظ الصفقات اللي تم إرسالها عشان ميتكررش إرسال نفس الإعلان
sent_deals = set()

try:
    while True:
        print("\n--- Scanning Marketplace for new deals ---")
        browser.visit(url)
        time.sleep(5)
        
        html = browser.html
        soup = BeautifulSoup(html, 'html.parser')
        
        title_class = "x1lliihq x6ikm8r x10wlt62 x1n2onr6 xlyipyv xuxw1ft" 
        titles = soup.find_all('span', class_=title_class)
        
        links_to_check = []
        for t in titles:
            title_text = t.text
            link_tag = t.find_parent('a')
            if link_tag and link_tag.get('href'):
                href = link_tag.get('href')
                if 'create' in href:
                    continue
                item_link = href if href.startswith('http') else f"https://www.facebook.com{href}"
                # تنظيف اللينك من أي بارامترات زائدة عشان الكشف يكون دقيق
                clean_link = item_link.split('?')[0]
                if clean_link not in [l[1].split('?')[0] for l in links_to_check]:
                    links_to_check.append((title_text, item_link))

        print(f"Found {len(links_to_check)} listings. Verifying prices...")

        for title_text, item_link in links_to_check[:10]:
            clean_link_base = item_link.split('?')[0]
            if clean_link_base in sent_deals:
                continue # لو اتبعت قبل كده، اخليه يتخطاه عشان ميصدعكش

            try:
                browser.visit(item_link)
                time.sleep(2)
                item_html = browser.html
                item_soup = BeautifulSoup(item_html, 'html.parser')
                
                price_text = ""
                all_spans = item_soup.find_all('span')
                for s in all_spans:
                    if 'ج.م' in s.text or 'LE' in s.text or 'جنيه' in s.text:
                        price_text = s.text
                        break
                
                clean_price = int(re.sub(r'[^\d]', '', price_text)) if price_text else 0
                
                if clean_price >= 4000:
                    if check_if_deal(title_text, clean_price):
                        sent_deals.add(clean_link_base)
                        deal_msg = f"🔥 <b>صفقة بلايستيشن حقيقية ومضمونة!</b>\n\n🎮 الجهاز: {title_text}\n💰 السعر الفعلي: {clean_price} جنيه\n🔗 <a href='{item_link}'>رابط الإعلان على الماركت بليس</a>"
                        print(f"New Deal Sent: {clean_price} EGP")
                        send_telegram_message(deal_msg)
            except Exception as e:
                continue
        
        print("Waiting 60 seconds before next scan...")
        time.sleep(60) # بيستريح دقيقة ويرجع يفحص تاني تلقائياً على مدار الساعة

except KeyboardInterrupt:
    print("Monitoring stopped by user.")
    browser.quit()