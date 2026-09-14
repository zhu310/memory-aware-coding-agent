"""Persistent single-owner authentication for the local workspace."""
import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path

SESSION_TTL = 7 * 24 * 3600
COOKIE = "agent_workspace_session"

class AuthError(ValueError):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status

class AuthStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS owner (id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (peer TEXT NOT NULL, at REAL NOT NULL);
            ''')

    def connect(self):
        return sqlite3.connect(str(self.path), timeout=15)

    def configured(self):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM owner").fetchone() is not None

    def authenticate(self, token):
        if not token or len(token)>256:
            return None
        with self.connect() as db:
            row=db.execute("SELECT username FROM owner WHERE EXISTS (SELECT 1 FROM sessions WHERE digest=? AND expires>?)", (self.digest(token), time.time())).fetchone()
            return row[0] if row else None

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def password_hash(password, salt):
        return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()

    def sign_in(self, username, password, peer, *, setup=False):
        now=time.time()
        # Reserve attempts before doing expensive password work, including across processes.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE at<?", (now-900,))
            count=db.execute("SELECT count(*) FROM attempts WHERE peer=?", (peer,)).fetchone()[0]
            if count>=10:
                raise AuthError("Too many attempts. Try again in 15 minutes.",429)
            db.execute("INSERT INTO attempts VALUES (?,?)",(peer,now))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT username,salt,password_hash FROM owner").fetchone()
            if setup:
                if row:
                    raise AuthError("Workspace account is already configured.",409)
                salt=secrets.token_hex(16)
                db.execute("INSERT INTO owner VALUES (1,?,?,?)",(username,salt,self.password_hash(password,salt)))
            else:
                salt=row[1] if row else '00'*16
                actual=self.password_hash(password,salt)
                if not row or not hmac.compare_digest(actual,row[2]) or not hmac.compare_digest(username.encode(),row[0].encode()):
                    raise AuthError("Incorrect username or password.")
            token=secrets.token_urlsafe(32)
            db.execute("DELETE FROM sessions WHERE expires<=?",(now,))
            db.execute("INSERT INTO sessions VALUES (?,?)",(self.digest(token),now+SESSION_TTL))
            db.execute("DELETE FROM attempts WHERE peer=?",(peer,))
            return token

    def logout(self, token):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE digest=?",(self.digest(token or ''),))
