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

# uid → "add" | "remove"
_WAITING_CHANNEL: dict[int, str] = {}


# ── Display helpers ───────────────────────────────────────────────

def _settings_text(uid: int) -> str:
    s = get_all(uid)
    upload_icon   = "📹" if s["upload_type"] == "video" else "📄"
    preset_icons  = {"ultrafast": "⚡", "veryfast": "🔥", "fast": "🎯", "medium": "⚖️"}
    preset_icon   = preset_icons.get(s["preset"], "⚙️")
    crf           = s["crf"]
    quality_label = (
        "🟢 High"   if crf <= 18 else
        "🟡 Good"   if crf <= 23 else
        "🟠 Medium" if crf <= 28 else
        "🔴 Small"
    )
    fwd_icon     = "✅" if s["auto_forward"] else "❌"
    channels     = s["channel_ids"]
    ch_text      = "\n".join(f"  • <code>{c}</code>" for c in channels) if channels else "  <i>none set</i>"

    return (
        "⚙️✨ <b>SETTINGS</b> ✨⚙️\n"
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
        f"{upload_icon} <b>Upload type:</b> <code>{s['upload_type'].capitalize()}</code>\n\n"
        f"{preset_icon} <b>Encode speed:</b> <code>{s['preset'].capitalize()}</code>\n\n"
        f"🎨 <b>Quality (CRF):</b> <code>{crf}</code> — {quality_label}\n\n"
        f"{fwd_icon} <b>Auto-forward:</b> <code>{'On' if s['auto_forward'] else 'Off'}</code>\n\n"
        f"📢 <b>Forward channels:</b>\n{ch_text}\n\n"
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
            InlineKeyboardButton(
                f"{'✅' if s['auto_forward'] else '❌'} Auto-Forward",
                callback_data="cfg:toggle_forward"
            ),
            InlineKeyboardButton("📢 Channels", callback_data="cfg:channels"),
        ],
        [
            InlineKeyboardButton("🔄 Reset defaults", callback_data="cfg:reset"),
            InlineKeyboardButton("✕ Close",           callback_data="cfg:close"),
        ],
    ])


def _channels_keyboard(uid: int) -> InlineKeyboardMarkup:
    channels = get(uid, "channel_ids")
    rows = []
    for ch in channels:
        rows.append([InlineKeyboardButton(f"🗑 Remove {ch}", callback_data=f"cfg:rmch:{ch}")])
    rows.append([InlineKeyboardButton("➕ Add channel", callback_data="cfg:add_channel")])
    rows.append([InlineKeyboardButton("‹ Back", callback_data="cfg:back")])
    return InlineKeyboardMarkup(rows)


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
    await msg.reply(_settings_text(uid), reply_markup=_settings_keyboard(uid))


