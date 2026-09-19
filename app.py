import os
import requests
from flask import Flask, request, jsonify
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ARCHIVE_CHAT_ID = os.environ.get("ARCHIVE_CHAT_ID", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ============================================================
# OUTLET MASTER
# Ubah / tambah outlet di sini kemudian jika diperlukan.
# ============================================================

OUTLETS = [
    "SBH307",
    "SBH458",
    "SBH001",
    "SBH002",
    "SBH003",
]

# Temporary session.
# Sesuai untuk workflow aktif.
# Jangan gunakan ini sebagai permanent database.
sessions = {}


# ============================================================
# TELEGRAM HELPERS
# ============================================================

def telegram(method, payload=None):
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN missing"}

    try:
        r = requests.post(
            f"{API}/{method}",
            json=payload or {},
            timeout=20
        )
        return r.json()
    except Exception as e:
        print("Telegram API error:", e)
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
        payload["text"] = text

    return telegram("answerCallbackQuery", payload)


def copy_message(from_chat_id, message_id, caption=None):
    payload = {
        "chat_id": ARCHIVE_CHAT_ID,
        "from_chat_id": from_chat_id,
        "message_id": message_id
    }

    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"

    return telegram("copyMessage", payload)


# ============================================================
# KEYBOARDS
# ============================================================

def outlet_keyboard():
    rows = []

    for i in range(0, len(OUTLETS), 2):
        row = []

        for outlet in OUTLETS[i:i+2]:
            row.append({
                "text": outlet,
                "callback_data": f"outlet:{outlet}"
            })

        rows.append(row)

    return {"inline_keyboard": rows}


def media_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📝 Tambah Remark",
                    "callback_data": "remark"
                }
            ],
            [
                {
                    "text": "✅ SELESAI & SUBMIT",
                    "callback_data": "submit"
                }
            ],
            [
                {
                    "text": "❌ Batal",
                    "callback_data": "cancel"
                }
            ]
        ]
    }


def new_record_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "➕ NEW BAGGING",
                    "callback_data": "new"
                }
            ]
        ]
    }


# ============================================================
# SESSION
# ============================================================

def create_session(user_id):
    sessions[user_id] = {
        "step": "outlet",
        "outlet": None,
        "seal": None,
        "photos": [],
        "videos": [],
        "documents": [],
        "remark": "",
        "started_at": datetime.now(
            ZoneInfo("Asia/Kuala_Lumpur")
        ).isoformat()
    }

    return sessions[user_id]


def get_session(user_id):
    return sessions.get(user_id)


# ============================================================
# START
# ============================================================

