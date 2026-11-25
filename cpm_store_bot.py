"""CPM account store: Telegram webhook + Flask + MongoDB + Stars."""
from __future__ import annotations

import os, re, secrets, time, threading, json, logging
from datetime import datetime, timezone, timedelta
from html import escape
from urllib.parse import urlparse

import telebot
try:
    from dotenv import load_dotenv
except ImportError:
    # Render injects environment variables directly; dotenv is for local runs.
    def load_dotenv(*args, **kwargs):
        return False
from flask import Flask, abort, jsonify, request
from pymongo import MongoClient, ReturnDocument
from telebot import types

load_dotenv(".env.cpm_store")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
if not BOT_TOKEN or not MONGODB_URI:
    raise RuntimeError("BOT_TOKEN va MONGODB_URI .env faylida kerak")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.isdigit()}
if OWNER_ID: ADMIN_IDS.add(OWNER_ID)
DB = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000)[os.getenv("MONGODB_DB", "cpm_store")]
users, products, stock, orders, free_grants = (DB[x] for x in ("users", "products", "stock", "orders", "free_grants"))
bot_sessions = DB.bot_sessions
polling_locks = DB.polling_locks
orders.create_index("payload", unique=True, sparse=True)
orders.create_index([("user_id", 1), ("created_at", -1)])
stock.create_index([("product_id", 1), ("status", 1)])
free_grants.create_index([("user_id", 1), ("product_id", 1)], unique=True)
bot_sessions.create_index("expires_at", expireAfterSeconds=0)
class BotExceptionLogger(telebot.ExceptionHandler):
    """Log exceptions raised inside threaded Telegram handlers."""
    def handle(self, exception):
        logging.getLogger("CPMStoreBot").error("Telegram handler crashed",exc_info=exception)
        return False

# Webhook updates are handled before returning HTTP 200. This keeps failures
# visible to Flask/Render and prevents a successful webhook ACK while the
# Telegram handler is still queued in an idle worker thread.
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=False, exception_handler=BotExceptionLogger())
app = Flask(__name__)
app.logger.setLevel(logging.INFO)
class MongoSessionStore:
    """Small dict-like session store shared safely by polling/web workers."""
    def get(self, user_id, default=None):
        doc=bot_sessions.find_one({"_id":int(user_id),"expires_at":{"$gt":now()}})
        if not doc: return default
        doc.pop("_id",None); doc.pop("expires_at",None)
        return doc
    def __setitem__(self, user_id, state):
        doc=dict(state); doc["_id"]=int(user_id); doc["expires_at"]=now()+timedelta(hours=24)
        bot_sessions.replace_one({"_id":int(user_id)},doc,upsert=True)
    def update(self, user_id, fields):
        changes=dict(fields); changes["expires_at"]=now()+timedelta(hours=24)
        bot_sessions.update_one({"_id":int(user_id)},{"$set":changes},upsert=True)
    def pop(self, user_id, default=None):
        return bot_sessions.find_one_and_delete({"_id":int(user_id)}) or default
sessions=MongoSessionStore()

def acquire_polling_lock(owner):
    """Serialize polling across Render instances that share this Mongo database."""
    lock_id="telegram_get_updates"
    while True:
        expiry=now()+timedelta(seconds=120)
        try:
            lock=polling_locks.find_one_and_update(
                {"_id":lock_id,"expires_at":{"$lte":now()}},
                {"$set":{"owner":owner,"expires_at":expiry}},
                return_document=ReturnDocument.AFTER,
            )
            if lock and lock.get("owner")==owner: return
            try:
                polling_locks.insert_one({"_id":lock_id,"owner":owner,"expires_at":expiry})
                return
            except Exception as exc:
                # A duplicate _id means another process owns the lease. Other DB
                # errors are logged, then retried rather than starting unguarded.
                if exc.__class__.__name__!="DuplicateKeyError":
                    app.logger.exception("Could not acquire Telegram polling lock")
        except Exception:
            app.logger.exception("Could not check Telegram polling lock")
        app.logger.warning("Another instance owns Telegram polling; waiting for its lease")
        time.sleep(5)

def keep_polling_lock(owner, stop_event):
    while not stop_event.wait(30):
        try:
            result=polling_locks.update_one(
                {"_id":"telegram_get_updates","owner":owner},
                {"$set":{"expires_at":now()+timedelta(seconds=120)}}
            )
            if result.matched_count!=1:
                app.logger.critical("Telegram polling lock was lost; stopping this poller")
                bot.stop_polling()
                return
        except Exception:
            app.logger.exception("Could not renew Telegram polling lock")
PRODUCTS = ["CPM1 VINIL AKK", "CPM1 FULL AKK", "CPM1 ANIME AKK", "CPM1 HD LOGO AKK"]
def product_names():
    """Built-in products plus products created from the admin panel."""
    extra=[doc["_id"] for doc in products.find({}, {"_id":1}).sort("_id",1) if doc.get("_id") not in PRODUCTS]
    return PRODUCTS+extra
def shop_product_names():
    # Do not expose a newly added product at zero price by accident.
    return [name for name in product_names() if name in PRODUCTS or int((products.find_one({"_id":name},{"price":1}) or {}).get("price",0))>0]
