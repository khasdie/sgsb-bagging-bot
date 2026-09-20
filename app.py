import os
import re
import json
import time
import base64
import secrets
import threading
import requests
from flask import Flask, request, jsonify
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

# ============================================================
# ENVIRONMENT
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ARCHIVE_CHAT_ID = os.environ.get("ARCHIVE_CHAT_ID", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
ADMIN_PASSCODE = os.environ.get("ADMIN_PASSCODE", "88888888")

# Permanent Outlet Master storage (GitHub Gist)
# Create one private/secret Gist containing sgsb_outlets.json, then set:
# GITHUB_TOKEN = GitHub token with Gist write access
# OUTLET_GIST_ID = the Gist ID
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
OUTLET_GIST_ID = os.environ.get("OUTLET_GIST_ID", "")
OUTLET_GIST_FILENAME = os.environ.get("OUTLET_GIST_FILENAME", "sgsb_outlets.json")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TZ = ZoneInfo("Asia/Kuala_Lumpur")

DEFAULT_OUTLETS = [
    "SBH307",
    "SBH458",
    "SBH001",
    "SBH002",
    "SBH003",
]

# ============================================================
# RUNTIME STATE
# ============================================================

# Multiple open records are allowed per user.
records = {}          # rid -> record state
builders = {}         # user_id -> outlet/seal builder
input_modes = {}      # user_id -> {"type": "remark", "rid": rid}
admin_sessions = {}   # user_id -> admin state
processed_updates = {}

state_lock = threading.RLock()
OUTLETS = list(DEFAULT_OUTLETS)
OUTLET_STORAGE_OK = False
OUTLET_STORAGE_ERROR = ""


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_my():
    return datetime.now(TZ)


def safe_html(value):
    s = str(value or "")
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def normalize_outlet(value):
    value = (value or "").strip().upper()
    value = re.sub(r"[^A-Z0-9_-]", "", value)
    return value[:30]


def normalize_seal(value):
    value = (value or "").strip().upper()
    value = re.sub(r"\s+", " ", value)
    return value[:100]


def hashtag(value):
    return re.sub(
        r"[^A-Z0-9_]",
        "",
        str(value).upper().replace("-", "_").replace(" ", "_")
    )


def encode_seal(seal):
    return base64.urlsafe_b64encode(
        seal.encode("utf-8")
    ).decode("ascii").rstrip("=")


def decode_seal(value):
    try:
        pad = "=" * ((4 - len(value) % 4) % 4)
        return base64.urlsafe_b64decode(
            (value + pad).encode("ascii")
        ).decode("utf-8")
    except Exception:
        return ""


def make_rid():
    return now_my().strftime("%Y%m%d%H%M%S") + secrets.token_hex(3).upper()


def record_display_id(record):
    return f"{record['outlet']}-{record['rid']}"


def remember_update(update_id):
    if update_id is None:
        return True

    now = time.time()
    with state_lock:
        stale = [
            key for key, ts in processed_updates.items()
            if now - ts > 3600
        ]
        for key in stale:
            processed_updates.pop(key, None)

        if update_id in processed_updates:
            return False

        processed_updates[update_id] = now
        return True


# ============================================================
# TELEGRAM HELPERS
# ============================================================

def telegram(method, payload=None, timeout=30):
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN missing"}

    try:
        response = requests.post(
            f"{API}/{method}",
            json=payload or {},
            timeout=timeout
        )
        try:
            return response.json()
        except Exception:
            return {
                "ok": False,
                "description": f"Non-JSON Telegram response HTTP {response.status_code}"
            }
    except Exception as e:
        print("Telegram API error:", method, e, flush=True)
        return {"ok": False, "description": str(e)}


def send_message(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if keyboard:
        payload["reply_markup"] = keyboard

    return telegram("sendMessage", payload)


def edit_message(chat_id, message_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if keyboard:
        payload["reply_markup"] = keyboard

    result = telegram("editMessageText", payload)

    # "message is not modified" is harmless.
    if (
        not result.get("ok")
        and "message is not modified" in str(result.get("description", "")).lower()
    ):
        return {"ok": True}

    return result


def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:200]
    return telegram("answerCallbackQuery", payload)


def send_media_group(chat_id, media):
    return telegram(
        "sendMediaGroup",
        {
            "chat_id": chat_id,
            "media": media
        },
        timeout=45
    )


# ============================================================
# PERMANENT OUTLET MASTER — GITHUB GIST
# ============================================================

def gist_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SGSB-Bagging-Bot"
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def outlet_storage_configured():
    return bool(GITHUB_TOKEN and OUTLET_GIST_ID)


def load_outlets_from_gist():
    global OUTLETS, OUTLET_STORAGE_OK, OUTLET_STORAGE_ERROR

    if not outlet_storage_configured():
        OUTLETS = list(DEFAULT_OUTLETS)
        OUTLET_STORAGE_OK = False
        OUTLET_STORAGE_ERROR = (
            "GITHUB_TOKEN / OUTLET_GIST_ID belum dikonfigurasi"
        )
        return False

    try:
        r = requests.get(
            f"https://api.github.com/gists/{OUTLET_GIST_ID}",
            headers=gist_headers(),
            timeout=15
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"GitHub Gist HTTP {r.status_code}: {r.text[:250]}"
            )

        gist = r.json()
        file_info = gist.get("files", {}).get(OUTLET_GIST_FILENAME)

        if not file_info:
            raise RuntimeError(
                f"Fail {OUTLET_GIST_FILENAME} tidak dijumpai dalam Gist"
            )

        content = file_info.get("content", "")
        data = json.loads(content)
        raw_outlets = data.get("outlets", [])

        clean = []
        for item in raw_outlets:
            outlet = normalize_outlet(str(item))
            if outlet and outlet not in clean:
                clean.append(outlet)

        if not clean:
            clean = list(DEFAULT_OUTLETS)

        with state_lock:
            OUTLETS = clean

        OUTLET_STORAGE_OK = True
        OUTLET_STORAGE_ERROR = ""
        print(
            f"Outlet Master loaded: {len(OUTLETS)} outlets",
            flush=True
        )
        return True

    except Exception as e:
        OUTLETS = list(DEFAULT_OUTLETS)
        OUTLET_STORAGE_OK = False
        OUTLET_STORAGE_ERROR = str(e)
        print("Outlet storage load error:", e, flush=True)
        return False


def save_outlets_to_gist(outlets):
    global OUTLET_STORAGE_OK, OUTLET_STORAGE_ERROR

    if not outlet_storage_configured():
        return False, (
            "Permanent Outlet Storage belum disambung. "
            "Set GITHUB_TOKEN dan OUTLET_GIST_ID di Render."
        )

    data = {
        "version": 1,
        "updated_at": now_my().isoformat(),
        "outlets": sorted(outlets)
    }

    payload = {
        "files": {
            OUTLET_GIST_FILENAME: {
                "content": json.dumps(
                    data,
                    ensure_ascii=False,
                    indent=2
                )
            }
        }
    }

    try:
        r = requests.patch(
            f"https://api.github.com/gists/{OUTLET_GIST_ID}",
            headers=gist_headers(),
            json=payload,
            timeout=20
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"GitHub Gist HTTP {r.status_code}: {r.text[:250]}"
            )

        OUTLET_STORAGE_OK = True
        OUTLET_STORAGE_ERROR = ""
        return True, None

    except Exception as e:
        OUTLET_STORAGE_OK = False
        OUTLET_STORAGE_ERROR = str(e)
        print("Outlet storage save error:", e, flush=True)
        return False, str(e)


# ============================================================
# KEYBOARDS
# ============================================================

def outlet_keyboard():
    with state_lock:
        outlets = sorted(OUTLETS)

    rows = []
    for i in range(0, len(outlets), 2):
        rows.append([
            {
                "text": outlet,
                "callback_data": f"outlet:{outlet}"
            }
            for outlet in outlets[i:i + 2]
        ])

    if not rows:
        rows = [[
            {
                "text": "Tiada outlet",
                "callback_data": "noop"
            }
        ]]

    return {"inline_keyboard": rows}


def record_keyboard(rid, completed=False, failed=False):
    rows = []

    if not completed:
        rows.append([
            {
                "text": "📝 Remark",
                "callback_data": f"remark:{rid}"
            }
        ])

    if failed:
        rows.append([
            {
                "text": "🔄 CUBA ARCHIVE SEMULA",
                "callback_data": f"retry:{rid}"
            }
        ])

    rows.append([
        {
            "text": "➕ NEW BAGGING",
            "callback_data": "new"
        }
    ])

    if not completed:
        rows.append([
            {
                "text": "❌ Batal Record",
                "callback_data": f"cancel_record:{rid}"
            }
        ])

    return {"inline_keyboard": rows}


def admin_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📋 Senarai Outlet",
                    "callback_data": "admin:list"
                }
            ],
            [
                {
                    "text": "➕ Tambah Outlet",
                    "callback_data": "admin:add"
                }
            ],
            [
                {
                    "text": "🗑 Padam Outlet",
                    "callback_data": "admin:delete"
                }
            ],
            [
                {
                    "text": "🔄 Sync Outlet Master",
                    "callback_data": "admin:sync"
                }
            ],
            [
                {
                    "text": "🚪 Keluar Admin",
                    "callback_data": "admin:logout"
                }
            ]
        ]
    }


