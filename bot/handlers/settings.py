"""
/settings command — per-user bot preferences.
"""

import logging
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
try:
    from pyrogram.errors import MessageNotModified, FloodWait
except ImportError:
    MessageNotModified = Exception
    FloodWait = Exception
from client import app
from utils.settings import get, set as sset, get_all, reset

logger = logging.getLogger(__name__)

# uid → True, waiting for channel text input
_WAITING_CHANNEL: dict[int, bool] = {}


# ── Display helpers ───────────────────────────────────────────────

def _settings_text(uid: int) -> str:
    s = get_all(uid)

    upload_icon  = "📹" if s["upload_type"] == "video" else "📄"
    preset_icons = {"ultrafast": "⚡", "veryfast": "🔥", "fast": "🎯", "medium": "⚖️"}
    preset_icon  = preset_icons.get(s["preset"], "⚙️")
    crf          = s["crf"]
    quality_label = (
        "🟢 High"   if crf <= 18 else
        "🟡 Good"   if crf <= 23 else
        "🟠 Medium" if crf <= 28 else
        "🔴 Small"
    )
    res_label    = "Same as source" if s["default_res"] == "source" else f"{s['default_res']}p"
    fwd_icon     = "✅" if s["auto_forward"] else "❌"
    channel      = s["channel_id"] if s["channel_id"] else "<i>not set</i>"

    return (
        "⚙️✨ <b>SETTINGS</b> ✨⚙️\n"
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
        f"{upload_icon} <b>Upload type:</b> <code>{s['upload_type'].capitalize()}</code>\n"
        f"> Send files as Video or Document\n\n"
        f"{preset_icon} <b>Encode speed:</b> <code>{s['preset'].capitalize()}</code>\n"
        f"> FFmpeg preset — faster = larger file\n\n"
        f"🎨 <b>Quality (CRF):</b> <code>{crf}</code> — {quality_label}\n"
        f"> Lower = better quality, bigger & slower\n\n"
        f"📐 <b>Default resolution:</b> <code>{res_label}</code>\n"
        f"> Auto-applied when changing resolution\n\n"
        f"📢 <b>Forward channel:</b> {channel}\n"
        f"> ID like <code>-1001234567890</code> or <code>@username</code>\n\n"
        f"{fwd_icon} <b>Auto-forward:</b> <code>{'On' if s['auto_forward'] else 'Off'}</code>\n"
        f"> Skip confirmation, forward automatically\n\n"
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
        "<i>Tap any setting below to change it</i>"
    )


def _settings_keyboard(uid: int) -> InlineKeyboardMarkup:
    s = get_all(uid)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{'📹' if s['upload_type']=='video' else '📄'} Upload: {s['upload_type'].capitalize()}",
                callback_data="cfg:upload_type"
            ),
        ],
        [
            InlineKeyboardButton("⚡ Speed Preset",  callback_data="cfg:preset"),
            InlineKeyboardButton("🎨 Quality (CRF)", callback_data="cfg:crf"),
        ],
        [
            InlineKeyboardButton("📐 Default Res",  callback_data="cfg:default_res"),
            InlineKeyboardButton(
                f"{'✅' if s['auto_forward'] else '❌'} Auto-Forward",
                callback_data="cfg:toggle_forward"
            ),
        ],
        [
            InlineKeyboardButton("📢 Set Channel",       callback_data="cfg:set_channel"),
            InlineKeyboardButton("🔄 Reset defaults",    callback_data="cfg:reset"),
        ],
        [
            InlineKeyboardButton("✕ Close", callback_data="cfg:close"),
        ],
    ])


# ── Sub-menu keyboards ────────────────────────────────────────────

def _upload_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📹 Video",    callback_data="cfg:set:upload_type:video"),
            InlineKeyboardButton("📄 Document", callback_data="cfg:set:upload_type:document"),
        ],
        [InlineKeyboardButton("‹ Back", callback_data="cfg:back")],
    ])


def _preset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Ultrafast", callback_data="cfg:set:preset:ultrafast"),
            InlineKeyboardButton("🔥 Veryfast",  callback_data="cfg:set:preset:veryfast"),
        ],
        [
            InlineKeyboardButton("🎯 Fast",   callback_data="cfg:set:preset:fast"),
            InlineKeyboardButton("⚖️ Medium", callback_data="cfg:set:preset:medium"),
        ],
        [InlineKeyboardButton("‹ Back", callback_data="cfg:back")],
    ])