LANGS = {"uz": "🇺🇿 O‘zbekcha", "ru": "🇷🇺 Русский", "en": "🇬🇧 English"}
T = {
"uz": {"choose":"Tilni tanlang:","menu":"Asosiy menyu","buy":"🛍 Mahsulotlar","orders":"📦 Buyurtmalarim","support":"💬 Support","admin":"⚙️ Admin panel","sub":"Davom etish uchun kanalga obuna bo‘ling, keyin Tekshirish tugmasini bosing.","sub_all":"Quyidagi kanallarning barchasiga obuna bo‘ling:","subscribe":"📢 {name} kanaliga obuna bo‘lish","missing_link":"Kanal havolasi admin tomonidan sozlanmagan.","check":"✅ Tekshirish","not_sub":"Kanalga obuna topilmadi. Obuna bo‘lib qayta tekshiring.","quantity":"Nechta akkaunt kerak? Miqdorni son bilan yuboring:","quantity_invalid":"1 dan 50 gacha butun son yuboring.","purchase":"Sotib olasizmi?","yes":"✅ HA","no":"❌ YO‘Q","pay":"TO‘LOV TURINI TANLANG","adminpay":"👤 ADMIN ORQALI","stars":"⭐ STARS ORQALI","empty":"Hozircha mahsulot yo‘q.","orders_empty":"Buyurtmalaringiz yo‘q.","product_prompt":"Mahsulotni tanlang:","stock_count":"{count} ta","stock_empty":"❌ <b>{product}</b> hozircha qolmagan. Admin yangi akkaunt qo‘shgandan keyin qayta tanlang.","preview_default":"Akkaunt video abzori","price_unit":"Narxi: <b>{price} ⭐ / dona</b>","orders_title":"📦 Buyurtmalarim","order_product":"Mahsulot","order_quantity":"Miqdor","order_status":"Holat","order_date":"Sana","status_pending_admin":"Admin tasdig‘ini kutmoqda","status_pending_payment":"To‘lov kutilmoqda","status_delivered":"Yetkazildi","status_rejected":"Rad etildi","status_paid":"To‘landi","status_free":"Bepul","status_stock_error":"Zaxira xatosi","status_unknown":"Noma’lum","order_stale":"Buyurtma eskirgan. Qaytadan boshlang.","stock_short":"Zaxirada yetarli akkaunt yo‘q. Boshqa mahsulotni tanlang yoki keyinroq urinib ko‘ring.","manual_sent":"So‘rovingiz adminga yuborildi. Tasdiqlanishini kuting.","payment_help":"To‘lov qayd qilindi, ammo buyurtmani tekshirish uchun qo‘llab-quvvatlashga yozing.","support_text":"📩 Taklif yoki muammo bo‘lsa, <a href='https://t.me/cpm_java'>@cpm_java</a> ga yozing. Sizga yordam berishdan mamnunmiz."},
"ru": {"choose":"Выберите язык:","menu":"Главное меню","buy":"🛍 Товары","orders":"📦 Мои заказы","support":"💬 Поддержка","admin":"⚙️ Панель администратора","sub":"Чтобы продолжить, подпишитесь на канал и нажмите «Проверить».","sub_all":"Подпишитесь на все каналы ниже:","subscribe":"📢 Подписаться на {name}","missing_link":"Ссылка канала не настроена администратором.","check":"✅ Проверить","not_sub":"Подписка не найдена. Подпишитесь и проверьте снова.","quantity":"Сколько аккаунтов нужно? Отправьте число:","quantity_invalid":"Введите целое число от 1 до 50.","purchase":"Покупаете?","yes":"✅ ДА","no":"❌ НЕТ","pay":"ВЫБЕРИТЕ СПОСОБ ОПЛАТЫ","adminpay":"👤 ЧЕРЕЗ АДМИНА","stars":"⭐ ЧЕРЕЗ STARS","empty":"Товаров пока нет.","orders_empty":"У вас пока нет заказов.","product_prompt":"Выберите товар:","stock_count":"{count} шт.","stock_empty":"❌ <b>{product}</b> временно закончился. Попробуйте позже.","preview_default":"Видеообзор аккаунта","price_unit":"Цена: <b>{price} ⭐ / шт.</b>","orders_title":"📦 Мои заказы","order_product":"Товар","order_quantity":"Количество","order_status":"Статус","order_date":"Дата","status_pending_admin":"Ожидает подтверждения администратора","status_pending_payment":"Ожидает оплату","status_delivered":"Доставлен","status_rejected":"Отклонён","status_paid":"Оплачен","status_free":"Бесплатно","status_stock_error":"Ошибка наличия","status_unknown":"Неизвестно","order_stale":"Заказ устарел. Начните заново.","stock_short":"Недостаточно аккаунтов. Выберите другой товар или попробуйте позже.","manual_sent":"Запрос отправлен администратору. Ожидайте подтверждения.","payment_help":"Платёж получен, но заказ не подтверждён. Напишите в поддержку.","support_text":"📩 Есть предложение или проблема? Напишите <a href='https://t.me/cpm_java'>@cpm_java</a>. Мы постараемся помочь."},
"en": {"choose":"Choose your language:","menu":"Main menu","buy":"🛍 Products","orders":"📦 My orders","support":"💬 Support","admin":"⚙️ Admin panel","sub":"Subscribe to the channel to continue, then tap Check.","sub_all":"Please subscribe to all channels below:","subscribe":"📢 Join {name}","missing_link":"The channel link has not been configured by the admin.","check":"✅ Check","not_sub":"Subscription not found. Join the channel and check again.","quantity":"How many accounts do you need? Send a number:","quantity_invalid":"Send a whole number from 1 to 50.","purchase":"Would you like to buy?","yes":"✅ YES","no":"❌ NO","pay":"CHOOSE A PAYMENT METHOD","adminpay":"👤 VIA ADMIN","stars":"⭐ VIA STARS","empty":"No products are available yet.","orders_empty":"You have no orders yet.","product_prompt":"Choose a product:","stock_count":"{count} available","stock_empty":"❌ <b>{product}</b> is currently out of stock. Please try again later.","preview_default":"Account video preview","price_unit":"Price: <b>{price} ⭐ each</b>","orders_title":"📦 My orders","order_product":"Product","order_quantity":"Quantity","order_status":"Status","order_date":"Date","status_pending_admin":"Awaiting admin approval","status_pending_payment":"Awaiting payment","status_delivered":"Delivered","status_rejected":"Rejected","status_paid":"Paid","status_free":"Free","status_stock_error":"Stock error","status_unknown":"Unknown","order_stale":"This order expired. Please start again.","stock_short":"Not enough accounts in stock. Choose another product or try later.","manual_sent":"Your request was sent to the admin. Please wait for approval.","payment_help":"Payment was recorded, but the order needs review. Please contact support.","support_text":"📩 Have a suggestion or a problem? Contact <a href='https://t.me/cpm_java'>@cpm_java</a>. We’ll be glad to help."}}
T["uz"].update({"preview_error":"Video obzor hozircha ochilmadi.","admin_profile":"👤 Admin profili","payment_failed":"To‘lov qayd qilindi, ammo buyurtma tekshiruvi uchun adminga yozing.","rejected":"To‘lov so‘rovi admin tomonidan rad etildi.","checkout_failed":"Buyurtma muddati o‘tgan yoki zaxira yetarli emas.","invoice_description":"{quantity} ta akkaunt","account_header":"✅ <b>Akkauntlaringiz:</b>","account_item":"akkaunt","account_code":"Kod","account_email":"Gmail","account_password":"Parol","account_note":"Holati","product_unpriced":"Bu mahsulotning narxi hali belgilanmagan."})
T["ru"].update({"preview_error":"Видеообзор пока недоступен.","admin_profile":"👤 Профиль администратора","payment_failed":"Платёж записан, но для проверки заказа напишите администратору.","rejected":"Администратор отклонил запрос на оплату.","checkout_failed":"Срок заказа истёк или товара недостаточно.","invoice_description":"Аккаунтов: {quantity}","account_header":"✅ <b>Ваши аккаунты:</b>","account_item":"аккаунт","account_code":"Код","account_email":"Почта","account_password":"Пароль","account_note":"Статус","product_unpriced":"Цена этого товара ещё не настроена."})
T["en"].update({"preview_error":"The video preview is temporarily unavailable.","admin_profile":"👤 Admin profile","payment_failed":"Payment was recorded, but please contact the admin to review the order.","rejected":"The admin rejected the payment request.","checkout_failed":"The order expired or there is not enough stock.","invoice_description":"{quantity} account(s)","account_header":"✅ <b>Your accounts:</b>","account_item":"account","account_code":"Code","account_email":"Email","account_password":"Password","account_note":"Status","product_unpriced":"The price for this product has not been set yet."})
def now(): return datetime.now(timezone.utc)
def lang_of(uid):
    user=users.find_one({"_id":int(uid)},{"lang":1}) or {}
    return user.get("lang") if user.get("lang") in T else "uz"
