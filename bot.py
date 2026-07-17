#!/usr/bin/env python3
"""Telethon media bot for Termux.

Place .env and, optionally, cookies.txt next to this file.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from dotenv import load_dotenv
from groq import Groq
from telethon import Button, TelegramClient, events

try:
    from shazamio import Shazam
except ImportError:
    Shazam = None  # type: ignore[assignment]
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import DocumentAttributeAudio
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from ytmusicapi import YTMusic

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

API_ID = os.getenv("API_ID", "")
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
COOKIES_FILE = BASE_DIR / "cookies.txt"
TELEGRAM_LIMIT = 2 * 1024**3
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
SUPPORTED_HOSTS = (
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
    "twitter.com", "x.com", "vk.com", "vkvideo.ru",
)
MUSIC_TRIGGER_RE = re.compile(r"^(?:найти|найди|трек)\s+(.+)", re.IGNORECASE)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("media-bot")


class UserError(Exception):
    """An error safe to display to a user."""


@dataclass
class Choice:
    owner_id: int
    url: str
    title: str
    heights: list[int]
    created_at: float = field(default_factory=time.time)


@dataclass
class Job:
    kind: str
    chat_id: int
    user_id: int
    source_message_id: int
    status_message: Any
    payload: dict[str, Any]


choices: dict[str, Choice] = {}
track_choices: dict[str, tuple[int, str, float]] = {}
queue: asyncio.Queue[Job] = asyncio.Queue()
client: TelegramClient


def require_config() -> None:
    missing = [name for name, value in {
        "API_ID": API_ID, "API_HASH": API_HASH, "BOT_TOKEN": BOT_TOKEN,
        "GROQ_API_KEY": GROQ_API_KEY,
    }.items() if not value]
    if missing:
        raise SystemExit("Не заданы переменные в .env: " + ", ".join(missing))
    if not API_ID.isdigit():
        raise SystemExit("API_ID в .env должен быть целым числом.")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg не найден. Выполните: pkg install ffmpeg")


def ydl_base() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    if COOKIES_FILE.is_file():
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def validate_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(".,);]}")
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if not any(host == item or host.endswith("." + item) for item in SUPPORTED_HOSTS):
        raise UserError("Эта ссылка не поддерживается. Поддерживаются YouTube, TikTok, Instagram, X/Twitter и VK.")
    return url


def inspect_url_sync(url: str) -> tuple[str, list[int]]:
    try:
        with YoutubeDL({**ydl_base(), "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise UserError("Не удалось прочитать ссылку. Проверьте адрес или добавьте актуальный cookies.txt.") from exc
    if not info:
        raise UserError("Видео по этой ссылке не найдено.")
    heights = sorted({int(f["height"]) for f in info.get("formats", []) if f.get("height") and f.get("vcodec") != "none"})
    if not heights:
        heights = [360]
    # Keep a practical subset while retaining the best source quality.
    preferred = [h for h in (360, 480, 720, 1080, 1440, 2160) if h in heights]
    if heights[-1] not in preferred:
        preferred.append(heights[-1])
    return str(info.get("title") or "Видео"), sorted(set(preferred))


def progress_hook(loop: asyncio.AbstractEventLoop, setter: Callable[[float], None]) -> Callable[[dict[str, Any]], None]:
    def hook(data: dict[str, Any]) -> None:
        if data.get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        downloaded = data.get("downloaded_bytes", 0)
        if total:
            loop.call_soon_threadsafe(setter, min(downloaded / total, 1.0))
    return hook


def find_output(folder: Path, suffixes: tuple[str, ...]) -> Path:
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in suffixes and not p.name.endswith(".part")]
    if not files:
        raise UserError("Загрузчик не создал ожидаемый файл.")
    return max(files, key=lambda p: p.stat().st_size)


def download_video_sync(url: str, height: int | None, folder: Path, hook: Callable[[dict[str, Any]], None]) -> tuple[Path, dict[str, Any]]:
    is_tiktok = "tiktok.com" in (urlparse(url).hostname or "")
    if is_tiktok:
        fmt = "bv*+ba/b"  # yt-dlp selects the best source stream exposed by TikTok.
    elif height:
        fmt = f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"
    else:
        fmt = "bv*+ba/b"
    opts = {
        **ydl_base(), "format": fmt, "outtmpl": str(folder / "video.%(ext)s"),
        "merge_output_format": "mp4", "progress_hooks": [hook],
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as exc:
        raise UserError("Не удалось скачать видео. Возможно, нужны свежие cookies.txt или ролик недоступен.") from exc
    return find_output(folder, (".mp4", ".mkv", ".webm", ".mov")), info or {}


def download_audio_url_sync(url: str, folder: Path, hook: Callable[[dict[str, Any]], None]) -> tuple[Path, dict[str, Any]]:
    opts = {
        **ydl_base(), "format": "bestaudio/best", "outtmpl": str(folder / "audio.%(ext)s"),
        "progress_hooks": [hook],
        "postprocessors": [{
            "key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320",
        }],
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as exc:
        raise UserError("Не удалось скачать аудиодорожку.") from exc
    return find_output(folder, (".mp3",)), info or {}


def search_music_sync(query: str) -> dict[str, str]:
    try:
        results = YTMusic().search(query, filter="songs", limit=5)
        if not results:
            results = YTMusic().search(query, filter="videos", limit=5)
    except Exception as exc:
        raise UserError("Поиск музыки сейчас недоступен.") from exc
    if not results:
        raise UserError("Трек не найден. Попробуйте уточнить исполнителя и название.")
    item = results[0]
    artists = ", ".join(a.get("name", "") for a in item.get("artists", []) if a.get("name"))
    return {
        "url": "https://music.youtube.com/watch?v=" + item["videoId"],
        "title": item.get("title") or query,
        "artist": artists or "Неизвестный исполнитель",
    }


def download_music_sync(query: str, folder: Path, hook: Callable[[dict[str, Any]], None]) -> tuple[Path, str, str, Path | None]:
    found = search_music_sync(query)
    opts = {
        **ydl_base(), "format": "bestaudio/best", "outtmpl": str(folder / "track.%(ext)s"),
        "writethumbnail": True, "progress_hooks": [hook],
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        ],
        "postprocessor_args": {"FFmpegMetadata": ["-metadata", f"title={found['title']}", "-metadata", f"artist={found['artist']}"]},
    }
    try:
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(found["url"], download=True)
    except DownloadError as exc:
        raise UserError("Не удалось скачать найденный трек.") from exc
    audio = find_output(folder, (".mp3",))
    covers = [p for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    return audio, found["title"], found["artist"], covers[0] if covers else None


def run_ffmpeg(*args: str) -> None:
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        log.error("ffmpeg failed: %s", exc.stderr.decode(errors="replace"))
        raise UserError("Не удалось обработать аудио через ffmpeg.") from exc


def transcribe_sync(wav: Path) -> str:
    try:
        groq = Groq(api_key=GROQ_API_KEY)
        with wav.open("rb") as audio:
            result = groq.audio.transcriptions.create(
                file=(wav.name, audio.read()), model="whisper-large-v3",
                response_format="text", temperature=0,
                prompt="Расшифруй речь с правильной пунктуацией, регистром букв и абзацами.",
            )
        text = result if isinstance(result, str) else getattr(result, "text", str(result))
    except Exception as exc:
        raise UserError("Сервис расшифровки Groq сейчас недоступен.") from exc
    if not text.strip():
        raise UserError("Не удалось распознать речь в сообщении.")
    return text.strip()


async def safe_edit(message: Any, text: str, **kwargs: Any) -> None:
    try:
        await message.edit(text, **kwargs)
    except MessageNotModifiedError:
        pass
    except FloodWaitError as exc:
        await asyncio.sleep(exc.seconds)
    except Exception:
        log.debug("Could not edit status message", exc_info=True)


def format_progress_bar(percent: float, length: int = 12) -> str:
    filled = int(length * percent / 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percent:.0f}%"


async def upload_progress(status: Any, label: str) -> Callable[[int, int], Any]:
    state = {"last": 0.0, "percent": -1}

    async def callback(current: int, total: int) -> None:
        now = time.monotonic()
        percent = int(current * 100 / total) if total else 0
        if percent == 100 or (now - state["last"] >= 2.5 and percent != state["percent"]):
            state.update(last=now, percent=percent)
            await safe_edit(status, f"📤 {label}\n{format_progress_bar(percent)}")
    return callback


async def report_download_progress(status: Any, state: dict[str, float], stop: asyncio.Event) -> None:
    last = -1
    while not stop.is_set():
        percent = int(state["value"] * 100)
        if percent != last:
            await safe_edit(status, f"⏳ Скачиваю с источника...\n{format_progress_bar(percent)}")
            last = percent
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=2.5)


async def send_long_reply(chat_id: int, message_id: int, text: str) -> None:
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    for index, chunk in enumerate(chunks):
        await client.send_message(chat_id, chunk, reply_to=message_id if index == 0 else None)


async def recognize_track(video: Path, folder: Path) -> tuple[str, str] | None:
    if Shazam is None:
        log.info("ShazamIO is not installed; track recognition is disabled")
        return None

    sample = folder / "shazam.wav"
    await asyncio.to_thread(run_ffmpeg, "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-t", "30", str(sample))
    try:
        shazam = Shazam()
        # ShazamIO 0.6.0 (Termux-friendly, без Rust) использует recognize_song,
        # новые версии используют recognize.
        recognize = getattr(shazam, "recognize", None)
        result = (
            await recognize(str(sample))
            if recognize is not None
            else await shazam.recognize_song(str(sample))
        )
        track = result.get("track") if isinstance(result, dict) else None
        if track and track.get("title"):
            return str(track["title"]), str(track.get("subtitle") or "")
    except Exception:
        log.warning("Shazam recognition failed", exc_info=True)
    return None


async def process_video(job: Job, folder: Path) -> None:
    status = job.status_message
    state = {"value": 0.0}
    stop = asyncio.Event()
    reporter = asyncio.create_task(report_download_progress(status, state, stop))
    loop = asyncio.get_running_loop()
    try:
        video, info = await asyncio.to_thread(
            download_video_sync, job.payload["url"], job.payload.get("height"), folder,
            progress_hook(loop, lambda value: state.__setitem__("value", value)),
        )
    finally:
        stop.set()
        await reporter
    if video.stat().st_size >= TELEGRAM_LIMIT:
        raise UserError("📦 Файл превышает лимит Telegram 2 ГБ. Выбери качество пониже.")
    await safe_edit(status, f"📤 Отправляю видео\n{format_progress_bar(0)}")
    progress = await upload_progress(status, "Отправляю видео")
    await client.send_file(
        job.chat_id, video, caption=f"✅ {str(info.get('title') or 'Видео')[:900]}",
        reply_to=job.source_message_id, supports_streaming=True,
        progress_callback=progress,
    )
    await safe_edit(status, "🎧 Видео отправлено. Распознаю музыку в нём...")
    found = await recognize_track(video, folder)
    if found:
        title, artist = found
        token = uuid.uuid4().hex[:12]
        query = f"{artist} {title}".strip()
        track_choices[token] = (job.user_id, query, time.time())
        await client.send_message(
            job.chat_id, f"🎧 Похоже, в видео звучит:\n**{artist} — {title}**" if artist else f"🎧 Похоже, в видео звучит:\n**{title}**",
            buttons=[[Button.inline("⬇️ Скачать трек", data=f"track:{token}")]],
            reply_to=job.source_message_id,
        )
        await safe_edit(status, "✅ Готово! Песня в видео распознана.")
    else:
        await safe_edit(status, "✅ Готово! Песню в видео распознать не удалось.")


async def process_url_audio(job: Job, folder: Path) -> None:
    await safe_edit(job.status_message, "⏳ Скачиваю аудио...")
    loop = asyncio.get_running_loop()
    audio, info = await asyncio.to_thread(download_audio_url_sync, job.payload["url"], folder, progress_hook(loop, lambda _: None))
    if audio.stat().st_size >= TELEGRAM_LIMIT:
        raise UserError("📦 Аудиофайл превышает лимит Telegram 2 ГБ.")
    progress = await upload_progress(job.status_message, "Отправляю аудио")
    await client.send_file(
        job.chat_id, audio, caption=f"✅ {str(info.get('title') or 'Аудио')[:900]}",
        reply_to=job.source_message_id, progress_callback=progress,
    )
    await safe_edit(job.status_message, "✅ Аудио отправлено.")


async def process_music(job: Job, folder: Path) -> None:
    await safe_edit(job.status_message, "🔎 Ищу трек...")
    loop = asyncio.get_running_loop()
    audio, title, artist, cover = await asyncio.to_thread(
        download_music_sync, job.payload["query"], folder, progress_hook(loop, lambda _: None),
    )
    if audio.stat().st_size >= TELEGRAM_LIMIT:
        raise UserError("📦 Трек превышает лимит Telegram 2 ГБ.")
    progress = await upload_progress(job.status_message, "Отправляю трек")
    await client.send_file(
        job.chat_id, audio, thumb=cover, reply_to=job.source_message_id,
        attributes=[DocumentAttributeAudio(duration=0, title=title, performer=artist, voice=False)],
        caption=f"🎵 **{title}**\n👤 {artist}", progress_callback=progress,
    )
    await safe_edit(job.status_message, "✅ Трек отправлен.")


async def process_transcription(job: Job, folder: Path) -> None:
    await safe_edit(job.status_message, "📥 Скачиваю голосовое сообщение...")
    source = folder / "source_media"
    message = await client.get_messages(job.chat_id, ids=job.source_message_id)
    downloaded = await message.download_media(file=str(source))
    if not downloaded:
        raise UserError("Не удалось скачать голосовое сообщение.")
    wav = folder / "speech.wav"
    await safe_edit(job.status_message, "🎧 Подготавливаю аудио...")
    await asyncio.to_thread(run_ffmpeg, "-i", str(downloaded), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav))
    await safe_edit(job.status_message, "🎤 Расшифровываю речь...")
    text = await asyncio.to_thread(transcribe_sync, wav)
    await send_long_reply(job.chat_id, job.source_message_id, f"🎤 **Вы сказали:**\n_{text}_")
    await safe_edit(job.status_message, "✅ Расшифровка готова.")


async def worker() -> None:
    while True:
        job = await queue.get()
        try:
            with tempfile.TemporaryDirectory(prefix="media_bot_") as tmp:
                folder = Path(tmp)
                if job.kind == "video":
                    await process_video(job, folder)
                elif job.kind == "url_audio":
                    await process_url_audio(job, folder)
                elif job.kind == "music":
                    await process_music(job, folder)
                elif job.kind == "transcribe":
                    await process_transcription(job, folder)
        except UserError as exc:
            await safe_edit(job.status_message, f"⚠️ {exc}")
        except FloodWaitError as exc:
            await safe_edit(job.status_message, f"⏱ Telegram просит подождать {exc.seconds} сек. Повтори запрос позже.")
        except Exception:
            log.exception("Unhandled job error (%s)", job.kind)
            await safe_edit(job.status_message, "⚠️ Произошла внутренняя ошибка. Попробуй ещё раз позже.")
        finally:
            queue.task_done()


async def enqueue(kind: str, event: Any, status: Any, **payload: Any) -> None:
    position = queue.qsize() + 1
    await queue.put(Job(kind, event.chat_id, event.sender_id, event.message.id, status, payload))
    await safe_edit(status, f"📋 Задание в очереди. Позиция: {position}")


@events.register(events.NewMessage(pattern=r"^/(start|help)(?:@\w+)?$"))
async def help_handler(event: Any) -> None:
    await event.reply(
        "📲 **Media Save Bot** — твой личный медиа-помощник\n\n"
        "🔗 Пришли ссылку (YouTube, TikTok, Instagram, X/Twitter, VK) — предложу качество на выбор.\n\n"
        "🎤 Пришли голосовое или кружок — расшифрую речь в текст.\n\n"
        "🎵 Напиши **найти <название>** — найду и пришлю трек.\n"
        "Например: `найти Нирвана Smells Like Teen Spirit`\n\n"
        "⏳ Тяжёлые задачи выполняются по одной, остальные ждут в очереди."
    )


@events.register(events.NewMessage(pattern=r"^/music(?:@\w+)?(?:\s+(.+))?$"))
async def music_command(event: Any) -> None:
    query = (event.pattern_match.group(1) or "").strip()
    if not query:
        await event.reply("🎵 Укажи название: `/music исполнитель — песня`")
        return
    status = await event.reply("🔎 Добавляю поиск музыки в очередь...")
    await enqueue("music", event, status, query=query)


@events.register(events.NewMessage(func=lambda e: bool(e.message.voice or e.message.video_note)))
async def voice_handler(event: Any) -> None:
    status = await event.reply("🎧 Добавляю расшифровку в очередь...")
    await enqueue("transcribe", event, status)


@events.register(events.NewMessage(incoming=True))
async def text_handler(event: Any) -> None:
    if event.message.voice or event.message.video_note or not event.raw_text or event.raw_text.startswith("/"):
        return
    try:
        url = validate_url(event.raw_text)
    except UserError as exc:
        await event.reply(str(exc))
        return
    if not url:
        match = MUSIC_TRIGGER_RE.match(event.raw_text.strip())
        if not match:
            # Обычный текст без ссылки и без "найти/трек" впереди — просто игнорируем,
            # чтобы бот не пытался искать музыку по любому случайному сообщению.
            return
        query = match.group(1).strip()
        status = await event.reply("🔎 Добавляю поиск музыки в очередь...")
        await enqueue("music", event, status, query=query)
        return
    status = await event.reply("⏳ Получаю список доступных качеств...")
    try:
        title, heights = await asyncio.to_thread(inspect_url_sync, url)
    except UserError as exc:
        await safe_edit(status, f"⚠️ {exc}")
        return
    token = uuid.uuid4().hex[:12]
    choices[token] = Choice(event.sender_id, url, title, heights)
    buttons = [
        [Button.inline(f"{'⚡️' if height >= 720 else '🔹'} {height}p", data=f"video:{token}:{height}")
         for height in heights[i:i + 3]]
        for i in range(0, len(heights), 3)
    ]
    buttons.append([Button.inline("🎧 Только звук (MP3)", data=f"audio:{token}")])
    await safe_edit(
        status,
        f"🎬 **{title[:180]}**\n\n⚡️ — быстрая загрузка (HD)\n🔹 — компактный размер\n\nВыбери, что скачать:",
        buttons=buttons,
    )


@events.register(events.CallbackQuery(pattern=rb"^(video|audio):([a-f0-9]{12})(?::(\d+))?$"))
async def quality_callback(event: Any) -> None:
    kind = event.pattern_match.group(1).decode()
    token = event.pattern_match.group(2).decode()
    item = choices.get(token)
    if not item or time.time() - item.created_at > REGISTRY_TTL:
        await event.answer("⌛ Выбор устарел, пришли ссылку снова.", alert=True)
        choices.pop(token, None)
        return
    if event.sender_id != item.owner_id:
        await event.answer("Эта кнопка предназначена другому пользователю.", alert=True)
        return
    await event.answer("✅ Добавлено в очередь")
    choices.pop(token, None)
    status = await event.get_message()
    payload = {"url": item.url}
    if kind == "video":
        payload["height"] = int(event.pattern_match.group(3))
    await enqueue("video" if kind == "video" else "url_audio", event, status, **payload)


@events.register(events.CallbackQuery(pattern=rb"^track:([a-f0-9]{12})$"))
async def track_callback(event: Any) -> None:
    token = event.pattern_match.group(1).decode()
    item = track_choices.get(token)
    if not item or time.time() - item[2] > REGISTRY_TTL:
        await event.answer("⌛ Кнопка устарела.", alert=True)
        track_choices.pop(token, None)
        return
    if event.sender_id != item[0]:
        await event.answer("Эта кнопка предназначена другому пользователю.", alert=True)
        return
    await event.answer("✅ Добавлено в очередь")
    track_choices.pop(token, None)
    status = await event.respond("🎵 Добавляю трек в очередь...")
    # Callback event has the recognition message as its source; that is suitable for a reply.
    await enqueue("music", event, status, query=item[1])


async def main() -> None:
    global client
    require_config()
    client = TelegramClient(str(BASE_DIR / "bot"), int(API_ID), API_HASH)
    client.add_event_handler(help_handler)
    client.add_event_handler(music_command)
    client.add_event_handler(voice_handler)
    client.add_event_handler(text_handler)
    client.add_event_handler(quality_callback)
    client.add_event_handler(track_callback)
    await client.start(bot_token=BOT_TOKEN)
    worker_task = asyncio.create_task(worker(), name="media-worker")
    me = await client.get_me()
    log.info("Bot @%s started; cookies.txt: %s", me.username, "yes" if COOKIES_FILE.is_file() else "no")
    try:
        await client.run_until_disconnected()
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task


if __name__ == "__main__":
    asyncio.run(main())
