import asyncio
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Optional

import flask
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
    Update,
)

# Optional engines (loyihadagi alohida fayllar)
try:
    import logo_engine  # type: ignore
except Exception:
    logo_engine = None
try:
    from template_engine import render_template, save_as_tgs  # type: ignore
except Exception:
    render_template = save_as_tgs = None  # type: ignore
try:
    from templates_config import EMOJI_IDS, TEMPLATE_ORDER, TEMPLATES  # type: ignore
except Exception:
    TEMPLATES, TEMPLATE_ORDER, EMOJI_IDS = {}, [], {}  # type: ignore
try:
    from logo_emoji_ids import LOGO_TEMPLATE_EMOJI_IDS  # type: ignore
except Exception:
    LOGO_TEMPLATE_EMOJI_IDS = {}  # type: ignore

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("8977201289:AAFft4dtWHHgtj2oDj7D_5udtxtWA1zeE5k", "8977201289:AAFft4dtWHHgtj2oDj7D_5udtxtWA1zeE5k").strip()
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://bmurotova822_db_user:mFqccIhPanyD69ko@cluster0.qqzmvxj.mongodb.net/?appName=Cluster0").strip()
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "telegram_bot").strip()
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "5000"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change_me_admin")


def _parse_admin_ids() -> set[int]:
    raw = os.environ.get("ADMIN_IDS") or os.environ.get("6968399046") or ""
    ids: set[int] = set()
    for part in re.split(r"[,;\s]+", raw.strip()):
        if part.isdigit():
            ids.add(int(part))
    return ids


ADMIN_IDS: set[int] = _parse_admin_ids()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------

mongo_client: Optional[MongoClient] = None
db = None


def connect_mongo() -> bool:
    global mongo_client, db
    if not MONGO_URI:
        logger.error("MONGO_URI is not set")
        return False
    try:
        mongo_client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=8000,
            connectTimeoutMS=8000,
            maxPoolSize=50,
        )
        mongo_client.admin.command("ping")
        db = mongo_client[MONGO_DB_NAME]
        ensure_indexes()
        ensure_default_settings()
        ensure_default_admin()
        logger.info("MongoDB connected: %s", MONGO_DB_NAME)
        return True
    except (ServerSelectionTimeoutError, PyMongoError) as e:
        logger.error("MongoDB connection failed: %s", e)
        mongo_client = None
        db = None
        return False


def ensure_indexes() -> None:
    if db is None:
        return
    db.users.create_index([("telegram_id", ASCENDING)], unique=True)
    db.users.create_index([("username", ASCENDING)])
    db.users.create_index([("referral_code", ASCENDING)], unique=True, sparse=True)
    db.users.create_index([("joined_at", DESCENDING)])
    db.admins.create_index([("telegram_id", ASCENDING)], unique=True)
    db.payments.create_index([("transaction_id", ASCENDING)], sparse=True)
    db.payments.create_index([("user_id", ASCENDING)])
    db.payments.create_index([("created_at", DESCENDING)])
    db.referrals.create_index([("referrer_id", ASCENDING)])
    db.referrals.create_index([("invited_user_id", ASCENDING)], unique=True)
    db.channels.create_index([("channel_id", ASCENDING)], unique=True)
    db.templates.create_index([("name", ASCENDING)], unique=True)
    db.balance_logs.create_index([("user_id", ASCENDING)])
    db.settings.create_index([("key", ASCENDING)], unique=True)


def ensure_default_settings() -> None:
    if db is None:
        return
    defaults = {
        "prices": {
            "premium_1d": 50,
            "premium_7d": 250,
            "premium_30d": 800,
            "name_emoji": 0,
            "logo_emoji": 0,
        },
        "texts": {
            "welcome": "👋 Xush kelibsiz!\n\nAsosiy menyudan kerakli bo‘limni tanlang.",
            "help": "ℹ️ Yordam\n\nSavollar uchun admin bilan bog‘laning.",
            "support": "@support",
        },
        "referral": {"reward_balance": 10, "reward_credit": 1},
        "premium_plans": [
            {"id": "1d", "title": "1 kun", "days": 1, "stars": 50, "price": 50},
            {"id": "7d", "title": "7 kun", "days": 7, "stars": 250, "price": 250},
            {"id": "30d", "title": "30 kun", "days": 30, "stars": 800, "price": 800},
        ],
        "channels": [],
        "bot_username": "",
    }
    for key, value in defaults.items():
        db.settings.update_one(
            {"key": key},
            {"$setOnInsert": {"key": key, "value": value}},
            upsert=True,
        )