# ── Callback handler ──────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^cfg:"))
async def settings_callback(client: Client, cb: CallbackQuery):
    uid    = cb.from_user.id
    parts  = cb.data.split(":")
    action = parts[1]

    if action == "set" and len(parts) >= 4:
        key   = parts[2]
        value = ":".join(parts[3:])
        if key == "crf":
            value = int(value)
        sset(uid, key, value)
        await cb.answer("✅ Saved!")
        await _edit(cb, _settings_text(uid), _settings_keyboard(uid))
        return

    if action == "toggle_forward":
        current = get(uid, "auto_forward")
        sset(uid, "auto_forward", not current)
        await cb.answer("✅ Auto-forward ON" if not current else "❌ Auto-forward OFF")
        await _edit(cb, _settings_text(uid), _settings_keyboard(uid))
        return

    if action == "channels":
        await cb.answer()
        channels = get(uid, "channel_ids")
        ch_text = "\n".join(f"  • <code>{c}</code>" for c in channels) if channels else "  <i>none yet</i>"
        await _edit(cb,
            f"📢 <b>FORWARD CHANNELS</b>\n\n{ch_text}\n\n"
            "<i>Add up to 10 channels. Bot must be admin in each.</i>",
            _channels_keyboard(uid)
        )
        return

    if action == "add_channel":
        _WAITING_CHANNEL[uid] = "add"
        await cb.answer()
        await _edit(cb,
            "📢 <b>ADD CHANNEL</b>\n\n"
            "Send the channel ID or username:\n\n"
            "> <code>-1001234567890</code> — private channel\n"
            "> <code>@mychannel</code> — public channel\n\n"
            "⚠️ <i>Make sure the bot is admin in the channel</i>",
            InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data="cfg:cancel_channel")]])
        )
        return

    if action == "rmch" and len(parts) >= 3:
        ch = ":".join(parts[2:])
        channels = get(uid, "channel_ids")
        channels = [c for c in channels if c != ch]
        sset(uid, "channel_ids", channels)
        await cb.answer(f"🗑 Removed {ch}")
        ch_text = "\n".join(f"  • <code>{c}</code>" for c in channels) if channels else "  <i>none yet</i>"
        await _edit(cb,
            f"📢 <b>FORWARD CHANNELS</b>\n\n{ch_text}\n\n"
            "<i>Add up to 10 channels. Bot must be admin in each.</i>",
            _channels_keyboard(uid)
        )
        return

    if action == "cancel_channel":
        _WAITING_CHANNEL.pop(uid, None)
        await cb.answer()
        await _edit(cb, _settings_text(uid), _settings_keyboard(uid))
        return

    if action == "upload_type":
        await cb.answer()
        await _edit(cb,
            "📹 <b>UPLOAD TYPE</b>\n\n"
            "> <b>Video</b> — inline player, thumbnail\n"
            "> <b>Document</b> — compact, preserves filename",
            _upload_type_keyboard()
        )
        return

    if action == "preset":
        await cb.answer()
        await _edit(cb,
            "⚡ <b>ENCODE SPEED</b>\n\n"
            "> Ultrafast — fastest, larger file\n"
            "> Fast — good balance\n"
            "> Medium — best compression, slowest",
            _preset_keyboard()
        )
        return

    if action == "crf":
        await cb.answer()
        await _edit(cb,
            "🎨 <b>VIDEO QUALITY (CRF)</b>\n\n"
            "> High (18) — near lossless\n"
            "> Good (23) — default\n"
            "> Medium (28) — smaller file\n"
            "> Small (35) — maximum compression",
            _crf_keyboard()
        )
        return

    if action == "reset":
        reset(uid)
        await cb.answer("🔄 Reset to defaults!")
        await _edit(cb, _settings_text(uid), _settings_keyboard(uid))
        return

    if action == "back":
        await cb.answer()
        await _edit(cb, _settings_text(uid), _settings_keyboard(uid))
        return

    if action == "close":
        await cb.answer()
        await cb.message.delete()
        return

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

    if not (text.startswith("@") or text.lstrip("-").isdigit()):
        await msg.reply(
            "❌ <i>Invalid format.</i>\n\n"
            "Use <code>-1001234567890</code> or <code>@username</code>.\n"
            "Type /settings to try again."
        )
        return

    try:
        chat     = await client.get_chat(text)
        channels = get(uid, "channel_ids")
        if text not in channels:
            if len(channels) >= 10:
                await msg.reply("❌ Max 10 channels. Remove one first via /settings.")
                return
            channels.append(text)
            sset(uid, "channel_ids", channels)
        await msg.reply(
            f"✅ <b>Channel added!</b>\n\n"
            f"📢 <code>{chat.title}</code>\n"
            f"🆔 <code>{text}</code>\n\n"
            f"<i>Total channels: {len(channels)}</i>"
        )
    except Exception:
        await msg.reply(
            f"❌ <b>Could not access</b> <code>{text}</code>\n\n"
            f"Make sure the bot is admin in the channel.\n"
            f"Type /settings to try again."
        )
