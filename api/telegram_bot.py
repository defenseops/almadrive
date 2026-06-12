from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import asyncio
import logging
import os
from datetime import datetime
from functools import wraps
from typing import List

import httpx
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("almadrive_bot")


# ====== ENV ======
BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
API_BASE_URL = (os.getenv("API_BASE_URL") or "").strip().rstrip("/")
ADMIN_USERNAME = (os.getenv("ADMIN_USERNAME") or "").strip()
ADMIN_PASSWORD = (os.getenv("ADMIN_PASSWORD") or "").strip()

# Telegram admins (comma separated IDs)
ADMINS = set()
for x in (os.getenv("TELEGRAM_ADMINS") or "").split(","):
    x = x.strip()
    if x.isdigit():
        ADMINS.add(int(x))


# ====== Bot states ======
SERVICE_PRICE_VALUE, CLASS_MULTIPLIER_VALUE = range(10, 12)


def _is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMINS)


def admin_only(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, **kwargs):
        if not _is_admin(update):
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Доступ запрещён (только админ).")
            elif update.callback_query:
                try:
                    await update.callback_query.answer("⛔ Доступ запрещён (только админ).", show_alert=True)
                except Exception:
                    pass
            return ConversationHandler.END if isinstance(update.callback_query, object) else None
        return await handler(update, context, **kwargs)

    return wrapper


def _api_auth():
    if not (ADMIN_USERNAME and ADMIN_PASSWORD):
        raise RuntimeError("ADMIN_USERNAME/ADMIN_PASSWORD are not configured for the bot")
    return (ADMIN_USERNAME, ADMIN_PASSWORD)


async def api_request(method: str, path: str, *, json: dict | None = None, params: dict | None = None):
    if not API_BASE_URL:
        raise RuntimeError("API_BASE_URL is not configured")

    url = f"{API_BASE_URL}{path}"
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        res = await client.request(method, url, auth=_api_auth(), json=json, params=params)
        if res.status_code == 204:
            return None
        if not res.is_success:
            text = res.text[:500]
            raise RuntimeError(f"API error {res.status_code}: {text}")
        return res.json()


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("💰 Тарифы"), KeyboardButton("📨 Заявки")],
            [KeyboardButton("📝 Отзывы о сервисе"), KeyboardButton("📋 Все отзывы сайта")],
            [KeyboardButton("❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def _booking_action_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧑 Мы Берём", callback_data=f"take_booking:{booking_id}"),
            InlineKeyboardButton("🚗 Наёмник", callback_data=f"freelancer_booking:{booking_id}"),
        ],
        [InlineKeyboardButton("📋 Открыть заявку", callback_data=f"open_booking:{booking_id}")],
    ])


async def safe_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, *, reply_markup=None):
    for attempt in range(3):
        try:
            return await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        except TimedOut:
            if attempt == 2:
                raise
            await asyncio.sleep(1.0 * (attempt + 1))


async def safe_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, *, reply_markup=None):
    msg = update.effective_message
    if not msg:
        return
    chat_id = msg.chat_id
    try:
        await msg.reply_text(text, reply_markup=reply_markup)
    except TimedOut:
        await safe_send(context, chat_id, text, reply_markup=reply_markup)


async def safe_edit_or_send(query, context: ContextTypes.DEFAULT_TYPE, text: str, *, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except (TimedOut, NetworkError, TelegramError) as e:
        logger.warning("edit_message_text failed (%s). Fallback to send_message.", type(e).__name__)
        chat_id = query.message.chat_id if query.message else query.from_user.id
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


def tariffs_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Показать тарифы", callback_data="tariffs_show")],
            [InlineKeyboardButton("💵 Изменить цену услуги", callback_data="tariffs_service_pick")],
            [InlineKeyboardButton("📈 Изменить коэффициент класса", callback_data="tariffs_class_pick")],
        ]
    )