def delete_outlet_keyboard():
    with state_lock:
        outlets = sorted(OUTLETS)

    rows = []
    for outlet in outlets:
        rows.append([
            {
                "text": f"🗑 {outlet}",
                "callback_data": f"admin:del:{outlet}"
            }
        ])

    rows.append([
        {
            "text": "⬅️ Kembali",
            "callback_data": "admin:menu"
        }
    ])

    return {"inline_keyboard": rows}


# ============================================================
# RECORD CARD
# ============================================================

def record_ref(record):
    return (
        f"SGR4|{record['rid']}|{record['outlet']}|"
        f"{encode_seal(record['seal'])}"
    )


def record_card_text(record):
    photo_status = "✅" if record.get("photo") else "⏳"
    video_status = "✅" if record.get("video") else "⏳"

    if record.get("archived"):
        title = "✅ <b>BAGGING ARCHIVED</b>"
        instruction = (
            "Rekod ini telah dihantar ke Archive.\n"
            "Tekan <b>NEW BAGGING</b> untuk sesi seterusnya."
        )
    elif record.get("archive_failed"):
        title = "⚠️ <b>ARCHIVE GAGAL</b>"
        instruction = (
            "Media sudah lengkap tetapi Archive gagal.\n"
            "Tekan <b>CUBA ARCHIVE SEMULA</b>."
        )
    else:
        title = "📦 <b>BAGGING SESSION OPEN</b>"
        instruction = (
            "↩️ <b>REPLY mesej card ini</b> dengan:\n"
            "• 1 gambar seal\n"
            "• 1 video bagging\n\n"
            "Anda boleh tekan <b>NEW BAGGING</b> sekarang dan "
            "buka Seal seterusnya walaupun video ini masih uploading."
        )

    remark = record.get("remark") or "—"

    return (
        f"{title}\n\n"
        f"🆔 <code>{safe_html(record_display_id(record))}</code>\n"
        f"🏢 Outlet: <b>{safe_html(record['outlet'])}</b>\n"
        f"🔒 Seal: <b>{safe_html(record['seal'])}</b>\n\n"
        f"📷 Gambar: {photo_status}\n"
        f"🎥 Video: {video_status}\n"
        f"📝 Remark: {safe_html(remark)}\n\n"
        f"{instruction}\n\n"
        f"<code>{safe_html(record_ref(record))}</code>"
    )