def tr(uid,key,**values):
    text=T.get(lang_of(uid),T["uz"]).get(key,T["uz"].get(key,key))
    return text.format(**values) if values else text
def order_status_text(uid,status):
    return tr(uid,"status_"+str(status)) if "status_"+str(status) in T[lang_of(uid)] else tr(uid,"status_unknown")
def account_text(uid,product_id,account,index,total):
    note=account.get("extra") or ""
    return (f"<b>{esc(product_id)}</b> — {tr(uid,'account_item')} {index}/{total}\n"
            f"{tr(uid,'account_code')}: <code>{esc(account.get('account_code') or '—')}</code>\n"
            f"{tr(uid,'account_email')}: <code>{esc(account.get('email'))}</code>\n"
            f"{tr(uid,'account_password')}: <code>{esc(account.get('password'))}</code>"+
            (f"\n{tr(uid,'account_note')}: {esc(note)}" if note else ""))
def esc(v, n=1200): return escape(str(v or "")[:n])
def admin(uid): return uid in ADMIN_IDS
def button_url(value):
    """Turn a public Telegram username into a URL and reject malformed link values."""
    value=(value or "").strip()
    if not value: return None
    if value.startswith("tg://"):
        return value if urlparse(value).scheme=="tg" else None
    if value.startswith(("https://", "http://")):
        parsed=urlparse(value)
        return value if parsed.scheme in ("https", "http") and parsed.netloc and not any(ch.isspace() for ch in value) else None
    username=value.lstrip("@").strip()
    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return f"https://t.me/{username}"
    return None

def channel_username_from_url(value):
    """Extract a public channel username from t.me/username (not private invite links)."""
    value=(value or "").strip()
    if value and not value.startswith(("https://","http://")):
        value="https://"+value
    parsed=urlparse(value)
    if parsed.netloc.lower() not in ("t.me","www.t.me","telegram.me","www.telegram.me"):
        return ""
    parts=[x for x in parsed.path.split("/") if x]
    if parts and parts[0].lower()=="s": parts=parts[1:]
    if not parts or parts[0].startswith(("+","joinchat")): return ""
    username=parts[0].lstrip("@")
    return "@"+username if re.fullmatch(r"[A-Za-z0-9_]{5,32}",username) else ""
def inline_btn(text, callback_data=None, url=None, style=None):
    if style is None:
        data = callback_data or ""
        style = "success" if data in ("confirm:yes", "sub:check") or data.startswith("approve:") else (
            "danger" if data in ("confirm:no",) or data.startswith("reject:") or data.startswith("adm:unfree:") else "primary")
    return types.InlineKeyboardButton(text=text, callback_data=callback_data, url=button_url(url), style=style)
def reply_btn(text, style="primary"):
    return types.KeyboardButton(text=text, style=style)
def main_kb(uid):
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(reply_btn(tr(uid,"buy"),"success"),reply_btn(tr(uid,"orders"),"primary"))
    kb.row(reply_btn(tr(uid,"support"),"primary"))
    if admin(uid): kb.add(reply_btn(tr(uid,"admin"),"primary"))
    return kb
def language_prompt(chat):
    kb=types.InlineKeyboardMarkup(row_width=1)
    for code,label in LANGS.items(): kb.add(inline_btn(label, callback_data=f"lang:{code}"))
    bot.send_message(chat,T["uz"]["choose"],reply_markup=kb)
def configured_subscriptions():
    """Return all explicitly enabled mandatory channels (supports old one-channel config)."""
    settings=DB.settings.find_one({"_id":"main"})
    if settings is not None:
        if settings.get("enabled") is not True: return []
        configured=settings.get("channels")
        if not configured:
            configured=[{"chat_id":settings.get("channel", ""),"url":settings.get("url", "")}]
    else:
        raw=(os.getenv("FORCE_SUB_CHANNEL", "") or "").strip()
        link=(os.getenv("FORCE_SUB_URL", "") or "").strip()
        configured=[{"chat_id":x.strip(),"url":link if i==0 else ""} for i,x in enumerate(raw.split(",")) if x.strip()]
    result=[]; seen=set()
    for item in configured:
        if isinstance(item,str): item={"chat_id":item}
        channel=str(item.get("chat_id") or item.get("channel") or "").strip()
        if not channel or channel.lower().lstrip("@") in {"your_channel","example","placeholder","none","off"} or channel in seen: continue
        original_channel=channel
        link=str(item.get("url") or "").strip()
        # Older admin setups sometimes saved the public t.me link in chat_id.
        # Telegram getChatMember requires @username or the numeric chat ID.
        if channel.startswith(("https://","http://","t.me/","www.t.me/","telegram.me/")):
            parsed_username=channel_username_from_url(channel)
            if parsed_username:
                link=link or channel
                channel=parsed_username
        if not link and channel.startswith("@"): link=f"https://t.me/{channel[1:]}"
        title=str(item.get("title") or channel).strip()
        if title in (original_channel,link) or channel_username_from_url(title): title=channel
        result.append({"chat_id":channel,"url":button_url(link),"title":title})
        seen.add(channel)
    return result

def configured_subscription():
    """Compatibility helper for callers that still expect one channel."""
    channels=configured_subscriptions()
    return (channels[0]["chat_id"],channels[0]["url"] or "") if channels else ("","")

def subscription_status(uid):
    """Return (ok, diagnostic lines) so channel permission/config errors are visible to admins."""
    channels=configured_subscriptions()
    if not channels: return True,["Majburiy obuna o‘chiq yoki kanal sozlanmagan."]
    diagnostics=[]; all_joined=True
    for channel in channels:
        try:
            member=bot.get_chat_member(channel["chat_id"],uid)
            joined=member.status in ("member","administrator","creator") or (member.status=="restricted" and member.is_member)
            diagnostics.append(f"{channel['title']}: {member.status} — {'✅ obuna bor' if joined else '❌ obuna yo‘q'}")
            if not joined: all_joined=False
        except Exception as exc:
            all_joined=False
            reason=str(exc).replace("<","[").replace(">","]")[:350]
            diagnostics.append(f"{channel['title']}: ❌ Telegram tekshira olmadi ({reason})")
            app.logger.exception("Subscription check failed for %s",channel["chat_id"])
    return all_joined,diagnostics

def has_subscription(uid):
    return subscription_status(uid)[0]
def subscription_prompt(chat,uid):
    channels=configured_subscriptions()
    if not channels:
        bot.send_message(chat,tr(uid,"menu"),reply_markup=main_kb(uid)); return
    kb=types.InlineKeyboardMarkup(row_width=1)
    for channel in channels:
        label=channel["title"]
        kb.add(inline_btn(tr(uid,"subscribe",name=label),url=channel["url"]) if channel["url"] else inline_btn(f"📢 {label}",callback_data="sub:link_missing"))
    kb.add(inline_btn(tr(uid,"check"),callback_data="sub:check"))
    bot.send_message(chat,tr(uid,"sub")+"\n\n"+tr(uid,"sub_all"),reply_markup=kb)
