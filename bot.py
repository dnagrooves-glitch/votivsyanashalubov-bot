import os
import io
import asyncio
import httpx
import replicate
import requests as req
import time
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
CHORUS_AUDIO_URL    = os.getenv("CHORUS_AUDIO_URL")
TRACK_URL           = os.getenv("TRACK_URL", "https://band.link/vcvotivsyanashalubov")
TIKTOK_SOUND_URL    = "https://vt.tiktok.com/ZS9dkQxdcqNFN-RYvnX"
ADMIN_CHAT_ID       = os.getenv("ADMIN_CHAT_ID")  # ← добавь в Railway Variables


# ─── НОВОЕ 1: Алерт администратору при ошибке ────────────────────────────────
async def notify_admin(app, user, error: Exception, stage: str):
    if not ADMIN_CHAT_ID:
        return
    try:
        username = f"@{user.username}" if user.username else f"id:{user.id}"
        text = (
            f"🚨 Ошибка у пользователя\n\n"
            f"👤 {user.full_name or '—'} ({username})\n"
            f"📍 Этап: {stage}\n"
            f"❌ {type(error).__name__}: {str(error)[:300]}"
        )
        await app.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as e:
        print(f"[WARN] Could not notify admin: {e}")


# ─── НОВОЕ 2: Лимит одно видео в сутки ──────────────────────────────────────
_user_last_video: dict = {}

def _check_daily_limit(user_id: int) -> bool:
    import datetime
    return _user_last_video.get(user_id) == datetime.date.today().isoformat()

def _mark_used(user_id: int):
    import datetime
    _user_last_video[user_id] = datetime.date.today().isoformat()


# ─── ШАГ 1: GFPGAN — AI-гламур на коже (~5с) ────────────────────────────────
async def enhance_face(image_bytes: bytes) -> bytes:
    os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    buf.seek(0)

    output = await asyncio.to_thread(
        replicate.run,
        "tencentarc/gfpgan:0fbacf7afc6c144e5be9767cff80f25aff23e52b0708f17e20f9879b2f21516c",
        input={
            "img":     buf,
            "version": "v1.4",
            "scale":   2,
        }
    )

    print(f"[INFO] GFPGAN output: {output}")

    url = output if isinstance(output, str) else (output.url if hasattr(output, "url") else str(output))

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


# ─── ШАГ 2: OmniHuman — фото + аудио → видео с пением ───────────────────────
async def create_singing_video(image_bytes: bytes) -> bytes:
    os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    buf.seek(0)

    # Retry при rate limit (429)
    for attempt in range(5):
        try:
            output = await asyncio.to_thread(
                replicate.run,
                "bytedance/omni-human",
                input={
                    "image": buf,
                    "audio": CHORUS_AUDIO_URL,
                }
            )
            break
        except Exception as e:
            if "429" in str(e) or "throttled" in str(e).lower():
                wait = 15 * (attempt + 1)
                print(f"[WARN] Rate limited, waiting {wait}s... (attempt {attempt+1})")
                await asyncio.sleep(wait)
                buf = io.BytesIO()
                Image.open(io.BytesIO(image_bytes)).convert("RGB").save(buf, format="JPEG", quality=95)
                buf.seek(0)
            else:
                raise
    else:
        raise RuntimeError("Превышен лимит запросов Replicate, попробуй позже")

    print(f"[INFO] OmniHuman raw output: {output}, type: {type(output)}")

    if hasattr(output, "url"):
        url = output.url
    elif isinstance(output, list) and hasattr(output[0], "url"):
        url = output[0].url
    elif isinstance(output, list) and isinstance(output[0], str):
        url = output[0]
    elif isinstance(output, str):
        url = output
    else:
        s = str(output)
        import re
        m = re.search(r'https://\S+\.mp4', s)
        if m:
            url = m.group(0)
        else:
            raise RuntimeError(f"Cannot extract URL from: {type(output)}: {s[:200]}")

    print(f"[INFO] Downloading video from: {url}")
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


# ─── НОВОЕ 3: Наложение текста на видео через ffmpeg ─────────────────────────
async def add_text_overlay(video_bytes: bytes) -> bytes:
    import tempfile, subprocess, shutil
    if not shutil.which("ffmpeg"):
        print("[WARN] ffmpeg not found, skipping text overlay")
        return video_bytes

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fin:
        fin.write(video_bytes)
        input_path = fin.name
    output_path = input_path.replace(".mp4", "_out.mp4")

    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font_arg = f":fontfile={font}" if os.path.exists(font) else ""

    vf = (
        f"drawtext=text='это ИИ-версия'{font_arg}"
        f":fontsize=52:fontcolor=white:borderw=3:bordercolor=black"
        f":x=(w-text_w)/2:y=80:enable='lte(t,3)',"

        f"drawtext=text='Сделать свою версию:'{font_arg}"
        f":fontsize=38:fontcolor=white:borderw=3:bordercolor=black"
        f":x=(w-text_w)/2:y=h-130,"

        f"drawtext=text='@veeka_ai_bot в Telegram'{font_arg}"
        f":fontsize=38:fontcolor=white:borderw=3:bordercolor=black"
        f":x=(w-text_w)/2:y=h-80"
    )

    cmd = ["ffmpeg", "-y", "-i", input_path, "-vf", vf,
           "-c:v", "libx264", "-c:a", "copy", "-preset", "fast", output_path]

    try:
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            print(f"[WARN] ffmpeg error: {result.stderr.decode()[:300]}")
            return video_bytes
        with open(output_path, "rb") as f:
            out = f.read()
        print(f"[INFO] Text overlay done, {len(out)} bytes")
        return out
    except Exception as e:
        print(f"[WARN] Text overlay failed: {e}")
        return video_bytes
    finally:
        for p in [input_path, output_path]:
            try: os.unlink(p)
            except Exception: pass


