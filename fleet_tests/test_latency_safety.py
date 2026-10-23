import ast
import unittest
from pathlib import Path


class LatencySafetyTests(unittest.TestCase):
    def test_gunicorn_allows_parallel_users_without_parallel_process_state(self):
        values = {}
        exec(Path('gunicorn.conf.py').read_text(encoding='utf8'), values)
        self.assertEqual(values['workers'], 1)
        self.assertEqual(values['worker_class'], 'gthread')
        self.assertEqual(values['threads'], 4)
        self.assertGreaterEqual(values['keepalive'], 5)

    def test_webhook_reuses_verified_outlets_and_serializes_each_actor(self):
        tree = ast.parse(Path('app.py').read_text(encoding='utf8'))
        webhook = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'webhook')
        source = ast.unparse(webhook)
        self.assertIn('with update_lock(update)', source)
        self.assertIn('ensure_outlets_available()', source)
        self.assertNotIn('load_outlets_from_supabase()', source)

    def test_callback_is_acknowledged_before_database_claim(self):
        text = Path('app.py').read_text(encoding='utf8')
        webhook = text[text.index('def webhook():'):text.index('# ============================================================\n# STARTUP')]
        self.assertLess(webhook.index("answer_callback(callback['id'])"), webhook.index("db.rpc('sgsb_claim_update'"))

    def test_supabase_and_telegram_use_thread_local_keepalive_sessions(self):
        app = Path('app.py').read_text(encoding='utf8')
        storage = Path('storage_v5_1.py').read_text(encoding='utf8')
        self.assertIn('http_local = threading.local()', app)
        self.assertGreaterEqual(app.count('http_session().post('), 3)
        self.assertIn('_http_local = threading.local()', storage)
        self.assertIn("_http().request(method", storage)


if __name__ == '__main__':
    unittest.main()