def list_products(chat,uid):
    kb=types.InlineKeyboardMarkup(row_width=1)
    names=shop_product_names()
    if not names:
        bot.send_message(chat,tr(uid,"empty")); return
    for name in names:
        p=products.find_one({"_id":name}) or {"price":0,"video_file_id":None}
        amount=stock.count_documents({"product_id":name,"status":"available"})
        kb.add(inline_btn(f"{name} · {int(p.get('price',0))}⭐ · {tr(uid,'stock_count',count=amount)}",callback_data=f"item:{name}"))
    bot.send_message(chat,tr(uid,"product_prompt"),reply_markup=kb)
def send_item(chat,p,uid):
    caption=f"<b>{esc(p['_id'])}</b>\n\n{esc(p.get('description') or tr(uid,'preview_default'))}\n\n{tr(uid,'price_unit',price=int(p.get('price',0)))}"
    if p.get("video_file_id"): bot.send_video(chat,p["video_file_id"],caption=caption)
    else: bot.send_message(chat,caption)
@bot.message_handler(commands=["start","help"])
def start(m):
    uid=m.from_user.id
    users.update_one({"_id":uid},{"$set":{"username":m.from_user.username,"first_name":m.from_user.first_name,"updated_at":now()},"$setOnInsert":{"created_at":now()}},upsert=True)
    u=users.find_one({"_id":uid})
    if not u or not u.get("lang"):
        language_prompt(m.chat.id)
        return
    if not has_subscription(uid): subscription_prompt(m.chat.id,uid); return
    bot.send_message(m.chat.id,tr(uid,"menu"),reply_markup=main_kb(uid))
@bot.message_handler(commands=["admin"])
def admin_cmd(m):
    if admin(m.from_user.id): admin_panel(m.chat.id)
    else: bot.reply_to(m,"Bu bo‘lim faqat egasi/admin uchun.")
@bot.message_handler(commands=["cancel"])
def cancel(m): sessions.pop(m.from_user.id,None); bot.send_message(m.chat.id,tr(m.from_user.id,"menu"),reply_markup=main_kb(m.from_user.id))
@bot.message_handler(content_types=["text"])
def text_handler(m):
    uid=m.from_user.id; val=(m.text or "").strip(); state=sessions.get(uid)
    # Admins may also buy products. Their quantity input belongs to the shopper
    # flow, not the admin wizard; don't consume it as an admin-panel response.
    if state and admin(uid) and state.get("step")=="broadcast_input":
        prepare_broadcast(m); return
    if state and admin(uid) and state.get("step")!="quantity": admin_text(m,state); return
    if not (users.find_one({"_id":uid},{"lang":1}) or {}).get("lang"): language_prompt(m.chat.id); return
    if not has_subscription(uid): subscription_prompt(m.chat.id,uid); return
    if val in (tr(uid,"buy"),"🛍 Mahsulotlar"): list_products(m.chat.id,uid)
    elif val in (tr(uid,"support"),"💬 Support","💬 Поддержка"):
        bot.send_message(m.chat.id,tr(uid,"support_text"),disable_web_page_preview=True)
    elif val in (tr(uid,"orders"),"📦 Buyurtmalarim"):
        rows=list(orders.find({"user_id":uid}).sort("created_at",-1).limit(20))
        if not rows: bot.send_message(m.chat.id,tr(uid,"orders_empty"))
        else:
            out=[]
            for o in rows:
                created=o.get("created_at"); stamp=created.strftime("%d.%m.%Y %H:%M") if created else ""
                account_details=o.get("account_details") or []
                account_output=("\n\n"+"\n\n".join(account_text(uid,o["product_id"],a,i+1,len(account_details)) for i,a in enumerate(account_details))) if account_details else ("\n"+"\n".join(f"<code>{esc(a,400)}</code>" for a in o.get("accounts",[])) if o.get("accounts") else "")
                out.append(f"• {tr(uid,'order_product')}: <b>{esc(o['product_id'])}</b>\n{tr(uid,'order_quantity')}: {o['quantity']}\n{tr(uid,'order_status')}: <b>{esc(order_status_text(uid,o.get('status')))}</b> · {stamp}"+account_output)
            bot.send_message(m.chat.id,"<b>"+tr(uid,"orders_title")+"</b>\n\n"+"\n\n".join(out))
    elif val in (tr(uid,"admin"),"⚙️ Admin panel") and admin(uid): admin_panel(m.chat.id)
    elif state and state.get("step")=="quantity":
        if not val.isdigit() or not 1<=int(val)<=50: bot.send_message(m.chat.id,tr(uid,"quantity_invalid")); return
        state["quantity"]=int(val); sessions.update(uid,{"quantity":int(val)}); kb=types.InlineKeyboardMarkup(); kb.row(inline_btn(tr(uid,"yes"),callback_data="confirm:yes"),inline_btn(tr(uid,"no"),callback_data="confirm:no"))
        p=products.find_one({"_id":state["product_id"]}) or {"price":0}
        bot.send_message(m.chat.id,f"{tr(uid,'purchase')}\n{esc(state['product_id'])} × {state['quantity']} — {int(p.get('price',0))*state['quantity']} ⭐",reply_markup=kb)
    else: bot.send_message(m.chat.id,tr(uid,"menu"),reply_markup=main_kb(uid))
def admin_panel(chat):
    kb=types.InlineKeyboardMarkup(row_width=1)
    for p in product_names(): kb.add(inline_btn(f"⚙️ {p}",callback_data=f"adm:product:{p}"))
    kb.add(inline_btn("➕ Mahsulot qo‘shish",callback_data="adm:addproduct",style="success"))
    kb.row(inline_btn("🧾 Kutayotgan buyurtmalar",callback_data="adm:pending"),inline_btn("📊 Statistika",callback_data="adm:stats"))
    kb.add(inline_btn("📢 Majburiy obuna",callback_data="adm:sub"))
    kb.add(inline_btn("🔎 Obunani tekshirish",callback_data="adm:testsub"))
    kb.add(inline_btn("📣 Sotuvlar kanali",callback_data="adm:saleschannel"))
    kb.add(inline_btn("📣 Foydalanuvchilarga xabar",callback_data="adm:broadcast"))
    bot.send_message(chat,"<b>⚙️ Admin panel</b>\nMahsulot kartasi, narx, обзор видео, захира ва махсус функцияларни бошқаринг.",reply_markup=kb)

def prepare_broadcast(m):
    uid=m.from_user.id
    sessions[uid]={"step":"broadcast_confirm","source_chat_id":m.chat.id,"source_message_id":m.message_id}
    kb=types.InlineKeyboardMarkup(row_width=2)
    kb.row(inline_btn("✅ Yuborish",callback_data="adm:broadcast:send",style="success"),inline_btn("❌ Bekor qilish",callback_data="adm:broadcast:cancel",style="danger"))
    bot.send_message(m.chat.id,f"Ko‘rsatilgan xabar {users.count_documents({})} ta foydalanuvchiga nusxa qilib yuboriladi. Davom etamizmi?",reply_markup=kb)