def _crf_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 High   (18)", callback_data="cfg:set:crf:18"),
            InlineKeyboardButton("🟡 Good   (23)", callback_data="cfg:set:crf:23"),
        ],
        [
            InlineKeyboardButton("🟠 Medium (28)", callback_data="cfg:set:crf:28"),
            InlineKeyboardButton("🔴 Small  (35)", callback_data="cfg:set:crf:35"),
        ],
        [InlineKeyboardButton("‹ Back", callback_data="cfg:back")],
    ])


def _res_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔁 Source", callback_data="cfg:set:default_res:source"),
            InlineKeyboardButton("🖥 1080p",  callback_data="cfg:set:default_res:1080"),
        ],
        [
            InlineKeyboardButton("📺 720p",  callback_data="cfg:set:default_res:720"),
            InlineKeyboardButton("📺 480p",  callback_data="cfg:set:default_res:480"),
            InlineKeyboardButton("📺 360p",  callback_data="cfg:set:default_res:360"),
        ],
        [InlineKeyboardButton("‹ Back", callback_data="cfg:back")],
    ])


# ── Safe edit helper ──────────────────────────────────────────────

async def _edit(cb: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup):
    try:
        await cb.message.edit(text, reply_markup=keyboard)
    except MessageNotModified:
        pass
    except FloodWait as e:
        import asyncio
        await asyncio.sleep(e.value)
        await cb.message.edit(text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Settings edit failed: {e}")
        await cb.answer("⚠️ Could not update — try again.", show_alert=True)


# ── Command handler ───────────────────────────────────────────────

@app.on_message(filters.command("settings") & filters.private)
async def cmd_settings(client: Client, msg: Message):
    uid = msg.from_user.id
    await msg.reply(
        _settings_text(uid),
        reply_markup=_settings_keyboard(uid),
    )


# ── Callback handler ──────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^cfg:"))
async def settings_callback(client: Client, cb: CallbackQuery):
    uid    = cb.from_user.id
    parts  = cb.data.split(":")
    action = parts[1]

    # ── Set a value ───────────────────────────────────────────────
    if action == "set" and len(parts) >= 4:
        key   = parts[2]
        value = ":".join(parts[3:])   # handles colons in value if any
        if key == "crf":
            value = int(value)
        sset(uid, key, value)
        await cb.answer("✅ Saved!")
        await _edit(cb, _settings_text(uid), _settings_keyboard(uid))
        return

    # ── Toggle auto-forward ───────────────────────────────────────
    if action == "toggle_forward":
        current = get(uid, "auto_forward")
        sset(uid, "auto_forward", not current)
        await cb.answer("✅ Auto-forward ON" if not current else "❌ Auto-forward OFF")
        await _edit(cb, _settings_text(uid), _settings_keyboard(uid))
        return

    # ── Set channel ───────────────────────────────────────────────
    if action == "set_channel":
        _WAITING_CHANNEL[uid] = True
        await cb.answer()
        await _edit(cb,
            "📢✨ <b>SET FORWARD CHANNEL</b> ✨📢\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "Send your channel ID or username:\n\n"
            "> <code>-1001234567890</code> — private channel ID\n"
            "> <code>@mychannel</code> — public channel username\n\n"
            "⚠️ <i>Make sure the bot is admin in the channel</i>\n\n"
            "<i>Type</i> <code>clear</code> <i>to remove the current channel</i>",
            InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data="cfg:cancel_channel")]])
        )
        return

    # ── Cancel channel input ──────────────────────────────────────
    if action == "cancel_channel":
        _WAITING_CHANNEL.pop(uid, None)
        await cb.answer()
        await _edit(cb, _settings_text(uid), _settings_keyboard(uid))
        return

    # ── Sub-menu: upload type ─────────────────────────────────────
    if action == "upload_type":
        await cb.answer()
        await _edit(cb,
            "📹 <b>UPLOAD TYPE</b>\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "> <b>Video</b> — inline player, thumbnail, duration\n"
            "> <b>Document</b> — compact, preserves filename\n\n"
            "<i>Which format do you prefer?</i>",
            _upload_type_keyboard()
        )
        return

    # ── Sub-menu: preset ──────────────────────────────────────────
    if action == "preset":
        await cb.answer()
        await _edit(cb,
            "⚡ <b>ENCODE SPEED (PRESET)</b>\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "> <b>Ultrafast</b> — fastest, larger file\n"
            "> <b>Veryfast</b> — slightly smaller, barely slower\n"
            "> <b>Fast</b> — good balance\n"
            "> <b>Medium</b> — best compression, slowest\n\n"
            "<i>Recommended: Ultrafast or Veryfast on Railway</i>",
            _preset_keyboard()
        )
        return

    # ── Sub-menu: CRF ────────────────────────────────────────────
    if action == "crf":
        await cb.answer()
        await _edit(cb,
            "🎨 <b>VIDEO QUALITY (CRF)</b>\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "> <b>High (18)</b> — near lossless, large file\n"
            "> <b>Good (23)</b> — default, great quality\n"
            "> <b>Medium (28)</b> — smaller, visible loss\n"
            "> <b>Small (35)</b> — maximum compression\n\n"
            "<i>Lower CRF = better quality, bigger file</i>",
            _crf_keyboard()
        )
        return

    # ── Sub-menu: resolution ─────────────────────────────────────
    if action == "default_res":
        await cb.answer()
        await _edit(cb,
            "📐 <b>DEFAULT RESOLUTION</b>\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "> <b>Source</b> — keep original resolution\n"
            "> <b>1080p</b> — Full HD\n"
            "> <b>720p</b> — HD, best size/quality ratio\n"
            "> <b>480p</b> — SD, small file\n"
            "> <b>360p</b> — very small, mobile-friendly\n\n"
            "<i>Applied automatically when you change resolution</i>",
            _res_keyboard()
        )
        return

    # ── Reset ─────────────────────────────────────────────────────
    if action == "reset":
        reset(uid)
        await cb.answer("🔄 Reset to defaults!")
        await _edit(cb, _settings_text(uid), _settings_keyboard(uid))
        return

    # ── Back ──────────────────────────────────────────────────────
    if action == "back":
        await cb.answer()
        await _edit(cb, _settings_text(uid), _settings_keyboard(uid))
        return

    # ── Close ─────────────────────────────────────────────────────
    if action == "close":
        await cb.answer()
        await cb.message.delete()
        return

    # ── Unknown ───────────────────────────────────────────────────
    logger.warning(f"Unknown cfg action: {cb.data}")
    await cb.answer("⚠️ Unknown action.", show_alert=True)


