# Bot latency improvement — 23 October 2026

## Baseline findings

- The Render service is healthy and located in Singapore.
- CPU and memory were below their service limits; resource exhaustion was not observed.
- Every Telegram update fetched the outlet master again.
- Telegram and Supabase calls opened fresh HTTP connections.
- One Gunicorn thread caused one waiting request to block other staff.
- Callback acknowledgement waited behind database work.
- Render Free may sleep after inactivity and cold-start on the next request.

## Implemented

- Cache and reuse the verified outlet master after startup.
- Reuse HTTP connections independently for each request thread.
- Run four Gunicorn threads in one process.
- Serialize updates per Telegram actor/chat to preserve draft order.
- Acknowledge Telegram callback buttons before database processing.

## Data protection

- No database schema or production record migration.
- Existing idempotency claim/finish logic remains active.
- Duplicate update, ambiguous-send, draft, access, media and archive protections remain covered by tests.

## Verification

- `python -m unittest discover -s fleet_tests -v`: 36/36 passed.
- Python compilation: passed.
- `git diff --check`: passed.

## Remaining platform limit

The free Render plan may cold-start after an idle period. Warm requests benefit from these changes. Removing idle cold starts requires a separately approved paid always-on plan.