def start_record(chat_id, user_id):
    create_session(user_id)

    send_message(
        chat_id,
        (
            "📦 <b>SGSB BAGGING RECORD</b>\n\n"
            "Sila pilih <b>Outlet Code</b>:"
        ),
        outlet_keyboard()
    )


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
    if text in ["/start", "/new"]:
        start_record(chat_id, user_id)
        return

    session = get_session(user_id)

    if not session:
        send_message(
            chat_id,
            (
                "👋 <b>SGSB Bagging Bot</b>\n\n"
                "Tekan /start untuk membuat rekod bagging baru."
            )
        )
        return

    # ========================================================
    # WAITING FOR SEAL
    # ========================================================

    if session["step"] == "seal":
        if not text:
            send_message(
                chat_id,
                "⚠️ Sila masukkan Seal Bag Number dalam bentuk teks."
            )
            return

        seal = text.upper().strip()

        if len(seal) > 100:
            send_message(
                chat_id,
                "⚠️ Seal Bag Number terlalu panjang."
            )
            return

        session["seal"] = seal
        session["step"] = "media"

        send_message(
            chat_id,
            (
                "✅ <b>Seal direkodkan</b>\n\n"
                f"🏢 Outlet: <b>{session['outlet']}</b>\n"
                f"🔒 Seal: <b>{session['seal']}</b>\n\n"
                "Sekarang hantar:\n"
                "📷 sekurang-kurangnya 1 gambar seal\n"
                "🎥 sekurang-kurangnya 1 video bagging\n\n"
                "Anda boleh menghantar lebih daripada satu gambar/video.\n\n"
                "Apabila selesai, tekan <b>SELESAI & SUBMIT</b>."
            ),
            media_keyboard()
        )
        return

    # ========================================================
    # WAITING FOR REMARK
    # ========================================================

    if session["step"] == "remark":
        if not text:
            send_message(
                chat_id,
                "⚠️ Sila masukkan remark dalam bentuk teks."
            )
            return

        session["remark"] = text[:1000]
        session["step"] = "media"

        send_message(
            chat_id,
            (
                "✅ Remark disimpan.\n\n"
                f"📝 {session['remark']}\n\n"
                "Anda boleh terus tambah gambar/video atau tekan "
                "<b>SELESAI & SUBMIT</b>."
            ),
            media_keyboard()
        )
        return

    # ========================================================
    # MEDIA
    # ========================================================

    if session["step"] == "media":

        if "photo" in message:
            session["photos"].append({
                "chat_id": chat_id,
                "message_id": message["message_id"]
            })

            send_message(
                chat_id,
                (
                    "📷 <b>Gambar diterima</b>\n\n"
                    f"Jumlah gambar: {len(session['photos'])}\n"
                    f"Jumlah video: {len(session['videos']) + len(session['documents'])}"
                ),
                media_keyboard()
            )
            return

        if "video" in message:
            session["videos"].append({
                "chat_id": chat_id,
                "message_id": message["message_id"]
            })

            send_message(
                chat_id,
                (
                    "🎥 <b>Video diterima</b>\n\n"
                    f"Jumlah gambar: {len(session['photos'])}\n"
                    f"Jumlah video: {len(session['videos']) + len(session['documents'])}"
                ),
                media_keyboard()
            )
            return

        # Some users may send video as "File/Document"
        if "document" in message:
            doc = message["document"]
            mime = doc.get("mime_type", "")

            if mime.startswith("video/"):
                session["documents"].append({
                    "chat_id": chat_id,
                    "message_id": message["message_id"]
                })

                send_message(
                    chat_id,
                    (
                        "🎥 <b>Video/File diterima</b>\n\n"
                        f"Jumlah gambar: {len(session['photos'])}\n"
                        f"Jumlah video: {len(session['videos']) + len(session['documents'])}"
                    ),
                    media_keyboard()
                )
                return

        send_message(
            chat_id,
            (
                "📷 Hantar gambar seal atau 🎥 video bagging.\n\n"
                "Jika semua media sudah dihantar, tekan "
                "<b>SELESAI & SUBMIT</b>."
            ),
            media_keyboard()
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

    if data == "new":
        start_record(chat_id, user_id)
        return

    session = get_session(user_id)

    if data.startswith("outlet:"):
        if not session:
            session = create_session(user_id)

        outlet = data.split(":", 1)[1]

        if outlet not in OUTLETS:
            send_message(chat_id, "⚠️ Outlet tidak sah.")
            return

        session["outlet"] = outlet
        session["step"] = "seal"

        send_message(
            chat_id,
            (
                f"🏢 Outlet: <b>{outlet}</b>\n\n"
                "Sekarang masukkan <b>Seal Bag Number</b>."
            )
        )
        return

    if not session:
        send_message(
            chat_id,
            "Sesi sudah tamat. Tekan /start untuk mula semula."
        )
        return

    if data == "remark":
        session["step"] = "remark"

        send_message(
            chat_id,
            (
                "📝 <b>Remark</b>\n\n"
                "Taip remark anda sekarang."
            )
        )
        return

    if data == "cancel":
        sessions.pop(user_id, None)

        send_message(
            chat_id,
            (
                "❌ Rekod dibatalkan.\n\n"
                "Tekan /start jika mahu membuat rekod baru."
            )
        )
        return

    if data == "submit":
        submit_record(chat_id, user, session)
        return


# ============================================================
# SUBMIT TO ARCHIVE CHANNEL
# ============================================================

def submit_record(chat_id, user, session):

    if not session.get("outlet"):
        send_message(chat_id, "⚠️ Outlet belum dipilih.")
        return

    if not session.get("seal"):
        send_message(chat_id, "⚠️ Seal Bag Number belum dimasukkan.")
        return

    if len(session["photos"]) < 1:
        send_message(
            chat_id,
            "⚠️ Sekurang-kurangnya <b>1 gambar seal</b> diperlukan.",
            media_keyboard()
        )
        return

    video_count = len(session["videos"]) + len(session["documents"])

    if video_count < 1:
        send_message(
            chat_id,
            "⚠️ Sekurang-kurangnya <b>1 video bagging</b> diperlukan.",
            media_keyboard()
        )
        return

    now = datetime.now(ZoneInfo("Asia/Kuala_Lumpur"))

    username = user.get("username")
    first_name = user.get("first_name", "Staff")

    if username:
        submitted_by = f"{first_name} (@{username})"
    else:
        submitted_by = first_name

    record_id = (
        f"{session['outlet']}-"
        f"{now.strftime('%Y%m%d-%H%M%S')}"
    )

    remark = session.get("remark") or "-"

    header = (
        "📦 <b>SGSB BAGGING RECORD</b>\n\n"
        f"🆔 Record: <code>{record_id}</code>\n"
        f"🏢 Outlet: <b>{session['outlet']}</b>\n"
        f"🔒 Seal: <b>{session['seal']}</b>\n"
        f"📅 Date: {now.strftime('%d/%m/%Y')}\n"
        f"🕐 Time: {now.strftime('%I:%M %p')}\n"
        f"👤 Submitted by: {submitted_by}\n"
        f"📷 Photos: {len(session['photos'])}\n"
        f"🎥 Videos: {video_count}\n"
        f"📝 Remark: {remark}\n\n"
        f"#{session['outlet']} "
        f"#SEAL{session['seal'].replace('-', '').replace(' ', '')} "
        f"#{now.strftime('%Y%m%d')}"
    )

    result = send_message(
        ARCHIVE_CHAT_ID,
        header
    )

    if not result.get("ok"):
        send_message(
            chat_id,
            (
                "❌ Gagal menghantar rekod ke Archive Channel.\n\n"
                "Sila hubungi admin dan jangan padam media ini."
            )
        )
        return

    # Copy photos
    for index, item in enumerate(session["photos"], start=1):
        copy_message(
            item["chat_id"],
            item["message_id"],
            (
                f"📷 Seal Photo {index}\n"
                f"🏢 {session['outlet']}\n"
                f"🔒 {session['seal']}"
            )
        )

    # Copy Telegram videos
    for index, item in enumerate(session["videos"], start=1):
        copy_message(
            item["chat_id"],
            item["message_id"],
            (
                f"🎥 Bagging Video {index}\n"
                f"🏢 {session['outlet']}\n"
                f"🔒 {session['seal']}"
            )
        )

    # Copy video documents/files
    start_index = len(session["videos"]) + 1

    for offset, item in enumerate(session["documents"]):
        copy_message(
            item["chat_id"],
            item["message_id"],
            (
                f"🎥 Bagging Video {start_index + offset}\n"
                f"🏢 {session['outlet']}\n"
                f"🔒 {session['seal']}"
            )
        )

    sessions.pop(user["id"], None)

    send_message(
        chat_id,
        (
            "✅ <b>BAGGING RECORD SUBMITTED</b>\n\n"
            f"🆔 {record_id}\n"
            f"🏢 Outlet: <b>{session['outlet']}</b>\n"
            f"🔒 Seal: <b>{session['seal']}</b>\n"
            f"📷 Photos: {len(session['photos'])}\n"
            f"🎥 Videos: {video_count}\n\n"
            "Rekod telah dihantar ke SGSB Bagging Archive."
        ),
        new_record_keyboard()
    )


# ============================================================
# WEB ROUTES
# ============================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "service": "SGSB Bagging Bot"
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy"
    })


@app.route("/webhook", methods=["POST"])
def webhook():

    # Optional Telegram secret-token verification
    if WEBHOOK_SECRET:
        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )

        if received_secret != WEBHOOK_SECRET:
            return jsonify({"ok": False}), 403

    update = request.get_json(silent=True) or {}

    try:
        if "message" in update:
            handle_message(update["message"])

        elif "callback_query" in update:
            handle_callback(update["callback_query"])

    except Exception as e:
        print("Webhook processing error:", e)

    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
