import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ['MOIPTV_DATA'] = tempfile.mkdtemp()
_spec = importlib.util.spec_from_file_location('ic', os.path.join(HERE, '..', 'iptv_checker.py'))
ic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ic)
import mock_iptv as mk


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock, cls.mstate, cls.murl = mk.start()
        cls.app = ic.ThreadingHTTPServer(('127.0.0.1', 0), ic.Handler)
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.aurl = f'http://127.0.0.1:{cls.app.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        cls.mock.shutdown()

    def post(self, path, body, timeout=60):
        req = urllib.request.Request(self.aurl + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'X-IPTV-Checker': '1'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def get(self, path, timeout=30):
        with urllib.request.urlopen(self.aurl + path, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)

    def reg(self, **kw):
        d = dict(type='xtream', host=self.murl, username=mk.USER, password=mk.PASS)
        d.update(kw)
        return self.post('/api/reg', d)['s']

    def stream_path(self, sid, **kw):
        q = dict(t=ic.TOKEN, s=sid)
        q.update(kw)
        return '/stream?' + urllib.parse.urlencode(q)


class TestCheck(Base):
    def test_xtream_ok_and_history(self):
        r = self.post('/api/check', dict(host=self.murl, username=mk.USER, password=mk.PASS, timeout=8))
        self.assertEqual(r['state'], 'active')
        self.assertEqual(r['max_cons'], 2)
        self.assertEqual(r['formats'], ['m3u8', 'ts'])
        r2 = self.post('/api/check', dict(host=self.murl, username=mk.USER, password=mk.PASS, timeout=8))
        self.assertEqual(r2['prev']['s'], 'active')
        self.assertGreaterEqual(len(r2['hist']), 2)

    def test_wrong_password(self):
        r = self.post('/api/check', dict(host=self.murl, username=mk.USER, password='bad', timeout=8))
        self.assertEqual(r['state'], 'invalid')

    def test_offline_message(self):
        r = self.post('/api/check', dict(host='http://127.0.0.1:1', username='a', password='b', timeout=3))
        self.assertEqual(r['state'], 'offline')
        self.assertTrue(r['message'])

    def test_stalker(self):
        r = self.post('/api/check', dict(type='stalker', portal=self.murl + '/portal.php', mac=mk.MAC, timeout=8))
        self.assertEqual(r['state'], 'active')

    def test_m3u(self):
        r = self.post('/api/check', dict(type='m3u', url=self.murl + '/list.m3u', timeout=8))
        self.assertEqual(r['state'], 'active_m3u')
        self.assertIn('#EXTINF', r['rawM3u'])

    def test_domain_limit_setting(self):
        ic.set_settings({'per_domain': 1})
        try:
            ts = []
            res = []
            def go():
                res.append(self.post('/api/check', dict(host=self.murl, username=mk.USER, password=mk.PASS, timeout=8))['state'])
            for _ in range(4):
                t = threading.Thread(target=go); t.start(); ts.append(t)
            for t in ts: t.join()
            self.assertEqual(res, ['active'] * 4)
        finally:
            ic.set_settings({'per_domain': 2})


class TestDeep(Base):
    def test_deep_excellent_or_good(self):
        r = self.post('/api/deep', dict(host=self.murl, username=mk.USER, password=mk.PASS, timeout=8))
        self.assertGreaterEqual(r['ok'], 1, r)
        self.assertIn(r['quality'], ('excellent', 'good', 'weak'))
        self.assertTrue(r['samples'][0]['ok'])

    def test_deep_none_on_html(self):
        # يصنّف الرد HTML كغير بث
        r = ic._probe_sample(self.murl + '/html', ic._OPENER, 5)
        self.assertFalse(r['ok'])

    def test_looks_ts(self):
        self.assertTrue(ic._looks_ts((b'\x47' + b'\0' * 187) * 6))
        self.assertFalse(ic._looks_ts(b'hello world' * 100))


class TestDetailsAndInfo(Base):
    def test_details(self):
        r = self.post('/api/details', dict(host=self.murl, username=mk.USER, password=mk.PASS))
        self.assertEqual(r['counts'], {'live': 2, 'vod': 1, 'series': 1})
        self.assertEqual(r['formats'], ['m3u8', 'ts'])

    def test_epg_vod_series(self):
        e = self.post('/api/epg', dict(host=self.murl, username=mk.USER, password=mk.PASS, stream_id=101))
        self.assertEqual(e['items'][0]['title'], 'Evening News')
        v = self.post('/api/vodinfo', dict(host=self.murl, username=mk.USER, password=mk.PASS, vod_id=201))
        self.assertEqual(v['info']['plot'], 'movie plot')
        s = self.post('/api/series', dict(host=self.murl, username=mk.USER, password=mk.PASS, series_id=301))
        self.assertEqual([x['season'] for x in s['episodes']], ['1', '2'])
        self.assertEqual(s['info']['plot'], 'plot text')

    def test_content_has_icons(self):
        c = self.post('/api/content', dict(host=self.murl, username=mk.USER, password=mk.PASS))
        self.assertTrue(c['live']['items'][0]['icon'])
        self.assertEqual(c['vod']['cats'], {'5': 'Movies'})


class TestStreaming(Base):
    def test_token_required(self):
        sid = self.reg()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get('/stream?' + urllib.parse.urlencode(dict(s=sid, id=101, ext='ts', kind='live')))
        self.assertEqual(cm.exception.code, 403)

    def test_ts_stream_through_proxy(self):
        sid = self.reg()
        st, body, _ = self.get(self.stream_path(sid, id=101, ext='ts', kind='live'))
        self.assertEqual(st, 200)
        self.assertTrue(ic._looks_ts(body[:4000]))

    def test_creds_not_in_url(self):
        sid = self.reg()
        self.assertNotIn(mk.PASS, self.stream_path(sid, id=101, ext='ts', kind='live'))

    def test_hls_rewrite(self):
        sid = self.reg()
        st, body, hd = self.get(self.stream_path(sid, id=101, ext='m3u8', kind='live'))
        txt = body.decode()
        self.assertIn('mpegurl', hd.get('Content-Type', '').lower())
        self.assertNotIn(self.murl, txt)               # لا روابط مباشرة للسيرفر
        self.assertEqual(txt.count('/relay?t='), 3)    # مقطعان + مفتاح التشفير
        # اتبع أول مقطع عبر الوسيط
        seg = [l for l in txt.splitlines() if l.startswith('/relay?')][1]
        st2, b2, _ = self.get(seg)
        self.assertTrue(ic._looks_ts(b2[:4000]))

    def test_probe_ok_and_error(self):
        sid = self.reg()
        _, b, _ = self.get(self.stream_path(sid, id=101, ext='ts', kind='live', probe=1))
        j = json.loads(b)
        self.assertEqual((j['status'], j['kind']), (200, 'ts'))
        bad = self.reg(password='wrong')
        _, b, _ = self.get(self.stream_path(bad, id=101, ext='ts', kind='live', probe=1))
        self.assertEqual(json.loads(b)['status'], 403)

    def test_relay_m3u_item(self):
        sid = self.reg(type='m3u', host=self.murl + '/list.m3u', username='', password='')
        u = f'{self.murl}/live/{mk.USER}/{mk.PASS}/101.ts'
        st, body, _ = self.get('/relay?' + urllib.parse.urlencode(dict(t=ic.TOKEN, s=sid, u=u)))
        self.assertTrue(ic._looks_ts(body[:4000]))

    def test_stalker_channels_and_link(self):
        r = self.post('/api/stalker', dict(portal=self.murl + '/portal.php', mac=mk.MAC))
        self.assertEqual(r['items'][0]['name'], 'ST One')
        self.assertEqual(r['cats'], {'10': 'Arabic'})
        sid = self.reg(type='stalker', host=self.murl + '/portal.php', mac=mk.MAC, username='', password='')
        st, body, _ = self.get(self.stream_path(sid, cmd=r['items'][0]['cmd']))
        self.assertTrue(ic._looks_ts(body[:4000]))

    def test_proxy_per_server_is_used(self):
        # بروكسي غير صالح يجب أن يفشل البث (يثبت أن بروكسي السيرفر يُستخدم فعلاً)
        saved = {k: os.environ.pop(k) for k in ('no_proxy', 'NO_PROXY') if k in os.environ}
        try:
            sid = self.reg(proxy='http://127.0.0.1:9')
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(self.stream_path(sid, id=101, ext='ts', kind='live'))
            self.assertEqual(cm.exception.code, 502)
            ok = self.reg()                       # بدون بروكسي يعمل
            self.assertEqual(self.get(self.stream_path(ok, id=101, ext='ts', kind='live'))[0], 200)
        finally:
            os.environ.update(saved)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg غير متوفر')
    def test_transcode_and_codec(self):
        sid = self.reg()
        src = self.stream_path(sid, id=101, ext='ts', kind='live')
        c = self.post('/api/codec', dict(src=src), timeout=60)
        self.assertTrue(c['ok'], c)
        self.assertEqual(c['video'], 'h264')
        q = urllib.parse.urlencode(dict(t=ic.TOKEN, s=sid, src=src, mode='full', fmt='ts'))
        st, body, hd = self.get('/transcode?' + q, timeout=90)
        self.assertEqual(hd.get('Content-Type'), 'video/mp2t')
        self.assertTrue(ic._looks_ts(body[:8000]))


class TestSessionSettingsLog(Base):
    def test_session_roundtrip(self):
        self.assertTrue(self.post('/api/session', dict(op='save', data={'rows': [{'id': 1, 'host': 'h'}]}))['ok'])
        self.assertEqual(self.post('/api/session', dict(op='load'))['data']['rows'][0]['host'], 'h')

    def test_session_beacon_with_token(self):
        req = urllib.request.Request(self.aurl + '/api/session?t=' + ic.TOKEN,
                                     data=json.dumps({'op': 'save', 'data': {'x': 1}}).encode(),
                                     headers={'Content-Type': 'text/plain'})
        with urllib.request.urlopen(req) as r:
            self.assertTrue(json.loads(r.read())['ok'])
        req = urllib.request.Request(self.aurl + '/api/session', data=b'{}', headers={'Content-Type': 'text/plain'})
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(req)

    def test_settings_and_caps(self):
        r = self.post('/api/settings', dict(op='set', data={'ext_player': 'mpv', 'bogus': 1}))
        self.assertEqual(r['settings']['ext_player'], 'mpv')
        self.assertNotIn('bogus', r['settings'])
        self.post('/api/settings', dict(op='set', data={'ext_player': 'auto'}))
        self.assertIn('ffmpeg', r['caps'])

    def test_log_redacts_passwords(self):
        self.post('/api/check', dict(host=self.murl, username=mk.USER, password=mk.PASS, timeout=8))
        items = self.post('/api/log', dict(op='get'))['items']
        self.assertTrue(items)
        self.assertNotIn(mk.PASS, json.dumps(items))

    def test_data_dir_env(self):
        self.assertEqual(ic._DATA_DIR, os.environ['MOIPTV_DATA'])


if __name__ == '__main__':
    unittest.main()


class TestChannelSearch(Base):
    def test_norm(self):
        self.assertEqual(ic._norm('beIN-Sports 1 HD'), 'beinsports1hd')
        self.assertIn(ic._norm('بي ان'), ic._norm('بي إن سبورت'))

    def test_search_all_types(self):
        r = self.post('/api/chsearch', dict(host=self.murl, username='u1', password='p1', terms='sport', verify=True))
        self.assertEqual(r['count'], 1)
        self.assertTrue(r['verified']['ok'])
        r = self.post('/api/chsearch', dict(type='m3u', url=self.murl + '/list.m3u', terms=['news']))
        self.assertEqual(r['count'], 1)
        r = self.post('/api/chsearch', dict(type='stalker', portal=self.murl + '/portal.php', mac=mk.MAC, terms=['one'], verify=True))
        self.assertEqual(r['count'], 1)
        self.assertTrue(r['verified']['ok'])
        r = self.post('/api/chsearch', dict(host=self.murl, username='u1', password='p1', terms=['zzzz']))
        self.assertEqual(r['count'], 0)


class TestUpdate(Base):
    def test_target_and_hash(self):
        mode, name = ic._update_target({'iptv_checker.py': 'x', 'SHA256SUMS.txt': 'y', 'a_Setup.exe': 'z'})
        self.assertEqual((mode, name), ('source', 'iptv_checker.py'))
        p = os.path.join(tempfile.mkdtemp(), 'f')
        with open(p, 'wb') as f:
            f.write(b'abc')
        self.assertEqual(ic._sha256(p), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')


class TestV66(Base):
    def test_search_vod_series(self):
        r = self.post('/api/chsearch', dict(host=self.murl, username='u1', password='p1', terms='movie, show',
                                            kinds=['live', 'vod', 'series'], verify=True))
        self.assertEqual(r['by'].get('vod'), 1)
        self.assertEqual(r['by'].get('series'), 1)
        kinds = sorted(m['kind'] for m in r['matches'])
        self.assertEqual(kinds, ['series', 'vod'])
        self.assertTrue(r['verified']['ok'])

    def test_list_cache(self):
        body = dict(host=self.murl, username='u1', password='p1')
        a = self.post('/api/content', body)
        self.assertNotIn('cached_at', a)
        b = self.post('/api/content', body)
        self.assertIn('cached_at', b)
        c = self.post('/api/content', dict(body, force=True))
        self.assertNotIn('cached_at', c)

    def test_ua_detect(self):
        r = self.post('/api/ua_detect', dict(host=self.murl, username='u1', password='p1', stream=True), timeout=120)
        self.assertTrue(r['default_ok'])
        self.assertTrue(all(x['api'] for x in r['results']))
        self.assertTrue(r['results'][0]['stream'])

    def test_bundled_ffmpeg_lookup(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, 'bin')
        os.makedirs(p)
        fake = os.path.join(p, 'ffmpeg' + ('.exe' if sys.platform.startswith('win') else ''))
        with open(fake, 'w') as f:
            f.write('x')
        old = ic._DATA_DIR
        try:
            ic._DATA_DIR = d
            self.assertEqual(ic._find_tool('ffmpeg'), fake)
        finally:
            ic._DATA_DIR = old


class TestVodResolve(Base):
    def test_resolve_real_extension(self):
        import http.server
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(s):
                hits.append(s.path)
                if s.path.endswith('/movie/u/p/7.mkv'):
                    s.send_response(206); s.send_header('Content-Type', 'video/x-matroska'); s.end_headers()
                    s.wfile.write(b'\x1aE\xdf\xa3' + b'\0' * 5000)
                elif '/movie/' in s.path:
                    s.send_response(401); s.end_headers()
                else:
                    b = json.dumps({'info': {}, 'movie_data': {'container_extension': 'mkv'}}).encode()
                    s.send_response(200); s.send_header('Content-Type', 'application/json'); s.end_headers(); s.wfile.write(b)

            def log_message(s, *a):
                pass
        srv = ic.ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            h = f'http://127.0.0.1:{srv.server_address[1]}'
            r = self.post('/api/vod_resolve', dict(host=h, username='u', password='p', vod_id=7, ext='mp4'))
            self.assertEqual(r['ext'], 'mkv')
            self.assertEqual(r['tried'][0]['ext'], 'mkv')        # الامتداد من get_vod_info يُجرَّب أولاً
        finally:
            srv.shutdown()
