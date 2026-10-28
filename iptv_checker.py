#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV Checker - فاحص سيرفرات IPTV
---------------------------------
التشغيل:            python iptv_checker.py        (تفتح الواجهة في المتصفح تلقائياً)
وضع سطر الأوامر:    python iptv_checker.py servers.txt
المتطلبات: Python 3.8 أو أحدث فقط - لا يحتاج أي مكتبات إضافية.
"""
import os
import json
import re
import ssl
import sys
import time
import socket
import threading
import webbrowser
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

VERSION = "5.1"
UA = "VLC/3.0.20 LibVLC/3.0.20"
DEFAULT_TIMEOUT = 15

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE
_PROXY = {'url': ''}
try:
    if getattr(sys, 'frozen', False):                 # برنامج مستقل (PyInstaller): احفظ بجانب الملف التنفيذي
        _BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    else:
        _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _CFG_PATH = os.path.join(_BASE_DIR, 'iptv_checker_config.json')
except Exception:
    _CFG_PATH = 'iptv_checker_config.json'


def _build_opener(proxy_url=''):
    handlers = [urllib.request.HTTPSHandler(context=_SSL)]
    proxy_url = (proxy_url or '').strip()
    if proxy_url:
        scheme = proxy_url.lower().split('://', 1)[0]
        if scheme in ('socks4', 'socks5', 'socks5h', 'socks4a'):
            raise RuntimeError('بروكسي SOCKS غير مدعوم — استخدم بروكسي HTTP/HTTPS بصيغة http://host:port')
        if '://' not in proxy_url:
            proxy_url = 'http://' + proxy_url
        handlers.append(urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))   # تجاهل بروكسي النظام
    return urllib.request.build_opener(*handlers)


def apply_proxy(url):
    global _OPENER
    op = _build_opener(url)        # قد يرفع استثناء لصيغة غير مدعومة قبل التبديل
    _OPENER = op
    _PROXY['url'] = (url or '').strip()


def test_proxy():
    for u in ('https://api.ipify.org/?format=json', 'http://api.ipify.org/?format=json'):
        try:
            with _OPENER.open(urllib.request.Request(u, headers={'User-Agent': UA}), timeout=12) as r:
                return json.loads(r.read(4000).decode('utf-8', 'replace')).get('ip')
        except Exception:
            continue
    return None


def load_cfg():
    try:
        with open(_CFG_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(d):
    try:
        with open(_CFG_PATH, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


_OPENER = _build_opener('')

# ----------------------------------------------------------------------------
#  استخراج البيانات من النص الملصوق
# ----------------------------------------------------------------------------
URL_RE = re.compile(r'https?://[^\s"\'<>`]+', re.I)
BARE_HOST_RE = re.compile(
    r'^(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d{2,5})?/?$|^\d{1,3}(?:\.\d{1,3}){3}(?::\d{2,5})?/?$', re.I)
SEP = r'\s*[^\w\s]+\s*'          # أي فاصل:  :  =  ➤  ->  👉  |
VAL = r'([^\s|,;]+)'
LBL_HOST = r'(?:host|server|url|portal|panel|dns|domain|السيرفر|سيرفر|الخادم|خادم|الرابط|رابط|الهوست|هوست)'
LBL_USER = r'(?:us[e3](?:r?\s*[nm]{1,2}[a@4]?(?:m|rn)[e3]|rn|r)|login|log\s*in|اسم\s*المستخدم|المستخدم|يوزر\s*نيم|اليوزر|يوزر)'
LBL_PASS = r'(?:p[a@4][s5]{2}(?:\s*w[o0][rn]?[dn])?|pwd|كلمة\s*(?:المرور|السر)|الباسورد|باسورد|الرقم\s*السري|الباس|باس)'
RE_HOST = re.compile(r'(?<!\w)' + LBL_HOST + SEP + VAL, re.I)
RE_USER = re.compile(r'(?<!\w)' + LBL_USER + SEP + VAL, re.I)
RE_PASS = re.compile(r'(?<!\w)' + LBL_PASS + SEP + VAL, re.I)
STREAM_ID = re.compile(r'^\d+(?:\.[a-z0-9]{2,4})?$', re.I)
PATH_KINDS = ('live', 'movie', 'series', 'timeshift', 'hls')


def _tok(v):
    return v.strip().strip('\'"`*').strip()


def _host_of(p):
    h = p.hostname
    if not h:
        return None
    try:
        port = p.port
    except ValueError:
        port = None
    scheme = (p.scheme or 'http').lower()
    return f"{scheme}://{h}" + (f":{port}" if port else "")


def _query(q):
    out = {}
    for kv in q.split('&'):
        k, _, v = kv.partition('=')
        if k:
            out[k.strip().lower()] = urllib.parse.unquote(v)
    return out


def parse_url(u):
    """يرجع (host, user, pass) - قد يكون user/pass فارغين إذا الرابط بدون بيانات دخول."""
    u = u.strip().rstrip('.,;)]}>\'"*')
    parts = u.split('|')
    if len(parts) >= 3 and parts[1].strip() and parts[2].strip():      # host|user|pass
        h = _host_of(urllib.parse.urlsplit(parts[0]))
        return (h, _tok(parts[1]), _tok(parts[2])) if h else (None, None, None)
    p = urllib.parse.urlsplit(parts[0])
    h = _host_of(p)
    if not h:
        return None, None, None
    q = _query(p.query)
    user = q.get('username') or q.get('user')
    pw = q.get('password') or q.get('pass') or q.get('pwd')
    if user and pw:
        return h, user, pw
    if p.username and p.password:
        return h, urllib.parse.unquote(p.username), urllib.parse.unquote(p.password)
    segs = [urllib.parse.unquote(s) for s in p.path.split('/') if s]
    if len(segs) >= 3 and segs[0].lower() in PATH_KINDS:            # /live/user/pass/123.ts
        return h, segs[1], segs[2]
    if len(segs) == 3 and STREAM_ID.match(segs[2]):                   # /user/pass/123
        return h, segs[0], segs[1]
    return h, None, None


def extract(text):
    """قراءة تكيّفية: تجمع host و user و pass بأي ترتيب، وبمجرد اكتمال الثلاثة
    تُغلق السجل وتبدأ سجلاً جديداً. تدعم: الهوست في آخر كل كتلة، أو مكتوباً مرة
    واحدة لعدة مستخدمين، أو الروابط الكاملة، مع تجاهل الرموز والتكرار.
    """
    out, seen = [], set()
    st = {'host': None, 'user': None, 'pass': None, 'fresh': False, 'last': None}
    mac_re = re.compile(r'(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}')
    pending_portal = {'u': None}
    pending_mac = {'m': None}

    def add_stalker(portal, mac):
        portal = portal.strip().rstrip('.,;')
        if not portal.lower().startswith('http'):
            portal = 'http://' + portal
        p = urllib.parse.urlsplit(portal)
        if not p.path or p.path == '/':
            portal = portal.rstrip('/') + '/portal.php'   # مسار البوابة الافتراضي
        mac = mac.strip().upper().replace('-', ':')
        k = ('stalker', portal.lower(), mac)
        if k not in seen:
            seen.add(k)
            out.append({'type': 'stalker', 'portal': portal, 'mac': mac, 'host': portal, 'username': mac, 'password': ''})

    def add_m3u(url):
        url = url.strip().rstrip('.,;')
        k = ('m3u', url.lower())
        if k not in seen:
            seen.add(k)
            out.append({'type': 'm3u', 'm3uurl': url, 'host': url, 'username': '', 'password': ''})

    def add(h, u, pw):
        if h and u and pw:
            k = (h.lower(), u, pw)
            if k not in seen:
                seen.add(k)
                out.append({'host': h, 'username': u, 'password': pw})

    def flush(h):
        add(h, st['user'], st['pass'])
        st['last'] = h
        st['user'] = None
        st['pass'] = None
        st['fresh'] = False

    def p_user(v):
        if st['user'] is not None:                 # مستخدم جديد قبل اكتمال السابق
            h = st['host'] or st['last']
            if h and st['pass']:
                flush(h)
            else:
                st['user'] = None
                st['pass'] = None
        st['user'] = v

    def p_pass(v):
        st['pass'] = v
        if st['fresh'] and st['host'] and st['user']:   # الهوست جاهز من قبل → اكتمل
            flush(st['host'])

    def p_host(v):
        st['host'] = v
        st['fresh'] = True
        if st['user'] and st['pass']:                   # الهوست أتى أخيراً → اكتمل
            flush(st['host'])

    for raw in (text or '').splitlines():
        line = (raw.replace('‏', '').replace('‎', '')
                .replace('﻿', '').replace('‍', '').strip())
        if not line or line.startswith('#EXTINF'):
            continue
        if re.search(r'user[\s_-]*agent', line, re.I):
            continue

        # --- Stalker: رابط بوابة + عنوان MAC (بأي شكل: سطر واحد أو سطرين، مع /portal.php أو بدونه) ---
        macs = mac_re.findall(line)
        line_urls = URL_RE.findall(line)
        if line_urls:
            pending_portal['u'] = line_urls[0]        # تذكّر آخر رابط كبوابة محتملة لأي MAC لاحق
        if macs:
            portal = None
            for u in line_urls:                       # فضّل رابطاً يبدو كبوابة
                if re.search(r'portal\.php|/c/|/stalker|/server/load\.php', u, re.I):
                    portal = u
                    break
            portal = portal or (line_urls[0] if line_urls else None) or pending_portal['u'] or st['host']
            if portal:
                add_stalker(portal, macs[0])
                if portal == st['host']:              # كان الهوست محجوزاً للـ Xtream → حرّره
                    st['host'] = st['user'] = st['pass'] = None
                    st['fresh'] = False
                pending_portal['u'] = None
            else:
                pending_mac['m'] = macs[0]            # MAC قبل الهوست → انتظر رابطاً لاحقاً
            continue                                   # سطر فيه MAC لا يُعامل كـ Xtream
        if pending_mac['m'] and line_urls:            # وصل الهوست بعد MAC سابق
            add_stalker(line_urls[0], pending_mac['m'])
            pending_mac['m'] = None
            continue

        # --- M3U: رابط قائمة مباشر بلا بيانات دخول Xtream ---
        if line_urls and not macs:
            u0 = line_urls[0]
            is_m3u = re.search(r'\.m3u8?(?:$|[?&])', u0, re.I) or (re.search(r'type=m3u', u0, re.I) and not re.search(r'username=', u0, re.I))
            if is_m3u and len(line_urls) == 1 and not (RE_USER.search(line) or RE_PASS.search(line)):
                _h, _u, _p = parse_url(u0)
                if not (_u and _p):                   # ليس رابط Xtream كامل
                    add_m3u(u0)
                    continue

        has_url = False
        has_label = bool(RE_HOST.search(line) or RE_USER.search(line) or RE_PASS.search(line))
        rest = line
        bare_hosts = []
        for u in URL_RE.findall(line):
            has_url = True
            rest = rest.replace(u, ' ')
            h, us, pw = parse_url(u)
            if not h:
                continue
            if us and pw:
                add(h, us, pw)                 # رابط كامل = سجل مستقل فوراً
            else:
                bare_hosts.append(h)
        if not has_url:
            mh = RE_HOST.search(rest)
            if mh:
                v = _tok(mh.group(1))
                if BARE_HOST_RE.match(v):
                    bare_hosts.append('http://' + v.lower().rstrip('/'))
                    rest = rest.replace(mh.group(0), ' ')
        mu = RE_USER.search(rest)
        mp = RE_PASS.search(rest)
        if mu:
            p_user(_tok(mu.group(1)))
        if mp:
            p_pass(_tok(mp.group(1)))
        if has_url and not has_label and not (mu or mp) and bare_hosts:
            toks = [t for t in re.split(r'[\s|,;]+', rest.strip(' \t|,;')) if t]
            if len(toks) == 2 and all(':' not in t and len(t) >= 2 for t in toks):
                p_user(_tok(toks[0]))
                p_pass(_tok(toks[1]))
        if not bare_hosts and not has_url and not (mu or mp):
            # سطر فيه عنوان بلا بروتوكول وبلا كلمة Host (مثل tr.babo01.com:8080) → اعتبره هوست
            tok = rest.strip().strip('\'"`*<>').strip()
            if BARE_HOST_RE.match(tok):
                bare_hosts.append('http://' + tok.lower().rstrip('/'))
        for h in bare_hosts:                   # الهوست يُعالَج أخيراً ليُكمل السجل
            p_host(h)

    # إغلاق آخر سجل (حالة كتابة الهوست مرة واحدة لعدة مستخدمين)
    if st['user'] and st['pass']:
        h = st['host'] or st['last']
        if h:
            add(h, st['user'], st['pass'])
    return out


# ----------------------------------------------------------------------------
#  الفحص
# ----------------------------------------------------------------------------
def _get(url, timeout, limit=8_000_000, opener=None):
    op = opener or _OPENER
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*', 'Connection': 'close'})
    t0 = time.time()
    with op.open(req, timeout=timeout) as r:
        data = r.read(limit)
        return r.status, data, int((time.time() - t0) * 1000)


def _fmt_ts(ts, fmt='%Y-%m-%d %H:%M'):
    try:
        return datetime.fromtimestamp(ts).strftime(fmt)
    except (OverflowError, OSError, ValueError):
        return ''


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _net_reason(e):
    reason = getattr(e, 'reason', e)
    s = str(reason).lower()
    if isinstance(reason, socket.timeout) or 'timed out' in s or isinstance(e, (socket.timeout, TimeoutError)):
        return 'انتهت مهلة الاتصال'
    if isinstance(reason, socket.gaierror) or 'name or service' in s or 'getaddrinfo' in s or 'nodename' in s:
        return 'اسم السيرفر غير موجود (DNS)'
    if 'refused' in s:
        return 'الاتصال مرفوض'
    if 'reset' in s:
        return 'السيرفر قطع الاتصال'
    if 'ssl' in s or 'certificate' in s:
        return 'مشكلة شهادة SSL'
    return str(reason)[:120]


def _fill_from_api(r, j):
    ui = j.get('user_info') or {}
    si = j.get('server_info') or {}
    auth = str(ui.get('auth', '')).strip()
    status = str(ui.get('status') or '').strip()
    r['status'] = status
    r['server_tz'] = si.get('timezone') or ''
    if auth == '0':
        r['state'] = 'invalid'
        r['message'] = 'اسم المستخدم أو كلمة المرور غير صحيحة'
        return
    now = time.time()
    exp = ui.get('exp_date')
    if exp in (None, '', 'null', 'None', '0', 0):
        r['unlimited'] = True
    else:
        ts = _int(exp)
        if ts:
            r['exp_ts'] = ts
            r['exp_date'] = _fmt_ts(ts)
            r['days_left'] = int((ts - now) // 86400)
    r['active_cons'] = _int(ui.get('active_cons'))
    r['max_cons'] = _int(ui.get('max_connections'))
    tr = ui.get('is_trial')
    r['trial'] = None if tr is None else str(tr).strip() == '1'
    c = _int(ui.get('created_at'))
    r['created'] = _fmt_ts(c, '%Y-%m-%d') if c else ''
    st = status.lower()
    expired = r['exp_ts'] is not None and r['exp_ts'] < now
    if st == 'expired' or expired:
        r['state'] = 'expired'
        r['message'] = 'الاشتراك منتهي'
    elif st == 'active' or not st:
        r['state'] = 'active'
        if r['active_cons'] is not None and r['max_cons'] and r['active_cons'] >= r['max_cons']:
            r['message'] = 'كل الاتصالات مستخدمة حالياً'
    else:
        r['state'] = 'blocked'
        r['message'] = f'حالة الحساب: {status}'


def _m3u_fallback(r, timeout, prev='', opener=None):
    try:
        _, data, ms = _get(r['m3u'], timeout, limit=4096, opener=opener)
        if r['ping_ms'] is None:
            r['ping_ms'] = ms
        if b'#EXTM3U' in data or b'#EXTINF' in data:
            r['state'] = 'active_m3u'
            r['message'] = 'السيرفر يعمل لكنه لا يُظهر تاريخ الانتهاء'
        else:
            r['state'] = 'invalid'
            r['message'] = prev or 'السيرفر لم يُرجع قائمة قنوات (غالباً بيانات خاطئة)'
    except urllib.error.HTTPError as e:
        r['state'] = 'invalid' if e.code in (401, 403, 404, 405, 512, 884) else 'error'
        r['message'] = (prev + ' / ' if prev else '') + f'HTTP {e.code}'
    except Exception as e:
        r['state'] = 'error'
        r['message'] = (prev + ' / ' if prev else '') + _net_reason(e)
    return r


def check(host, user, pw, timeout=DEFAULT_TIMEOUT, count_channels=False, proxy=''):
    host = host.rstrip('/')
    op = _OPENER
    if proxy:
        try:
            op = _build_opener(proxy)
        except Exception:
            op = _OPENER
    qs = urllib.parse.urlencode({'username': user, 'password': pw})
    r = dict(host=host, username=user, password=pw, state='error', status='', exp_ts=None,
             exp_date='', days_left=None, unlimited=False, active_cons=None, max_cons=None,
             trial=None, created='', ping_ms=None, channels=None, server_tz='', message='',
             m3u=f"{host}/get.php?{qs}&type=m3u_plus&output=ts")
    try:
        _, data, ms = _get(f"{host}/player_api.php?{qs}", timeout, opener=op)
        r['ping_ms'] = ms
    except urllib.error.HTTPError as e:
        return _m3u_fallback(r, timeout, prev=f'player_api: HTTP {e.code}', opener=op)
    except Exception as e:
        r['state'] = 'offline'
        r['message'] = _net_reason(e)
        return r

    try:
        j = json.loads(data.decode('utf-8', 'replace').strip() or 'null')
    except ValueError:
        j = None
    if isinstance(j, dict) and isinstance(j.get('user_info'), dict):
        _fill_from_api(r, j)
        if count_channels and r['state'] == 'active':
            try:
                _, d2, _ = _get(f"{host}/player_api.php?{qs}&action=get_live_streams",
                                max(timeout, 30), limit=80_000_000, opener=op)
                arr = json.loads(d2.decode('utf-8', 'replace'))
                if isinstance(arr, list):
                    r['channels'] = len(arr)
            except Exception:
                pass
        return r
    return _m3u_fallback(r, timeout, opener=op)


def _blank_result(**kw):
    r = dict(host='', username='', password='', state='error', status='', exp_ts=None,
             exp_date='', days_left=None, unlimited=False, active_cons=None, max_cons=None,
             trial=None, created='', ping_ms=None, channels=None, server_tz='', message='',
             m3u='', type='xtream')
    r.update(kw)
    return r


def _opener_for(proxy):
    if proxy:
        try:
            return _build_opener(proxy)
        except Exception:
            return _OPENER
    return _OPENER


def check_m3u(url, timeout=DEFAULT_TIMEOUT, proxy=''):
    """فحص رابط قائمة M3U مباشر: يعمل إن أرجع قائمة قنوات صالحة."""
    op = _opener_for(proxy)
    url = url.strip()
    r = _blank_result(type='m3u', host=url, m3u=url)
    try:
        _, data, ms = _get(url, timeout, limit=300000, opener=op)
        r['ping_ms'] = ms
        if b'#EXTM3U' in data or b'#EXTINF' in data:
            r['state'] = 'active_m3u'
            r['message'] = 'قائمة M3U تعمل'
        else:
            r['state'] = 'invalid'
            r['message'] = 'الرابط لا يُرجع قائمة M3U صالحة'
    except urllib.error.HTTPError as e:
        r['state'] = 'invalid' if e.code in (401, 403, 404, 405, 512) else 'error'
        r['message'] = f'HTTP {e.code}'
    except Exception as e:
        r['state'] = 'offline'
        r['message'] = _net_reason(e)
    return r


def _parse_stalker_date(v):
    if v in (None, '', 0, '0', 'null', 'None'):
        return None
    ts = _int(v)
    if ts and ts > 10_000_000:
        return ts
    s = str(v).strip()
    for fmt in ('%B %d, %Y, %I:%M %p', '%B %d, %Y, %I:%M%p', '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d', '%d.%m.%Y', '%B %d, %Y', '%d %B %Y'):
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except (ValueError, OverflowError):
            pass
    return None


_STALKER_UA = ('Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) '
               'MAG200 stbapp ver: 2 rev: 250 Safari/533.3')


def check_stalker(portal, mac, timeout=DEFAULT_TIMEOUT, proxy=''):
    """فحص بوابة Stalker/MAC: مصافحة للحصول على رمز ثم جلب معلومات الحساب والانتهاء."""
    op = _opener_for(proxy)
    portal = portal.strip()
    mac = mac.strip().upper()
    base = portal if portal.lower().startswith('http') else 'http://' + portal
    sep = '&' if '?' in base else '?'
    r = _blank_result(type='stalker', host=portal, username=mac)

    def g(url, token=None):
        h = {'User-Agent': _STALKER_UA, 'Accept': '*/*',
             'Cookie': f'mac={mac}; stb_lang=en; timezone=America/New_York',
             'X-User-Agent': 'Model: MAG250; Link: WiFi', 'Referer': base}
        if token:
            h['Authorization'] = 'Bearer ' + token
        t0 = time.time()
        with op.open(urllib.request.Request(url, headers=h), timeout=timeout) as resp:
            return resp.read(300000), int((time.time() - t0) * 1000)

    try:
        data, ms = g(base + sep + 'type=stb&action=handshake&token=&JsHttpRequest=1-xml')
        r['ping_ms'] = ms
        j = json.loads(data.decode('utf-8', 'replace').strip() or '{}')
        token = (j.get('js') or {}).get('token') if isinstance(j, dict) else None
        if not token:
            r['state'] = 'invalid'
            r['message'] = 'البوابة لم تُرجع رمز دخول (MAC غير صالح أو محظور)'
            return r
        try:
            g(base + sep + 'type=stb&action=get_profile&JsHttpRequest=1-xml', token)
        except Exception:
            pass
        d2, _ = g(base + sep + 'type=account_info&action=get_main_info&JsHttpRequest=1-xml', token)
        ji = json.loads(d2.decode('utf-8', 'replace').strip() or '{}')
        js = ji.get('js') or {} if isinstance(ji, dict) else {}
        end = js.get('end_date') or js.get('phone') or ''
        r['status'] = 'Active'
        ts = _parse_stalker_date(end)
        if ts:
            r['exp_ts'] = ts
            r['exp_date'] = _fmt_ts(ts)
            r['days_left'] = int((ts - time.time()) // 86400)
            if ts < time.time():
                r['state'] = 'expired'
                r['message'] = 'الاشتراك منتهي'
            else:
                r['state'] = 'active'
                r['message'] = 'بوابة Stalker تعمل'
        else:
            r['state'] = 'active'
            r['message'] = 'بوابة Stalker تعمل' + (f' — انتهاء: {end}' if end else ' (تاريخ الانتهاء غير متاح)')
    except urllib.error.HTTPError as e:
        r['state'] = 'invalid' if e.code in (401, 403, 404, 512) else 'error'
        r['message'] = f'HTTP {e.code}'
    except Exception as e:
        r['state'] = 'offline'
        r['message'] = _net_reason(e)
    return r


def _xt(host, qs, action, timeout, limit):
    _, d, _ = _get(f"{host}/player_api.php?{qs}&action={action}", timeout, limit=limit)
    return json.loads(d.decode('utf-8', 'replace'))


def _cats(host, qs, action, timeout):
    try:
        arr = _xt(host, qs, action, timeout, 20_000_000)
        if isinstance(arr, list):
            return {str(c.get('category_id')): c.get('category_name', '')
                    for c in arr if isinstance(c, dict)}
    except Exception:
        pass
    return {}


def _xlsx_cells(data_bytes):
    """يقرأ خلايا أول ورقة في ملف xlsx ويرجع {رقم الصف: {حرف العمود: نص}}."""
    import io
    import zipfile
    import xml.etree.ElementTree as ET
    strip = lambda tag: tag.split('}')[-1]
    z = zipfile.ZipFile(io.BytesIO(data_bytes))
    names = z.namelist()
    shared = []
    if 'xl/sharedStrings.xml' in names:
        root = ET.fromstring(z.read('xl/sharedStrings.xml'))
        for si in root:
            if strip(si.tag) == 'si':
                shared.append(''.join(t.text or '' for t in si.iter() if strip(t.tag) == 't'))
    sheet = next((n for n in names if n.startswith('xl/worksheets/') and n.endswith('.xml')), None)
    rows = {}
    if sheet:
        root = ET.fromstring(z.read(sheet))
        for c in root.iter():
            if strip(c.tag) != 'c':
                continue
            ref = c.get('r') or ''
            col = ''.join(ch for ch in ref if ch.isalpha())
            digits = ''.join(ch for ch in ref if ch.isdigit())
            if not col or not digits:
                continue
            t = c.get('t')
            val = ''
            if t == 's':
                v = next((x for x in c if strip(x.tag) == 'v'), None)
                if v is not None and v.text is not None:
                    try:
                        val = shared[int(v.text)]
                    except (ValueError, IndexError):
                        val = ''
            elif t == 'inlineStr':
                val = ''.join(x.text or '' for x in c.iter() if strip(x.tag) == 't')
            else:
                v = next((x for x in c if strip(x.tag) == 'v'), None)
                val = v.text if (v is not None and v.text is not None) else ''
            rows.setdefault(int(digits), {})[col] = val or ''
    return rows


def _parse_combo(s):
    h = u = p = ''
    lines = [l.strip() for l in re.split(r'[\r\n]+', s or '') if l.strip()]
    labeled = False
    for l in lines:
        m = re.match(r'(?i)^\s*(host|server|url|السيرفر|هوست|الخادم|الرابط)\s*[:=]\s*(.+)$', l)
        if m:
            h = m.group(2).strip(); labeled = True; continue
        m = re.match(r'(?i)^\s*(user\s*name|username|user|login|المستخدم|يوزر)\s*[:=]\s*(.+)$', l)
        if m:
            u = m.group(2).strip(); labeled = True; continue
        m = re.match(r'(?i)^\s*(pass\s*word|password|pass|pwd|كلمة\s*المرور|المرور|باسورد|باس)\s*[:=]\s*(.+)$', l)
        if m:
            p = m.group(2).strip(); labeled = True; continue
    if not labeled and len(lines) >= 3:         # ثلاثة أسطر بلا دلالات → هوست/مستخدم/مرور
        h, u, p = lines[0], lines[1], lines[2]
    return h, u, p


def parse_xlsx(data_bytes):
    """يستخرج (host, user, pass) من ملف إكسل مصدَّر من الأداة أو أي ملف بأعمدة مشابهة."""
    rows = _xlsx_cells(data_bytes)
    if not rows:
        return []
    hdr_rn = min(rows)
    hmap = {}
    for col, txt in rows[hdr_rn].items():
        t = (txt or '').strip().lower()
        if not t:
            continue
        if 'm3u' in t or 'رابط' in t:
            continue
        if any(w in t for w in ['السيرفر', 'host', 'هوست', 'الخادم', 'server', 'dns']):
            hmap.setdefault('host', col)
        elif any(w in t for w in ['المستخدم', 'user', 'يوزر', 'login']):
            hmap.setdefault('user', col)
        elif any(w in t for w in ['كلمة المرور', 'المرور', 'password', 'pass', 'باس']):
            hmap.setdefault('pass', col)
        elif 'دلالة' in t or 'هوست/مستخدم' in t or 'بيانات' in t:
            hmap.setdefault('combo', col)
    out, seen = [], set()
    for rn in sorted(rows):
        if rn == hdr_rn:
            continue
        row = rows[rn]
        h = row.get(hmap.get('host', ''), '')
        u = row.get(hmap.get('user', ''), '')
        p = row.get(hmap.get('pass', ''), '')
        if not (h and u and p) and 'combo' in hmap:
            h2, u2, p2 = _parse_combo(row.get(hmap['combo'], ''))
            h, u, p = h or h2, u or u2, p or p2
        h, u, p = (h or '').strip(), (u or '').strip(), (p or '').strip()
        if not (h and u and p):
            continue
        if not re.match(r'(?i)^https?://', h) and BARE_HOST_RE.match(h):
            h = 'http://' + h.lower().rstrip('/')
        k = (h.lower(), u, p)
        if k not in seen:
            seen.add(k)
            out.append({'host': h, 'username': u, 'password': p})
    return out


def get_streams(host, user, pw, kind, timeout=DEFAULT_TIMEOUT):
    """يرجع عناصر نوع واحد (live/vod/series) — للتحميل عند الطلب."""
    host = host.rstrip('/')
    qs = urllib.parse.urlencode({'username': user, 'password': pw})
    action = {'live': 'get_live_streams', 'vod': 'get_vod_streams', 'series': 'get_series'}.get(kind, 'get_live_streams')
    idkey = 'series_id' if kind == 'series' else 'stream_id'
    extdef = 'mp4' if kind == 'vod' else 'ts'
    out = {'items': [], 'error': ''}
    try:
        arr = _xt(host, qs, action, max(timeout, 30), 180_000_000)
        if isinstance(arr, list):
            for c in arr:
                if not isinstance(c, dict) or c.get(idkey) is None:
                    continue
                it = {'id': c[idkey], 'name': c.get('name', '') or ('#' + str(c[idkey])),
                      'cat': str(c.get('category_id', ''))}
                if kind != 'series':
                    it['ext'] = c.get('container_extension') or extdef
                out['items'].append(it)
    except Exception as e:
        out['error'] = _net_reason(e)
    return out


def get_content(host, user, pw, timeout=DEFAULT_TIMEOUT):
    """فتح سريع: تصنيفات الأنواع الثلاثة + عناصر البث المباشر فقط.
    الأفلام والمسلسلات تُحمَّل عند فتح قسمها عبر get_streams."""
    host = host.rstrip('/')
    qs = urllib.parse.urlencode({'username': user, 'password': pw})
    res = {'live': {'cats': {}, 'items': []}, 'vod': {'cats': {}},
           'series': {'cats': {}}, 'error': ''}
    res['live']['cats'] = _cats(host, qs, 'get_live_categories', timeout)
    res['vod']['cats'] = _cats(host, qs, 'get_vod_categories', timeout)
    res['series']['cats'] = _cats(host, qs, 'get_series_categories', timeout)
    live = get_streams(host, user, pw, 'live', timeout)
    res['live']['items'] = live['items']
    res['error'] = live.get('error', '')
    return res


def get_series_info(host, user, pw, series_id, timeout=DEFAULT_TIMEOUT):
    """يرجع حلقات مسلسل معيّن مرتبة حسب المواسم."""
    host = host.rstrip('/')
    qs = urllib.parse.urlencode({'username': user, 'password': pw})
    out = {'episodes': [], 'error': ''}
    try:
        _, d, _ = _get(f"{host}/player_api.php?{qs}&action=get_series_info"
                       f"&series_id={urllib.parse.quote(str(series_id))}", max(timeout, 30), limit=80_000_000)
        j = json.loads(d.decode('utf-8', 'replace'))
        eps = j.get('episodes') if isinstance(j, dict) else None
        if isinstance(eps, dict):
            for season in sorted(eps, key=lambda x: _int(x) if _int(x) is not None else 0):
                for e in eps[season]:
                    if not isinstance(e, dict) or e.get('id') is None:
                        continue
                    title = e.get('title') or f"S{season}E{e.get('episode_num', '')}"
                    out['episodes'].append({'id': e['id'], 'name': title,
                                            'ext': e.get('container_extension') or 'mp4', 'cat': ''})
    except Exception as e:
        out['error'] = _net_reason(e)
    return out


def _direct_url(host, user, pw, sid, ext, kind):
    seg = {'live': 'live', 'vod': 'movie', 'movie': 'movie', 'series': 'series'}.get(kind, 'live')
    return (f"{host.rstrip('/')}/{seg}/{urllib.parse.quote(user)}/"
            f"{urllib.parse.quote(pw)}/{sid}.{str(ext).lstrip('.')}")


def launch_external(url):
    """يشغّل الرابط في VLC إن وُجد، وإلا في المشغّل الافتراضي عبر ملف تشغيل مؤقت."""
    import os
    import shutil
    import tempfile
    import subprocess
    vlc = shutil.which('vlc')
    if not vlc:
        cands = []
        if sys.platform.startswith('win'):
            pf = os.environ.get('ProgramFiles', r'C:\Program Files')
            pf86 = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
            cands = [pf + r'\VideoLAN\VLC\vlc.exe', pf86 + r'\VideoLAN\VLC\vlc.exe']
        elif sys.platform == 'darwin':
            cands = ['/Applications/VLC.app/Contents/MacOS/VLC']
        vlc = next((c for c in cands if os.path.exists(c)), None)
    if vlc:
        subprocess.Popen([vlc, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 'vlc'
    f = tempfile.NamedTemporaryFile('w', suffix='.m3u', delete=False, encoding='utf-8')
    f.write('#EXTM3U\n' + url + '\n')
    f.close()
    if sys.platform.startswith('win'):
        os.startfile(f.name)  # noqa
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', f.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(['xdg-open', f.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 'default'


def _xesc(v):
    return (str(v).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&apos;'))


def _col(i):
    s = ''
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


_XLSX_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="3">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
<font><sz val="11"/><color rgb="FF1F2937"/><name val="Calibri"/></font>
</fonts>
<fills count="7">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF374151"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFD1FAE5"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFEF3C7"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFEE2E2"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFEDE9FE"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color rgb="FFD1D5DB"/></left><right style="thin"><color rgb="FFD1D5DB"/></right><top style="thin"><color rgb="FFD1D5DB"/></top><bottom style="thin"><color rgb="FFD1D5DB"/></bottom></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="10">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="4" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="5" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="6" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="top" wrapText="1"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

_STATE_AR = {'active': 'يعمل', 'active_m3u': 'يعمل (بدون تاريخ)', 'expired': 'منتهي',
             'invalid': 'بيانات خاطئة', 'blocked': 'محظور/معطّل', 'offline': 'لا يستجيب',
             'error': 'خطأ', 'pending': 'لم يُفحص', 'checking': 'جاري الفحص'}
_STATE_XF = {'active': 3, 'active_m3u': 3, 'expired': 4, 'invalid': 5, 'blocked': 5,
             'offline': 6, 'error': 6, 'pending': 2, 'checking': 2}


def _cell(ref, xf, val, num=False):
    if val is None or val == '':
        return f'<c r="{ref}" s="{xf}"/>'
    if num:
        return f'<c r="{ref}" s="{xf}"><v>{_xesc(val)}</v></c>'
    return f'<c r="{ref}" s="{xf}" t="inlineStr"><is><t xml:space="preserve">{_xesc(val)}</t></is></c>'


def build_xlsx(rows):
    heads = ['الحالة', 'السيرفر', 'المستخدم', 'كلمة المرور', 'تاريخ الانتهاء', 'المتبقي (أيام)',
             'الاتصالات', 'تاريخ الإنشاء', 'الاستجابة (ms)', 'ملاحظات', 'رابط M3U',
             'البيانات (هوست/مستخدم/مرور)', 'البيانات مع الدلالة']
    widths = [17, 34, 18, 18, 18, 14, 12, 14, 14, 40, 52, 40, 44]
    xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
           '<sheetViews><sheetView rightToLeft="1" tabSelected="1" workbookViewId="0">',
           '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>',
           '</sheetView></sheetViews>',
           '<sheetFormatPr defaultRowHeight="16"/>',
           '<cols>']
    for i, w in enumerate(widths):
        xml.append(f'<col min="{i+1}" max="{i+1}" width="{w}" customWidth="1"/>')
    xml.append('</cols><sheetData>')
    xml.append('<row r="1" ht="22" customHeight="1">')
    for i, h in enumerate(heads):
        xml.append(_cell(f'{_col(i)}1', 1, h))
    xml.append('</row>')
    for ri, r in enumerate(rows, start=2):
        st = r.get('state', '')
        unlimited = r.get('unlimited')
        dl = r.get('days_left')
        days = 'غير محدود' if unlimited else ('' if dl is None else dl)
        mx, ac = r.get('max_cons'), r.get('active_cons')
        conns = f"{ac if ac is not None else '?'} / {mx if mx is not None else '?'}" if (mx is not None or ac is not None) else ''
        exp = r.get('exp_date') or ('غير محدود' if unlimited else '')
        vals = [
            (_STATE_XF.get(st, 2), _STATE_AR.get(st, st), False),
            (7, r.get('host', ''), False),
            (7, r.get('username', ''), False),
            (7, r.get('password', ''), False),
            (8, exp, False),
            (8, days, isinstance(days, (int, float))),
            (8, conns, False),
            (8, r.get('created', ''), False),
            (8, r.get('ping_ms'), isinstance(r.get('ping_ms'), (int, float))),
            (2, r.get('message', ''), False),
            (7, r.get('m3u', ''), False),
            (9, '\n'.join([r.get('host', ''), r.get('username', ''), r.get('password', '')]), False),
            (9, 'Host: ' + r.get('host', '') + '\nUser: ' + r.get('username', '') + '\nPass: ' + r.get('password', ''), False),
        ]
        cells = [_cell(f'{_col(i)}{ri}', xf, val, num=num) for i, (xf, val, num) in enumerate(vals)]
        xml.append(f'<row r="{ri}" ht="48" customHeight="1">' + ''.join(cells) + '</row>')
    last = _col(len(heads) - 1)
    xml.append('</sheetData>')
    xml.append(f'<autoFilter ref="A1:{last}1"/>')
    xml.append('</worksheet>')
    sheet = ''.join(xml)

    import io
    import zipfile
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED)
    z.writestr('[Content_Types].xml',
               '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
               '<Default Extension="xml" ContentType="application/xml"/>'
               '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
               '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
               '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
               '</Types>')
    z.writestr('_rels/.rels',
               '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
               '</Relationships>')
    z.writestr('xl/workbook.xml',
               '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
               'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
               '<sheets><sheet name="السيرفرات" sheetId="1" r:id="rId1"/></sheets></workbook>')
    z.writestr('xl/_rels/workbook.xml.rels',
               '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
               '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
               '</Relationships>')
    z.writestr('xl/styles.xml', _XLSX_STYLES)
    z.writestr('xl/worksheets/sheet1.xml', sheet)
    z.close()
    return buf.getvalue()


_IPV4_PORT = re.compile(r'(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})')
_PROXY_LISTS = [
    'https://www.proxy-list.download/api/v1/get?type=http',
    'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt',
    'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt',
    'https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt',
]


_CC_NAMES = {'eg': 'مصر', 'sa': 'السعودية', 'us': 'أمريكا', 'gb': 'بريطانيا',
             'de': 'ألمانيا', 'nl': 'هولندا', 'fr': 'فرنسا'}


def _proxy_sources(cc):
    c = (cc or 'any').lower()
    scrape = (f'https://api.proxyscrape.com/v2/?request=getproxies&protocol=http'
              f'&timeout=8000&country={"all" if c == "any" else c}&ssl=all&anonymity=all')
    if c == 'any':
        return [scrape] + _PROXY_LISTS      # أي دولة: كل المصادر
    return [scrape]                          # دولة محددة: المصدر المفلتر بالدولة فقط


def _fetch_text(url, opener, timeout=15, limit=5_000_000):
    with opener.open(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=timeout) as r:
        return r.read(limit).decode('utf-8', 'replace')


def gather_proxies(cc='any', cap=160):
    direct = _build_opener('')
    out, seen = [], set()
    for u in _proxy_sources(cc):
        try:
            txt = _fetch_text(u, direct)
        except Exception:
            continue
        for m in _IPV4_PORT.finditer(txt):
            hp = m.group(1) + ':' + m.group(2)
            if hp not in seen:
                seen.add(hp)
                out.append(hp)
        if len(out) >= cap:
            break
    return out[:cap]


def _test_proxy_hp(hp, timeout=6):
    try:
        op = _build_opener('http://' + hp)
        with op.open(urllib.request.Request('http://api.ipify.org/?format=json',
                                            headers={'User-Agent': UA}), timeout=timeout) as r:
            return json.loads(r.read(2000).decode('utf-8', 'replace')).get('ip')
    except Exception:
        return None


def _geo(ip):
    try:
        direct = _build_opener('')
        txt = _fetch_text(f'http://ip-api.com/json/{ip}?fields=country,countryCode', direct, timeout=8, limit=2000)
        return json.loads(txt)
    except Exception:
        return {}


def find_working_proxy(cc='any', budget=45, apply=True):
    want = (cc or 'any').lower()
    want = '' if want == 'any' else want
    cname = _CC_NAMES.get(want, want.upper())
    cands = gather_proxies(cc)
    if not cands:
        if want:
            return {'ok': False, 'reason': f'لا توجد قائمة بروكسي متاحة لـ{cname} الآن — جرّب «أي دولة» أو أدخل بروكسي يدوياً.'}
        return {'ok': False, 'reason': 'تعذّر جلب قوائم البروكسي — تحقق من الإنترنت وحاول مجدداً.'}
    start = time.time()
    found = None
    other = None
    ex = ThreadPoolExecutor(max_workers=40)
    futs = {ex.submit(_test_proxy_hp, hp): hp for hp in cands}
    try:
        for f in as_completed(futs):
            if time.time() - start > budget:
                break
            ip = f.result()
            if not ip:
                continue
            hp = futs[f]
            if not want:
                found = (hp, ip, {})
                break
            info = _geo(ip)                              # تحقّق من بلد البروكسي فعلياً
            if (info.get('countryCode', '') or '').lower() == want:
                found = (hp, ip, info)
                break
            other = other or (hp, ip, info)             # يعمل لكن بدولة أخرى
    finally:
        ex.shutdown(wait=False)
    if not found:
        if want and other:
            return {'ok': False, 'reason': f'لم أجد بروكسي يعمل في {cname} الآن (المتاح من دول أخرى فقط). حاول مجدداً أو أدخل بروكسي {cname} يدوياً.'}
        return {'ok': False, 'reason': f'جرّبت {len(cands)} بروكسي ولم يعمل أيٌّ منها الآن — حاول مرة أخرى بعد قليل.'}
    hp, ip, info = found
    url = 'http://' + hp
    if apply:
        apply_proxy(url)
        save_cfg({'proxy': url})
    return {'ok': True, 'proxy': url, 'ip': ip,
            'country': info.get('country', ''), 'cc': info.get('countryCode', ''),
            'tested': len(cands)}


def find_proxies(cc='us', count=5, budget=55):
    """يرجع عدة بروكسيات تعمل (مع التحقق من الدولة) لإعادة التجربة عبرها."""
    want = (cc or 'any').lower()
    want = '' if want == 'any' else want
    cands = gather_proxies(cc)
    out = {'ok': False, 'proxies': [], 'reason': ''}
    if not cands:
        out['reason'] = 'تعذّر جلب قوائم البروكسي.'
        return out
    start = time.time()
    found = []
    ex = ThreadPoolExecutor(max_workers=50)
    futs = {ex.submit(_test_proxy_hp, hp): hp for hp in cands}
    try:
        for f in as_completed(futs):
            if time.time() - start > budget or len(found) >= count:
                break
            ip = f.result()
            if not ip:
                continue
            hp = futs[f]
            if want:
                info = _geo(ip)
                if (info.get('countryCode', '') or '').lower() != want:
                    continue
            found.append('http://' + hp)
    finally:
        ex.shutdown(wait=False)
    out['ok'] = bool(found)
    out['proxies'] = found
    if not found:
        out['reason'] = f'لم أجد بروكسي {_CC_NAMES.get(want, want.upper()) or ""} يعمل الآن.'
    return out


# ----------------------------------------------------------------------------
#  الواجهة
# ----------------------------------------------------------------------------
HTML = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>فاحص سيرفرات IPTV</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%230e1318'/%3E%3Cpath d='M20 22a24 24 0 0 1 24 24' fill='none' stroke='%2322c55e' stroke-width='4' stroke-linecap='round'/%3E%3Cpath d='M20 32a14 14 0 0 1 14 14' fill='none' stroke='%233b82f6' stroke-width='4' stroke-linecap='round'/%3E%3Ccircle cx='21' cy='45' r='4.5' fill='%233b82f6'/%3E%3Cpath d='M34 17l17 9.5-17 9.5z' fill='%23e6edf3'/%3E%3C/svg%3E">
<script>window.__PROXY__=__PROXYJSON__;</script>
<style>
:root{--bg:#0e1318;--card:#171e26;--line:#26313d;--text:#e6edf3;--mute:#8b98a5;--acc:#3b82f6;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444;--off:#a855f7}
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;background:var(--bg);color:var(--text);font-family:"Segoe UI",Tahoma,"Noto Kufi Arabic",Arial,sans-serif;font-size:14px}
header{padding:16px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
h1{font-size:20px;margin:0}
main{padding:18px 24px;max-width:1600px;margin:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
textarea{width:100%;min-height:170px;background:#0a0e12;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px;font-family:Consolas,"Courier New",monospace;font-size:13px;direction:ltr;text-align:left;resize:vertical}
textarea:focus,input:focus,select:focus{outline:2px solid var(--acc);outline-offset:-1px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px}
.row > button{flex:0 0 auto}
main{overflow-x:hidden}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:9px 16px;font:inherit;cursor:pointer;font-weight:600}
button:hover{filter:brightness(1.1)}
button.sec{background:#26313d}
button.danger{background:var(--bad)}
button:disabled{opacity:.45;cursor:default;filter:none}
label.opt{display:flex;gap:6px;align-items:center;color:var(--mute)}
input[type=number],input[type=search],input[type=text],select{background:#0a0e12;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
input[type=number]{width:66px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;cursor:pointer;text-align:center;user-select:none}
.stat b{display:block;font-size:26px;margin-bottom:2px}
.stat.sel{outline:2px solid var(--acc)}
.prog{height:6px;background:#0a0e12;border-radius:4px;overflow:hidden;margin-top:14px}
.prog div{height:100%;width:0;background:var(--acc);transition:width .2s}
.tw{overflow:auto;max-height:68vh;border-radius:8px}
table{width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right;vertical-align:middle;word-break:break-word;overflow-wrap:anywhere}
th{color:var(--mute);font-weight:600;position:sticky;top:0;background:var(--card);z-index:1}
tr:hover td{background:#1c2530}
tr.dragging{opacity:.4}
tr.bestrow td{background:#132617}
.dup{display:inline-block;background:#3b2a12;color:#fcd34d;border-radius:6px;padding:0 5px;font-size:11px;margin-inline-start:4px;direction:ltr}
.best{margin-inline-start:3px}
.viap{display:inline-block;background:#0d3b2e;color:#6ee7b7;border:1px solid #10b981;border-radius:6px;padding:0 5px;font-size:11px;margin-inline-start:4px}
tr.viarow td{background:#0d241c}
tr.drop-into td{box-shadow:inset 0 2px 0 var(--acc)}
.drag{cursor:grab;color:var(--mute);user-select:none;white-space:nowrap}
.drag:active{cursor:grabbing}
.ltr{direction:ltr;unicode-bidi:isolate;font-family:Consolas,"Courier New",monospace;word-break:break-all}
.host{overflow-wrap:anywhere}
.acts{white-space:nowrap}
.namein{width:100%;min-width:70px;background:transparent;border:1px solid transparent;color:var(--text);border-radius:6px;padding:5px 7px}
.namein:hover{border-color:var(--line)}
.namein:focus{background:#0a0e12;border-color:var(--acc)}
/* عرض الأعمدة — المجموع 100% ليتّسع الجدول دون تمرير */
col.c-num{width:3%} col.c-state{width:9%} col.c-host{width:12%} col.c-user{width:10%}
col.c-pass{width:8%} col.c-exp{width:9%} col.c-days{width:6%} col.c-conn{width:6%}
col.c-ping{width:6%} col.c-note{width:7%} col.c-name{width:13%} col.c-act{width:11%}
.b{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
.s-active{background:#123d24;color:#86efac}
.s-expired{background:#4a3208;color:#fcd34d}
.s-invalid{background:#4c1414;color:#fca5a5}
.s-offline{background:#3b1d52;color:#d8b4fe}
.s-pending{background:#26313d;color:var(--mute)}
.s-checking{background:#16325c;color:#93c5fd}
.small{color:var(--mute);font-size:12px}
.icon{background:none;padding:1px 3px;color:var(--mute);font-size:14px}
.icon:hover{background:#26313d}
.note{white-space:normal;line-height:1.35;max-height:2.8em;overflow:hidden}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#26313d;padding:10px 18px;border-radius:8px;opacity:0;transition:opacity .2s;pointer-events:none}
#toast.on{opacity:1}
body.drag main{outline:3px dashed var(--acc);outline-offset:-8px;border-radius:12px}
.ov{position:fixed;inset:0;background:rgba(0,0,0,.72);display:none;align-items:center;justify-content:center;z-index:50;padding:14px}
.ov.on{display:flex}
.modal{background:var(--card);border:1px solid var(--line);border-radius:14px;width:100%;max-width:1040px;max-height:94vh;display:flex;flex-direction:column;overflow:hidden}
.mhead{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.mbody{display:flex;gap:12px;padding:12px;min-height:0;flex:1;overflow:hidden}
.mvid{flex:2;min-width:0;display:flex;flex-direction:column;gap:8px}
.mlist{flex:1;min-width:220px;max-width:340px;display:flex;flex-direction:column;gap:8px;min-height:0}
.ov video{width:100%;background:#000;border-radius:8px;aspect-ratio:16/9;max-height:56vh}
.chlist{overflow:auto;border:1px solid var(--line);border-radius:8px;flex:1;min-height:130px}
.ch{padding:7px 10px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}
.ch:hover{background:#1c2530}
.ch.sel{background:#16325c;color:#93c5fd}
.tabs{display:flex;gap:4px}
.tab{flex:1;background:#0a0e12;border:1px solid var(--line);color:var(--mute);border-radius:7px;padding:6px 4px;font-size:12px;cursor:pointer;font-weight:600}
.tab.sel{background:var(--acc);color:#fff;border-color:var(--acc)}
@media(max-width:760px){.mbody{flex-direction:column}.mlist{max-width:none}.ov video{max-height:40vh}}
</style>
</head>
<body>
<header>
  <h1>📡 فاحص سيرفرات <span class="ltr">IPTV</span></h1>
  <span class="small">يعمل محلياً على جهازك — الإصدار <span class="ltr">__VER__</span></span>
</header>
<main>
<div class="card">
  <div class="small" style="margin-bottom:8px">
    الصق السيرفرات هنا بأي شكل وبأي عدد: روابط
    <span class="ltr">M3U</span>
    ، روابط
    <span class="ltr">player_api</span>
    ، روابط القنوات، أو نصوص تحتوي على
    <span class="ltr">Host / User / Pass</span>
    — الأداة تستخرج البيانات وتحذف المكرر تلقائياً.
    <br>📷 ويمكنك أيضاً لصق صورة
    (<span class="ltr">Ctrl+V</span> / <span class="ltr">⌘+V</span>)
    أو سحبها إلى هنا، أو اختيارها من زر «قراءة من صورة».
  </div>
  <textarea id="inp" spellcheck="false" placeholder="http://example.com:8080/get.php?username=USER&password=PASS&type=m3u_plus

Host: http://example.com:8080
Username: USER
Password: PASS

http://example.com:8080/live/USER/PASS/12345.ts"></textarea>
  <div class="row">
    <button id="btnGo">🔍 استخراج وفحص المكتوب</button>
    <button class="sec" id="btnExtract">استخراج فقط</button>
    <button id="btnCheckAll">✅ اختبار كل السيرفرات</button>
    <button class="danger" id="btnStop" disabled>⏹ إيقاف</button>
    <button class="sec" id="btnImg">📷 قراءة من صورة</button>
    <input type="file" id="img" accept="image/*" multiple hidden>
    <button class="sec" id="btnFile">📂 فتح ملف</button>
    <input type="file" id="file" accept=".txt,.m3u,.m3u8,.csv,.json,.xlsx,image/*" multiple hidden>
    <button class="sec" id="btnClearText">🧹 مسح الكتابة</button>
    <button class="sec" id="btnClear">🗑 مسح الكل</button>
    <span style="flex:1"></span>
    <label class="opt">عدد الفحوص المتزامنة <input type="number" id="conc" value="8" min="1" max="50"></label>
    <label class="opt">المهلة بالثواني <input type="number" id="tmo" value="15" min="3" max="120"></label>
    <label class="opt"><input type="checkbox" id="ara"> الصور فيها كتابة عربية</label>
    <label class="opt"><input type="checkbox" id="noport" checked> تجاهل المنفذ (بعد :) عند حذف المكرر</label>
    <label class="opt"><input type="checkbox" id="retryproxy"> أعد تجربة غير العاملة عبر VPN أمريكي</label>
  </div>
  <div class="row">
    <span class="small">🌐 بروكسي لتجاوز الحجب (اختياري):</span>
    <input type="text" id="proxy" class="ltr" placeholder="http://user:pass@host:port" style="flex:1 1 180px;min-width:0">
    <button class="sec" id="btnProxy">تفعيل واختبار</button>
    <select id="proxyCC" title="الدولة المفضّلة للبروكسي التلقائي">
      <option value="any">أي دولة</option>
      <option value="eg">مصر</option>
      <option value="sa">السعودية</option>
      <option value="us">أمريكا</option>
      <option value="gb">بريطانيا</option>
      <option value="de">ألمانيا</option>
      <option value="nl">هولندا</option>
      <option value="fr">فرنسا</option>
    </select>
    <button class="sec" id="btnAuto">🔎 ابحث تلقائياً</button>
    <button class="sec" id="btnProxyOff">إيقاف</button>
    <span class="small" id="proxyMsg" style="flex-basis:100%"></span>
  </div>
  <div class="prog"><div id="bar"></div></div>
  <div class="small" id="msg" style="margin-top:6px">&nbsp;</div>
</div>

<div class="stats" id="stats"></div>

<div class="card" style="margin-top:16px">
  <div class="row" style="margin-top:0;margin-bottom:12px">
    <label class="opt">الترتيب
      <select id="sort">
        <option value="orig">يدوي / حسب الإدخال (اسحب للترتيب)</option>
        <option value="daysDesc">الأطول صلاحية أولاً</option>
        <option value="daysAsc">الأقرب انتهاءً أولاً</option>
        <option value="state">حسب الحالة</option>
        <option value="ping">الأسرع استجابة</option>
      </select>
    </label>
    <input type="search" id="q" placeholder="بحث في السيرفر أو المستخدم..." style="flex:1 1 160px;min-width:0">
    <button class="sec" id="btnReverse">⇅ عكس الترتيب</button>
    <button class="sec" id="btnDedup">🧹 حذف المكرر (إبقاء الأفضل)</button>
    <span style="flex:1"></span>
    <button class="sec" id="btnRecheck">🔁 إعادة فحص غير الشغالة</button>
    <button class="sec" id="btnCopy">📋 نسخ روابط الشغالة</button>
    <button class="sec" id="btnCsv">⬇️ تصدير إلى إكسل</button>
    <button class="sec" id="btnSave">💾 حفظ مشروع</button>
    <button class="sec" id="btnImport">📁 استيراد مشروع</button>
    <button class="sec" id="btnIptv">📤 تصدير إلى iptvnator</button>
    <input type="file" id="proj" accept=".json,application/json" hidden>
  </div>
  <div class="tw">
    <table>
      <colgroup>
        <col class="c-num"><col class="c-state"><col class="c-host"><col class="c-user"><col class="c-pass">
        <col class="c-exp"><col class="c-days"><col class="c-conn"><col class="c-ping"><col class="c-note">
        <col class="c-name"><col class="c-act">
      </colgroup>
      <thead><tr>
        <th>#</th><th>الحالة</th><th>السيرفر</th><th>المستخدم</th><th>كلمة المرور</th>
        <th>تاريخ الانتهاء</th><th>المتبقي</th><th>الاتصالات</th>
        <th>الاستجابة</th><th>ملاحظات</th><th>اسم iptvnator</th><th></th>
      </tr></thead>
      <tbody id="tb"></tbody>
    </table>
  </div>
  <div class="small" id="empty" style="padding:24px;text-align:center">لا توجد سيرفرات بعد — الصق القائمة بالأعلى واضغط «استخراج وفحص».</div>
</div>
</main>
<div id="toast"></div>

<div class="ov" id="ov">
  <div class="modal">
    <div class="mhead">
      <b>▶️ تشغيل ومعاينة</b>
      <span class="small ltr" id="pvHost" style="flex:1;overflow:hidden;text-overflow:ellipsis"></span>
      <button class="sec" id="pvExt" title="فتح رابط القناة في مشغل خارجي مثل VLC">فتح خارجياً</button>
      <button class="sec" id="pvCopy">نسخ رابط القناة</button>
      <button class="danger" id="pvClose">إغلاق ✕</button>
    </div>
    <div class="mbody">
      <div class="mvid">
        <video id="pvVideo" controls autoplay playsinline></video>
        <div class="small" id="pvMsg">اختر قناة من القائمة لبدء التشغيل.</div>
      </div>
      <div class="mlist">
        <div class="tabs" id="pvTabs">
          <button class="tab sel" data-k="live">📺 بث مباشر</button>
          <button class="tab" data-k="vod">🎬 أفلام</button>
          <button class="tab" data-k="series">📂 مسلسلات</button>
        </div>
        <button class="sec" id="pvBack" style="display:none">↪ رجوع لقائمة المسلسلات</button>
        <input type="search" id="pvQ" placeholder="ابحث...">
        <select id="pvCat"><option value="">كل التصنيفات</option></select>
        <div class="chlist" id="pvChs"></div>
      </div>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const ST = {
  pending:['في الانتظار','s-pending'], checking:['جاري الفحص…','s-checking'],
  active:['يعمل ✅','s-active'], active_m3u:['يعمل (بدون تاريخ)','s-active'],
  expired:['منتهي ⌛','s-expired'], invalid:['بيانات خاطئة','s-invalid'],
  blocked:['محظور / معطّل','s-invalid'], offline:['لا يستجيب ❌','s-offline'], error:['خطأ','s-offline']
};
const FILTERS = [['all','الإجمالي'],['ok','يعمل'],['expired','منتهي'],['bad','بيانات خاطئة / محظور'],['offline','لا يستجيب / خطأ'],['pending','لم يُفحص'],['viaproxy','تعمل عبر VPN']];
const ORDER = {active:0,active_m3u:1,expired:2,blocked:3,invalid:4,offline:5,error:6,checking:7,pending:8};
let rows = [], seq = 0, stop = false, running = false, filter = 'all', renderQueued = false;

const grp = s => (s==='active'||s==='active_m3u')?'ok':s==='expired'?'expired':(s==='invalid'||s==='blocked')?'bad':(s==='offline'||s==='error')?'offline':'pending';
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const hasPort = h => /:\d+\/?$/.test(String(h||''));
const stripPort = h => String(h||'').toLowerCase().replace(/\/+$/,'').replace(/:\d+$/,'');
function keyHost(h){ try{ return (document.getElementById('noport') && document.getElementById('noport').checked) ? stripPort(h) : String(h||'').toLowerCase().replace(/\/+$/,''); }catch(e){ return stripPort(h); } }
const key = a => {
  if(a.type==='stalker') return 'stalker|'+keyHost(a.portal||a.host)+'|'+String(a.mac||a.username||'').toUpperCase();
  if(a.type==='m3u') return 'm3u|'+String(a.m3uurl||a.host||'').toLowerCase().replace(/\/+$/,'');
  return keyHost(a.host)+'|'+(a.username||'')+'|'+(a.password||'');
};
const msg = t => $('#msg').textContent = t || '\u00a0';
function toast(t){ const el=$('#toast'); el.textContent=t; el.classList.add('on'); clearTimeout(toast._t); toast._t=setTimeout(()=>el.classList.remove('on'),1800); }

async function api(path, body){
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-IPTV-Checker':'1'}, body:JSON.stringify(body)});
  if(!r.ok) throw new Error('HTTP '+r.status);
  return r.json();
}

function daysTxt(r){
  if(r.unlimited) return '<span style="color:var(--ok)">غير محدود ∞</span>';
  if(r.days_left == null) return '—';
  const d = r.days_left;
  if(d < 0) return `<span style="color:var(--bad)">انتهى منذ ${-d} يوم</span>`;
  if(d === 0) return '<span style="color:var(--warn)">ينتهي اليوم</span>';
  return `<span style="color:${d<=7?'var(--warn)':'var(--ok)'}">${d} يوم</span>`;
}

let dupInfo = {};        // id -> {count, best}
function computeDup(){
  dupInfo = {};
  const groups = {};
  rows.forEach(r => { const g = keyHost(r.host); (groups[g] = groups[g] || []).push(r); });
  for(const g in groups){
    const list = groups[g];
    if(list.length < 2) continue;
    let best=null, bestScore=-Infinity;
    for(const r of list){
      const s = r.unlimited ? Infinity : (r.days_left!=null ? r.days_left : -Infinity);
      if(s > bestScore){ bestScore = s; best = r; }
    }
    const known = bestScore > -Infinity;
    for(const r of list) dupInfo[r.id] = {count:list.length, best: known && r===best};
  }
}
function rowHtml(r, i){
  const [lbl, cls] = ST[r.state] || ST.error;
  const conn = (r.max_cons!=null || r.active_cons!=null) ? `${r.active_cons ?? '?'} / ${r.max_cons ?? '?'}` : '—';
  const exp = r.exp_date ? esc(r.exp_date) : (r.unlimited ? '∞' : '—');
  const di = dupInfo[r.id];
  const dupBadge = di ? `<span class="dup" title="سيرفر مكرر بمستخدمين مختلفين (${di.count} مرات)">🔁${di.count}</span>${di.best?'<span class="best" title="الأطول صلاحية بين المكرر">⭐</span>':''}` : '';
  const viaBadge = r.viaProxy ? `<span class="viap" title="هذا السيرفر يعمل عبر VPN أمريكي (محجوب بدونه)">🛡️ VPN</span>` : '';
  return `<tr data-id="${r.id}" draggable="true" class="${di&&di.best?'bestrow':''} ${r.viaProxy?'viarow':''}">
    <td class="drag" title="اسحب لإعادة الترتيب">⠿ ${i+1}</td>
    <td><span class="b ${cls}">${lbl}</span></td>
    <td class="ltr host" title="${esc(r.host)}">${viaBadge}${dupBadge}${esc(r.host)}</td>
    <td class="ltr">${esc(r.username)}</td>
    <td class="ltr">${esc(r.password)}</td>
    <td class="ltr">${exp}</td>
    <td>${daysTxt(r)}</td>
    <td class="ltr">${conn}</td>
    <td class="ltr">${r.ping_ms!=null ? r.ping_ms+' ms' : '—'}</td>
    <td class="small note" title="${esc(r.message||'')}">${esc(r.message||'')}</td>
    <td><input type="text" class="namein ltr" data-id="${r.id}" value="${esc(r.title||'')}" placeholder="${esc((r.host||'').replace(/^https?:\/\//,''))}" title="اسم السيرفر عند التصدير إلى iptvnator"></td>
    <td class="acts"><button class="icon" data-act="up" title="نقل للأعلى">▲</button><button class="icon" data-act="down" title="نقل للأسفل">▼</button><button class="icon" data-act="play" title="تشغيل / معاينة">▶️</button><button class="icon" data-act="copy" title="نسخ رابط M3U">📋</button><button class="icon" data-act="re" title="إعادة الفحص">🔁</button><button class="icon" data-act="del" title="حذف">✕</button></td>
  </tr>`;
}

function visible(){
  const q = $('#q').value.trim().toLowerCase();
  let list = rows.filter(r => (filter==='all' || (filter==='viaproxy' ? r.viaProxy : grp(r.state)===filter)) &&
    (!q || (r.host+' '+r.username).toLowerCase().includes(q)));
  const s = $('#sort').value;
  const dv = r => r.unlimited ? 1e9 : (r.days_left ?? -1e9);
  if(s==='daysDesc') list.sort((a,b)=>dv(b)-dv(a));
  else if(s==='daysAsc') list.sort((a,b)=>{const x=a.days_left??1e9, y=b.days_left??1e9; return (a.unlimited?1e9:x)-(b.unlimited?1e9:y);});
  else if(s==='state') list.sort((a,b)=>(ORDER[a.state]??9)-(ORDER[b.state]??9));
  else if(s==='ping') list.sort((a,b)=>(a.ping_ms??1e9)-(b.ping_ms??1e9));
  return list;
}

function render(){
  computeDup();
  const list = visible();
  $('#tb').innerHTML = list.map(rowHtml).join('');
  $('#empty').style.display = rows.length ? (list.length ? 'none' : 'block') : 'block';
  if(rows.length && !list.length) $('#empty').textContent = 'لا توجد نتائج مطابقة للتصفية الحالية.';
  const c = {all:rows.length, ok:0, expired:0, bad:0, offline:0, pending:0, viaproxy:0};
  rows.forEach(r => { c[grp(r.state)]++; if(r.viaProxy) c.viaproxy++; });
  const col = {ok:'var(--ok)',expired:'var(--warn)',bad:'var(--bad)',offline:'var(--off)',pending:'var(--mute)',all:'var(--text)',viaproxy:'#6ee7b7'};
  $('#stats').innerHTML = FILTERS.map(([k,l]) => `<div class="stat ${filter===k?'sel':''}" data-f="${k}"><b style="color:${col[k]}">${c[k]}</b><span class="small">${l}</span></div>`).join('');
}
function scheduleRender(){ if(renderQueued) return; renderQueued = true; requestAnimationFrame(()=>{ renderQueued=false; render(); }); }

function setBtns(){
  $('#btnStop').disabled = !running;
  ['#btnGo','#btnExtract','#btnCheckAll','#btnRecheck','#btnClear','#btnClearText'].forEach(s => $(s).disabled = running);
}

function checkPayload(r, extra){
  let p;
  if(r.type==='stalker') p = {type:'stalker', portal:r.portal||r.host, mac:r.mac||r.username};
  else if(r.type==='m3u') p = {type:'m3u', url:r.m3uurl||r.host};
  else p = {host:r.host, username:r.username, password:r.password};
  return Object.assign(p, extra||{});
}
async function doExtract(){
  const text = $('#inp').value;
  if(!text.trim()){ msg('الصق السيرفرات أولاً.'); return []; }
  let res;
  try { res = await api('/api/extract', {text}); }
  catch(e){ msg('تعذر الاتصال بالأداة — تأكد أن نافذة البرنامج ما زالت مفتوحة.'); return []; }
  const byKey = {}; rows.forEach(r => byKey[key(r)] = r);
  let added = 0; const current = [];
  for(const a of res.accounts){
    const kk = key(a);
    let r = byKey[kk];
    if(!r){ r = {...a, id:++seq, state:'pending'}; rows.push(r); byKey[kk] = r; added++; }
    else if(!hasPort(r.host) && hasPort(a.host)){ r.host = a.host; r.m3u = ''; }   // فضّل النسخة التي فيها منفذ
    current.push(r);                       // سيرفرات النص الحالي (جديدة أو موجودة)
  }
  msg(res.accounts.length ? `تم استخراج ${res.accounts.length} حساب (${added} جديد).` : 'لم يتم العثور على بيانات سيرفرات في النص.');
  render();
  return current;
}

async function runChecks(list){
  if(running || !list.length) return;
  running = true; stop = false; setBtns();
  const conc = Math.max(1, Math.min(50, +$('#conc').value || 8));
  const timeout = Math.max(3, Math.min(120, +$('#tmo').value || 15));
  const count = false;
  let i = 0, done = 0; const total = list.length;
  $('#bar').style.width = '0%';
  msg(`جاري فحص ${total} حساب...`);
  async function worker(){
    while(!stop && i < list.length){
      const r = list[i++];
      r.state = 'checking'; r.message = ''; scheduleRender();
      try { Object.assign(r, await api('/api/check', checkPayload(r, {timeout, count}))); }
      catch(e){ r.state = 'error'; r.message = 'تعذر الاتصال بالأداة: ' + e.message; }
      done++;
      $('#bar').style.width = (done/total*100).toFixed(1) + '%';
      msg(`تم فحص ${done} من ${total}`);
      scheduleRender();
    }
  }
  await Promise.all(Array.from({length:Math.min(conc, total)}, worker));
  const ok = list.filter(r => grp(r.state)==='ok').length;
  msg(stop ? `تم الإيقاف بعد فحص ${done} من ${total}.` : `انتهى الفحص: ${ok} يعمل من أصل ${total}.`);
  render();
  // إعادة تجربة غير العاملة عبر VPN أمريكي (اختياري) — تشمل غير المستجيبة والبيانات الخاطئة/المحظورة
  if(!stop && $('#retryproxy') && $('#retryproxy').checked){
    await retryViaProxy(list.filter(r => grp(r.state)==='offline' || grp(r.state)==='bad'));
  }
  running = false; setBtns();
  render();
}

async function retryViaProxy(failed){
  if(!failed.length) return;
  const manual = ($('#proxy').value || '').trim();
  let proxies = [];
  if(manual) proxies.push(manual);                     // بروكسي المستخدم اليدوي أولاً (الأوثق)
  msg(`جاري تجهيز بروكسيات VPN لإعادة تجربة ${failed.length} سيرفراً (لم يستجب أو بيانات خاطئة)...`);
  try{
    const pr = await api('/api/find_proxies', {country:'us', count:6});
    if(pr.ok) proxies = proxies.concat(pr.proxies);
  }catch(e){}
  if(!proxies.length){
    msg('لم أجد VPN أمريكي يعمل الآن. للحصول على نتيجة موثوقة، أدخل بروكسي/VPN جيداً في خانة البروكسي بالأعلى ثم أعد المحاولة.');
    return;
  }
  const timeout = Math.max(12, Math.min(120, (+$('#tmo').value || 15) + 10));   // مهلة أطول للبروكسي
  const conc = Math.max(1, Math.min(12, +$('#conc').value || 6));
  let i = 0, done = 0, fixed = 0; const total = failed.length;
  $('#bar').style.width = '0%';
  async function w(){
    while(!stop && i < failed.length){
      const r = failed[i++];
      let best = null, usedPx = '';
      for(const px of proxies){                        // جرّب البروكسيات واحداً تلو الآخر حتى ينجح
        if(stop) break;
        r.state = 'checking'; r.message = 'تجربة عبر VPN...'; scheduleRender();
        let res;
        try{ res = await api('/api/check', checkPayload(r, {timeout, proxy:px})); }
        catch(e){ continue; }
        if(grp(res.state)==='ok'){ best = res; usedPx = px; break; }               // عمل → توقف
        if(!best || (grp(res.state)!=='offline' && grp(best.state)==='offline')){ best = res; usedPx = px; }
      }
      if(best){
        Object.assign(r, best);
        if(grp(r.state)==='ok'){ r.viaProxy = true; fixed++; r.message = (r.message?r.message+' — ':'') + 'يعمل عبر VPN'; }
        else r.viaProxy = false;
      }
      done++;
      $('#bar').style.width = (done/total*100).toFixed(1) + '%';
      msg(`إعادة المحاولة عبر VPN: ${done}/${total} (نجح ${fixed})`);
      scheduleRender();
    }
  }
  await Promise.all(Array.from({length:Math.min(conc, total)}, w));
  msg(`انتهت إعادة المحاولة عبر VPN: ${fixed} سيرفراً يعمل من أصل ${total} — جُرّب ${proxies.length} بروكسي. إن كانت النتيجة أقل من المتوقع فاستخدم بروكسي يدوياً موثوقاً.`);
}

async function copyText(t){
  try { await navigator.clipboard.writeText(t); }
  catch(e){ const ta=document.createElement('textarea'); ta.value=t; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
}

function downloadBlob(blob, name){
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name;
  a.click(); setTimeout(()=>URL.revokeObjectURL(a.href), 2000);
}
function stamp(){ return new Date().toISOString().slice(0,16).replace(/[:T]/g,'-'); }
function exportCsvFallback(){
  const head = ['الحالة','السيرفر','المستخدم','كلمة المرور','تاريخ الانتهاء','الأيام المتبقية','الاتصالات النشطة','الحد الأقصى','تاريخ الإنشاء','الاستجابة ms','ملاحظات','رابط M3U'];
  const q = v => '"' + String(v ?? '').replace(/"/g,'""') + '"';
  const lines = [head.map(q).join(',')];
  for(const r of visible()){
    lines.push([ (ST[r.state]||ST.error)[0], r.host, r.username, r.password,
      r.exp_date || (r.unlimited?'غير محدود':''), r.unlimited?'غير محدود':(r.days_left ?? ''),
      r.active_cons, r.max_cons, r.created, r.ping_ms, r.message, r.m3u ].map(q).join(','));
  }
  downloadBlob(new Blob(['\ufeff' + lines.join('\r\n')], {type:'text/csv;charset=utf-8'}), 'iptv_results_' + stamp() + '.csv');
}
async function exportXlsx(){
  const list = visible();
  if(!list.length) return toast('لا توجد نتائج للتصدير');
  toast('جاري تجهيز ملف إكسل...');
  try{
    const res = await fetch('/api/export', {method:'POST', headers:{'Content-Type':'application/json','X-IPTV-Checker':'1'}, body:JSON.stringify({rows:list})});
    if(!res.ok) throw new Error('HTTP '+res.status);
    const blob = await res.blob();
    downloadBlob(blob, 'iptv_results_' + stamp() + '.xlsx');
    toast('تم تصدير ملف إكسل');
  }catch(e){ toast('تعذّر إنشاء إكسل — تم التصدير بصيغة CSV بدلاً منه'); exportCsvFallback(); }
}

const m3uOf = r => (r.type==='m3u') ? (r.m3uurl||r.host) : (r.type==='stalker') ? (r.portal||r.host) : (r.m3u || `${r.host}/get.php?username=${encodeURIComponent(r.username)}&password=${encodeURIComponent(r.password)}&type=m3u_plus&output=ts`);

$('#btnGo').onclick = async () => {
  const current = await doExtract();        // يفحص فقط السيرفرات المكتوبة حالياً في المربع
  if(current.length) runChecks(current);
  else msg('لا توجد سيرفرات في النص لفحصها.');
};
$('#btnCheckAll').onclick = () => {         // يفحص كل السيرفرات في الجدول
  if(!rows.length) return toast('لا توجد سيرفرات في القائمة');
  runChecks(rows.slice());
};
$('#btnExtract').onclick = doExtract;
$('#btnStop').onclick = () => { stop = true; msg('جاري الإيقاف...'); };
$('#btnClearText').onclick = () => { $('#inp').value=''; $('#inp').focus(); msg('تم مسح النص فقط — النتائج في الجدول باقية.'); };
$('#btnClear').onclick = () => { if(rows.length && !confirm('مسح كل النتائج والنص؟')) return; rows=[]; $('#inp').value=''; $('#bar').style.width='0%'; msg(''); render(); };
$('#btnRecheck').onclick = () => runChecks(rows.filter(r => grp(r.state)!=='ok'));
$('#btnCopy').onclick = async () => {
  const ok = visible().filter(r => grp(r.state)==='ok');
  if(!ok.length) return toast('لا توجد سيرفرات شغالة لنسخها');
  await copyText(ok.map(m3uOf).join('\n')); toast(`تم نسخ ${ok.length} رابط`);
};
$('#btnCsv').onclick = () => rows.length ? exportXlsx() : toast('لا توجد نتائج للتصدير');

// ---------------- حفظ/استيراد المشروع ----------------
const PROJ_FIELDS = ['host','username','password','type','portal','mac','isFull','m3uurl','rawM3u','m3uKind','title','userState','portalType','exportedId','autoRefresh','state','status','exp_ts','exp_date','days_left',
  'unlimited','active_cons','max_cons','trial','created','channels','ping_ms','server_tz','message','m3u','viaProxy'];
function exportProject(){
  if(!rows.length) return toast('لا توجد سيرفرات لحفظها');
  const payload = {app:'IPTV_Checker', format:1, exported_at:new Date().toISOString(), count:rows.length,
    servers: rows.map(r => { const o={}; PROJ_FIELDS.forEach(k => o[k]=r[k]); return o; })};
  downloadBlob(new Blob([JSON.stringify(payload, null, 2)], {type:'application/json'}), 'iptv_project_' + stamp() + '.json');
  toast('تم حفظ المشروع (' + rows.length + ' سيرفر)');
}
function importProject(data){
  let arr = Array.isArray(data) ? data : (data && (data.servers || data.rows));
  if(!arr && data && Array.isArray(data.playlists)){        // نسخة iptvnator الاحتياطية
    const pls = data.playlists.slice().sort((a,b) => {       // iptvnator يحفظ الترتيب في position
      const pa = (a&&a.position!=null)?a.position:1e9, pb = (b&&b.position!=null)?b.position:1e9;
      return pa - pb;
    });
    arr = pls.map(pl => {
      const t = (pl&&pl.portalType) || 'xtream';
      const base = {type:t, portalType:t, title:(pl&&pl.title)||'', userState:(pl&&pl.userState)||null,
                    exportedId:(pl&&pl.exportedId)||null, autoRefresh:(pl&&pl.autoRefresh)||false};
      if(t==='stalker'){ const c=(pl&&pl.connection)||{}; return {...base, host:(c.portalUrl||'').trim(), portal:(c.portalUrl||'').trim(), mac:(c.macAddress||'').trim(), username:(c.macAddress||'').trim(), password:'', isFull:!!c.isFullStalkerPortal}; }
      if(t==='m3u'){ const s=(pl&&pl.source)||{}; return {...base, host:(s.url||'').trim(), m3uurl:(s.url||'').trim(), rawM3u:s.rawM3u||'', m3uKind:s.kind||'url', username:'', password:''}; }
      const c=(pl&&pl.connection)||{}; return {...base, host:(c.serverUrl||c.url||'').trim(), username:(c.username||'').trim(), password:(c.password||'').trim()};
    }).filter(x => x.type==='stalker' ? (x.portal&&x.mac) : x.type==='m3u' ? !!x.m3uurl : (x.host&&x.username&&x.password));
  }
  if(!Array.isArray(arr) || !arr.length){ toast('لا توجد سيرفرات في الملف'); return 0; }
  const valid = a => a && (a.type==='stalker' ? (a.portal&&a.mac) : a.type==='m3u' ? !!a.m3uurl : (a.host&&a.username&&a.password));
  const byKey = {}; rows.forEach(r => byKey[key(r)] = r); let added=0;
  for(const a of arr){
    if(!valid(a)) continue;
    const k = key(a);
    const ex = byKey[k];
    if(ex){ if((ex.type||'xtream')==='xtream' && !hasPort(ex.host) && hasPort(a.host)){ ex.host=a.host; ex.m3u=''; } continue; }
    const r = Object.assign({state:'pending'}, a, {id:++seq});
    if(!r.type) r.type='xtream';
    if(!r.m3u && r.type==='xtream') r.m3u = `${a.host}/get.php?username=${encodeURIComponent(a.username)}&password=${encodeURIComponent(a.password)}&type=m3u_plus&output=ts`;
    rows.push(r); byKey[k]=r; added++;
  }
  if(added){ $('#sort').value = 'orig'; filter = 'all'; $('#q').value=''; }   // اعرض بترتيب الاستيراد نفسه
  render();
  msg(added ? `تم استيراد ${added} سيرفر بنفس ترتيب المصدر. (الترتيب على «يدوي» — أي اختيار ترتيب آخر سيعيد الترتيب).` : 'كل السيرفرات في الملف موجودة مسبقاً.');
  toast('تم استيراد ' + added + ' سيرفر');
  return added;
}
function uuid(){
  try{ return crypto.randomUUID(); }catch(e){
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r=Math.random()*16|0, v=c==='x'?r:(r&0x3|0x8); return v.toString(16); });
  }
}
function exportIptvnator(){
  const list = visible();
  if(!list.length) return toast('لا توجد سيرفرات للتصدير');
  const playlists = list.map((r,i) => {
    const t = r.type || r.portalType || 'xtream';
    const common = {
      portalType:t, exportedId:r.exportedId || uuid(),   // يحافظ على معرّف السيرفر الأصلي ليبقى ربط المفضلات سليماً
      title:((r.title&&r.title.trim()) ? r.title.trim() : (r.host||('سيرفر '+(i+1))).replace(/^https?:\/\//,'')),
      autoRefresh:r.autoRefresh!=null?r.autoRefresh:false, position:i,
      userState:r.userState || {hiddenCategories:[], favorites:[], recentlyViewed:[], playbackPositions:[]}   // يحافظ على المفضلات
    };
    if(t==='stalker') return {...common, connection:{portalUrl:r.portal||r.host, macAddress:r.mac||r.username, isFullStalkerPortal:!!r.isFull}};
    if(t==='m3u') return {...common, source:{kind:r.m3uKind||'url', url:r.m3uurl||r.host, rawM3u:r.rawM3u||''}};
    return {...common, connection:{serverUrl:r.host, username:r.username, password:r.password}};
  });
  const payload = {kind:'iptvnator-playlist-backup', version:1, exportedAt:new Date().toISOString(), includeSecrets:true, playlists};
  downloadBlob(new Blob([JSON.stringify(payload, null, 1)], {type:'application/json'}), 'iptvnator-playlist-backup-' + stamp() + '.json');
  toast('تم التصدير إلى iptvnator (' + list.length + ' سيرفر)');
}
$('#btnSave').onclick = exportProject;
$('#btnImport').onclick = () => $('#proj').click();
$('#btnIptv').onclick = () => rows.length ? exportIptvnator() : toast('لا توجد سيرفرات للتصدير');
$('#proj').onchange = async e => {
  const f = e.target.files[0]; e.target.value='';
  if(!f) return;
  let data; try{ data = JSON.parse(await f.text()); }
  catch(err){ return toast('الملف ليس مشروعاً صالحاً (JSON)'); }
  importProject(data);
};
$('#btnFile').onclick = () => $('#file').click();
async function importXlsx(f){
  msg('جاري قراءة ملف الإكسل واستخراج السيرفرات...');
  try{
    const buf = new Uint8Array(await f.arrayBuffer());
    let bin=''; const CH=0x8000;
    for(let i=0;i<buf.length;i+=CH) bin += String.fromCharCode.apply(null, buf.subarray(i, i+CH));
    const res = await api('/api/import_xlsx', {xlsx_b64: btoa(bin)});
    if(res.accounts && res.accounts.length) importProject(res.accounts);
    else { msg('لم أجد سيرفرات في ملف الإكسل.'); toast('لا سيرفرات في الملف'); }
  }catch(e){ msg('تعذّر قراءة ملف الإكسل.'); toast('تعذّر قراءة الإكسل'); }
}
async function loadFiles(list){
  const files = [...list];
  const xlsxs = files.filter(f => /\.xlsx$/i.test(f.name||''));
  for(const f of xlsxs) await importXlsx(f);
  const rest = files.filter(f => !xlsxs.includes(f));
  const imgs = rest.filter(f => (f.type||'').startsWith('image/') || /\.(png|jpe?g|webp|bmp|gif|heic)$/i.test(f.name||''));
  const txts = rest.filter(f => !imgs.includes(f));
  let loadedTxt = false;
  for(const f of txts){
    const t = await f.text();
    if(/\.json$/i.test(f.name||'')){                 // ملف مشروع → استيراد منظّم بالحالات
      try{ const d=JSON.parse(t); if(d && (d.app==='IPTV_Checker' || d.servers || d.playlists || d.kind==='iptvnator-playlist-backup' || Array.isArray(d))){ importProject(d); continue; } }catch(e){}
    }
    $('#inp').value += ($('#inp').value ? '\n' : '') + t; loadedTxt = true;
  }
  if(loadedTxt) toast('تم تحميل الملف');
  if(imgs.length) await ocrFiles(imgs);
}
$('#file').onchange = async e => { const l = [...e.target.files]; e.target.value = ''; await loadFiles(l); };
$('#btnImg').onclick = () => $('#img').click();
$('#img').onchange = async e => { const l = [...e.target.files]; e.target.value = ''; await ocrFiles(l); };

// ---------------- قراءة الصور (OCR) ----------------
let ocrLib = null, ocrBusy = false;
function loadTesseract(){
  if(ocrLib) return ocrLib;
  ocrLib = new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/tesseract.js@5/dist/tesseract.min.js';
    s.onload = () => res(window.Tesseract);
    s.onerror = () => { ocrLib = null; rej(new Error('تعذر تحميل محرك قراءة الصور — تأكد من اتصال الإنترنت')); };
    document.head.appendChild(s);
  });
  return ocrLib;
}
async function prepImage(file){
  const bmp = await createImageBitmap(file);
  const scale = bmp.width < 1600 ? Math.min(3, 1600 / bmp.width) : Math.min(1, 4000 / bmp.width);
  const c = document.createElement('canvas');
  c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale);
  const g = c.getContext('2d', {willReadFrequently:true});
  g.imageSmoothingQuality = 'high'; g.drawImage(bmp, 0, 0, c.width, c.height);
  const im = g.getImageData(0, 0, c.width, c.height), d = im.data; let sum = 0;
  for(let i = 0; i < d.length; i += 4){ const y = 0.299*d[i] + 0.587*d[i+1] + 0.114*d[i+2]; d[i] = d[i+1] = d[i+2] = y; sum += y; }
  if(sum / (d.length/4) < 128) for(let i = 0; i < d.length; i += 4){ d[i] = d[i+1] = d[i+2] = 255 - d[i]; }   // خلفية داكنة → عكس الألوان
  g.putImageData(im, 0, 0);
  return c;
}
function cleanOcr(t){
  // دمج الروابط الطويلة التي انكسرت على سطرين في الصورة
  const src = t.split('\n'), merged = [];
  for(let i = 0; i < src.length; i++){
    let line = src[i];
    while(/https?\s*[:;]/i.test(line) && /[&?=\/_\-.]\s*$/.test(line)){
      let j = i + 1; while(j < src.length && !src[j].trim()) j++;
      if(j < src.length && /^\S+$/.test(src[j].trim()) && !/^https?:/i.test(src[j].trim())){ line = line.trimEnd() + src[j].trim(); i = j; }
      else break;
    }
    merged.push(line);
  }
  return merged.map(line => {
    line = line.replace(/h\s*t\s*t\s*p\s*(s?)\s*[:;]\s*[\/\\|]\s*[\/\\|]\s*/gi, (m, s) => 'http' + s.toLowerCase() + '://');
    const k = line.search(/https?:\/\//i);
    if(k >= 0) line = line.slice(0, k) + line.slice(k).replace(/\s*([\/?&=.:])\s*/g, '$1');
    return line;
  }).join('\n');
}
async function ocrFiles(files){
  if(!files.length) return;
  if(ocrBusy) return toast('جاري قراءة صور أخرى، انتظر قليلاً');
  ocrBusy = true; $('#btnImg').disabled = true;
  let cur = 0;
  try{
    msg('جاري تجهيز قارئ الصور (أول مرة فقط قد يستغرق دقيقة)...');
    const T = await loadTesseract();
    const langs = $('#ara').checked ? 'eng+ara' : 'eng';
    const worker = await T.createWorker(langs, 1, {logger: m => {
      if(m.status === 'recognizing text') msg(`جاري قراءة الصورة ${cur} من ${files.length}: ${Math.round(m.progress*100)}%`);
    }});
    await worker.setParameters({preserve_interword_spaces: '1'});
    let all = '';
    for(const f of files){
      cur++;
      try{
        const { data } = await worker.recognize(await prepImage(f));
        all += `\n# ---- ${f.name || 'صورة ' + cur} ----\n` + cleanOcr(data.text || '');
      }catch(e){ all += `\n# ---- تعذرت قراءة ${f.name || 'الصورة'} ----\n`; }
    }
    await worker.terminate();
    $('#inp').value += ($('#inp').value.trim() ? '\n' : '') + all.trim();
    $('#inp').scrollTop = $('#inp').scrollHeight;
    const before = rows.length;
    await doExtract();
    const n = rows.length - before;
    msg(`تمت قراءة ${files.length} صورة واستخراج ${n} حساب جديد. راجع النص في المربع قبل الفحص — القراءة الآلية قد تخلط بين حروف متشابهة مثل O و 0 أو l و I.`);
  }catch(e){ msg(e.message || String(e)); }
  finally{ ocrBusy = false; $('#btnImg').disabled = false; }
}
document.addEventListener('paste', e => {
  const imgs = [...(e.clipboardData?.items || [])].filter(i => i.type.startsWith('image/')).map(i => i.getAsFile()).filter(Boolean);
  if(imgs.length){ e.preventDefault(); ocrFiles(imgs); }
});
let dragN = 0;
document.addEventListener('dragenter', e => { if([...e.dataTransfer.types].includes('Files')){ dragN++; document.body.classList.add('drag'); } });
document.addEventListener('dragleave', () => { if(--dragN <= 0){ dragN = 0; document.body.classList.remove('drag'); } });
document.addEventListener('dragover', e => e.preventDefault());
document.addEventListener('drop', e => { e.preventDefault(); dragN = 0; document.body.classList.remove('drag'); if(e.dataTransfer.files.length) loadFiles(e.dataTransfer.files); });
$('#sort').onchange = render;
$('#q').oninput = scheduleRender;
$('#stats').onclick = e => { const s = e.target.closest('.stat'); if(s){ filter = s.dataset.f; render(); } };

// ---------------- البروكسي ----------------
try{ $('#proxy').value = window.__PROXY__ || ''; }catch(e){}
if(window.__PROXY__) $('#proxyMsg').textContent = 'البروكسي مُفعّل عند بدء التشغيل.';
async function setProxy(url){
  $('#btnProxy').disabled = true;
  $('#proxyMsg').textContent = url ? 'جاري تطبيق البروكسي واختباره...' : 'جاري إيقاف البروكسي...';
  try{
    const r = await api('/api/proxy', {url});
    if(r.ok){
      if(!url) $('#proxyMsg').textContent = '✅ تم إيقاف البروكسي — الاتصال مباشر.';
      else $('#proxyMsg').textContent = '✅ البروكسي يعمل' + (r.ip ? ' — عنوانك الظاهر: ' + r.ip : ' (مُفعّل، لكن تعذّر جلب العنوان للتأكد)');
    } else $('#proxyMsg').textContent = '⚠️ ' + (r.reason || 'تعذّر تطبيق البروكسي');
  }catch(e){ $('#proxyMsg').textContent = 'تعذّر الاتصال بالأداة.'; }
  finally{ $('#btnProxy').disabled = false; }
}
$('#btnProxy').onclick = () => setProxy($('#proxy').value.trim());
$('#btnProxyOff').onclick = () => { $('#proxy').value=''; setProxy(''); };
$('#btnAuto').onclick = async () => {
  $('#btnAuto').disabled = true; $('#btnProxy').disabled = true;
  $('#proxyMsg').textContent = '🔎 جاري البحث عن بروكسي يعمل واختباره... قد يستغرق حتى دقيقة.';
  try{
    const r = await api('/api/autoproxy', {country: $('#proxyCC').value});
    if(r.ok){
      $('#proxy').value = r.proxy;
      $('#proxyMsg').textContent = `✅ تم العثور على بروكسي يعمل: ${r.proxy}` +
        (r.country ? ` — الدولة: ${r.country}` : '') + (r.ip ? ` — عنوانك الظاهر: ${r.ip}` : '');
    } else $('#proxyMsg').textContent = '⚠️ ' + (r.reason || 'لم أعثر على بروكسي');
  }catch(e){ $('#proxyMsg').textContent = 'تعذّر الاتصال بالأداة.'; }
  finally{ $('#btnAuto').disabled = false; $('#btnProxy').disabled = false; }
};

// ---------------- المشغّل والمعاينة ----------------
function loadScript(src){ return new Promise((res,rej)=>{ const s=document.createElement('script'); s.src=src; s.onload=res; s.onerror=()=>rej(new Error('تعذر تحميل مشغّل الفيديو — تأكد من الإنترنت')); document.head.appendChild(s); }); }
let mpegtsP=null, hlsP=null;
const ensureMpegts = () => mpegtsP || (mpegtsP = loadScript('https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.js'));
const ensureHls = () => hlsP || (hlsP = loadScript('https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js'));
const KPATH = {live:'live', vod:'movie', series:'series'};
const KNAME = {live:'قناة', vod:'فيلم', series:'حلقة'};
let pv = {r:null, kind:'live', view:'list', curSeries:null, episodes:[],
          data:{live:{cats:{},items:[],loaded:false}, vod:{cats:{},items:[],loaded:false}, series:{cats:{},items:[],loaded:false}},
          player:null, hls:null, cur:null};
const streamUrlExt = (r,it,ext) => '/stream?' + new URLSearchParams({host:r.host,username:r.username,password:r.password,id:it.id,ext:ext,kind:pv.kind}).toString();
const streamUrl = (r,it) => streamUrlExt(r,it,it.ext||'ts');
const directUrl = (r,it) => `${r.host}/${KPATH[pv.kind]}/${encodeURIComponent(r.username)}/${encodeURIComponent(r.password)}/${it.id}.${it.ext||'ts'}`;
async function probeStream(u){
  try{
    const ctrl = new AbortController();
    const resp = await fetch(u, {signal:ctrl.signal});
    const s = resp.status;
    try{ ctrl.abort(); }catch(e){}
    return s;
  }catch(e){ return -1; }
}
function diagMsg(st){
  let why;
  if(st===401 || st===403 || st===455) why='رفض السيرفر البث (رمز '+st+') — غالباً الحساب محظور أو عدد الاتصالات ممتلئ.';
  else if(st===404 || st===512 || st===400) why='هذه القناة/المحتوى غير متوفر على السيرفر حالياً (رمز '+st+').';
  else if(st===-1 || st===502 || st===0) why='تعذّر الوصول إلى السيرفر — قد يكون مغلقاً أو بطيئاً أو محجوباً (جرّب تفعيل بروكسي).';
  else why='تعذّر بدء البث (رمز '+st+').';
  return why + ' جرّب «فتح خارجياً» لتشغيله في VLC.';
}

function pvStop(){
  const v=$('#pvVideo');
  if(pv.player){ try{pv.player.destroy();}catch(e){} pv.player=null; }
  if(pv.hls){ try{pv.hls.destroy();}catch(e){} pv.hls=null; }
  try{ v.pause(); v.removeAttribute('src'); v.load(); }catch(e){}
}
function pvClose(){ pvStop(); $('#ov').classList.remove('on'); pv.r=null; pv.cur=null; }

function tabLabel(k){
  const d=pv.data[k], base={live:'📺 بث مباشر',vod:'🎬 أفلام',series:'📂 مسلسلات'}[k];
  const n = d.loaded ? ` (${d.items.length})` : '';
  $('#pvTabs').querySelector(`[data-k="${k}"]`).textContent = base + n;
}
async function openPlayer(r){
  pv={r, kind:'live', view:'list', curSeries:null, episodes:[],
      data:{live:{cats:{},items:[],loaded:false}, vod:{cats:{},items:[],loaded:false}, series:{cats:{},items:[],loaded:false}},
      player:null, hls:null, cur:null};
  $('#pvHost').textContent = r.host + ' — ' + r.username;
  $('#pvBack').style.display='none'; $('#pvQ').value=''; $('#pvChs').innerHTML='';
  ['live','vod','series'].forEach(tabLabel);
  $('#pvMsg').textContent='جاري جلب البث المباشر...';
  $('#ov').classList.add('on');
  let res;
  try{ res = await api('/api/content', {host:r.host, username:r.username, password:r.password}); }
  catch(e){ $('#pvMsg').textContent='تعذر جلب المحتوى من السيرفر.'; return; }
  if(!pv.r) return;
  pv.data.live   = {cats:(res.live||{}).cats||{},   items:(res.live||{}).items||[], loaded:true};
  pv.data.vod    = {cats:(res.vod||{}).cats||{},     items:[], loaded:false};
  pv.data.series = {cats:(res.series||{}).cats||{},  items:[], loaded:false};
  ['live','vod','series'].forEach(tabLabel);
  switchKind('live');
  const nL=pv.data.live.items.length;
  $('#pvMsg').textContent = nL ? `جاهز — ${nL} قناة مباشرة. الأفلام والمسلسلات تُحمَّل عند فتح قسمها.`
    : (res.error ? ('تعذر جلب البث: '+res.error) : 'لا قنوات مباشرة — جرّب قسم الأفلام أو المسلسلات.');
}

async function ensureLoaded(k){
  if(pv.data[k].loaded || k==='live') return;
  $('#pvChs').innerHTML='<div class="small" style="padding:10px">⏳ جاري تحميل القائمة...</div>';
  $('#pvMsg').textContent = (k==='vod'?'جاري تحميل الأفلام':'جاري تحميل المسلسلات') + ' (مرة واحدة فقط)...';
  try{
    const res = await api('/api/streams', {host:pv.r.host, username:pv.r.username, password:pv.r.password, kind:k});
    pv.data[k].items = res.items || []; pv.data[k].loaded = true;
    tabLabel(k);
    $('#pvMsg').textContent = `${pv.data[k].items.length} عنصر — اختر واحداً.`;
  }catch(e){ $('#pvMsg').textContent='تعذّر تحميل هذا القسم.'; }
}

async function switchKind(k){
  pv.kind=k; pv.view='list'; pv.curSeries=null; pv.cur=null;
  $('#pvBack').style.display='none';
  [...$('#pvTabs').children].forEach(t=>t.classList.toggle('sel', t.dataset.k===k));
  const cats=pv.data[k].cats||{};
  $('#pvCat').style.display='';
  $('#pvCat').innerHTML = '<option value="">كل التصنيفات</option>' +
    Object.entries(cats).map(([id,n])=>`<option value="${esc(id)}">${esc(n||id)}</option>`).join('');
  $('#pvQ').value=''; renderList();
  await ensureLoaded(k);
  if(pv.kind===k) renderList();
}

function curItems(){ return pv.view==='episodes' ? pv.episodes : pv.data[pv.kind].items; }
function renderList(){
  const q=$('#pvQ').value.trim().toLowerCase(), cat=$('#pvCat').value;
  let items = curItems();
  if(pv.view==='list') items = items.filter(c=>(!cat || String(c.cat)===cat));
  items = items.filter(c=>(!q || (c.name||'').toLowerCase().includes(q))).slice(0,800);
  const seriesList = pv.kind==='series' && pv.view==='list';
  $('#pvChs').innerHTML = items.map(c=>{
    const sel = pv.cur && String(pv.cur.id)===String(c.id) && pv.view!=='list' ? 'sel' : '';
    return `<div class="ch ${sel}" data-id="${esc(c.id)}">${seriesList?'📂 ':''}${esc(c.name)}</div>`;
  }).join('') || '<div class="small" style="padding:10px">لا يوجد شيء مطابق.</div>';
}

async function onItemClick(id){
  if(pv.kind==='series' && pv.view==='list'){
    const sr = pv.data.series.items.find(x=>String(x.id)===String(id)); if(!sr) return;
    pv.curSeries=sr; $('#pvMsg').textContent='جاري جلب حلقات: '+sr.name;
    let res; try{ res = await api('/api/series', {host:pv.r.host, username:pv.r.username, password:pv.r.password, series_id:sr.id}); }
    catch(e){ $('#pvMsg').textContent='تعذر جلب الحلقات.'; return; }
    pv.episodes = res.episodes || []; pv.view='episodes';
    $('#pvBack').style.display=''; $('#pvCat').style.display='none'; $('#pvQ').value='';
    $('#pvMsg').textContent = pv.episodes.length ? `${sr.name} — ${pv.episodes.length} حلقة، اختر واحدة.` : 'لا توجد حلقات لهذا المسلسل.';
    renderList(); return;
  }
  const it = curItems().find(x=>String(x.id)===String(id)); if(it) playItem(it);
}

async function playTs(v, url, live){
  await ensureMpegts();
  if(window.mpegts && mpegts.isSupported()){
    pv.player = mpegts.createPlayer({type:'mpegts', isLive:live, url}, {
      enableWorker:false, enableStashBuffer:true, stashInitialSize:384,
      liveBufferLatencyChasing:false, autoCleanupSourceBuffer:true   // إيقاف الملاحقة يمنع التسرّع
    });
    pv.player.attachMediaElement(v); pv.player.load();
    pv.player.on(mpegts.Events.ERROR, (a,b,info) => {
      $('#pvMsg').textContent='تعذّر تشغيل هذه القناة داخل الأداة'+((info&&info.msg)?(' ('+info.msg+')'):'')+' — جرّب «فتح خارجياً».';
    });
    try{ v.playbackRate = 1.0; await v.play(); }catch(e){}
    return true;
  }
  return false;
}
async function playHls(v, url){
  await ensureHls();
  if(window.Hls && Hls.isSupported()){ pv.hls=new Hls(); pv.hls.loadSource(url); pv.hls.attachMedia(v); }
  else { v.src=url; }
  try{ await v.play(); }catch(e){}
}
async function playItem(it){
  if(!pv.r) return;
  pv.cur=it; renderList(); pvStop();
  const v=$('#pvVideo');
  const live = pv.kind==='live';
  const exts = [];
  if(it.ext) exts.push(it.ext.toLowerCase());
  if(live){ ['ts','m3u8'].forEach(e=>{ if(!exts.includes(e)) exts.push(e); }); }
  else { ['mp4','mkv','m3u8','ts'].forEach(e=>{ if(!exts.includes(e)) exts.push(e); }); }
  $('#pvMsg').textContent = 'جاري التحقق من البث: ' + (it.name||'');
  let chosen=null, lastSt=0;
  for(const e of exts){
    const st = await probeStream(streamUrlExt(pv.r, it, e));
    if(!pv.r || String((pv.cur||{}).id)!==String(it.id)) return;   // اختار المستخدم عنصراً آخر
    if(st===200 || st===206){ chosen=e; break; }
    lastSt = st;
  }
  if(!chosen){ $('#pvMsg').textContent = diagMsg(lastSt); return; }
  const url = streamUrlExt(pv.r, it, chosen);
  $('#pvMsg').textContent = 'جاري التشغيل: ' + (it.name||'');
  try{
    if(chosen==='m3u8'){ await playHls(v, url); }
    else if(chosen==='ts'){ const ok=await playTs(v, url, live); if(!ok){ v.src=url; try{await v.play();}catch(e){} } }
    else {
      // أفلام/حلقات: تشغيل مباشر — يعتمد على دعم المتصفح للترميز
      let errored=false;
      v.src=url;
      v.onerror = () => { errored=true; $('#pvMsg').textContent='المتصفح لا يدعم ترميز هذا الملف (غالباً H.265/HEVC أو صوت AC3). استخدم «فتح خارجياً» — VLC يشغّل كل الصيغ.'; };
      v.oncanplay = () => { if(!errored) $('#pvMsg').textContent='يعمل: ' + (it.name||''); };
      try{ await v.play(); }catch(e){}
    }
  }catch(e){ $('#pvMsg').textContent = (e.message||'تعذّر التشغيل') + ' — استخدم «فتح خارجياً».'; }
}

$('#pvTabs').onclick = e => { const t=e.target.closest('.tab'); if(t) switchKind(t.dataset.k); };
$('#pvBack').onclick = () => { pv.view='list'; pv.cur=null; $('#pvBack').style.display='none'; $('#pvCat').style.display=''; $('#pvQ').value=''; renderList(); $('#pvMsg').textContent='اختر مسلسلاً.'; };
$('#pvClose').onclick = pvClose;
$('#ov').onclick = e => { if(e.target===$('#ov')) pvClose(); };
document.addEventListener('keydown', e => { if(e.key==='Escape' && $('#ov').classList.contains('on')) pvClose(); });
$('#pvQ').oninput = renderList;
$('#pvCat').onchange = renderList;
$('#pvChs').onclick = e => { const d=e.target.closest('.ch'); if(d) onItemClick(d.dataset.id); };
$('#pvCopy').onclick = async () => { if(pv.cur&&pv.r){ await copyText(directUrl(pv.r,pv.cur)); toast('تم نسخ الرابط'); } else toast('اختر عنصراً أولاً'); };
$('#pvExt').onclick = async () => {
  if(!(pv.cur&&pv.r)) return toast('اختر عنصراً أولاً');
  $('#pvMsg').textContent='جاري الفتح في مشغّل خارجي...';
  try{
    const res = await api('/api/open', {host:pv.r.host, username:pv.r.username, password:pv.r.password, id:pv.cur.id, ext:pv.cur.ext||'ts', kind:pv.kind});
    if(res.ok){ toast(res.via==='vlc' ? 'يُفتح الآن في VLC' : 'يُفتح الآن في المشغّل الافتراضي'); $('#pvMsg').textContent='تم الإرسال إلى المشغّل الخارجي.'; }
    else { toast('لم يُعثر على مشغّل — انسخ الرابط وافتحه في VLC'); $('#pvMsg').textContent='تعذّر العثور على مشغّل خارجي. ثبّت VLC أو استخدم «نسخ الرابط».'; }
  }catch(e){ toast('تعذّر الفتح الخارجي'); }
};
$('#tb').onclick = async e => {
  const b = e.target.closest('button'); if(!b) return;
  const id = +b.closest('tr').dataset.id; const r = rows.find(x => x.id===id); if(!r) return;
  if(b.dataset.act==='play'){ if((r.type||'xtream')!=='xtream'){ toast('التشغيل داخل الأداة متاح لسيرفرات Xtream فقط'); } else openPlayer(r); }
  else if(b.dataset.act==='copy'){ await copyText(m3uOf(r)); toast('تم نسخ رابط M3U'); }
  else if(b.dataset.act==='re'){ runChecks([r]); }
  else if(b.dataset.act==='del'){ rows = rows.filter(x => x.id!==id); render(); }
  else if(b.dataset.act==='up'){ moveRow(id, -1); }
  else if(b.dataset.act==='down'){ moveRow(id, 1); }
};
function bakeOrder(){                 // يثبّت الترتيب المعروض حالياً كترتيب يدوي
  const vis = visible(); const ids = new Set(vis.map(r => r.id));
  rows = vis.concat(rows.filter(r => !ids.has(r.id)));
  $('#sort').value = 'orig';
}
function moveRow(id, dir){
  bakeOrder();
  const i = rows.findIndex(r => r.id===id), j = i + dir;
  if(i<0 || j<0 || j>=rows.length) return;
  const t = rows[i]; rows[i] = rows[j]; rows[j] = t;
  render();
}
$('#btnReverse').onclick = () => { if(!rows.length) return toast('لا توجد سيرفرات'); bakeOrder(); rows.reverse(); render(); msg('تم عكس الترتيب.'); };

function bestOf(list){
  const sv = r => r.unlimited ? 1e12 : (r.days_left!=null ? r.days_left : -1e12);
  const og = r => grp(r.state)==='ok' ? 0 : 1;
  return list.slice().sort((a,b) => (sv(b)-sv(a)) || (og(a)-og(b)) || ((a.ping_ms??1e9)-(b.ping_ms??1e9)))[0];
}
$('#btnDedup').onclick = () => {
  if(!rows.length) return toast('لا توجد سيرفرات');
  const groups = {};
  rows.forEach(r => { const g = keyHost(r.host); (groups[g] = groups[g] || []).push(r); });
  const keep = new Set(); let removed = 0, unknown = 0;
  for(const g in groups){
    const list = groups[g];
    if(list.length < 2){ list.forEach(r => keep.add(r.id)); continue; }
    if(!list.some(r => r.unlimited || r.days_left!=null)) unknown++;
    keep.add(bestOf(list).id);
    removed += list.length - 1;
  }
  if(!removed) return toast('لا توجد سيرفرات مكررة');
  const warn = unknown ? `\n\nتنبيه: ${unknown} مجموعة غير مفحوصة، فسيُبقى منها الأول. يُفضّل الفحص أولاً.` : '';
  if(!confirm(`سيتم حذف ${removed} سيرفراً مكرراً مع إبقاء الأفضل (الأطول صلاحية) في كل مجموعة.${warn}\n\nهل تتابع؟`)) return;
  rows = rows.filter(r => keep.has(r.id));
  render();
  msg(`تم حذف ${removed} سيرفراً مكرراً، مع إبقاء الأفضل في كل مجموعة.`);
};
$('#tb').addEventListener('input', e => {        // تعديل اسم السيرفر للتصدير إلى iptvnator
  const el = e.target.closest('.namein'); if(!el) return;
  const r = rows.find(x => x.id === +el.dataset.id); if(r) r.title = el.value;
});

// ---------------- الترتيب اليدوي بالسحب ----------------
let dragId = null;
function manualMode(){ return $('#sort').value==='orig' && filter==='all' && !$('#q').value.trim(); }
$('#tb').addEventListener('dragstart', e => {
  const tr = e.target.closest('tr'); if(!tr) return;
  if(!manualMode()){ e.preventDefault(); toast('للترتيب اليدوي: اختر «يدوي» وأزل البحث والتصفية'); return; }
  if(e.target.closest('input,button,select,a,textarea')){ e.preventDefault(); return; }  // لا تسحب أثناء الكتابة/الأزرار
  dragId = +tr.dataset.id; tr.classList.add('dragging');
  try{ e.dataTransfer.effectAllowed='move'; e.dataTransfer.setData('text/plain', String(dragId)); }catch(x){}
});
$('#tb').addEventListener('dragover', e => {
  if(dragId==null) return;
  e.preventDefault();
  const tr = e.target.closest('tr'); const dragging = $('#tb').querySelector('tr.dragging');
  if(!tr || !dragging || tr===dragging) return;
  const rect = tr.getBoundingClientRect();
  const after = (e.clientY - rect.top) / rect.height > 0.5;
  tr.parentNode.insertBefore(dragging, after ? tr.nextSibling : tr);
});
$('#tb').addEventListener('drop', e => { e.preventDefault(); });
$('#tb').addEventListener('dragend', () => {
  const dragging = $('#tb').querySelector('tr.dragging');
  if(dragging) dragging.classList.remove('dragging');
  if(dragId==null) return;
  dragId = null;
  const order = [...$('#tb').querySelectorAll('tr')].map(tr => +tr.dataset.id);
  const pos = {}; order.forEach((id,i) => pos[id]=i);
  rows.sort((a,b) => (pos[a.id]??0) - (pos[b.id]??0));   // يعيد ترتيب المصفوفة حسب الترتيب الجديد
  render();
  msg('تم إعادة الترتيب يدوياً.');
});
render();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "IPTVChecker/" + VERSION

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        b = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            page = HTML.replace('__VER__', VERSION).replace('__PROXYJSON__', json.dumps(_PROXY.get('url', '')))
            self._send(200, page, 'text/html; charset=utf-8')
        elif self.path.startswith('/stream?'):
            self._proxy_stream()
        else:
            self._send(404, 'not found', 'text/plain; charset=utf-8')

    def _proxy_stream(self):
        q = _query(urllib.parse.urlsplit(self.path).query)
        host = (q.get('host') or '').rstrip('/')
        user = q.get('username') or q.get('user') or ''
        pw = q.get('password') or q.get('pass') or ''
        sid = q.get('id') or ''
        ext = (q.get('ext') or 'ts').lstrip('.')
        kind = q.get('kind') or 'live'
        seg = {'live': 'live', 'vod': 'movie', 'movie': 'movie', 'series': 'series'}.get(kind, 'live')
        if not (host and user and pw and sid):
            return self._send(400, 'missing', 'text/plain; charset=utf-8')
        url = f"{host}/{seg}/{urllib.parse.quote(user)}/{urllib.parse.quote(pw)}/{sid}.{ext}"
        rng = self.headers.get('Range')
        up, err = None, None
        for ua in (UA, 'Mozilla/5.0', ''):
            headers = {'Accept': '*/*', 'Connection': 'close'}
            if ua:
                headers['User-Agent'] = ua
            if rng:
                headers['Range'] = rng
            try:
                up = _OPENER.open(urllib.request.Request(url, headers=headers), timeout=20)
                break
            except urllib.error.HTTPError as e:
                err = e
                if e.code in (401, 403, 455):   # قد يكون السبب حظر الـ User-Agent، جرّب غيره
                    continue
                break
            except Exception as e:
                err = e
                break
        if up is None:
            code = getattr(err, 'code', 502) if isinstance(err, urllib.error.HTTPError) else 502
            try:
                self.send_response(code)
                self.end_headers()
            except Exception:
                pass
            return
        try:
            try:
                import socket as _s
                self.connection.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)   # تقليل زمن التأخير
            except Exception:
                pass
            self.send_response(getattr(up, 'status', 200) or 200)
            for h in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
                v = up.headers.get(h)
                if v:
                    self.send_header(h, v)
            if not up.headers.get('Content-Type'):
                self.send_header('Content-Type', 'video/mp2t')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            while True:
                chunk = up.read(262144)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass
        finally:
            try:
                up.close()
            except Exception:
                pass

    def do_POST(self):
        # حماية: يقبل الطلبات من واجهة الأداة فقط
        if self.headers.get('X-IPTV-Checker') != '1':
            return self._send(403, 'forbidden', 'text/plain; charset=utf-8')
        try:
            n = int(self.headers.get('Content-Length') or 0)
            data = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            return self._send(400, 'bad json', 'text/plain; charset=utf-8')
        if self.path == '/api/extract':
            out = {'accounts': extract(data.get('text', ''))}
        elif self.path == '/api/import_xlsx':
            try:
                import base64
                raw = base64.b64decode(data.get('xlsx_b64', '') or '')
                out = {'accounts': parse_xlsx(raw)}
            except Exception as e:
                return self._send(400, 'xlsx parse error: ' + str(e)[:120], 'text/plain; charset=utf-8')
        elif self.path == '/api/content':
            try:
                out = get_content(str(data['host']), str(data['username']), str(data['password']))
            except KeyError:
                return self._send(400, 'missing fields', 'text/plain; charset=utf-8')
        elif self.path == '/api/streams':
            try:
                out = get_streams(str(data['host']), str(data['username']),
                                  str(data['password']), str(data.get('kind', 'live')))
            except KeyError:
                return self._send(400, 'missing fields', 'text/plain; charset=utf-8')
        elif self.path == '/api/series':
            try:
                out = get_series_info(str(data['host']), str(data['username']),
                                      str(data['password']), data.get('series_id'))
            except KeyError:
                return self._send(400, 'missing fields', 'text/plain; charset=utf-8')
        elif self.path == '/api/proxy':
            url = str(data.get('url', '')).strip()
            try:
                apply_proxy(url)
                ip = test_proxy() if url else None
                save_cfg({'proxy': url})
                out = {'ok': True, 'ip': ip, 'proxy': url}
            except Exception as e:
                out = {'ok': False, 'reason': str(e)[:200]}
        elif self.path == '/api/find_proxy':
            out = find_working_proxy(str(data.get('country', 'us')), apply=False)
        elif self.path == '/api/find_proxies':
            try:
                n = max(1, min(10, int(data.get('count', 5))))
            except (TypeError, ValueError):
                n = 5
            out = find_proxies(str(data.get('country', 'us')), count=n)
        elif self.path == '/api/export':
            try:
                data_rows = data.get('rows') or []
                blob = build_xlsx(data_rows)
            except Exception as e:
                return self._send(500, 'export error: ' + str(e)[:120], 'text/plain; charset=utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            self.send_header('Content-Disposition', 'attachment; filename="iptv_results.xlsx"')
            self.send_header('Content-Length', str(len(blob)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(blob)
            except Exception:
                pass
            return
        elif self.path == '/api/autoproxy':
            out = find_working_proxy(str(data.get('country', 'any')))
        elif self.path == '/api/open':
            try:
                port = self.server.server_address[1]
                qp = urllib.parse.urlencode({'host': str(data['host']), 'username': str(data['username']),
                                             'password': str(data['password']), 'id': str(data['id']),
                                             'ext': str(data.get('ext', 'ts')), 'kind': str(data.get('kind', 'live'))})
                local = f"http://127.0.0.1:{port}/stream?{qp}"
                out = {'ok': True, 'via': launch_external(local)}
            except KeyError:
                return self._send(400, 'missing fields', 'text/plain; charset=utf-8')
            except Exception as e:
                out = {'ok': False, 'reason': str(e)[:160]}
        elif self.path == '/api/check':
            try:
                t = max(3, min(120, int(data.get('timeout') or DEFAULT_TIMEOUT)))
                proxy = str(data.get('proxy', ''))
                typ = str(data.get('type', 'xtream'))
                if typ == 'stalker':
                    out = check_stalker(str(data['portal']), str(data['mac']), timeout=t, proxy=proxy)
                elif typ == 'm3u':
                    out = check_m3u(str(data['url']), timeout=t, proxy=proxy)
                else:
                    out = check(str(data['host']), str(data['username']), str(data['password']),
                                timeout=t, count_channels=bool(data.get('count')), proxy=proxy)
            except KeyError:
                return self._send(400, 'missing fields', 'text/plain; charset=utf-8')
        else:
            return self._send(404, 'not found', 'text/plain; charset=utf-8')
        self._send(200, json.dumps(out, ensure_ascii=False), 'application/json; charset=utf-8')


def run_cli(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        accs = extract(f.read())
    print(f"Found {len(accs)} account(s)\n")
    with ThreadPoolExecutor(10) as ex:
        for r in ex.map(lambda a: check(a['host'], a['username'], a['password']), accs):
            exp = r['exp_date'] or ('unlimited' if r['unlimited'] else '-')
            cons = f"{r['active_cons']}/{r['max_cons']}" if r['max_cons'] is not None else '-'
            days = 'inf' if r['unlimited'] else ('-' if r['days_left'] is None else r['days_left'])
            print(f"{r['state']:<11} {r['host']:<38} {r['username']:<18} exp={exp:<17} days={days!s:<6} cons={cons}")


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    try:
        pxy = load_cfg().get('proxy', '')
        if pxy:
            apply_proxy(pxy)
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    if args:
        return run_cli(args[0])
    srv = None
    for port in range(8765, 8800):
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        print("Could not find a free port.")
        return
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print("=" * 52)
    print(f"  IPTV Checker v{VERSION} is running")
    print(f"  Open: {url}")
    print("  Keep this window open. Press Ctrl+C to stop.")
    print("=" * 52)

    # شغّل الخادم في خيط خلفي ليبقى واجهة المستخدم مسيطرة
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    if '--no-browser' in sys.argv:                      # وضع بلا واجهة
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    # نافذة أصلية بوضع «التطبيق» من كروم/إيدج (محرك Chromium) — إغلاقها يُنهي البرنامج
    if _open_chrome_app(url):
        return

    # بديل احتياطي: المتصفح الافتراضي
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nStopped.")


def _find_chromium():
    """يبحث عن متصفح مبني على Chromium: كروم ثم إيدج ثم Brave ثم Chromium."""
    import shutil
    cands = []
    if sys.platform == 'darwin':
        cands = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                 '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
                 '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
                 '/Applications/Chromium.app/Contents/MacOS/Chromium']
    elif sys.platform.startswith('win'):
        pf = os.environ.get('ProgramFiles', r'C:\Program Files')
        pf86 = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
        la = os.environ.get('LOCALAPPDATA', '')
        cands = [pf + r'\Google\Chrome\Application\chrome.exe',
                 pf86 + r'\Google\Chrome\Application\chrome.exe',
                 (la + r'\Google\Chrome\Application\chrome.exe') if la else '',
                 pf + r'\Microsoft\Edge\Application\msedge.exe',
                 pf86 + r'\Microsoft\Edge\Application\msedge.exe',
                 pf + r'\BraveSoftware\Brave-Browser\Application\brave.exe']
    else:
        for n in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser',
                  'brave-browser', 'microsoft-edge'):
            p = shutil.which(n)
            if p:
                cands.append(p)
    for c in cands:
        if c and os.path.exists(c):
            return c
    for n in ('google-chrome', 'chromium', 'chrome', 'msedge', 'brave'):
        p = shutil.which(n)
        if p:
            return p
    return None


def _open_chrome_app(url):
    """يفتح النافذة بوضع التطبيق (--app) ويبقى حتى تُغلق النافذة."""
    chrome = _find_chromium()
    if not chrome:
        return False
    import tempfile
    import subprocess
    profile = os.path.join(tempfile.gettempdir(), 'iptv_checker_profile')
    args = [chrome, f'--app={url}', f'--user-data-dir={profile}',
            '--window-size=1360,880', '--no-first-run', '--no-default-browser-check',
            '--disable-features=Translate']
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait()                                     # ينتظر حتى تُغلق النافذة
        return True
    except Exception:
        return False


if __name__ == '__main__':
    main()