def parse_record_ref(text):
    text = text or ""
    match = re.search(
        r"SGR4\|([A-Z0-9]+)\|([A-Z0-9_-]+)\|([A-Za-z0-9_-]+)",
        text
    )
    if not match:
        return None

    rid, outlet, seal_encoded = match.groups()
    seal = decode_seal(seal_encoded)
    if not seal:
        return None

    return {
        "rid": rid,
        "outlet": outlet,
        "seal": seal
    }


def register_record(user_id, chat_id, outlet, seal):
    rid = make_rid()
    record = {
        "rid": rid,
        "owner_id": user_id,
        "chat_id": chat_id,
        "outlet": outlet,
        "seal": seal,
        "photo": None,
        "video": None,
        "remark": "",
        "card_message_id": None,
        "created_at": now_my().isoformat(),
        "archived": False,
        "archive_failed": False,
        "archiving": False
    }

    result = send_message(
        chat_id,
        record_card_text(record),
        record_keyboard(rid)
    )

    if not result.get("ok"):
        return None, result

    message = result.get("result", {})
    record["card_message_id"] = message.get("message_id")

    with state_lock:
        records[rid] = record

    return record, result


def reconstruct_record_from_reply(user_id, chat_id, reply_message):
    ref = parse_record_ref(
        reply_message.get("text")
        or reply_message.get("caption")
        or ""
    )
    if not ref:
        return None

    rid = ref["rid"]

    with state_lock:
        existing = records.get(rid)
        if existing:
            return existing

        record = {
            "rid": rid,
            "owner_id": user_id,
            "chat_id": chat_id,
            "outlet": ref["outlet"],
            "seal": ref["seal"],
            "photo": None,
            "video": None,
            "remark": "",
            "card_message_id": reply_message.get("message_id"),
            "created_at": now_my().isoformat(),
            "archived": False,
            "archive_failed": False,
            "archiving": False
        }
        records[rid] = record
        return record