def execute_broadcast(admin_chat,uid):
    state=sessions.get(uid) or {}
    if state.get("step")!="broadcast_confirm":
        bot.send_message(admin_chat,"Tarqatish xabari topilmadi. Qaytadan boshlang."); return
    recipients=users.find({}, {"_id":1})
    sent=failed=0
    for recipient in recipients:
        try:
            bot.copy_message(int(recipient["_id"]),state["source_chat_id"],state["source_message_id"])
            sent+=1
        except Exception:
            failed+=1
            app.logger.info("Broadcast failed for user %s",recipient.get("_id"))
        time.sleep(0.04)
    sessions.pop(uid,None)
    bot.send_message(admin_chat,f"Tarqatish yakunlandi. Yetkazildi: {sent}; yetkazilmadi: {failed}.")
def admin_text(m,s):
    uid=m.from_user.id; val=(m.text or "").strip(); step=s["step"]
    if val=="/cancel": sessions.pop(uid,None); admin_panel(m.chat.id); return
    if step=="new_product":
        name=" ".join(val.split())
        if not name or len(name.encode("utf-8"))>40 or "\n" in name:
            bot.send_message(m.chat.id,"Mahsulot nomi bo‘sh bo‘lmasin va 40 baytdan oshmasin. Qisqaroq nom yuboring yoki /cancel bosing."); return
        if name in product_names():
            bot.send_message(m.chat.id,"Bu nomdagi mahsulot allaqachon bor. Boshqa nom yuboring yoki /cancel bosing."); return
        products.insert_one({"_id":name,"price":0,"video_file_id":None,"description":"","created_at":now(),"custom":True})
        sessions.pop(uid,None)
        bot.send_message(m.chat.id,f"✅ <b>{esc(name)}</b> mahsulot qo‘shildi. Narxini, videosini va tavsifini mahsulot kartasidan sozlang.")
        admin_panel(m.chat.id); return
    if step=="price":
        if not val.isdigit() or not 1<=int(val)<=100000: bot.send_message(m.chat.id,"Stars narxini musbat butun son kiriting."); return
        products.update_one({"_id":s["product"]},{"$set":{"price":int(val)}},upsert=True); sessions.pop(uid,None); bot.send_message(m.chat.id,"Narx saqlandi."); return
    if step=="video":
        if val=="/skip": products.update_one({"_id":s["product"]},{"$set":{"video_file_id":None}},upsert=True); sessions.pop(uid,None); return
        bot.send_message(m.chat.id,"Video fayl yuboring yoki /skip."); return
    if step=="description": products.update_one({"_id":s["product"]},{"$set":{"description":val}},upsert=True); sessions.pop(uid,None); bot.send_message(m.chat.id,"Tavsif saqlandi."); return
    if step=="sub":
        if val.lower() in ("off","o‘chirish","ochirish"):
            DB.settings.update_one({"_id":"main"},{"$set":{"channels":[],"channel":"","url":"","enabled":False}},upsert=True)
            sessions.pop(uid,None); bot.send_message(m.chat.id,"Majburiy obuna o‘chirildi."); return
        channels=[]; invalid=[]
        for line_no,line in enumerate(val.splitlines(),1):
            line=line.strip()
            if not line: continue
            bits=[x.strip() for x in line.split("|",2)]
            channel=bits[0]
            link=bits[1] if len(bits)>1 else (f"https://t.me/{channel[1:]}" if channel.startswith("@") else "")
            if channel.startswith(("https://","http://","t.me/","www.t.me/","telegram.me/")):
                link=link or channel
                channel=channel_username_from_url(channel)
            if not (channel.startswith("@") or re.fullmatch(r"-?\d{5,20}",channel)):
                invalid.append(str(line_no)); continue
            valid_link=button_url(link)
            if not valid_link:
                invalid.append(str(line_no)); continue
            channels.append({"chat_id":channel,"url":valid_link,"title":bits[2] if len(bits)>2 and bits[2] else channel})
        if invalid or not channels:
            bot.send_message(m.chat.id,"Kanal formatini tekshiring. Har qator: @username | https://t.me/username | Ko‘rinadigan nom\nYopiq kanal uchun: -1001234567890 | https://t.me/+taklifhavola | Kanal nomi\nBir nechta kanalni alohida qatorlarda yuboring. O‘chirish: off")
            return
        DB.settings.update_one({"_id":"main"},{"$set":{"channels":channels,"channel":channels[0]["chat_id"],"url":channels[0]["url"],"enabled":True}},upsert=True)
        sessions.pop(uid,None); bot.send_message(m.chat.id,f"Majburiy obuna saqlandi: {len(channels)} ta kanal. Botni har bir kanalga administrator qilib qo‘ying."); return
    if step=="sales_channel":
        if val.lower() in ("off","o‘chirish","ochirish"):
            DB.settings.update_one({"_id":"main"},{"$set":{"sales_channel":""}},upsert=True)
            sessions.pop(uid,None); bot.send_message(m.chat.id,"Sotuvlar kanaliga xabar yuborish o‘chirildi."); return
        target=val
        if target.startswith(("https://","http://","t.me/")):
            target=channel_username_from_url(target)
        if not (target.startswith("@") or re.fullmatch(r"-?\d{5,20}",target)):
            bot.send_message(m.chat.id,"Kanal @username yoki -100... ko‘rinishidagi ID bo‘lishi kerak. O‘chirish: off"); return
        DB.settings.update_one({"_id":"main"},{"$set":{"sales_channel":target}},upsert=True)
        sessions.pop(uid,None); bot.send_message(m.chat.id,f"Sotuvlar kanali saqlandi: {esc(target)}. Botni kanalga administrator qilib qo‘ying."); return
    if step=="add_stock":
        added=0; invalid=[]
        for line_no,line in enumerate(val.splitlines(),1):
            if not line.strip(): continue
            parts=[x.strip() for x in line.split("|")]
            if len(parts)<3 or not parts[0] or "@" not in parts[1] or not parts[2]: invalid.append(str(line_no)); continue
            stock.insert_one({"product_id":s["product"],"account_code":parts[0],"email":parts[1],"password":parts[2],"extra":" | ".join(parts[3:]),"status":"available","created_at":now()}); added+=1
        if added:
            sessions.pop(uid,None); bot.send_message(m.chat.id,f"✅ {added} ta akkaunt zaxiraga qo‘shildi."+("\nFormat xato bo‘lgan qatorlar: "+", ".join(invalid) if invalid else "")); return
        bot.send_message(m.chat.id,"Format xato. Har bir akkauntni bitta qatorda shu tartibda yuboring:\n02 | email@gmail.com | parol | OK\nBir xabarda bir nechta qator jo‘natish mumkin."); return
    if step in ("free","unfree"):
        try: target=int(val.split()[0])
        except Exception: bot.send_message(m.chat.id,"Telegram ID raqamini birinchi yozing."); return
        if step=="free":
            pid=s["product"]
            try: free_grants.update_one({"user_id":target,"product_id":pid},{"$setOnInsert":{"remaining":1,"created_at":now()}},upsert=True); bot.send_message(m.chat.id,f"Bir martalik bepul huquq berildi: {target}, {esc(pid)}")
            except Exception: bot.send_message(m.chat.id,"Bu foydalanuvchiga bu mahsulot uchun huquq avval berilgan.")
        else:
            free_grants.delete_one({"user_id":target,"product_id":s["product"]}); bot.send_message(m.chat.id,"Bepul huquq o‘chirildi.")
        sessions.pop(uid,None)
