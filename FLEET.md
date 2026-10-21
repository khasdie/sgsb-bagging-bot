# Telegram FLEET integration candidate

This directory is a separate, additive extension of SGSB Bagging Bot V5.1.1.
Do not publish to the auto-deploy main branch until isolated SQL/handler tests,
the private FLEET channel identity/permissions, backend preview authorization,
and live source parity gates pass. Existing Bagging tables and handlers stay intact.

Storage: new service-only `sgsb_fleet_reports` and `sgsb_fleet_events` tables.
Only metadata and Telegram file references persist; media remains in Telegram.
BTM receives scoped projections and bounded authenticated image reads, never bot
tokens, file identifiers or token-bearing Telegram download URLs.

Reports are immutable after confirmation. A subsequent report keeps a distinct
identity and history; the dashboard counts only the latest confirmed report per
outlet/date. Manual BTM records are kept separate and are never overwritten.

Required deployment configuration (values must use official secret storage):
`FLEET_ARCHIVE_CHAT_ID`, `FLEET_BTM_READ_TOKEN`, `FLEET_ENABLED=1` on Render;
`FLEET_TELEGRAM_URL`, `FLEET_TELEGRAM_READ_TOKEN` on BTM API.
The channel must differ from `ARCHIVE_CHAT_ID`. Feature remains disabled until
configuration is complete. Do not copy BOT_TOKEN into BTM or frontend assets.

Staff can report only explicitly assigned outlets. The initial allowlist is a
frozen baseline of accepted bot ownership, guarded by the exact record hash in
bootstrap-access.sql; future Bagging submissions never grant Fleet access.
`/fleet_access TelegramUserId SBH307,SBH458` (or NONE) requires the existing
authenticated bot administrator session and affects Fleet only.
At cutover staff retain outlets they already own in the existing bot records;
an authenticated bot administrator can report any approved SBH outlet. New
staff without a verified association require an explicit access assignment;
knowing an outlet code alone never grants write access.