def ensure_default_admin() -> None:
    if db is None:
        return
    for aid in ADMIN_IDS:
        db.admins.update_one(
            {"telegram_id": aid},
            {
                "$setOnInsert": {
                    "telegram_id": aid,
                    "role": "superadmin",
                    "added_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )


def get_setting(key: str, default: Any = None) -> Any:
    if db is None:
        return default
    doc = db.settings.find_one({"key": key})
    return doc["value"] if doc else default


def set_setting(key: str, value: Any) -> None:
    if db is None:
        return
    db.settings.update_one({"key": key}, {"$set": {"key": key, "value": value}}, upsert=True)


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def gen_referral_code(telegram_id: int) -> str:
    return f"ref{telegram_id}"


def get_or_create_user(
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    language: Optional[str] = None,
    referred_by: Optional[int] = None,
) -> Optional[dict]:
    """DB yo'q bo'lsa None qaytaradi (crash qilmaydi)."""
    if db is None:
        logger.warning("get_or_create_user: DB not connected")
        return None

    existing = db.users.find_one({"telegram_id": telegram_id})
    if existing:
        db.users.update_one(
            {"telegram_id": telegram_id},
            {
                "$set": {
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                    "language": language or existing.get("language"),
                    "last_activity": now_utc(),
                }
            },
        )
        return db.users.find_one({"telegram_id": telegram_id})

    code = gen_referral_code(telegram_id)
    doc = {
        "telegram_id": telegram_id,
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "language": language or "uz",
        "joined_at": now_utc(),
        "last_activity": now_utc(),
        "is_premium": False,
        "premium_until": None,
        "referral_code": code,
        "referred_by": None,
        "referral_count": 0,
        "balance": 0,
        "is_banned": False,
    }
    try:
        db.users.insert_one(doc)
    except DuplicateKeyError:
        return db.users.find_one({"telegram_id": telegram_id})

    if referred_by and referred_by != telegram_id:
        apply_referral(referred_by, telegram_id)
    return db.users.find_one({"telegram_id": telegram_id})


def get_user(telegram_id: int) -> Optional[dict]:
    if db is None:
        return None
    return db.users.find_one({"telegram_id": telegram_id})


def update_user(telegram_id: int, **fields) -> Optional[dict]:
    if db is None:
        return None
    fields["last_activity"] = now_utc()
    return db.users.find_one_and_update(
        {"telegram_id": telegram_id},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )


def is_admin(telegram_id: int) -> bool:
    if telegram_id in ADMIN_IDS:
        return True
    if db is None:
        return False
    return db.admins.find_one({"telegram_id": telegram_id}) is not None


def is_banned(telegram_id: int) -> bool:
    u = get_user(telegram_id)
    return bool(u and u.get("is_banned"))


def check_premium(telegram_id: int) -> bool:
    u = get_user(telegram_id)
    if not u:
        return False
    until = u.get("premium_until")
    if not until:
        if u.get("is_premium"):
            update_user(telegram_id, is_premium=False)
        return False
    if isinstance(until, str):
        try:
            until = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except Exception:
            return False
    if getattr(until, "tzinfo", None) is None:
        until = until.replace(tzinfo=timezone.utc)
    if until > now_utc():
        if not u.get("is_premium"):
            update_user(telegram_id, is_premium=True)
        return True
    update_user(telegram_id, is_premium=False, premium_until=None)
    return False


def add_balance(telegram_id: int, amount: int, reason: str = "") -> int:
    if db is None or amount == 0:
        u = get_user(telegram_id)
        return (u or {}).get("balance", 0)
    u = db.users.find_one_and_update(
        {"telegram_id": telegram_id},
        {"$inc": {"balance": amount}},
        return_document=ReturnDocument.AFTER,
    )
    db.balance_logs.insert_one(
        {
            "user_id": telegram_id,
            "amount": amount,
            "reason": reason,
            "created_at": now_utc(),
            "balance_after": (u or {}).get("balance", 0),
        }
    )
    return (u or {}).get("balance", 0)


def apply_referral(referrer_id: int, invited_id: int) -> bool:
    if db is None or referrer_id == invited_id:
        return False
    if db.referrals.find_one({"invited_user_id": invited_id}):
        return False
    reward_cfg = get_setting("referral", {"reward_balance": 10}) or {}
    reward = int(reward_cfg.get("reward_balance", 10))
    try:
        db.referrals.insert_one(
            {
                "referrer_id": referrer_id,
                "invited_user_id": invited_id,
                "reward": reward,
                "created_at": now_utc(),
            }
        )
    except DuplicateKeyError:
        return False
    db.users.update_one({"telegram_id": invited_id}, {"$set": {"referred_by": referrer_id}})
    db.users.update_one({"telegram_id": referrer_id}, {"$inc": {"referral_count": 1}})
    add_balance(referrer_id, reward, reason=f"referral:{invited_id}")
    return True


def grant_premium(telegram_id: int, days: int) -> None:
    until = now_utc() + timedelta(days=days)
    u = get_user(telegram_id)
    if u and u.get("premium_until"):
        cur = u["premium_until"]
        if isinstance(cur, str):
            try:
                cur = datetime.fromisoformat(cur.replace("Z", "+00:00"))
            except Exception:
                cur = None
        if cur and getattr(cur, "tzinfo", None) is None:
            cur = cur.replace(tzinfo=timezone.utc)
        if cur and cur > now_utc():
            until = cur + timedelta(days=days)
    update_user(telegram_id, is_premium=True, premium_until=until)


def revoke_premium(telegram_id: int) -> None:
    update_user(telegram_id, is_premium=False, premium_until=None)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

def get_required_channels() -> list:
    if db is None:
        return []
    return list(db.channels.find({"is_required": True}))


async def is_user_subscribed(bot_instance: Bot, user_id: int) -> bool:
    channels = get_required_channels()
    if not channels:
        return True
    for ch in channels:
        cid = ch.get("channel_id") or ch.get("channel_username")
        if not cid:
            continue
        try:
            member = await bot_instance.get_chat_member(cid, user_id)
            if member.status in ("left", "kicked"):
                return False
        except Exception as e:
            logger.warning("Channel check failed %s: %s", cid, e)
            continue
    return True


def subscribe_keyboard() -> InlineKeyboardMarkup:
    channels = get_required_channels()
    rows = []
    for i, ch in enumerate(channels, 1):
        uname = (ch.get("channel_username") or "").lstrip("@")
        if uname:
            label = f"📢 Obuna #{i}" if len(channels) > 1 else "📢 Kanalga o‘tish"
            rows.append([InlineKeyboardButton(text=label, url=f"https://t.me/{uname}")])
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Profil"), KeyboardButton(text="⭐ Premium")],
            [KeyboardButton(text="🎁 Referal"), KeyboardButton(text="💰 Balans")],
            [KeyboardButton(text="ℹ️ Yordam"), KeyboardButton(text="⚙️ Sozlamalar")],
        ],
        resize_keyboard=True,
    )


def premium_plans_kb() -> InlineKeyboardMarkup:
    plans = get_setting("premium_plans", []) or []
    rows = []
    for p in plans:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{p.get('title', p.get('id'))} — {p.get('price', 0)} ⭐",
                    callback_data=f"buy_prem:{p.get('id')}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# FSM + Filters
# ---------------------------------------------------------------------------

class Form(StatesGroup):
    waiting_broadcast = State()


class IsAdminFilter(BaseFilter):
    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        if user is None:
            return False
        return is_admin(user.id)


# ---------------------------------------------------------------------------
# Bot / Dispatcher
# ---------------------------------------------------------------------------

router = Router()
bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None


def create_bot_dp() -> tuple[Bot, Dispatcher]:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required. export BOT_TOKEN=...")
    b = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    d = Dispatcher(storage=MemoryStorage())
    d.include_router(router)
    return b, d


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    if not message.from_user:
        return
    uid = message.from_user.id
    if is_banned(uid):
        await message.answer("⛔ Siz bloklangansiz.")
        return

    referred_by = None
    if command.args:
        arg = command.args.strip()
        if arg.isdigit():
            referred_by = int(arg)
        elif arg.startswith("ref") and arg[3:].isdigit():
            referred_by = int(arg[3:])

    if db is None:
        await message.answer(
            "⚠️ Database ulanmagan. Admin MONGO_URI ni tekshirsin.\n"
            "Bot cheklangan rejimda ishlayapti."
        )
        return

    get_or_create_user(
        uid,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        language=message.from_user.language_code,
        referred_by=referred_by,
    )

    if not await is_user_subscribed(message.bot, uid):
        await message.answer(
            "📢 Botdan foydalanish uchun quyidagi kanallarga obuna bo‘ling:",
            reply_markup=subscribe_keyboard(),
        )
        return

    welcome = (get_setting("texts", {}) or {}).get("welcome") or "👋 Xush kelibsiz!"
    await message.answer(welcome, reply_markup=main_menu_kb())


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery):
    if not callback.from_user or not callback.message:
        return
    uid = callback.from_user.id
    if await is_user_subscribed(callback.bot, uid):
        try:
            await callback.message.edit_text("✅ Obuna tasdiqlandi!")
        except Exception:
            pass
        welcome = (get_setting("texts", {}) or {}).get("welcome") or "👋 Xush kelibsiz!"
        await callback.message.answer(welcome, reply_markup=main_menu_kb())
        await callback.answer()
    else:
        await callback.answer("❌ Hali barcha kanallarga obuna bo‘lmadingiz.", show_alert=True)


