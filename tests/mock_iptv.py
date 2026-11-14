"""سيرفر IPTV وهمي للاختبارات: Xtream + HLS + Stalker + M3U (لا يحتاج إنترنت)."""
import json
import base64
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

USER, PASS, MAC = 'u1', 'p1', '00:1A:79:AA:BB:CC'


def make_ts(path, secs=3):
    """يولّد ملف MPEG-TS حقيقياً بـ ffmpeg إن وُجد، وإلا يولّد حزم TS اصطناعية صالحة للمزامنة."""
    ff = shutil.which('ffmpeg')
    if ff:
        subprocess.run([ff, '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc=size=320x240:rate=15:duration={secs}',
                        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={secs}', '-c:v', 'libx264', '-preset', 'ultrafast',
                        '-c:a', 'aac', '-f', 'mpegts', path], check=True, timeout=60)
        return
    pkt = bytes([0x47, 0x01, 0x00, 0x10]) + b'\xff' * 184
    with open(path, 'wb') as f:
        f.write(pkt * 6000)


class Mock(ThreadingHTTPServer):
    daemon_threads = True


def start(tsfile=None):
    tmp = tempfile.mkdtemp()
    ts = tsfile or os.path.join(tmp, 'a.ts')
    if not os.path.exists(ts):
        make_ts(ts)
    with open(ts, 'rb') as _f:
        tsdata = _f.read()
    state = {'hits': [], 'stalker_tokens': 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _j(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _raw(self, data, ctype, code=200):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            sp = urllib.parse.urlsplit(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(sp.query).items()}
            p = sp.path
            state['hits'].append(self.path)
            base = f'http://127.0.0.1:{self.server.server_address[1]}'
            if p == '/player_api.php':
                if q.get('username') != USER or q.get('password') != PASS:
                    return self._j({'user_info': {'auth': 0}})
                a = q.get('action')
                if not a:
                    return self._j({'user_info': {'auth': 1, 'status': 'Active', 'exp_date': '4102444800', 'active_cons': '0',
                                                  'max_connections': '2', 'is_trial': '0', 'created_at': '1600000000',
                                                  'allowed_output_formats': ['m3u8', 'ts']},
                                    'server_info': {'url': '127.0.0.1', 'port': '80', 'timezone': 'UTC'}})
                if a == 'get_live_categories':
                    return self._j([{'category_id': '1', 'category_name': 'News'}, {'category_id': '2', 'category_name': 'Sport'}])
                if a == 'get_live_streams':
                    allc = [{'stream_id': 101, 'name': 'News One', 'category_id': '1', 'container_extension': 'ts',
                             'stream_icon': base + '/i.png', 'epg_channel_id': 'n1'},
                            {'stream_id': 102, 'name': 'Sport One', 'category_id': '2', 'container_extension': 'ts'}]
                    if q.get('category_id'):
                        allc = [c for c in allc if c['category_id'] == q['category_id']]
                    return self._j(allc)
                if a == 'get_vod_categories':
                    return self._j([{'category_id': '5', 'category_name': 'Movies'}])
                if a == 'get_vod_streams':
                    return self._j([{'stream_id': 201, 'name': 'Movie A', 'category_id': '5', 'container_extension': 'mp4', 'rating': '7.5'}])
                if a == 'get_series_categories':
                    return self._j([{'category_id': '7', 'category_name': 'Shows'}])
                if a == 'get_series':
                    return self._j([{'series_id': 301, 'name': 'Show A', 'category_id': '7', 'cover': base + '/c.png'}])
                if a == 'get_series_info':
                    return self._j({'info': {'cover': base + '/c.png', 'plot': 'plot text', 'rating': '8'},
                                    'episodes': {'1': [{'id': '9001', 'title': 'Ep1', 'episode_num': 1, 'container_extension': 'mp4',
                                                        'info': {'plot': 'ep plot', 'duration': '40:00'}}],
                                                 '2': [{'id': '9002', 'title': 'Ep2', 'episode_num': 1, 'container_extension': 'mkv'}]}})
                if a == 'get_vod_info':
                    return self._j({'info': {'movie_image': base + '/m.png', 'plot': 'movie plot', 'duration': '01:30:00', 'rating': '7.5'}})
                if a == 'get_short_epg':
                    t = lambda s: base64.b64encode(s.encode()).decode()
                    return self._j({'epg_listings': [{'title': t('Evening News'), 'description': t('desc'),
                                                      'start_timestamp': '1000', 'stop_timestamp': '2000'}]})
                return self._j([])
            if p.startswith('/live/'):
                parts = p.split('/')
                if len(parts) >= 5 and (parts[2], parts[3]) != (USER, PASS):
                    return self._raw(b'denied', 'text/plain', 403)
                name = parts[-1]
                if name.endswith('.m3u8'):
                    return self._raw(f'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:3\n#EXT-X-MEDIA-SEQUENCE:0\n'
                                     f'#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXTINF:3.0,\nseg0.ts\n#EXTINF:3.0,\n{base}/live/seg1.ts\n#EXT-X-ENDLIST\n'.encode(),
                                     'application/vnd.apple.mpegurl')
                if name.endswith('.ts'):
                    return self._raw(tsdata, 'video/mp2t')
                if name == 'key.bin':
                    return self._raw(b'k' * 16, 'application/octet-stream')
                return self._raw(b'x', 'text/plain', 404)
            if p.startswith('/movie/') or p.startswith('/series/'):
                return self._raw(tsdata, 'video/mp2t')
            if p == '/html':
                return self._raw(b'<html>blocked</html>', 'text/html')
            if p == '/list.m3u':
                return self._raw((f'#EXTM3U\n#EXTINF:-1 tvg-logo="{base}/i.png" group-title="News",Chan A\n{base}/live/{USER}/{PASS}/101.ts\n'
                                  f'#EXTINF:-1 group-title="Sport",Chan B\n{base}/live/{USER}/{PASS}/102.m3u8\n').encode(), 'audio/x-mpegurl')
            if p == '/portal.php':
                typ, act = q.get('type'), q.get('action')
                if (act == 'handshake'):
                    if 'mac=' + MAC not in self.headers.get('Cookie', ''):
                        return self._j({'js': {}})
                    state['stalker_tokens'] += 1
                    return self._j({'js': {'token': 'TKN123'}})
                if self.headers.get('Authorization') != 'Bearer TKN123':
                    return self._j({'js': []})
                if (typ, act) == ('stb', 'get_profile'):
                    return self._j({'js': {'id': 1}})
                if (typ, act) == ('account_info', 'get_main_info'):
                    return self._j({'js': {'end_date': '2099-01-01 00:00:00'}})
                if (typ, act) == ('itv', 'get_genres'):
                    return self._j({'js': [{'id': '10', 'title': 'Arabic'}]})
                if (typ, act) == ('itv', 'get_all_channels'):
                    return self._j({'js': {'data': [{'id': '1', 'name': 'ST One', 'cmd': 'ffrt http://localhost/ch/1', 'tv_genre_id': '10'}]}})
                if (typ, act) == ('itv', 'create_link'):
                    return self._j({'js': {'cmd': f'ffmpeg {base}/live/{USER}/{PASS}/101.ts'}})
            return self._raw(b'nf', 'text/plain', 404)

    srv = Mock(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state, f'http://127.0.0.1:{srv.server_address[1]}'
