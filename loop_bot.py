#!/usr/bin/env python3
"""
Mom's Audio Looper & Shloka Splitter Bot
========================================
- Automatically detects full 4-line shlokas (~20-22 seconds each).
- Never cuts half-shlokas! Enforces full 4-line verse meter.
- Detects if an audio already has repeated batches (e.g. 225s block) and extracts
  the clean unique verses.
- 1-Tap "🎧 S1 (7x) ➔ S2 (7x) ➔ S3 (7x)... (All in 1 File)":
    Repeats Shloka 1 (7x), then Shloka 2 (7x), then Shloka 3 (7x)... into ONE continuous file!
- Also supports separate files: <filename>_s1.mp3, <filename>_s2.mp3...
- Natural 1.2s pause between repeats, 2.5s transition pause between shlokas.
- Zero audio pops / clicks (30ms micro-fades).
- Generous 5-minute timeouts.
"""

import os
import re
import shutil
import asyncio
import logging
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("MomAudioBot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    # Check if .env file exists locally
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                if line.startswith("BOT_TOKEN="):
                    BOT_TOKEN = line.strip().split("=", 1)[1]
                    break

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required. Set BOT_TOKEN environment variable or put it in .env")

ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

MAX_LOOPS = 50
MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Responds to Fly.io or Render HTTP health checks."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("🌸 Mom's Audio Bot is Live and Running 24/7 on Fly.io!\n".encode("utf-8"))

    def log_message(self, format, *args):
        pass  # Quiet logging


def start_health_check_server(port: int = 8080):
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Health check HTTP server listening on 0.0.0.0:{port}")
    except Exception as e:
        logger.warning(f"Could not start HTTP server on port {port}: {e}")


def get_binary_path(name: str) -> str:
    """Find executable in PATH or standard Homebrew paths."""
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        candidate = os.path.join(prefix, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return name


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


def get_audio_duration(input_path: str) -> float:
    """Get total duration using ffprobe."""
    ffprobe_bin = get_binary_path("ffprobe")
    probe_cmd = [
        ffprobe_bin,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        input_path,
    ]
    res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        return float(res.stdout.strip())
    except Exception:
        return 60.0


def detect_all_pauses(input_path: str, max_check_seconds: float = None):
    """Find all pause endpoints using FFmpeg."""
    ffmpeg_bin = get_binary_path("ffmpeg")
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
    ]
    if max_check_seconds:
        cmd.extend(["-t", str(max_check_seconds)])
    cmd.extend([
        "-i", input_path,
        "-af", "silencedetect=noise=-25dB:d=0.35",
        "-f", "null",
        "-",
    ])
    result = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)

    pauses = []
    for line in result.stderr.splitlines():
        if "silence_end" in line:
            m = re.search(r"silence_end:\s*([0-9.]+)", line)
            if m:
                pauses.append(float(m.group(1)))

    return pauses


def detect_first_cycle_boundary(input_path: str, total_duration: float):
    """
    If an audio has a long silence (> 1.8s) indicating the entire batch repeated,
    extract only the first cycle of unique shlokas.
    """
    ffmpeg_bin = get_binary_path("ffmpeg")
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-i", input_path,
        "-af", "silencedetect=noise=-28dB:d=1.5",
        "-f", "null",
        "-",
    ]
    result = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)

    major_pauses = []
    for line in result.stderr.splitlines():
        if "silence_end" in line:
            m = re.search(r"silence_end:\s*([0-9.]+)", line)
            if m:
                major_pauses.append(float(m.group(1)))

    # If we found repeating batch pauses (e.g. at 225s in a 1574s file)
    if major_pauses and major_pauses[0] > 60.0 and total_duration > major_pauses[0] * 2:
        logger.info(f"Detected repeating batch file. First cycle ends at: {major_pauses[0]:.2f}s")
        return major_pauses[0]

    return total_duration