@router.message(F.text == "👤 Profil")
async def menu_profile(message: Message):
    if not message.from_user or is_banned(message.from_user.id):
        return
    u = get_user(message.from_user.id)
    if not u:
        await message.answer("Foydalanuvchi topilmadi. /start bosing.")
        return
    check_premium(message.from_user.id)
    u = get_user(message.from_user.id) or u
    prem = "✅ Ha" if u.get("is_premium") else "❌ Yo‘q"
    until = u.get("premium_until")
    until_s = (
        until.strftime("%Y-%m-%d %H:%M")
        if isinstance(until, datetime)
        else (str(until) if until else "—")
    )
    text = (
        f"👤 <b>Profil</b>\n\n"
        f"ID: <code>{u['telegram_id']}</code>\n"
        f"Username: @{u.get('username') or '—'}\n"
        f"Ism: {u.get('first_name') or '—'}\n"
        f"Premium: {prem}\n"
        f"Premium tugashi: {until_s}\n"
        f"Referallar: {u.get('referral_count', 0)}\n"
        f"Balans: {u.get('balance', 0)}"
    )
    await message.answer(text, reply_markup=main_menu_kb())


@router.message(F.text == "⭐ Premium")
async def menu_premium(message: Message):
    if not message.from_user or is_banned(message.from_user.id):
        return
    check_premium(message.from_user.id)
    u = get_user(message.from_user.id)
    status = "Faol" if u and u.get("is_premium") else "Yo‘q"
    await message.answer(
        f"⭐ <b>Premium</b>\n\nHolat: {status}\n\nTarifni tanlang:",
        reply_markup=premium_plans_kb(),
    )