def update_record_card(record):
    if not record.get("card_message_id"):
        return

    edit_message(
        record["chat_id"],
        record["card_message_id"],
        record_card_text(record),
        record_keyboard(
            record["rid"],
            completed=record.get("archived", False),
            failed=record.get("archive_failed", False)
        )
    )


def open_records_for_user(user_id):
    with state_lock:
        return [
            r for r in records.values()
            if r.get("owner_id") == user_id
            and not r.get("archived")
        ]


def resolve_record_for_media(message, user_id, chat_id):
    reply = message.get("reply_to_message")

    if reply:
        record = reconstruct_record_from_reply(
            user_id,
            chat_id,
            reply
        )
        if record:
            if record.get("owner_id") != user_id:
                return None, (
                    "⚠️ Record card ini bukan milik sesi anda."
                )
            return record, None

    # Convenience: if there is only ONE open record,
    # media may be sent without Reply.
    open_records = open_records_for_user(user_id)

    if len(open_records) == 1:
        return open_records[0], None

    if len(open_records) > 1:
        return None, (
            "⚠️ Anda mempunyai lebih daripada satu bagging yang masih OPEN.\n\n"
            "Untuk elakkan video tersalah Seal, <b>REPLY Record Card</b> "
            "yang betul kemudian hantar gambar/video."
        )

    return None, (
        "⚠️ Tiada Bagging Session yang aktif.\n"
        "Tekan /start atau <b>NEW BAGGING</b>."
    )


# ============================================================
# ARCHIVE — ONE SEAL = ONE TELEGRAM ALBUM
# ============================================================

def archive_caption(record):
    created = datetime.fromisoformat(
        record["created_at"]
    ).astimezone(TZ)

    return (
        "📦 <b>SGSB BAGGING RECORD</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <code>{safe_html(record_display_id(record))}</code>\n"
        f"🏢 <b>{safe_html(record['outlet'])}</b>\n"
        f"🔒 <b>{safe_html(record['seal'])}</b>\n"
        f"🕒 {created.strftime('%d/%m/%Y • %I:%M %p')}\n"
        f"📝 {safe_html(record.get('remark') or '—')}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📷 1 Photo • 🎥 1 Video\n\n"
        f"#{hashtag(record['outlet'])} "
        f"#{created.strftime('%Y%m%d')} "
        f"#SEAL_{hashtag(record['seal'])}"
    )


