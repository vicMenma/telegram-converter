"""
/settings command — per-user bot preferences.
"""

import os
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from client import app
from utils.settings import get, set as sset, get_all, reset, DEFAULTS

# uid → message_id waiting for channel input
_WAITING_CHANNEL: dict[int, int] = {}


# ── Helper: build the full settings menu ─────────────────────────

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
    res_label = "Same as source" if s["default_res"] == "source" else f"{s['default_res']}p"
    forward_icon = "✅" if s["auto_forward"] else "❌"

    return (
        "⚙️✨ **SETTINGS** ✨⚙️\n"
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
        f"{upload_icon} **Upload type:** `{s['upload_type'].capitalize()}`\n"
        f"> Send files as Video or Document\n\n"
        f"{preset_icon} **Encode speed:** `{s['preset'].capitalize()}`\n"
        f"> FFmpeg preset — faster = larger file\n\n"
        f"🎨 **Quality (CRF):** `{crf}` — {quality_label}\n"
        f"> Lower = better quality, slower & bigger\n\n"
        f"📐 **Default resolution:** `{res_label}`\n"
        f"> Auto-applied when changing resolution\n\n"
        f"{forward_icon} **Auto-forward to channel:** `{'On' if s['auto_forward'] else 'Off'}`\n"
        f"> Skip confirmation, forward automatically\n\n"
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
        "_Tap any setting below to change it_"
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
            InlineKeyboardButton("📐 Default Res",   callback_data="cfg:default_res"),
            InlineKeyboardButton(
                f"{'✅' if s['auto_forward'] else '❌'} Auto-Forward",
                callback_data="cfg:toggle_forward"
            ),
        ],
        [
            InlineKeyboardButton("📢 Set Channel",   callback_data="cfg:set_channel"),
        ],
        [
            InlineKeyboardButton("🔄 Reset to defaults", callback_data="cfg:reset"),
            InlineKeyboardButton("✕ Close",               callback_data="cfg:close"),
        ],
    ])


# ── Sub-menus ─────────────────────────────────────────────────────

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
            InlineKeyboardButton("🟢 High   (CRF 18)", callback_data="cfg:set:crf:18"),
            InlineKeyboardButton("🟡 Good   (CRF 23)", callback_data="cfg:set:crf:23"),
        ],
        [
            InlineKeyboardButton("🟠 Medium (CRF 28)", callback_data="cfg:set:crf:28"),
            InlineKeyboardButton("🔴 Small  (CRF 35)", callback_data="cfg:set:crf:35"),
        ],
        [InlineKeyboardButton("‹ Back", callback_data="cfg:back")],
    ])


def _res_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔁 Source",  callback_data="cfg:set:default_res:source"),
            InlineKeyboardButton("📺 1080p",   callback_data="cfg:set:default_res:1080"),
        ],
        [
            InlineKeyboardButton("📺 720p",  callback_data="cfg:set:default_res:720"),
            InlineKeyboardButton("📺 480p",  callback_data="cfg:set:default_res:480"),
            InlineKeyboardButton("📺 360p",  callback_data="cfg:set:default_res:360"),
        ],
        [InlineKeyboardButton("‹ Back", callback_data="cfg:back")],
    ])


# ── Handlers ──────────────────────────────────────────────────────

@app.on_message(filters.command("settings") & filters.private)
async def cmd_settings(client: Client, msg: Message):
    uid = msg.from_user.id
    await msg.reply(
        _settings_text(uid),
        reply_markup=_settings_keyboard(uid),
    )