# ─── ROAST MESSAGES ──────────────────────────────────────────────────────────
ROAST_MESSAGES = [
    "🔍 Сканирую его активность в сети...",
    "📱 Найдено: последний онлайн 2 минуты назад.\nВидел сообщение. Не ответил.",
    "🤖 ИИ-анализ профиля:\n\n"
    "— Тип: «я занят» (сидит в TikTok)\n"
    "— Статус: притворяется что спит\n"
    "— Уровень игнора: 94/100",
    "📊 Статистика за 7 дней:\n\n"
    "Просмотрел твои сторис: ✅\n"
    "Ответил: ❌\n"
    "Поставил лайк чужой: ✅✅✅",
    "💀 Нейросеть говорит:\n\n"
    "«Он не потерялся. Телефон не сломан.\n"
    "Он просто ИИ-девушку нашёл.»",
    "🎤 Твоя ИИ-версия разогревает голос...\n\nОн об этом пожалеет.",
    "⚠️ ВНИМАНИЕ: видео почти готово.\n\nПодготовь попкорн.",
]

async def _roast_while_waiting(message):
    try:
        roast_msg = await message.reply_text(ROAST_MESSAGES[0])
        for text in ROAST_MESSAGES[1:]:
            await asyncio.sleep(18)
            try:
                await roast_msg.edit_text(text)
            except Exception:
                pass
    except Exception:
        pass


# ─── HANDLERS ────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Он в сети — общается с ИИ.\nНу и ладно.\n\n"
        "Отправь своё фото — получи себя в виде ИИ-девушки "
        "которая поёт про него 🤖🎵\n\n"
        "⚠️ Одно видео в сутки на человека."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Проверяем лимит
    if _check_daily_limit(user_id):
        await update.message.reply_text(
            "Ты уже получила своё видео сегодня 🎬\n\n"
            "Возвращайся завтра — сделаю новое 🤍\n\n"
            "А пока → слушай трек: band.link/vcvotivsyanashalubov"
        )
        return

    msg = await update.message.reply_text("⏳ Шаг 1/2 — делаю твою ИИ-версию... (~10с)")

    stage = "GFPGAN (шаг 1/2)"
    try:
        t0 = time.time()

        photo = update.message.photo[-1]
        file = await photo.get_file()
        image_bytes = bytes(await file.download_as_bytearray())
        print(f"[INFO] Got photo {len(image_bytes)} bytes")

        enhanced_bytes = await enhance_face(image_bytes)
        print(f"[INFO] GFPGAN done in {time.time()-t0:.1f}s")

        await msg.edit_text("⏳ Шаг 2/2 — записываю видео... (~2-3 мин)")

        asyncio.create_task(_roast_while_waiting(update.message))

        stage = "OmniHuman (шаг 2/2)"
        video_bytes = await create_singing_video(enhanced_bytes)
        print(f"[INFO] Total done in {time.time()-t0:.1f}s, video {len(video_bytes)} bytes")

        # Накладываем текст
        video_bytes = await add_text_overlay(video_bytes)

        keyboard = [
            [InlineKeyboardButton("🎵 Слушать трек", url=TRACK_URL)],
            [InlineKeyboardButton("📱 Снять видео под этот звук в TikTok", url=TIKTOK_SOUND_URL)],
        ]

        await update.message.reply_video(
            video=io.BytesIO(video_bytes),
            caption=(
                "Твоя ИИ-версия поёт про него 💀\n\n"
                "Сохрани видео → нажми кнопку ниже → снимешь ролик под этот звук в TikTok 👇\n\n"
                "Отметь @veeka_chered и #яИИдевушка"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            supports_streaming=True,
        )

        await msg.delete()
        _mark_used(user_id)

    except Exception as e:
        await msg.edit_text(
            "Что-то пошло не так 😔\n"
            "Попробуй другое фото — лучше всего портрет анфас с чётким лицом."
        )
        print(f"[ERROR] {type(e).__name__}: {e}")
        await notify_admin(context.application, update.effective_user, e, stage)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Отправь своё фото 📸 — сделаю ИИ-версию которая поёт трек VEÉKA"
    )


def main():
    for attempt in range(5):
        try:
            req.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=10)
        except:
            pass
        time.sleep(5)
        try:
            resp = req.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?timeout=1", timeout=5)
            if resp.status_code == 200:
                print(f"[INFO] Ready to start (attempt {attempt+1})")
                break
        except:
            pass
        print(f"[INFO] Waiting for old instance... attempt {attempt+1}/5")
    time.sleep(3)

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("VEÉKA AI бот запущен ✅")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