def archive_record(record):
    if record.get("archived"):
        return True, None

    if not record.get("photo") or not record.get("video"):
        return False, "Record belum lengkap"

    if record.get("archiving"):
        return False, "Archive sedang diproses"

    record["archiving"] = True
    record["archive_failed"] = False
    update_record_card(record)

    media = [
        {
            "type": "photo",
            "media": record["photo"]["file_id"],
            "caption": archive_caption(record),
            "parse_mode": "HTML"
        },
        {
            "type": "video",
            "media": record["video"]["file_id"]
        }
    ]

    result = send_media_group(
        ARCHIVE_CHAT_ID,
        media
    )

    record["archiving"] = False

    if not result.get("ok"):
        record["archive_failed"] = True
        update_record_card(record)
        error = result.get("description", str(result))
        print(
            "Archive media group failed:",
            record_display_id(record),
            error,
            flush=True
        )
        return False, error

    record["archived"] = True
    record["archive_failed"] = False
    update_record_card(record)

    send_message(
        record["chat_id"],
        "✅ <b>ARCHIVE BERJAYA</b>\n\n"
        f"🆔 <code>{safe_html(record_display_id(record))}</code>\n"
        f"🏢 <b>{safe_html(record['outlet'])}</b>\n"
        f"🔒 <b>{safe_html(record['seal'])}</b>\n\n"
        "Gambar + video telah dihantar sebagai <b>satu album</b> "
        "ke SGSB Bagging Archive.",
        {
            "inline_keyboard": [
                [
                    {
                        "text": "➕ NEW BAGGING",
                        "callback_data": "new"
                    }
                ]
            ]
        }
    )

    return True, None


def maybe_archive_record(record):
    if (
        record.get("photo")
        and record.get("video")
        and not record.get("archived")
        and not record.get("archiving")
    ):
        return archive_record(record)

    update_record_card(record)
    return True, None


# ============================================================
# STAFF BUILDER
# ============================================================

def start_builder(chat_id, user_id):
    with state_lock:
        builders[user_id] = {
            "step": "outlet",
            "outlet": None
        }

    send_message(
        chat_id,
        "📦 <b>NEW BAGGING</b>\n\n"
        "Pilih <b>Outlet Code</b>:",
        outlet_keyboard()
    )


# ============================================================
# ADMIN
# ============================================================

def begin_admin(chat_id, user_id):
    admin_sessions[user_id] = {
        "authenticated": False,
        "step": "passcode"
    }

    send_message(
        chat_id,
        "🔐 <b>ADMIN OUTLET MANAGEMENT</b>\n\n"
        "Masukkan Admin Passcode."
    )


def show_admin_menu(chat_id, user_id):
    admin_sessions[user_id] = {
        "authenticated": True,
        "step": "menu"
    }

    storage_text = (
        "✅ Permanent Storage: Connected"
        if OUTLET_STORAGE_OK
        else "⚠️ Permanent Storage: NOT CONNECTED"
    )

    send_message(
        chat_id,
        "⚙️ <b>ADMIN OUTLET MANAGEMENT</b>\n\n"
        f"{storage_text}\n"
        f"🏢 Total Outlet: {len(OUTLETS)}",
        admin_keyboard()
    )


