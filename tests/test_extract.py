"""اختبارات دالة extract() — تُشغَّل في GitHub Actions قبل البناء."""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('app', os.path.join(_HERE, '..', 'iptv_checker.py'))
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)


def ex(text):
    r = app.extract(text)
    return r if isinstance(r, list) else r.get('accounts', [])


class TestExtract(unittest.TestCase):
    def xt(self, a, host, user, pw):
        self.assertIn(a.get('type', 'xtream'), ('xtream', None, ''))
        self.assertEqual((a['host'], a['username'], a['password']), (host, user, pw))

    def test_single_line(self):
        r = ex('http://a.com:8080 user1 pass1')
        self.assertEqual(len(r), 1); self.xt(r[0], 'http://a.com:8080', 'user1', 'pass1')

    def test_multi_line_english_labels(self):
        r = ex('Host: http://a.com:8080\nUsername: user1\nPassword: pass1')
        self.assertEqual(len(r), 1); self.xt(r[0], 'http://a.com:8080', 'user1', 'pass1')

    def test_arabic_labels(self):
        r = ex('السيرفر: http://a.com:8080\nالمستخدم: user1\nكلمة المرور: pass1')
        self.assertEqual(len(r), 1); self.xt(r[0], 'http://a.com:8080', 'user1', 'pass1')

    def test_get_php_link(self):
        r = ex('http://a.com:8080/get.php?username=user1&password=pass1&type=m3u_plus&output=ts')
        self.assertEqual(len(r), 1); self.xt(r[0], 'http://a.com:8080', 'user1', 'pass1')

    def test_mac_username_with_password_is_xtream(self):
        r = ex('http://foxbleu-cdn.com:80 Username = e8:aa:cb:c4:bf:92 Password = aMzpeeG')
        self.assertEqual(len(r), 1); self.xt(r[0], 'http://foxbleu-cdn.com:80', 'e8:aa:cb:c4:bf:92', 'aMzpeeG')

    def test_numeric_credentials(self):
        r = ex('http://u.goldeniptv.com:2052 Username = 3690224495676292 Password = 41bbb9029297')
        self.assertEqual(len(r), 1); self.xt(r[0], 'http://u.goldeniptv.com:2052', '3690224495676292', '41bbb9029297')

    def test_mac_without_password_is_stalker(self):
        r = ex('Portal: http://p.com/c/\nMAC: 00:1A:79:AA:BB:CC')
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]['type'], 'stalker')
        self.assertEqual(r[0]['mac'], '00:1A:79:AA:BB:CC')

    def test_m3u_url(self):
        r = ex('http://x.com/playlist.m3u')
        self.assertEqual(len(r), 1); self.assertEqual(r[0]['type'], 'm3u')

    def test_mixed_list(self):
        r = ex('http://a.com:8080 u1 p1\n\nhttp://b.com/get.php?username=u2&password=p2\nPortal http://p.com/c MAC 00:1A:79:AA:BB:CC')
        self.assertEqual(len(r), 3)
        self.xt(r[0], 'http://a.com:8080', 'u1', 'p1')
        self.xt(r[1], 'http://b.com', 'u2', 'p2')
        self.assertEqual(r[2]['type'], 'stalker')

    def test_hyphenated_host_single_line(self):
        r = ex('http://my-server.com:8080 userx passx\nhttp://srv.example-long-domain.com:8080 userA passB')
        self.assertEqual(len(r), 2)
        self.xt(r[0], 'http://my-server.com:8080', 'userx', 'passx')
        self.xt(r[1], 'http://srv.example-long-domain.com:8080', 'userA', 'passB')

    def test_empty_and_noise(self):
        self.assertEqual(ex(''), [])
        self.assertEqual(ex('مرحبا، لا توجد بيانات هنا'), [])


if __name__ == '__main__':
    unittest.main()