# ── Receive channel text input ────────────────────────────────────

@app.on_message(filters.private & filters.text & ~filters.command([
    "start", "help", "stats", "queue", "settings"
]), group=1)
async def settings_text_input(client: Client, msg: Message):
    uid = msg.from_user.id
    if uid not in _WAITING_CHANNEL:
        return

    _WAITING_CHANNEL.pop(uid)
    text = msg.text.strip()

    if text.lower() == "clear":
        sset(uid, "channel_id", "")
        await msg.reply("✅ <i>Channel removed.</i>\n\nUse /settings to configure again.")
        return

    if not (text.startswith("@") or text.lstrip("-").isdigit()):
        await msg.reply(
            "❌ <i>Invalid format.</i>\n\n"
            "Use <code>-1001234567890</code> or <code>@username</code>.\n"
            "Type /settings to try again."
        )
        return

    try:
        chat = await client.get_chat(text)
        sset(uid, "channel_id", text)
        await msg.reply(
            f"✅ <b>Channel saved!</b>\n\n"
            f"📢 <code>{chat.title}</code>\n"
            f"🆔 <code>{text}</code>\n\n"
            f"<i>Files will be forwarded here after processing.</i>"
        )
    except Exception:
        await msg.reply(
            f"❌ <b>Could not access</b> <code>{text}</code>\n\n"
            f"Make sure:\n"
            f"> Bot is admin in the channel\n"
            f"> The ID or username is correct\n\n"
            f"Type /settings to try again."
        )