def handle_admin_text(chat_id, user_id, text):
    adm = admin_sessions.get(user_id)
    if not adm:
        return False

    if adm.get("step") == "passcode":
        if text == ADMIN_PASSCODE:
            show_admin_menu(chat_id, user_id)
        else:
            admin_sessions.pop(user_id, None)
            send_message(chat_id, "❌ Passcode salah.")
        return True

    if not adm.get("authenticated"):
        return False

    if adm.get("step") == "add":
        outlet = normalize_outlet(text)

        if not outlet:
            send_message(
                chat_id,
                "⚠️ Outlet Code tidak sah."
            )
            return True

        with state_lock:
            current = list(OUTLETS)

        if outlet in current:
            adm["step"] = "menu"
            send_message(
                chat_id,
                f"ℹ️ <b>{safe_html(outlet)}</b> sudah wujud.",
                admin_keyboard()
            )
            return True

        updated = current + [outlet]
        ok, error = save_outlets_to_gist(updated)

        if not ok:
            send_message(
                chat_id,
                "❌ <b>OUTLET TIDAK DISIMPAN</b>\n\n"
                "Saya tidak akan tunjuk success kerana Permanent Storage "
                "belum berjaya.\n\n"
                f"Error: <code>{safe_html(error)}</code>",
                admin_keyboard()
            )
            adm["step"] = "menu"
            return True

        with state_lock:
            OUTLETS[:] = sorted(updated)

        adm["step"] = "menu"
        send_message(
            chat_id,
            f"✅ Outlet <b>{safe_html(outlet)}</b> disimpan secara permanent.",
            admin_keyboard()
        )
        return True

    return False


# ============================================================
# MESSAGE HANDLER
# ============================================================

def handle_message(message):
    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    user_id = user.get("id")

    if not user_id:
        return

    text = message.get("text", "").strip()

    # Commands
    if text == "/admin":
        begin_admin(chat_id, user_id)
        return

    if handle_admin_text(chat_id, user_id, text):
        return

    if text in ["/start", "/new"]:
        start_builder(chat_id, user_id)
        return

    # Remark input has priority over Seal input.
    mode = input_modes.get(user_id)
    if mode and mode.get("type") == "remark":
        rid = mode.get("rid")
        record = records.get(rid)

        if not record:
            input_modes.pop(user_id, None)
            send_message(
                chat_id,
                "⚠️ Record tidak lagi tersedia."
            )
            return

        if not text:
            send_message(
                chat_id,
                "⚠️ Taip remark dalam bentuk teks."
            )
            return

        record["remark"] = text[:500]
        input_modes.pop(user_id, None)
        update_record_card(record)

        send_message(
            chat_id,
            "✅ Remark disimpan."
        )
        return

    # Media handling
    if (
        "photo" in message
        or "video" in message
        or "document" in message
    ):
        record, error = resolve_record_for_media(
            message,
            user_id,
            chat_id
        )

        if not record:
            send_message(chat_id, error)
            return

        if record.get("archived"):
            send_message(
                chat_id,
                "ℹ️ Record ini sudah selesai dan telah di-Archive."
            )
            return

        if "photo" in message:
            if record.get("photo"):
                send_message(
                    chat_id,
                    "⚠️ Record ini sudah mempunyai 1 gambar."
                )
                return

            photo = message["photo"][-1]
            record["photo"] = {
                "file_id": photo["file_id"],
                "message_id": message["message_id"]
            }

            send_message(
                chat_id,
                "📷 Gambar diterima untuk "
                f"<b>{safe_html(record['seal'])}</b>."
            )

            maybe_archive_record(record)
            return

        if "video" in message:
            if record.get("video"):
                send_message(
                    chat_id,
                    "⚠️ Record ini sudah mempunyai 1 video."
                )
                return

            video = message["video"]
            record["video"] = {
                "file_id": video["file_id"],
                "message_id": message["message_id"]
            }

            send_message(
                chat_id,
                "🎥 Video diterima untuk "
                f"<b>{safe_html(record['seal'])}</b>."
            )

            maybe_archive_record(record)
            return

        if "document" in message:
            doc = message["document"]
            mime = doc.get("mime_type", "")

            if mime.startswith("video/"):
                send_message(
                    chat_id,
                    "⚠️ Untuk memastikan <b>Gambar + Video menjadi satu album</b> "
                    "di Archive, sila hantar video sebagai <b>Video</b>, "
                    "bukan sebagai File/Document."
                )
                return

            send_message(
                chat_id,
                "⚠️ Fail ini tidak digunakan untuk Bagging Record."
            )
            return

    # Builder input
    builder = builders.get(user_id)

    if builder and builder.get("step") == "seal":
        if not text:
            send_message(
                chat_id,
                "⚠️ Masukkan Seal Bag Number."
            )
            return

        seal = normalize_seal(text)
        if not seal:
            send_message(
                chat_id,
                "⚠️ Seal Bag Number tidak sah."
            )
            return

        outlet = builder.get("outlet")
        record, result = register_record(
            user_id,
            chat_id,
            outlet,
            seal
        )

        if not record:
            send_message(
                chat_id,
                "❌ Gagal membuka Bagging Session.\n"
                f"<code>{safe_html(result)}</code>"
            )
            return

        # Builder is done. Record stays open independently.
        with state_lock:
            builders.pop(user_id, None)

        send_message(
            chat_id,
            "✅ <b>SESSION DIBUKA</b>\n\n"
            "Sekarang <b>Reply Record Card di atas</b> dengan gambar/video.\n\n"
            "Anda juga boleh tekan <b>NEW BAGGING</b> terus untuk membuka "
            "Seal seterusnya sementara video sebelumnya masih uploading."
        )
        return

    if not builder:
        send_message(
            chat_id,
            "👋 <b>SGSB Bagging Bot V4</b>\n\n"
            "Tekan /start untuk buka Bagging Session baru.\n"
            "Admin: /admin"
        )
        return


