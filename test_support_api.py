"""Live Smoke Test техподдержки: getChat → getChatMember → create/delete_forum_topic.

Дословно показывает ответы Telegram API по текущему SUPPORT_CHAT_ID. Временный
диагностический скрипт (не часть бота). Токен НЕ печатаем.
"""

from __future__ import annotations

import asyncio
import sys

from aiogram import Bot

from config import SUPPORT_CHAT_ID, TELEGRAM_BOT_TOKEN

# Можно передать chat_id первым аргументом, иначе берём из config.
SUPPORT_CHAT_ID = int(sys.argv[1]) if len(sys.argv) > 1 else SUPPORT_CHAT_ID


def line() -> None:
    print("-" * 60)


async def main() -> None:
    print(f"SUPPORT_CHAT_ID = {SUPPORT_CHAT_ID}  (тип id: "
          f"{'супергруппа -100…' if SUPPORT_CHAT_ID < 0 else 'НЕ супергруппа (положит.)'})")
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN не задан — выходим.")
        return
    if not SUPPORT_CHAT_ID:
        print("❌ SUPPORT_CHAT_ID = 0 — нечего проверять.")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        # 0. Кто мы.
        line()
        me = await bot.get_me()
        print(f"[0] getMe → id={me.id}, @{me.username}")

        # 1. Видит ли бот чат, его тип и is_forum.
        line()
        try:
            chat = await bot.get_chat(SUPPORT_CHAT_ID)
            print(f"[1] getChat → OK")
            print(f"      type   = {chat.type}")
            print(f"      title  = {chat.title!r}")
            print(f"      is_forum = {chat.is_forum}")
        except Exception as exc:  # noqa: BLE001
            print(f"[1] getChat → ОШИБКА: {type(exc).__name__}: {exc}")

        # 2. Статус бота в чате + право can_manage_topics.
        line()
        try:
            member = await bot.get_chat_member(SUPPORT_CHAT_ID, me.id)
            print(f"[2] getChatMember(bot) → OK")
            print(f"      status = {member.status}")
            print(f"      can_manage_topics = {getattr(member, 'can_manage_topics', 'n/a')}")
        except Exception as exc:  # noqa: BLE001
            print(f"[2] getChatMember → ОШИБКА: {type(exc).__name__}: {exc}")

        # 3. Экспериментально создать топик и сразу удалить.
        line()
        try:
            topic = await bot.create_forum_topic(
                chat_id=SUPPORT_CHAT_ID, name="🧪 Тест Системы"
            )
            print(f"[3] create_forum_topic → OK, message_thread_id={topic.message_thread_id}")
            try:
                await bot.delete_forum_topic(
                    chat_id=SUPPORT_CHAT_ID, message_thread_id=topic.message_thread_id
                )
                print(f"      delete_forum_topic → OK (тестовый топик удалён)")
                print("✅ ПОЛНАЯ РАБОТОСПОСОБНОСТЬ: топики создаются и удаляются.")
            except Exception as exc:  # noqa: BLE001
                print(f"      delete_forum_topic → ОШИБКА: {type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[3] create_forum_topic → ОШИБКА: {type(exc).__name__}: {exc}")
            print("    ↑ это и есть причина падения техподдержки в боте.")
        line()
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
