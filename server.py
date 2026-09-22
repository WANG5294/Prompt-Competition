"""Local prompt contest demo. Python 3.10+, no third-party dependencies."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import unicodedata
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie

ROOT = Path(__file__).resolve().parent
for line in (ROOT / '.env').read_text().splitlines() if (ROOT / '.env').exists() else []:
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())
DB = Path(os.environ.get('CONTEST_DB', str(ROOT / 'data/contest-v2.sqlite3')))
MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-flash')
SESSIONS = {}
SESSION_LOCK = threading.Lock()
PASSWORD_SALT = b'local-contest-demo'
PASSWORD_HASH = hashlib.scrypt(b'mock', salt=PASSWORD_SALT, n=16384, r=8, p=1)
QUESTIONS = json.loads((ROOT / 'questions.json').read_text())

def connect():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS exam (user TEXT PRIMARY KEY, started REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS focus_events (
          user TEXT NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL,
          occurred REAL NOT NULL, received REAL NOT NULL,
          PRIMARY KEY(user, event_id));
        CREATE TABLE IF NOT EXISTS submissions (
          id INTEGER PRIMARY KEY, user TEXT NOT NULL, question INTEGER NOT NULL,
          prompt TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL,
          output TEXT, score INTEGER, reason TEXT, model TEXT NOT NULL,
          UNIQUE(user, question));
        ''')
        columns = {r['name'] for r in c.execute('PRAGMA table_info(focus_events)')}
        if 'penalty_seconds' not in columns:
            # Old warning-only records remain as history and are not charged retroactively.
            c.execute('ALTER TABLE focus_events ADD COLUMN penalty_seconds INTEGER NOT NULL DEFAULT 0')
        c.execute("UPDATE submissions SET status='error', reason='服务重启中断了判分，请重试原答案。' WHERE status='running'")

def timing(c, user):
    exam = c.execute('SELECT started FROM exam WHERE user=?', (user,)).fetchone()
    row = c.execute('SELECT count(*) AS n, coalesce(sum(penalty_seconds),0) AS total FROM focus_events WHERE user=? AND penalty_seconds>0', (user,)).fetchone()
    base_end = exam['started'] + 3600 if exam else None
    return {'baseEndsAt': base_end, 'endsAt': base_end - row['total'] if exam else None,
            'focusCount': row['n'], 'penaltySeconds': row['total'], 'serverTime': time.time()}

def record_focus(c, user, event_id, kind, occurred):
    c.execute('BEGIN IMMEDIATE')
    before = timing(c, user)
    old = c.execute('SELECT penalty_seconds FROM focus_events WHERE user=? AND event_id=?', (user, event_id)).fetchone()
    if old:
        return dict(before, ok=True, applied=bool(old['penalty_seconds']), duplicate=True, eventPenalty=old['penalty_seconds'])
    # Client event time permits offline replay; it is not trusted evidence of cheating.
    if not before['baseEndsAt'] or not (before['baseEndsAt']-3600 <= occurred < before['endsAt'] and occurred <= time.time()+5):
        return dict(before, ok=True, applied=False, eventPenalty=0, reason='事件不在有效作答时段内，未扣时。')
    penalty = 30 * (before['focusCount'] + 1)
    c.execute('INSERT INTO focus_events(user,event_id,kind,occurred,received,penalty_seconds) VALUES (?,?,?,?,?,?)',
              (user, event_id, kind, occurred, time.time(), penalty))
    return dict(timing(c, user), ok=True, applied=True, eventPenalty=penalty)

def normalize(s):
    return unicodedata.normalize('NFKC', s).casefold()

def blocked_word(prompt, q):
    # Ignore whitespace and zero-width formatting characters for blocked words.
    def clean(s):
        return ''.join(x for x in normalize(s) if not x.isspace() and unicodedata.category(x) != 'Cf')
    return next((word for word in q['blocked'] if clean(word) in clean(prompt)), None)

def grade(output, target):
    s = output.strip()
    if s == target:
        return 5, '输出与谜底完全一致（忽略首尾空白）。'
    if target not in s:
        return 0, '输出未连续包含谜底。'
    wrappers = ['谜底是', '谜底是：', '谜底是:', '答案是', '答案是：', '答案是:', '答案：', '答案:', '谜底：', '谜底:']
    variants = {p + target + end for p in ['', *wrappers] for end in ['', '。', '！', '.', '!']}
    variants |= {'“' + target + '”', '"' + target + '"', '「' + target + '」'}
    if s in variants:
        return 4, '仅包含规则白名单内的简短前后缀或标点。'
    extra = len(s) - len(target)
    if extra <= 30:
        return 2, f'包含谜底，额外字符 {extra} 个（不超过 30）。'
    return 1, f'包含谜底，额外字符 {extra} 个（至少 31）。'

def call_model(prompt):
    key = os.environ.get('DEEPSEEK_API_KEY', '')
    if not key:
        raise RuntimeError('未配置 API key，请在 .env 中配置后重启服务。')
    payload = {'model': MODEL, 'messages': [{'role': 'user', 'content': prompt}],
               'thinking': {'type': 'disabled'}, 'temperature': 0, 'max_tokens': 512, 'stream': False}
    req = urllib.request.Request('https://api.deepseek.com/chat/completions',
          data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    try:
        with urllib.request.urlopen(req, timeout=65) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        labels = {401: '密钥验证失败', 402: '账户余额不足', 429: '请求限流', 400: '模型参数或请求不受支持'}
        raise RuntimeError(f"DeepSeek HTTP {e.code}：{labels.get(e.code, '服务暂不可用')}。原答案已保留，可重试。") from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError('模型连接失败或超时。原答案已保留，可重试。') from None
    choice = data['choices'][0]
    if choice.get('finish_reason') != 'stop':
        raise RuntimeError('模型输出未正常完成，暂不计分，可重试原答案。')
    output = choice['message'].get('content')
    if not isinstance(output, str):
        raise RuntimeError('模型未返回有效文本，可重试原答案。')
    return output

def worker(stop_event=None):
    while stop_event is None or not stop_event.is_set():
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT * FROM submissions WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                c.execute("UPDATE submissions SET status='running' WHERE id=?", (row['id'],))
        if not row:
            time.sleep(.3)
            continue
        q = QUESTIONS[row['question'] - 1]
        try:
            hit = blocked_word(row['prompt'], q)
            if hit:
                output, score, reason = '', 0, f'提示词命中屏蔽词「{hit}」，未调用模型。'
            else:
                output = call_model(row['prompt'])
                score, reason = grade(output, q['target'])
            with connect() as c:
                c.execute("UPDATE submissions SET status='done', output=?, score=?, reason=? WHERE id=?", (output, score, reason, row['id']))
        except Exception as e:
            reason = str(e) if isinstance(e, RuntimeError) else '判分服务异常，原答案已保留，请重试。'
            with connect() as c:
                c.execute("UPDATE submissions SET status='error', reason=? WHERE id=?", (reason, row['id']))

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, status=200, cookie=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def user(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            sid = cookie['session'].value if 'session' in cookie else ''
            with SESSION_LOCK:
                expiry = SESSIONS.get(sid, 0)
            return '123456' if expiry > time.time() else None
        except Exception:
            return None

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/api/state':
            user = self.user()
            if not user:
                return self.send_json({'error': '请先登录'}, 401)
            with connect() as c:
                c.execute('BEGIN')
                clock = timing(c, user)
                rows = c.execute('SELECT * FROM submissions WHERE user=? ORDER BY question', (user,)).fetchall()
            return self.send_json({'user': user, **clock,
                'questions': QUESTIONS, 'submissions': [dict(x) for x in rows], 'model': MODEL})
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/guard.js': ('guard.js', 'application/javascript; charset=utf-8'), '/app.js': ('app.js', 'application/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if path not in files:
            return self.send_json({'error': '页面不存在'}, 404)
        name, mime = files[path]
        body = (ROOT / 'static' / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        origin = self.headers.get('Origin')
        if self.headers.get('X-Contest-Request') != '1' or (origin and origin != 'http://' + self.headers.get('Host', '')):
            return self.send_json({'error': '请求来源无效'}, 403)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size < 0 or size > 20000:
                return self.send_json({'error': '请求过大'}, 413)
            data = json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            return self.send_json({'error': '请求格式错误'}, 400)
        if self.path == '/api/login':
            password = data.get('password', '')
            if not isinstance(password, str):
                return self.send_json({'error': '账号或密码错误'}, 401)
            digest = hashlib.scrypt(password.encode(), salt=PASSWORD_SALT, n=16384, r=8, p=1)
            if data.get('username') != '123456' or not hmac.compare_digest(digest, PASSWORD_HASH):
                return self.send_json({'error': '账号或密码错误'}, 401)
            sid = secrets.token_urlsafe(32)
            with SESSION_LOCK:
                SESSIONS[sid] = time.time() + 86400
            return self.send_json({'ok': True}, cookie=f'session={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400')
        user = self.user()
        if not user:
            return self.send_json({'error': '请先登录'}, 401)
        if self.path == '/api/focus-event':
            event_id, kind, occurred = data.get('id'), data.get('kind'), data.get('occurred')
            if (not isinstance(event_id, str) or not 1 <= len(event_id) <= 80
                or kind not in ['hidden', 'blur', 'pagehide']
                or type(occurred) not in [int, float]):
                return self.send_json({'error': '离开记录格式错误'}, 400)
            with connect() as c:
                result = record_focus(c, user, event_id, kind, occurred)
            return self.send_json(result)
        if self.path == '/api/logout':
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            with SESSION_LOCK:
                SESSIONS.pop(cookie['session'].value, None)
            return self.send_json({'ok': True}, cookie='session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
        if self.path == '/api/start':
            with connect() as c:
                c.execute('INSERT OR IGNORE INTO exam VALUES (?, ?)', (user, time.time()))
            return self.send_json({'ok': True})
        if self.path not in ['/api/submit', '/api/retry']:
            return self.send_json({'error': '接口不存在'}, 404)
        qid = data.get('question')
        if type(qid) is not int or not 1 <= qid <= len(QUESTIONS):
            return self.send_json({'error': '题目不存在'}, 400)
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            existing = c.execute('SELECT id FROM submissions WHERE user=? AND question=?', (user, qid)).fetchone()
            if self.path == '/api/retry':
                c.execute("UPDATE submissions SET status='queued', reason=NULL WHERE user=? AND question=? AND status='error'", (user, qid))
            elif not existing:
                clock = timing(c, user)
                if clock['endsAt'] is None or time.time() >= clock['endsAt']:
                    return self.send_json({'error': '考试未开始或已经结束'}, 403)
                prompt = data.get('prompt', '')
                if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
                    return self.send_json({'error': '请输入 1～2000 字的提示词'}, 400)
                c.execute("INSERT INTO submissions(user, question, prompt, created, status, model) VALUES (?, ?, ?, ?, 'queued', ?)", (user, qid, prompt, time.time(), MODEL))
        return self.send_json({'ok': True})

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    init_db()
    for _ in range(3):
        threading.Thread(target=worker, daemon=True).start()
    httpd = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'本地测试网站：http://127.0.0.1:{args.port}', flush=True)
    httpd.serve_forever()