@bot.message_handler(content_types=["photo","video","document","audio","voice","animation","sticker","video_note","location","contact","venue","poll","dice"])
def media(m):
    s=sessions.get(m.from_user.id)
    if s and admin(m.from_user.id) and s.get("step")=="broadcast_input":
        prepare_broadcast(m); return
    if s and admin(m.from_user.id) and s.get("step")=="video":
        if not getattr(m,"video",None):
            bot.reply_to(m,"Video yuboring yoki /skip yozing."); return
        products.update_one({"_id":s["product"]},{"$set":{"video_file_id":m.video.file_id}},upsert=True); sessions.pop(m.from_user.id,None); bot.reply_to(m,"Video обзор saqlandi.")
@bot.callback_query_handler(func=lambda c:True)
def callbacks(c):
    uid=c.from_user.id; data=c.data or ""; chat=c.message.chat.id
    if data!="sub:link_missing":
        try: bot.answer_callback_query(c.id)
        except Exception: app.logger.exception("Could not answer callback query")
    if data.startswith("lang:"):
        lang=data.split(":",1)[1]
        if lang not in LANGS:return
        users.update_one({"_id":uid},{"$set":{"lang":lang}},upsert=True)
        if not has_subscription(uid): subscription_prompt(chat,uid)
        else: bot.send_message(chat,tr(uid,"menu"),reply_markup=main_kb(uid))
        return
    if data=="sub:check":
        if has_subscription(uid): bot.send_message(chat,tr(uid,"menu"),reply_markup=main_kb(uid))
        else:
            bot.send_message(chat,tr(uid,"not_sub"))
            subscription_prompt(chat,uid)
        return
    if data=="sub:link_missing":
        bot.answer_callback_query(c.id,tr(uid,"missing_link"),show_alert=True); return
    if not admin(uid) and (not users.find_one({"_id":uid}) or not users.find_one({"_id":uid}).get("lang")):
        language_prompt(chat); return
    if not admin(uid) and not has_subscription(uid):
        subscription_prompt(chat,uid); return
    if data.startswith("item:"):
        pid=data[5:]
        if pid not in shop_product_names():
            bot.send_message(chat,tr(uid,"product_unpriced")); return
        p=products.find_one({"_id":pid}) or {"_id":pid,"price":0}
        available=stock.count_documents({"product_id":pid,"status":"available"})
        if available==0:
            bot.send_message(chat,tr(uid,"stock_empty",product=esc(pid)))
            for aid in ADMIN_IDS:
                try: bot.send_message(aid,f"⚠️ Zaxira bo‘sh: {esc(pid)}. Yangi akkauntlar qo‘shing.")
                except Exception: app.logger.exception("Could not notify admin about empty stock")
            return
        # Save the order step before sending any media. A missing/invalid video
        # file_id must not leave the user with a dead product button.
        sessions[uid]={"step":"quantity","product_id":pid}
        try:
            send_item(chat,p,uid)
        except Exception:
            app.logger.exception("Product preview failed for %s",pid)
            bot.send_message(chat,f"<b>{esc(pid)}</b>\n{tr(uid,'price_unit',price=int(p.get('price',0)))}\n\n{tr(uid,'preview_error')}")
        bot.send_message(chat,tr(uid,"quantity")); return
    if data=="confirm:no": sessions.pop(uid,None); bot.send_message(chat,tr(uid,"menu"),reply_markup=main_kb(uid)); return
    if data=="confirm:yes":
        s=sessions.get(uid)
        if not s or s.get("step")!="quantity": bot.send_message(chat,tr(uid,"order_stale")); return
        kb=types.InlineKeyboardMarkup(); kb.row(inline_btn(tr(uid,"adminpay"),callback_data="pay:admin"),inline_btn(tr(uid,"stars"),callback_data="pay:stars",style="success"))
        admin_profile=button_url(os.getenv("ADMIN_PROFILE_URL", "") or os.getenv("ADMIN_USERNAME", ""))
        if admin_profile: kb.add(inline_btn(tr(uid,"admin_profile"),url=admin_profile))
        bot.send_message(chat,tr(uid,"pay"),reply_markup=kb); return
    if data.startswith("pay:"):
        if data not in ("pay:admin","pay:stars"): return
        s=sessions.get(uid)
        if not s or s.get("step")!="quantity": bot.send_message(chat,tr(uid,"order_stale")); return
        qty=s["quantity"]; pid=s["product_id"]; p=products.find_one({"_id":pid}) or {"price":0}
        if stock.count_documents({"product_id":pid,"status":"available"})<qty:
            bot.send_message(chat,tr(uid,"stock_short"))
            for aid in ADMIN_IDS:
                try: bot.send_message(aid,f"⚠️ Zaxira yetarli emas: {esc(pid)}; talab {qty} dona, mavjud {stock.count_documents({'product_id':pid,'status':'available'})} dona.")
                except Exception: pass
            return
        grant=free_grants.find_one_and_update({"user_id":uid,"product_id":pid,"remaining":{"$gt":0}},{"$inc":{"remaining":-1}},return_document=ReturnDocument.BEFORE) if qty==1 else None
        if grant:
            order_id=secrets.token_hex(12); orders.insert_one({"_id":order_id,"user_id":uid,"product_id":pid,"quantity":1,"method":"free","status":"paid","created_at":now()})
            deliver(order_id,chat); sessions.pop(uid,None); return
        oid=secrets.token_hex(12); qty_available=stock.count_documents({"product_id":pid,"status":"available"})
        if qty_available<qty: bot.send_message(chat,tr(uid,"stock_short")); return
        order={"_id":oid,"user_id":uid,"product_id":pid,"quantity":qty,"method":data[4:],"price":int(p.get("price",0))*qty,"status":"pending_admin" if data=="pay:admin" else "pending_payment","created_at":now()}
        if data=="pay:stars":
            payload=f"cpm:{oid}:{secrets.token_urlsafe(8)}"; order["payload"]=payload; orders.insert_one(order)
            bot.send_invoice(chat,title=pid[:32],description=tr(uid,"invoice_description",quantity=qty)[:255],invoice_payload=payload,provider_token="",currency="XTR",prices=[types.LabeledPrice(label=f"{pid} × {qty}"[:32],amount=order["price"])],start_parameter=f"cpm{oid}")
        else:
            orders.insert_one(order); kb=types.InlineKeyboardMarkup(); kb.row(inline_btn("✅ HA — tasdiqlash",callback_data=f"approve:{oid}"),inline_btn("❌ YO‘Q — rad etish",callback_data=f"reject:{oid}"))
            name=users.find_one({"_id":uid}) or {}
            for aid in ADMIN_IDS:
                try: bot.send_message(aid,f"<b>To‘lov tasdiqlansinmi?</b>\n{esc(pid)} × {qty}\nFoydalanuvchi: {uid} @{esc(name.get('username',''))}\nNarx: {order['price']} ⭐",reply_markup=kb)
                except Exception: pass
            bot.send_message(chat,tr(uid,"manual_sent"))
        sessions.pop(uid,None); return
    if data.startswith("approve:") or data.startswith("reject:"):
        if not admin(uid):return
        action,oid=data.split(":",1); o=orders.find_one({"_id":oid,"status":"pending_admin"})
        if not o: bot.send_message(chat,tr(uid,"order_stale")); return
        if action=="approve":
            orders.update_one({"_id":oid,"status":"pending_admin"},{"$set":{"status":"paid","approved_by":uid,"approved_at":now()}}); deliver(oid,o["user_id"])
        else: orders.update_one({"_id":oid,"status":"pending_admin"},{"$set":{"status":"rejected","approved_by":uid}}); bot.send_message(o["user_id"],tr(o["user_id"],"rejected"))
        bot.send_message(chat,"Bajarildi."); return
    if data.startswith("adm:"):
        if not admin(uid):return
        _,action,*args=data.split(":")
        if action=="broadcast":
            if args and args[0]=="cancel":
                sessions.pop(uid,None); bot.send_message(chat,"Tarqatish bekor qilindi."); return
            if args and args[0]=="send":
                execute_broadcast(chat,uid); return
            sessions[uid]={"step":"broadcast_input"}
            bot.send_message(chat,"Foydalanuvchilarga yuboriladigan xabarni jo‘nating. Matn, rasm, video, fayl, audio va boshqa Telegram xabar turlari mumkin. Bekor qilish: /cancel")
            return
        if action=="saleschannel":
            current=(DB.settings.find_one({"_id":"main"}) or {}).get("sales_channel") or "sozlanmagan"
            sessions[uid]={"step":"sales_channel"}
            bot.send_message(chat,f"Sotuvlar kanali hozir: <code>{esc(current)}</code>\nKanal @username yoki -100... ko‘rinishidagi ID sini yuboring. Public t.me havolasi ham mumkin. O‘chirish: off")
            return
        if action=="addproduct":
            sessions[uid]={"step":"new_product"}
            bot.send_message(chat,"Yangi mahsulot nomini yuboring (40 baytgacha). Masalan: CPM2 GOLD AKK\nBekor qilish: /cancel")
            return
        if action=="testsub":
            channels=configured_subscriptions()
            if not channels:
                bot.send_message(chat,"Majburiy obuna sozlanmagan yoki o‘chiq. Admin → Majburiy obuna bo‘limida kanal qo‘shing."); return
            ok,lines=subscription_status(uid)
            bot.send_message(chat,"<b>Majburiy obuna diagnostikasi</b>\n"+"\n".join(esc(line,500) for line in lines)+("\n\n✅ Tekshiruv muvaffaqiyatli." if ok else "\n\n❌ Bot kanalga administrator sifatida qo‘shilganini va kanal ID/username to‘g‘riligini tekshiring."))
            return
        if action=="pending":
            rows=list(orders.find({"status":"pending_admin"}).sort("created_at",1).limit(20))
            if not rows: bot.send_message(chat,"Kutayotgan to‘lov so‘rovlari yo‘q."); return
            for o in rows:
                kb=types.InlineKeyboardMarkup(); kb.row(inline_btn("✅ HA — tasdiqlash",callback_data=f"approve:{o['_id']}"),inline_btn("❌ YO‘Q — rad etish",callback_data=f"reject:{o['_id']}"))
                bot.send_message(chat,f"Buyurtma <code>{o['_id']}</code>\n{esc(o['product_id'])} × {o['quantity']}\nUser: <code>{o['user_id']}</code>\nSumma: {o.get('price',0)}⭐",reply_markup=kb)
            return
        if action=="stats":
            parts=["<b>📊 Bot statistikasi</b>",f"Foydalanuvchilar: {users.count_documents({})}",f"Sotuvdagi akkauntlar: {stock.count_documents({'status':'available'})}",f"Yakunlangan buyurtmalar: {orders.count_documents({'status':'delivered'})}",f"Admin tasdig‘ini kutmoqda: {orders.count_documents({'status':'pending_admin'})}",f"To‘lovni kutmoqda: {orders.count_documents({'status':'pending_payment'})}"]
            bot.send_message(chat,"\n".join(parts)); return
        if action=="product":
            pid=":".join(args); p=products.find_one({"_id":pid}) or {"price":0}
            kb=types.InlineKeyboardMarkup(); kb.row(inline_btn("💰 Narx",callback_data=f"adm:price:{pid}"),inline_btn("🎬 Video обзор",callback_data=f"adm:video:{pid}")); kb.row(inline_btn("📝 Tavsif",callback_data=f"adm:desc:{pid}"),inline_btn("📥 Akkaunt qo‘shish",callback_data=f"adm:stock:{pid}",style="success")); kb.add(inline_btn("🎁 Bepul huquq berish",callback_data=f"adm:free:{pid}",style="success"),inline_btn("➖ Huquqni o‘chirish",callback_data=f"adm:unfree:{pid}"))
            bot.send_message(chat,f"{esc(pid)}\nNarx: {int(p.get('price',0))}⭐\nZaxira: {stock.count_documents({'product_id':pid,'status':'available'})}",reply_markup=kb); return
        if action in ("price","video","desc","stock","free","unfree"):
            pid=":".join(args); step={"price":"price","video":"video","desc":"description","stock":"add_stock","free":"free","unfree":"unfree"}[action]; sessions[uid]={"step":step,"product":pid}
            prompts={"price":"Yangi Stars narxini yuboring:","video":"Akkaunt обзор videosini yuboring (yoki /skip):","desc":"Yangi tavsifni yuboring:","add_stock":"Gmail va parolni alohida qatorda yuboring:\nemail@gmail.com\nparol","free":"Bir martalik bepul akkaunt oladigan foydalanuvchining Telegram ID sini yuboring:","unfree":"Bepul huquqi o‘chiriladigan foydalanuvchining Telegram ID sini yuboring:"}
            if step=="add_stock": prompts[step]="Har bir akkauntni bitta qatorda kiriting:\n02 | email@gmail.com | parol | OK\nTartib: kod | Gmail | parol | holati/izoh. Bir xabarda bir nechta akkaunt qo‘shish mumkin."
            bot.send_message(chat,prompts[step]); return
        if action=="sub": sessions[uid]={"step":"sub"}; bot.send_message(chat,"Majburiy obuna kanallarini har qatorga bittadan yuboring:\n@username | https://t.me/username | Kanal nomi\nYopiq kanal: -1001234567890 | https://t.me/+taklifhavola | Kanal nomi\nBot har bir kanalga administrator bo‘lishi kerak. O‘chirish uchun off yuboring."); return
        if action in ("free","unfree"):return
