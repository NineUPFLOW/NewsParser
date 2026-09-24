"""
Telegram-бот для скачивания видео через yt-dlp.
Работает на GitHub Actions без cookies.

Для YouTube пробует 5 разных player_client по очереди —
иногда один из них обходит bot-check без авторизации.
"""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, Message

# ═══════════════════════════════════════════════════════════════════════
# Логирование
# ═══════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("download_bot")

# ═══════════════════════════════════════════════════════════════════════
# Конфигурация
# ═══════════════════════════════════════════════════════════════════════
BOT_TOKEN = os.environ["BOT_TOKEN"]

MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_RUN_TIME = 5 * 3600
DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOADS_DELAY = 3

COOKIES_FILE = "youtube_cookies.txt"

SUPPORTED_DOMAINS = (
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
    "twitter.com", "x.com", "reddit.com", "vimeo.com",
    "soundcloud.com", "twitch.tv", "vk.com", "dailymotion.com",
)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ═══════════════════════════════════════════════════════════════════════
# Утилиты
# ═══════════════════════════════════════════════════════════════════════

def _is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


def _detect_platform(url: str) -> str:
    url_lower = url.lower()
    for d in SUPPORTED_DOMAINS:
        if d in url_lower:
            return d.split(".")[0].capitalize()
    return "Сайт"


def _clean_dir():
    if DOWNLOAD_DIR.exists():
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ═══════════════════════════════════════════════════════════════════════
# Скачивание
# ═══════════════════════════════════════════════════════════════════════

async def download_media(url: str) -> tuple[Path | None, str]:
    """
    Пытается скачать через разные player_client, пока один не сработает.
    Возвращает (file_path, error_text). При успехе error_text пустой.
    """
    _clean_dir()

    strategies = [
        {
            "name": "tv_embedded",
            "args": ["--extractor-args", "youtube:player_client=tv_embedded"],
        },
        {
            "name": "ios",
            "args": ["--extractor-args", "youtube:player_client=ios"],
        },
        {
            "name": "android",
            "args": ["--extractor-args", "youtube:player_client=android"],
        },
        {
            "name": "web_embedded",
            "args": ["--extractor-args", "youtube:player_client=web_embedded"],
        },
        {
            "name": "default",
            "args": [],
        },
    ]

    last_error = "Не удалось скачать"

    for strategy in strategies:
        logger.info("Пробую стратегию: %s", strategy["name"])

        output_template = str(DOWNLOAD_DIR / "%(title).100s.%(ext)s")

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--max-filesize", "50M",
            "--format", "best[ext=mp4][filesize<50M]/best[filesize<50M]",
            "--js-runtimes", "deno",
            "--no-warnings",
            "--quiet",
            "--no-progress",
            "-o", output_template,
        ]

        if Path(COOKIES_FILE).exists():
            cmd.extend(["--cookies", COOKIES_FILE])

        cmd.extend(strategy["args"])
        cmd.append(url)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            except asyncio.TimeoutError:
                proc.kill()
                last_error = "Таймаут скачивания"
                continue

            if proc.returncode == 0:
                files = [
                    f for f in DOWNLOAD_DIR.glob("*")
                    if f.is_file() and not f.name.endswith((".part", ".ytdl"))
                ]
                if files:
                    logger.info("✅ Сработала стратегия: %s", strategy["name"])
                    return max(files, key=lambda f: f.stat().st_size), ""

            err = stderr.decode(errors="ignore")
            logger.warning("Стратегия %s провалилась: %s", strategy["name"], err[:200])

            if "Sign in to confirm" in err:
                last_error = "YouTube блокирует бота (нужны cookies)"
            elif "Video unavailable" in err:
                return None, "Видео недоступно"
            elif "Private video" in err:
                return None, "Приватное видео"
            elif "File is larger than max-filesize" in err:
                return None, "Файл больше 50 MB"
            elif "This video is available to this channel" in err:
                return None, "Видео ограничено"

        except Exception as e:
            logger.error("Ошибка стратегии %s: %s", strategy["name"], e)
            continue

    return None, last_error


# ═══════════════════════════════════════════════════════════════════════
# Обработчики
# ═══════════════════════════════════════════════════════════════════════

@dp.message(F.text == "/start")
async def cmd_start(msg: Message):
    await msg.answer(
        "📥 <b>Бот для скачивания видео</b>\n\n"
        "Отправь ссылку на видео с:\n"
        "• YouTube, TikTok, Instagram, Twitter/X, Reddit\n"
        "• Vimeo, SoundCloud, Twitch, VK, Dailymotion\n\n"
        "⚠️ Максимальный размер — 50 MB\n"
        "⚠️ YouTube может требовать авторизации"
    )


@dp.message(F.text == "/help")
async def cmd_help(msg: Message):
    await msg.answer("Отправь ссылку — пришлю файл.")


@dp.message(F.text)
async def handle_url(msg: Message):
    text = msg.text.strip()

    if not _is_url(text):
        await msg.answer("❌ Это не похоже на ссылку")
        return

    chat_id = msg.chat.id
    platform = _detect_platform(text)
    status = await msg.answer(f"⏳ Скачиваю с {platform}...")

    started = time.time()
    file, err = await download_media(text)

    if not file or not file.exists():
        await status.edit_text(f"❌ {err or 'Не удалось скачать'}")
        return

    size = file.stat().st_size
    if size > MAX_FILE_SIZE:
        await status.edit_text(
            f"❌ Файл слишком большой: {_human_size(size)} (лимит 50 MB)"
        )
        try:
            file.unlink()
        except Exception:
            pass
        return

    elapsed = int(time.time() - started)
    await status.edit_text(
        f"📤 Отправляю... ({_human_size(size)}, скачано за {elapsed} с)"
    )

    try:
        await bot.send_video(
            chat_id=chat_id,
            video=FSInputFile(file),
            caption=f"📥 {file.name}\n{_human_size(size)}",
            supports_streaming=True,
            read_timeout=300,
            write_timeout=300,
        )
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error("send_video failed: %s", e)
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=FSInputFile(file),
                caption=f"📥 {file.name}",
            )
            try:
                await status.delete()
            except Exception:
                pass
        except Exception as e2:
            await status.edit_text(f"❌ Ошибка отправки: {e2}")
    finally:
        try:
            file.unlink()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
# Главный цикл
# ═══════════════════════════════════════════════════════════════════════

async def main():
    logger.info("🚀 Бот запущен")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning("delete_webhook: %s", e)

    async def stop_after_timeout():
        await asyncio.sleep(MAX_RUN_TIME)
        logger.info("⏰ Лимит времени — останавливаю polling")
        await dp.stop_polling()

    asyncio.create_task(stop_after_timeout())

    try:
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
        await bot.session.close()
        logger.info("👋 Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