@router.callback_query(F.data.startswith("buy_prem:"))
async def cb_buy_prem(callback: CallbackQuery):
    if not callback.from_user or not callback.message:
        return
    uid = callback.from_user.id
    if is_banned(uid):
        await callback.answer("Bloklangansiz", show_alert=True)
        return
    plan_id = callback.data.split(":", 1)[1]
    plans = get_setting("premium_plans", []) or []
    plan = next((p for p in plans if str(p.get("id")) == plan_id), None)
    if not plan:
        await callback.answer("Tarif topilmadi", show_alert=True)
        return
    price = int(plan.get("price", 0))
    u = get_user(uid)
    if not u:
        await callback.answer("Xato", show_alert=True)
        return
    if u.get("balance", 0) < price:
        await callback.answer(
            "Balans yetarli emas. Referal yoki admin orqali to‘ldiring.",
            show_alert=True,
        )
        return
    add_balance(uid, -price, reason=f"premium:{plan_id}")
    grant_premium(uid, int(plan.get("days", 1)))
    if db is not None:
        db.payments.insert_one(
            {
                "user_id": uid,
                "amount": price,
                "currency": "balance",
                "payment_method": "balance",
                "status": "approved",
                "created_at": now_utc(),
                "approved_at": now_utc(),
                "transaction_id": f"prem_{uid}_{int(time.time())}",
                "meta": {"plan": plan_id},
            }
        )
    try:
        await callback.message.edit_text(
            f"✅ Premium faollashtirildi: {plan.get('title')} ({plan.get('days')} kun)"
        )
    except Exception:
        await callback.message.answer(
            f"✅ Premium faollashtirildi: {plan.get('title')} ({plan.get('days')} kun)"
        )
    await callback.answer()


@router.message(F.text == "🎁 Referal")
async def menu_referral(message: Message):
    if not message.from_user or is_banned(message.from_user.id):
        return
    u = get_user(message.from_user.id)
    if not u:
        await message.answer("/start bosing.")
        return
    me = await message.bot.get_me()
    link = f"https://t.me/{me.username}?start={u['telegram_id']}"
    reward = (get_setting("referral", {}) or {}).get("reward_balance", 10)
    text = (
        f"🎁 <b>Referal tizimi</b>\n\n"
        f"Sizning linkingiz:\n<code>{link}</code>\n\n"
        f"Taklif qilganlar: <b>{u.get('referral_count', 0)}</b>\n"
        f"Har bir yangi user uchun: <b>+{reward}</b> balans"
    )
    await message.answer(text, reply_markup=main_menu_kb())


@router.message(F.text == "💰 Balans")
async def menu_balance(message: Message):
    if not message.from_user or is_banned(message.from_user.id):
        return
    u = get_user(message.from_user.id)
    bal = (u or {}).get("balance", 0)
    await message.answer(
        f"💰 <b>Balans</b>: {bal}\n\nReferal orqali to‘ldirish mumkin.",
        reply_markup=main_menu_kb(),
    )


@router.message(F.text == "ℹ️ Yordam")
async def menu_help(message: Message):
    texts = get_setting("texts", {}) or {}
    help_t = texts.get("help") or "Yordam"
    support = texts.get("support") or ""
    await message.answer(f"{help_t}\n\nSupport: {support}", reply_markup=main_menu_kb())


@router.message(F.text == "⚙️ Sozlamalar")
async def menu_settings_user(message: Message):
    await message.answer("⚙️ Sozlamalar.\nTil: o‘zbekcha", reply_markup=main_menu_kb())


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery):
    if callback.message:
        await callback.message.answer("Asosiy menyu:", reply_markup=main_menu_kb())
        try:
            await callback.message.delete()
        except Exception:
            pass
    await callback.answer()


# ---------- Admin commands ----------

@router.message(Command("admin"), IsAdminFilter())
async def cmd_admin(message: Message):
    text = (
        "🛠 <b>Admin panel</b>\n\n"
        "/stats — statistika\n"
        "/users — userlar soni\n"
        "/ban &lt;id&gt;\n"
        "/unban &lt;id&gt;\n"
        "/addpremium &lt;id&gt; &lt;days&gt;\n"
        "/delpremium &lt;id&gt;\n"
        "/addbalance &lt;id&gt; &lt;amount&gt;\n"
        "/channels — kanallar\n"
        "/broadcast — xabar yuborish\n"
        "/settings — sozlamalar\n\n"
        f"Web admin: {WEBHOOK_URL or 'http://localhost:' + str(PORT)}/admin"
    )
    await message.answer(text)