def split_into_full_shlokas(input_path: str, output_dir: str, total_duration: float, target_shlokas: int = None):
    """
    Splits audio into FULL 4-LINE SHLOKAS (~20-22 seconds each).
    - Ignores half-shloka pauses (which happen at ~11s).
    - Enforces full 4-line verse boundaries.
    """
    ffmpeg_bin = get_binary_path("ffmpeg")

    # 1. Determine active portion (e.g. 225s if the audio repeats 7 times)
    cycle_duration = detect_first_cycle_boundary(input_path, total_duration)

    # 2. Detect line pauses in this portion
    pauses = detect_all_pauses(input_path, max_check_seconds=cycle_duration)

    if not pauses:
        out_single = os.path.join(output_dir, "shloka_1.mp3")
        shutil.copy(input_path, out_single)
        return [out_single]

    # 3. Determine ideal shloka duration
    # In Vedic / Sahasranama chanting, a full 4-line shloka is typically 18 to 25 seconds.
    if target_shlokas and target_shlokas > 0:
        expected_dur = cycle_duration / target_shlokas
        min_threshold = max(14.0, expected_dur * 0.85)
    else:
        # Default: full shloka is at least 18.0 seconds
        min_threshold = 18.0

    full_shlokas_intervals = []
    current_start = 0.0

    for p in pauses:
        dur_so_far = p - current_start
        if dur_so_far >= min_threshold:
            full_shlokas_intervals.append((current_start, p))
            current_start = p

    if cycle_duration - current_start >= 12.0:
        full_shlokas_intervals.append((current_start, cycle_duration))

    # If it found only 1 segment, fallback to equal-length splitting by target count or 22s
    if len(full_shlokas_intervals) <= 1:
        if not target_shlokas:
            # Estimate count by 22 seconds per shloka
            target_shlokas = max(1, round(cycle_duration / 22.5))

        if target_shlokas > 1:
            interval_len = cycle_duration / target_shlokas
            full_shlokas_intervals = []
            for i in range(target_shlokas):
                s = i * interval_len
                e = (i + 1) * interval_len if i < target_shlokas - 1 else cycle_duration
                full_shlokas_intervals.append((s, e))

    segment_paths = []
    for idx, (start, end) in enumerate(full_shlokas_intervals, start=1):
        out_path = os.path.join(output_dir, f"shloka_{idx}.mp3")
        dur = end - start
        cmd = [
            ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(start), "-i", input_path, "-t", str(dur),
            "-c:a", "libmp3lame", "-b:a", "128k", out_path,
        ]
        subprocess.run(cmd, check=True)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            segment_paths.append(out_path)

    if not segment_paths:
        out_single = os.path.join(output_dir, "shloka_1.mp3")
        shutil.copy(input_path, out_single)
        segment_paths.append(out_single)

    return segment_paths


def loop_audio(src: str, out: str, n: int, pause_seconds: float = 1.2) -> None:
    """Loop src n times with micro-fades (no pop/glitch) and a natural pause between repeats."""
    ffmpeg_bin = get_binary_path("ffmpeg")
    src_duration = get_audio_duration(src)
    fade_out_st = max(0.0, src_duration - 0.03)

    unit_path = out + ".unit.mp3"
    af_filter = f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out_st:.3f}:d=0.03,apad=pad_dur={pause_seconds}"

    prep_cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src, "-af", af_filter, "-c:a", "libmp3lame", "-b:a", "128k", unit_path,
    ]
    res_prep = subprocess.run(prep_cmd, capture_output=True, text=True)
    if res_prep.returncode != 0 or not os.path.exists(unit_path):
        extra_loops = n - 1
        subprocess.run([
            ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-stream_loop", str(extra_loops), "-i", src, "-c", "copy", out
        ], check=True)
        return

    try:
        extra_loops = n - 1
        loop_cmd = [
            ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-stream_loop", str(extra_loops), "-i", unit_path, "-c", "copy", out,
        ]
        res_loop = subprocess.run(loop_cmd, capture_output=True)
        if res_loop.returncode != 0 or not (os.path.exists(out) and os.path.getsize(out) > 0):
            subprocess.run([
                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-stream_loop", str(extra_loops), "-i", unit_path, out
            ], check=True)
    finally:
        if os.path.exists(unit_path):
            os.remove(unit_path)


