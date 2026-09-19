import os
import re
import json
import time
import queue
import threading
import requests
from flask import Flask, request, jsonify
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ARCHIVE_CHAT_ID = os.environ.get("ARCHIVE_CHAT_ID", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
ADMIN_PASSCODE = os.environ.get("ADMIN_PASSCODE", "88888888")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TZ = ZoneInfo("Asia/Kuala_Lumpur")

# ============================================================
# SGSB BAGGING BOT V2
# 1 seal = 1 photo + 1 video
# Staff can submit and immediately start the next record.
# Archive copying runs through a background queue.
# ============================================================

DEFAULT_OUTLETS = ["SBH307", "SBH458", "SBH001", "SBH002", "SBH003"]
OUTLETS = list(DEFAULT_OUTLETS)

sessions = {}
admin_sessions = {}
processed_updates = {}
archive_queue = queue.Queue()
state_lock = threading.RLock()

# Render free filesystem/RAM is not permanent.
# Outlet changes made in /admin work immediately, but after a Render restart
# the list falls back to DEFAULT_OUTLETS. A persistent store can be added later.


# ============================================================
# HELPERS
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
    return re.sub(r"[^A-Z0-9_]", "", str(value).upper().replace("-", "_").replace(" ", "_"))


def telegram(method, payload=None, timeout=25):
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN missing"}
    try:
        r = requests.post(f"{API}/{method}", json=payload or {}, timeout=timeout)
        return r.json()
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


def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:200]
    return telegram("answerCallbackQuery", payload)


def copy_message(from_chat_id, message_id, caption=None):
    payload = {
        "chat_id": ARCHIVE_CHAT_ID,
        "from_chat_id": from_chat_id,
        "message_id": message_id
    }
    if caption is not None:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"
    return telegram("copyMessage", payload, timeout=30)


def remember_update(update_id):
    """Deduplicate Telegram retries in-process."""
    if update_id is None:
        return True

    now = time.time()
    with state_lock:
        # prune entries older than 1 hour
        old = [k for k, ts in processed_updates.items() if now - ts > 3600]
        for k in old:
            processed_updates.pop(k, None)

        if update_id in processed_updates:
            return False

        processed_updates[update_id] = now
        return True


# ============================================================
# KEYBOARDS
# ============================================================

def outlet_keyboard():
    with state_lock:
        outlets = sorted(OUTLETS)

    rows = []
    for i in range(0, len(outlets), 2):
        rows.append([
            {"text": x, "callback_data": f"outlet:{x}"}
            for x in outlets[i:i + 2]
        ])

    if not rows:
        rows = [[{"text": "Tiada outlet", "callback_data": "noop"}]]

    return {"inline_keyboard": rows}


def media_keyboard(session=None):
    photo_ok = bool(session and session.get("photo"))
    video_ok = bool(session and session.get("video"))

    return {
        "inline_keyboard": [
            [{"text": "📝 Remark", "callback_data": "remark"}],
            [{
                "text": "✅ SUBMIT" if (photo_ok and video_ok) else "⏳ SUBMIT (belum lengkap)",
                "callback_data": "submit"
            }],
            [{"text": "❌ Batal", "callback_data": "cancel"}]
        ]
    }


def new_record_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "➕ NEW BAGGING", "callback_data": "new"}]
        ]
    }


def admin_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "📋 Senarai Outlet", "callback_data": "admin:list"}],
            [{"text": "➕ Tambah Outlet", "callback_data": "admin:add"}],
            [{"text": "🗑 Padam Outlet", "callback_data": "admin:delete"}],
            [{"text": "🚪 Keluar Admin", "callback_data": "admin:logout"}]
        ]
    }


def delete_outlet_keyboard():
    with state_lock:
        outlets = sorted(OUTLETS)

    rows = []
    for outlet in outlets:
        rows.append([{
            "text": f"🗑 {outlet}",
            "callback_data": f"admin:del:{outlet}"
        }])

    rows.append([{"text": "⬅️ Kembali", "callback_data": "admin:menu"}])
    return {"inline_keyboard": rows}


# ============================================================
# STAFF SESSION
# ============================================================

def create_session(user_id):
    session = {
        "step": "outlet",
        "outlet": None,
        "seal": None,
        "photo": None,
        "video": None,
        "remark": "",
        "started_at": now_my().isoformat()
    }
    with state_lock:
        sessions[user_id] = session
    return session


def get_session(user_id):
    with state_lock:
        return sessions.get(user_id)


