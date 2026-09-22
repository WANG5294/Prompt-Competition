import concurrent.futures
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import server


class PenaltyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = Path(self.tmp.name) / 'penalties.sqlite3'
        server.init_db()
        self.start = time.time() - 1
        with server.connect() as c:
            c.execute('INSERT INTO exam VALUES (?,?)', ('123456', self.start))

    def tearDown(self):
        server.DB = self.old_db
        self.tmp.cleanup()

    def event(self, event_id, occurred=None):
        with server.connect() as c:
            return server.record_focus(c, '123456', event_id, 'hidden', occurred or time.time())

    def test_incremental_cumulative_and_duplicate(self):
        for i, total in [(1,30),(2,90),(3,180)]:
            result = self.event(str(i))
            self.assertEqual(result['eventPenalty'], i*30)
            self.assertEqual(result['penaltySeconds'], total)
            self.assertAlmostEqual(result['endsAt'], self.start+3600-total)
        again = self.event('1')
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['penaltySeconds'],180)
        server.init_db()
        with server.connect() as c:
            self.assertEqual(server.timing(c,'123456')['penaltySeconds'],180)

    def test_concurrent_reports_are_atomic(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(self.event, ['same']*8))
        with server.connect() as c:
            self.assertEqual(server.timing(c,'123456')['penaltySeconds'],30)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(self.event, [str(i) for i in range(8)]))
        self.assertEqual(sorted(x['eventPenalty'] for x in results),list(range(60,271,30)))
        with server.connect() as c:
            self.assertEqual(server.timing(c,'123456')['penaltySeconds'],1350)

    def test_expiry_and_offline_replay(self):
        with server.connect() as c:
            c.execute('UPDATE exam SET started=?', (time.time()-3590,))
        result=self.event('last-seconds')
        self.assertLess(result['endsAt'],time.time())
        self.assertFalse(self.event('after-end')['applied'])
        self.assertEqual(self.event('last-seconds')['penaltySeconds'],30)
        # A delayed event that occurred before the adjusted deadline is accepted.
        result=self.event('offline',time.time()-25)
        self.assertTrue(result['applied'])
        self.assertEqual(result['penaltySeconds'],90)

    def test_legacy_warning_migration(self):
        with server.connect() as c:
            c.execute('DROP TABLE focus_events')
            c.execute('CREATE TABLE focus_events(user TEXT,event_id TEXT,kind TEXT,occurred REAL,received REAL,PRIMARY KEY(user,event_id))')
            c.execute('INSERT INTO focus_events VALUES (?,?,?,?,?)',('123456','old','blur',self.start,self.start))
        server.init_db()
        result=self.event('new')
        self.assertEqual(result['eventPenalty'],30)
        self.assertEqual(result['focusCount'],1)
        with server.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM focus_events').fetchone()[0],2)

    def test_question_bank(self):
        self.assertEqual(len(server.QUESTIONS),20)
        self.assertEqual([q['id'] for q in server.QUESTIONS],list(range(1,21)))
        self.assertEqual(server.QUESTIONS[0]['target'],'苹果')
        self.assertEqual(server.QUESTIONS[-1]['target'],'黄仁勋的皮夹克')
        for q in server.QUESTIONS:
            self.assertTrue(q['blocked'] and q['hint'] and q['category'])
            self.assertTrue(server.blocked_word(q['target'],q))


if __name__ == '__main__':
    unittest.main()
