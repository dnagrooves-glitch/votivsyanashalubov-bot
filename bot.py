import os
import io
import asyncio
import httpx
import replicate
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN        = os.getenv("TELEGRAM_TOKEN")
REPLICATE_API_TOKEN   = os.getenv("REPLICATE_API_TOKEN")
DID_API_KEY           = os.getenv("DID_API_KEY")
CHORUS_AUDIO_URL      = os.getenv("CHORUS_AUDIO_URL")
TRACK_URL             = os.getenv("TRACK_URL", "https://band.link/vcvotivsyanashalubov")

WATERMARK_LINE1 = "vot i vsya nasha lyubov - VEEKA"
WATERMARK_LINE2 = "slushat trek"

AI_PROMPT = (
    "portrait of a beautiful woman, same face, same features, "
    "neon lights, futuristic, cyberpunk city background, "
    "glowing eyes, ultra detailed, 8k, cinematic lighting, digital art"
)
NEGATIVE_PROMPT = (
    "ugly, deformed, extra limbs, bad anatomy, blurry, low quality, "
    "nsfw, different person, changed face"
)


async def transform_to_ai(image_bytes: bytes) -> str:
    os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as f:
            output = await asyncio.to_thread(
                replicate.run,
                "tencentarc/photomaker:ddfc2b08d209f9fa8c1eca692712918bd449f695dabb4a958da31802a9570fe4",
                input={
                    "prompt": f"img, {AI_PROMPT}",
                    "input_image": f,
                    "negative_prompt": NEGATIVE_PROMPT,
                    "num_outputs": 1,
                    "guidance_scale": 5,
                    "num_inference_steps": 20,
                    "style_name": "Neonpunk",
                }
            )
    finally:
        os.unlink(tmp_path)

    # Replicate может вернуть FileOutput, список или строку — обрабатываем все варианты
    if isinstance(output, list):
        item = output[0]
    else:
        item = output

    # FileOutput имеет метод url или его можно привести к строке
    if hasattr(item, 'url'):
        return item.url
    elif hasattr(item, 'read'):
        # Это file-like объект — читаем байты и загружаем на tmpfiles.org
        data = item.read()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": ("image.jpg", data, "image/jpeg")}
            )
            return resp.json()["data"]["url"].replace("tmpfiles.org/", "tmpfiles.org/dl/")
    else:
        return str(item)


async def create_lipsync(image_url: str) -> bytes:
    headers = {
        "Authorization": f"Basic {DID_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "source_url": image_url,
        "script": {
            "type": "audio",
            "audio_url": CHORUS_AUDIO_URL,
        },
        "config": {
            "fluent": True,
            "pad_audio": 0.0,
            "stitch": True,
        },
        "result_format": "mp4",
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.d-id.com/talks",
            headers=headers,
            json=payload
        )
        resp.raise_for_status()
        talk_id = resp.json()["id"]

        for attempt in range(30):
            await asyncio.sleep(3)
            status_resp = await client.get(
                f"https://api.d-id.com/talks/{talk_id}",
                headers=headers
            )
            data = status_resp.json()
            status = data.get("status")

            if status == "done":
                video_url = data["result_url"]
                video_resp = await client.get(video_url, timeout=60)
                return video_resp.content

            elif status == "error":
                raise RuntimeError(f"D-ID error: {data.get('error', 'unknown')}")

        raise TimeoutError("D-ID не ответил за 90 секунд")


async def add_watermark_to_video(video_bytes: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_in:
        tmp_in.write(video_bytes)
        tmp_in_path = tmp_in.name

    tmp_out_path = tmp_in_path.replace(".mp4", "_wm.mp4")

    watermark = (
        f"drawtext=text='{WATERMARK_LINE1}':"
        f"fontcolor=white:fontsize=28:x=(w-text_w)/2:y=h-80:"
        f"shadowcolor=black:shadowx=2:shadowy=2,"
        f"drawtext=text='{WATERMARK_LINE2}':"
        f"fontcolor=#aaaaff:fontsize=20:x=(w-text_w)/2:y=h-48:"
        f"shadowcolor=black:shadowx=1:shadowy=1"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", tmp_in_path,
        "-vf", watermark,
        "-codec:a", "copy",
        tmp_out_path
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()

    with open(tmp_out_path, "rb") as f:
        result = f.read()

    os.unlink(tmp_in_path)
    os.unlink(tmp_out_path)
    return result


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🎵 Слушать трек", url=TRACK_URL)]]
    await update.message.reply_text(
        "Он в сети — общается с ИИ.\n"
        "Ну и ладно.\n\n"
        "Отправь своё фото — получи себя в виде ИИ-девушки "
        "которая поёт про него 🤖🎵",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "⏳ Шаг 1/2 — создаю твою ИИ-версию..."
    )

    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        image_bytes = await file.download_as_bytearray()

        ai_image_url = await transform_to_ai(bytes(image_bytes))
        print(f"[INFO] AI image URL: {ai_image_url}")

        await msg.edit_text("⏳ Шаг 2/2 — добавляю голос и движение... (~30с)")

        video_bytes = await create_lipsync(ai_image_url)
        final_video = await add_watermark_to_video(video_bytes)

        keyboard = [[InlineKeyboardButton("🎵 Слушать трек", url=TRACK_URL)]]

        await update.message.reply_video(
            video=io.BytesIO(final_video),
            caption=(
                "Твоя ИИ-версия поёт про него 💀\n\n"
                "Поделись и отметь *@veeka\\_chered*\n"
                "#яИИдевушка"
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
            supports_streaming=True,
        )

        await msg.delete()

    except Exception as e:
        await msg.edit_text(
            "Что-то пошло не так 😔\n"
            "Попробуй другое фото — лучше всего портрет анфас с чётким лицом."
        )
        print(f"[ERROR] {type(e).__name__}: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Отправь своё фото 📸 — сделаю ИИ-версию которая поёт трек VEÉKA"
    )


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("VEÉKA AI бот запущен ✅")
    app.run_polling()


if __name__ == "__main__":
    main()
