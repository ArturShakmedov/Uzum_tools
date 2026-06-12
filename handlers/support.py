"""Техподдержка через форум-топики супергруппы (двусторонний мост).

Пользователь → бот: «🆘 Техподдержка» → FSM → сообщение копируется в персональный
топик супергруппы поддержки (создаётся при первом обращении).
Админ → пользователь: ответ в топике копируется обратно в личку юзеру.

Требования к группе: SUPPORT_CHAT_ID — супергруппа с топиками (Forum), бот — админ
с правом «Manage Topics», Privacy Mode = OFF (иначе бот не видит ответы админов).
Роутер открыт всем (не под SubscriptionMiddleware).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from config import SUPPORT_CHAT_ID
from database.connection import session_scope
from database.repository import (
    create_support_ticket,
    get_support_ticket,
    get_ticket_user_by_topic,
)
from keyboards.menu import BTN_SUPPORT, main_menu_kb
from utils.logger import get_logger

log = get_logger(__name__)
router = Router(name="support")

# Кнопка выхода из режима чата с поддержкой (показывается, пока активен диалог).
BTN_SUPPORT_EXIT = "❌ Выйти из чата поддержки"


class SupportState(StatesGroup):
    waiting_for_message = State()


def _support_kb() -> ReplyKeyboardMarkup:
    """Клавиатура режима чата: единственная кнопка выхода."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_SUPPORT_EXIT)]],
        resize_keyboard=True,
        input_field_placeholder="Опишите проблему — мы на связи…",
    )


# --------------------------------------------------------------------------- #
#  Вход / выход из режима чата поддержки
# --------------------------------------------------------------------------- #
@router.message(F.text == BTN_SUPPORT)
async def on_support_button(message: Message, state: FSMContext) -> None:
    """Открыть непрерывный чат с поддержкой и удерживать состояние."""
    await state.set_state(SupportState.waiting_for_message)
    await message.answer(
        "🆘 <b>Техподдержка</b>\n\n"
        "Напишите ваш вопрос, и наши специалисты ответят вам прямо здесь. "
        "Можно отправить несколько сообщений подряд.\n\n"
        "Когда закончите — нажмите «❌ Выйти из чата поддержки».",
        reply_markup=_support_kb(),
    )


@router.message(SupportState.waiting_for_message, F.text == BTN_SUPPORT_EXIT)
async def on_exit_support(message: Message, state: FSMContext) -> None:
    """Выйти из чата поддержки и вернуть главное меню."""
    await state.clear()
    await message.answer(
        "Вы вышли из чата с поддержкой. Возвращаю вас в главное меню.",
        reply_markup=main_menu_kb(),
    )


def _get_topic_id(telegram_id: int) -> int | None:
    with session_scope() as session:
        ticket = get_support_ticket(session, telegram_id)
        return ticket.topic_id if ticket else None


def _save_topic(telegram_id: int, topic_id: int) -> None:
    with session_scope() as session:
        create_support_ticket(session, telegram_id, topic_id)