def deliver(oid,chat_id):
    o=orders.find_one_and_update({"_id":oid,"status":{"$in":["paid","delivered"]},"delivery_started":{"$ne":True}},
        {"$set":{"delivery_started":True,"delivery_started_at":now()}},return_document=ReturnDocument.AFTER)
    if not o:return
    accounts=[]
    for _ in range(o["quantity"]):
        acc=stock.find_one_and_update({"product_id":o["product_id"],"status":"available"},{"$set":{"status":"sold","order_id":oid,"sold_at":now()}},return_document=ReturnDocument.AFTER)
        if not acc: break
        accounts.append(acc)
    if len(accounts)<o["quantity"]:
        for a in accounts: stock.update_one({"_id":a["_id"]},{"$set":{"status":"available"},"$unset":{"order_id":""}})
        orders.update_one({"_id":oid},{"$set":{"status":"stock_error"},"$unset":{"delivery_started":""}})
        for aid in ADMIN_IDS:
            try: bot.send_message(aid,f"⚠️ Zaxira yetarli emas: {esc(o['product_id'])}, kerak {o['quantity']}. Akkaunt ma’lumotlarini qo‘shing.")
            except Exception: pass
        return
    orders.update_one({"_id":oid},{"$set":{"delivered":True,"status":"delivered","accounts":[f"Kod: {a.get('account_code','')}\nGmail: {a['email']}\nParol: {a['password']}"+("\nHolati: "+a['extra'] if a.get('extra') else "") for a in accounts],"account_details":[{"account_code":a.get("account_code",""),"email":a.get("email",""),"password":a.get("password",""),"extra":a.get("extra","")} for a in accounts],"delivered_at":now()}})
    if o.get("method") in ("admin","stars"):
        settings=DB.settings.find_one({"_id":"main"}) or {}
        sales_channel=settings.get("sales_channel")
        if sales_channel:
            buyer=users.find_one({"_id":int(o["user_id"])}) or {}
            username=(buyer.get("username") or "").strip().lstrip("@")
            if username:
                buyer_label=f"<a href='tg://user?id={int(o['user_id'])}'>@{esc(username,64)}</a>"
            else:
                buyer_label=f"<a href='tg://user?id={int(o['user_id'])}'>{esc(buyer.get('first_name') or 'Foydalanuvchi',100)}</a>"
            quantity=int(o.get("quantity",1) or 1)
            total=int(o.get("price",0) or 0)
            unit=total//quantity if quantity else total
            notice=("🛍 <b>YANGI XARID</b>\n\n"
                    f"👤 <b>FOYDALANUVCHI:</b> {buyer_label}\n"
                    f"🎮 <b>AKKAUNT TURI:</b> {esc(o.get('product_id',''),120)}\n"
                    f"🔢 <b>SONI:</b> {quantity} ta\n"
                    f"💳 <b>AKKAUNT NARXI:</b> {unit} ⭐\n"
                    f"💰 <b>JAMI:</b> {total} ⭐")
            try: bot.send_message(sales_channel,notice,disable_web_page_preview=True)
            except Exception: app.logger.exception("Could not send sale notice to configured sales channel")
    lines=[account_text(int(o["user_id"]),o["product_id"],a,i+1,len(accounts)) for i,a in enumerate(accounts)]
    bot.send_message(chat_id,tr(int(o["user_id"]),"account_header")+"\n\n"+"\n\n".join(lines))
    if stock.count_documents({"product_id":o["product_id"],"status":"available"})==0:
        for aid in ADMIN_IDS:
            try: bot.send_message(aid,f"⚠️ <b>{esc(o['product_id'])} tugadi.</b> Yangi akkaunt Gmail va parollarini matn ko‘rinishida yuboring.")
            except Exception: pass