def start_record(chat_id, user_id):
    create_session(user_id)
    send_message(
        chat_id,
        "📦 <b>SGSB BAGGING RECORD</b>\n\n"
        "1 Seal = 1 Gambar + 1 Video\n\n"
        "Sila pilih <b>Outlet Code</b>:",
        outlet_keyboard()
    )


def media_status(session):
    return (
        f"🏢 Outlet: <b>{safe_html(session.get('outlet'))}</b>\n"
        f"🔒 Seal: <b>{safe_html(session.get('seal'))}</b>\n\n"
        f"📷 Gambar: {'✅' if session.get('photo') else '❌'}\n"
        f"🎥 Video: {'✅' if session.get('video') else '❌'}\n"
        f"📝 Remark: {'✅' if session.get('remark') else '—'}"
    )


# ============================================================
# ADMIN
# ============================================================

def begin_admin(chat_id, user_id):
    admin_sessions[user_id] = {"authenticated": False, "step": "passcode"}
    send_message(
        chat_id,
        "🔐 <b>ADMIN OUTLET MANAGEMENT</b>\n\n"
        "Masukkan Admin Passcode."
    )


def show_admin_menu(chat_id, user_id):
    admin_sessions[user_id] = {"authenticated": True, "step": "menu"}
    send_message(
        chat_id,
        "⚙️ <b>ADMIN OUTLET MANAGEMENT</b>\n\n"
        "Urus senarai outlet terus dari Telegram.",
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
            send_message(chat_id, "⚠️ Code outlet tidak sah.")
            return True

        with state_lock:
            if outlet in OUTLETS:
                send_message(chat_id, f"ℹ️ <b>{safe_html(outlet)}</b> sudah ada.", admin_keyboard())
                adm["step"] = "menu"
                return True
            OUTLETS.append(outlet)

        adm["step"] = "menu"
        send_message(
            chat_id,
            f"✅ Outlet <b>{safe_html(outlet)}</b> ditambah.",
            admin_keyboard()
        )
        return True

    return False


# ============================================================
# STAFF MESSAGE HANDLER
# ============================================================

def handle_message(message):
    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    user_id = user.get("id")
    if not user_id:
        return

    text = message.get("text", "").strip()

    if text == "/admin":
        begin_admin(chat_id, user_id)
        return

    if handle_admin_text(chat_id, user_id, text):
        return

    if text in ["/start", "/new"]:
        start_record(chat_id, user_id)
        return

    session = get_session(user_id)

    if not session:
        send_message(
            chat_id,
            "👋 <b>SGSB Bagging Bot V2</b>\n\n"
            "Tekan /start untuk rekod baru.\n"
            "Admin: /admin"
        )
        return

    if session["step"] == "seal":
        if not text:
            send_message(chat_id, "⚠️ Masukkan Seal Bag Number dalam bentuk teks.")
            return

        seal = normalize_seal(text)
        if not seal:
            send_message(chat_id, "⚠️ Seal Bag Number tidak sah.")
            return

        session["seal"] = seal
        session["step"] = "media"

        send_message(
            chat_id,
            "✅ <b>Seal direkodkan</b>\n\n"
            f"{media_status(session)}\n\n"
            "Sekarang hantar <b>1 gambar seal</b> dan <b>1 video bagging</b>.\n\n"
            "Telegram akan mengurus upload video. Jika internet terputus sekejap, "
            "biarkan Telegram menyambung penghantaran.",
            media_keyboard(session)
        )
        return

    if session["step"] == "remark":
        if not text:
            send_message(chat_id, "⚠️ Taip remark dalam bentuk teks.")
            return
        session["remark"] = text[:1000]
        session["step"] = "media"
        send_message(
            chat_id,
            f"✅ Remark disimpan.\n\n{media_status(session)}",
            media_keyboard(session)
        )
        return

    if session["step"] != "media":
        return

    # Exactly one photo
    if "photo" in message:
        if session.get("photo"):
            send_message(
                chat_id,
                "⚠️ Rekod ini sudah mempunyai 1 gambar.\n"
                "Jika tersalah gambar, batal rekod dan buat semula.",
                media_keyboard(session)
            )
            return

        session["photo"] = {
            "chat_id": chat_id,
            "message_id": message["message_id"]
        }

        send_message(
            chat_id,
            f"📷 <b>Gambar diterima</b>\n\n{media_status(session)}",
            media_keyboard(session)
        )
        return

    # Exactly one Telegram video
    if "video" in message:
        if session.get("video"):
            send_message(
                chat_id,
                "⚠️ Rekod ini sudah mempunyai 1 video.\n"
                "1 Seal hanya dibenarkan 1 video.",
                media_keyboard(session)
            )
            return

        session["video"] = {
            "chat_id": chat_id,
            "message_id": message["message_id"],
            "kind": "video"
        }

        send_message(
            chat_id,
            f"🎥 <b>Video diterima</b>\n\n{media_status(session)}",
            media_keyboard(session)
        )
        return

    # Video sent as File/Document
    if "document" in message:
        doc = message["document"]
        mime = doc.get("mime_type", "")
        if mime.startswith("video/"):
            if session.get("video"):
                send_message(
                    chat_id,
                    "⚠️ Rekod ini sudah mempunyai 1 video.\n"
                    "1 Seal hanya dibenarkan 1 video.",
                    media_keyboard(session)
                )
                return

            session["video"] = {
                "chat_id": chat_id,
                "message_id": message["message_id"],
                "kind": "document"
            }

            send_message(
                chat_id,
                f"🎥 <b>Video/File diterima</b>\n\n{media_status(session)}",
                media_keyboard(session)
            )
            return

    send_message(
        chat_id,
        "📷 Hantar 1 gambar seal dan 🎥 1 video bagging.",
        media_keyboard(session)
    )


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
        start_record(chat_id, user_id)
        return

    # Admin callbacks
    if data.startswith("admin:"):
        adm = admin_sessions.get(user_id)
        if not adm or not adm.get("authenticated"):
            send_message(chat_id, "🔐 Sesi admin tamat. Taip /admin semula.")
            return

        if data == "admin:menu":
            show_admin_menu(chat_id, user_id)
            return

        if data == "admin:list":
            with state_lock:
                outlets = sorted(OUTLETS)
            text = "\n".join(f"• {safe_html(x)}" for x in outlets) or "Tiada outlet."
            send_message(
                chat_id,
                f"📋 <b>SENARAI OUTLET</b>\n\n{text}\n\nTotal: {len(outlets)}",
                admin_keyboard()
            )
            return

        if data == "admin:add":
            adm["step"] = "add"
            send_message(chat_id, "➕ Taip <b>Outlet Code</b> baru.\nContoh: SBH999")
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
                if outlet in OUTLETS:
                    OUTLETS.remove(outlet)
                    ok = True
                else:
                    ok = False
            send_message(
                chat_id,
                f"{'✅ Dipadam' if ok else 'ℹ️ Tidak dijumpai'}: <b>{safe_html(outlet)}</b>",
                admin_keyboard()
            )
            return

        if data == "admin:logout":
            admin_sessions.pop(user_id, None)
            send_message(chat_id, "🚪 Admin logout.")
            return

    session = get_session(user_id)

    if data.startswith("outlet:"):
        if not session:
            session = create_session(user_id)

        outlet = data.split(":", 1)[1]
        with state_lock:
            valid = outlet in OUTLETS

        if not valid:
            send_message(chat_id, "⚠️ Outlet tidak sah / sudah dipadam.")
            return

        session["outlet"] = outlet
        session["step"] = "seal"

        send_message(
            chat_id,
            f"🏢 Outlet: <b>{safe_html(outlet)}</b>\n\n"
            "Masukkan <b>Seal Bag Number</b>."
        )
        return

    if not session:
        send_message(chat_id, "Sesi tamat. Tekan /start untuk mula semula.")
        return

    if data == "remark":
        session["step"] = "remark"
        send_message(chat_id, "📝 Taip remark sekarang.")
        return

    if data == "cancel":
        with state_lock:
            sessions.pop(user_id, None)
        send_message(
            chat_id,
            "❌ Rekod dibatalkan.",
            new_record_keyboard()
        )
        return

    if data == "submit":
        submit_record(chat_id, user, session)
        return


# ============================================================
# SUBMIT + ASYNC ARCHIVE QUEUE
# ============================================================

def submit_record(chat_id, user, session):
    if not session.get("outlet"):
        send_message(chat_id, "⚠️ Outlet belum dipilih.")
        return

    if not session.get("seal"):
        send_message(chat_id, "⚠️ Seal belum dimasukkan.")
        return

    if not session.get("photo"):
        send_message(
            chat_id,
            "⚠️ <b>1 gambar seal</b> diperlukan.",
            media_keyboard(session)
        )
        return

    if not session.get("video"):
        send_message(
            chat_id,
            "⚠️ <b>1 video bagging</b> diperlukan.",
            media_keyboard(session)
        )
        return

    submitted_at = now_my()
    username = user.get("username")
    first_name = user.get("first_name", "Staff")
    submitted_by = f"{first_name} (@{username})" if username else first_name

    record_id = f"{session['outlet']}-{submitted_at.strftime('%Y%m%d-%H%M%S')}"

    job = {
        "record_id": record_id,
        "outlet": session["outlet"],
        "seal": session["seal"],
        "photo": dict(session["photo"]),
        "video": dict(session["video"]),
        "remark": session.get("remark") or "-",
        "submitted_by": submitted_by,
        "submitted_at": submitted_at.isoformat(),
        "staff_chat_id": chat_id
    }

    # Queue first, then immediately release the staff session.
    archive_queue.put(job)
    with state_lock:
        sessions.pop(user["id"], None)

    send_message(
        chat_id,
        "✅ <b>SUBMISSION DITERIMA</b>\n\n"
        f"🆔 <code>{safe_html(record_id)}</code>\n"
        f"🏢 <b>{safe_html(job['outlet'])}</b>\n"
        f"🔒 <b>{safe_html(job['seal'])}</b>\n"
        "📷 Gambar: ✅\n"
        "🎥 Video: ✅\n\n"
        "📤 Archive sedang diproses di belakang.\n"
        "Anda boleh terus buat bagging seterusnya.",
        new_record_keyboard()
    )


def archive_worker():
    while True:
        job = archive_queue.get()
        try:
            process_archive_job(job)
        except Exception as e:
            print("Archive worker error:", e, flush=True)
            try:
                send_message(
                    job.get("staff_chat_id"),
                    "⚠️ <b>ARCHIVE GAGAL</b>\n\n"
                    f"Record: <code>{safe_html(job.get('record_id'))}</code>\n"
                    "Media asal masih berada dalam chat Telegram.\n"
                    "Sila maklumkan admin."
                )
            except Exception:
                pass
        finally:
            archive_queue.task_done()


def process_archive_job(job):
    submitted_at = datetime.fromisoformat(job["submitted_at"]).astimezone(TZ)
    outlet_tag = hashtag(job["outlet"])
    seal_tag = hashtag(job["seal"])
    date_tag = submitted_at.strftime("%Y%m%d")

    # Archive Format V2:
    # 1 compact record card + photo + video.
    # The large video is copied server-side inside Telegram; Render does not download it.
    header = (
        "📦 <b>BAGGING RECORD</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <code>{safe_html(job['record_id'])}</code>\n"
        f"🏢 <b>{safe_html(job['outlet'])}</b>\n"
        f"🔒 <b>{safe_html(job['seal'])}</b>\n"
        f"🕒 {submitted_at.strftime('%d/%m/%Y • %I:%M %p')}\n"
        f"👤 {safe_html(job['submitted_by'])}\n"
        f"📝 {safe_html(job['remark'])}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📷 1 Photo  •  🎥 1 Video\n\n"
        f"#{outlet_tag} #{date_tag} #SEAL_{seal_tag}"
    )

    result = send_message(ARCHIVE_CHAT_ID, header)
    if not result.get("ok"):
        raise RuntimeError(f"Archive header failed: {result}")

    photo_caption = (
        f"📷 <b>SEAL PHOTO</b> • {safe_html(job['outlet'])} • "
        f"{safe_html(job['seal'])}\n"
        f"<code>{safe_html(job['record_id'])}</code>"
    )
    photo_result = copy_message(
        job["photo"]["chat_id"],
        job["photo"]["message_id"],
        photo_caption
    )
    if not photo_result.get("ok"):
        raise RuntimeError(f"Photo copy failed: {photo_result}")

    video_caption = (
        f"🎥 <b>BAGGING VIDEO</b> • {safe_html(job['outlet'])} • "
        f"{safe_html(job['seal'])}\n"
        f"<code>{safe_html(job['record_id'])}</code>"
    )
    video_result = copy_message(
        job["video"]["chat_id"],
        job["video"]["message_id"],
        video_caption
    )
    if not video_result.get("ok"):
        raise RuntimeError(f"Video copy failed: {video_result}")


# ============================================================
# WORKER STARTUP
# ============================================================

_worker_started = False
_worker_lock = threading.Lock()


def ensure_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=archive_worker, daemon=True, name="archive-worker")
        t.start()
        _worker_started = True


ensure_worker()


# ============================================================
# WEB ROUTES
# ============================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "service": "SGSB Bagging Bot V2",
        "queue": archive_queue.qsize(),
        "outlets": len(OUTLETS)
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token", ""
        )
        if received_secret != WEBHOOK_SECRET:
            return jsonify({"ok": False}), 403

    update = request.get_json(silent=True) or {}
    update_id = update.get("update_id")

    if not remember_update(update_id):
        return jsonify({"ok": True, "duplicate": True})

    try:
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
    except Exception as e:
        print("Webhook processing error:", e, flush=True)

    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