@app.on_callback_query(filters.regex(r"^cfg:"))
async def settings_callback(client: Client, cb: CallbackQuery):
    uid   = cb.from_user.id
    parts = cb.data.split(":")
    action = parts[1]

    # ── Set a value ───────────────────────────────────────────────
    if action == "set":
        key   = parts[2]
        value = parts[3]
        if key == "crf":
            value = int(value)
        sset(uid, key, value)
        await cb.answer(f"✅ Saved!")
        await cb.message.edit(
            _settings_text(uid),
            reply_markup=_settings_keyboard(uid),
        )
        return

    # ── Set channel ──────────────────────────────────────────────
    if action == "set_channel":
        await cb.answer()
        from utils.settings import set as sset
        # Store state so next text message is treated as channel input
        _WAITING_CHANNEL[uid] = cb.message.id
        await cb.message.edit(
            "📢✨ **SET FORWARD CHANNEL** ✨📢\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "Send your channel ID or username:\n\n"
            "> `-1001234567890` — private channel ID\n"
            "> `@mychannel` — public channel username\n\n"
            "⚠️ _Make sure the bot is admin in the channel_\n\n"
            "_Type_ `clear` _to remove the current channel_",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✕ Cancel", callback_data="cfg:back")]
            ])
        )
        return

    # ── Toggle auto-forward ───────────────────────────────────────
    if action == "toggle_forward":
        current = get(uid, "auto_forward")
        sset(uid, "auto_forward", not current)
        await cb.answer(f"{'✅ Auto-forward ON' if not current else '❌ Auto-forward OFF'}")
        await cb.message.edit(
            _settings_text(uid),
            reply_markup=_settings_keyboard(uid),
        )
        return

    # ── Sub-menu: upload type ─────────────────────────────────────
    if action == "upload_type":
        await cb.answer()
        await cb.message.edit(
            "📹 **UPLOAD TYPE**\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "> **Video** — shows inline player, thumbnail, duration\n"
            "> **Document** — compact, preserves original filename\n\n"
            "_Which format do you prefer?_",
            reply_markup=_upload_type_keyboard(),
        )
        return

    # ── Sub-menu: preset ──────────────────────────────────────────
    if action == "preset":
        await cb.answer()
        await cb.message.edit(
            "⚡ **ENCODE SPEED (PRESET)**\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "> **Ultrafast** — fastest processing, larger file\n"
            "> **Veryfast** — slightly smaller, barely slower\n"
            "> **Fast** — good balance, noticeable encode time\n"
            "> **Medium** — best compression, slowest\n\n"
            "_Recommended: Ultrafast or Veryfast on Railway_",
            reply_markup=_preset_keyboard(),
        )
        return

    # ── Sub-menu: CRF quality ─────────────────────────────────────
    if action == "crf":
        await cb.answer()
        await cb.message.edit(
            "🎨 **VIDEO QUALITY (CRF)**\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "> **High (18)** — near lossless, large file\n"
            "> **Good (23)** — default, great quality\n"
            "> **Medium (28)** — smaller file, visible loss\n"
            "> **Small (35)** — maximum compression, low quality\n\n"
            "_Lower CRF = better quality but bigger file & slower processing_",
            reply_markup=_crf_keyboard(),
        )
        return

    # ── Sub-menu: default resolution ─────────────────────────────
    if action == "default_res":
        await cb.answer()
        await cb.message.edit(
            "📐 **DEFAULT RESOLUTION**\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "> **Source** — keep original resolution\n"
            "> **1080p** — Full HD\n"
            "> **720p** — HD, best size/quality ratio\n"
            "> **480p** — SD, small file\n"
            "> **360p** — very small, mobile-friendly\n\n"
            "_This is applied automatically when you change resolution_",
            reply_markup=_res_keyboard(),
        )
        return

    # ── Reset ─────────────────────────────────────────────────────
    if action == "reset":
        reset(uid)
        await cb.answer("🔄 Settings reset to defaults!")
        await cb.message.edit(
            _settings_text(uid),
            reply_markup=_settings_keyboard(uid),
        )
        return

    # ── Back ──────────────────────────────────────────────────────
    if action == "back":
        await cb.answer()
        await cb.message.edit(
            _settings_text(uid),
            reply_markup=_settings_keyboard(uid),
        )
        return

    # ── Close ─────────────────────────────────────────────────────
    if action == "close":
        await cb.answer()
        await cb.message.delete()



# ── Receive channel input ─────────────────────────────────────────
@app.on_message(filters.private & filters.text & ~filters.command([
    "start", "help", "stats", "queue", "settings"
]))
async def settings_text_input(client: Client, msg: Message):
    uid = msg.from_user.id
    if uid not in _WAITING_CHANNEL:
        return  # not waiting — let other handlers process

    _WAITING_CHANNEL.pop(uid)
    text = msg.text.strip()

    if text.lower() == "clear":
        sset(uid, "channel_id", "")
        await msg.reply("✅ _Channel removed._\n\nUse /settings to configure again.")
        return

    if not (text.startswith("@") or text.lstrip("-").isdigit()):
        await msg.reply(
            "❌ _Invalid format._\n\n"
            "Use `-1001234567890` or `@username`.\n"
            "Type /settings to try again."
        )
        return

    try:
        chat = await client.get_chat(text)
        sset(uid, "channel_id", text)
        await msg.reply(
            f"✅ **Channel saved!**\n\n"
            f"📢 `{chat.title}`\n"
            f"🆔 `{text}`\n\n"
            f"_Files will be forwarded here after processing._"
        )
    except Exception:
        await msg.reply(
            f"❌ **Could not access** `{text}`\n\n"
            f"Make sure:\n"
            f"> Bot is admin in the channel\n"
            f"> The ID or username is correct\n\n"
            f"Type /settings to try again."
        )
