"""Local accounts and revocable sessions. Credentials live outside jobs/cache."""
from pathlib import Path
from http.cookies import SimpleCookie, CookieError
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

COOKIE = 'ct_session'
ROLES = {'admin': '管理员', 'user': '上传用户', 'viewer': '只读用户'}
ITERATIONS = 600_000
TTL = 12 * 3600
IDLE = 3600


class Forbidden(ValueError):
    pass


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def validate_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValueError('密码须为 12 至 128 个字符')


class Accounts:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.login_slots = threading.BoundedSemaphore(2)
        self.failures = {}
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, salt TEXT NOT NULL,
                    password TEXT NOT NULL, role TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf TEXT NOT NULL,
                    expires REAL NOT NULL, touched REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
            ''')
        if os.name != 'nt':
            os.chmod(self.path.parent, 0o700)
            os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def users(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT id,username,role,disabled FROM users ORDER BY username')]

    def create(self, username, password, role='user'):
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{2,31}', username):
            raise ValueError('账号须为 3 至 32 位字母、数字、下划线、短横线或句点')
        if not isinstance(role,str) or role not in ROLES:
            raise ValueError('账号权限无效')
        validate_password(password)
        salt = secrets.token_hex(16)
        value = password_hash(password, salt)
        uid = secrets.token_hex(16)
        try:
            with self.lock, self.connect() as db:
                db.execute('INSERT INTO users(id,username,salt,password,role) VALUES(?,?,?,?,?)',
                           (uid, username, salt, value, role))
        except sqlite3.IntegrityError:
            raise ValueError('账号已存在') from None
        return uid

    def login(self, username, password, address):
        if not isinstance(username, str) or not isinstance(password, str) or len(username)>128 or len(password)>128:
            return None
        now = time.time()
        keys = [('ip', str(address)), ('user', username)]
        with self.lock:
            self.failures = {key:value for key,value in self.failures.items() if value[1]>now-900}
            if len(self.failures)>4096 or any(self.failures.get(key,(0,0))[0] >= (10 if key[0]=='user' else 20) for key in keys):
                raise Forbidden('登录尝试过多，请 15 分钟后重试')
        if not self.login_slots.acquire(blocking=False):
            raise Forbidden('登录繁忙，请稍后重试')
        try:
            with self.connect() as db:
                row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            # Unknown accounts still pay the same password verification cost.
            value = password_hash(password, row['salt'] if row else '00'*16)
            valid = row is not None and hmac.compare_digest(value, row['password']) and not row['disabled']
            if not valid:
                with self.lock:
                    for key in keys:
                        self.failures[key] = (self.failures.get(key,(0,0))[0]+1,now)
                return None
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            with self.lock, self.connect() as db:
                # Recheck after verification: a concurrent disable/reset must win.
                current = db.execute('SELECT password,disabled FROM users WHERE id=?',(row['id'],)).fetchone()
                if not current or current['disabled'] or current['password']!=row['password']:
                    return None
                db.execute('DELETE FROM sessions WHERE expires<? OR touched<?',(now,now-IDLE))
                # Bound active sessions per account without evicting other accounts.
                db.execute('DELETE FROM sessions WHERE token IN (SELECT token FROM sessions WHERE user_id=? ORDER BY touched DESC LIMIT -1 OFFSET 19)',(row['id'],))
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), row['id'],csrf,now+TTL,now))
                with self.lock:
                    self.failures.pop(keys[1], None)
            return token
        finally:
            self.login_slots.release()

    def session(self, cookie):
        try:
            jar = SimpleCookie(); jar.load(cookie or '')
            token = jar[COOKIE].value
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
                return None
        except (CookieError,KeyError):
            return None
        digest = hashlib.sha256(token.encode()).hexdigest(); now = time.time()
        with self.connect() as db:
            row = db.execute('''SELECT u.id,u.username,u.role,s.csrf,s.token FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token=? AND s.expires>? AND s.touched>? AND u.disabled=0''',(digest,now,now-IDLE)).fetchone()
            if not row:return None
            db.execute('UPDATE sessions SET touched=? WHERE token=? AND touched<?',(now,digest,now-60))
            return dict(row)

    def logout(self, principal):
        with self.connect() as db:db.execute('DELETE FROM sessions WHERE token=?',(principal['token'],))

    def change(self, uid, request, actor):
        if not isinstance(request,dict) or (actor['role']!='admin' and uid!=actor['id']):
            raise Forbidden('无权修改此账号')
        password = request.get('password')
        if password is not None:validate_password(password)
        with self.lock, self.connect() as db:
            row=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            if not row:raise ValueError('账号不存在')
            role, disabled = row['role'], row['disabled']
            if actor['role']=='admin':
                role=request.get('role',role);disabled=request.get('disabled',bool(disabled))
                if not isinstance(role,str) or role not in ROLES or not isinstance(disabled,bool):raise ValueError('账号权限无效')
            elif not isinstance(request.get('old_password'),str) or not hmac.compare_digest(password_hash(request['old_password'],row['salt']),row['password']):
                raise Forbidden('当前密码不正确')
            if row['role']=='admin' and not row['disabled'] and (role!='admin' or disabled):
                count=db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0").fetchone()[0]
                if count<=1:raise ValueError('须保留至少一个启用的管理员账号')
            if password is not None:
                salt=secrets.token_hex(16)
                db.execute('UPDATE users SET salt=?,password=? WHERE id=?',(salt,password_hash(password,salt),uid))
            db.execute('UPDATE users SET role=?,disabled=? WHERE id=?',(role,int(disabled),uid))
            db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))


def can_manage(principal, status):
    return principal['role']=='admin' or (principal['role']=='user' and status.get('owner_id')==principal['id'])


def authorize_cleanup(principal, request, statuses):
    if principal['role']=='admin':return
    if request.get('action')=='cache':
        raise Forbidden('共享缓存仅管理员可清理')
    ids=request.get('job_ids',[])
    if not isinstance(ids,list) or any(not isinstance(sid,str) or sid not in statuses or not can_manage(principal,statuses[sid]) for sid in ids):
        raise Forbidden('只能清理您上传的检查')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Initialize the first administrator; never overwrite accounts')
    parser.add_argument('--database',required=True)
    parser.add_argument('--credentials-file',required=True)
    args=parser.parse_args()
    store=Accounts(args.database)
    if store.users():raise SystemExit('Accounts already initialized')
    password=secrets.token_urlsafe(21)
    descriptor=os.open(args.credentials_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
        stream.write('账号：admin\n密码：'+password+'\n登录后可在「修改密码」中更改。\n')
    store.create('admin',password,'admin')
    print('Administrator initialized; credentials saved to the requested private file.')