@bot.message_handler(content_types=["successful_payment"])
def payment(m):
    p=m.successful_payment; o=orders.find_one({"payload":p.invoice_payload,"user_id":m.from_user.id,"status":"pending_payment"})
    if not o or p.currency!="XTR" or p.total_amount!=o["price"]: bot.send_message(m.chat.id,tr(m.from_user.id,"payment_failed")); return
    res=orders.update_one({"_id":o["_id"],"status":"pending_payment"},{"$set":{"status":"paid","charge_id":p.telegram_payment_charge_id,"paid_at":now()}})
    if res.modified_count: deliver(o["_id"],m.chat.id)
@bot.pre_checkout_query_handler(func=lambda q:True)
def pre_checkout(q):
    o=orders.find_one({"payload":q.invoice_payload,"user_id":q.from_user.id,"status":"pending_payment"}); ok=bool(o and q.currency=="XTR" and q.total_amount==o["price"] and stock.count_documents({"product_id":o["product_id"],"status":"available"})>=o["quantity"])
    bot.answer_pre_checkout_query(q.id,ok=ok,error_message=None if ok else tr(q.from_user.id,"checkout_failed"))
@app.get("/health")
def health():
    try: DB.command("ping"); return jsonify(status="ok",database="connected")
    except Exception:return jsonify(status="degraded",database="disconnected"),503
@app.post("/telegram/webhook")
def webhook():
    secret=os.getenv("WEBHOOK_SECRET","")
    if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token")!=secret:
        app.logger.warning("Rejected Telegram webhook request: secret header did not match")
        abort(403)
    try:
        body=request.get_data(as_text=True)
        data=json.loads(body)
        kinds=[key for key in ("message","callback_query","pre_checkout_query") if key in data]
        app.logger.info("Telegram webhook update received: id=%s type=%s",data.get("update_id"),",".join(kinds) or "other")
        bot.process_new_updates([types.Update.de_json(body)])
        return "ok"
    except Exception: app.logger.exception("Telegram update failed"); return "error",500
@app.cli.command("set-webhook")
def set_webhook():
    # PUBLIC_BASE_URL can override a custom domain; Render supplies
    # RENDER_EXTERNAL_URL automatically for Web Services.
    base=(os.getenv("PUBLIC_BASE_URL","").strip() or os.getenv("RENDER_EXTERNAL_URL","").strip()).rstrip("/")
    if not base:raise RuntimeError("PUBLIC_BASE_URL yoki Render RENDER_EXTERNAL_URL mavjud emas")
    webhook_url=base+"/telegram/webhook"
    bot.set_webhook(url=webhook_url,max_connections=1,secret_token=os.getenv("WEBHOOK_SECRET") or None,allowed_updates=["message","callback_query","pre_checkout_query"])
    info=bot.get_webhook_info()
    print(f"Webhook o‘rnatildi: {info.url}; navbatda={info.pending_update_count}; oxirgi xato={info.last_error_message or 'yo‘q'}",flush=True)
if __name__ == "__main__":
    mode=os.getenv("BOT_MODE", "polling").strip().lower()
    if mode == "polling":
        # Local development: direct Telegram polling doesn't require a public HTTPS endpoint.
        poller_id=secrets.token_hex(16)
        acquire_polling_lock(poller_id)
        lock_stop=threading.Event()
        lock_thread=threading.Thread(target=keep_polling_lock,args=(poller_id,lock_stop),daemon=True)
        lock_thread.start()
        try:
            bot.remove_webhook()
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        finally:
            lock_stop.set()
            polling_locks.delete_one({"_id":"telegram_get_updates","owner":poller_id})
    else:
        # Production webhook: configure using `flask --app cpm_store_bot set-webhook`.
        app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=False)