@router.message(Command("stats"), IsAdminFilter())
async def cmd_stats(message: Message):
    if db is None:
        await message.answer("DB yo‘q")
        return
    total = db.users.count_documents({})
    today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    today_n = db.users.count_documents({"joined_at": {"$gte": today}})
    prem = db.users.count_documents({"is_premium": True})
    banned = db.users.count_documents({"is_banned": True})
    refs = db.referrals.count_documents({})
    pays = db.payments.count_documents({})
    await message.answer(
        f"📊 <b>Statistika</b>\n\n"
        f"Userlar: {total}\nBugun: {today_n}\nPremium: {prem}\nBan: {banned}\n"
        f"Referallar: {refs}\nTo‘lovlar: {pays}"
    )


@router.message(Command("users"), IsAdminFilter())
async def cmd_users(message: Message):
    if db is None:
        await message.answer("DB yo‘q")
        return
    n = db.users.count_documents({})
    await message.answer(f"Jami userlar: {n}")


@router.message(Command("ban"), IsAdminFilter())
async def cmd_ban(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Ishlatish: /ban USER_ID")
        return
    tid = int(command.args.strip())
    update_user(tid, is_banned=True)
    await message.answer(f"🚫 {tid} ban qilindi.")


@router.message(Command("unban"), IsAdminFilter())
async def cmd_unban(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Ishlatish: /unban USER_ID")
        return
    tid = int(command.args.strip())
    update_user(tid, is_banned=False)
    await message.answer(f"✅ {tid} unban.")


@router.message(Command("addpremium"), IsAdminFilter())
async def cmd_addpremium(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await message.answer("/addpremium USER_ID DAYS")
        return
    grant_premium(int(parts[0]), int(parts[1]))
    await message.answer("✅ Premium berildi.")


@router.message(Command("delpremium"), IsAdminFilter())
async def cmd_delpremium(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        await message.answer("/delpremium USER_ID")
        return
    revoke_premium(int(command.args.strip()))
    await message.answer("✅ Premium olib tashlandi.")


@router.message(Command("addbalance"), IsAdminFilter())
async def cmd_addbalance(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer("/addbalance USER_ID AMOUNT")
        return
    try:
        amount = int(parts[1])
    except ValueError:
        await message.answer("Miqdor noto‘g‘ri")
        return
    bal = add_balance(int(parts[0]), amount, reason="admin")
    await message.answer(f"✅ Balans: {bal}")


@router.message(Command("channels"), IsAdminFilter())
async def cmd_channels(message: Message):
    chs = list(db.channels.find({})) if db is not None else []
    if not chs:
        await message.answer("Kanallar yo‘q. Web admin orqali qo‘shing.")
        return
    lines = [
        f"• {c.get('channel_username') or c.get('channel_id')} (required={c.get('is_required')})"
        for c in chs
    ]
    await message.answer("Kanallar:\n" + "\n".join(lines))


@router.message(Command("broadcast"), IsAdminFilter())
async def cmd_broadcast(message: Message, state: FSMContext):
    await state.set_state(Form.waiting_broadcast)
    await message.answer("Broadcast xabarini yuboring (text/photo/video...). Bekor: /cancel")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())


@router.message(Form.waiting_broadcast, IsAdminFilter())
async def process_broadcast(message: Message, state: FSMContext):
    await state.clear()
    if db is None:
        await message.answer("DB yo‘q")
        return
    users = db.users.find({"is_banned": {"$ne": True}}, {"telegram_id": 1})
    ok, fail = 0, 0
    status = await message.answer("Broadcast boshlandi...")
    for u in users:
        tid = u["telegram_id"]
        try:
            await message.copy_to(tid)
            ok += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            fail += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await message.copy_to(tid)
                ok += 1
            except Exception:
                fail += 1
        except Exception:
            fail += 1
        if (ok + fail) % 25 == 0:
            try:
                await status.edit_text(f"Yuborilmoqda... {ok} ok / {fail} fail")
            except Exception:
                pass
        await asyncio.sleep(0.05)
    try:
        await status.edit_text(f"✅ Broadcast tugadi.\nYuborildi: {ok}\nXato: {fail}")
    except Exception:
        await message.answer(f"✅ Broadcast tugadi.\nYuborildi: {ok}\nXato: {fail}")


@router.message(Command("settings"), IsAdminFilter())
async def cmd_settings(message: Message):
    await message.answer("Sozlamalarni web admin panel orqali boshqaring.")


@router.message()
async def fallback(message: Message):
    if message.from_user and is_banned(message.from_user.id):
        return
    await message.answer("Menyudan tanlang yoki /start bosing.", reply_markup=main_menu_kb())


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------

app = flask.Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

BASE_HTML = """
<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} — Admin</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
.nav{background:#1e293b;padding:12px 20px;display:flex;gap:16px;flex-wrap:wrap;align-items:center}
.nav a{color:#94a3b8;text-decoration:none} .nav a:hover{color:#fff}
.container{max-width:1100px;margin:24px auto;padding:0 16px}
.card{background:#1e293b;border-radius:12px;padding:20px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.stat{background:#334155;padding:16px;border-radius:10px;text-align:center}
.stat b{font-size:1.6rem;display:block}
table{width:100%;border-collapse:collapse} th,td{padding:8px;border-bottom:1px solid #334155;text-align:left}
input,select,textarea,button{padding:8px 12px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#e2e8f0}
button,.btn{background:#3b82f6;border:none;cursor:pointer;color:#fff;text-decoration:none;display:inline-block}
.btn-danger{background:#ef4444} .btn-success{background:#22c55e}
.flash{padding:10px;border-radius:8px;margin-bottom:12px;background:#334155}
.pagination a{margin-right:8px;color:#93c5fd}
</style>
</head>
<body>
<div class="nav">
  <strong>Admin</strong>
  <a href="{{ url_for('admin_dashboard') }}">Dashboard</a>
  <a href="{{ url_for('admin_users') }}">Users</a>
  <a href="{{ url_for('admin_payments') }}">Payments</a>
  <a href="{{ url_for('admin_referrals') }}">Referrals</a>
  <a href="{{ url_for('admin_channels') }}">Channels</a>
  <a href="{{ url_for('admin_settings') }}">Settings</a>
  <a href="{{ url_for('admin_broadcast') }}">Broadcast</a>
  <a href="{{ url_for('admin_logout') }}">Logout</a>
</div>
<div class="container">
  {% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}
  {{ body|safe }}
</div>
</body></html>
"""


def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not flask.session.get("admin_logged"):
            return flask.redirect(flask.url_for("admin_login"))
        return f(*args, **kwargs)

    return wrapped


@app.route("/health")
def health():
    return flask.jsonify(
        {
            "status": "ok" if db is not None else "degraded",
            "mongo": db is not None,
            "bot": bool(BOT_TOKEN),
            "mode": "webhook" if WEBHOOK_URL else "polling",
        }
    )


@app.route("/")
def index():
    return flask.jsonify({"bot": "running", "admin": "/admin", "health": "/health"})


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if flask.request.method == "POST":
        pwd = flask.request.form.get("password", "")
        if pwd == ADMIN_PASSWORD or (ADMIN_PASSWORD == "change_me_admin" and pwd == "admin"):
            flask.session["admin_logged"] = True
            return flask.redirect(flask.url_for("admin_dashboard"))
        flask.flash("Noto‘g‘ri parol")
    body = """
    <div class="card"><h2>Admin Login</h2>
    <form method="post"><input type="password" name="password" placeholder="Parol" required>
    <button type="submit">Kirish</button></form></div>
    """
    return flask.render_template_string(BASE_HTML, title="Login", body=body)


@app.route("/admin/logout")
def admin_logout():
    flask.session.clear()
    return flask.redirect(flask.url_for("admin_login"))


@app.route("/admin")
@app.route("/admin/dashboard")
@login_required
def admin_dashboard():
    if db is None:
        body = "<div class='card'>MongoDB ulanmagan</div>"
        return flask.render_template_string(BASE_HTML, title="Dashboard", body=body)
    total = db.users.count_documents({})
    today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    today_n = db.users.count_documents({"joined_at": {"$gte": today}})
    prem = db.users.count_documents({"is_premium": True})
    banned = db.users.count_documents({"is_banned": True})
    refs = db.referrals.count_documents({})
    pays = db.payments.count_documents({})
    body = f"""
    <h1>Dashboard</h1>
    <div class="grid">
      <div class="stat"><b>{total}</b>Userlar</div>
      <div class="stat"><b>{today_n}</b>Bugun</div>
      <div class="stat"><b>{prem}</b>Premium</div>
      <div class="stat"><b>{banned}</b>Ban</div>
      <div class="stat"><b>{refs}</b>Referrals</div>
      <div class="stat"><b>{pays}</b>Payments</div>
    </div>
    """
    return flask.render_template_string(BASE_HTML, title="Dashboard", body=body)


@app.route("/admin/users")
@login_required
def admin_users():
    if db is None:
        return flask.render_template_string(BASE_HTML, title="Users", body="<div class='card'>DB yo‘q</div>")
    page = max(1, int(flask.request.args.get("page", 1)))
    q = flask.request.args.get("q", "").strip()
    per = 20
    filt: dict = {}
    if q:
        if q.isdigit():
            filt = {"telegram_id": int(q)}
        else:
            filt = {"username": {"$regex": re.escape(q), "$options": "i"}}
    total = db.users.count_documents(filt)
    users = list(
        db.users.find(filt).sort("joined_at", DESCENDING).skip((page - 1) * per).limit(per)
    )
    rows = ""
    for u in users:
        rows += f"""<tr>
          <td>{u.get('telegram_id')}</td>
          <td>@{u.get('username') or '—'}</td>
          <td>{u.get('first_name') or ''}</td>
          <td>{u.get('balance',0)}</td>
          <td>{'⭐' if u.get('is_premium') else '—'}</td>
          <td>{'🚫' if u.get('is_banned') else '✅'}</td>
          <td>
            <a class="btn" href="{flask.url_for('admin_user_action', tid=u['telegram_id'], action='ban')}">Ban</a>
            <a class="btn btn-success" href="{flask.url_for('admin_user_action', tid=u['telegram_id'], action='unban')}">Unban</a>
          </td>
        </tr>"""
    pages = max(1, (total + per - 1) // per)
    pag = " ".join(
        f'<a href="?page={i}&q={q}">{i}</a>' for i in range(1, min(pages, 20) + 1)
    )
    body = f"""
    <h1>Users ({total})</h1>
    <form method="get"><input name="q" value="{q}" placeholder="ID yoki username"><button>Qidirish</button></form>
    <div class="card"><table>
    <tr><th>ID</th><th>Username</th><th>Ism</th><th>Balans</th><th>Prem</th><th>Status</th><th></th></tr>
    {rows or '<tr><td colspan=7>Bo‘sh</td></tr>'}
    </table>
    <div class="pagination">{pag}</div></div>
    """
    return flask.render_template_string(BASE_HTML, title="Users", body=body)


@app.route("/admin/users/<int:tid>/<action>")
@login_required
def admin_user_action(tid: int, action: str):
    if action == "ban":
        update_user(tid, is_banned=True)
        flask.flash(f"{tid} ban")
    elif action == "unban":
        update_user(tid, is_banned=False)
        flask.flash(f"{tid} unban")
    elif action == "delpremium":
        revoke_premium(tid)
        flask.flash("Premium olib tashlandi")
    return flask.redirect(flask.url_for("admin_users"))


@app.route("/admin/payments")
@login_required
def admin_payments():
    if db is None:
        return flask.render_template_string(BASE_HTML, title="Payments", body="DB yo‘q")
    page = max(1, int(flask.request.args.get("page", 1)))
    per = 20
    total = db.payments.count_documents({})
    items = list(
        db.payments.find().sort("created_at", DESCENDING).skip((page - 1) * per).limit(per)
    )
    rows = ""
    for p in items:
        rows += (
            f"<tr><td>{p.get('user_id')}</td><td>{p.get('amount')}</td>"
            f"<td>{p.get('currency')}</td><td>{p.get('status')}</td>"
            f"<td>{p.get('created_at')}</td></tr>"
        )
    body = f"""<h1>Payments ({total})</h1><div class="card"><table>
    <tr><th>User</th><th>Amount</th><th>Currency</th><th>Status</th><th>Date</th></tr>{rows}</table></div>"""
    return flask.render_template_string(BASE_HTML, title="Payments", body=body)


@app.route("/admin/referrals")
@login_required
def admin_referrals():
    if db is None:
        return flask.render_template_string(BASE_HTML, title="Referrals", body="DB yo‘q")
    items = list(db.referrals.find().sort("created_at", DESCENDING).limit(100))
    rows = "".join(
        f"<tr><td>{r.get('referrer_id')}</td><td>{r.get('invited_user_id')}</td>"
        f"<td>{r.get('reward')}</td><td>{r.get('created_at')}</td></tr>"
        for r in items
    )
    body = f"""<h1>Referrals</h1><div class="card"><table>
    <tr><th>Referrer</th><th>Invited</th><th>Reward</th><th>Date</th></tr>{rows}</table></div>"""
    return flask.render_template_string(BASE_HTML, title="Referrals", body=body)


@app.route("/admin/channels", methods=["GET", "POST"])
@login_required
def admin_channels():
    if db is None:
        return flask.render_template_string(BASE_HTML, title="Channels", body="DB yo‘q")
    if flask.request.method == "POST":
        uname = flask.request.form.get("username", "").strip().lstrip("@")
        cid = flask.request.form.get("channel_id", "").strip()
        required = flask.request.form.get("required") == "on"
        if uname or cid:
            doc = {
                "channel_id": cid or f"@{uname}",
                "channel_username": uname,
                "title": uname,
                "is_required": required,
                "added_at": now_utc(),
            }
            try:
                db.channels.update_one(
                    {"channel_id": doc["channel_id"]},
                    {"$set": doc},
                    upsert=True,
                )
                flask.flash("Kanal qo‘shildi/yangilandi")
            except Exception as e:
                flask.flash(str(e))
        return flask.redirect(flask.url_for("admin_channels"))
    chs = list(db.channels.find())
    rows = ""
    for c in chs:
        rows += f"""<tr>
          <td>{c.get('channel_username')}</td><td>{c.get('channel_id')}</td>
          <td>{c.get('is_required')}</td>
          <td><a class="btn btn-danger" href="{flask.url_for('admin_channel_del', cid=c.get('channel_id'))}">O‘chirish</a></td>
        </tr>"""
    body = f"""
    <h1>Channels</h1>
    <div class="card">
    <form method="post">
      Username: <input name="username" placeholder="channel">
      ID: <input name="channel_id" placeholder="-100...">
      Required <input type="checkbox" name="required" checked>
      <button type="submit">Qo‘shish</button>
    </form>
    <table><tr><th>Username</th><th>ID</th><th>Required</th><th></th></tr>{rows}</table>
    </div>"""
    return flask.render_template_string(BASE_HTML, title="Channels", body=body)


@app.route("/admin/channels/del/<path:cid>")
@login_required
def admin_channel_del(cid: str):
    if db is not None:
        db.channels.delete_one({"channel_id": cid})
        flask.flash("O‘chirildi")
    return flask.redirect(flask.url_for("admin_channels"))


@app.route("/admin/settings", methods=["GET", "POST"])
@login_required
def admin_settings():
    if flask.request.method == "POST":
        try:
            reward = int(flask.request.form.get("ref_reward", 10))
            set_setting("referral", {"reward_balance": reward, "reward_credit": 1})
            support = flask.request.form.get("support", "")
            texts = get_setting("texts", {}) or {}
            texts["support"] = support
            texts["welcome"] = flask.request.form.get("welcome", texts.get("welcome", ""))
            texts["help"] = flask.request.form.get("help", texts.get("help", ""))
            set_setting("texts", texts)
            flask.flash("Saqlangan")
        except Exception as e:
            flask.flash(str(e))
        return flask.redirect(flask.url_for("admin_settings"))
    ref = get_setting("referral", {}) or {}
    texts = get_setting("texts", {}) or {}
    body = f"""
    <h1>Settings</h1>
    <div class="card">
    <form method="post">
      Referal reward: <input name="ref_reward" value="{ref.get('reward_balance',10)}"><br><br>
      Support: <input name="support" value="{texts.get('support','')}"><br><br>
      Welcome:<br><textarea name="welcome" rows="3" cols="50">{texts.get('welcome','')}</textarea><br>
      Help:<br><textarea name="help" rows="3" cols="50">{texts.get('help','')}</textarea><br>
      <button type="submit">Saqlash</button>
    </form>
    </div>"""
    return flask.render_template_string(BASE_HTML, title="Settings", body=body)


@app.route("/admin/broadcast", methods=["GET", "POST"])
@login_required
def admin_broadcast():
    body = """
    <h1>Broadcast</h1>
    <div class="card">
    <p>Telegram orqali admin sifatida <b>/broadcast</b> buyrug‘ini ishlating.</p>
    </div>
    """
    return flask.render_template_string(BASE_HTML, title="Broadcast", body=body)


# Webhook — token path faqat token bo'lsa ro'yxatdan o'tadi
def _feed_update_sync(data: dict) -> None:
    global bot, dp
    if bot is None or dp is None:
        return
    update = Update.model_validate(data, context={"bot": bot})
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(dp.feed_update(bot, update))
    finally:
        loop.close()


@app.route("/webhook", methods=["POST"])
@app.route("/webhook/<path:token>", methods=["POST"])
def telegram_webhook(token: str = ""):
    if not BOT_TOKEN:
        return "no token", 503
    # Token yo'lda yoki queryda tekshiriladi
    if token and token != BOT_TOKEN:
        return "forbidden", 403
    if bot is None or dp is None:
        return "bot not ready", 503
    try:
        data = flask.request.get_json(force=True, silent=True)
        if not data:
            return "bad request", 400
        _feed_update_sync(data)
    except Exception as e:
        logger.exception("Webhook error: %s", e)
        return "error", 500
    return "ok"


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def _set_webhook() -> None:
    if not WEBHOOK_URL or not BOT_TOKEN or bot is None:
        return
    url = f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}"
    try:
        await bot.set_webhook(url, drop_pending_updates=True)
        logger.info("Webhook set: %s", url.replace(BOT_TOKEN, "***TOKEN***"))
    except Exception as e:
        logger.error("set_webhook failed: %s", e)


async def _delete_webhook() -> None:
    if bot is None:
        return
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted (polling mode)")
    except Exception as e:
        logger.warning("delete_webhook: %s", e)


async def _run_polling() -> None:
    assert bot is not None and dp is not None
    await _delete_webhook()
    logger.info("Starting long-polling...")
    await dp.start_polling(bot)


def _start_polling_thread() -> None:
    """Alohida thread + yangi event loop (Flask bilan birga)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_polling())
    except Exception as e:
        logger.exception("Polling stopped: %s", e)
    finally:
        loop.close()


def main() -> None:
    global bot, dp

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN majburiy! export BOT_TOKEN=your_token")
        # Flask baribir ishga tushadi (health/admin)
    else:
        bot, dp = create_bot_dp()

    connected = connect_mongo()
    if not connected:
        logger.warning("MongoDB yo'q — cheklangan rejim")

    use_webhook = bool(WEBHOOK_URL and BOT_TOKEN)

    if use_webhook and bot is not None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_set_webhook())
        finally:
            loop.close()
        logger.info("Webhook mode. Flask on 0.0.0.0:%s", PORT)
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
    elif BOT_TOKEN and bot is not None:
        # Local: polling + Flask parallel
        t = threading.Thread(target=_start_polling_thread, daemon=True)
        t.start()
        logger.info("Polling mode + Flask on 0.0.0.0:%s", PORT)
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
    else:
        logger.warning("BOT_TOKEN yo'q — faqat Flask")
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
