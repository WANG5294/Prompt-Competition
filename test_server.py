import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import server


class ScoringTests(unittest.TestCase):
    def test_exact_and_whitelist(self):
        for output, expected in [('苹果', 5), (' 苹果\n', 5), ('谜底是苹果', 4), ('苹果。', 4), ('“苹果”', 4), ('香蕉', 0)]:
            self.assertEqual(server.grade(output, '苹果')[0], expected)

    def test_length_boundary_and_repeat(self):
        self.assertEqual(server.grade('苹果' + '字' * 30, '苹果')[0], 2)
        self.assertEqual(server.grade('苹果' + '字' * 31, '苹果')[0], 1)
        self.assertEqual(server.grade('苹果苹果', '苹果')[0], 2)

    def test_blocked_normalization(self):
        for prompt in ['苹 果', 'ＡＰＰＬＥ', 'aPpLe', '苹\u200b果']:
            self.assertTrue(server.blocked_word(prompt, server.QUESTIONS[0]))
        self.assertIsNone(server.blocked_word('请说出一种红色水果', server.QUESTIONS[0]))


class IntegrationTests(unittest.TestCase):
    def test_full_lifecycle(self):
        temporary = tempfile.TemporaryDirectory()
        server.DB = Path(temporary.name) / 'test.sqlite3'
        server.init_db()
        calls = []
        def fake_model(prompt):
            calls.append(prompt)
            if prompt == '模拟异常':
                raise RuntimeError('模型暂时不可用')
            return '苹果'
        self.addCleanup(setattr, server, 'call_model', server.call_model)
        server.call_model = fake_model
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        stop = threading.Event()
        worker_thread = threading.Thread(target=server.worker, args=(stop,), daemon=True)
        worker_thread.start()
        self.addCleanup(lambda: (stop.set(), worker_thread.join(timeout=2)))
        base = f'http://127.0.0.1:{httpd.server_port}'
        cookie = ''
        def request(path, data=None, use_cookie=True):
            headers = {'X-Contest-Request': '1', 'Content-Type': 'application/json'}
            if use_cookie:
                headers['Cookie'] = cookie
            req = urllib.request.Request(base + path, data=None if data is None else json.dumps(data).encode(), headers=headers)
            try:
                response = urllib.request.urlopen(req)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response), response.headers
        def wait_done(qid, status):
            for _ in range(60):
                state = request('/api/state')[1]
                row = next((s for s in state['submissions'] if s['question'] == qid), None)
                if row and row['status'] == status:
                    return row
                time.sleep(.05)
            self.fail(f'Timed out waiting for question {qid}: {status}')
        self.assertEqual(request('/api/state', use_cookie=False)[0], 401)
        self.assertEqual(request('/.env')[0], 404)
        self.assertEqual(request('/api/login', {'username': '123456', 'password': 'bad'})[0], 401)
        status, _, headers = request('/api/login', {'username': '123456', 'password': 'mock'})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertEqual(request('/api/submit', {'question':1, 'prompt':'红色水果'})[0], 403)
        event = {'id':'focus-test-1', 'kind':'hidden', 'occurred':time.time()}
        self.assertFalse(request('/api/focus-event', event)[1]['applied'])
        self.assertEqual(request('/api/focus-event', event, use_cookie=False)[0], 401)
        request('/api/start', {})
        event['occurred'] = time.time()
        self.assertEqual(request('/api/focus-event', event)[1]['focusCount'], 1)
        self.assertEqual(request('/api/focus-event', event)[1]['focusCount'], 1)
        self.assertEqual(request('/api/state')[1]['focusCount'], 1)
        self.assertEqual(request('/api/focus-event', dict(event, kind='invalid'))[0], 400)
        self.assertFalse(request('/api/focus-event', dict(event, id='nan', occurred=float('nan')))[1]['applied'])
        ends_at = request('/api/state')[1]['endsAt']
        request('/api/start', {})
        self.assertEqual(request('/api/state')[1]['endsAt'], ends_at)
        self.assertEqual(len(request('/api/state')[1]['questions']), 20)
        self.assertEqual(request('/api/submit', {'question':21, 'prompt':'无效题号'})[0], 400)
        self.assertEqual(request('/api/submit', {'question':20, 'prompt':'黄仁勋的皮夹克'})[0], 200)
        self.assertEqual(wait_done(20, 'done')['score'], 0)
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: request('/api/submit', {'question':1, 'prompt':'红色水果'}), range(12)))
        self.assertTrue(all(r[0] == 200 for r in results))
        self.assertEqual(wait_done(1, 'done')['score'], 5)
        self.assertEqual(calls.count('红色水果'), 1)
        request('/api/submit', {'question':1, 'prompt':'试图覆盖'})
        self.assertEqual(request('/api/state')[1]['submissions'][0]['prompt'], '红色水果')
        request('/api/submit', {'question':2, 'prompt':'长 城'})
        self.assertEqual(wait_done(2, 'done')['score'], 0)
        self.assertEqual(len(calls), 1)
        request('/api/submit', {'question':3, 'prompt':'模拟异常'})
        failed = wait_done(3, 'error')
        self.assertIsNone(failed['score'])
        with server.connect() as c:
            c.execute('UPDATE exam SET started=?', (time.time()-3580,))
        self.assertEqual(request('/api/submit', {'question':4, 'prompt':'未到原截止时间，但罚时已耗尽'})[0], 403)
        with server.connect() as c:
            c.execute('UPDATE exam SET started=?', (time.time()-3601,))
        self.assertFalse(request('/api/focus-event', dict(event, id='expired', occurred=time.time()))[1]['applied'])
        self.assertTrue(request('/api/focus-event', dict(event, id='delayed', occurred=time.time()-100))[1]['applied'])
        server.call_model = lambda prompt: '人工智能'
        request('/api/retry', {'question':3, 'prompt':'不能替换'})
        final = wait_done(3, 'done')
        self.assertEqual(final['score'], 5)
        self.assertEqual(final['prompt'], '模拟异常')
        with server.connect() as c:
            c.execute('DELETE FROM submissions WHERE question=2')
        self.assertEqual(request('/api/submit', {'question':2, 'prompt':'新答案'})[0], 403)
        server.init_db()
        self.assertEqual(len(request('/api/state')[1]['submissions']), 3)
        request('/api/logout', {})
        self.assertEqual(request('/api/state')[0], 401)
        httpd.shutdown()
        httpd.server_close()
        # Keep temporary DB alive until daemon worker exits with the test process.
        self.__class__.temporary = temporary


if __name__ == '__main__':
    unittest.main()