# ============================================================
# CALLBACK HANDLER
# ============================================================

def handle_callback(callback):
    callback_id = callback["id"]
    user = callback["from"]
    user_id = user["id"]

    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    data = callback.get("data", "")

    answer_callback(callback_id)

    if data == "noop":
        return

    if data == "new":
        start_builder(chat_id, user_id)
        return

    # Outlet selection
    if data.startswith("outlet:"):
        outlet = data.split(":", 1)[1]

        with state_lock:
            valid = outlet in OUTLETS

        if not valid:
            send_message(
                chat_id,
                "⚠️ Outlet tidak sah / sudah dipadam."
            )
            return

        builders[user_id] = {
            "step": "seal",
            "outlet": outlet
        }

        send_message(
            chat_id,
            f"🏢 Outlet: <b>{safe_html(outlet)}</b>\n\n"
            "Masukkan <b>Seal Bag Number</b>."
        )
        return

    # Record callbacks
    if data.startswith("remark:"):
        rid = data.split(":", 1)[1]
        record = records.get(rid)

        if not record or record.get("owner_id") != user_id:
            send_message(
                chat_id,
                "⚠️ Record tidak dijumpai."
            )
            return

        input_modes[user_id] = {
            "type": "remark",
            "rid": rid
        }

        send_message(
            chat_id,
            f"📝 Taip remark untuk Seal "
            f"<b>{safe_html(record['seal'])}</b>."
        )
        return

    if data.startswith("retry:"):
        rid = data.split(":", 1)[1]
        record = records.get(rid)

        if not record or record.get("owner_id") != user_id:
            send_message(
                chat_id,
                "⚠️ Record tidak dijumpai."
            )
            return

        ok, error = archive_record(record)

        if not ok and error != "Archive sedang diproses":
            send_message(
                chat_id,
                "⚠️ Archive masih gagal.\n"
                f"<code>{safe_html(error)}</code>"
            )
        return

    if data.startswith("cancel_record:"):
        rid = data.split(":", 1)[1]
        record = records.get(rid)

        if not record or record.get("owner_id") != user_id:
            send_message(
                chat_id,
                "⚠️ Record tidak dijumpai."
            )
            return

        if record.get("archived"):
            send_message(
                chat_id,
                "⚠️ Record yang sudah di-Archive tidak boleh dibatalkan."
            )
            return

        with state_lock:
            records.pop(rid, None)

        edit_message(
            chat_id,
            record.get("card_message_id"),
            "❌ <b>BAGGING RECORD DIBATALKAN</b>\n\n"
            f"🏢 {safe_html(record['outlet'])}\n"
            f"🔒 {safe_html(record['seal'])}\n"
            f"🆔 <code>{safe_html(record_display_id(record))}</code>",
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "➕ NEW BAGGING",
                            "callback_data": "new"
                        }
                    ]
                ]
            }
        )
        return

    # Admin callbacks
    if data.startswith("admin:"):
        adm = admin_sessions.get(user_id)

        if not adm or not adm.get("authenticated"):
            send_message(
                chat_id,
                "🔐 Sesi admin tamat. Taip /admin semula."
            )
            return

        if data == "admin:menu":
            show_admin_menu(chat_id, user_id)
            return

        if data == "admin:list":
            with state_lock:
                outlets = sorted(OUTLETS)

            outlet_text = (
                "\n".join(
                    f"• {safe_html(x)}"
                    for x in outlets
                )
                or "Tiada outlet."
            )

            storage_text = (
                "✅ Permanent"
                if OUTLET_STORAGE_OK
                else "⚠️ Tidak permanent"
            )

            send_message(
                chat_id,
                "📋 <b>SENARAI OUTLET</b>\n\n"
                f"{outlet_text}\n\n"
                f"Total: {len(outlets)}\n"
                f"Storage: {storage_text}",
                admin_keyboard()
            )
            return

        if data == "admin:add":
            adm["step"] = "add"
            send_message(
                chat_id,
                "➕ Taip <b>Outlet Code</b> baru.\n"
                "Contoh: SBH999"
            )
            return

        if data == "admin:delete":
            send_message(
                chat_id,
                "🗑 <b>Pilih outlet untuk dipadam:</b>",
                delete_outlet_keyboard()
            )
            return

        if data.startswith("admin:del:"):
            outlet = data.split(":", 2)[2]

            with state_lock:
                current = list(OUTLETS)

            if outlet not in current:
                send_message(
                    chat_id,
                    "ℹ️ Outlet tidak dijumpai.",
                    admin_keyboard()
                )
                return

            updated = [
                x for x in current
                if x != outlet
            ]

            ok, error = save_outlets_to_gist(updated)

            if not ok:
                send_message(
                    chat_id,
                    "❌ <b>OUTLET TIDAK DIPADAM</b>\n\n"
                    "Permanent Storage gagal dikemaskini.\n"
                    f"<code>{safe_html(error)}</code>",
                    admin_keyboard()
                )
                return

            with state_lock:
                OUTLETS[:] = sorted(updated)

            send_message(
                chat_id,
                f"✅ <b>{safe_html(outlet)}</b> dipadam secara permanent.",
                admin_keyboard()
            )
            return

        if data == "admin:sync":
            ok = load_outlets_from_gist()
            if ok:
                send_message(
                    chat_id,
                    f"✅ Outlet Master synced.\n"
                    f"Total: {len(OUTLETS)}",
                    admin_keyboard()
                )
            else:
                send_message(
                    chat_id,
                    "❌ Sync gagal.\n"
                    f"<code>{safe_html(OUTLET_STORAGE_ERROR)}</code>",
                    admin_keyboard()
                )
            return

        if data == "admin:logout":
            admin_sessions.pop(user_id, None)
            send_message(
                chat_id,
                "🚪 Admin logout."
            )
            return


# ============================================================
# WEB ROUTES
# ============================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "service": "SGSB Bagging Bot V4",
        "outlets": len(OUTLETS),
        "outlet_storage": (
            "persistent-gist"
            if OUTLET_STORAGE_OK
            else "fallback-memory"
        ),
        "open_records": len([
            r for r in records.values()
            if not r.get("archived")
        ])
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "version": "4"
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )
        if received_secret != WEBHOOK_SECRET:
            return jsonify({"ok": False}), 403

    update = request.get_json(silent=True) or {}

    if not remember_update(update.get("update_id")):
        return jsonify({
            "ok": True,
            "duplicate": True
        })

    try:
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
    except Exception as e:
        print(
            "Webhook processing error:",
            repr(e),
            flush=True
        )

    return jsonify({"ok": True})


# ============================================================
# STARTUP
# ============================================================

load_outlets_from_gist()

if __name__ == "__main__":
    port = int(
        os.environ.get("PORT", 10000)
    )
    app.run(
        host="0.0.0.0",
        port=port
    )