def service_price_pick_keyboard(services: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for s in services[:30]:
        sid = s.get("id")
        name = s.get("name", "Услуга")
        price = s.get("price_from")
        price_text = f"{price} тг" if price is not None else "не задано"
        rows.append([InlineKeyboardButton(f"{name[:40]} — {price_text}", callback_data=f"set_srv_price:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu_tariffs")])
    return InlineKeyboardMarkup(rows)


def class_multiplier_pick_keyboard(classes: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for c in classes[:30]:
        cid = c.get("id")
        name = c.get("name", "Класс")
        mult = c.get("price_multiplier", 100)
        rows.append([InlineKeyboardButton(f"{name[:40]} — {mult}%", callback_data=f"set_cls_mult:{cid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu_tariffs")])
    return InlineKeyboardMarkup(rows)


def _published_reviews_nav_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"pub_rev_page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️ Вперёд", callback_data=f"pub_rev_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="pub_rev_menu")])
    return InlineKeyboardMarkup(rows)


def _service_review_keyboard(review_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Одобрить", callback_data=f"srv_appr:{review_id}"),
            InlineKeyboardButton("🙈 Скрыть", callback_data=f"srv_hide:{review_id}"),
            InlineKeyboardButton("🗑️ Удалить", callback_data=f"srv_del:{review_id}"),
        ]]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(
        update,
        context,
        "🚗 AlmaDrive Bot\n\nВыберите действие:",
        reply_markup=main_menu_keyboard(),
    )


@admin_only
async def tariffs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(
        update,
        context,
        "💰 Управление тарифами",
        reply_markup=tariffs_menu_keyboard(),
    )


@admin_only
async def tariffs_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        services = await api_request("GET", "/api/admin/services")
        classes = await api_request("GET", "/api/admin/vehicle-classes")
    except Exception as e:
        await safe_reply(update, context, f"❌ Ошибка загрузки тарифов: {e}")
        return

    lines = ["💰 Текущие тарифы\n", "Услуги:"]
    for s in services:
        price = s.get("price_from")
        price_text = f"{price} тг" if price is not None else "не задано"
        lines.append(f"#{s.get('id')} — {s.get('name')} — {price_text}")

    lines.append("\nКлассы авто:")
    for c in classes:
        mult = c.get("price_multiplier", 100)
        lines.append(f"#{c.get('id')} — {c.get('name')} — {mult}%")

    await safe_reply(update, context, "\n".join(lines), reply_markup=tariffs_menu_keyboard())


async def tariffs_show_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass

    if not _is_admin(update):
        try:
            await query.answer("⛔ Только админ", show_alert=True)
        except Exception:
            pass
        return

    try:
        services = await api_request("GET", "/api/admin/services")
        classes = await api_request("GET", "/api/admin/vehicle-classes")
    except Exception as e:
        logger.exception("tariffs_show_callback failed")
        await safe_edit_or_send(query, context, f"❌ Ошибка загрузки тарифов: {e}")
        return

    lines = ["💰 Текущие тарифы\n", "Услуги:"]
    for s in services:
        price = s.get("price_from")
        price_text = f"{price} тг" if price is not None else "не задано"
        lines.append(f"#{s.get('id')} — {s.get('name')} — {price_text}")

    lines.append("\nКлассы авто:")
    for c in classes:
        mult = c.get("price_multiplier", 100)
        lines.append(f"#{c.get('id')} — {c.get('name')} — {mult}%")

    await safe_edit_or_send(query, context, "\n".join(lines), reply_markup=tariffs_menu_keyboard())


async def tariffs_service_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    logger.info(
        "tariffs_service_pick_callback clicked by user_id=%s username=%s data=%s",
        update.effective_user.id if update.effective_user else None,
        update.effective_user.username if update.effective_user else None,
        query.data,
    )

    try:
        await query.answer()
    except Exception:
        pass

    if not _is_admin(update):
        logger.warning("tariffs_service_pick_callback denied for user_id=%s", update.effective_user.id if update.effective_user else None)
        try:
            await query.answer("⛔ Только админ", show_alert=True)
        except Exception:
            pass
        return

    try:
        services = await api_request("GET", "/api/admin/services")
        logger.info("Loaded %s services for user_id=%s", len(services or []), update.effective_user.id if update.effective_user else None)
    except Exception as e:
        logger.exception("Error loading services")
        await safe_edit_or_send(query, context, f"❌ Ошибка загрузки услуг: {e}")
        return

    await safe_edit_or_send(
        query,
        context,
        "Выберите услугу, для которой хотите изменить базовую цену:",
        reply_markup=service_price_pick_keyboard(services),
    )


async def tariffs_class_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    logger.info(
        "tariffs_class_pick_callback clicked by user_id=%s username=%s data=%s",
        update.effective_user.id if update.effective_user else None,
        update.effective_user.username if update.effective_user else None,
        query.data,
    )

    try:
        await query.answer()
    except Exception:
        pass

    if not _is_admin(update):
        logger.warning("tariffs_class_pick_callback denied for user_id=%s", update.effective_user.id if update.effective_user else None)
        try:
            await query.answer("⛔ Только админ", show_alert=True)
        except Exception:
            pass
        return

    try:
        classes = await api_request("GET", "/api/admin/vehicle-classes")
        logger.info("Loaded %s classes for user_id=%s", len(classes or []), update.effective_user.id if update.effective_user else None)
    except Exception as e:
        logger.exception("Error loading classes")
        await safe_edit_or_send(query, context, f"❌ Ошибка загрузки классов: {e}")
        return

    await safe_edit_or_send(
        query,
        context,
        "Выберите класс авто, для которого хотите изменить коэффициент:",
        reply_markup=class_multiplier_pick_keyboard(classes),
    )


async def select_service_price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return ConversationHandler.END

    logger.info(
        "select_service_price_callback clicked by user_id=%s username=%s data=%s",
        update.effective_user.id if update.effective_user else None,
        update.effective_user.username if update.effective_user else None,
        query.data,
    )

    try:
        await query.answer()
    except Exception:
        pass

    if not _is_admin(update):
        logger.warning("select_service_price_callback denied for user_id=%s", update.effective_user.id if update.effective_user else None)
        try:
            await query.answer("⛔ Только админ", show_alert=True)
        except Exception:
            pass
        return ConversationHandler.END

    try:
        _, service_id_s = (query.data or "").split(":", 1)
        service_id = int(service_id_s)
    except Exception:
        logger.exception("Bad service callback data: %s", query.data)
        await safe_edit_or_send(query, context, "❌ Некорректный выбор услуги.")
        return ConversationHandler.END

    try:
        services = await api_request("GET", "/api/admin/services")
        service = next((s for s in services if int(s.get("id")) == service_id), None)
    except Exception as e:
        logger.exception("Error loading service list")
        await safe_edit_or_send(query, context, f"❌ Ошибка загрузки услуги: {e}")
        return ConversationHandler.END

    if not service:
        logger.warning("Service not found: %s", service_id)
        await safe_edit_or_send(query, context, "❌ Услуга не найдена.")
        return ConversationHandler.END

    context.user_data["selected_service_id"] = service_id
    context.user_data["selected_service_name"] = service.get("name", "Услуга")

    logger.info(
        "Service selected user_id=%s service_id=%s",
        update.effective_user.id if update.effective_user else None,
        service_id,
    )

    await safe_edit_or_send(
        query,
        context,
        f"Введите новую базовую цену для услуги:\n\n{service.get('name')}\n\nТекущая цена: {service.get('price_from', 'не задано')} тг"
    )
    return SERVICE_PRICE_VALUE


async def process_service_price_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await safe_reply(update, context, "Введите цену числом, например: 9000")
        return SERVICE_PRICE_VALUE

    service_id = context.user_data.get("selected_service_id")
    service_name = context.user_data.get("selected_service_name", "Услуга")
    if not service_id:
        await safe_reply(update, context, "❌ Не выбрана услуга.")
        return ConversationHandler.END

    price = int(text)

    try:
        await api_request("PUT", f"/api/admin/services/{service_id}/price", json={"price_from": price})
        await safe_reply(
            update,
            context,
            f"✅ Цена обновлена:\n{service_name} — {price} тг",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        await safe_reply(update, context, f"❌ Ошибка обновления цены: {e}", reply_markup=main_menu_keyboard())

    context.user_data.pop("selected_service_id", None)
    context.user_data.pop("selected_service_name", None)
    return ConversationHandler.END


async def select_class_multiplier_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return ConversationHandler.END

    logger.info(
        "select_class_multiplier_callback clicked by user_id=%s username=%s data=%s",
        update.effective_user.id if update.effective_user else None,
        update.effective_user.username if update.effective_user else None,
        query.data,
    )

    try:
        await query.answer()
    except Exception:
        pass

    if not _is_admin(update):
        logger.warning("select_class_multiplier_callback denied for user_id=%s", update.effective_user.id if update.effective_user else None)
        try:
            await query.answer("⛔ Только админ", show_alert=True)
        except Exception:
            pass
        return ConversationHandler.END

    try:
        _, class_id_s = (query.data or "").split(":", 1)
        class_id = int(class_id_s)
    except Exception:
        logger.exception("Bad class callback data: %s", query.data)
        await safe_edit_or_send(query, context, "❌ Некорректный выбор класса.")
        return ConversationHandler.END

    try:
        classes = await api_request("GET", "/api/admin/vehicle-classes")
        vehicle_class = next((c for c in classes if int(c.get("id")) == class_id), None)
    except Exception as e:
        logger.exception("Error loading class list")
        await safe_edit_or_send(query, context, f"❌ Ошибка загрузки класса: {e}")
        return ConversationHandler.END

    if not vehicle_class:
        logger.warning("Class not found: %s", class_id)
        await safe_edit_or_send(query, context, "❌ Класс не найден.")
        return ConversationHandler.END

    context.user_data["selected_class_id"] = class_id
    context.user_data["selected_class_name"] = vehicle_class.get("name", "Класс")

    logger.info(
        "Class selected user_id=%s class_id=%s",
        update.effective_user.id if update.effective_user else None,
        class_id,
    )

    await safe_edit_or_send(
        query,
        context,
        f"Введите новый коэффициент для класса:\n\n{vehicle_class.get('name')}\n\n"
        f"Текущий коэффициент: {vehicle_class.get('price_multiplier', 100)}%\n\n"
        f"Примеры:\n100 = обычная цена\n150 = +50%\n130 = +30%"
    )
    return CLASS_MULTIPLIER_VALUE


async def process_class_multiplier_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await safe_reply(update, context, "Введите коэффициент числом, например: 150")
        return CLASS_MULTIPLIER_VALUE

    class_id = context.user_data.get("selected_class_id")
    class_name = context.user_data.get("selected_class_name", "Класс")
    if not class_id:
        await safe_reply(update, context, "❌ Не выбран класс.")
        return ConversationHandler.END

    multiplier = int(text)

    try:
        await api_request(
            "PUT",
            f"/api/admin/vehicle-classes/{class_id}/multiplier",
            json={"price_multiplier": multiplier},
        )
        await safe_reply(
            update,
            context,
            f"✅ Коэффициент обновлён:\n{class_name} — {multiplier}%",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        await safe_reply(update, context, f"❌ Ошибка обновления коэффициента: {e}", reply_markup=main_menu_keyboard())

    context.user_data.pop("selected_class_id", None)
    context.user_data.pop("selected_class_name", None)
    return ConversationHandler.END


@admin_only
async def service_reviews_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = await api_request("GET", "/api/service-reviews/pending", params={"limit": "20"})
    except Exception as e:
        await safe_reply(update, context, f"❌ Ошибка запроса: {e}")
        return

    if not items:
        await safe_reply(update, context, "✅ Нет отзывов о сервисе на модерацию.", reply_markup=main_menu_keyboard())
        return

    await safe_reply(update, context, f"🛡️ Отзывы о сервисе на модерации: {len(items)}", reply_markup=main_menu_keyboard())

    for r in items:
        rid = r.get("id")
        if rid is None:
            continue

        rating = r.get("rating")
        author = r.get("author_name") or "Аноним"
        text = (r.get("text") or "").strip()
        short = text if len(text) <= 600 else (text[:600] + "…")

        msg = (
            f"🆕 Отзыв о сервисе #{rid}\n"
            f"⭐ Оценка: {rating}/5\n"
            f"👤 Автор: {author}\n\n"
            f"{short}"
        )
        await safe_reply(update, context, msg, reply_markup=_service_review_keyboard(int(rid)))


async def service_review_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    try:
        await query.answer("⏳", show_alert=False)
    except Exception:
        pass

    if not _is_admin(update):
        try:
            await query.answer("⛔ Только админ", show_alert=True)
        except Exception:
            pass
        return

    data = query.data or ""
    try:
        action, rid_s = data.split(":", 1)
        rid = int(rid_s)
    except Exception:
        return

    try:
        if action == "srv_appr":
            await api_request("PUT", f"/api/service-reviews/{rid}/approve")
            await safe_edit_or_send(query, context, f"✅ Отзыв о сервисе #{rid} одобрен")
        elif action == "srv_hide":
            await api_request("PUT", f"/api/service-reviews/{rid}/hide")
            await safe_edit_or_send(query, context, f"🙈 Отзыв о сервисе #{rid} скрыт")
        elif action == "srv_del":
            await api_request("DELETE", f"/api/service-reviews/{rid}")
            await safe_edit_or_send(query, context, f"🗑️ Отзыв о сервисе #{rid} удалён")
    except Exception as e:
        await safe_edit_or_send(query, context, f"❌ Ошибка: {e}")


_PUB_REV_PAGE_SIZE = 5


def _format_published_review(r: dict, index: int, total: int) -> str:
    rid = r.get("id", "?")
    rating = r.get("rating")
    author = (r.get("author_name") or "Аноним").strip()
    text = (r.get("text") or "").strip()
    short = text if len(text) <= 500 else (text[:500] + "…")
    stars = "⭐" * int(rating) if rating else "—"
    return (
        f"[{index}/{total}] Отзыв #{rid}\n"
        f"{stars} {rating}/5\n"
        f"👤 {author}\n\n"
        f"{short}"
    )


@admin_only
async def published_reviews_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    try:
        items = await api_request("GET", "/api/service-reviews", params={"approved_only": "true", "limit": "100"})
    except Exception as e:
        await safe_reply(update, context, f"❌ Ошибка запроса: {e}", reply_markup=main_menu_keyboard())
        return

    if not items:
        await safe_reply(update, context, "ℹ️ На сайте пока нет опубликованных отзывов.", reply_markup=main_menu_keyboard())
        return

    total = len(items)
    total_pages = (total + _PUB_REV_PAGE_SIZE - 1) // _PUB_REV_PAGE_SIZE
    page = max(0, min(page, total_pages - 1))

    start = page * _PUB_REV_PAGE_SIZE
    batch = items[start:start + _PUB_REV_PAGE_SIZE]

    header = f"📋 Опубликованные отзывы на сайте\nВсего: {total} | Страница {page + 1}/{total_pages}\n{'─' * 30}"
    await safe_reply(update, context, header, reply_markup=main_menu_keyboard())

    for i, r in enumerate(batch, start=start + 1):
        msg = _format_published_review(r, i, total)
        nav_kb = _published_reviews_nav_keyboard(page, total_pages) if i == start + len(batch) else None
        await safe_reply(update, context, msg, reply_markup=nav_kb)


async def published_reviews_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass

    if not _is_admin(update):
        try:
            await query.answer("⛔ Только админ", show_alert=True)
        except Exception:
            pass
        return

    data = query.data or ""
    if data == "pub_rev_menu":
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Главное меню",
            reply_markup=main_menu_keyboard(),
        )
        return

    try:
        _, page_s = data.split(":", 1)
        page = int(page_s)
    except Exception:
        return

    # Delete the navigation message and show next page
    try:
        await query.message.delete()
    except Exception:
        pass

    class _FakeUpdate:
        def __init__(self, original_update):
            self._u = original_update
        @property
        def effective_message(self):
            return self._u.effective_message
        @property
        def effective_user(self):
            return self._u.effective_user
        @property
        def callback_query(self):
            return None

    fake = _FakeUpdate(update)
    await published_reviews_list(fake, context, page=page)


@admin_only
async def menu_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        bookings = await api_request("GET", "/api/admin/booking-requests", params={"status": "new", "limit": "20"})
    except Exception as e:
        await safe_reply(update, context, f"❌ Ошибка загрузки заявок: {e}")
        return

    if not bookings:
        await safe_reply(update, context, "✅ Нет новых заявок.", reply_markup=main_menu_keyboard())
        return

    await safe_reply(update, context, f"📨 Новые заявки: {len(bookings)}", reply_markup=main_menu_keyboard())

    for b in bookings:
        bid = b.get("id")
        service_name = b.get("service_name") or "?"
        vehicle_class_name = b.get("vehicle_class_name") or "?"
        service_date = b.get("service_date") or "?"
        try:
            service_date = datetime.fromisoformat(service_date).strftime("%d.%m.%Y %H:%M")
        except Exception:
            pass
        contact = b.get("contact") or "?"
        comment = (b.get("comment") or "—")[:300]
        price = b.get("estimated_price")
        price_text = f"{price:,} тг".replace(",", " ") if price else "не рассчитана"

        msg = (
            f"🆕 Заявка #{bid}\n"
            f"🛎 {service_name}\n"
            f"🚘 {vehicle_class_name}\n"
            f"📅 {service_date}\n"
            f"💲 Примерная: {price_text}\n"
            f"📞 {contact}\n"
            f"💬 {comment}"
        )
        await safe_reply(update, context, msg, reply_markup=_booking_action_keyboard(int(bid)))


async def booking_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    try:
        await query.answer("⏳", show_alert=False)
    except Exception:
        pass

    if not _is_admin(update):
        try:
            await query.answer("⛔ Только админ", show_alert=True)
        except Exception:
            pass
        return

    data = query.data or ""
    try:
        action, bid_s = data.split(":", 1)
        bid = int(bid_s)
    except Exception:
        return

    if action == "take_booking":
        try:
            await api_request("PUT", f"/api/admin/booking-requests/{bid}/status", json={"status": "accepted"})
            await safe_edit_or_send(query, context, f"✅ Заявка #{bid} принята — везём сами")
        except Exception as e:
            await safe_edit_or_send(query, context, f"❌ Ошибка: {e}")

    elif action == "freelancer_booking":
        try:
            await api_request("PUT", f"/api/admin/booking-requests/{bid}/status", json={"status": "freelancer"})
            await safe_edit_or_send(query, context, f"🚗 Заявка #{bid} передана наёмнику")
        except Exception as e:
            await safe_edit_or_send(query, context, f"❌ Ошибка: {e}")

    elif action == "open_booking":
        try:
            b = await api_request("GET", f"/api/admin/booking-requests/{bid}")
            service_name = b.get("service_name") or "?"
            vehicle_class_name = b.get("vehicle_class_name") or "?"
            service_date = b.get("service_date") or "?"
            try:
                service_date = datetime.fromisoformat(service_date).strftime("%d.%m.%Y %H:%M")
            except Exception:
                pass
            contact = b.get("contact") or "?"
            comment = (b.get("comment") or "—")
            price = b.get("estimated_price")
            price_text = f"{price:,} тг".replace(",", " ") if price else "не рассчитана"
            bstatus_map = {
                "new": "🆕 Новая",
                "accepted": "✅ Принята",
                "freelancer": "🚗 Наёмник",
                "cancelled": "❌ Отменена",
                "completed": "✔️ Завершена",
            }
            bstatus = bstatus_map.get(b.get("status", ""), b.get("status", "?"))

            msg = (
                f"📋 Заявка #{bid} — {bstatus}\n\n"
                f"🛎 {service_name}\n"
                f"🚘 {vehicle_class_name}\n"
                f"📅 {service_date}\n"
                f"💲 Примерная: {price_text}\n"
                f"📞 {contact}\n"
                f"💬 {comment}"
            )
            await safe_edit_or_send(query, context, msg, reply_markup=_booking_action_keyboard(bid))
        except Exception as e:
            await safe_edit_or_send(query, context, f"❌ Ошибка загрузки заявки: {e}")


async def menu_buttons_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()

    if text == "💰 Тарифы":
        return await tariffs_menu(update, context)
    if text == "📝 Отзывы о сервисе":
        return await service_reviews_pending(update, context)
    if text == "📋 Все отзывы сайта":
        return await published_reviews_list(update, context, page=0)
    if text == "📨 Заявки":
        return await menu_bookings(update, context)
    if text == "❌ Отмена":
        return await cancel(update, context)

    await safe_reply(update, context, "Выберите действие через кнопки или /start", reply_markup=main_menu_keyboard())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await safe_reply(update, context, "Отменено.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, context, "Неизвестная команда. Нажмите /start", reply_markup=main_menu_keyboard())


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    if not API_BASE_URL:
        raise RuntimeError("API_BASE_URL is required (e.g. https://your-domain.kz)")
    if not ADMINS:
        logger.warning("⚠️ TELEGRAM_ADMINS is empty. Nobody will have admin access in the bot.")

    request = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=10.0,
    )
    app = Application.builder().token(BOT_TOKEN).request(request).build()
# DEBUG: ловим ВСЕ callback'и
    async def debug_all_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.callback_query:
            logger.warning(f"🔥 ANY CALLBACK: {update.callback_query.data}")

    app.add_handler(CallbackQueryHandler(debug_all_callbacks), group=1)

    app.add_error_handler(on_error)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("service_reviews_pending", service_reviews_pending))
    app.add_handler(CommandHandler("tariffs", tariffs_show))

    # ConversationHandler для редактирования тарифов (price_conv регистрируем до кнопочного меню)
    price_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(select_service_price_callback, pattern=r"^set_srv_price:\d+$"),
            CallbackQueryHandler(select_class_multiplier_callback, pattern=r"^set_cls_mult:\d+$"),
        ],
        states={
            SERVICE_PRICE_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_service_price_value)],
            CLASS_MULTIPLIER_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_class_multiplier_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
    )

    app.add_handler(price_conv)

    # Кнопочное меню
    app.add_handler(MessageHandler(
        filters.Regex(r"^(💰 Тарифы|📝 Отзывы о сервисе|📋 Все отзывы сайта|📨 Заявки|❌ Отмена)$"),
        menu_buttons_handler
    ))

    # Inline callback-и
    app.add_handler(CallbackQueryHandler(booking_action_callback, pattern=r"^(take_booking|freelancer_booking|open_booking):\d+$"))
    app.add_handler(CallbackQueryHandler(published_reviews_page_callback, pattern=r"^pub_rev_page:\d+$"))
    app.add_handler(CallbackQueryHandler(published_reviews_page_callback, pattern=r"^pub_rev_menu$"))
    app.add_handler(CallbackQueryHandler(service_review_action_callback, pattern=r"^srv_(appr|hide|del):"))
    app.add_handler(CallbackQueryHandler(tariffs_show_callback, pattern=r"^tariffs_show$"))
    app.add_handler(CallbackQueryHandler(tariffs_service_pick_callback, pattern=r"^tariffs_service_pick$"))
    app.add_handler(CallbackQueryHandler(tariffs_class_pick_callback, pattern=r"^tariffs_class_pick$"))
    app.add_handler(CallbackQueryHandler(tariffs_show_callback, pattern=r"^menu_tariffs$"))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("🤖 Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
