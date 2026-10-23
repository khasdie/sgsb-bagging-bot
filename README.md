# SGSB Bagging Bot V5.1

Supabase stores outlet master, bagging metadata, staff sessions and processed Telegram update IDs. Telegram retains all images/videos and the archive albums. The production application does not call GitHub Gist.

Server environment: `BOT_TOKEN`, `ARCHIVE_CHAT_ID`, `WEBHOOK_SECRET`, `ADMIN_PASSCODE`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`. Never commit their values. Use a Supabase server secret key, not a publishable key.

Run `supabase_setup_v5_1.sql` against the project before deployment. Tables use RLS without public policies, and API privileges/functions are restricted to `service_role`. Existing outlets must be seeded before the bot starts. Start command remains `gunicorn app:app`; `gunicorn.conf.py` applies one shared process with four request threads and a 150-second request timeout. Telegram webhook must point to `/webhook` with the existing secret token. No pending updates should be dropped during cutover.

## Response performance

- Different staff can be served concurrently while existing shared workflow state remains consistent.
- Updates from the same Telegram user/chat are serialized to protect draft order.
- Telegram callback queries are acknowledged before database work so the button spinner clears immediately.
- Verified outlet data is reused after startup instead of being fetched for every update.
- Telegram and Supabase HTTP connections are reused per request thread.

The Render Free service can still cold-start after inactivity. That hosting delay requires an always-on Render plan to remove completely.

A temporary database failure returns a friendly staff message and HTTP 503 so Telegram can retry. Photo/video identifiers are persisted before acceptance. The database prevents duplicate active Outlet + Seal + Date records. Staff state survives process restarts; admin access expires after eight hours. Generated report files are temporary, never database storage.

Archive delivery is recorded as SENDING before contacting Telegram. An explicit Telegram failure retains media and allows retry. A timeout or a process/database failure after sending leaves UNKNOWN/SENDING; an admin must check the archive channel before retry to avoid duplicate albums. Successful sends retain both archive message IDs. To reconcile an uncertain send, locate its record ID in the archive and update metadata with the verified IDs; never blindly reset delivery state.

`migrate_gist_v5_1.py` is an offline importer, separate from the live app. It accepts legacy JSON backup paths, or fetches Gist using `GITHUB_TOKEN` and `OUTLET_GIST_ID`. API failures stop only migration. Imports preserve existing RIDs and live records; natural-key conflicts are retained in `sgsb_migration_conflicts` for review. Run it again safely to verify idempotency.