@router.message(SupportState.waiting_for_message)
async def on_user_support_message(message: Message, state: FSMContext) -> None:
    """Транслировать сообщение в топик. Состояние НЕ сбрасываем — чат продолжается."""
    # Подстраховка: кнопку выхода не пересылаем в админ-топик (её ловит on_exit_support,
    # но если порядок хендлеров изменится — текст не утечёт в поддержку).
    if message.text == BTN_SUPPORT_EXIT:
        await on_exit_support(message, state)
        return

    if not SUPPORT_CHAT_ID:
        await message.answer(
            "⚠️ Техподдержка временно недоступна. Попробуйте позже.",
            reply_markup=main_menu_kb(),
        )
        await state.clear()
        log.warning("SUPPORT_CHAT_ID не настроен — обращение не отправлено.")
        return

    user = message.from_user
    topic_id = await asyncio.to_thread(_get_topic_id, user.id)

    # Топика ещё нет → создаём персональную тему в супергруппе поддержки.
    if topic_id is None:
        # Компактное имя темы (≤128 симв.): только имя + ID, без юзернейма/мусора.
        try:
            topic = await message.bot.create_forum_topic(
                chat_id=SUPPORT_CHAT_ID,
                name=f"🎫 {user.first_name} (ID: {user.id})",
            )
        except Exception as exc:  # noqa: BLE001 — диагностика причины падения
            log.error("=== КРИТИЧЕСКАЯ ОШИБКА ТЕХПОДДЕРЖКИ ===")
            log.error("Пытались создать топик в чате ID: %s", SUPPORT_CHAT_ID)
            log.error("Тип ошибки: %s", type(exc).__name__)
            log.error("Детали ошибки от Telegram API: %s", str(exc))
            log.exception(exc)
            await message.answer(
                "⚠️ Не удалось создать обращение. Попробуйте позже.",
                reply_markup=main_menu_kb(),
            )
            await state.clear()
            return
        topic_id = topic.message_thread_id
        await asyncio.to_thread(_save_topic, user.id, topic_id)

        # Карточка-анкета юзера в шапку топика (детали вынесены из названия).
        full_name = escape(f"{user.first_name} {user.last_name or ''}".strip())
        username = f"@{escape(user.username)}" if user.username else "нет"
        created = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
        card_text = (
            "🎫 <b>Новое обращение в поддержку</b>\n"
            "───────────────────\n"
            f"👤 <b>Имя:</b> {full_name}\n"
            f"🆔 <b>Telegram ID:</b> <code>{user.id}</code>\n"
            f"🔗 <b>Юзернейм:</b> {username}\n"
            f"📅 <b>Дата обращения:</b> {created}\n"
            "───────────────────\n"
            "<i>Ответьте на это сообщение или пишите в этот топик, чтобы "
            "отправить ответ пользователю.</i>"
        )
        try:
            card = await message.bot.send_message(
                chat_id=SUPPORT_CHAT_ID,
                message_thread_id=topic_id,
                text=card_text,
            )
            # Закрепляем анкету в шапке темы (без шумного уведомления).
            await message.bot.pin_chat_message(
                chat_id=SUPPORT_CHAT_ID,
                message_id=card.message_id,
                disable_notification=True,
            )
        except Exception as exc:  # noqa: BLE001 — нет права pin / прочие сбои не критичны
            log.warning("Карточка/закрепление в топике %s: %s", topic_id, exc)

    # Копируем сообщение юзера в топик (текст/фото/документ — любой контент).
    try:
        await message.copy_to(chat_id=SUPPORT_CHAT_ID, message_thread_id=topic_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("copy_to в топик %s упал: %s", topic_id, exc)
        await message.answer(
            "⚠️ Не удалось доставить сообщение в поддержку. Попробуйте позже."
        )
        return

    # Состояние удерживаем (чат-комната): следующее сообщение снова уйдёт в топик.
    await message.answer("✅ Отправлено в поддержку. Ожидайте ответа специалиста.")


# --------------------------------------------------------------------------- #
#  Поддержка → пользователь (ответ админа из топика)
# --------------------------------------------------------------------------- #
def _user_by_topic(topic_id: int) -> int | None:
    with session_scope() as session:
        return get_ticket_user_by_topic(session, topic_id)


@router.message(
    F.chat.id == SUPPORT_CHAT_ID,
    F.message_thread_id.is_not(None),
    ~F.forum_topic_created,                          # игнорируем сервисные сообщения
)
async def on_admin_reply(message: Message) -> None:
    # Ответы пишет человек-админ; собственные сообщения бот в апдейтах не получает.
    if message.from_user and message.from_user.is_bot:
        return
    user_id = await asyncio.to_thread(_user_by_topic, message.message_thread_id)
    if user_id is None:
        return                                       # топик без тикета — не наш
    try:
        await message.copy_to(chat_id=user_id)       # текст или медиа — в личку юзеру
    except Exception as exc:  # noqa: BLE001 — юзер мог заблокировать бота
        log.warning("Ответ поддержки юзеру %s не доставлен: %s", user_id, exc)
        try:  # сообщить админу в топик, чтобы он не общался «в пустоту»
            await message.reply(
                "⚠️ <b>Не доставлено:</b> пользователь заблокировал бота "
                "или его чат недоступен."
            )
        except Exception as notify_exc:  # noqa: BLE001 — нет прав писать в топик
            log.warning("Алерт о недоставке в топик %s не отправлен: %s",
                        message.message_thread_id, notify_exc)


__all__ = ["router"]
