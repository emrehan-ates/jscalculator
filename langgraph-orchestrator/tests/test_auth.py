"""User store (SQLite + bcrypt), app JWT sessions and the /signup page."""
import asyncio
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jwt  # noqa: E402


def fresh_modules(db_path, **env):
    base = {"USERS_DB": db_path, "APP_JWT_SECRET": "s" * 40,
            "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "AdminPass123",
            "ADMIN_EMAIL": "admin@example.com", "ADMIN_TEAM": "MAY1"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import users, tokens  # noqa: E401
    users = importlib.reload(users)
    tokens = importlib.reload(tokens)
    users.init()
    return users, tokens, patcher


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "users.db")
        self.users, self.tokens, self.patcher = fresh_modules(self.db)

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_admin_bootstrapped_from_env_and_hash_stored(self):
        admin = self.users.verify_user("admin", "AdminPass123")
        self.assertEqual((admin["role"], admin["status"], admin["team"]), ("admin", "active", "MAY1"))
        raw = sqlite3.connect(self.db).execute(
            "SELECT password_hash FROM users WHERE username='admin'").fetchone()[0]
        self.assertTrue(raw.startswith("$2"))
        self.assertNotIn("AdminPass123", raw)
        self.assertIsNone(self.users.verify_user("admin", "wrong"))

    def test_env_password_change_updates_hash_and_revokes_tokens(self):
        old = self.tokens.issue_token(self.users.verify_user("admin", "AdminPass123"))
        with mock.patch.dict(os.environ, {"ADMIN_PASSWORD": "NewAdminPass456"}):
            self.users.init()
        self.assertIsNone(self.users.verify_user("admin", "AdminPass123"))
        self.assertIsNotNone(self.users.verify_user("admin", "NewAdminPass456"))
        with self.assertRaises(self.tokens.InvalidSession):
            self.tokens.verify_token(old)

    def test_signup_pending_until_approved(self):
        user, err = self.users.create_signup("sevil", "Password1", "s@example.com", "msy")
        self.assertIsNone(err)
        self.assertEqual((user["status"], user["team"]), ("pending", "MSY"))
        self.assertIsNone(self.users.verify_user("sevil", "Password1"))

        self.users.decide_signup("sevil", True, "admin", role="user")
        self.assertEqual(self.users.verify_user("sevil", "Password1")["status"], "active")
        _, err = self.users.decide_signup("sevil", True, "admin")
        self.assertIn("already active", err)

    def test_rejected_and_invalid_signups(self):
        self.users.create_signup("bob", "Password1", "b@example.com", "CSY")
        self.users.decide_signup("bob", False, "admin")
        self.assertIsNone(self.users.verify_user("bob", "Password1"))
        self.assertIn("taken", self.users.create_signup("BOB", "Password1", "b@x.io", "CSY")[1])
        self.assertIn("at least", self.users.create_signup("eve", "short", "e@x.io", "CSY")[1])
        self.assertIn("e-mail", self.users.create_signup("eve", "Password1", "nope", "CSY")[1])

    def test_token_carries_session_and_is_revoked_on_change(self):
        self.users.add_user("ali", "Password1", "a@example.com", "MSY")
        token = self.tokens.issue_token(self.users.verify_user("ali", "Password1"))
        claims = self.tokens.verify_token(token)
        self.assertEqual((claims["username"], claims["role"], claims["team"]), ("ali", "user", "MSY"))

        self.users.update_user("ali", team="CSY")
        with self.assertRaises(self.tokens.InvalidSession):
            self.tokens.verify_token(token)
        fresh = self.tokens.issue_token(self.users.verify_user("ali", "Password1"))
        self.assertEqual(self.tokens.verify_token(fresh)["team"], "CSY")

        self.users.update_user("ali", status="disabled")
        with self.assertRaises(self.tokens.InvalidSession):
            self.tokens.verify_token(fresh)

    def test_tampered_and_expired_tokens_rejected(self):
        user = self.users.verify_user("admin", "AdminPass123")
        forged = jwt.encode({"sub": "admin", "role": "admin", "ver": user["token_version"],
                             "iss": "networkgpt", "iat": 0, "exp": 9999999999},
                            "x" * 40, algorithm="HS256")
        with self.assertRaises(self.tokens.InvalidSession):
            self.tokens.verify_token(forged)
        with mock.patch.dict(os.environ, {"APP_JWT_TTL_HOURS": "-1"}):
            expired = self.tokens.issue_token(user)
        with self.assertRaises(self.tokens.InvalidSession):
            self.tokens.verify_token(expired)
        with self.assertRaises(self.tokens.InvalidSession):
            self.tokens.verify_token(None)

    def test_short_secret_refused(self):
        with mock.patch.dict(os.environ, {"APP_JWT_SECRET": "short", "CHAINLIT_AUTH_SECRET": ""}):
            with self.assertRaises(RuntimeError):
                self.tokens.issue_token(self.users.verify_user("admin", "AdminPass123"))

    def test_signup_page(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import signup
        signup = importlib.reload(signup)

        app = FastAPI()

        @app.get("/{full_path:path}")
        async def catch_all(full_path: str):  # stands in for Chainlit's frontend route
            return {"frontend": full_path}

        signup.register(app)
        client = TestClient(app)
        self.assertIn("Request access", client.get("/signup").text)

        sent = []

        async def fake_mail(to, subject, body):
            sent.append((to, subject))
        with mock.patch.object(signup, "send_email", fake_mail):
            r = client.post("/signup", data={"username": "veli", "email": "v@example.com",
                                             "team": "CSY", "password": "Password1",
                                             "password2": "Password1"})
        self.assertIn("administrator approves", r.text)
        self.assertEqual(self.users.get_user("veli")["status"], "pending")
        self.assertEqual(sent[0][0], ["admin@example.com"])

        r = client.post("/signup", data={"username": "<script>", "email": "x@example.com",
                                         "team": "CSY", "password": "Password1",
                                         "password2": "Password1"})
        self.assertNotIn("<script>", r.text)
        r = client.post("/signup", data={"username": "ayse", "email": "x@example.com",
                                         "team": "CSY", "password": "Password1",
                                         "password2": "Password2"})
        self.assertIn("do not match", r.text)


if __name__ == "__main__":
    unittest.main()