def concat_audio_files(file_paths: list, out_path: str, transition_pause: float = 2.5) -> None:
    """Combine multiple audio tracks into ONE single continuous file with transition pauses."""
    ffmpeg_bin = get_binary_path("ffmpeg")
    tmp_dir = os.path.dirname(out_path)
    concat_list = os.path.join(tmp_dir, "concat.txt")

    silence_file = os.path.join(tmp_dir, "silence_pause.mp3")
    silence_cmd = [
        ffmpeg_bin, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", str(transition_pause), "-c:a", "libmp3lame", "-b:a", "128k", silence_file,
    ]
    subprocess.run(silence_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    with open(concat_list, "w") as f:
        for idx, p in enumerate(file_paths):
            f.write(f"file '{p}'\n")
            if idx < len(file_paths) - 1 and os.path.exists(silence_file):
                f.write(f"file '{silence_file}'\n")

    cmd = [
        ffmpeg_bin, "-y", "-f", "concat", "-safe", "0",
        "-i", concat_list, "-c", "copy", out_path,
    ]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0 or not (os.path.exists(out_path) and os.path.getsize(out_path) > 0):
        # Fallback to re-encode
        cmd_fallback = [
            ffmpeg_bin, "-y", "-f", "concat", "-safe", "0",
            "-i", concat_list, "-c:a", "libmp3lame", "-b:a", "128k", out_path,
        ]
        subprocess.run(cmd_fallback, check=True)


def build_shlokas_keyboard(num_segments: int) -> InlineKeyboardMarkup:
    """Generate keyboard for picking shlokas."""
    keyboard = [
        # Top option: Sequential practice track
        [InlineKeyboardButton(f"🎧 Play S1(7x) ➔ S2(7x) ➔ S{num_segments}(7x)... in 1 File", callback_data="select_shloka_all_in_one")],
        [InlineKeyboardButton("📁 Send as Separate Files (S1 7x, S2 7x...)", callback_data="select_shloka_each_all")],
    ]
    # Individual shloka buttons
    row = []
    for idx in range(1, num_segments + 1):
        row.append(InlineKeyboardButton(f"🌸 Shloka {idx}", callback_data=f"select_shloka_{idx-1}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Change count button
    keyboard.append([InlineKeyboardButton("🔢 Change Shloka Count (e.g. 5, 8, 10)", callback_data="menu_change_count")])
    keyboard.append([InlineKeyboardButton("🔁 Repeat Entire Recording (As Is)", callback_data="select_shloka_all")])

    return InlineKeyboardMarkup(keyboard)


def build_repeat_keyboard(show_back: bool = True) -> InlineKeyboardMarkup:
    """Generate repeat count buttons with optional Back button."""
    keyboard = [
        [
            InlineKeyboardButton("🔁 3x", callback_data="loop_3"),
            InlineKeyboardButton("🔁 5x", callback_data="loop_5"),
            InlineKeyboardButton("🔁 7x", callback_data="loop_7"),
        ],
        [
            InlineKeyboardButton("🔁 11x", callback_data="loop_11"),
            InlineKeyboardButton("🔁 21x", callback_data="loop_21"),
            InlineKeyboardButton("🔁 50x", callback_data="loop_50"),
        ],
    ]
    if show_back:
        keyboard.append([InlineKeyboardButton("⬅️ Back to Shlokas List", callback_data="menu_back_shlokas")])
    return InlineKeyboardMarkup(keyboard)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id if user else "Unknown"
    await update.message.reply_text(
        "🌸 *Namaste! Welcome to Mom's Audio & Shloka Bot!* 🌸\n\n"
        "Send or forward your audio recording here:\n"
        "• The bot automatically identifies full 4-line verses (~22s each).\n"
        "• Then tap **`🎧 Play S1(7x) ➔ S2(7x)... in 1 File`** to get your complete practice track!\n\n"
        f"_(Your Telegram ID: `{uid}`)_",
        parse_mode="Markdown",
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming photos without asking repetitive questions."""
    if not is_allowed(update):
        return

    msg = update.message
    photo = msg.photo[-1]

    temp_dir = context.user_data.get("temp_dir") or tempfile.mkdtemp()
    context.user_data["temp_dir"] = temp_dir
    photo_path = os.path.join(temp_dir, "lyrics.jpg")

    try:
        tg_file = await context.bot.get_file(photo.file_id, read_timeout=60, write_timeout=60)
        await tg_file.download_to_drive(photo_path, read_timeout=60, write_timeout=60)
        context.user_data["lyrics_photo_path"] = photo_path
    except Exception as e:
        logger.warning(f"Could not download photo: {e}")

    input_path = context.user_data.get("input_path")
    if input_path and os.path.exists(input_path):
        await msg.reply_text(
            "📸 *Got your lyrics photo!*\n\nYour audio is ready. Choose an option below:",
            reply_markup=build_shlokas_keyboard(len(context.user_data.get("segments", []))),
        )
    else:
        await msg.reply_text(
            "📸 *Got your lyrics photo!*\n\nNow please send or forward your **audio recording**."
        )


async def handle_media_or_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified handler for all audio and document messages."""
    if not is_allowed(update):
        await update.message.reply_text("⛔ Sorry, you are not authorized to use this bot.")
        return

    msg = update.message
    title = None
    performer = None
    media = None
    kind = "audio"

    if msg.audio:
        media = msg.audio
        name = media.file_name or "recording.mp3"
        kind = "audio"
        title = media.title
        performer = media.performer
    elif msg.voice:
        media = msg.voice
        name = "voice_recording.ogg"
        kind = "voice"
    elif msg.document:
        mime = msg.document.mime_type or ""
        if mime.startswith("image/"):
            await handle_photo(update, context)
            return
        elif mime.startswith("audio/") or any(name_ext in (msg.document.file_name or "").lower() for name_ext in (".mp3", ".m4a", ".wav", ".aac", ".ogg")):
            media = msg.document
            name = media.file_name or "recording.mp3"
            kind = "document"
        else:
            await msg.reply_text("Please send an audio recording (or photo of lyrics).")
            return
    else:
        await msg.reply_text("Please send an audio recording (or photo of lyrics).")
        return

    clean_name = Path(name).name
    if not Path(clean_name).suffix:
        clean_name += ".mp3"

    status_msg = await msg.reply_text("📥 *Analyzing recording and detecting full 4-line shlokas...* Please wait.", parse_mode="Markdown")

    old_dir = context.user_data.get("temp_dir")
    if old_dir and os.path.exists(old_dir):
        shutil.rmtree(old_dir, ignore_errors=True)

    temp_dir = tempfile.mkdtemp()
    input_path = os.path.join(temp_dir, clean_name)

    try:
        tg_file = await context.bot.get_file(media.file_id, read_timeout=300, write_timeout=300)
        await tg_file.download_to_drive(input_path, read_timeout=300, write_timeout=300)

        total_duration = await asyncio.to_thread(get_audio_duration, input_path)
        shlokas_dir = os.path.join(temp_dir, "split_shlokas")
        os.makedirs(shlokas_dir, exist_ok=True)

        # Check if the filename mentions shloka numbers (e.g. SL 91-100 = 10 shlokas)
        target_shlokas = context.user_data.get("target_shlokas")
        if not target_shlokas:
            m_range = re.search(r"(\d+)\s*-\s*(\d+)", clean_name)
            if m_range:
                start_n, end_n = int(m_range.group(1)), int(m_range.group(2))
                if end_n >= start_n and (end_n - start_n + 1) <= 25:
                    target_shlokas = end_n - start_n + 1
                    logger.info(f"Auto-detected {target_shlokas} shlokas from filename range {start_n}-{end_n}")

        segment_paths = await asyncio.to_thread(
            split_into_full_shlokas, input_path, shlokas_dir, total_duration, target_shlokas
        )

        context.user_data["temp_dir"] = temp_dir
        context.user_data["input_path"] = input_path
        context.user_data["segments"] = segment_paths
        context.user_data["original_name"] = clean_name
        context.user_data["original_stem"] = Path(clean_name).stem
        context.user_data["original_suffix"] = Path(clean_name).suffix or ".mp3"
        context.user_data["kind"] = kind
        context.user_data["title"] = title
        context.user_data["performer"] = performer
        context.user_data["awaiting_repeat_count"] = False

        if len(segment_paths) > 1:
            reply_markup = build_shlokas_keyboard(len(segment_paths))
            await status_msg.edit_text(
                f"✨ Separated into **{len(segment_paths)} full 4-line shlokas**!\n\n"
                f"Tap below to generate your practice track:",
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
        else:
            context.user_data["selected_audio_path"] = input_path
            context.user_data["selected_idx"] = None
            context.user_data["selected_label"] = Path(clean_name).stem
            context.user_data["mode"] = "single"
            context.user_data["awaiting_repeat_count"] = True
            await status_msg.edit_text(
                f"🎵 Got: *“{clean_name}”*\n\nTap how many times to repeat it, or type any number (e.g. *7*):",
                reply_markup=build_repeat_keyboard(show_back=False),
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.exception("Error analyzing audio")
        await status_msg.edit_text(f"⚠️ Error processing audio: {e}")


async def handle_target_count_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User specified how many shlokas are in the recording."""
    query = update.callback_query
    await query.answer()

    data = query.data
    count = int(data.split("_")[-1])
    context.user_data["target_shlokas"] = count

    input_path = context.user_data.get("input_path")
    temp_dir = context.user_data.get("temp_dir")

    if not input_path or not os.path.exists(input_path):
        await query.edit_message_text(f"✅ Set to {count} shlokas! Now please send your audio recording.")
        return

    await query.edit_message_text(f"⏳ Splitting recording into **{count} exact 4-line shlokas**... Please wait.")

    total_duration = await asyncio.to_thread(get_audio_duration, input_path)
    shlokas_dir = os.path.join(temp_dir, f"split_target_{count}")
    os.makedirs(shlokas_dir, exist_ok=True)

    segment_paths = await asyncio.to_thread(
        split_into_full_shlokas, input_path, shlokas_dir, total_duration, count
    )

    context.user_data["segments"] = segment_paths

    reply_markup = build_shlokas_keyboard(len(segment_paths))
    await query.edit_message_text(
        f"✨ Separated into **{len(segment_paths)} exact shlokas**!\n\n"
        f"Choose how to play your shlokas:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def handle_shloka_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped an action or shloka."""
    query = update.callback_query
    await query.answer()

    data = query.data
    segments = context.user_data.get("segments", [])
    input_path = context.user_data.get("input_path")

    if data == "menu_back_shlokas":
        context.user_data["awaiting_repeat_count"] = False
        reply_markup = build_shlokas_keyboard(len(segments))
        await query.edit_message_text(
            f"✨ Pick an option below:",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
        return

    if data == "menu_change_count":
        keyboard = [
            [
                InlineKeyboardButton("2 Shlokas", callback_data="target_count_2"),
                InlineKeyboardButton("3 Shlokas", callback_data="target_count_3"),
                InlineKeyboardButton("4 Shlokas", callback_data="target_count_4"),
            ],
            [
                InlineKeyboardButton("5 Shlokas", callback_data="target_count_5"),
                InlineKeyboardButton("8 Shlokas", callback_data="target_count_8"),
                InlineKeyboardButton("10 Shlokas", callback_data="target_count_10"),
            ],
            [InlineKeyboardButton("⬅️ Back to Shlokas Menu", callback_data="menu_back_shlokas")],
        ]
        await query.edit_message_text(
            "How many shlokas are in this recording? Tap a number below:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "select_shloka_all_in_one":
        context.user_data["mode"] = "all_in_one"
        context.user_data["selected_label"] = "ALL Shlokas (Combined into 1 Continuous File)"
        label_text = "creating **1 Continuous File: S1 (7x) ➔ S2 (7x) ➔ S3 (7x)...**"
    elif data == "select_shloka_each_all":
        context.user_data["mode"] = "separate_all"
        context.user_data["selected_label"] = "ALL Shlokas (Separate Files)"
        label_text = "**EACH shloka sent as separate files**"
    elif data == "select_shloka_all":
        context.user_data["mode"] = "entire_recording"
        context.user_data["selected_idx"] = None
        context.user_data["selected_audio_path"] = input_path
        context.user_data["selected_label"] = "Entire Recording"
        label_text = "the **Entire Recording (As Is)**"
    else:
        idx = int(data.split("_")[-1])
        context.user_data["mode"] = "individual"
        context.user_data["selected_idx"] = idx + 1
        if idx < len(segments):
            context.user_data["selected_audio_path"] = segments[idx]
            context.user_data["selected_label"] = f"Shloka {idx+1}"
            label_text = f"**Shloka {idx + 1}**"
        else:
            context.user_data["selected_audio_path"] = input_path
            context.user_data["selected_label"] = "Audio"
            label_text = "**Audio**"

    context.user_data["awaiting_repeat_count"] = True
    show_back = len(segments) > 1
    await query.edit_message_text(
        text=f"Selected {label_text} 🌸\n\nHow many times should each verse repeat? Tap a button or type a number (e.g. *7*):",
        reply_markup=build_repeat_keyboard(show_back=show_back),
        parse_mode="Markdown",
    )


async def execute_loop(chat_id: int, message_to_edit, context: ContextTypes.DEFAULT_TYPE, count: int):
    """Perform audio looping and delivery."""
    context.user_data["awaiting_repeat_count"] = False
    mode = context.user_data.get("mode", "individual")
    segments = context.user_data.get("segments", [])
    temp_dir = context.user_data.get("temp_dir")
    kind = context.user_data.get("kind", "audio")
    performer = context.user_data.get("performer") or "Mom Audio Looper"
    base_stem = context.user_data.get("original_stem", "audio")
    suffix = context.user_data.get("original_suffix", ".mp3")

    if not 1 <= count <= MAX_LOOPS:
        await context.bot.send_message(chat_id=chat_id, text=f"Please choose a number between 1 and {MAX_LOOPS}.")
        return

    # 1. Continuous All-in-One Practice File
    if mode == "all_in_one":
        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Building **1 Continuous File** (repeating each of the {len(segments)} shlokas {count} times)... Please wait.",
            parse_mode="Markdown",
        )
        try:
            looped_shloka_paths = []
            for idx, seg_path in enumerate(segments, start=1):
                tmp_shloka_out = os.path.join(temp_dir or tempfile.gettempdir(), f"loop_s{idx}.mp3")
                await asyncio.to_thread(loop_audio, seg_path, tmp_shloka_out, count, 1.2)
                looped_shloka_paths.append(tmp_shloka_out)

            final_master_name = f"{base_stem}_All_Shlokas{suffix}"
            final_master_path = os.path.join(temp_dir or tempfile.gettempdir(), final_master_name)

            await asyncio.to_thread(concat_audio_files, looped_shloka_paths, final_master_path, 2.5)

            with open(final_master_path, "rb") as fh:
                caption = f"✅ Here is your **Complete Practice File** with all {len(segments)} shlokas repeated {count} times!"
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=fh,
                    filename=final_master_name,
                    title=f"{base_stem} (Complete Practice)",
                    performer=performer,
                    caption=caption,
                    parse_mode="Markdown",
                    read_timeout=300,
                    write_timeout=300,
                )

            await status_msg.delete()

            reply_markup = build_shlokas_keyboard(len(segments))
            await context.bot.send_message(
                chat_id=chat_id,
                text="🌸 Practice file sent! You can also loop individual shlokas from this recording below:",
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception("Error creating all-in-one file")
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error creating file: {e}")
        return

    # 2. Separate Files for Each Shloka
    if mode == "separate_all":
        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Processing **ALL {len(segments)} shlokas** repeated {count} times each... Please hold on.",
            parse_mode="Markdown",
        )
        try:
            for idx, seg_path in enumerate(segments, start=1):
                out_name = f"{base_stem}_s{idx}{suffix}"
                out_path = os.path.join(temp_dir or tempfile.gettempdir(), out_name)
                await asyncio.to_thread(loop_audio, seg_path, out_path, count, 1.2)

                with open(out_path, "rb") as fh:
                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=fh,
                        filename=out_name,
                        title=f"{base_stem}_s{idx}",
                        performer=performer,
                        caption=f"🌸 {out_name}",
                        read_timeout=300,
                        write_timeout=300,
                    )
            await status_msg.delete()
        except Exception as e:
            logger.exception("Error in batch processing")
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error: {e}")

        if len(segments) > 1:
            reply_markup = build_shlokas_keyboard(len(segments))
            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ **All shlokas sent!**\n\nNeed anything else from this recording? Pick an option below:",
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
        return

    # 3. Individual Shloka or Single Recording
    target_path = context.user_data.get("selected_audio_path")
    label = context.user_data.get("selected_label", "Shloka")
    selected_idx = context.user_data.get("selected_idx")

    if not target_path or not os.path.exists(target_path):
        await context.bot.send_message(chat_id=chat_id, text="Please send an audio recording first!")
        return

    status_text = f"⏳ Repeating *{label}* ({count} times)... Please wait."
    if message_to_edit:
        try:
            await message_to_edit.edit_text(status_text, parse_mode="Markdown")
            status_msg = message_to_edit
        except Exception:
            status_msg = await context.bot.send_message(chat_id=chat_id, text=status_text, parse_mode="Markdown")
    else:
        status_msg = await context.bot.send_message(chat_id=chat_id, text=status_text, parse_mode="Markdown")

    if selected_idx is not None:
        out_file_name = f"{base_stem}_s{selected_idx}{suffix}"
        track_title = f"{base_stem}_s{selected_idx}"
    else:
        out_file_name = f"{base_stem}{suffix}"
        track_title = base_stem

    out_path = os.path.join(temp_dir or tempfile.gettempdir(), out_file_name)

    try:
        await asyncio.to_thread(loop_audio, target_path, out_path, count, 1.2)

        with open(out_path, "rb") as fh:
            caption = f"✅ Here is *{out_file_name}*!"
            if kind == "voice":
                await context.bot.send_voice(chat_id=chat_id, voice=fh, caption=caption, parse_mode="Markdown", read_timeout=300, write_timeout=300)
            else:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=fh,
                    filename=out_file_name,
                    title=track_title,
                    performer=performer,
                    caption=caption,
                    parse_mode="Markdown",
                    read_timeout=300,
                    write_timeout=300,
                )

        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

        if len(segments) > 1:
            reply_markup = build_shlokas_keyboard(len(segments))
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🌸 Sent *{out_file_name}*!\n\nPick the **next shloka** to loop from this recording:",
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )

    except Exception as e:
        logger.exception("Error while looping audio")
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Something went wrong: {e}")


async def handle_loop_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 1-tap quick button repeat count."""
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        await query.message.reply_text("⛔ Not authorized.")
        return

    data = query.data
    if data and data.startswith("loop_"):
        count = int(data.split("_")[1])
        await execute_loop(query.message.chat_id, query.message, context, count)


async def handle_typed_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle typed text and numbers gracefully."""
    if not is_allowed(update):
        return

    text = update.message.text.strip()

    if text.isdigit():
        count = int(text)
        if context.user_data.get("awaiting_repeat_count") and context.user_data.get("input_path"):
            await execute_loop(update.message.chat_id, None, context, count)
            return

    input_path = context.user_data.get("input_path")
    if input_path and os.path.exists(input_path):
        segments = context.user_data.get("segments", [])
        if len(segments) > 1:
            reply_markup = build_shlokas_keyboard(len(segments))
            await update.message.reply_text(
                "🌸 Your audio is ready! Please pick an option from the buttons below:",
                reply_markup=reply_markup,
            )
        else:
            await update.message.reply_text(
                "Please tap a repeat number above (like 7x) or type a number."
            )
    else:
        await update.message.reply_text(
            "Please send or forward an audio recording to get started! 🌸"
        )


def main():
    logger.info("Starting Mom's Audio Looper & Shloka Splitter Bot...")

    # Start health check server on port 8080 for Fly.io
    port = int(os.environ.get("PORT", "8080"))
    start_health_check_server(port)

    request = HTTPXRequest(
        connection_pool_size=10,
        connect_timeout=60.0,
        read_timeout=300.0,
        write_timeout=300.0,
        media_write_timeout=300.0,
    )
    app = Application.builder().token(BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start_command))
    # Photos
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    # Unified handler for Audio, Voice, Documents
    app.add_handler(
        MessageHandler(
            filters.AUDIO | filters.VOICE | (filters.Document.ALL & ~filters.COMMAND),
            handle_media_or_document,
        )
    )
    # Target shloka count buttons
    app.add_handler(CallbackQueryHandler(handle_target_count_button, pattern=r"^target_count_"))
    # Shloka selection, back, and change count menu
    app.add_handler(CallbackQueryHandler(handle_shloka_selected, pattern=r"^(select_shloka_|menu_back_shlokas|menu_change_count)"))
    # Loop count buttons
    app.add_handler(CallbackQueryHandler(handle_loop_button, pattern=r"^loop_"))
    # Text and typed numbers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_typed_text))

    logger.info("Bot is active and polling Telegram updates! Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
