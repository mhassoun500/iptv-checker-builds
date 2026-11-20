#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mo IPTV Checker - فاحص سيرفرات IPTV
-----------------------------------
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
import base64
import atexit
import random
import secrets
import hashlib
import shutil
import subprocess
import tempfile
import threading
import webbrowser
from collections import deque
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

VERSION = "6.8"
UA = "VLC/3.0.20 LibVLC/3.0.20"
DEFAULT_TIMEOUT = 15

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE
_PROXY = {'url': ''}


def _data_dir():
    """مجلد بيانات قابل للكتابة دائماً (إعدادات، جلسة، سجل، مكتبات المشغّل)."""
    env = os.environ.get('MOIPTV_DATA')
    try:
        if env:
            d = env
        elif sys.platform.startswith('win'):
            d = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), 'Mo IPTV Checker')
        elif sys.platform == 'darwin':
            d = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'Mo IPTV Checker')
        else:
            d = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config'),
                             'mo-iptv-checker')
        os.makedirs(d, exist_ok=True)
        t = os.path.join(d, '.w')
        with open(t, 'w') as f:
            f.write('1')
        os.remove(t)
        return d
    except Exception:
        d = os.path.join(tempfile.gettempdir(), 'mo-iptv-checker')
        os.makedirs(d, exist_ok=True)
        return d


_DATA_DIR = _data_dir()
_CFG_PATH = os.path.join(_DATA_DIR, 'iptv_checker_config.json')
_SESSION_PATH = os.path.join(_DATA_DIR, 'session.json')
_HIST_PATH = os.path.join(_DATA_DIR, 'history.json')
_LIB_DIR = os.path.join(_DATA_DIR, 'lib')
try:                                                   # ترحيل ملف الإعدادات القديم (كان بجانب الملف التنفيذي)
    _old_base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
    _old_cfg = os.path.join(_old_base, 'iptv_checker_config.json')
    if os.path.exists(_old_cfg) and not os.path.exists(_CFG_PATH) and os.path.abspath(_old_cfg) != os.path.abspath(_CFG_PATH):
        shutil.copyfile(_old_cfg, _CFG_PATH)
except Exception:
    pass
TOKEN = secrets.token_urlsafe(18)                      # رمز جلسة عشوائي يحمي الوسيط المحلي


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


_FILE_LOCK = threading.Lock()


def _write_json(path, obj):
    try:
        tmp = path + '.tmp'
        with _FILE_LOCK:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, path)
        return True
    except Exception:
        return False


def load_cfg():
    try:
        with open(_CFG_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(d):
    cur = load_cfg()
    cur.update(d)
    _write_json(_CFG_PATH, cur)


DEFAULT_SETTINGS = {'per_domain': 2, 'ffmpeg_path': '', 'ext_player': 'auto', 'ext_player_path': '',
                    'ext_direct': False, 'updates': True, 'auto_hours': 0, 'deep_default': False,
                    'count_default': False, 'timeout': 15, 'conc': 8, 'adult_scan': True, 'ua_auto': True, 'window_engine': 'auto'}


def get_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(load_cfg().get('settings') or {})
    except Exception:
        pass
    return s


def set_settings(d):
    cur = get_settings()
    for k, v in (d or {}).items():
        if k in DEFAULT_SETTINGS:
            cur[k] = v
    save_cfg({'settings': cur})
    return cur


# ---- سجل التشخيص (بدون كلمات مرور) ----
_LOG = deque(maxlen=500)


def _redact(t):
    t = re.sub(r'(password|pass|pwd)=[^&\s]+', r'\1=***', str(t), flags=re.I)
    return re.sub(r'/(live|movie|series)/([^/\s]+)/([^/\s]+)/', r'/\1/\2/***/', t)


def log_event(kind, msg):
    _LOG.append({'t': time.strftime('%H:%M:%S'), 'k': kind, 'm': _redact(msg)[:300]})


# ---- سجل تاريخي لنتائج الفحص ----
_HIST = None
_HIST_DIRTY = False
_HIST_LOCK = threading.Lock()


def _hist_load():
    global _HIST
    if _HIST is None:
        try:
            with open(_HIST_PATH, encoding='utf-8') as f:
                _HIST = json.load(f)
        except Exception:
            _HIST = {}
    return _HIST


def hist_record(key, out):
    global _HIST_DIRTY
    e = {'t': int(time.time()), 's': out.get('state'), 'e': out.get('exp_ts'), 'ms': out.get('ping_ms')}
    with _HIST_LOCK:
        h = _hist_load()
        lst = h.setdefault(key, [])
        prev = lst[-1] if lst else None
        lst.append(e)
        del lst[:-40]
        while len(h) > 4000:
            h.pop(next(iter(h)))
        _HIST_DIRTY = True
        return prev, list(lst[-10:])


def hist_flush():
    global _HIST_DIRTY
    with _HIST_LOCK:
        if _HIST is not None and _HIST_DIRTY:
            _HIST_DIRTY = False
            _write_json(_HIST_PATH, _HIST)


def _hist_saver():
    while True:
        time.sleep(5)
        try:
            hist_flush()
        except Exception:
            pass


atexit.register(lambda: hist_flush())


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

    clines = [(raw.replace('‏', '').replace('‎', '')
               .replace('﻿', '').replace('‍', '').strip())
              for raw in (text or '').splitlines()]

    def _mac_block_has_pass(i):
        # قاعدة: طالما يوجد باسورد في نفس الكتلة ⇒ السيرفر Xtream وليس Stalker
        # (Stalker لا يستخدم باسورد، بل MAC فقط). نبحث عن الباسورد قبل أو بعد سطر الـ MAC.
        if st['pass']:                                # باسورد مُلتقط قبل الـ MAC
            return True
        m0 = RE_PASS.search(clines[i])                # باسورد في نفس سطر الـ MAC
        if m0:
            return bool(_tok(m0.group(1)))
        for j in range(i + 1, len(clines)):           # ابحث للأمام حتى حدّ الكتلة
            lj = clines[j]
            if not lj:
                continue
            mp = RE_PASS.search(lj)
            if mp:
                return bool(_tok(mp.group(1)))
            if URL_RE.search(lj) or RE_USER.search(lj) or mac_re.search(lj):
                return False                          # بدأت كتلة/سيرفر جديد بلا باسورد
        return False

    for i, line in enumerate(clines):
        if not line or line.startswith('#EXTINF'):
            continue
        if re.search(r'user[\s_-]*agent', line, re.I):
            continue

        # --- Stalker: رابط بوابة + عنوان MAC (بأي شكل: سطر واحد أو سطرين، مع /portal.php أو بدونه) ---
        macs = mac_re.findall(line)
        line_urls = URL_RE.findall(line)
        if line_urls:
            pending_portal['u'] = line_urls[0]        # تذكّر آخر رابط كبوابة محتملة لأي MAC لاحق
        if macs and not _mac_block_has_pass(i):        # MAC بلا باسورد ⇒ Stalker؛ مع باسورد ⇒ يُكمَل كـ Xtream
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
            continue                                   # سطر فيه MAC بلا باسورد لا يُعامل كـ Xtream
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
_UAS = (UA, 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
        'okhttp/3.12.1')


_TL = threading.local()


def _cur_ua():
    """ترويسة User-Agent المخصصة للسيرفر الجاري التعامل معه (إن حدّدها المستخدم)."""
    return getattr(_TL, 'ua', '') or ''


def _get(url, timeout, limit=8_000_000, opener=None, ua=None):
    op = opener or _OPENER
    req = urllib.request.Request(url, headers={'User-Agent': ua or _cur_ua() or UA, 'Accept': '*/*', 'Connection': 'close'})
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


def _http_reason(code):
    if code == 401:
        return 'السيرفر رفض الدخول (401)'
    if code in (403, 451, 884):
        return f'السيرفر رفض الاتصال ({code}) — غالباً محجوب جغرافياً أو محظور، جرّب VPN'
    if code == 404:
        return 'العنوان غير موجود (404) — تأكد من الرابط والمنفذ'
    if code in (502, 503, 504, 520, 521, 522):
        return f'السيرفر متوقف أو مشغول حالياً ({code})'
    return f'HTTP {code}'


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
    fm = ui.get('allowed_output_formats')
    r['formats'] = [str(x) for x in fm] if isinstance(fm, list) else []
    r['https_port'] = si.get('https_port') or ''
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
        r['message'] = (prev + ' / ' if prev else '') + _http_reason(e.code)
    except Exception as e:
        r['state'] = 'error'
        r['message'] = (prev + ' / ' if prev else '') + _net_reason(e)
    return r


def _alt_host(host):
    """يرجع العنوان بالبروتوكول الآخر (http↔https) عندما يكون المنفذ افتراضياً أو غير مذكور."""
    m = re.match(r'^(https?)://([^/:]+)(?::(\d+))?(.*)$', host, re.I)
    if not m:
        return None
    sch, hn, port, rest = m.groups()
    if port in (None, '', '80', '443'):
        return ('https' if sch.lower() == 'http' else 'http') + '://' + hn + (rest or '')
    return None


def _api_fetch(host, qs, timeout, op):
    """يجلب player_api: يجرّب User-Agent بديلاً عند الحظر (403) والبروتوكول الآخر عند فشل الشبكة.
    يرجع (data, ms, host_used) أو يرفع آخر استثناء."""
    last = None
    cands = [host]
    alt = _alt_host(host)
    ci = 0
    retried = False
    while ci < len(cands):
        h = cands[ci]
        ci += 1
        net_err = False
        for ua in (((_cur_ua(),) + _UAS) if _cur_ua() else _UAS):
            try:
                _, data, ms = _get(f"{h}/player_api.php?{qs}", timeout, opener=op, ua=ua)
                return data, ms, h
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (403, 451, 884):          # قد يكون حظر User-Agent — جرّب التالي
                    continue
                raise
            except Exception as e:
                last = e
                net_err = True
                break
        timed_out = net_err and ('timed out' in str(last).lower() or isinstance(last, (socket.timeout, TimeoutError)))
        if timed_out and not retried:                   # إعادة محاولة واحدة تلقائية عند انتهاء المهلة
            retried = True
            ci -= 1
            continue
        if net_err and alt and alt not in cands:
            if (not timed_out) or not re.search(r':\d+', host.split('://', 1)[-1].split('/')[0]):
                cands.append(alt)
    raise last


def check(host, user, pw, timeout=DEFAULT_TIMEOUT, count_channels=False, proxy=''):
    host = host.rstrip('/')
    op = _opener_for(proxy)
    qs = urllib.parse.urlencode({'username': user, 'password': pw})
    r = dict(host=host, base=host, username=user, password=pw, state='error', status='', exp_ts=None,
             exp_date='', days_left=None, unlimited=False, active_cons=None, max_cons=None,
             trial=None, created='', ping_ms=None, channels=None, server_tz='', message='',
             m3u=f"{host}/get.php?{qs}&type=m3u_plus&output=ts")
    try:
        data, ms, used = _api_fetch(host, qs, timeout, op)
        r['ping_ms'] = ms
        if used != host:
            host = used
            r['base'] = used
            r['m3u'] = f"{host}/get.php?{qs}&type=m3u_plus&output=ts"
            r['scheme_note'] = 'يعمل عبر ' + used.split(':', 1)[0].upper()
    except urllib.error.HTTPError as e:
        r['http_code'] = e.code
        return _m3u_fallback(r, timeout, prev=f'player_api: {_http_reason(e.code)}', opener=op)
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
        if r.get('scheme_note') and not r['message']:
            r['message'] = r['scheme_note']
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
    r['http_code'] = 'nojson'
    return _m3u_fallback(r, timeout, opener=op)


def _blank_result(**kw):
    r = dict(host='', username='', password='', state='error', status='', exp_ts=None,
             exp_date='', days_left=None, unlimited=False, active_cons=None, max_cons=None,
             trial=None, created='', ping_ms=None, channels=None, server_tz='', message='',
             m3u='', type='xtream')
    r.update(kw)
    return r


_OP_CACHE = {}


def _opener_for(proxy):
    proxy = (proxy or '').strip()
    if not proxy:
        return _OPENER
    op = _OP_CACHE.get(proxy)
    if op:
        return op
    try:
        op = _build_opener(proxy)
    except Exception:
        return _OPENER
    _OP_CACHE[proxy] = op
    return op


def check_m3u(url, timeout=DEFAULT_TIMEOUT, proxy=''):
    """فحص رابط قائمة M3U مباشر: يعمل إن أرجع قائمة قنوات صالحة."""
    op = _opener_for(proxy)
    url = url.strip()
    r = _blank_result(type='m3u', host=url, m3u=url, rawM3u='')
    try:
        _, data, ms = _get(url, timeout, limit=4_000_000, opener=op)   # اجلب المحتوى لتخزينه
        r['ping_ms'] = ms
        if b'#EXTM3U' in data or b'#EXTINF' in data:
            r['state'] = 'active_m3u'
            r['message'] = 'قائمة M3U تعمل'
            txt = data.decode('utf-8', 'replace')
            if '\n' in txt:
                txt = txt[:txt.rfind('\n')]              # قُصّ عند آخر سطر كامل
            r['rawM3u'] = txt[:3_000_000]                # خزّن محتوى القائمة لتصدير iptvnator سليم
        else:
            r['state'] = 'invalid'
            r['message'] = 'الرابط لا يُرجع قائمة M3U صالحة'
    except urllib.error.HTTPError as e:
        r['state'] = 'invalid' if e.code in (401, 403, 404, 405, 512) else 'error'
        r['message'] = _http_reason(e.code)
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
        h = {'User-Agent': _cur_ua() or _STALKER_UA, 'Accept': '*/*',
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
        r['message'] = _http_reason(e.code)
    except Exception as e:
        r['state'] = 'offline'
        r['message'] = _net_reason(e)
    return r


def _xt(host, qs, action, timeout, limit, opener=None, extra=''):
    _, d, _ = _get(f"{host}/player_api.php?{qs}&action={action}{extra}", timeout, limit=limit, opener=opener)
    return json.loads(d.decode('utf-8', 'replace'))


def _cats(host, qs, action, timeout, opener=None):
    try:
        arr = _xt(host, qs, action, timeout, 20_000_000, opener)
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


def get_streams(host, user, pw, kind, timeout=DEFAULT_TIMEOUT, opener=None):
    """يرجع عناصر نوع واحد (live/vod/series) — للتحميل عند الطلب."""
    host = host.rstrip('/')
    qs = urllib.parse.urlencode({'username': user, 'password': pw})
    action = {'live': 'get_live_streams', 'vod': 'get_vod_streams', 'series': 'get_series'}.get(kind, 'get_live_streams')
    idkey = 'series_id' if kind == 'series' else 'stream_id'
    extdef = 'mp4' if kind == 'vod' else 'ts'
    out = {'items': [], 'error': ''}
    try:
        arr = _xt(host, qs, action, max(timeout, 30), 180_000_000, opener)
        if isinstance(arr, list):
            for c in arr:
                if not isinstance(c, dict) or c.get(idkey) is None:
                    continue
                it = {'id': c[idkey], 'name': str(c.get('name', '') or ('#' + str(c[idkey]))),
                      'cat': str(c.get('category_id', ''))}
                ic = c.get('stream_icon') or c.get('cover') or ''
                if ic:
                    it['icon'] = ic
                if c.get('rating') not in (None, '', 0, '0'):
                    it['rating'] = str(c.get('rating'))[:5]
                if c.get('epg_channel_id'):
                    it['epg'] = c.get('epg_channel_id')
                if kind != 'series':
                    it['ext'] = c.get('container_extension') or extdef
                out['items'].append(it)
    except Exception as e:
        out['error'] = _net_reason(e)
    return out


_LCACHE_TTL = 12 * 3600


def _lcache_path(key):
    import hashlib
    d = os.path.join(_DATA_DIR, 'cache')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, hashlib.sha1(key.encode('utf-8')).hexdigest()[:20] + '.json')


def cached_list(key, fetch, force=False, ok=lambda r: True):
    """يحفظ قوائم السيرفر (القنوات/الأفلام/المسلسلات) على القرص لتفتح فوراً في المرة التالية."""
    path = _lcache_path(key)
    if not force:
        try:
            if time.time() - os.path.getmtime(path) < _LCACHE_TTL:
                with open(path, encoding='utf-8') as f:
                    r = json.load(f)
                r['cached_at'] = int(os.path.getmtime(path))
                return r
        except Exception:
            pass
    r = fetch()
    if ok(r):
        try:
            _write_json(path, r)
        except Exception:
            pass
    return r


def get_content(host, user, pw, timeout=DEFAULT_TIMEOUT, opener=None):
    """فتح سريع: تصنيفات الأنواع الثلاثة + عناصر البث المباشر فقط.
    الأفلام والمسلسلات تُحمَّل عند فتح قسمها عبر get_streams."""
    host = host.rstrip('/')
    qs = urllib.parse.urlencode({'username': user, 'password': pw})
    res = {'live': {'cats': {}, 'items': []}, 'vod': {'cats': {}},
           'series': {'cats': {}}, 'error': ''}
    res['live']['cats'] = _cats(host, qs, 'get_live_categories', timeout, opener)
    res['vod']['cats'] = _cats(host, qs, 'get_vod_categories', timeout, opener)
    res['series']['cats'] = _cats(host, qs, 'get_series_categories', timeout, opener)
    live = get_streams(host, user, pw, 'live', timeout, opener)
    res['live']['items'] = live['items']
    res['error'] = live.get('error', '')
    return res


def get_series_info(host, user, pw, series_id, timeout=DEFAULT_TIMEOUT, opener=None):
    """يرجع حلقات مسلسل معيّن مرتبة حسب المواسم، مع معلومات المسلسل."""
    host = host.rstrip('/')
    qs = urllib.parse.urlencode({'username': user, 'password': pw})
    out = {'episodes': [], 'info': {}, 'error': ''}
    try:
        _, d, _ = _get(f"{host}/player_api.php?{qs}&action=get_series_info"
                       f"&series_id={urllib.parse.quote(str(series_id))}", max(timeout, 30), limit=80_000_000, opener=opener)
        j = json.loads(d.decode('utf-8', 'replace'))
        if isinstance(j, dict) and isinstance(j.get('info'), dict):
            i = j['info']
            out['info'] = {'cover': i.get('cover') or '', 'plot': i.get('plot') or '', 'rating': i.get('rating') or '',
                           'genre': i.get('genre') or '', 'year': str(i.get('releaseDate') or i.get('release_date') or '')[:4]}
        eps = j.get('episodes') if isinstance(j, dict) else None
        if isinstance(eps, dict):
            for season in sorted(eps, key=lambda x: _int(x) if _int(x) is not None else 0):
                for e in eps[season]:
                    if not isinstance(e, dict) or e.get('id') is None:
                        continue
                    ei = e.get('info') if isinstance(e.get('info'), dict) else {}
                    title = e.get('title') or f"S{season}E{e.get('episode_num', '')}"
                    out['episodes'].append({'id': e['id'], 'name': title, 'season': str(season),
                                            'num': e.get('episode_num'), 'plot': ei.get('plot') or '',
                                            'img': ei.get('movie_image') or '', 'dur': ei.get('duration') or '',
                                            'ext': e.get('container_extension') or 'mp4', 'cat': ''})
    except Exception as e:
        out['error'] = _net_reason(e)
    return out


def get_vod_info(host, user, pw, vod_id, timeout=DEFAULT_TIMEOUT, opener=None):
    host = host.rstrip('/')
    qs = urllib.parse.urlencode({'username': user, 'password': pw})
    out = {'info': {}, 'error': ''}
    try:
        j = _xt(host, qs, 'get_vod_info', max(timeout, 20), 8_000_000, opener,
                extra=f"&vod_id={urllib.parse.quote(str(vod_id))}")
        i = (j.get('info') if isinstance(j, dict) else None) or {}
        out['info'] = {'cover': i.get('movie_image') or i.get('cover_big') or '', 'plot': i.get('plot') or i.get('description') or '',
                       'duration': i.get('duration') or '', 'rating': i.get('rating') or '', 'genre': i.get('genre') or '',
                       'year': str(i.get('releasedate') or i.get('release_date') or '')[:4],
                       'cast': i.get('cast') or i.get('actors') or '', 'director': i.get('director') or ''}
        md = (j.get('movie_data') if isinstance(j, dict) else None) or {}
        if isinstance(md, dict) and md.get('container_extension'):
            out['ext'] = str(md.get('container_extension')).lstrip('.').lower()
    except Exception as e:
        out['error'] = _net_reason(e)
    return out


def vod_resolve(d, vod_id, exts, timeout=12, kind='vod'):
    """بعض السيرفرات ترفض الفيلم إن طُلب بامتداد غير امتداده الحقيقي (فتظهر 401/404):
    يجرّب الامتدادات واحداً تلو الآخر بطلب صغير ويرجع أول امتداد يعطي بثاً فعلياً."""
    host = str(d.get('host') or '').rstrip('/')
    op = _opener_for(str(d.get('proxy') or ''))
    tried = []
    seen = set()
    for ext in exts:
        ext = str(ext or '').lstrip('.').lower()
        if not ext or ext in seen:
            continue
        seen.add(ext)
        url = _direct_url(host, d.get('username', ''), d.get('password', ''), vod_id, ext, 'series' if kind == 'series' else 'vod')
        row = {'ext': ext, 'status': None, 'ok': False}
        try:
            req = urllib.request.Request(url, headers={'User-Agent': _cur_ua() or UA, 'Range': 'bytes=0-8191',
                                                       'Accept': '*/*', 'Connection': 'close'})
            with op.open(req, timeout=timeout) as r:
                row['status'] = r.status
                ctype = (r.headers.get('Content-Type') or '').lower()
                head = r.read(4096)
                row['ok'] = r.status in (200, 206) and 'text/html' not in ctype and len(head) > 0 \
                    and not head.lstrip().startswith((b'<', b'{'))
        except urllib.error.HTTPError as e:
            row['status'] = e.code
        except Exception as e:
            row['status'] = _net_reason(e)[:60]
        tried.append(row)
        if row['ok']:
            return {'ext': ext, 'tried': tried}
        time.sleep(0.6)                                    # لا تفتح اتصالين معاً
    return {'ext': '', 'tried': tried}


def get_epg(host, user, pw, stream_id, timeout=DEFAULT_TIMEOUT, opener=None):
    """البرنامج الحالي والتالي لقناة (get_short_epg)."""
    host = host.rstrip('/')
    qs = urllib.parse.urlencode({'username': user, 'password': pw})
    out = {'items': [], 'error': ''}

    def b64(v):
        try:
            return base64.b64decode(v + '=' * (-len(str(v)) % 4)).decode('utf-8', 'replace')
        except Exception:
            return str(v or '')
    try:
        j = _xt(host, qs, 'get_short_epg', timeout, 2_000_000, opener,
                extra=f"&stream_id={urllib.parse.quote(str(stream_id))}&limit=4")
        for e in ((j.get('epg_listings') if isinstance(j, dict) else None) or []):
            if isinstance(e, dict):
                out['items'].append({'title': b64(e.get('title', '')), 'desc': b64(e.get('description', ''))[:300],
                                     'start': _int(e.get('start_timestamp')), 'end': _int(e.get('stop_timestamp'))})
    except Exception as e:
        out['error'] = _net_reason(e)
    return out


def _direct_url(host, user, pw, sid, ext, kind):
    seg = {'live': 'live', 'vod': 'movie', 'movie': 'movie', 'series': 'series'}.get(kind, 'live')
    return (f"{host.rstrip('/')}/{seg}/{urllib.parse.quote(user)}/"
            f"{urllib.parse.quote(pw)}/{sid}.{str(ext).lstrip('.')}")


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
             'البيانات (هوست/مستخدم/مرور)', 'البيانات مع الدلالة', 'محتوى للكبار']
    widths = [17, 34, 18, 18, 18, 14, 12, 14, 14, 40, 52, 40, 44, 14]
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
            (8, ('نعم 🔞' if (r.get('adult') or {}).get('adult') else ('لا' if r.get('adult') else '')), False),
        ]
        cells = [_cell(f'{_col(i)}{ri}', xf, val, num=num) for i, (xf, val, num) in enumerate(vals)]
        xml.append(f'<row r="{ri}" ht="48" customHeight="1">' + ''.join(cells) + '</row>')
    last = _col(len(heads) - 1)
    xml.append('</sheetData>')
    fref = f'A1:{last}{max(1, len(rows) + 1)}'
    xml.append(f'<autoFilter ref="{fref}"/>')
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
               '<sheets><sheet name="السيرفرات" sheetId="1" r:id="rId1"/></sheets>'
               '<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
               f"'السيرفرات'!$A$1:${last}${max(1, len(rows) + 1)}</definedName></definedNames></workbook>")
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
    with opener.open(urllib.request.Request(url, headers={'User-Agent': _cur_ua() or UA}), timeout=timeout) as r:
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
                                            headers={'User-Agent': _cur_ua() or UA}), timeout=timeout) as r:
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
#  السجل، القيود، الفحص العميق، التفاصيل، Stalker، ffmpeg، المشغّلات الخارجية
# ----------------------------------------------------------------------------
_REG = {}
_REG_LOCK = threading.Lock()


def reg_server(d):
    """يسجّل بيانات سيرفر في الذاكرة ويرجع معرّفاً قصيراً، فلا تظهر كلمات المرور في روابط الوسيط."""
    t = str(d.get('type') or 'xtream')
    srv = {'type': t, 'proxy': str(d.get('proxy') or '').strip(),
           'host': str(d.get('host') or d.get('portal') or d.get('url') or '').strip().rstrip('/'),
           'username': str(d.get('username') or ''), 'password': str(d.get('password') or ''),
           'mac': str(d.get('mac') or '').strip().upper(), 'ua': str(d.get('ua') or '').strip()[:300]}
    sid = hashlib.sha1('|'.join([t, srv['host'], srv['username'], srv['password'], srv['mac'], srv['proxy'], srv['ua']])
                       .encode('utf-8')).hexdigest()[:12]
    with _REG_LOCK:
        _REG[sid] = srv
    return sid


_DOM_SEMS = {}
_DOM_LOCK = threading.Lock()


def _dom_key(host):
    h = str(host or '').strip()
    try:
        return (urllib.parse.urlsplit(h if '://' in h else 'http://' + h).hostname or h).lower()
    except ValueError:
        return h.lower()


def _dom_sem(host):
    """حد أقصى للفحوص المتزامنة على نفس النطاق حتى لا يحظر السيرفر عنواننا."""
    n = max(1, min(10, int(get_settings().get('per_domain') or 2)))
    k = _dom_key(host)
    with _DOM_LOCK:
        cur = _DOM_SEMS.get(k)
        if cur is None or cur[0] != n:
            cur = (n, threading.Semaphore(n))
            _DOM_SEMS[k] = cur
        return cur[1]


_DEEP_LOCKS = {}


def _deep_lock(key):
    with _DOM_LOCK:
        return _DEEP_LOCKS.setdefault(key, threading.Lock())


def hist_key(typ, d):
    if typ == 'stalker':
        return 'stalker|' + _dom_key(d.get('portal')) + '|' + str(d.get('mac') or '').upper()
    if typ == 'm3u':
        return 'm3u|' + str(d.get('url') or '').lower().rstrip('/')
    import hashlib
    pw = hashlib.sha1(str(d.get('password') or '').encode('utf-8')).hexdigest()[:10]
    return str(d.get('host') or '').lower().rstrip('/') + '|' + str(d.get('username') or '') + '|' + pw


# ---------------- الفحص العميق للبث ----------------
def _looks_ts(buf):
    """مزامنة MPEG-TS: بايت 0x47 كل 188 بايت (4 مرات متتالية على الأقل)."""
    n = len(buf)
    for off in range(0, min(188, n)):
        if buf[off] != 0x47:
            continue
        k = 1
        good = True
        while off + 188 * k < n and k < 6:
            if buf[off + 188 * k] != 0x47:
                good = False
                break
            k += 1
        if good and k >= 4:
            return True
    return False


def _probe_sample(url, op, timeout, read_secs=4.0, max_bytes=700_000):
    res = {'ok': False, 'ttfb': None, 'kbps': None, 'why': ''}
    t0 = time.time()
    try:
        resp = op.open(urllib.request.Request(url, headers={'User-Agent': _cur_ua() or UA, 'Accept': '*/*', 'Connection': 'close'}),
                       timeout=timeout)
    except urllib.error.HTTPError as e:
        res['why'] = _http_reason(e.code)
        return res
    except Exception as e:
        res['why'] = _net_reason(e)
        return res
    try:
        ctype = (resp.headers.get('Content-Type') or '').lower()
        buf = bytearray()
        first = None
        deadline = time.time() + read_secs
        try:
            while len(buf) < max_bytes and time.time() < deadline:
                chunk = resp.read(32768)
                if not chunk:
                    break
                if first is None:
                    first = time.time()
                    res['ttfb'] = int((first - t0) * 1000)
                buf += chunk
        except Exception as e:
            if first is None:
                res['why'] = _net_reason(e)
                return res
        if first is None:
            res['why'] = 'السيرفر لم يُرسل أي بيانات'
            return res
        dur = max(time.time() - first, 0.2)
        res['kbps'] = int(len(buf) * 8 / dur / 1000)
        head = bytes(buf[:64]).lstrip().lower()
        if 'text/html' in ctype or head.startswith(b'<!doctype') or head.startswith(b'<html'):
            res['why'] = 'الرد صفحة نصية وليس بثاً'
        elif _looks_ts(bytes(buf)):
            res['ok'] = True
        elif bytes(buf[:7]) == b'#EXTM3U':
            res['ok'] = True
        elif len(buf) >= 100_000 and ('video' in ctype or 'octet' in ctype or 'mpegurl' in ctype):
            res['ok'] = True
        else:
            res['why'] = 'محتوى غير معروف أو قصير جداً'
        return res
    finally:
        try:
            resp.close()
        except Exception:
            pass


def deep_check(host, user, pw, timeout=DEFAULT_TIMEOUT, proxy='', samples=3):
    """يجرّب بث 2-3 قنوات عشوائية فعلياً ويقيس جودة البث (قناة واحدة في كل مرة لاحترام حد الاتصالات)."""
    base = host.rstrip('/')
    op = _opener_for(proxy)
    qs = urllib.parse.urlencode({'username': user, 'password': pw})
    out = {'quality': 'none', 'ok': 0, 'total': 0, 'ttfb_ms': None, 'kbps': None, 'samples': [], 'message': ''}
    with _deep_lock(base.lower() + '|' + user):
        try:
            _, d, _ = _get(f"{base}/player_api.php?{qs}", timeout, opener=op)
            j = json.loads(d.decode('utf-8', 'replace'))
            ui = (j.get('user_info') or {}) if isinstance(j, dict) else {}
            ac, mc = _int(ui.get('active_cons')), _int(ui.get('max_connections'))
            if mc and ac is not None and ac >= mc:
                out['quality'] = 'unknown'
                out['message'] = 'كل الاتصالات مستخدمة حالياً — تم تخطي فحص البث لتجنب قطع المشاهدة'
                return out
        except Exception:
            pass
        picks = []
        try:
            cats = _xt(base, qs, 'get_live_categories', timeout, 5_000_000, op)
            ids = [str(c.get('category_id')) for c in cats if isinstance(c, dict) and c.get('category_id') is not None]
            random.shuffle(ids)
            for cid in ids[:6]:
                if len(picks) >= samples:
                    break
                try:
                    arr = _xt(base, qs, 'get_live_streams', timeout, 6_000_000, op, extra=f'&category_id={cid}')
                except Exception:
                    continue
                arr = [c for c in arr if isinstance(c, dict) and c.get('stream_id') is not None] if isinstance(arr, list) else []
                if arr:
                    picks.append(random.choice(arr))
        except Exception:
            pass
        if not picks:
            try:
                arr = _xt(base, qs, 'get_live_streams', max(timeout, 30), 60_000_000, op)
                arr = [c for c in arr if isinstance(c, dict) and c.get('stream_id') is not None] if isinstance(arr, list) else []
                random.shuffle(arr)
                picks = arr[:samples]
            except Exception as e:
                out['message'] = 'تعذّر جلب قائمة القنوات: ' + _net_reason(e)
                return out
        if not picks:
            out['message'] = 'لا توجد قنوات مباشرة على هذا الحساب'
            return out
        for c in picks[:samples]:
            ext = 'ts'
            url = _direct_url(base, user, pw, c['stream_id'], ext, 'live')
            r = _probe_sample(url, op, min(timeout, 15))
            r['name'] = str(c.get('name') or c['stream_id'])[:60]
            out['samples'].append(r)
            time.sleep(0.4)
    tot = len(out['samples'])
    oks = [x for x in out['samples'] if x['ok']]
    out['total'], out['ok'] = tot, len(oks)
    if oks:
        tt = [x['ttfb'] for x in oks if x['ttfb'] is not None]
        kk = [x['kbps'] for x in oks if x['kbps'] is not None]
        out['ttfb_ms'] = int(sum(tt) / len(tt)) if tt else None
        out['kbps'] = int(sum(kk) / len(kk)) if kk else None
        if len(oks) == tot and (out['ttfb_ms'] or 0) < 3000 and (out['kbps'] or 0) >= 1500:
            out['quality'] = 'excellent'
        elif len(oks) * 2 >= tot and (out['kbps'] or 0) >= 500:
            out['quality'] = 'good'
        else:
            out['quality'] = 'weak'
        out['message'] = f"{len(oks)} من {tot} قنوات تبث فعلاً"
    else:
        out['message'] = (out['samples'][0]['why'] if out['samples'] else '') or 'لا توجد قناة تبث'
    return out


# ---------------- تفاصيل السيرفر ----------------
_DETAIL_CACHE = {}


def get_details(host, user, pw, timeout=DEFAULT_TIMEOUT, proxy=''):
    base = host.rstrip('/')
    ck = (base, user, proxy)
    c = _DETAIL_CACHE.get(ck)
    if c and time.time() - c[0] < 600:
        return c[1]
    op = _opener_for(proxy)
    qs = urllib.parse.urlencode({'username': user, 'password': pw})
    out = {'counts': {}, 'ip': '', 'country': '', 'https': None, 'formats': [], 'protocol': '', 'error': ''}
    try:
        hn = urllib.parse.urlsplit(base).hostname
        out['ip'] = socket.gethostbyname(hn) if hn else ''
        if out['ip']:
            out['country'] = (_geo(out['ip']) or {}).get('country', '')
    except Exception:
        pass
    try:
        _, d, _ = _get(f"{base}/player_api.php?{qs}", timeout, opener=op)
        j = json.loads(d.decode('utf-8', 'replace'))
        si = (j.get('server_info') or {}) if isinstance(j, dict) else {}
        ui = (j.get('user_info') or {}) if isinstance(j, dict) else {}
        fm = ui.get('allowed_output_formats')
        out['formats'] = [str(x) for x in fm] if isinstance(fm, list) else []
        out['protocol'] = str(si.get('server_protocol') or '')
        hp = si.get('https_port')
        if base.lower().startswith('https://'):
            out['https'] = True
        elif hp:
            try:
                hn = urllib.parse.urlsplit(base).hostname
                _get(f"https://{hn}:{hp}/player_api.php?{qs}", min(timeout, 8), limit=2000, opener=op)
                out['https'] = True
            except urllib.error.HTTPError:
                out['https'] = True
            except Exception:
                out['https'] = False
    except Exception as e:
        out['error'] = _net_reason(e)

    def cnt(k, action):
        try:
            arr = _xt(base, qs, action, max(timeout, 30), 250_000_000, op)
            out['counts'][k] = len(arr) if isinstance(arr, list) else None
        except Exception:
            out['counts'][k] = None
    ths = [threading.Thread(target=cnt, args=(k, a)) for k, a in
           (('live', 'get_live_streams'), ('vod', 'get_vod_streams'), ('series', 'get_series'))]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    _DETAIL_CACHE[ck] = (time.time(), out)
    return out


# ---------------- Stalker: جلب القنوات وروابط البث ----------------
def _st_base(portal):
    base = portal.strip()
    base = base if base.lower().startswith('http') else 'http://' + base
    return base, ('&' if '?' in base else '?')


def _st_req(portal, mac, qs, token=None, timeout=15, proxy=''):
    op = _opener_for(proxy)
    base, sep = _st_base(portal)
    h = {'User-Agent': _cur_ua() or _STALKER_UA, 'Accept': '*/*',
         'Cookie': f'mac={mac}; stb_lang=en; timezone=America/New_York',
         'X-User-Agent': 'Model: MAG250; Link: WiFi', 'Referer': base}
    if token:
        h['Authorization'] = 'Bearer ' + token
    with op.open(urllib.request.Request(base + sep + qs, headers=h), timeout=timeout) as resp:
        return json.loads(resp.read(12_000_000).decode('utf-8', 'replace').strip() or '{}')


_ST_TOK = {}


def _st_token(portal, mac, proxy='', timeout=15, force=False):
    k = (portal, mac, proxy)
    c = _ST_TOK.get(k)
    if c and not force and time.time() - c[1] < 480:
        return c[0]
    j = _st_req(portal, mac, 'type=stb&action=handshake&token=&JsHttpRequest=1-xml', None, timeout, proxy)
    tok = (j.get('js') or {}).get('token') if isinstance(j, dict) else None
    if not tok:
        raise RuntimeError('المصافحة فشلت — MAC غير صالح أو محظور')
    try:
        _st_req(portal, mac, 'type=stb&action=get_profile&JsHttpRequest=1-xml', tok, timeout, proxy)
    except Exception:
        pass
    _ST_TOK[k] = (tok, time.time())
    return tok

# ---------------- البحث داخل قنوات السيرفرات ----------------
_CH_CACHE = {}           # مفتاح السيرفر -> (الوقت، القائمة)
_CH_LOCK = threading.Lock()
_CH_TTL = 30 * 60


def _norm(s):
    """توحيد النص للبحث: حروف صغيرة، بدون مسافات ورموز، وتوحيد الألف والتاء المربوطة والياء."""
    s = str(s or '').lower()
    s = s.translate(str.maketrans({'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ة': 'ه', 'ى': 'ي', 'ؤ': 'و', 'ئ': 'ي'}))
    return re.sub(r'[^0-9a-zء-ي]+', '', s)


def _parse_m3u_text(text):
    items, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#EXTINF'):
            g = re.search(r'group-title="([^"]*)"', line, re.I)
            nm = line.rsplit(',', 1)[1].strip() if ',' in line else ''
            cur = {'name': nm, 'cat': g.group(1) if g else ''}
        elif not line.startswith('#'):
            it = dict(cur or {'name': '', 'cat': ''})
            it['ou'] = line
            it['id'] = len(items) + 1
            items.append(it)
            cur = None
    return items


def _channel_list(d, timeout=25, force=False):
    """قائمة القنوات المباشرة لسيرفر (مع أسماء التصنيفات)، مخزّنة مؤقتاً 30 دقيقة."""
    typ = d.get('type') or 'xtream'
    key = hist_key(typ, d)
    now = time.time()
    with _CH_LOCK:
        c = _CH_CACHE.get(key)
    if c and not force and now - c[0] < _CH_TTL:
        return c[1]
    proxy = str(d.get('proxy') or '')
    op = _opener_for(proxy)
    if typ == 'stalker':
        res = stalker_channels(d.get('portal') or d.get('host'), d.get('mac') or d.get('username'), proxy, timeout)
        cats = res.get('cats') or {}
        items = [{'name': x.get('name', ''), 'cat': cats.get(str(x.get('cat')), ''), 'cmd': x.get('cmd', ''),
                  'id': x.get('id')} for x in res.get('items') or []]
        if not items and res.get('error'):
            raise RuntimeError(res['error'])
    elif typ == 'm3u':
        items = _parse_m3u_text(_fetch_text(d.get('url') or d.get('host'), op, timeout, 60_000_000))
    else:
        host = str(d.get('host') or '').rstrip('/')
        qs = urllib.parse.urlencode({'username': d.get('username', ''), 'password': d.get('password', '')})
        cats = _cats(host, qs, 'get_live_categories', timeout, op)
        st = get_streams(host, d.get('username', ''), d.get('password', ''), 'live', max(timeout, 30), op)
        if st.get('error') and not st.get('items'):
            raise RuntimeError(st['error'])
        items = [dict(x, cat=cats.get(str(x.get('cat')), '')) for x in st.get('items') or []]
    with _CH_LOCK:
        _CH_CACHE[key] = (now, items)
    return items


def _vod_list(d, kind, timeout=30, force=False):
    """قائمة الأفلام أو المسلسلات (Xtream) مع أسماء التصنيفات — تشارك ذاكرة القرص مع المشغّل."""
    host = str(d.get('host') or '').rstrip('/')
    op = _opener_for(str(d.get('proxy') or ''))
    k = 'streams|' + kind + '|' + hist_key('xtream', d) + '|' + _cur_ua()
    r = cached_list(k, lambda: get_streams(host, d.get('username', ''), d.get('password', ''), kind, timeout, op),
                    force, lambda x: bool(x.get('items')))
    if r.get('error') and not r.get('items'):
        raise RuntimeError(r['error'])
    qs = urllib.parse.urlencode({'username': d.get('username', ''), 'password': d.get('password', '')})
    cats = _cats(host, qs, 'get_vod_categories' if kind == 'vod' else 'get_series_categories', timeout, op)
    return [dict(x, cat=cats.get(str(x.get('cat')), '')) for x in r.get('items') or []]


def channel_search(d, terms, verify=False, timeout=25, force=False, kinds=('live',)):
    terms = [t for t in (_norm(x) for x in terms) if len(t) >= 2]
    out = {'count': 0, 'total': 0, 'matches': [], 'verified': None, 'error': '', 'by': {}}
    if not terms:
        out['error'] = 'اكتب كلمة بحث من حرفين على الأقل'
        return out
    typ = d.get('type') or 'xtream'
    kinds = [k for k in kinds if k in ('live', 'vod', 'series')] or ['live']
    if typ != 'xtream':
        kinds = ['live']                                 # الأفلام والمسلسلات متاحة في Xtream فقط
    hits, errs = [], []
    for kind in kinds:
        try:
            items = _channel_list(d, timeout, force) if kind == 'live' else _vod_list(d, kind, max(timeout, 30), force)
        except Exception as e:
            errs.append((str(e) if isinstance(e, RuntimeError) else _net_reason(e))[:150])
            continue
        out['total'] += len(items)
        n_k = 0
        for it in items:
            n, c = _norm(it.get('name')), _norm(it.get('cat'))
            if any(t in n or t in c for t in terms):
                hits.append(dict(it, kind=kind))
                n_k += 1
        out['by'][kind] = n_k
    if errs and not out['total']:
        out['error'] = 'تعذّر جلب المحتوى: ' + errs[0]
        return out
    out['count'] = len(hits)
    out['matches'] = [{k: v for k, v in h.items() if k in ('name', 'cat', 'id', 'ext', 'cmd', 'ou', 'icon', 'kind')}
                      for h in hits[:400]]
    vhits = [h for h in hits if h.get('kind') == 'live'] or [h for h in hits if h.get('kind') == 'vod']
    if verify and vhits:
        hits = vhits
        typ = d.get('type') or 'xtream'
        proxy = str(d.get('proxy') or '')
        op = _opener_for(proxy)
        tried = 0
        for h in hits[:3]:                              # حتى 3 قنوات، واحدة في كل مرة
            tried += 1
            try:
                if typ == 'stalker':
                    url = stalker_link(d.get('portal') or d.get('host'), d.get('mac') or d.get('username'),
                                       h.get('cmd', ''), proxy, 15)
                elif typ == 'm3u':
                    url = h.get('ou')
                else:
                    vk = h.get('kind') == 'vod'
                    url = _direct_url(str(d.get('host')).rstrip('/'), d.get('username', ''), d.get('password', ''),
                                      h.get('id'), (h.get('ext') or 'mp4') if vk else 'ts', 'vod' if vk else 'live')
                r = _probe_sample(url, op, 15)
            except Exception as e:
                r = {'ok': False, 'why': str(e)[:120]}
            r['name'] = str(h.get('name') or '')[:60]
            out['verified'] = r
            if r.get('ok'):
                break
            time.sleep(0.4)
        out['verified']['tried'] = tried
    return out


# ---------------- كشف المحتوى المخصص للكبار ----------------
_ADULT_RE = re.compile(
    r'(xxx|\badults?\b|\b18\s*\+|\+\s*18\b|\bporn|\bsex\b|\bsexy\b|erotic|playboy|brazzers|hustler|penthouse|'
    r'redlight|red\s*light|\bhot\s*club\b|\bxx\b|\bnsfw\b|للكبار|كبار\s*فقط|سكس|اباحي|إباحي|\+١٨|١٨\+)', re.I)

_ADULT_NOT = re.compile(r'(adult\s*swim|young\s+adults?|sex\s+and\s+the\s+city|sex\s+education|sex\s*/\s*life|sex\s+lives)', re.I)


def adult_scan(d, timeout=20):
    """يفحص أسماء التصنيفات (وأسماء القنوات في M3U وستالكر) بحثاً عن محتوى للكبار."""
    typ = d.get('type') or 'xtream'
    out = {'adult': False, 'hits': [], 'error': ''}
    names = []
    try:
        if typ == 'xtream':
            host = str(d.get('host') or '').rstrip('/')
            qs = urllib.parse.urlencode({'username': d.get('username', ''), 'password': d.get('password', '')})
            op = _opener_for(str(d.get('proxy') or ''))
            for act, lbl in (('get_live_categories', 'قنوات'), ('get_vod_categories', 'أفلام'),
                             ('get_series_categories', 'مسلسلات')):
                names += [(lbl, n) for n in _cats(host, qs, act, timeout, op).values()]
        else:
            items = _channel_list(d, timeout)
            cats = sorted({str(x.get('cat') or '') for x in items if x.get('cat')})
            names += [('تصنيف', c) for c in cats]
            names += [('قناة', str(x.get('name') or '')) for x in items]
    except Exception as e:
        out['error'] = str(e)[:150]
        return out
    seen = set()
    for lbl, n in names:
        if n and _ADULT_RE.search(_ADULT_NOT.sub(' ', n)) and n not in seen:
            seen.add(n)
            out['hits'].append(f'{lbl}: {n}'[:80])
    out['adult'] = bool(out['hits'])
    out['hits'] = out['hits'][:30]
    return out


# ---------------- اكتشاف هوية المشغّل المناسبة للسيرفر ----------------
UA_PRESETS = [
    ('VLC', 'VLC/3.0.20 LibVLC/3.0.20'),
    ('IPTV Smarters Pro', 'IPTVSmartersPro'),
    ('TiviMate', 'TiviMate/4.7.0 (Android 11)'),
    ('OTT Navigator', 'OTT Navigator/1.6.9.4 (Linux;Android 11)'),
    ('Kodi', 'Kodi/20.2 (Windows NT 10.0; Win64; x64) App_Bitness/64 Version/20.2'),
    ('MAG', 'Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3'),
    ('okhttp', 'okhttp/4.9.0'),
    ('Chrome', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
]


def ua_detect(d, timeout=10, stream=True):
    """يجرّب كل هوية على السيرفر: هل يقبل الحساب؟ وهل يبث فعلاً؟ ويقترح الأنسب."""
    typ = d.get('type') or 'xtream'
    proxy = str(d.get('proxy') or '')
    op = _opener_for(proxy)
    res = []
    sid = None
    host = str(d.get('host') or '').rstrip('/')
    qs = urllib.parse.urlencode({'username': d.get('username', ''), 'password': d.get('password', '')})
    for name, ua in UA_PRESETS:
        row = {'name': name, 'ua': ua, 'api': False, 'stream': None, 'why': ''}
        _TL.ua = ua
        try:
            if typ == 'stalker':
                _st_token(d.get('portal') or d.get('host'), str(d.get('mac') or d.get('username') or '').upper(),
                          proxy, timeout, force=True)
                row['api'] = True
            elif typ == 'm3u':
                _, b, _ = _get(d.get('url') or d.get('host'), timeout, 65536, op, ua)
                row['api'] = b'#EXT' in b[:2000] or b'http' in b[:4000]
                if not row['api']:
                    row['why'] = 'الرد ليس قائمة M3U'
            else:
                _, b, _ = _get(f'{host}/player_api.php?{qs}', timeout, 2_000_000, op, ua)
                j = json.loads(b.decode('utf-8', 'replace') or 'null')
                ui = j.get('user_info') if isinstance(j, dict) else None
                row['api'] = isinstance(ui, dict) and str(ui.get('auth', 1)) != '0'
                if not row['api']:
                    row['why'] = 'رفض الحساب'
                elif stream:
                    if sid is None:
                        try:
                            arr = _xt(host, qs, 'get_live_streams', max(timeout, 20), 60_000_000, op)
                            sid = next((c.get('stream_id') for c in arr if isinstance(c, dict) and c.get('stream_id')), '') \
                                if isinstance(arr, list) else ''
                        except Exception:
                            sid = ''
                    if sid:
                        pr = _probe_sample(_direct_url(host, d.get('username', ''), d.get('password', ''), sid, 'ts', 'live'),
                                           op, min(timeout, 12), read_secs=2.0, max_bytes=200_000)
                        row['stream'] = bool(pr.get('ok'))
                        if not pr.get('ok'):
                            row['why'] = pr.get('why') or 'لا يبث'
                        time.sleep(0.4)
        except urllib.error.HTTPError as e:
            row['why'] = _http_reason(e.code)
        except Exception as e:
            row['why'] = (str(e) if isinstance(e, RuntimeError) else _net_reason(e))[:100]
        finally:
            _TL.ua = ''
        res.append(row)
    best = next((r for r in res if r['api'] and r['stream']), None) or next((r for r in res if r['api']), None)
    default_ok = bool(res and res[0]['api'] and res[0]['stream'] is not False)
    return {'results': res, 'best': best['ua'] if best else '', 'best_name': best['name'] if best else '',
            'default_ok': default_ok}



def stalker_channels(portal, mac, proxy='', timeout=20):
    out = {'cats': {}, 'items': [], 'error': ''}
    mac = mac.strip().upper()
    try:
        tok = _st_token(portal, mac, proxy, timeout)
        try:
            g = _st_req(portal, mac, 'type=itv&action=get_genres&JsHttpRequest=1-xml', tok, timeout, proxy)
            for x in ((g.get('js') if isinstance(g, dict) else None) or []):
                if isinstance(x, dict) and x.get('id') is not None:
                    out['cats'][str(x.get('id'))] = x.get('title') or x.get('name') or ''
        except Exception:
            pass
        lst = []
        try:
            j = _st_req(portal, mac, 'type=itv&action=get_all_channels&JsHttpRequest=1-xml', tok, max(timeout, 40), proxy)
            js = (j.get('js') if isinstance(j, dict) else None)
            lst = (js.get('data') if isinstance(js, dict) else js) or []
        except Exception:
            lst = []
        if not lst:
            for p in range(1, 31):
                jj = _st_req(portal, mac, 'type=itv&action=get_ordered_list&genre=*&force_ch_link_check=&fav=0'
                             f'&sortby=number&hd=0&p={p}&JsHttpRequest=1-xml', tok, timeout, proxy)
                dd = (jj.get('js') if isinstance(jj, dict) else None) or {}
                chunk = dd.get('data') or [] if isinstance(dd, dict) else []
                if not chunk:
                    break
                lst += chunk
                tot = _int(dd.get('total_items')) or 0
                if tot and len(lst) >= tot:
                    break
        for c in lst:
            if isinstance(c, dict) and c.get('cmd'):
                out['items'].append({'id': c.get('id') or c.get('cmd'), 'name': str(c.get('name') or ''),
                                     'cat': str(c.get('tv_genre_id', '')), 'cmd': c.get('cmd')})
        if not out['items']:
            out['error'] = 'لم تُرجع البوابة أي قنوات'
    except Exception as e:
        out['error'] = str(e) if isinstance(e, RuntimeError) else _net_reason(e)
    return out


def stalker_link(portal, mac, cmd, proxy='', timeout=15):
    mac = mac.strip().upper()
    qs = ('type=itv&action=create_link&cmd=' + urllib.parse.quote(cmd, safe='') +
          '&series=&forced_storage=undefined&disable_ad=0&download=0&JsHttpRequest=1-xml')
    for force in (False, True):
        tok = _st_token(portal, mac, proxy, timeout, force=force)
        j = _st_req(portal, mac, qs, tok, timeout, proxy)
        c = ((j.get('js') if isinstance(j, dict) else None) or {}).get('cmd') or ''
        m = re.search(r'(https?://\S+)', str(c))
        if m:
            return m.group(1)
    raise RuntimeError('تعذّر الحصول على رابط البث من البوابة')


# ---------------- ffmpeg والمشغّلات الخارجية ----------------
def _bundle_dirs():
    """المجلدات التي قد يوجد فيها ffmpeg المدمج: بجانب البرنامج، داخل حزمة ماك، أو مجلد الاستخراج."""
    ds = []
    if getattr(sys, 'frozen', False):
        ed = os.path.dirname(os.path.abspath(sys.executable))
        ds += [ed, os.path.join(ed, 'ffmpeg'), os.path.join(ed, '..', 'Frameworks'), os.path.join(ed, '..', 'Resources'),
               os.path.join(ed, '..', 'Frameworks', 'ffmpeg'), os.path.join(ed, '..', 'Resources', 'ffmpeg')]
        mp = getattr(sys, '_MEIPASS', '')
        if mp:
            ds += [mp, os.path.join(mp, 'ffmpeg')]
    else:
        ds.append(os.path.dirname(os.path.abspath(__file__)))
    ds.append(os.path.join(_DATA_DIR, 'bin'))
    return ds


def _find_tool(name, custom=''):
    if custom and os.path.exists(custom):
        d = os.path.dirname(custom)
        cand = os.path.join(d, name + ('.exe' if sys.platform.startswith('win') else ''))
        if os.path.basename(custom).lower().startswith(name):
            return custom
        if os.path.exists(cand):
            return cand
    exe = name + ('.exe' if sys.platform.startswith('win') else '')
    for d in _bundle_dirs():                           # النسخة المدمجة مع البرنامج أولاً
        c = os.path.join(d, exe)
        if os.path.isfile(c):
            return c
    p = shutil.which(name)
    if p:
        return p
    extra = []
    if sys.platform == 'darwin':
        extra = ['/opt/homebrew/bin/' + name, '/usr/local/bin/' + name]
    elif sys.platform.startswith('win'):
        for pf in (os.environ.get('ProgramFiles', r'C:\Program Files'), os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                   r'C:\ffmpeg\bin'):
            extra.append(os.path.join(pf, 'ffmpeg', 'bin', name + '.exe'))
            extra.append(os.path.join(pf, name + '.exe'))
    else:
        extra = ['/usr/local/bin/' + name, '/snap/bin/' + name]
    return next((c for c in extra if os.path.exists(c)), '')


def find_ffmpeg():
    return _find_tool('ffmpeg', get_settings().get('ffmpeg_path') or '')


def find_ffprobe():
    return _find_tool('ffprobe', get_settings().get('ffmpeg_path') or '')


def _player_candidates():
    """مسارات المشغّلات المعروفة حسب النظام."""
    c = {'vlc': [], 'mpv': [], 'iina': [], 'potplayer': []}
    if sys.platform.startswith('win'):
        pf = os.environ.get('ProgramFiles', r'C:\Program Files')
        pf86 = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
        c['vlc'] = [pf + r'\VideoLAN\VLC\vlc.exe', pf86 + r'\VideoLAN\VLC\vlc.exe']
        c['mpv'] = [pf + r'\mpv\mpv.exe', r'C:\mpv\mpv.exe']
        c['potplayer'] = [pf + r'\DAUM\PotPlayer\PotPlayerMini64.exe', pf86 + r'\DAUM\PotPlayer\PotPlayerMini.exe']
    elif sys.platform == 'darwin':
        c['vlc'] = ['/Applications/VLC.app/Contents/MacOS/VLC']
        c['mpv'] = ['/opt/homebrew/bin/mpv', '/usr/local/bin/mpv', '/Applications/mpv.app/Contents/MacOS/mpv']
        c['iina'] = ['/Applications/IINA.app/Contents/MacOS/IINA']
    return c


def find_player(name):
    p = shutil.which(name if name != 'potplayer' else 'PotPlayerMini64')
    if p:
        return p
    return next((x for x in _player_candidates().get(name, []) if os.path.exists(x)), '')


def get_caps():
    return {'ffmpeg': find_ffmpeg(), 'ffprobe': find_ffprobe(), 'vlc': find_player('vlc'), 'mpv': find_player('mpv'),
            'iina': find_player('iina') if sys.platform == 'darwin' else '',
            'potplayer': find_player('potplayer') if sys.platform.startswith('win') else '',
            'data_dir': _DATA_DIR, 'version': VERSION, 'platform': sys.platform}


def launch_external(url, player='auto', custom=''):
    """يشغّل الرابط في المشغّل المفضّل، وإلا VLC، وإلا المشغّل الافتراضي عبر ملف تشغيل مؤقت."""
    exe = ''
    name = player
    if player == 'custom' and custom and os.path.exists(custom):
        exe = custom
    elif player in ('vlc', 'mpv', 'iina', 'potplayer'):
        exe = find_player(player)
    if not exe:
        for n in ('vlc', 'mpv', 'iina', 'potplayer'):
            if player not in ('auto', n) and exe:
                break
            if player == 'auto' and n != 'vlc':
                continue
            exe = find_player(n)
            if exe:
                name = n
                break
    if exe:
        subprocess.Popen([exe, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return name if name in ('vlc', 'mpv', 'iina', 'potplayer') else 'custom'
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


def _probe_with_ffmpeg(local_url):
    ff = find_ffmpeg()
    if not ff:
        return {'ok': False, 'reason': 'ffmpeg غير موجود'}
    try:
        p = subprocess.run([ff, '-hide_banner', '-analyzeduration', '3000000', '-probesize', '3000000', '-i', local_url],
                           capture_output=True, timeout=25)
        txt = p.stderr.decode('utf-8', 'replace')
        out = {'ok': True, 'video': '', 'audio': '', 'w': None, 'h': None}
        mv = re.search(r'Stream #.*?Video:\s*([a-z0-9_]+).*?(\d{2,5})x(\d{2,5})', txt)
        ma = re.search(r'Stream #.*?Audio:\s*([a-z0-9_]+)', txt)
        if mv:
            out['video'], out['w'], out['h'] = mv.group(1), int(mv.group(2)), int(mv.group(3))
        if ma:
            out['audio'] = ma.group(1)
        if not out['video'] and not out['audio']:
            return {'ok': False, 'reason': 'تعذّر قراءة البث'}
        return out
    except Exception as e:
        return {'ok': False, 'reason': str(e)[:120]}


def probe_codec(local_url):
    ff = find_ffprobe()
    if not ff:
        return _probe_with_ffmpeg(local_url)
    try:
        p = subprocess.run([ff, '-v', 'error', '-analyzeduration', '4000000', '-probesize', '4000000',
                            '-show_entries', 'stream=codec_type,codec_name,width,height', '-of', 'json', local_url],
                           capture_output=True, timeout=25)
        j = json.loads(p.stdout.decode('utf-8', 'replace') or '{}')
        out = {'ok': True, 'video': '', 'audio': '', 'w': None, 'h': None}
        for s_ in j.get('streams', []):
            if s_.get('codec_type') == 'video' and not out['video']:
                out['video'], out['w'], out['h'] = s_.get('codec_name', ''), s_.get('width'), s_.get('height')
            elif s_.get('codec_type') == 'audio' and not out['audio']:
                out['audio'] = s_.get('codec_name', '')
        if not out['video'] and not out['audio']:
            out = {'ok': False, 'reason': 'تعذّر قراءة البث'}
        return out
    except Exception as e:
        return {'ok': False, 'reason': str(e)[:120]}


# ---------------- التحقق من التحديثات ----------------
def _vtuple(v):
    return tuple(int(x) for x in re.findall(r'\d+', str(v))[:4])


_REPO = 'mhassoun500/iptv-checker-builds'
_UPD = {'stage': 'idle', 'pct': 0, 'msg': '', 'error': ''}
_UPD_INFO = {}


def update_check():
    try:
        txt = _fetch_text(f'https://api.github.com/repos/{_REPO}/releases/latest',
                          _build_opener(''), timeout=6, limit=400_000)
        j = json.loads(txt)
        tag = str(j.get('tag_name') or '')
        if tag and _vtuple(tag) > _vtuple(VERSION):
            assets = {a.get('name'): a.get('browser_download_url') for a in j.get('assets') or [] if a.get('name')}
            _UPD_INFO.clear()
            _UPD_INFO.update(tag=tag, assets=assets)
            kind, _ = _update_target(assets)
            return {'newer': True, 'version': tag.lstrip('v'), 'url': j.get('html_url') or '',
                    'auto': bool(kind and 'SHA256SUMS.txt' in assets), 'notes': str(j.get('body') or '')[:1500]}
    except Exception:
        pass
    return {'newer': False}


def _app_paths():
    """(نوع التشغيل، المسار الحالي): source / win-setup / win-portable / mac / linux."""
    if not getattr(sys, 'frozen', False):
        return 'source', os.path.abspath(__file__)
    exe = os.path.abspath(sys.executable)
    if sys.platform == 'darwin':
        i = exe.find('.app/')
        return 'mac', (exe[:i + 4] if i > 0 else exe)
    if sys.platform.startswith('win'):
        d = os.path.dirname(exe)
        inst = any(n.lower().startswith('unins') and n.lower().endswith('.exe') for n in os.listdir(d))
        return ('win-setup' if inst else 'win-portable'), exe
    return 'linux', exe


def _update_target(assets):
    mode, _ = _app_paths()
    want = {'source': lambda n: n == 'iptv_checker.py', 'win-setup': lambda n: n.endswith('Setup.exe'),
            'win-portable': lambda n: n.endswith('-windows.exe'), 'mac': lambda n: n.endswith('.dmg'),
            'linux': lambda n: n.endswith('-linux')}[mode]
    for n in assets:
        if want(n):
            return mode, n
    return None, None


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def _download(url, dest, progress=None):
    op = _build_opener('')
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with op.open(req, timeout=60) as r, open(dest, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        got = 0
        while True:
            b = r.read(1 << 16)
            if not b:
                break
            f.write(b)
            got += len(b)
            if progress and total:
                progress(got * 100 // total)
    return dest


def _update_worker():
    import subprocess
    try:
        assets = _UPD_INFO.get('assets') or {}
        mode, name = _update_target(assets)
        if not name:
            raise RuntimeError('لا يوجد ملف تحديث مناسب لهذا الجهاز في الإصدار الجديد')
        if 'SHA256SUMS.txt' not in assets:
            raise RuntimeError('ملف البصمة SHA256SUMS.txt غير موجود في الإصدار — لن يُثبَّت التحديث')
        d = os.path.join(_DATA_DIR, 'update')
        os.makedirs(d, exist_ok=True)
        _UPD.update(stage='download', pct=0, msg='جاري تنزيل التحديث...')
        sums = _fetch_text(assets['SHA256SUMS.txt'], _build_opener(''), timeout=20, limit=200_000)
        expect = ''
        for line in sums.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[-1].lstrip('*') == name:
                expect = parts[0].lower()
        if not expect:
            raise RuntimeError('لا توجد بصمة لهذا الملف في SHA256SUMS.txt — لن يُثبَّت التحديث')
        dest = os.path.join(d, name)
        _download(assets[name], dest, lambda p: _UPD.update(pct=p))
        _UPD.update(stage='verify', pct=100, msg='جاري التحقق من البصمة...')
        got = _sha256(dest)
        if got != expect:
            os.remove(dest)
            raise RuntimeError('البصمة لا تطابق — الملف تالف أو معدّل، تم إلغاء التحديث')
        log_event('update', f'{name} sha256 OK')
        _UPD.update(stage='install', msg='جاري التثبيت وإعادة التشغيل...')
        _, cur = _app_paths()
        pid = os.getpid()
        if mode == 'win-setup':                         # انتظر إغلاق البرنامج ثم شغّل المثبّت بصمت (يعيد فتحه بعد التثبيت)
            bat = os.path.join(d, 'apply_update.bat')
            with open(bat, 'w', encoding='utf-8') as f:
                f.write('@echo off\r\n:w\r\ntasklist /FI "PID eq %d" | find "%d" >nul && (timeout /t 1 >nul & goto w)\r\n'
                        '"%s" /SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS\r\ndel "%%~f0"\r\n' % (pid, pid, dest))
            subprocess.Popen(['cmd', '/c', bat], creationflags=0x08000000, close_fds=True)
        elif mode == 'win-portable':
            bat = os.path.join(d, 'apply_update.bat')
            with open(bat, 'w', encoding='utf-8') as f:
                f.write('@echo off\r\n:w\r\ntasklist /FI "PID eq %d" | find "%d" >nul && (timeout /t 1 >nul & goto w)\r\n'
                        'move /Y "%s" "%s" >nul\r\nstart "" "%s"\r\ndel "%%~f0"\r\n' % (pid, pid, dest, cur, cur))
            subprocess.Popen(['cmd', '/c', bat], creationflags=0x08000000, close_fds=True)
        elif mode == 'mac':
            sh = os.path.join(d, 'apply_update.sh')
            mnt = os.path.join(d, 'mnt')
            with open(sh, 'w') as f:
                f.write(f'''#!/bin/bash
while kill -0 {pid} 2>/dev/null; do sleep 0.5; done
mkdir -p "{mnt}"
hdiutil attach -nobrowse -quiet -mountpoint "{mnt}" "{dest}" || exit 1
APP=$(ls -d "{mnt}"/*.app | head -1)
rm -rf "{cur}.old"; mv "{cur}" "{cur}.old" && cp -R "$APP" "{cur}" && rm -rf "{cur}.old" || mv "{cur}.old" "{cur}"
hdiutil detach -quiet "{mnt}"
xattr -cr "{cur}" 2>/dev/null
open "{cur}"
''')
            os.chmod(sh, 0o755)
            subprocess.Popen(['/bin/bash', sh], start_new_session=True, close_fds=True)
        else:                                            # linux / source
            sh = os.path.join(d, 'apply_update.sh')
            run = f'"{sys.executable}" "{cur}"' if mode == 'source' else f'"{cur}"'
            with open(sh, 'w') as f:
                f.write(f'#!/bin/sh\nwhile kill -0 {pid} 2>/dev/null; do sleep 0.5; done\n'
                        f'cp "{dest}" "{cur}" && chmod +x "{cur}"\nnohup {run} >/dev/null 2>&1 &\n')
            os.chmod(sh, 0o755)
            subprocess.Popen(['/bin/sh', sh], start_new_session=True, close_fds=True)
        _UPD.update(stage='restart', msg='سيُعاد فتح البرنامج بالإصدار الجديد...')
        threading.Timer(2.0, lambda: os._exit(0)).start()
    except Exception as e:
        log_event('error', f'update: {e}')
        _UPD.update(stage='error', error=str(e)[:200], msg='')


def update_apply():
    if _UPD['stage'] in ('download', 'verify', 'install', 'restart'):
        return dict(_UPD)
    if not _UPD_INFO.get('assets'):
        update_check()
    if not _UPD_INFO.get('assets'):
        return {'stage': 'error', 'error': 'لا يوجد تحديث متاح'}
    _UPD.update(stage='download', pct=0, msg='بدء التحديث...', error='')
    threading.Thread(target=_update_worker, daemon=True).start()
    return dict(_UPD)


# ---------------- مكتبات المشغّل محلياً ----------------
_LIBS = {'mpegts.js': 'https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.js',
         'hls.js': 'https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js'}


def get_lib(name):
    """يقدّم مكتبة المشغّل من مجلد البيانات، ويحمّلها مرة واحدة من الإنترنت عند الحاجة."""
    if name not in _LIBS:
        return None
    path = os.path.join(_LIB_DIR, name)
    try:
        if os.path.exists(path) and os.path.getsize(path) > 10_000:
            with open(path, 'rb') as f:
                return f.read()
        os.makedirs(_LIB_DIR, exist_ok=True)
        _, data, _ = _get(_LIBS[name], 30, limit=5_000_000, opener=_build_opener(''))
        if len(data) > 10_000:
            with open(path, 'wb') as f:
                f.write(data)
            return data
    except Exception as e:
        log_event('lib', f'تعذّر تحميل {name}: {e}')
    return None


# ---------------- إعادة كتابة قوائم HLS لتمر عبر الوسيط ----------------
def rewrite_hls(text, base_url, wrap):
    out = []
    for line in text.splitlines():
        s_ = line.strip()
        if not s_:
            out.append(line)
        elif s_.startswith('#'):
            if 'URI="' in s_:
                out.append(re.sub(r'URI="([^"]*)"',
                                  lambda m: 'URI="' + (m.group(1) if m.group(1).startswith('data:') else
                                                       wrap(urllib.parse.urljoin(base_url, m.group(1)))) + '"', s_))
            else:
                out.append(line)
        else:
            out.append(wrap(urllib.parse.urljoin(base_url, s_)))
    return '\n'.join(out) + '\n'


def open_upstream(url, srv, rng=None, timeout=20):
    op = _opener_for(srv.get('proxy', ''))
    err = None
    for ua in [x for x in (srv.get('ua') or _cur_ua(),) if x] + [UA, 'Mozilla/5.0', '']:
        headers = {'Accept': '*/*', 'Connection': 'close'}
        if ua:
            headers['User-Agent'] = ua
        if rng:
            headers['Range'] = rng
        try:
            return op.open(urllib.request.Request(url, headers=headers), timeout=timeout), None
        except urllib.error.HTTPError as e:
            err = e
            if e.code in (401, 403, 455):
                continue
            break
        except Exception as e:
            err = e
            break
    return None, err


def probe_upstream(url, srv):
    """تشخيص بعد الفشل: حالة السيرفر ونوع المحتوى (يفتح اتصالاً قصيراً ثم يغلقه)."""
    up, err = open_upstream(url, srv, timeout=12)
    if up is None:
        code = err.code if isinstance(err, urllib.error.HTTPError) else 0
        return {'status': code, 'msg': _http_reason(code) if code else _net_reason(err)}
    try:
        ctype = (up.headers.get('Content-Type') or '').lower()
        buf = up.read(4096 * 8)
        kind = 'ts' if _looks_ts(buf) else ('hls' if buf[:7] == b'#EXTM3U' else
                                          ('html' if b'<html' in buf[:200].lower() else 'other'))
        return {'status': getattr(up, 'status', 200), 'ctype': ctype, 'kind': kind, 'bytes': len(buf)}
    except Exception as e:
        return {'status': 0, 'msg': _net_reason(e)}
    finally:
        try:
            up.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------
#  الواجهة
# ----------------------------------------------------------------------------
HTML = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mo IPTV Checker</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 64 64%22%3E %3Cdefs%3E %3ClinearGradient id=%22bg%22 x1=%220%22 y1=%220%22 x2=%221%22 y2=%221%22%3E%3Cstop offset=%220%22 stop-color=%22%2316202b%22/%3E%3Cstop offset=%221%22 stop-color=%22%230a0f14%22/%3E%3C/linearGradient%3E %3ClinearGradient id=%22scr%22 x1=%220%22 y1=%220%22 x2=%221%22 y2=%221%22%3E%3Cstop offset=%220%22 stop-color=%22%233b82f6%22/%3E%3Cstop offset=%221%22 stop-color=%22%231d4ed8%22/%3E%3C/linearGradient%3E %3ClinearGradient id=%22ok%22 x1=%220%22 y1=%220%22 x2=%220%22 y2=%221%22%3E%3Cstop offset=%220%22 stop-color=%22%234ade80%22/%3E%3Cstop offset=%221%22 stop-color=%22%2316a34a%22/%3E%3C/linearGradient%3E %3C/defs%3E %3Crect width=%2264%22 height=%2264%22 rx=%2214%22 fill=%22url(%23bg)%22/%3E %3Cpath d=%22M24 10.5l8 6.5 8-6.5%22 fill=%22none%22 stroke=%22%2393c5fd%22 stroke-width=%222.6%22 stroke-linecap=%22round%22 stroke-linejoin=%22round%22/%3E %3Crect x=%228%22 y=%2217%22 width=%2248%22 height=%2232%22 rx=%226%22 fill=%22url(%23scr)%22/%3E %3Crect x=%2211.5%22 y=%2220.5%22 width=%2241%22 height=%2225%22 rx=%223.5%22 fill=%22%230b1220%22/%3E %3Cpath d=%22M14.5 40.5V26.5l6.3 8.2 6.3-8.2v14%22 fill=%22none%22 stroke=%22%23e6edf3%22 stroke-width=%223.6%22 stroke-linecap=%22round%22 stroke-linejoin=%22round%22/%3E %3Ccircle cx=%2234.6%22 cy=%2234.6%22 r=%225%22 fill=%22none%22 stroke=%22%2360a5fa%22 stroke-width=%223.6%22/%3E %3Crect x=%2225%22 y=%2251.5%22 width=%2214%22 height=%223%22 rx=%221.5%22 fill=%22%233b82f6%22/%3E %3Ccircle cx=%2248%22 cy=%2245%22 r=%2211%22 fill=%22%230a0f14%22/%3E %3Ccircle cx=%2248%22 cy=%2245%22 r=%229%22 fill=%22url(%23ok)%22/%3E %3Cpath d=%22M43.6 45.2l3.1 3.1 5.8-6.3%22 fill=%22none%22 stroke=%22%23fff%22 stroke-width=%222.8%22 stroke-linecap=%22round%22 stroke-linejoin=%22round%22/%3E %3C/svg%3E">
<script>window.__PROXY__=__PROXYJSON__;window.__TOKEN__="__TOKVAL__";</script>
<style>
:root{--bg:#0e1318;--card:#171e26;--line:#26313d;--text:#e6edf3;--mute:#8b98a5;--acc:#3b82f6;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444;--off:#a855f7}
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;background:var(--bg);color:var(--text);font-family:"Segoe UI",Tahoma,"Noto Kufi Arabic",Arial,sans-serif;font-size:14px}
header{padding:clamp(8px,1.2vw,16px) clamp(10px,1.6vw,26px);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
h1{font-size:20px;margin:0}
main{padding:clamp(8px,1.4vw,22px) clamp(8px,1.6vw,26px);max-width:none;margin:0}
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
.tw{overflow:auto;max-height:68vh;min-height:260px;border-radius:8px}
table{width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right;vertical-align:middle;word-break:break-word;overflow-wrap:anywhere}
th{color:var(--mute);font-weight:600;position:sticky;top:0;background:var(--card);z-index:1}
.thf{display:flex;align-items:center;gap:4px;justify-content:space-between}
.thf span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
th{white-space:nowrap}
.fbtn{background:#0a0e12;border:1px solid var(--line);color:var(--mute);border-radius:5px;padding:0 5px;font-size:11px;line-height:18px;cursor:pointer;flex:0 0 auto}
.fbtn:hover{color:var(--text);border-color:var(--acc)}
.fbtn.on{background:var(--acc);color:#fff;border-color:var(--acc)}
#fmenu{position:fixed;z-index:60;width:250px;max-height:420px;display:none;flex-direction:column;gap:6px;padding:10px}
#fmenu.on{display:flex}
#fmenu .fl{overflow:auto;max-height:260px;border:1px solid var(--line);border-radius:8px;padding:4px 6px}
#fmenu label{display:flex;gap:6px;align-items:center;padding:2px;cursor:pointer;font-size:13px}
#fmenu label span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#fmenu label em{color:var(--mute);font-style:normal;font-size:11px}
#fmenu .frow{display:flex;gap:6px}
#fmenu .frow button{flex:1;padding:5px 8px;font-size:12px}
#clrF{display:none}
#clrF.on{display:inline-block}
tr:hover td{background:#1c2530}
tr.dragging{opacity:.4}
tr.dragmate{opacity:.4}
tr.bestrow td{background:#132617}
tr.chrow td{background:#2b2108}
tr.hi td{background:#16325c !important;transition:background .3s}
tr.chrow td:first-child{box-shadow:inset -4px 0 0 #f59e0b}
#chbox{display:flex;gap:8px;align-items:center;flex-wrap:wrap;background:#1a1606;border:1px solid #5b4a12;border-radius:10px;padding:8px 12px;margin-bottom:12px}
#chbox input[type=search]{flex:1 1 260px;min-width:0}
.chchip{background:#2b2108;border:1px solid #5b4a12;color:#fcd34d;border-radius:14px;padding:2px 10px;font-size:12px;cursor:pointer}
.chchip i{font-style:normal;margin-inline-start:6px;color:var(--mute)}
.chbtn{background:#4a3208;color:#fcd34d;border:1px solid #92400e;border-radius:6px;padding:2px 8px;font-size:12px;cursor:pointer}
.chlist2 .it{display:flex;gap:8px;align-items:center;padding:5px 4px;border-bottom:1px solid var(--line)}
.chlist2 .it span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chlist2 .it em{color:var(--mute);font-style:normal;font-size:11px}
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
.namein{width:100%;min-width:70px;background:#0a0e12;border:1px solid var(--line);color:var(--text);border-radius:6px;padding:5px 7px;font-weight:600}
.namein:hover{border-color:var(--mute)}
.namein:focus{background:#0a0e12;border-color:var(--acc);outline:none}
.datain{width:100%;min-width:80px;background:transparent;border:1px solid transparent;color:var(--text);border-radius:6px;padding:4px 6px;font:inherit;font-size:13px}
.datain:hover{border-color:var(--line);background:#0a0e12}
.datain:focus{background:#0a0e12;border-color:var(--acc);outline:none}
td.hostcell{min-width:200px}
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
.modal{background:var(--card);border:1px solid var(--line);border-radius:14px;width:100%;max-width:min(96vw,1800px);max-height:96vh;display:flex;flex-direction:column;overflow:hidden}
.mhead{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.mbody{display:flex;gap:12px;padding:12px;min-height:0;flex:1;overflow:hidden}
.mvid{flex:2;min-width:0;display:flex;flex-direction:column;gap:8px}
.mlist{flex:1;min-width:240px;max-width:clamp(260px,26vw,460px);display:flex;flex-direction:column;gap:8px;min-height:0}
.ov video{width:100%;background:#000;border-radius:8px;aspect-ratio:16/9;max-height:72vh}
.chlist{overflow:auto;border:1px solid var(--line);border-radius:8px;flex:1;min-height:130px}
.chlist{position:relative}
#pvSp{position:relative;width:100%}
.ch{position:absolute;left:0;right:0;height:34px;padding:0 10px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px;display:flex;align-items:center;gap:7px}
.ch:hover{background:#1c2530}
.ch.sel{background:#16325c;color:#93c5fd}
.ch.hi{outline:1px solid var(--acc);outline-offset:-1px}
.ch.hdr{background:#0a0e12;color:var(--mute);font-weight:700;cursor:default}
.ch img{width:22px;height:22px;object-fit:contain;border-radius:4px;background:#0a0e12;flex:0 0 auto}
.ch .tx{overflow:hidden;text-overflow:ellipsis;flex:1}
.ch .rt{color:var(--warn);font-size:11px;flex:0 0 auto}
.row2{display:flex;gap:6px}
.row2 input{flex:1;min-width:0}
.vwrap{position:relative;background:#000;border-radius:8px}
.vwrap:fullscreen video{max-height:100vh;height:100vh;border-radius:0}
body.nfs #pvWrap{position:fixed;inset:0;z-index:100;border-radius:0}
body.nfs #pvVideo{width:100vw;height:100vh;max-height:100vh;border-radius:0}
.pvstats{position:absolute;top:6px;right:6px;background:rgba(0,0,0,.68);color:#d1fae5;font:11px/1.5 Consolas,monospace;padding:6px 8px;border-radius:6px;display:none;direction:ltr;white-space:pre;pointer-events:none}
.pvstats.on{display:block}
.pvinfo{display:flex;gap:10px;align-items:flex-start;font-size:13px}
.pvinfo img{max-height:130px;border-radius:6px;flex:0 0 auto}
.pvinfo .pt{color:var(--mute);font-size:12px;line-height:1.5;max-height:7.5em;overflow:auto}
.logo{flex:0 0 auto}
.hact{margin-inline-start:auto;display:flex;gap:6px;flex-wrap:wrap}
#upd{display:none;background:#16325c;border:1px solid var(--acc);border-radius:8px;padding:8px 12px;margin:12px 24px 0;color:#bfdbfe}
#upd.on{display:block}
#bulk{display:none;gap:8px;align-items:center;flex-wrap:wrap;background:#16325c;border:1px solid var(--acc);border-radius:10px;padding:8px 12px;margin-bottom:10px}
#bulk.on{display:flex}
#bulk button{padding:6px 12px;font-size:13px}
.typesel{background:#0a0e12;color:var(--mute);border:1px solid var(--line);border-radius:5px;font-size:11px;padding:0 2px;margin-inline-end:4px;max-width:70px}
.chg{display:inline-block;font-size:11px;border-radius:6px;padding:0 5px;margin-top:3px}
.chg.bad{background:#4c1414;color:#fca5a5}.chg.ok{background:#123d24;color:#86efac}.chg.info{background:#16325c;color:#93c5fd}
.q-excellent{background:#123d24;color:#86efac}.q-good{background:#233d17;color:#bef264}.q-weak{background:#4a3208;color:#fcd34d}
.q-none{background:#4c1414;color:#fca5a5}.q-unknown{background:#26313d;color:var(--mute)}
.score{font-weight:700}
.ordin{width:38px;padding:2px 3px;text-align:center;font-size:12px;margin-inline-start:3px;background:#0a0e12;border:1px solid var(--line);border-radius:5px;color:var(--text)}
.ordin:focus{border-color:var(--acc);outline:none}
.adb{display:inline-block;background:#4c1414;color:#fecaca;border:1px solid #991b1b;border-radius:6px;padding:0 4px;font-size:11px;margin-inline-end:3px;cursor:help}
.tagb{display:inline-block;background:#2a2147;color:#c4b5fd;border-radius:6px;padding:0 5px;font-size:11px;margin-inline-start:4px}
.menu{position:absolute;z-index:40;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px;display:none;min-width:180px;box-shadow:0 8px 24px rgba(0,0,0,.4)}
.menu.on{display:block}
.menu label{display:flex;gap:6px;padding:3px 2px;color:var(--text);cursor:pointer}
#toast.act{pointer-events:auto}
#toast .tbtn{margin-inline-start:12px;padding:3px 12px;font-size:13px}
.mgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px 12px;margin:10px 0}
.mgrid>div{background:#0a0e12;border:1px solid var(--line);border-radius:8px;padding:6px 10px;overflow-wrap:anywhere}
.mgrid b{display:block;color:var(--mute);font-size:11px;font-weight:600}
.mpad{padding:14px 16px;overflow:auto}
.tbl2{width:100%;font-size:12px;table-layout:auto}
.tbl2 th,.tbl2 td{padding:4px 6px}
.fld{display:flex;flex-direction:column;gap:4px;margin:8px 0}
.fld input[type=text],.fld select{width:100%}
.srow{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px}
.hint{color:var(--mute);font-size:12px}
.modal.sm{max-width:560px}
.tabs{display:flex;gap:4px}
.tab{flex:1;background:#0a0e12;border:1px solid var(--line);color:var(--mute);border-radius:7px;padding:6px 4px;font-size:12px;cursor:pointer;font-weight:600}
.tab.sel{background:var(--acc);color:#fff;border-color:var(--acc)}
.mbody{min-height:0}
#ov .modal{height:96vh}
#ov .mlist .chlist{flex:1;min-height:200px;max-height:none}
body.compact #inCard{display:none}
#uam,#chm{z-index:60}
@media(min-width:1900px){body{font-size:15px}table{font-size:14px}}
@media(max-width:760px){.mbody{flex-direction:column}.mlist{max-width:none}.ov video{max-height:40vh}}
</style>
</head>
<body>
<header>
  <svg class="logo" viewBox="0 0 64 64" width="40" height="40" aria-hidden="true">
  <defs>
    <linearGradient id="lg-bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#16202b"/><stop offset="1" stop-color="#0a0f14"/></linearGradient>
    <linearGradient id="lg-scr" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3b82f6"/><stop offset="1" stop-color="#1d4ed8"/></linearGradient>
    <linearGradient id="lg-ok" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#4ade80"/><stop offset="1" stop-color="#16a34a"/></linearGradient>
  </defs>
  <rect width="64" height="64" rx="14" fill="url(#lg-bg)"/>
  <path d="M24 10.5l8 6.5 8-6.5" fill="none" stroke="#93c5fd" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
  <rect x="8" y="17" width="48" height="32" rx="6" fill="url(#lg-scr)"/>
  <rect x="11.5" y="20.5" width="41" height="25" rx="3.5" fill="#0b1220"/>
  <path d="M14.5 40.5V26.5l6.3 8.2 6.3-8.2v14" fill="none" stroke="#e6edf3" stroke-width="3.6" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="34.6" cy="34.6" r="5" fill="none" stroke="#60a5fa" stroke-width="3.6"/>
  <rect x="25" y="51.5" width="14" height="3" rx="1.5" fill="#3b82f6"/>
  <circle cx="48" cy="45" r="11" fill="#0a0f14"/>
  <circle cx="48" cy="45" r="9" fill="url(#lg-ok)"/>
  <path d="M43.6 45.2l3.1 3.1 5.8-6.3" fill="none" stroke="#fff" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
  <div>
    <h1 class="ltr" style="letter-spacing:.3px">Mo <span style="color:var(--acc)">IPTV</span> Checker</h1>
    <span class="small">فاحص سيرفرات IPTV — يعمل محلياً على جهازك — الإصدار <span class="ltr">__VER__</span></span>
  </div>
  <div class="hact">
    <button class="sec" id="btnCompact" title="إخفاء/إظهار خانة الإدخال لإعطاء الجدول مساحة أكبر">⬍ تكبير الجدول</button>
    <button class="sec" id="btnLog" title="سجل التشخيص">🧾 السجل</button>
    <button class="sec" id="btnSet" title="الإعدادات">⚙️ الإعدادات</button>
  </div>
</header>
<div id="upd"></div>
<main>
<div class="card" id="inCard">
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
    <label class="opt" title="يجرّب بث 2-3 قنوات فعلياً ويقيس الجودة (أبطأ)"><input type="checkbox" id="deep"> فحص بث فعلي (عميق)</label>
    <label class="opt"><input type="checkbox" id="countch"> عدّ القنوات</label>
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
  <div id="chbox">
    <b>🔎 بحث في محتوى السيرفرات</b>
    <input type="search" id="chq" placeholder="مثال: bein, بي ان سبورت, Breaking Bad — افصل بين الكلمات بفاصلة">
    <label class="opt"><input type="checkbox" id="chkLive" checked> 📺 قنوات</label>
    <label class="opt"><input type="checkbox" id="chkVod" checked> 🎬 أفلام</label>
    <label class="opt"><input type="checkbox" id="chkSeries" checked> 📂 مسلسلات</label>
    <label class="opt" title="يجرّب تشغيل عنصر مطابق فعلياً للتأكد أنه يبث"><input type="checkbox" id="chVerify"> تأكد من البث</label>
    <button id="btnChSearch">🔎 ابحث</button>
    <button class="sec" id="btnChClear" title="إزالة نتائج البحث في القنوات">✕ مسح</button>
    <span id="chSaved" style="display:flex;gap:6px;flex-wrap:wrap"></span>
  </div>
  <div class="row" style="margin-top:0;margin-bottom:12px">
    <label class="opt">الترتيب
      <select id="sort">
        <option value="orig">يدوي / حسب الإدخال (اسحب للترتيب)</option>
        <option value="daysDesc">الأطول صلاحية أولاً</option>
        <option value="daysAsc">الأقرب انتهاءً أولاً</option>
        <option value="state">حسب الحالة</option>
        <option value="ping">الأسرع استجابة</option>
        <option value="score">الأعلى تقييماً</option>
        <option value="chs">الأكثر قنوات مطابقة</option>
      </select>
    </label>
    <input type="search" id="q" placeholder="بحث في السيرفر أو المستخدم..." style="flex:1 1 160px;min-width:0">
    <button class="sec" id="btnReverse">⇅ عكس الترتيب</button>
    <button class="sec" id="btnDedup">🧹 حذف المكرر (إبقاء الأفضل)</button>
    <span style="flex:1"></span>
    <button class="sec" id="btnAdd">➕ إضافة سيرفر</button>
    <button class="sec" id="btnRecheck">🔁 إعادة فحص غير الشغالة</button>
    <button class="sec" id="btnCopy">📋 نسخ روابط الشغالة</button>
    <button class="sec" id="btnM3uFile">📃 ملف M3U للشغّالة</button>
    <span style="position:relative"><button class="sec" id="btnCols">🧱 الأعمدة</button><div class="menu" id="colmenu"></div></span>
    <button class="sec" id="clrF" title="إزالة كل فلاتر الأعمدة">🧹 مسح فلاتر الأعمدة</button>
    <button class="sec" id="btnCsv">⬇️ تصدير إلى إكسل</button>
    <button class="sec" id="btnSave">💾 حفظ مشروع</button>
    <button class="sec" id="btnImport">📁 استيراد مشروع</button>
    <button class="sec" id="btnIptv">📤 تصدير إلى iptvnator</button>
    <input type="file" id="proj" accept=".json,application/json" hidden>
  </div>
  <div id="bulk">
    <b>المحدد: <span id="bulkN">0</span></b>
    <button id="bkCheck">🔁 فحص المحدد</button>
    <button class="sec" id="bkXlsx">⬇️ إكسل</button>
    <button class="sec" id="bkIptv">📤 iptvnator</button>
    <button class="sec" id="bkM3u">📃 ملف M3U</button>
    <button class="sec" id="bkLinks">📋 نسخ الروابط</button>
    <button class="sec" id="bkUa">🪪 هوية المشغّل</button>
    <button class="danger" id="bkDel">🗑 حذف المحدد</button>
    <button class="sec" id="bkClear">إلغاء التحديد</button>
  </div>
  <div class="tw">
    <table>
      <colgroup id="cg"></colgroup>
      <thead><tr id="hr"></tr></thead>
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
      <select id="pvSrv" title="تبديل السيرفر مع بقاء البحث والقسم" style="max-width:190px"></select>
      <span class="small ltr" id="pvHost" style="flex:1;overflow:hidden;text-overflow:ellipsis;min-width:60px"></span>
      <button class="sec" id="pvFav" title="إضافة/إزالة من المفضلة">☆</button>
      <button class="sec" id="pvTx" title="تحويل لحظي عبر ffmpeg (للترميزات غير المدعومة)">🎞 تحويل</button>
      <button class="sec" id="pvPip" title="صورة داخل صورة">PiP</button>
      <button class="sec" id="pvFs" title="ملء الشاشة (F)">⛶</button>
      <button class="sec" id="pvSt" title="معلومات البث الحية">ℹ️</button>
      <button class="sec" id="pvExt" title="فتح رابط القناة في مشغل خارجي">فتح خارجياً</button>
      <button class="sec" id="pvCopy">نسخ الرابط</button>
      <button class="danger" id="pvClose">إغلاق ✕</button>
    </div>
    <div class="mbody">
      <div class="mvid">
        <div class="vwrap" id="pvWrap">
          <video id="pvVideo" controls autoplay playsinline></video>
          <div class="pvstats" id="pvStats"></div>
        </div>
        <div class="small" id="pvMsg">اختر قناة من القائمة لبدء التشغيل. (↑↓ تبديل، مسافة إيقاف، F ملء الشاشة، M كتم، أرقام للقفز)</div>
        <div class="pvinfo" id="pvInfo"></div>
      </div>
      <div class="mlist">
        <div class="tabs" id="pvTabs">
          <button class="tab sel" data-k="live">📺 مباشر</button>
          <button class="tab" data-k="vod">🎬 أفلام</button>
          <button class="tab" data-k="series">📂 مسلسلات</button>
          <button class="tab" data-k="fav">⭐</button>
          <button class="tab" data-k="recent">🕘</button>
          <button class="tab" data-k="all" title="بحث شامل في القنوات والأفلام والمسلسلات">🔎 الكل</button>
        </div>
        <button class="sec" id="pvBack" style="display:none">↪ رجوع لقائمة المسلسلات</button>
        <div class="row2"><input type="search" id="pvQ" placeholder="ابحث..."><button class="sec" id="pvRefresh" title="إعادة جلب القوائم من السيرفر">🔄</button></div>
        <select id="pvCat"><option value="">كل التصنيفات</option></select>
        <div class="chlist" id="pvChs"><div id="pvSp"></div></div>
        <div class="small" id="pvEmpty"></div>
      </div>
    </div>
  </div>
</div>

<div class="menu" id="fmenu"></div>

<div class="ov" id="uam"><div class="modal sm">
  <div class="mhead"><b>🪪 هوية المشغّل (User-Agent)</b><span class="small" id="uamFor" style="flex:1"></span><button class="danger" id="uamClose">إغلاق ✕</button></div>
  <div class="mpad">
    <div class="hint">بعض السيرفرات ترفض الاتصال إلا من مشغّلات معيّنة. اختر الهوية التي يتصل بها البرنامج بهذا السيرفر في الفحص والمشغّل وجلب القنوات.</div>
    <div class="fld"><label>الهوية</label><select id="uaSel">
      <option value="">تلقائي (الافتراضي — يجرّب أكثر من هوية)</option>
      <option value="VLC/3.0.20 LibVLC/3.0.20">VLC</option>
      <option value="IPTVSmartersPro">IPTV Smarters Pro</option>
      <option value="TiviMate/4.7.0 (Android 11)">TiviMate</option>
      <option value="OTT Navigator/1.6.9.4 (Linux;Android 11)">OTT Navigator</option>
      <option value="Kodi/20.2 (Windows NT 10.0; Win64; x64) App_Bitness/64 Version/20.2">Kodi</option>
      <option value="Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3">MAG (جهاز ستالكر)</option>
      <option value="okhttp/4.9.0">okhttp (تطبيقات أندرويد)</option>
      <option value="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36">Chrome</option>
      <option value="__custom">مخصص...</option>
    </select></div>
    <div class="fld" id="uaCustomRow"><label>النص المخصص</label><input type="text" id="uaCustom" class="ltr" placeholder="MyPlayer/1.0"></div>
    <div class="row" style="margin:6px 0"><button class="sec" id="uaDetect">🔎 اكتشف الهوية المناسبة تلقائياً</button><span class="small" id="uaDetMsg"></span></div>
    <div id="uaRes"></div>
    <label class="opt"><input type="checkbox" id="uaRecheck" checked> أعد الفحص بعد الحفظ</label>
    <div class="row"><button id="uaSave">حفظ</button></div>
  </div>
</div></div>

<div class="ov" id="chm"><div class="modal sm" style="max-width:640px">
  <div class="mhead"><b>📺 القنوات المطابقة</b><span class="small ltr" id="chmHost" style="flex:1;overflow:hidden;text-overflow:ellipsis"></span><button class="danger" id="chmClose">إغلاق ✕</button></div>
  <div class="mpad"><div class="small" id="chmInfo"></div><input type="search" id="chmQ" placeholder="تصفية..." style="width:100%;margin:8px 0"><div class="chlist2" id="chmList"></div></div>
</div></div>

<div class="ov" id="dt"><div class="modal sm" style="max-width:760px">
  <div class="mhead"><b>🔍 تفاصيل السيرفر</b><span class="small ltr" id="dtHost" style="flex:1;overflow:hidden;text-overflow:ellipsis"></span><button class="danger" id="dtClose">إغلاق ✕</button></div>
  <div class="mpad" id="dtBody"></div>
</div></div>

<div class="ov" id="ad"><div class="modal sm">
  <div class="mhead"><b>➕ إضافة سيرفر يدوياً</b><span style="flex:1"></span><button class="danger" id="adClose">إغلاق ✕</button></div>
  <div class="mpad">
    <div class="fld"><label>النوع</label><select id="adType"><option value="xtream">Xtream (عنوان + مستخدم + كلمة مرور)</option><option value="stalker">Stalker / MAC</option><option value="m3u">رابط M3U</option></select></div>
    <div class="fld"><label id="adL1">عنوان السيرفر</label><input type="text" id="adHost" class="ltr" placeholder="http://example.com:8080"></div>
    <div class="fld" id="adRow2"><label id="adL2">اسم المستخدم</label><input type="text" id="adUser" class="ltr"></div>
    <div class="fld" id="adRow3"><label>كلمة المرور</label><input type="text" id="adPass" class="ltr"></div>
    <div class="fld"><label>الاسم في iptvnator (اختياري)</label><input type="text" id="adName"></div>
    <div class="row"><button id="adGo">إضافة وفحص</button></div>
  </div>
</div></div>

<div class="ov" id="st"><div class="modal sm">
  <div class="mhead"><b>⚙️ الإعدادات</b><span style="flex:1"></span><button class="danger" id="stClose">إغلاق ✕</button></div>
  <div class="mpad">
    <div class="srow">
      <div class="fld"><label>المهلة بالثواني</label><input type="number" id="sTmo" min="3" max="120"></div>
      <div class="fld"><label>عدد الفحوص المتزامنة</label><input type="number" id="sConc" min="1" max="50"></div>
      <div class="fld"><label>حد الفحوص لكل نطاق (حماية من الحظر)</label><input type="number" id="sDom" min="1" max="10"></div>
      <div class="fld"><label>فحص دوري تلقائي كل (ساعات، 0 = متوقف)</label><input type="number" id="sAuto" min="0" max="168"></div>
    </div>
    <label class="opt"><input type="checkbox" id="sDeep"> تفعيل الفحص العميق افتراضياً</label>
    <label class="opt"><input type="checkbox" id="sCount"> تفعيل عدّ القنوات افتراضياً</label>
    <label class="opt"><input type="checkbox" id="sUaAuto"> عند رفض السيرفر للاتصال: جرّب هويات مشغّل أخرى تلقائياً واحفظ الناجحة</label>
    <label class="opt"><input type="checkbox" id="sAdult"> تمييز السيرفرات التي تحتوي محتوى للكبار 🔞 (يفحص أسماء التصنيفات بعد كل فحص)</label>
    <label class="opt"><input type="checkbox" id="sUpd"> التحقق من وجود تحديثات عند التشغيل</label>
    <hr style="border-color:var(--line)">
    <div class="srow">
      <div class="fld"><label>المشغّل الخارجي</label><select id="sPlayer"><option value="auto">تلقائي (VLC ثم الافتراضي)</option><option value="vlc">VLC</option><option value="mpv">mpv</option><option value="iina">IINA (ماك)</option><option value="potplayer">PotPlayer (ويندوز)</option><option value="custom">مسار مخصص</option></select></div>
      <div class="fld"><label>مسار المشغّل المخصص</label><input type="text" id="sPlayerPath" class="ltr"></div>
    </div>
    <div class="fld"><label>محرك نافذة البرنامج (يُطبَّق عند إعادة فتح البرنامج)</label><select id="sEngine">
      <option value="auto">تلقائي (نافذة مستقلة إن توفرت، وإلا كروم)</option>
      <option value="webview">نافذة مستقلة — محرك سفاري على ماك / إيدج على ويندوز</option>
      <option value="chrome">نافذة كروم / إيدج (تدعم ملفات MKV أكثر، أيقونتها أيقونة كروم)</option>
      <option value="browser">المتصفح العادي (تبويب في متصفحك الافتراضي)</option>
    </select></div>
    <label class="opt"><input type="checkbox" id="sDirect"> أرسل الرابط المباشر للسيرفر للمشغّل الخارجي (بدل الوسيط المحلي) عند عدم استخدام VPN</label>
    <div class="fld"><label>مسار ffmpeg (اختياري — يُكتشف تلقائياً)</label><input type="text" id="sFfmpeg" class="ltr" placeholder="/opt/homebrew/bin/ffmpeg"></div>
    <div class="hint" id="sCaps"></div>
    <div class="hint ltr" id="sDir" style="margin-top:6px"></div>
    <div class="row"><button class="sec" id="sUpdNow">🔄 التحقق من التحديثات الآن</button></div>
    <div class="row"><button id="sSave">حفظ</button></div>
  </div>
</div></div>

<div class="ov" id="lg"><div class="modal">
  <div class="mhead"><b>🧾 سجل التشخيص</b><span class="small">(بدون كلمات مرور)</span><span style="flex:1"></span>
    <button class="sec" id="lgRefresh">🔄 تحديث</button><button class="sec" id="lgCopy">📋 نسخ</button><button class="sec" id="lgClear">🧹 مسح</button><button class="danger" id="lgClose">إغلاق ✕</button></div>
  <div class="mpad"><table class="tbl2 ltr" id="lgTb"></table></div>
</div></div>

<script>
const $ = s => document.querySelector(s);
const ST = {
  pending:['في الانتظار','s-pending'], checking:['جاري الفحص…','s-checking'],
  active:['يعمل ✅','s-active'], active_m3u:['يعمل (بدون تاريخ)','s-active'],
  expired:['منتهي ⌛','s-expired'], invalid:['بيانات خاطئة','s-invalid'],
  blocked:['محظور / معطّل','s-invalid'], offline:['لا يستجيب ❌','s-offline'], error:['خطأ','s-offline']
};
const FILTERS = [['all','الإجمالي'],['ok','يعمل'],['expired','منتهي'],['bad','بيانات خاطئة / محظور'],['offline','لا يستجيب / خطأ'],['pending','لم يُفحص'],['viaproxy','تعمل عبر VPN'],['soon7','ينتهي ≤ 7 أيام'],['soon30','ينتهي ≤ 30 يوماً'],['chmatch','📺 يحتوي القنوات المطلوبة'],['adult','🔞 يحتوي محتوى للكبار']];
const ORDER = {active:0,active_m3u:1,expired:2,blocked:3,invalid:4,offline:5,error:6,checking:7,pending:8};
let rows = [], seq = 0, stop = false, running = false, filter = 'all', renderQueued = false;
let SET = {per_domain:2, ffmpeg_path:'', ext_player:'auto', ext_player_path:'', ext_direct:false, updates:true, auto_hours:0, deep_default:false, count_default:false, timeout:15, conc:8};
let CAPS = {};
let UI = {hidden:['conn','msg'], vol:1, favs:{}, recents:{}, resume:{}, lastAuto:0, chTerms:[], chq:''};
const COLS = [
  ['sel','☑',34,false],['n','#',76,false],['st','الحالة',106,false],['host','السيرفر',170,false],['user','المستخدم',90,true],['pass','كلمة المرور',90,true],
  ['exp','الانتهاء',96,true],['days','الأيام',88,true],['conn','الاتصالات',80,true],['ping','الاستجابة',90,true],['q','الجودة',88,true],['score','التقييم',78,true],
  ['chs','القنوات المطابقة',110,true],['note','وسوم / ملاحظة',90,true],['msg','ملاحظات الفحص',110,true],['name','الاسم في iptvnator',100,true],['acts','',176,false]
];
const COLNAME = {chs:'القنوات المطابقة',user:'المستخدم',pass:'كلمة المرور',exp:'تاريخ الانتهاء',days:'الأيام المتبقية',conn:'الاتصالات',ping:'الاستجابة',q:'جودة البث',score:'التقييم',note:'وسوم / ملاحظة',msg:'ملاحظات الفحص',name:'الاسم في iptvnator'};
const hasCh = () => rows.some(r => r.chs);
const shownCols = () => COLS.filter(c => !(c[3] && UI.hidden.includes(c[0])) && !(c[0]==='chs' && !hasCh()));

const grp = s => (s==='active'||s==='active_m3u')?'ok':s==='expired'?'expired':(s==='invalid'||s==='blocked')?'bad':(s==='offline'||s==='error')?'offline':'pending';
function defaultName(host){
  let h = String(host||'').replace(/^https?:\/\//i,'').replace(/\/.*$/,'').replace(/:\d+$/,'');
  if(!/^\d{1,3}(\.\d{1,3}){3}$/.test(h)) h = h.replace(/\.[a-z]{2,}$/i,'');
  return h;
}
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const hasPort = h => /:\d+\/?$/.test(String(h||''));
const stripPort = h => String(h||'').toLowerCase().replace(/\/+$/,'').replace(/:\d+$/,'');
function keyHost(h){ try{ return (document.getElementById('noport') && document.getElementById('noport').checked) ? stripPort(h) : String(h||'').toLowerCase().replace(/\/+$/,''); }catch(e){ return stripPort(h); } }
const key = a => {
  if(a.type==='stalker') return 'stalker|'+keyHost(a.portal||a.host)+'|'+String(a.mac||a.username||'').toUpperCase();
  if(a.type==='m3u') return 'm3u|'+String(a.m3uurl||a.host||'').toLowerCase().replace(/\/+$/,'');
  return keyHost(a.host)+'|'+(a.username||'')+'|'+(a.password||'');
};
const msg = t => $('#msg').textContent = t || ' ';
function toast(t, label, fn, ms){
  const el=$('#toast'); el.textContent=t; el.classList.add('on');
  el.classList.toggle('act', !!fn);
  if(fn){ const b=document.createElement('button'); b.className='tbtn'; b.textContent=label; b.onclick=()=>{ el.classList.remove('on'); fn(); }; el.appendChild(b); }
  clearTimeout(toast._t); toast._t=setTimeout(()=>el.classList.remove('on'), ms || (fn ? 9000 : 1800));
}
async function api(path, body){
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-IPTV-Checker':'1'}, body:JSON.stringify(body||{})});
  if(!r.ok) throw new Error('HTTP '+r.status);
  return r.json();
}
const proxyOf = r => (r && r.viaProxy && r.proxyUsed) ? r.proxyUsed : '';
const tnum = n => (n==null||n==='') ? '—' : Number(n).toLocaleString('en-US');

function daysTxt(r){
  if(r.unlimited) return '<span style="color:var(--ok)">غير محدود ∞</span>';
  if(r.days_left == null) return '—';
  const d = r.days_left;
  if(d < 0) return `<span style="color:var(--bad)">انتهى منذ ${-d} يوم</span>`;
  if(d === 0) return '<span style="color:var(--warn)">ينتهي اليوم</span>';
  return `<span style="color:${d<=7?'var(--warn)':'var(--ok)'}">${d} يوم</span>`;
}

// تقييم السيرفر من 100: صلاحية + استجابة + اتصالات + جودة البث الفعلي
function scoreOf(r){
  if(grp(r.state)!=='ok') return null;
  let s = 40;
  s += r.unlimited ? 25 : (r.days_left!=null ? Math.max(0, Math.min(25, Math.round(r.days_left*25/180))) : 8);
  if(r.ping_ms!=null) s += r.ping_ms<400 ? 10 : r.ping_ms<1000 ? 5 : 0;
  if(r.max_cons!=null){ if(r.max_cons>=2) s += 5; if((r.max_cons - (r.active_cons||0))>0) s += 5; }
  if(r.channels) s += 5;
  const q = r.quality;
  if(q==='excellent') s += 15; else if(q==='good') s += 8; else if(q==='none') s -= 30;
  return Math.max(0, Math.min(100, s));
}
const QL = {excellent:['ممتازة','q-excellent'], good:['جيدة','q-good'], weak:['ضعيفة','q-weak'], none:['لا بث','q-none']};
function changeBadge(r){
  const p = r.prev; if(!p) return '';
  const was = grp(p.s), now = grp(r.state), out = [];
  if(was==='ok' && now!=='ok' && now!=='pending') out.push('<span class="chg bad" title="كان يعمل في الفحص السابق">⬇ توقف</span>');
  else if(was!=='ok' && now==='ok') out.push('<span class="chg ok" title="لم يكن يعمل سابقاً">⬆ عاد للعمل</span>');
  if(p.e && r.exp_ts && r.exp_ts > p.e + 3600) out.push('<span class="chg info" title="تغيّر تاريخ الانتهاء">🔄 جُدّد</span>');
  return out.join(' ');
}

let dupInfo = {};
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
const TYPES = [['xtream','Xtream'],['stalker','Stalker'],['m3u','M3U']];
function cellHtml(c, r, i){
  const [lbl, cls] = ST[r.state] || ST.error;
  switch(c){
    case 'sel': return `<td><input type="checkbox" class="rsel" ${r.sel?'checked':''}></td>`;
    case 'n': return `<td class="drag" title="اسحب لإعادة الترتيب، أو اكتب رقم الترتيب ثم Enter"><span class="dh">⠿</span><input type="text" inputmode="numeric" class="ordin ltr" data-id="${r.id}" value="${i+1}" data-v="${i+1}"></td>`;
    case 'st': return `<td><span class="b ${cls}">${lbl}</span>${changeBadge(r) ? '<br>'+changeBadge(r) : ''}</td>`;
    case 'host': {
      const di = dupInfo[r.id];
      const dupBadge = di ? `<span class="dup" title="سيرفر مكرر بمستخدمين مختلفين (${di.count} مرات)">🔁${di.count}</span>${di.best?'<span class="best" title="الأطول صلاحية بين المكرر">⭐</span>':''}` : '';
      const adb = (r.adult && r.adult.adult) ? `<span class="adb" title="يحتوي محتوى للكبار:\n${esc(r.adult.hits.slice(0,10).join('\n'))}">🔞</span>` : '';
      const via = adb + (r.viaProxy ? `<span class="viap" title="يعمل عبر VPN فقط">🛡️ VPN</span>` : '') + (r.ua ? `<span class="viap" title="هوية مخصصة: ${esc(r.ua)}">🪪</span>` : '');
      const t = r.type || 'xtream';
      const ts = `<select class="typesel" title="نوع السيرفر — غيّره ثم أعد الفحص">${TYPES.map(([v,l])=>`<option value="${v}" ${v===t?'selected':''}>${l}</option>`).join('')}</select>`;
      return `<td class="ltr hostcell">${ts}${via}${dupBadge}<input type="text" class="datain ltr" data-id="${r.id}" data-f="host" value="${esc(r.host)}" title="عنوان السيرفر — عدّله ثم أعد الفحص 🔁"></td>`;
    }
    case 'user': return `<td><input type="text" class="datain ltr" data-id="${r.id}" data-f="username" value="${esc(r.username)}"></td>`;
    case 'pass': return `<td><input type="text" class="datain ltr" data-id="${r.id}" data-f="password" value="${esc(r.password)}"></td>`;
    case 'exp': return `<td class="ltr">${r.exp_date ? esc(r.exp_date) : (r.unlimited ? '∞' : '—')}</td>`;
    case 'days': return `<td>${daysTxt(r)}</td>`;
    case 'conn': return `<td class="ltr">${(r.max_cons!=null || r.active_cons!=null) ? `${r.active_cons ?? '?'} / ${r.max_cons ?? '?'}` : '—'}</td>`;
    case 'ping': return `<td class="ltr">${r.ping_ms!=null ? r.ping_ms+' ms' : '—'}</td>`;
    case 'q': {
      if(r.deepBusy) return '<td class="small">⏳</td>';
      const q = QL[r.quality]; if(!q) return '<td>—</td>';
      return `<td><span class="b ${q[1]}" title="${esc(r.deepMsg||'')}">${q[0]}</span>${r.kbps?`<div class="small ltr">${r.kbps} kbps</div>`:''}</td>`;
    }
    case 'score': { const s = scoreOf(r); return `<td class="score" style="color:${s==null?'var(--mute)':s>=75?'var(--ok)':s>=50?'var(--warn)':'var(--bad)'}">${s==null?'—':s}</td>`; }
    case 'chs': {
      const x = r.chs; if(!x) return '<td class="small">—</td>';
      if(x.busy) return '<td class="small">⏳ جاري البحث</td>';
      if(x.error) return `<td class="small" title="${esc(x.error)}" style="color:var(--off)">تعذّر</td>`;
      if(!x.count) return `<td class="small" title="من أصل ${x.total} قناة">لا يوجد</td>`;
      const v = x.verified, vb = !v ? '' : v.ok ? ' <span title="تم التأكد أن «'+esc(v.name)+'» تبث">✅</span>' : ' <span title="'+esc((v.name||'')+': '+(v.why||'لا تبث'))+'">⚠️</span>';
      const by = x.by || {live:x.count}, KI = {live:'📺', vod:'🎬', series:'📂'};
      const lbl = ['live','vod','series'].filter(k => by[k]).map(k => KI[k] + ' ' + by[k]).join(' ') || ('📺 ' + x.count);
      return `<td><button class="chbtn" data-act="chs" title="${esc((x.matches||[]).slice(0,8).map(m=>m.name).join('، '))}">${lbl}</button>${vb}</td>`;
    }
    case 'note': return `<td><input type="text" class="notein" data-id="${r.id}" value="${esc(r.note||'')}" placeholder="#وسم أو ملاحظة"></td>`;
    case 'msg': return `<td class="small note" title="${esc(r.message||'')}${r.scheme_note?' — '+esc(r.scheme_note):''}">${esc(r.message||'')}</td>`;
    case 'name': return `<td><input type="text" class="namein ltr" data-id="${r.id}" value="${esc(r.title || defaultName(r.host))}" placeholder="${esc(defaultName(r.host))}" title="اسم السيرفر عند التصدير إلى iptvnator"></td>`;
    case 'acts': return `<td class="acts"><button class="icon" data-act="up" title="للأعلى">▲</button><button class="icon" data-act="down" title="للأسفل">▼</button><button class="icon" data-act="play" title="تشغيل / معاينة">▶️</button><button class="icon" data-act="info" title="تفاصيل">🔍</button><button class="icon" data-act="copy" title="نسخ رابط M3U">📋</button><button class="icon" data-act="re" title="إعادة الفحص">🔁</button><button class="icon" data-act="del" title="حذف">✕</button></td>`;
  }
  return '<td></td>';
}
function rowHtml(r, i){
  const di = dupInfo[r.id];
  const cols = shownCols();
  return `<tr data-id="${r.id}" draggable="true" class="${di&&di.best?'bestrow':''} ${r.viaProxy?'viarow':''} ${r.chs&&r.chs.count>0?'chrow':''}">${cols.map(c => cellHtml(c[0], r, i)).join('')}</tr>`;
}
function renderHead(){
  const cols = shownCols();
  const FLEX = {host:3, user:1.2, pass:1.2, msg:1.8, note:1.1, name:1.4};
  const fixed = cols.filter(c => !FLEX[c[0]]).reduce((a,c) => a + c[2], 0);
  const flexW = cols.filter(c => FLEX[c[0]]).reduce((a,c) => a + FLEX[c[0]], 0);
  const avail = Math.max(0, ($('.tw').clientWidth || window.innerWidth) - fixed - 4);
  const minTotal = fixed + cols.filter(c => FLEX[c[0]]).reduce((a,c) => a + c[2], 0);
  $('#cg').innerHTML = cols.map(c => `<col style="width:${FLEX[c[0]] ? Math.max(c[2], Math.floor(avail*FLEX[c[0]]/flexW)) : c[2]}px">`).join('');
  $('#cg').closest('table').style.minWidth = minTotal + 'px';
  const cf = UI.colf || {};
  $('#hr').innerHTML = cols.map(c => c[0]==='sel' ? '<th><input type="checkbox" id="selAll" title="تحديد الكل"></th>'
    : FCOL[c[0]] ? `<th title="${c[1]}"><div class="thf"><span>${c[1]}</span><button class="fbtn ${cf[c[0]]?'on':''}" data-fc="${c[0]}" title="فلتر العمود">▾</button></div></th>` : `<th>${c[1]}</th>`).join('');
  $('#clrF').classList.toggle('on', Object.keys(cf).length>0);
}

// ---------------- فلاتر الأعمدة (مثل إكسل) ----------------
const bucketDays = r => r.unlimited ? 'غير محدود ∞' : r.days_left==null ? '— غير معروف' : r.days_left<0 ? 'منتهي' : r.days_left<=7 ? '0–7 أيام' : r.days_left<=30 ? '8–30 يوماً' : r.days_left<=90 ? '31–90 يوماً' : r.days_left<=365 ? '91–365 يوماً' : 'أكثر من سنة';
const FCOL = {
  st: r => (ST[r.state]||ST.error)[0],
  host: r => String(r.host||'').replace(/^https?:\/\//i,'').replace(/\/+$/,'').toLowerCase() || '—',
  user: r => r.username || '—',
  pass: r => r.password || '—',
  exp: r => r.exp_date || (r.unlimited ? '∞' : '—'),
  days: bucketDays,
  conn: r => r.max_cons!=null ? 'حد '+r.max_cons : '—',
  ping: r => r.ping_ms==null ? '—' : r.ping_ms<400 ? 'أقل من 400 ms' : r.ping_ms<1000 ? '400–1000 ms' : r.ping_ms<3000 ? '1–3 ث' : 'أكثر من 3 ث',
  q: r => (QL[r.quality]||['—'])[0],
  score: r => { const v = scoreOf(r); return v==null ? '—' : v>=75 ? '75–100' : v>=50 ? '50–74' : 'أقل من 50'; },
  note: r => (r.note||'').trim() || '— بدون',
  chs: r => !r.chs ? '—' : r.chs.error ? 'تعذّر' : r.chs.count>0 ? (r.chs.verified && r.chs.verified.ok ? 'يحتوي ✅ يبث' : 'يحتوي') : 'لا يحتوي',
  name: r => (r.title || defaultName(r.host)) || '—',
};
const passCol = (r, skip) => { const cf = UI.colf || {}; for(const c in cf){ if(c===skip || !FCOL[c]) continue; if(!cf[c].includes(FCOL[c](r))) return false; } return true; };
let fmCol = null;
function openFMenu(col, btn){
  fmCol = col;
  const base = baseList().filter(r => passCol(r, col));
  const cnt = new Map(); base.forEach(r => { const v = FCOL[col](r); cnt.set(v, (cnt.get(v)||0)+1); });
  const cur = (UI.colf||{})[col];
  (cur||[]).forEach(v => { if(!cnt.has(v)) cnt.set(v, 0); });
  const vals = [...cnt.keys()].sort((a,b) => String(a).localeCompare(String(b), 'ar', {numeric:true}));
  const m = $('#fmenu');
  m.innerHTML = `<input type="search" id="fmQ" placeholder="بحث في القيم...">
    <label><input type="checkbox" id="fmAll"> <span><b>تحديد الكل</b></span></label>
    <div class="fl">${vals.map(v => `<label><input type="checkbox" class="fmv" value="${esc(v)}" ${!cur||cur.includes(v)?'checked':''}> <span title="${esc(v)}">${esc(v)}</span><em>${cnt.get(v)}</em></label>`).join('')}</div>
    <div class="frow"><button id="fmOk">تطبيق</button><button class="sec" id="fmClr">مسح الفلتر</button></div>`;
  const syncAll = () => { const b = [...m.querySelectorAll('.fmv')].filter(x => x.closest('label').style.display!=='none'); $('#fmAll').checked = b.length>0 && b.every(x => x.checked); };
  syncAll();
  $('#fmQ').oninput = () => { const q = $('#fmQ').value.trim().toLowerCase(); m.querySelectorAll('.fmv').forEach(x => x.closest('label').style.display = !q || x.value.toLowerCase().includes(q) ? '' : 'none'); syncAll(); };
  $('#fmAll').onchange = () => { m.querySelectorAll('.fmv').forEach(x => { if(x.closest('label').style.display!=='none') x.checked = $('#fmAll').checked; }); };
  m.querySelector('.fl').onchange = syncAll;
  $('#fmOk').onclick = () => {
    const all = [...m.querySelectorAll('.fmv')], on = all.filter(x => x.checked).map(x => x.value);
    UI.colf = UI.colf || {};
    if(on.length===all.length) delete UI.colf[col]; else UI.colf[col] = on;
    closeFMenu(); render();
  };
  $('#fmClr').onclick = () => { if(UI.colf) delete UI.colf[col]; closeFMenu(); render(); };
  m.classList.add('on');
  const rc = btn.getBoundingClientRect(), w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, rc.right - w)) + 'px';
  m.style.top = Math.max(8, Math.min(window.innerHeight - h - 8, rc.bottom + 4)) + 'px';
  $('#fmQ').focus();
}
function closeFMenu(){ $('#fmenu').classList.remove('on'); fmCol = null; }
$('#fmenu').onclick = e => e.stopPropagation();
$('#fmenu').addEventListener('keydown', e => { if(e.key==='Enter'){ e.preventDefault(); $('#fmOk').click(); } });
document.addEventListener('click', closeFMenu);
window.addEventListener('resize', closeFMenu);
$('#clrF').onclick = () => { UI.colf = {}; render(); toast('تم مسح كل فلاتر الأعمدة'); };

function visible(){ return sortList(baseList().filter(r => passCol(r, null))); }
function baseList(){
  const q = $('#q').value.trim().toLowerCase();
  const fm = r => {
    if(filter==='all') return true;
    if(filter==='viaproxy') return !!r.viaProxy;
    if(filter==='chmatch') return !!(r.chs && r.chs.count>0);
    if(filter==='adult') return !!(r.adult && r.adult.adult);
    if(filter==='soon7' || filter==='soon30'){ const n = filter==='soon7' ? 7 : 30; return grp(r.state)==='ok' && !r.unlimited && r.days_left!=null && r.days_left>=0 && r.days_left<=n; }
    return grp(r.state)===filter;
  };
  return rows.filter(r => fm(r) && (!q || (r.host+' '+r.username+' '+(r.note||'')+' '+(r.title||'')).toLowerCase().includes(q)));
}
function sortList(list){
  const s = $('#sort').value;
  const dv = r => r.unlimited ? 1e9 : (r.days_left ?? -1e9);
  if(s==='daysDesc') list.sort((a,b)=>dv(b)-dv(a));
  else if(s==='daysAsc') list.sort((a,b)=>{const x=a.days_left??1e9, y=b.days_left??1e9; return (a.unlimited?1e9:x)-(b.unlimited?1e9:y);});
  else if(s==='state') list.sort((a,b)=>(ORDER[a.state]??9)-(ORDER[b.state]??9));
  else if(s==='ping') list.sort((a,b)=>(a.ping_ms??1e9)-(b.ping_ms??1e9));
  else if(s==='score') list.sort((a,b)=>(scoreOf(b)??-1)-(scoreOf(a)??-1));
  else if(s==='chs') list.sort((a,b)=>((b.chs&&b.chs.count)||0)-((a.chs&&a.chs.count)||0));
  return list;
}
const selected = () => rows.filter(r => r.sel);
function renderStats(){
  const c = {all:rows.length, ok:0, expired:0, bad:0, offline:0, pending:0, viaproxy:0, soon7:0, soon30:0, chmatch:0, adult:0};
  rows.forEach(r => { c[grp(r.state)]++; if(r.viaProxy) c.viaproxy++; if(r.chs && r.chs.count>0) c.chmatch++; if(r.adult && r.adult.adult) c.adult++;
    if(grp(r.state)==='ok' && !r.unlimited && r.days_left!=null && r.days_left>=0){ if(r.days_left<=7) c.soon7++; if(r.days_left<=30) c.soon30++; } });
  const col = {ok:'var(--ok)',expired:'var(--warn)',bad:'var(--bad)',offline:'var(--off)',pending:'var(--mute)',all:'var(--text)',viaproxy:'#6ee7b7',soon7:'var(--warn)',soon30:'var(--warn)',chmatch:'#fbbf24',adult:'#f87171'};
  const anyAdult = rows.some(r => r.adult && r.adult.adult);
  $('#stats').innerHTML = FILTERS.filter(([k]) => (k!=='chmatch' || hasCh()) && (k!=='adult' || anyAdult)).map(([k,l]) => `<div class="stat ${filter===k?'sel':''}" data-f="${k}"><b style="color:${col[k]}">${c[k]}</b><span class="small">${l}</span></div>`).join('');
  const n = selected().length;
  $('#bulk').classList.toggle('on', n>0); $('#bulkN').textContent = n;
}
function render(){
  computeDup();
  renderHead();
  const list = visible();
  $('#tb').innerHTML = list.map(rowHtml).join('');
  const sa = $('#selAll'); if(sa){ sa.checked = list.length>0 && list.every(r=>r.sel); }
  $('#empty').style.display = rows.length ? (list.length ? 'none' : 'block') : 'block';
  if(rows.length && !list.length) $('#empty').textContent = 'لا توجد نتائج مطابقة للتصفية الحالية.';
  renderStats();
  saveSoon();
}
// تحديث تدريجي: يعيد رسم الصفوف المتغيرة فقط أثناء الفحص
const dirty = new Set(); let lastFull = 0, flushT = 0;
function touch(r){ dirty.add(r.id); scheduleRender(); }
function scheduleRender(){ if(renderQueued) return; renderQueued = true; requestAnimationFrame(flush); }
function flush(){
  renderQueued = false;
  const plain = $('#sort').value==='orig' && filter==='all' && !$('#q').value.trim() && !Object.keys(UI.colf||{}).length;
  const ae = document.activeElement, editing = !!(ae && ae.closest && ae.closest('#tb'));
  if(dirty.size && plain){
    computeDup();
    const idx = {}; visible().forEach((r,i) => idx[r.id]=i);
    let left = false;
    [...dirty].forEach(id => {
      const r = rows.find(x => x.id===id), tr = $('#tb').querySelector(`tr[data-id="${id}"]`);
      if(tr && tr.contains(ae)){ left = true; return; }
      dirty.delete(id);
      if(r && tr && idx[id]!=null){ const t = document.createElement('tbody'); t.innerHTML = rowHtml(r, idx[id]); tr.replaceWith(t.firstElementChild); }
    });
    renderStats(); saveSoon();
    if(left) setTimeout(scheduleRender, 800);
    return;
  }
  dirty.clear();
  if(editing && running){ setTimeout(scheduleRender, 800); return; }
  const now = Date.now();
  if(now - lastFull < 350){ clearTimeout(flushT); flushT = setTimeout(()=>{ lastFull = Date.now(); render(); }, 350); return; }
  lastFull = now; render();
}

function setBtns(){
  $('#btnStop').disabled = !running;
  ['#btnGo','#btnExtract','#btnCheckAll','#btnRecheck','#btnClear','#btnClearText','#bkCheck','#btnChSearch'].forEach(s => { const e=$(s); if(e) e.disabled = running; });
}

function checkPayload(r, extra){
  let p;
  if(r.type==='stalker') p = {type:'stalker', portal:r.portal||r.host, mac:r.mac||r.username};
  else if(r.type==='m3u') p = {type:'m3u', url:r.m3uurl||r.host};
  else p = {host:r.host, username:r.username, password:r.password};
  if(proxyOf(r)) p.proxy = proxyOf(r);
  if(r.ua) p.ua = r.ua;
  return Object.assign(p, extra||{});
}
function newRow(a){
  const r = Object.assign({state:'pending'}, a, {id:++seq});
  if(!r.type) r.type = 'xtream';
  return r;
}
// إن كانت فلاتر الأعمدة تخفي سيرفرات أُضيفت للتو فأزلها حتى لا تختفي الإضافة
function unhideNew(list){
  if(!Object.keys(UI.colf||{}).length || !list.length) return;
  if(list.some(r => !passCol(r, null))){ UI.colf = {}; toast('تم مسح فلاتر الأعمدة لإظهار السيرفرات المضافة'); }
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
    if(!r){ r = newRow(a); rows.push(r); byKey[kk] = r; added++; }
    else if(!hasPort(r.host) && hasPort(a.host)){ r.host = a.host; r.m3u = ''; }
    current.push(r);
  }
  unhideNew(current);
  msg(res.accounts.length ? `تم استخراج ${res.accounts.length} حساب (${added} جديد).` : 'لم يتم العثور على بيانات سيرفرات في النص.');
  render();
  return current;
}

async function checkOne(r, timeout, count, deep){
  r.state = 'checking'; r.message = ''; touch(r);
  try {
    const out = await api('/api/check', checkPayload(r, {timeout, count}));
    const keepQ = {quality:r.quality, kbps:r.kbps, deepMsg:r.deepMsg};
    Object.assign(r, out);
    if(!deep) Object.assign(r, keepQ);
  } catch(e){ r.state = 'error'; r.message = 'تعذر الاتصال بالأداة: ' + e.message; }
  if(deep && grp(r.state)==='ok' && (r.type||'xtream')==='xtream') await deepOne(r, timeout);
  if(grp(r.state)!=='ok' && r.http_code && !r.ua && SET.ua_auto!==false){   // رفض السيرفر — جرّب هويات أخرى
    r.message = (r.message||'') + ' — جاري تجربة هويات مشغّل أخرى...'; touch(r);
    try{
      const d = await api('/api/ua_detect', checkPayload(r, {stream:false, timeout: Math.min(10, timeout||10)}));
      if(d.best && !d.default_ok){
        r.ua = d.best;
        const out = await api('/api/check', checkPayload(r, {timeout, count}));
        Object.assign(r, out);
        if(grp(r.state)==='ok') r.message = 'يعمل بهوية ' + d.best_name + ' (اكتُشفت تلقائياً)'; else delete r.ua;
      } else { r.message = String(r.message||'').replace(' — جاري تجربة هويات مشغّل أخرى...', ''); }
    }catch(e){}
  }
  if(grp(r.state)==='ok' && SET.adult_scan!==false) await adultOne(r, timeout);
  touch(r);
}
async function adultOne(r, timeout){
  try{
    const a = await api('/api/adult', checkPayload(r, {timeout: Math.max(15, timeout||15)}));
    if(!a.error) r.adult = {adult: !!a.adult, hits: a.hits||[]};
  }catch(e){}
}
async function deepOne(r, timeout){
  r.deepBusy = true; touch(r);
  try{
    const d = await api('/api/deep', {host:r.host, username:r.username, password:r.password, timeout:timeout||15, proxy:proxyOf(r), ua:r.ua||''});
    r.quality = d.quality; r.kbps = d.kbps; r.ttfb = d.ttfb_ms; r.deepMsg = d.message || ''; r.deepRes = d;
  }catch(e){ r.deepMsg = 'تعذر الفحص العميق'; }
  r.deepBusy = false;
}

async function runChecks(list){
  if(running || !list.length) return;
  running = true; stop = false; setBtns();
  const conc = Math.max(1, Math.min(50, +$('#conc').value || 8));
  const timeout = Math.max(3, Math.min(120, +$('#tmo').value || 15));
  const count = $('#countch').checked, deep = $('#deep').checked;
  let i = 0, done = 0; const total = list.length;
  $('#bar').style.width = '0%';
  msg(`جاري فحص ${total} حساب...`);
  async function worker(){
    while(!stop && i < list.length){
      const r = list[i++];
      await checkOne(r, timeout, count, deep);
      done++;
      $('#bar').style.width = (done/total*100).toFixed(1) + '%';
      msg(`تم فحص ${done} من ${total}`);
    }
  }
  await Promise.all(Array.from({length:Math.min(conc, total)}, worker));
  const ok = list.filter(r => grp(r.state)==='ok').length;
  msg(stop ? `تم الإيقاف بعد فحص ${done} من ${total}.` : `انتهى الفحص: ${ok} يعمل من أصل ${total}.`);
  render();
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
  if(manual) proxies.push(manual);
  msg(`جاري تجهيز بروكسيات VPN لإعادة تجربة ${failed.length} سيرفراً...`);
  try{
    const pr = await api('/api/find_proxies', {country:'us', count:6});
    if(pr.ok) proxies = proxies.concat(pr.proxies);
  }catch(e){}
  if(!proxies.length){
    msg('لم أجد VPN أمريكي يعمل الآن. أدخل بروكسي/VPN جيداً في خانة البروكسي بالأعلى ثم أعد المحاولة.');
    return;
  }
  const timeout = Math.max(12, Math.min(120, (+$('#tmo').value || 15) + 10));
  const conc = Math.max(1, Math.min(12, +$('#conc').value || 6));
  const deep = $('#deep').checked;
  let i = 0, done = 0, fixed = 0; const total = failed.length;
  $('#bar').style.width = '0%';
  async function w(){
    while(!stop && i < failed.length){
      const r = failed[i++];
      let best = null, usedPx = '';
      for(const px of proxies){
        if(stop) break;
        r.state = 'checking'; r.message = 'تجربة عبر VPN...'; touch(r);
        let res;
        try{ res = await api('/api/check', Object.assign(checkPayload(r, {timeout}), {proxy:px})); }
        catch(e){ continue; }
        if(grp(res.state)==='ok'){ best = res; usedPx = px; break; }
        if(!best || (grp(res.state)!=='offline' && grp(best.state)==='offline')){ best = res; usedPx = px; }
      }
      if(best){
        Object.assign(r, best);
        if(grp(r.state)==='ok'){ r.viaProxy = true; r.proxyUsed = usedPx; fixed++; r.message = (r.message?r.message+' — ':'') + 'يعمل عبر VPN';
          if(deep && (r.type||'xtream')==='xtream') await deepOne(r, timeout); }
        else { r.viaProxy = false; r.proxyUsed = ''; }
      }
      done++;
      $('#bar').style.width = (done/total*100).toFixed(1) + '%';
      msg(`إعادة المحاولة عبر VPN: ${done}/${total} (نجح ${fixed})`);
      touch(r);
    }
  }
  await Promise.all(Array.from({length:Math.min(conc, total)}, w));
  msg(`انتهت إعادة المحاولة عبر VPN: ${fixed} سيرفراً يعمل من أصل ${total} — جُرّب ${proxies.length} بروكسي.`);
}

async function copyText(t){
  try { await navigator.clipboard.writeText(t); }
  catch(e){ const ta=document.createElement('textarea'); ta.value=t; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
}
const NATIVE = () => window.pywebview && window.pywebview.api;
function downloadBlob(blob, name){
  if(NATIVE()){                                   // نافذة مستقلة: نافذة حفظ أصلية
    const fr = new FileReader();
    fr.onload = async () => {
      try{
        const r = await window.pywebview.api.save_file(name, String(fr.result).split(',')[1] || '');
        if(r && r.ok) toast('تم الحفظ: ' + r.path); else if(r && !r.cancel) toast('تعذّر الحفظ');
      }catch(e){ toast('تعذّر الحفظ'); }
    };
    fr.readAsDataURL(blob); return;
  }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name;
  a.click(); setTimeout(()=>URL.revokeObjectURL(a.href), 2000);
}
function stamp(){ return new Date().toISOString().slice(0,16).replace(/[:T]/g,'-'); }
function exportCsvFallback(list){
  const head = ['الحالة','السيرفر','المستخدم','كلمة المرور','تاريخ الانتهاء','الأيام المتبقية','الاتصالات النشطة','الحد الأقصى','تاريخ الإنشاء','الاستجابة ms','التقييم','الجودة','ملاحظات','رابط M3U'];
  const q = v => '"' + String(v ?? '').replace(/"/g,'""') + '"';
  const lines = [head.map(q).join(',')];
  for(const r of list){
    lines.push([ (ST[r.state]||ST.error)[0], r.host, r.username, r.password,
      r.exp_date || (r.unlimited?'غير محدود':''), r.unlimited?'غير محدود':(r.days_left ?? ''),
      r.active_cons, r.max_cons, r.created, r.ping_ms, scoreOf(r) ?? '', (QL[r.quality]||[''])[0], r.message, r.m3u ].map(q).join(','));
  }
  downloadBlob(new Blob(['﻿' + lines.join('\r\n')], {type:'text/csv;charset=utf-8'}), 'iptv_results_' + stamp() + '.csv');
}
async function exportXlsx(list){
  list = list || visible();
  if(!list.length) return toast('لا توجد نتائج للتصدير');
  toast('جاري تجهيز ملف إكسل...');
  try{
    const res = await fetch('/api/export', {method:'POST', headers:{'Content-Type':'application/json','X-IPTV-Checker':'1'}, body:JSON.stringify({rows:list})});
    if(!res.ok) throw new Error('HTTP '+res.status);
    downloadBlob(await res.blob(), 'iptv_results_' + stamp() + '.xlsx');
    toast('تم تصدير ملف إكسل');
  }catch(e){ toast('تعذّر إنشاء إكسل — تم التصدير بصيغة CSV بدلاً منه'); exportCsvFallback(list); }
}
const m3uOf = r => (r.type==='m3u') ? (r.m3uurl||r.host) : (r.type==='stalker') ? (r.portal||r.host) : (r.m3u || `${r.host}/get.php?username=${encodeURIComponent(r.username)}&password=${encodeURIComponent(r.password)}&type=m3u_plus&output=ts`);
function m3uFile(list){
  const ok = list.filter(r => grp(r.state)==='ok');
  if(!ok.length) return toast('لا توجد سيرفرات شغالة');
  const body = '#EXTM3U\n' + ok.map(r => `#EXTINF:-1 tvg-name="${(r.title||defaultName(r.host)).replace(/"/g,"'")}",${r.title||defaultName(r.host)}\n${m3uOf(r)}`).join('\n') + '\n';
  downloadBlob(new Blob([body], {type:'audio/x-mpegurl'}), 'iptv_working_' + stamp() + '.m3u');
  toast(`تم حفظ ملف M3U (${ok.length} سيرفر)`);
}
async function copyLinks(list, onlyOk){
  const l = onlyOk ? list.filter(r => grp(r.state)==='ok') : list;
  if(!l.length) return toast('لا توجد سيرفرات لنسخها');
  await copyText(l.map(m3uOf).join('\n')); toast(`تم نسخ ${l.length} رابط`);
}
// حذف مع تراجع
function delRows(ids){
  const set = new Set(ids), snap = rows.map((r,i)=>[r,i]).filter(([r]) => set.has(r.id));
  if(!snap.length) return;
  rows = rows.filter(r => !set.has(r.id)); render();
  toast(`تم حذف ${snap.length} سيرفر`, '↩ تراجع', () => {
    snap.forEach(([r,i]) => rows.splice(Math.min(i, rows.length), 0, r)); render(); toast('تم التراجع عن الحذف');
  });
}

$('#btnGo').onclick = async () => {
  const current = await doExtract();
  if(current.length) runChecks(current);
  else msg('لا توجد سيرفرات في النص لفحصها.');
};
$('#btnCheckAll').onclick = () => { if(!rows.length) return toast('لا توجد سيرفرات في القائمة'); runChecks(rows.slice()); };
$('#btnExtract').onclick = doExtract;
$('#btnStop').onclick = () => { stop = true; msg('جاري الإيقاف...'); };
$('#btnClearText').onclick = () => { $('#inp').value=''; $('#inp').focus(); msg('تم مسح النص فقط — النتائج في الجدول باقية.'); };
$('#btnClear').onclick = () => {
  if(!rows.length){ $('#inp').value=''; return; }
  const old = rows, oldTxt = $('#inp').value;
  rows=[]; $('#inp').value=''; $('#bar').style.width='0%'; msg(''); render();
  toast('تم مسح كل النتائج', '↩ تراجع', () => { rows = old; $('#inp').value = oldTxt; render(); });
};
$('#btnRecheck').onclick = () => runChecks(rows.filter(r => grp(r.state)!=='ok'));
$('#btnCopy').onclick = () => copyLinks(visible(), true);
$('#btnM3uFile').onclick = () => m3uFile(visible());
$('#btnCsv').onclick = () => rows.length ? exportXlsx() : toast('لا توجد نتائج للتصدير');
// إجراءات جماعية
$('#bkCheck').onclick = () => runChecks(selected());
$('#bkXlsx').onclick = () => exportXlsx(selected());
$('#bkIptv').onclick = () => exportIptvnator(selected());
$('#bkM3u').onclick = () => m3uFile(selected());
$('#bkLinks').onclick = () => copyLinks(selected(), false);
$('#bkDel').onclick = () => delRows(selected().map(r => r.id));
$('#bkClear').onclick = () => { rows.forEach(r => r.sel=false); render(); };

// ---------------- حفظ/استيراد المشروع ----------------
const PROJ_FIELDS = ['host','username','password','type','portal','mac','isFull','m3uurl','rawM3u','m3uKind','title','userState','portalType','exportedId','autoRefresh','state','status','exp_ts','exp_date','days_left',
  'unlimited','active_cons','max_cons','trial','created','channels','ping_ms','server_tz','message','m3u','viaProxy','proxyUsed','note','quality','kbps','deepMsg','ua','adult'];
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
function exportIptvnator(sel){
  const list = sel || visible();
  if(!list.length) return toast('لا توجد سيرفرات للتصدير');
  // iptvnator يطلب userState كاملاً لكل سيرفر؛ نملأ الحقول الناقصة مع الحفاظ على الموجود
  const fullUserState = (r) => {
    const us = (r.userState && typeof r.userState==='object') ? r.userState : {};
    const t = r.type || r.portalType || 'xtream';
    const base = (t==='m3u')
      ? {favorites:[], recentlyViewed:[], hiddenGroupTitles:[]}
      : {hiddenCategories:[], favorites:[], recentlyViewed:[], playbackPositions:[], sourcePins:[]};
    return Object.assign(base, us);   // قيم المستخدم تبقى، والناقص يُملأ بمصفوفات فارغة
  };
  const playlists = list.map((r,i) => {
    const t = r.type || r.portalType || 'xtream';
    const common = {
      portalType:t, exportedId:r.exportedId || uuid(),   // يحافظ على معرّف السيرفر الأصلي ليبقى ربط المفضلات سليماً
      title:((r.title&&r.title.trim()) ? r.title.trim() : (defaultName(r.host) || ('سيرفر '+(i+1)))),
      autoRefresh:r.autoRefresh!=null?r.autoRefresh:false, position:i,
      userState:fullUserState(r)
    };
    if(t==='stalker') return {...common, connection:{portalUrl:r.portal||r.host, macAddress:r.mac||r.username, isFullStalkerPortal:!!r.isFull}};
    if(t==='m3u') return {...common, source:{kind:r.m3uKind||'url', url:r.m3uurl||r.host, rawM3u:(r.rawM3u && r.rawM3u.length) ? r.rawM3u : '#EXTM3U\n'}};   // rawM3u صالح دائماً
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
function loadScript(src){ return new Promise((res,rej)=>{ const s=document.createElement('script'); s.src=src; s.onload=res; s.onerror=()=>rej(new Error('تعذر تحميل مكتبة المشغّل — تأكد من الإنترنت لأول مرة فقط')); document.head.appendChild(s); }); }
let mpegtsP=null, hlsP=null;
const ensureMpegts = () => window.mpegts ? Promise.resolve() : (mpegtsP || (mpegtsP = loadScript('/lib/mpegts.js').catch(e=>{ mpegtsP=null; throw e; })));
const ensureHls = () => window.Hls ? Promise.resolve() : (hlsP || (hlsP = loadScript('/lib/hls.js').catch(e=>{ hlsP=null; throw e; })));
const KPATH = {live:'live', vod:'movie', series:'series'};
const RH = 34;
const PVC = {};                       // ذاكرة مؤقتة للقوائم لكل سيرفر
let pv = null, pvTimers = {};
const sk = () => key(pv.r);
const kindOf = it => (it && it.kind) || (pv.view==='episodes' ? 'series' : (pv.tab==='fav'||pv.tab==='recent') ? 'live' : pv.tab);
const itemKey = it => it.ou || it.cmd || (kindOf(it)+':'+it.id);
const tabKinds = () => (pv.type==='xtream') ? ['live','vod','series','fav','recent','all'] : ['live','fav','recent'];
const jnorm = t => String(t||'').toLowerCase().replace(/[أإآ]/g,'ا').replace(/ة/g,'ه').replace(/ى/g,'ي').replace(/[^0-9a-z\u0621-\u064a]+/g,'');
const isFolder = it => pv.view==='list' && (pv.tab==='series' || (pv.tab==='all' && it.kind==='series'));

async function regSrv(r){
  const t = r.type||'xtream';
  const o = await api('/api/reg', {type:t, host: t==='stalker'?(r.portal||r.host): t==='m3u'?(r.m3uurl||r.host): r.host,
    username:r.username||'', password:r.password||'', mac:r.mac||(t==='stalker'?r.username:''), proxy:proxyOf(r), ua:r.ua||''});
  return o.s;
}
function surl(it, ext){
  const b = `t=${window.__TOKEN__}&s=${pv.sid}`;
  if(pv.type==='m3u') return `/relay?${b}&u=${encodeURIComponent(it.ou)}`;
  if(pv.type==='stalker') return `/stream?${b}&cmd=${encodeURIComponent(it.cmd)}`;
  return `/stream?${b}&kind=${kindOf(it)}&id=${encodeURIComponent(it.id)}&ext=${ext||it.ext||'ts'}`;
}
function directUrl(it){
  const r = pv.r;
  if(pv.type==='m3u') return it.ou;
  if(pv.type==='stalker') return '';
  return `${r.host}/${KPATH[kindOf(it)]}/${encodeURIComponent(r.username)}/${encodeURIComponent(r.password)}/${it.id}.${it.ext||'ts'}`;
}
function attemptsOf(it){
  const k = kindOf(it);
  if(pv.type!=='xtream') return [{ext:''}];
  if(k==='live'){ const a=[{ext:it.ext||'ts'}]; if((it.ext||'ts')!=='m3u8') a.push({ext:'m3u8'}); return a; }
  return [{ext:it.ext||'mp4'}];
}
const pvMsg = t => $('#pvMsg').textContent = t;

function pvStop(keepTimers){
  const v=$('#pvVideo');
  if(pv){ if(pv.player){ try{pv.player.destroy();}catch(e){} pv.player=null; } if(pv.hls){ try{pv.hls.destroy();}catch(e){} pv.hls=null; } }
  try{ v.pause(); v.removeAttribute('src'); v.load(); }catch(e){}
  Object.values(pvTimers).forEach(clearTimeout); Object.values(pvTimers).forEach(clearInterval); pvTimers = {};
  v.onerror = v.onplaying = v.onloadedmetadata = v.ontimeupdate = v.onended = v.onstalled = null;
}
function pvClose(){
  try{ if(document.fullscreenElement) document.exitFullscreen(); }catch(e){}
  pvStop(); $('#ov').classList.remove('on'); pv = null; $('#pvStats').classList.remove('on'); saveSoon();
}

// ---- بيانات القوائم ----
function parseM3u(text){
  const items = [], cats = {}; let cur = null, n = 0;
  for(const raw of text.split(/\r?\n/)){
    const line = raw.trim(); if(!line) continue;
    if(line.startsWith('#EXTINF')){
      const g = (line.match(/group-title="([^"]*)"/i)||[])[1] || '';
      const logo = (line.match(/tvg-logo="([^"]*)"/i)||[])[1] || '';
      const nm = line.includes(',') ? line.slice(line.lastIndexOf(',')+1).trim() : '';
      cur = {name:nm || (line.match(/tvg-name="([^"]*)"/i)||[])[1] || ('#'+(n+1)), cat:g, icon:logo};
    } else if(!line.startsWith('#')){
      let u = line;
      if(u.startsWith('/relay?')){ try{ u = decodeURIComponent((u.match(/[?&]u=([^&]*)/)||[])[1] || ''); }catch(e){} }
      if(!u) continue;
      const it = Object.assign({id:++n, ou:u}, cur || {name:'#'+n, cat:'', icon:''});
      items.push(it); if(it.cat) cats[it.cat] = it.cat; cur = null;
    }
  }
  return {cats, items};
}
async function loadContent(force){
  const r = pv.r, c = PVC[sk()];
  if(c && !force){ pv.data = c; return ''; }
  const data = {live:{cats:{},items:[],loaded:true}, vod:{cats:{},items:[],loaded:false}, series:{cats:{},items:[],loaded:false}};
  let err = '';
  if(pv.type==='xtream'){
    const res = await api('/api/content', {host:r.host, username:r.username, password:r.password, proxy:proxyOf(r), ua:r.ua||'', force:!!force});
    pv.cachedAt = res.cached_at || 0;
    data.live = {cats:(res.live||{}).cats||{}, items:(res.live||{}).items||[], loaded:true};
    data.vod.cats = (res.vod||{}).cats||{}; data.series.cats = (res.series||{}).cats||{};
    err = res.error || '';
  } else if(pv.type==='stalker'){
    const res = await api('/api/stalker', {portal:r.portal||r.host, mac:r.mac||r.username, proxy:proxyOf(r), ua:r.ua||'', force:!!force});
    pv.cachedAt = res.cached_at || 0;
    data.live = {cats:res.cats||{}, items:res.items||[], loaded:true}; err = res.error || '';
  } else {
    const resp = await fetch(`/relay?t=${window.__TOKEN__}&s=${pv.sid}&u=${encodeURIComponent(r.m3uurl||r.host)}`);
    if(!resp.ok) err = 'تعذر جلب ملف M3U (رمز '+resp.status+')';
    else { const p = parseM3u(await resp.text()); data.live = {cats:p.cats, items:p.items, loaded:true}; if(!p.items.length) err = 'الملف لا يحتوي قنوات'; }
  }
  if(!err || data.live.items.length) PVC[sk()] = data;
  pv.data = data; return err;
}
async function ensureLoaded(k){
  if(!pv.data[k] || pv.data[k].loaded || pv.type!=='xtream') return;
  pvMsg(k==='vod' ? 'جاري تحميل الأفلام (مرة واحدة)...' : 'جاري تحميل المسلسلات (مرة واحدة)...');
  try{
    const res = await api('/api/streams', {host:pv.r.host, username:pv.r.username, password:pv.r.password, kind:k, proxy:proxyOf(pv.r), ua:pv.r.ua||'', force:!!pv.forceLists});
    pv.data[k].items = res.items || []; pv.data[k].loaded = true;
    pvMsg(res.error ? ('تعذّر التحميل: '+res.error) : `${pv.data[k].items.length} عنصر — اختر واحداً.`);
  }catch(e){ pvMsg('تعذّر تحميل هذا القسم.'); }
}
function tabLabels(){
  const base = {live:'📺 مباشر',vod:'🎬 أفلام',series:'📂 مسلسلات',fav:'⭐',recent:'🕘',all:'🔎 الكل'};
  const kinds = tabKinds();
  [...$('#pvTabs').children].forEach(b => {
    const k = b.dataset.k; b.style.display = kinds.includes(k) ? '' : 'none';
    let n = ''; if(pv && pv.data && pv.data[k] && pv.data[k].loaded) n = ` (${pv.data[k].items.length})`;
    if(k==='fav') n = ` (${(UI.favs[sk()]||[]).length})`;
    b.textContent = base[k] + n;
  });
}

// ---- فتح المشغّل ----
async function openPlayer(r, keep){
  pvStop();
  const t = r.type||'xtream';
  pv = {r, type:t, sid:'', tab:'live', view:'list', curSeries:null, episodes:[], cur:null, list:[], data:null, player:null, hls:null,
        tries:0, att:0, started:false, txTried:false, forceTx:false, mode:''};
  keep = keep || {};
  $('#pvHost').textContent = (r.title || defaultName(r.host)) + (r.username ? ' — ' + r.username : '');
  $('#pvBack').style.display='none'; $('#pvQ').value = keep.q || ''; $('#pvInfo').innerHTML=''; $('#pvSp').innerHTML=''; $('#pvSp').style.height='0';
  $('#pvCat').innerHTML = '<option value="">كل التصنيفات</option>';
  const okRows = rows.filter(x => x===r || grp(x.state)==='ok');
  $('#pvSrv').innerHTML = okRows.map(x => `<option value="${x.id}" ${x===r?'selected':''}>${esc((x.title||defaultName(x.host)) + (x.username?' · '+x.username:''))}</option>`).join('');
  $('#pvTx').style.display = CAPS.ffmpeg ? '' : 'none';
  $('#ov').classList.add('on');
  pvMsg('جاري تجهيز الاتصال...');
  try{ pv.sid = await regSrv(r); }catch(e){ pvMsg('تعذر تجهيز السيرفر.'); return; }
  if(!pv) return;
  pv.data = null; tabLabels();
  const t0 = Date.now(), me = pv;
  const tick = () => { if(pv===me && !pv.data) pvMsg(`جاري جلب القنوات من السيرفر... ${Math.round((Date.now()-t0)/1000)} ث (القوائم الكبيرة تأخذ وقتاً في أول مرة فقط، ثم تُحفظ)`); };
  tick(); const tmr = setInterval(tick, 1000);
  let err = '';
  try{ err = await loadContent(false); }catch(e){ err = 'تعذر جلب المحتوى من السيرفر.'; }
  clearInterval(tmr);
  if(!pv || pv!==me) return;
  if(!pv.data) pv.data = {live:{cats:{},items:[],loaded:true}, vod:{cats:{},items:[],loaded:false}, series:{cats:{},items:[],loaded:false}};
  try{ tabLabels(); await switchTab('live', keep); }
  catch(e){ pvMsg('تعذّر عرض القائمة: ' + (e && e.message || e) + ' — اضغط 🔄'); return; }
  const nL = pv.data.live.items.length;
  const cnote = pv.cachedAt ? ` (من الذاكرة المحفوظة — ${new Date(pv.cachedAt*1000).toLocaleString()}، اضغط 🔄 للتحديث)` : '';
  pvMsg(nL ? `جاهز — ${nL} قناة.${t==='xtream'?' الأفلام والمسلسلات تُحمَّل عند فتح قسمها.':''}${cnote}` : (err ? ('تعذر جلب البث: '+err) : 'لا قنوات.'));
}
function fillCats(keepName){
  const sel = $('#pvCat');
  const cats = (pv.data && pv.data[pv.tab] && pv.data[pv.tab].cats) || {};
  sel.innerHTML = '<option value="">كل التصنيفات</option>' + Object.entries(cats).map(([id,n])=>`<option value="${esc(id)}">${esc(n||id)}</option>`).join('');
  sel.style.display = (pv.tab==='fav'||pv.tab==='recent'||pv.tab==='all'||pv.view==='episodes') ? 'none' : '';
  if(keepName){ const o = [...sel.options].find(o => o.textContent===keepName); if(o) sel.value = o.value; }
}
async function switchTab(k, keep){
  pv.tab = k; pv.view = 'list'; pv.curSeries = null;
  $('#pvBack').style.display='none';
  [...$('#pvTabs').children].forEach(t=>t.classList.toggle('sel', t.dataset.k===k));
  if(!(keep && keep.q!=null)) $('#pvQ').value='';
  fillCats(keep && keep.catName);
  buildList();
  if(k==='vod' || k==='series'){ await ensureLoaded(k); if(pv && pv.tab===k){ fillCats(keep && keep.catName); buildList(); tabLabels(); } }
  if(k==='all'){
    $('#pvQ').focus();
    await ensureLoaded('vod'); if(pv && pv.tab==='all') await ensureLoaded('series');
    if(pv && pv.tab==='all'){ buildList(); tabLabels(); pvMsg('اكتب اسم قناة أو فيلم أو مسلسل للبحث فيها كلها.'); }
  }
}
function curItems(){
  if(pv.view==='episodes') return pv.episodes;
  if(pv.tab==='fav') return UI.favs[sk()] || [];
  if(pv.tab==='recent') return UI.recents[sk()] || [];
  return (pv.data[pv.tab]||{items:[]}).items;
}
// ---- قائمة افتراضية (تُرسم الصفوف الظاهرة فقط) ----
function buildList(){
  const q = $('#pvQ').value.trim().toLowerCase(), cat = $('#pvCat').value;
  const qn = jnorm(q);
  let out = [];
  if(pv.tab==='all' && pv.view==='list'){                 // بحث شامل: مباشر + أفلام + مسلسلات
    if(qn.length >= 2){
      for(const [k, lbl] of [['live','📺 القنوات'],['vod','🎬 الأفلام'],['series','📂 المسلسلات']]){
        const d = pv.data[k]; if(!d || !d.items) continue;
        const hits = d.items.filter(c => jnorm(c.name).includes(qn));
        if(!hits.length) continue;
        out.push({h:true, text:`${lbl} (${hits.length})`});
        hits.slice(0, 500).forEach(c => out.push({it:Object.assign({}, c, {kind:k})}));
      }
    }
    pv.list = out;
    $('#pvSp').style.height = (out.length*RH)+'px';
    $('#pvEmpty').textContent = out.length ? '' : (qn.length < 2 ? 'اكتب حرفين على الأقل للبحث في كل المحتوى.' : 'لا يوجد شيء مطابق.');
    drawVirtual(); return;
  }
  let items = curItems();
  if(pv.view==='list' && (pv.tab==='live'||pv.tab==='vod'||pv.tab==='series') && cat) items = items.filter(c => String(c.cat)===cat);
  if(q) items = items.filter(c => (c.name||'').toLowerCase().includes(q) || (qn && jnorm(c.name).includes(qn)));
  if(pv.view==='episodes'){
    let last = null;
    for(const e of items){ if(e.season!==last){ last = e.season; out.push({h:true, text:'الموسم '+e.season}); } out.push({it:e}); }
  } else out = items.map(it => ({it}));
  pv.list = out;
  $('#pvSp').style.height = (out.length*RH)+'px';
  $('#pvEmpty').textContent = out.length ? '' : (pv.tab==='fav' ? 'لا توجد مفضلة بعد — اضغط ☆ أثناء التشغيل.' : pv.tab==='recent' ? 'لا يوجد سجل مشاهدة بعد.' : 'لا يوجد شيء مطابق.');
  drawVirtual();
}
const isFav = it => (UI.favs[sk()]||[]).some(x => itemKey(x)===itemKey(it) && kindOf(x)===kindOf(it));
let vdQ = false;
function drawVirtual(){
  if(!pv) return;
  const box = $('#pvChs'), top = box.scrollTop, h = box.clientHeight || 320;
  const a = Math.max(0, Math.floor(top/RH)-8), b = Math.min(pv.list.length, Math.ceil((top+h)/RH)+8);
  let html = '';
  const KI = {live:'📺 ', vod:'🎬 ', series:'📂 '};
  for(let i=a;i<b;i++){
    const e = pv.list[i];
    if(e.h){ html += `<div class="ch hdr" style="top:${i*RH}px">${esc(e.text)}</div>`; continue; }
    const it = e.it, folder = isFolder(it), sel = pv.cur && itemKey(pv.cur)===itemKey(it) && kindOf(pv.cur)===kindOf(it) && !folder;
    const pre = pv.tab==='all' && pv.view==='list' ? (KI[it.kind]||'') : (folder ? '📂 ' : '');
    html += `<div class="ch ${sel?'sel':''}" style="top:${i*RH}px" data-i="${i}">${it.icon?`<img loading="lazy" src="${esc(it.icon)}" onerror="this.remove()" alt="">`:''}<span class="tx">${pre}${it.num&&pv.view==='episodes'?esc(it.num)+'. ':''}${esc(it.name)}</span>${isFav(it)?'<span class="rt">★</span>':''}${it.rating?`<span class="rt">${esc(it.rating)}</span>`:''}</div>`;
  }
  $('#pvSp').innerHTML = html;
}
$('#pvChs').addEventListener('scroll', () => { if(vdQ) return; vdQ = true; requestAnimationFrame(()=>{ vdQ=false; drawVirtual(); }); });

async function onItemClick(i){
  const e = pv.list[i]; if(!e || e.h) return;
  const it = e.it;
  if(isFolder(it)){
    pv.allQ = $('#pvQ').value;
    pv.curSeries = it; pvMsg('جاري جلب حلقات: '+it.name);
    let res; try{ res = await api('/api/series', {host:pv.r.host, username:pv.r.username, password:pv.r.password, series_id:it.id, proxy:proxyOf(pv.r), ua:pv.r.ua||''}); }
    catch(x){ pvMsg('تعذر جلب الحلقات.'); return; }
    if(!pv) return;
    pv.episodes = (res.episodes||[]).map(x => Object.assign({}, x, {kind:'series'})); pv.view = 'episodes';
    $('#pvBack').style.display=''; $('#pvCat').style.display='none'; $('#pvQ').value=''; $('#pvChs').scrollTop = 0;
    showInfo(res.info, it.name);
    pvMsg(pv.episodes.length ? `${it.name} — ${pv.episodes.length} حلقة، اختر واحدة.` : (res.error || 'لا توجد حلقات لهذا المسلسل.'));
    buildList(); return;
  }
  playItem(it);
}
function showInfo(info, title, extraHtml){
  info = info || {};
  const meta = [info.year, info.genre, info.rating ? '⭐ '+info.rating : '', info.duration].filter(Boolean).map(esc).join(' · ');
  $('#pvInfo').innerHTML = (title||info.plot||extraHtml) ? `${info.cover?`<img src="${esc(info.cover)}" onerror="this.remove()" alt="">`:''}<div><b>${esc(title||'')}</b><div class="small">${meta}</div><div class="pt">${esc(info.plot||'')}</div>${extraHtml||''}</div>` : '';
}

// ---- التشغيل ----
function addRecent(it){
  const k = sk(); const l = (UI.recents[k] = UI.recents[k] || []);
  const x = Object.assign({}, it, {kind:kindOf(it)});
  const i = l.findIndex(y => itemKey(y)===itemKey(x)); if(i>=0) l.splice(i,1);
  l.unshift(x); if(l.length>30) l.length = 30; saveSoon();
}
function updateFavBtn(){ $('#pvFav').textContent = (pv && pv.cur && isFav(pv.cur)) ? '★' : '☆'; }
function toggleFav(){
  if(!(pv && pv.cur)) return toast('اختر عنصراً أولاً');
  const k = sk(); const l = (UI.favs[k] = UI.favs[k] || []); const it = pv.cur;
  const i = l.findIndex(y => itemKey(y)===itemKey(it) && kindOf(y)===kindOf(it));
  if(i>=0) l.splice(i,1); else l.unshift(Object.assign({}, it, {kind:kindOf(it)}));
  updateFavBtn(); tabLabels(); if(pv.tab==='fav') buildList(); else drawVirtual(); saveSoon();
}
async function playItem(it, opts){
  if(!pv) return;
  opts = opts || {};
  pvStop();
  pv.cur = it; pv.tries = 0; pv.att = 0; pv.started = false; pv.txTried = false; pv.forceTx = !!opts.tx; pv.noDirect = false; pv.curIdx = pv.list.findIndex(e => e.it===it);
  addRecent(it); drawVirtual(); updateFavBtn(); loadExtra(it);
  startAttempt(it);
}
async function loadExtra(it){
  const k = kindOf(it), r = pv.r, mine = it;
  $('#pvInfo').innerHTML = '';
  if(pv.type!=='xtream') { showInfo({}, it.name); return; }
  try{
    if(k==='live'){
      showInfo({}, it.name);
      const e = await api('/api/epg', {host:r.host, username:r.username, password:r.password, stream_id:it.id, proxy:proxyOf(r), ua:r.ua||''});
      if(!pv || pv.cur!==mine) return;
      const now = Date.now()/1000, items = (e.items||[]).filter(x => x.end>now).sort((a,b)=>a.start-b.start);
      const hm = t => new Date(t*1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
      const cur = items.find(x => x.start<=now), nxt = items.find(x => x.start>now);
      const html = (cur ? `<div class="small">▶ الآن: <b>${esc(cur.title)}</b> (${hm(cur.start)}–${hm(cur.end)})</div>` : '') + (nxt ? `<div class="small">⏭ التالي: ${esc(nxt.title)} (${hm(nxt.start)})</div>` : '');
      if(html) showInfo({}, it.name, html);
    } else if(k==='vod'){
      const v = await api('/api/vodinfo', {host:r.host, username:r.username, password:r.password, vod_id:it.id, proxy:proxyOf(r), ua:r.ua||''});
      if(!pv || pv.cur!==mine) return;
      showInfo(v.info, it.name);
    } else if(pv.curSeries && it.plot){ showInfo({cover:it.img, plot:it.plot, duration:it.dur}, it.name); }
  }catch(e){}
}
function isHlsUrl(it, att){ return att.ext==='m3u8' || (pv.type==='m3u' && /\.m3u8?(\?|$)/i.test(it.ou||'')); }
async function startAttempt(it){
  if(!pv || pv.cur!==it) return;
  const v = $('#pvVideo'), live = kindOf(it)==='live';
  const atts = attemptsOf(it), att = atts[Math.min(pv.att, atts.length-1)];
  const url = surl(it, att.ext);
  v.volume = UI.vol ?? 1;
  v.onplaying = () => { pv.started = true; pv.tries = 0; clearTimeout(pvTimers.start); pvMsg('يعمل: '+(it.name||'')); };
  v.onerror = () => {
    if(pv && pv.mode==='vod-direct' && !pv.started){ pv.noDirect = true; pvStop(); return startAttempt(it); }   // فشل المباشر — عبر الوسيط
    const c = v.error && v.error.code;
    if((c===3 || c===4) && !live && !pv.started && !pv.forceTx){          // قبل اعتبارها مشكلة ترميز: هل رفض السيرفر الملف أصلاً؟
      fetch(url+'&probe=1').then(r => r.json()).then(d => {
        if(!pv || pv.cur!==it) return;
        if(d && (d.status===200 || d.status===206) && d.kind!=='html') onDecodeFail(it, att, url);
        else onFail(it, 'http:'+(d && d.status));
      }).catch(() => onFail(it, 'error'));
      return;
    }
    if(c===3 || c===4) onDecodeFail(it, att, url); else onFail(it, 'error'); };
  v.onended = () => { if(live) onFail(it, 'ended'); };
  v.onloadedmetadata = () => { if(!live) resumeTo(it); };
  v.ontimeupdate = () => { if(!live) saveResume(it); };
  clearInterval(pvTimers.watch); let lastT = -1, still = 0;
  pvTimers.watch = setInterval(() => {                       // مراقبة التجمّد
    if(!pv || pv.cur!==it) return;
    if(!v.paused && pv.started){ if(Math.abs(v.currentTime-lastT)<0.01) still++; else still = 0; lastT = v.currentTime; if(live && still>=12){ still = 0; onFail(it, 'stall'); } }
  }, 1000);
  clearTimeout(pvTimers.start);
  const direct = !live && pv.type==='xtream' && !proxyOf(pv.r) && !pv.r.ua && !pv.noDirect && !pv.forceTx;
  let extended = false;
  const onTimeout = () => {
    if(!(pv && pv.cur===it && !pv.started)) return;
    const gotData = v.readyState >= 2 || (v.buffered && v.buffered.length) || (pv.player && pv.player.statisticsInfo && pv.player.statisticsInfo.speed > 0);
    if(gotData && !extended){ extended = true; pvTimers.start = setTimeout(onTimeout, 15000); return; }   // البيانات تصل — امنحه وقتاً إضافياً
    onFail(it, 'timeout');
  };
  pvTimers.start = setTimeout(onTimeout, direct ? 9000 : 25000);
  pvMsg((pv.tries ? `إعادة الاتصال (${pv.tries}/3): ` : 'جاري التشغيل: ') + (it.name||''));
  try{
    if(pv.forceTx){ await playTranscode(it, url); return; }
    if(isHlsUrl(it, att)) { pv.mode='hls'; await playHls(v, url); }
    else if(live && (att.ext==='ts' || pv.type==='stalker' || pv.type==='m3u')){ pv.mode='mpegts'; await playTs(v, url, true, it); }
    else if(!live && pv.type==='xtream' && !proxyOf(pv.r) && !pv.r.ua && !pv.noDirect){   // الأفلام: مباشرة من السيرفر (أسرع)
      pv.mode='vod-direct'; v.src = directUrl(it); try{ await v.play(); }catch(e){}
    }
    else { pv.mode='direct'; v.src = url; try{ await v.play(); }catch(e){} }
  }catch(e){ pvMsg((e.message||'تعذّر التشغيل')+' — جرّب «فتح خارجياً».'); }
}
async function playTs(v, url, live, it){
  await ensureMpegts();
  if(!(window.mpegts && mpegts.isSupported())){ v.src = url; try{ await v.play(); }catch(e){} return; }
  pv.player = mpegts.createPlayer({type:'mpegts', isLive:live, url}, {enableWorker:false, enableStashBuffer:true, stashInitialSize:384, liveBufferLatencyChasing:false, autoCleanupSourceBuffer:true});
  pv.player.attachMediaElement(v); pv.player.load();
  pv.player.on(mpegts.Events.ERROR, (type, detail, info) => {
    if(!pv || pv.cur!==it) return;
    if(type==='MediaError' && !pv.started) onDecodeFail(it, {}, url); else onFail(it, pv.started ? 'stall' : 'net:'+(detail||''));
  });
  try{ v.playbackRate = 1.0; await v.play(); }catch(e){}
}
async function playHls(v, url){
  await ensureHls();
  if(window.Hls && Hls.isSupported()){
    pv.hls = new Hls({maxBufferLength:20});
    pv.hls.on(Hls.Events.ERROR, (ev, d) => { if(d && d.fatal && pv && pv.cur){ if(d.type===Hls.ErrorTypes.MEDIA_ERROR) { try{ pv.hls.recoverMediaError(); }catch(e){ onDecodeFail(pv.cur,{},url); } } else onFail(pv.cur, 'hls:'+(d.details||'')); } });
    pv.hls.loadSource(url); pv.hls.attachMedia(v);
  } else v.src = url;
  try{ await v.play(); }catch(e){}
}
async function playTranscode(it, url){
  const v = $('#pvVideo'), live = kindOf(it)==='live';
  pvMsg('تحليل البث لاختيار طريقة التحويل...');
  let mode = 'full';
  try{ const c = await api('/api/codec', {src:url}); if(c.ok){ pv.codec = c; if(c.video==='h264') mode = 'audio'; } else if(c.reason){ if(/ffprobe/.test(c.reason)===false) {} } }catch(e){}
  if(!pv || pv.cur!==it) return;
  pv.mode = 'transcode-'+mode; pv.txTried = true;
  const turl = `/transcode?t=${window.__TOKEN__}&s=${pv.sid}&mode=${mode}&fmt=${live?'ts':'mp4'}&src=${encodeURIComponent(url)}`;
  pvMsg((mode==='audio' ? 'تحويل الصوت فقط (الفيديو كما هو)' : 'تحويل الفيديو والصوت (قد يستهلك المعالج)') + ' عبر ffmpeg...');
  if(live) await playTs(v, turl, true, it); else { v.src = turl; try{ await v.play(); }catch(e){} }
}
async function onDecodeFail(it, att, url){
  if(!pv || pv.cur!==it || pv.txTried) { if(pv && pv.cur===it) pvMsg('الترميز غير مدعوم داخل الأداة — استخدم «فتح خارجياً».'); return; }
  if(!CAPS.ffmpeg){ pvMsg('الترميز غير مدعوم (HEVC/AC3...). ثبّت ffmpeg لتفعيل التحويل اللحظي، أو استخدم «فتح خارجياً».'); return; }
  pv.txTried = true; pv.forceTx = true; pvStop(); pvMsg('صيغة غير مدعومة — جاري التحويل عبر ffmpeg...');
  pvTimers.re = setTimeout(() => { if(pv && pv.cur===it) startAttempt(it); }, 1200);
}
async function onFail(it, why){
  if(!pv || pv.cur!==it) return;
  clearTimeout(pvTimers.start);
  const live = kindOf(it)==='live', atts = attemptsOf(it);
  const again = (ms) => { pvStop(); pvTimers.re = setTimeout(() => { if(pv && pv.cur===it) startAttempt(it); }, ms); };
  if(pv.mode==='vod-direct' && !pv.started){ pv.noDirect = true; pvMsg('جاري التشغيل عبر الوسيط المحلي...'); return again(300); }
  if(!pv.started && pv.att+1 < atts.length){ pv.att++; pvMsg('جاري تجربة صيغة بديلة...'); return again(1200); }   // انتظر تحرير الاتصال ثم جرّب الصيغة التالية
  if(live && pv.tries < 3 && (pv.started || why==='stall' || why==='ended')){          // إعادة اتصال تلقائية
    pv.tries++; const w = [1500,3000,6000][pv.tries-1]; pvStop(); pvMsg(`انقطع البث — إعادة الاتصال (${pv.tries}/3)...`);
    pvTimers.re = setTimeout(() => { if(pv && pv.cur===it){ pv.started = false; pv.att = 0; startAttempt(it); } }, w); return;
  }
  pvStop();
  await new Promise(res => setTimeout(res, 1200));          // لا تفحص قبل أن يغلق السيرفر الاتصال السابق
  if(!pv || pv.cur!==it) return;
  if(!live && pv.type==='xtream' && !it._resolved){          // فيلم/حلقة: ابحث عن الامتداد الحقيقي للملف
    it._resolved = true;
    pvMsg('السيرفر رفض الملف — جاري البحث عن الصيغة الصحيحة للملف...');
    try{
      const rr = await api('/api/vod_resolve', {host:pv.r.host, username:pv.r.username, password:pv.r.password, vod_id:it.id, ext:it.ext||'', kind:kindOf(it), proxy:proxyOf(pv.r), ua:pv.r.ua||''});
      if(!pv || pv.cur!==it) return;
      try{ api('/api/log', {op:'add', msg:`بحث الامتداد لـ «${it.name}»: ` + (rr.tried||[]).map(t => t.ext+'='+t.status).join('، ')}); }catch(e){}
      if(rr.ext && rr.ext !== (it.ext||'')){
        it.ext = rr.ext; pv.att = 0; pv.started = false; pv.noDirect = false; pv.txTried = false; pv.forceTx = false;
        pvMsg('وُجدت الصيغة الصحيحة (' + rr.ext + ') — جاري التشغيل...'); return startAttempt(it);
      }
      if(!rr.ext){ pvMsg('السيرفر يرفض هذا الملف بكل الصيغ (' + (rr.tried||[]).map(t => t.ext+': '+t.status).slice(0,4).join('، ') + ') — الملف معطّل على السيرفر أو غير مسموح لحسابك.'); return; }
    }catch(e){}
  }
  const url = surl(it, atts[0].ext); let d = null;
  try{ const r = await fetch(url+'&probe=1'); d = await r.json(); }catch(e){}
  if(!pv || pv.cur!==it) return;
  pvMsg(diagMsg(d) + ' جرّب «فتح خارجياً» أو زر «تحويل».');
  try{ api('/api/log', {op:'add', msg:`فشل التشغيل [${kindOf(it)}] «${it.name}» — السبب: ${why}، الوضع: ${pv.mode}، الفحص: ${d ? (d.status||'')+' '+(d.kind||'')+' '+(d.msg||'') : 'لا رد'}`}); }catch(e){}
}
function diagMsg(d){
  if(!d) return 'تعذّر الوصول إلى السيرفر.';
  const st = d.status;
  if(d.kind==='html') return 'السيرفر أعاد صفحة ويب بدل البث — غالباً الحساب محظور أو منتهٍ.';
  if(st===401 || st===403 || st===455) return 'رفض السيرفر البث (رمز '+st+') — الحساب محظور أو عدد الاتصالات ممتلئ.';
  if(st===404 || st===512 || st===400) return 'هذا المحتوى غير متوفر على السيرفر حالياً (رمز '+st+').';
  if(!st) return (d.msg || 'تعذّر الوصول إلى السيرفر') + ' — جرّب تفعيل بروكسي.';
  if(st===200 && d.bytes===0) return 'السيرفر فتح الاتصال لكن لم يرسل بيانات.';
  return 'تعذّر بدء البث (رمز '+st+').';
}
function resumeTo(it){
  const p = UI.resume[sk()+'|'+itemKey(it)], v = $('#pvVideo');
  if(p && p>10 && v.duration && p < v.duration-30){ try{ v.currentTime = p; toast('استئناف من '+Math.floor(p/60)+':'+String(Math.floor(p%60)).padStart(2,'0')); }catch(e){} }
}
let lastResSave = 0;
function saveResume(it){
  const v = $('#pvVideo'), now = Date.now(); if(now-lastResSave < 5000 || !v.duration) return; lastResSave = now;
  const k = sk()+'|'+itemKey(it);
  if(v.currentTime > v.duration-30) delete UI.resume[k]; else UI.resume[k] = Math.floor(v.currentTime);
  saveSoon();
}
// ---- إحصاءات حية ----
function statsTick(){
  if(!pv) return; const v = $('#pvVideo'), L = [];
  L.push('الوضع: ' + (pv.mode||'—'));
  if(v.videoWidth) L.push(`الدقة: ${v.videoWidth}×${v.videoHeight}`);
  if(pv.codec) L.push(`الترميز: ${pv.codec.video||'?'} / ${pv.codec.audio||'?'}`);
  try{ if(v.buffered.length) L.push('المخزّن: '+(v.buffered.end(v.buffered.length-1)-v.currentTime).toFixed(1)+' ث'); }catch(e){}
  const s = pv.player && pv.player.statisticsInfo; if(s){ L.push('السرعة: '+Math.round(s.speed||0)+' KB/s'); }
  if(pv.hls){ L.push('نطاق: '+Math.round((pv.hls.bandwidthEstimate||0)/1000)+' kbps'); }
  try{ const q = v.getVideoPlaybackQuality(); L.push(`الإطارات: ${q.totalVideoFrames} (مُسقَط ${q.droppedVideoFrames})`); }catch(e){}
  $('#pvStats').textContent = L.join('\n');
}
$('#pvSt').onclick = () => { const e = $('#pvStats'); e.classList.toggle('on'); clearInterval(statsTick.t); if(e.classList.contains('on')){ statsTick(); statsTick.t = setInterval(statsTick, 1000); } };

$('#pvTabs').onclick = e => { const t = e.target.closest('.tab'); if(t && pv) switchTab(t.dataset.k); };
$('#pvBack').onclick = () => { pv.view='list'; pv.curSeries=null; $('#pvBack').style.display='none'; fillCats(); $('#pvQ').value = pv.tab==='all' ? (pv.allQ||'') : ''; $('#pvInfo').innerHTML=''; buildList(); pvMsg(pv.tab==='all' ? 'نتائج البحث الشامل.' : 'اختر مسلسلاً.'); };
$('#pvClose').onclick = pvClose;
$('#ov').onclick = e => { if(e.target===$('#ov')) pvClose(); };
$('#pvQ').oninput = () => { if(pv){ $('#pvChs').scrollTop = 0; buildList(); } };
$('#pvCat').onchange = () => { if(pv){ $('#pvChs').scrollTop = 0; buildList(); } };
$('#pvChs').onclick = e => { const d = e.target.closest('.ch'); if(d && d.dataset.i!=null && pv) onItemClick(+d.dataset.i); };
$('#pvFav').onclick = toggleFav;
$('#pvTx').onclick = () => { if(!(pv && pv.cur)) return toast('اختر عنصراً أولاً'); pvStop(); pv.forceTx = !pv.forceTx; startAttempt(pv.cur); toast(pv.forceTx ? 'تحويل ffmpeg مفعّل' : 'تم إيقاف التحويل'); };
$('#pvFs').onclick = () => {
  const w = $('#pvWrap');
  if(NATIVE() && !w.requestFullscreen){ document.body.classList.toggle('nfs'); window.pywebview.api.fullscreen(); return; }
  try{ document.fullscreenElement ? document.exitFullscreen() : (w.requestFullscreen ? w.requestFullscreen() : $('#pvVideo').webkitEnterFullscreen()); }
  catch(e){ if(NATIVE()){ document.body.classList.toggle('nfs'); window.pywebview.api.fullscreen(); } }
};
document.addEventListener('click', e => {
  const a = e.target.closest && e.target.closest('a[target=_blank]');
  if(a && NATIVE()){ e.preventDefault(); window.pywebview.api.open_url(a.href); }
});
$('#pvPip').onclick = async () => { try{ const v=$('#pvVideo'); document.pictureInPictureElement ? await document.exitPictureInPicture() : await v.requestPictureInPicture(); }catch(e){ toast('صورة داخل صورة غير متاحة الآن'); } };
$('#pvVideo').addEventListener('volumechange', () => { if(pv){ UI.vol = $('#pvVideo').volume; saveSoon(); } });
$('#pvRefresh').onclick = async () => {
  if(!pv) return; pvMsg('جاري إعادة جلب القوائم من السيرفر...'); delete PVC[sk()];
  pv.forceLists = true;
  let err=''; try{ err = await loadContent(true); }catch(e){ err='تعذر الجلب'; }
  if(!pv) return; tabLabels(); await switchTab(pv.tab==='fav'||pv.tab==='recent' ? pv.tab : pv.tab);
  pvMsg(err ? ('تعذر الجلب: '+err) : 'تم تحديث القوائم.');
};
$('#pvSrv').onchange = () => {
  const r = rows.find(x => x.id === +$('#pvSrv').value); if(!r || !pv) return;
  const catName = $('#pvCat').selectedOptions[0] && $('#pvCat').value ? $('#pvCat').selectedOptions[0].textContent : '';
  openPlayer(r, {q:$('#pvQ').value, catName});
};
$('#pvCopy').onclick = async () => { const u = pv && pv.cur ? directUrl(pv.cur) : ''; if(u){ await copyText(u); toast('تم نسخ الرابط'); } else toast(pv&&pv.cur ? 'لا يوجد رابط مباشر لهذا النوع' : 'اختر عنصراً أولاً'); };
$('#pvExt').onclick = async () => {
  if(!(pv && pv.cur)) return toast('اختر عنصراً أولاً');
  const it = pv.cur, atts = attemptsOf(it);
  pvMsg('جاري الفتح في مشغّل خارجي...');
  const useProxy = proxyOf(pv.r) || ($('#proxy').value||'').trim();
  try{
    const res = await api('/api/open', {path:surl(it, atts[0].ext), direct: useProxy ? '' : directUrl(it)});
    if(res.ok){ toast('يُفتح الآن في ' + (res.via||'المشغّل')); pvMsg('تم الإرسال إلى المشغّل الخارجي.'); }
    else { toast('لم يُعثر على مشغّل'); pvMsg((res.reason||'تعذّر العثور على مشغّل خارجي')+' — ثبّت VLC أو mpv أو حدّد مساراً في الإعدادات.'); }
  }catch(e){ toast('تعذّر الفتح الخارجي'); }
};
function pvStep(d){
  if(!pv || !pv.list.length) return;
  let i = (pv.cur ? pv.list.findIndex(e => e.it && itemKey(e.it)===itemKey(pv.cur)) : -1) + d;
  while(i>=0 && i<pv.list.length && pv.list[i].h) i += d;
  if(i<0 || i>=pv.list.length) return;
  const box = $('#pvChs'); box.scrollTop = Math.max(0, i*RH - box.clientHeight/2);
  onItemClick(i);
}


// ---------------- البحث في قنوات السيرفرات ----------------
const chTerms = () => $('#chq').value.split(/[,،;\n]+/).map(x => x.trim()).filter(x => x.length>=2);
function renderChSaved(){
  $('#chSaved').innerHTML = (UI.chTerms||[]).map((t,i) => `<span class="chchip" data-i="${i}" title="بحث بهذه الكلمات">${esc(t)}<i data-x="${i}" title="حذف">✕</i></span>`).join('');
}
$('#chSaved').onclick = e => {
  const x = e.target.closest('[data-x]'); if(x){ UI.chTerms.splice(+x.dataset.x,1); renderChSaved(); saveSoon(); return; }
  const c = e.target.closest('.chchip'); if(c){ $('#chq').value = UI.chTerms[+c.dataset.i]; $('#btnChSearch').click(); }
};
$('#chq').addEventListener('keydown', e => { if(e.key==='Enter'){ e.preventDefault(); $('#btnChSearch').click(); } });
$('#chq').addEventListener('input', () => { UI.chq = $('#chq').value; saveSoon(); });
$('#btnChClear').onclick = () => { rows.forEach(r => delete r.chs); if(filter==='chmatch') filter='all'; if(UI.colf) delete UI.colf.chs; render(); };
$('#btnChSearch').onclick = async () => {
  if(running) return toast('انتظر حتى ينتهي الفحص الحالي');
  const terms = chTerms();
  if(!terms.length) return toast('اكتب اسم القناة (حرفان على الأقل)');
  let list = rows.filter(r => grp(r.state)==='ok');
  if(selected().length) list = selected();
  if(!list.length) return toast('لا توجد سيرفرات شغّالة — افحص السيرفرات أولاً');
  const label = terms.join(', ');
  UI.chTerms = [label].concat((UI.chTerms||[]).filter(t => t!==label)).slice(0,10); renderChSaved();
  const verify = $('#chVerify').checked, force = !!$('#btnChSearch')._force;
  const kinds = [['live','#chkLive'],['vod','#chkVod'],['series','#chkSeries']].filter(([k,id]) => $(id).checked).map(([k]) => k);
  if(!kinds.length) return toast('اختر نوعاً واحداً على الأقل: قنوات أو أفلام أو مسلسلات');
  UI.chKinds = kinds;
  rows.forEach(r => { if(!list.includes(r)) delete r.chs; });
  list.forEach(r => r.chs = {busy:true, terms});
  running = true; stop = false; setBtns(); render();
  const conc = Math.max(1, Math.min(20, +$('#conc').value || 8));
  let i = 0, done = 0, found = 0; const total = list.length;
  $('#bar').style.width = '0%';
  msg(`جاري البحث عن «${label}» في محتوى ${total} سيرفر...`);
  async function w(){
    while(!stop && i < list.length){
      const r = list[i++];
      try{
        const out = await api('/api/chsearch', checkPayload(r, {terms, verify, force, kinds, timeout: Math.max(20, +$('#tmo').value || 15)}));
        r.chs = Object.assign(out, {terms, at: Date.now()});
        if(out.count>0) found++;
      }catch(e){ r.chs = {error:'تعذّر الاتصال بالأداة', terms}; }
      done++; $('#bar').style.width = (done/total*100).toFixed(1)+'%';
      msg(`البحث في المحتوى: ${done}/${total} — وُجدت في ${found} سيرفر`);
      touch(r);
    }
  }
  await Promise.all(Array.from({length:Math.min(conc,total)}, w));
  list.forEach(r => { if(r.chs && r.chs.busy) delete r.chs; });
  running = false; setBtns();
  msg(stop ? `تم الإيقاف — وُجدت «${label}» في ${found} سيرفر.` : `انتهى البحث: «${label}» موجودة في ${found} من ${total} سيرفر.`);
  render();
};
let chmRow = null;
function openChMatches(r){
  chmRow = r; const x = r.chs || {};
  $('#chmHost').textContent = (r.title || defaultName(r.host)) + (r.username ? ' — ' + r.username : '');
  const v = x.verified;
  const by = x.by || {}, parts = [['live','📺 قنوات'],['vod','🎬 أفلام'],['series','📂 مسلسلات']].filter(([k]) => by[k]).map(([k,l]) => `${l}: ${by[k]}`).join(' — ');
  $('#chmInfo').innerHTML = `البحث: <b>${esc((x.terms||[]).join(', '))}</b> — ${x.count} نتيجة من أصل ${x.total}${parts ? '<br>'+parts : ''}` +
    (v ? `<br>${v.ok ? '✅ تم التأكد أن «'+esc(v.name)+'» تبث'+(v.kbps?` (${v.kbps} kbps)`:'') : '⚠️ لم تبث القنوات المجرَّبة: '+esc(v.why||'')}` : '') +
    (x.count > (x.matches||[]).length ? `<br><span class="small">يُعرض أول ${(x.matches||[]).length} نتيجة.</span>` : '');
  $('#chmQ').value = ''; drawChm(); $('#chm').classList.add('on');
}
function drawChm(){
  const q = $('#chmQ').value.trim().toLowerCase(), m = ((chmRow && chmRow.chs && chmRow.chs.matches) || []);
  $('#chmList').innerHTML = m.map((x,i) => [x,i]).filter(([x]) => !q || (x.name+' '+(x.cat||'')).toLowerCase().includes(q))
    .map(([x,i]) => `<div class="it"><button class="icon" data-i="${i}" title="${x.kind==='series'?'فتح الحلقات':'تشغيل'}">${x.kind==='series'?'📂':'▶️'}</button><span title="${esc(x.name)}">${({live:'📺 ',vod:'🎬 ',series:'📂 '})[x.kind||'live']}${esc(x.name)}</span><em>${esc(x.cat||'')}</em></div>`).join('') || '<div class="small">لا يوجد</div>';
}
$('#chmQ').oninput = drawChm;
$('#chmClose').onclick = () => closeOv('#chm');
$('#chmList').onclick = async e => {
  const b = e.target.closest('[data-i]'); if(!b || !chmRow) return;
  const m = chmRow.chs.matches[+b.dataset.i]; closeOv('#chm');
  const kind = m.kind || 'live';
  await openPlayer(chmRow, {q: kind==='live' ? m.name : ''});
  if(!pv) return;
  if(kind !== 'live'){
    await switchTab(kind); if(!pv) return;
    $('#pvQ').value = m.name; buildList();
    const items = (pv.data[kind] && pv.data[kind].items) || [];
    const it = items.find(c => String(c.id)===String(m.id)) || items.find(c => c.name===m.name);
    if(!it) return pvMsg('لم أجد العنصر في القائمة — اختره من القائمة.');
    const idx = pv.list.findIndex(e => e.it && String(e.it.id)===String(it.id));
    if(idx >= 0) onItemClick(idx); else if(kind==='vod') playItem(it);
    return;
  }
  const it = (pv.data.live.items||[]).find(c => (m.cmd && c.cmd===m.cmd) || (m.ou && c.ou===m.ou) || (m.id!=null && !m.cmd && !m.ou && String(c.id)===String(m.id))) || (pv.data.live.items||[]).find(c => c.name===m.name);
  if(it) playItem(it); else pvMsg('لم أجد القناة في القائمة — اخترها من القائمة.');
};


// ---------------- تخطيط ديناميكي حسب حجم النافذة ----------------
function fitTable(){
  const tw = $('.tw'); if(!tw) return;
  const top = tw.getBoundingClientRect().top + window.scrollY;
  const h = window.innerHeight - Math.min(top, window.innerHeight*0.45) - 28;
  tw.style.maxHeight = Math.max(300, h) + 'px';
}
let fitT = 0;
window.addEventListener('resize', () => { clearTimeout(fitT); fitT = setTimeout(() => { renderHead(); fitTable(); }, 120); });
$('#btnCompact').onclick = () => {
  document.body.classList.toggle('compact');
  UI.compact = document.body.classList.contains('compact');
  $('#btnCompact').textContent = UI.compact ? '⬍ إظهار خانة الإدخال' : '⬍ تكبير الجدول';
  renderHead(); fitTable(); saveSoon();
};


// ---------------- هوية المشغّل (User-Agent) لكل سيرفر ----------------
let uaRows = [];
function uaName(ua){
  if(!ua) return 'تلقائي';
  const o = [...$('#uaSel').options].find(o => o.value===ua);
  return o ? o.textContent : ua.slice(0, 24);
}
function openUa(list){
  uaRows = list; if(!list.length) return;
  const cur = list[0].ua || '';
  const known = [...$('#uaSel').options].some(o => o.value===cur);
  $('#uaSel').value = known ? cur : '__custom'; $('#uaCustom').value = known ? '' : cur;
  $('#uaCustomRow').style.display = $('#uaSel').value==='__custom' ? '' : 'none';
  $('#uaRes').innerHTML = ''; $('#uaDetMsg').textContent = '';
  $('#uamFor').textContent = list.length > 1 ? `(${list.length} سيرفرات محددة)` : (list[0].title || defaultName(list[0].host));
  $('#uam').classList.add('on');
}
$('#uaSel').onchange = () => { $('#uaCustomRow').style.display = $('#uaSel').value==='__custom' ? '' : 'none'; if($('#uaSel').value==='__custom') $('#uaCustom').focus(); };
$('#uamClose').onclick = () => closeOv('#uam');
$('#uaSave').onclick = () => {
  let ua = $('#uaSel').value; if(ua==='__custom') ua = $('#uaCustom').value.trim();
  uaRows.forEach(r => { if(ua) r.ua = ua; else delete r.ua; delete r.chs; });
  Object.keys(PVC).forEach(k => delete PVC[k]);
  closeOv('#uam'); render(); toast('تم حفظ هوية المشغّل: ' + uaName(ua));
  if($('#uaRecheck').checked) runChecks(uaRows.slice());
};
$('#bkUa').onclick = () => openUa(selected());
async function detectUa(r, stream){
  return api('/api/ua_detect', checkPayload(r, {stream: stream!==false, timeout: 10}));
}
$('#uaDetect').onclick = async () => {
  if(!uaRows.length) return;
  $('#uaDetect').disabled = true;
  if(uaRows.length > 1){                                   // عدة سيرفرات: اكتشف لكل واحد وطبّق الأفضل
    let done = 0, set = 0;
    for(const r of uaRows){
      $('#uaDetMsg').textContent = `جاري الاكتشاف ${++done}/${uaRows.length}...`;
      try{ const d = await detectUa(r, false); if(d.best && !d.default_ok){ r.ua = d.best; set++; } }catch(e){}
    }
    $('#uaDetMsg').textContent = `تم: ضُبطت هوية مخصصة لـ ${set} سيرفر، والباقي يعمل بالتلقائي أو لم تنجح معه أي هوية.`;
    $('#uaDetect').disabled = false; render(); return;
  }
  $('#uaDetMsg').textContent = 'جاري تجربة كل الهويات (قد يستغرق دقيقة)...';
  let d; try{ d = await detectUa(uaRows[0], true); }catch(e){ $('#uaDetMsg').textContent = 'تعذّر الاكتشاف'; $('#uaDetect').disabled = false; return; }
  const ic = v => v===true ? '✅' : v===false ? '❌' : '—';
  $('#uaRes').innerHTML = `<table class="tbl2"><tr><th>الهوية</th><th>قبول الحساب</th><th>البث</th><th>ملاحظة</th></tr>` +
    d.results.map(x => `<tr${x.ua===d.best?' style="background:#123d24"':''}><td>${esc(x.name)}</td><td>${ic(x.api)}</td><td>${ic(x.stream)}</td><td class="small">${esc(x.why||'')}</td></tr>`).join('') + '</table>';
  if(!d.best) $('#uaDetMsg').textContent = '⚠️ لم تنجح أي هوية — المشكلة غالباً ليست في الهوية (حساب منتهٍ أو محظور أو السيرفر متوقف).';
  else if(d.default_ok) $('#uaDetMsg').textContent = '✅ السيرفر يقبل الهوية الافتراضية — لا حاجة لهوية مخصصة (اتركها «تلقائي»).';
  else { $('#uaDetMsg').textContent = '✅ الأنسب: ' + d.best_name + ' — تم اختيارها، اضغط «حفظ».'; $('#uaSel').value = d.best; $('#uaCustomRow').style.display = 'none'; }
  if(d.default_ok) $('#uaSel').value = '';
  $('#uaDetect').disabled = false;
};

// ---------------- أحداث الجدول ----------------
$('#tb').onclick = async e => {
  const cb = e.target.closest('.rsel');
  if(cb){ const r = rows.find(x => x.id === +cb.closest('tr').dataset.id); if(r){ r.sel = cb.checked; renderStats(); } return; }
  const b = e.target.closest('button'); if(!b) return;
  const id = +b.closest('tr').dataset.id; const r = rows.find(x => x.id===id); if(!r) return;
  const a = b.dataset.act;
  if(a==='play') openPlayer(r);
  else if(a==='info') openDetails(r);
  else if(a==='chs') openChMatches(r);
  else if(a==='copy'){ await copyText(m3uOf(r)); toast('تم نسخ رابط M3U'); }
  else if(a==='re') runChecks([r]);
  else if(a==='del') delRows([id]);
  else if(a==='up') moveRow(id, -1);
  else if(a==='down') moveRow(id, 1);
};
// رقم الترتيب: يكتب المستخدم الموضع الجديد فيُنقل السيرفر إليه وتُرحَّل الباقية للأسفل
const toLatin = v => String(v).replace(/[٠-٩]/g, d => '٠١٢٣٤٥٦٧٨٩'.indexOf(d)).replace(/[۰-۹]/g, d => '۰۱۲۳۴۵۶۷۸۹'.indexOf(d));
function applyOrder(el){
  const n = parseInt(toLatin(el.value).replace(/[^0-9]/g,''), 10), old = +el.dataset.v;
  if(!n || n===old){ el.value = old; return; }
  const id = +el.dataset.id, ids = groupOf(id);
  moveBlock(ids, n - 1);
  const pos = visible().findIndex(r => r.id===ids[0]) + 1;
  msg(ids.length > 1 ? `تم نقل ${ids.length} سيرفرات محددة لتبدأ من الترتيب ${pos}.` : `تم نقل السيرفر إلى الترتيب ${pos}.`);
}
$('#tb').addEventListener('keydown', e => { const el = e.target.closest('.ordin'); if(el && e.key==='Enter'){ e.preventDefault(); el.blur(); } });
$('#tb').addEventListener('focusout', e => { const el = e.target.closest('.ordin'); if(el) applyOrder(el); });
$('#tb').addEventListener('focusin', e => { const el = e.target.closest('.ordin'); if(el) el.select(); });
$('#hr').onclick = e => { const fb = e.target.closest('.fbtn'); if(fb){ e.stopPropagation(); if(fmCol===fb.dataset.fc && $('#fmenu').classList.contains('on')) closeFMenu(); else openFMenu(fb.dataset.fc, fb); return; } if(e.target.id==='selAll'){ const on = e.target.checked; visible().forEach(r => r.sel = on); render(); } };
$('#tb').addEventListener('change', e => {
  const ts = e.target.closest('.typesel'); if(!ts) return;
  const r = rows.find(x => x.id === +ts.closest('tr').dataset.id); if(!r) return;
  const t = ts.value, old = r.type || 'xtream'; if(t===old) return;
  if(t==='stalker'){ r.portal = r.host; r.mac = (r.mac || (/^([0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(r.username)?r.username:'')).toUpperCase(); if(r.mac) r.username = r.mac; }
  if(t==='m3u'){ r.m3uurl = r.host; r.rawM3u = ''; }
  r.type = t; r.state = 'pending'; r.message = 'تم تغيير النوع — أعد الفحص'; r.m3u = ''; r.quality = ''; render();
  toast('تم تغيير النوع إلى ' + (TYPES.find(x=>x[0]===t)||[])[1] + ' — اضغط 🔁 لإعادة الفحص');
});
function bakeOrder(){
  const vis = visible(); const ids = new Set(vis.map(r => r.id));
  rows = vis.concat(rows.filter(r => !ids.has(r.id)));
  $('#sort').value = 'orig';
}
// المجموعة التي تتحرك معاً: كل المحدد إن كان السيرفر ضمن تحديد متعدد، وإلا السيرفر وحده
function groupOf(id){
  const sel = selected();
  return (sel.length > 1 && sel.some(r => r.id===id)) ? sel.map(r => r.id) : [id];
}
// ينقل مجموعة سيرفرات لتصبح متتالية (بترتيبها الحالي) بدءاً من الموضع target بين باقي السيرفرات الظاهرة
function moveBlock(ids, target){
  bakeOrder();
  const set = new Set(ids), vis = visible();
  const block = vis.filter(r => set.has(r.id)), rest = vis.filter(r => !set.has(r.id));
  if(!block.length) return;
  target = Math.max(0, Math.min(rest.length, target));
  rows = rows.filter(r => !set.has(r.id));
  const anchor = rest[target];
  const at = anchor ? rows.indexOf(anchor) : rows.length;
  rows.splice(at, 0, ...block);
  render();
  flashRows(block.map(r => r.id));
}
function flashRows(ids){
  const trs = ids.map(id => $('#tb').querySelector(`tr[data-id="${id}"]`)).filter(Boolean);
  if(!trs.length) return;
  trs[0].scrollIntoView({block:'nearest'});
  trs.forEach(tr => tr.classList.add('hi'));
  setTimeout(() => trs.forEach(tr => tr.classList.remove('hi')), 1400);
}
function moveRow(id, dir){
  const ids = groupOf(id), set = new Set(ids), vis = visible();
  const first = vis.findIndex(r => set.has(r.id)); if(first<0) return;
  const before = vis.slice(0, first).filter(r => !set.has(r.id)).length;   // موضع المجموعة بين الباقي
  const compact = vis.filter(r => set.has(r.id)).every((r,k,a) => k===0 || vis.indexOf(r)===vis.indexOf(a[k-1])+1);
  moveBlock(ids, compact ? before + dir : before);                          // أول ضغطة تجمعها متتالية
}
$('#btnReverse').onclick = () => { if(!rows.length) return toast('لا توجد سيرفرات'); bakeOrder(); rows.reverse(); render(); msg('تم عكس الترتيب.'); };
function bestOf(list){
  const sv = r => r.unlimited ? 1e12 : (r.days_left!=null ? r.days_left : -1e12);
  const og = r => grp(r.state)==='ok' ? 0 : 1;
  return list.slice().sort((a,b) => (sv(b)-sv(a)) || (og(a)-og(b)) || ((scoreOf(b)??-1)-(scoreOf(a)??-1)) || ((a.ping_ms??1e9)-(b.ping_ms??1e9)))[0];
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
    keep.add(bestOf(list).id); removed += list.length - 1;
  }
  if(!removed) return toast('لا توجد سيرفرات مكررة');
  const warn = unknown ? `\n\nتنبيه: ${unknown} مجموعة غير مفحوصة، فسيُبقى منها الأول. يُفضّل الفحص أولاً.` : '';
  if(!confirm(`سيتم حذف ${removed} سيرفراً مكرراً مع إبقاء الأفضل (الأطول صلاحية) في كل مجموعة.${warn}\n\nهل تتابع؟`)) return;
  delRows(rows.filter(r => !keep.has(r.id)).map(r => r.id));
  msg(`تم حذف ${removed} سيرفراً مكرراً، مع إبقاء الأفضل في كل مجموعة.`);
};
$('#tb').addEventListener('input', e => {
  const nm = e.target.closest('.namein');
  if(nm){ const r = rows.find(x => x.id === +nm.dataset.id); if(r){ r.title = nm.value; saveSoon(); } return; }
  const nt = e.target.closest('.notein');
  if(nt){ const r = rows.find(x => x.id === +nt.dataset.id); if(r){ r.note = nt.value; saveSoon(); } return; }
  const el = e.target.closest('.datain'); if(!el) return;
  const r = rows.find(x => x.id === +el.dataset.id); if(!r) return;
  const f = el.dataset.f, v = el.value.trim();
  if(f === 'host'){
    r.host = v;
    if(r.type === 'stalker') r.portal = v;
    if(r.type === 'm3u'){ r.m3uurl = v; r.rawM3u = ''; }
  } else if(f === 'username'){
    r.username = v;
    if(r.type === 'stalker') r.mac = v.toUpperCase();
  } else if(f === 'password'){ r.password = v; }
  r.m3u = ''; saveSoon();
});

// ---------------- الترتيب اليدوي بالسحب ----------------
let dragId = null, dragGroup = [];
function manualMode(){ return $('#sort').value==='orig' && filter==='all' && !$('#q').value.trim() && !Object.keys(UI.colf||{}).length; }
$('#tb').addEventListener('dragstart', e => {
  const tr = e.target.closest('tr'); if(!tr) return;
  if(!manualMode()){ e.preventDefault(); toast('للترتيب اليدوي: اختر «يدوي» وأزل البحث والتصفية'); return; }
  if(e.target.closest('input,button,select,a,textarea')){ e.preventDefault(); return; }
  dragId = +tr.dataset.id; tr.classList.add('dragging');
  dragGroup = groupOf(dragId);
  if(dragGroup.length > 1){
    dragGroup.forEach(id => { const t = $('#tb').querySelector(`tr[data-id="${id}"]`); if(t && t!==tr) t.classList.add('dragmate'); });
    try{ const g = document.createElement('div'); g.textContent = `⠿ ${dragGroup.length} سيرفرات`; g.style.cssText = 'position:absolute;top:-99px;background:#3b82f6;color:#fff;padding:6px 12px;border-radius:8px;font:bold 13px sans-serif'; document.body.appendChild(g); e.dataTransfer.setDragImage(g, 10, 10); setTimeout(()=>g.remove(), 0); }catch(x){}
  }
  try{ e.dataTransfer.effectAllowed='move'; e.dataTransfer.setData('text/plain', String(dragId)); }catch(x){}
});
$('#tb').addEventListener('dragover', e => {
  if(dragId==null) return;
  e.preventDefault();
  const tr = e.target.closest('tr'); const dragging = $('#tb').querySelector('tr.dragging');
  if(!tr || !dragging || tr===dragging || tr.classList.contains('dragmate')) return;
  const rect = tr.getBoundingClientRect();
  tr.parentNode.insertBefore(dragging, (e.clientY - rect.top) / rect.height > 0.5 ? tr.nextSibling : tr);
});
$('#tb').addEventListener('drop', e => { e.preventDefault(); });
$('#tb').addEventListener('dragend', () => {
  const dragging = $('#tb').querySelector('tr.dragging');
  if(dragging) dragging.classList.remove('dragging');
  $('#tb').querySelectorAll('tr.dragmate').forEach(t => t.classList.remove('dragmate'));
  if(dragId==null) return;
  const did = dragId; dragId = null;
  const order = [...$('#tb').querySelectorAll('tr')].map(tr => +tr.dataset.id);
  if(dragGroup.length > 1){                               // سحب جماعي: المجموعة كلها متتالية مكان السحب
    const set = new Set(dragGroup), at = order.indexOf(did);
    const target = order.slice(0, at).filter(id => !set.has(id)).length;
    moveBlock(dragGroup, target); dragGroup = [];
    msg('تم نقل السيرفرات المحددة معاً.'); return;
  }
  const pos = {}; order.forEach((id,i) => pos[id]=i);
  rows.sort((a,b) => (pos[a.id]??0) - (pos[b.id]??0));
  render(); msg('تم إعادة الترتيب يدوياً.');
});

// ---------------- نافذة التفاصيل ----------------
const closeOv = id => $(id).classList.remove('on');
async function openDetails(r){
  const t = r.type||'xtream';
  $('#dtHost').textContent = r.host; $('#dt').classList.add('on');
  const cell = (k,v) => `<div><b>${k}</b>${v==null||v===''?'—':v}</div>`;
  const base = [cell('الحالة', esc((ST[r.state]||ST.error)[0])), cell('الانتهاء', esc(r.exp_date || (r.unlimited?'غير محدود':''))), cell('الأيام المتبقية', r.unlimited?'∞':(r.days_left ?? '')),
    cell('الاتصالات', (r.max_cons!=null||r.active_cons!=null) ? `${r.active_cons ?? '?'} / ${r.max_cons ?? '?'}` : ''), cell('تاريخ الإنشاء', esc(r.created||'')), cell('تجريبي', r.trial==null?'':(r.trial?'نعم':'لا')),
    cell('الاستجابة', r.ping_ms!=null ? r.ping_ms+' ms':''), cell('التقييم', scoreOf(r) ?? ''), cell('عدد القنوات', tnum(r.channels)), cell('المنطقة الزمنية', esc(r.server_tz||'')),
    cell('محتوى للكبار', r.adult ? (r.adult.adult ? '🔞 نعم — ' + esc(r.adult.hits.slice(0,6).join('، ')) : 'لا') : 'لم يُفحص'),
    cell('العنوان الفعلي', esc(r.base||'')), cell('ملاحظة العنوان', esc(r.scheme_note||''))].join('');
  const hist = (r.hist||[]).slice().reverse().map(h => `<tr><td>${new Date(h.t*1000).toLocaleString()}</td><td>${esc((ST[h.s]||ST.error)[0])}</td><td>${h.e?new Date(h.e*1000).toLocaleDateString():'—'}</td><td>${h.ms!=null?h.ms+' ms':'—'}</td></tr>`).join('');
  const deep = r.deepRes ? `<h4>الفحص العميق: <span class="b ${(QL[r.quality]||[])[1]||''}">${(QL[r.quality]||['—'])[0]}</span> <span class="small">${esc(r.deepMsg||'')}</span></h4><table class="tbl2"><tr><th>القناة</th><th>نجح</th><th>TTFB</th><th>kbps</th><th>ملاحظة</th></tr>${(r.deepRes.samples||[]).map(s => `<tr><td>${esc(s.name||'')}</td><td>${s.ok?'✅':'❌'}</td><td>${s.ttfb!=null?s.ttfb+' ms':'—'}</td><td>${s.kbps ?? '—'}</td><td>${esc(s.why||'')}</td></tr>`).join('')}</table>` : '';
  $('#dtBody').innerHTML = `<div class="mgrid">${base}</div><div id="dtExtra" class="small">⏳ جاري جلب التفاصيل من السيرفر...</div>${deep}
    ${hist ? `<h4>سجل الفحوص</h4><table class="tbl2"><tr><th>الوقت</th><th>الحالة</th><th>الانتهاء</th><th>الاستجابة</th></tr>${hist}</table>` : ''}
    <div class="row" style="margin-top:12px"><button id="dtDeep" class="sec">⚡ فحص بث فعلي الآن</button><button id="dtUa" class="sec">🪪 هوية المشغّل: ${esc(uaName(r.ua))}</button></div>`;
  $('#dtUa').onclick = () => openUa([r]);
  $('#dtDeep').disabled = t!=='xtream' || grp(r.state)!=='ok';
  $('#dtDeep').onclick = async () => { $('#dtDeep').disabled = true; await deepOne(r, 20); touch(r); openDetails(r); };
  if(t!=='xtream'){ $('#dtExtra').textContent = ''; return; }
  try{
    const d = await api('/api/details', {host:r.host, username:r.username, password:r.password, proxy:proxyOf(r), ua:r.ua||''});
    if(!$('#dt').classList.contains('on')) return;
    const c = d.counts||{};
    $('#dtExtra').innerHTML = `<div class="mgrid">${cell('قنوات مباشرة', tnum(c.live))}${cell('أفلام', tnum(c.vod))}${cell('مسلسلات', tnum(c.series))}${cell('الدولة', esc(d.country||''))}${cell('IP', esc(d.ip||''))}${cell('HTTPS', d.https==null?'':(d.https?'متاح':'غير متاح'))}${cell('الصيغ', esc((d.formats||[]).join(', ')))}${cell('البروتوكول', esc(d.protocol||''))}</div>${d.error?`<div class="small">${esc(d.error)}</div>`:''}`;
  }catch(e){ $('#dtExtra').textContent = 'تعذر جلب التفاصيل.'; }
}
$('#dtClose').onclick = () => closeOv('#dt');

// ---------------- إضافة سيرفر يدوياً ----------------
function adLabels(){
  const t = $('#adType').value;
  $('#adL1').textContent = t==='stalker' ? 'رابط البوابة (Portal)' : t==='m3u' ? 'رابط M3U' : 'عنوان السيرفر';
  $('#adL2').textContent = t==='stalker' ? 'عنوان MAC' : 'اسم المستخدم';
  $('#adRow3').style.display = t==='xtream' ? '' : 'none';
  $('#adRow2').style.display = t==='m3u' ? 'none' : '';
}
$('#btnAdd').onclick = () => { adLabels(); $('#ad').classList.add('on'); $('#adHost').focus(); };
$('#adType').onchange = adLabels;
$('#adClose').onclick = () => closeOv('#ad');
$('#adGo').onclick = () => {
  const t = $('#adType').value; let host = $('#adHost').value.trim(), u = $('#adUser').value.trim(), p = $('#adPass').value.trim();
  if(!host) return toast('أدخل العنوان');
  if(!/^https?:\/\//i.test(host)) host = 'http://' + host;
  host = host.replace(/\/+$/,'');
  let a;
  if(t==='xtream'){ if(!u||!p) return toast('أدخل المستخدم وكلمة المرور'); a = {type:'xtream', host, username:u, password:p, m3u:`${host}/get.php?username=${encodeURIComponent(u)}&password=${encodeURIComponent(p)}&type=m3u_plus&output=ts`}; }
  else if(t==='stalker'){ if(!u) return toast('أدخل عنوان MAC'); a = {type:'stalker', host, portal:host, mac:u.toUpperCase(), username:u.toUpperCase(), password:''}; }
  else a = {type:'m3u', host, m3uurl:host, username:'', password:''};
  if($('#adName').value.trim()) a.title = $('#adName').value.trim();
  if(rows.some(r => key(r)===key(a))) return toast('هذا السيرفر موجود مسبقاً');
  const r = newRow(a); rows.push(r); unhideNew([r]); closeOv('#ad'); ['#adHost','#adUser','#adPass','#adName'].forEach(s => $(s).value=''); render(); runChecks([r]);
};

// ---------------- الأعمدة ----------------
function colMenu(){
  $('#colmenu').innerHTML = COLS.filter(c => c[3]).map(c => `<label><input type="checkbox" data-c="${c[0]}" ${UI.hidden.includes(c[0])?'':'checked'}> ${COLNAME[c[0]]||c[1]}</label>`).join('');
}
$('#btnCols').onclick = e => { e.stopPropagation(); colMenu(); $('#colmenu').classList.toggle('on'); };
$('#colmenu').onclick = e => e.stopPropagation();
$('#colmenu').onchange = e => {
  const c = e.target.dataset.c; if(!c) return;
  UI.hidden = UI.hidden.filter(x => x!==c); if(!e.target.checked) UI.hidden.push(c);
  render();
};
document.addEventListener('click', () => $('#colmenu').classList.remove('on'));

// ---------------- الإعدادات / السجل / التحديث ----------------
async function loadSettings(){
  try{
    const r = await api('/api/settings', {});
    SET = Object.assign(SET, r.settings||{}); CAPS = r.caps || {};
  }catch(e){}
  if(SET.timeout) $('#tmo').value = SET.timeout;
  if(SET.conc) $('#conc').value = SET.conc;
  if(SET.deep_default) $('#deep').checked = true;
  if(SET.count_default) $('#countch').checked = true;
}
function fillSettings(){
  $('#sTmo').value = SET.timeout; $('#sConc').value = SET.conc; $('#sDom').value = SET.per_domain; $('#sAuto').value = SET.auto_hours;
  $('#sDeep').checked = !!SET.deep_default; $('#sCount').checked = !!SET.count_default; $('#sAdult').checked = SET.adult_scan!==false; $('#sEngine').value = SET.window_engine || 'auto'; $('#sUaAuto').checked = SET.ua_auto!==false; $('#sUpd').checked = SET.updates!==false;
  $('#sPlayer').value = SET.ext_player || 'auto'; $('#sPlayerPath').value = SET.ext_player_path || ''; $('#sDirect').checked = !!SET.ext_direct; $('#sFfmpeg').value = SET.ffmpeg_path || '';
  const f = (n,p) => `${n}: ${p ? '✅' : '—'}`;
  $('#sCaps').textContent = `الأدوات المكتشفة — ${f('ffmpeg',CAPS.ffmpeg)} | ${f('VLC',CAPS.vlc)} | ${f('mpv',CAPS.mpv)}${CAPS.iina!==undefined&&CAPS.platform==='darwin'?' | '+f('IINA',CAPS.iina):''}${CAPS.platform&&CAPS.platform.startsWith('win')?' | '+f('PotPlayer',CAPS.potplayer):''}`;
  $('#sDir').textContent = 'مجلد البيانات: ' + (CAPS.data_dir||'') + ' — الإصدار ' + (CAPS.version||'');
}
$('#btnSet').onclick = async () => { await loadSettings(); fillSettings(); $('#st').classList.add('on'); };
$('#stClose').onclick = () => closeOv('#st');
$('#sUpdNow').onclick = () => { closeOv('#st'); checkUpdate(true); };
$('#sSave').onclick = async () => {
  const d = {timeout:+$('#sTmo').value||15, conc:+$('#sConc').value||8, per_domain:+$('#sDom').value||2, auto_hours:+$('#sAuto').value||0,
    deep_default:$('#sDeep').checked, count_default:$('#sCount').checked, adult_scan:$('#sAdult').checked, window_engine:$('#sEngine').value, ua_auto:$('#sUaAuto').checked, updates:$('#sUpd').checked, ext_player:$('#sPlayer').value,
    ext_player_path:$('#sPlayerPath').value.trim(), ext_direct:$('#sDirect').checked, ffmpeg_path:$('#sFfmpeg').value.trim()};
  try{ const r = await api('/api/settings', {op:'set', data:d}); SET = Object.assign(SET, r.settings||d); CAPS = r.caps||CAPS; }catch(e){ return toast('تعذر الحفظ'); }
  $('#tmo').value = SET.timeout; $('#conc').value = SET.conc; closeOv('#st'); toast('تم حفظ الإعدادات');
};
async function showLog(clear){
  $('#lg').classList.add('on');
  try{
    const r = await api('/api/log', clear ? {op:'clear'} : {});
    $('#lgTb').innerHTML = '<tr><th>الوقت</th><th>النوع</th><th>الرسالة</th></tr>' + (r.items||[]).slice().reverse().map(x => `<tr><td>${esc(x.t)}</td><td>${esc(x.k)}</td><td>${esc(x.m)}</td></tr>`).join('');
  }catch(e){ $('#lgTb').innerHTML = '<tr><td>تعذر جلب السجل</td></tr>'; }
}
$('#btnLog').onclick = () => showLog(false);
$('#lgRefresh').onclick = () => showLog(false);
$('#lgClear').onclick = () => showLog(true);
$('#lgClose').onclick = () => closeOv('#lg');
$('#lgCopy').onclick = () => { copyText([...$('#lgTb').querySelectorAll('tr')].map(tr => [...tr.children].map(td => td.textContent).join('\t')).join('\n')); toast('تم نسخ السجل'); };
async function checkUpdate(force){
  if(SET.updates===false && !force) return;
  try{
    const u = await api('/api/update_check', {force:!!force});
    const el = $('#upd');
    if(u && u.newer){
      el.innerHTML = `🔔 يتوفر إصدار أحدث <b class="ltr">${esc(u.version)}</b> (الحالي <span class="ltr">${esc(CAPS.version||'')}</span>) — ` +
        (u.auto ? `<button id="updGo">⬇️ حدّث الآن</button> ` : '') +
        `<a href="${esc(u.url)}" target="_blank" rel="noopener" style="color:#93c5fd">صفحة الإصدار</a> <span id="updSt" class="small"></span>`;
      el.classList.add('on');
      if(u.auto) $('#updGo').onclick = runUpdate;
    } else if(force){ toast('أنت تستخدم أحدث إصدار'); }
  }catch(e){ if(force) toast('تعذّر التحقق من التحديثات'); }
}
async function runUpdate(){
  $('#updGo').disabled = true;
  try{ await saveNow(); }catch(e){}
  let st;
  try{ st = await api('/api/update_apply', {}); }catch(e){ $('#updSt').textContent = 'تعذّر بدء التحديث'; $('#updGo').disabled = false; return; }
  const tick = async () => {
    try{ st = await api('/api/update_status', {}); }catch(e){ $('#updSt').textContent = '🔄 جاري إعادة التشغيل... يمكنك إغلاق هذه النافذة إن لم تُغلق تلقائياً.'; return; }
    if(st.stage==='error'){ $('#updSt').textContent = '⚠️ ' + st.error; $('#updGo').disabled = false; return; }
    $('#updSt').textContent = (st.msg||'') + (st.stage==='download' ? ` ${st.pct||0}%` : '');
    setTimeout(tick, 700);
  };
  tick();
}

// ---------------- حفظ الجلسة واستعادتها ----------------
let saveT = 0, lastSaved = '';
function sessionData(){
  return {v:1, text:$('#inp').value, sort:$('#sort').value, filter, ui:UI, seq,
    rows: rows.map(r => { const o = Object.assign({}, r); delete o.deepBusy; delete o.sel; if(o.state==='checking') o.state='pending';
      if(o.chs){ o.chs = Object.assign({}, o.chs, {matches:(o.chs.matches||[]).slice(0,60)}); if(o.chs.busy) delete o.chs; } return o; })};
}
function saveSoon(){ clearTimeout(saveT); saveT = setTimeout(saveNow, 1500); }
async function saveNow(){
  try{ const s = JSON.stringify(sessionData()); if(s===lastSaved) return; lastSaved = s; await api('/api/session', {op:'save', data:JSON.parse(s)}); }catch(e){}
}
setInterval(() => { fetch('/api/ping', {method:'POST', headers:{'X-IPTV-Checker':'1'}, body:'{}'}).catch(()=>{}); }, 10000);
window.addEventListener('pagehide', () => {
  try{ navigator.sendBeacon('/api/session?t='+window.__TOKEN__, new Blob([JSON.stringify({op:'save', data:sessionData()})], {type:'application/json'})); }catch(e){}
  try{ navigator.sendBeacon('/api/bye?t='+window.__TOKEN__, new Blob(['{}'], {type:'application/json'})); }catch(e){}
});
async function restoreSession(){
  try{
    const r = await api('/api/session', {});
    const d = r.data; if(!d) return;
    if(d.ui) UI = Object.assign(UI, d.ui, {hidden:Array.isArray(d.ui.hidden)?d.ui.hidden:UI.hidden});
    if(Array.isArray(d.rows) && d.rows.length){
      rows = d.rows; seq = Math.max(d.seq||0, ...rows.map(x => x.id||0));
      if(d.sort) $('#sort').value = d.sort; if(d.filter) filter = d.filter;
      toast(`تمت استعادة الجلسة السابقة (${rows.length} سيرفر)`, '🗑 ابدأ من جديد', () => { rows = []; render(); });
    }
    if(d.text && !$('#inp').value) $('#inp').value = d.text;
    if(UI.chq) $('#chq').value = UI.chq;
    if(Array.isArray(UI.chKinds)){ $('#chkLive').checked = UI.chKinds.includes('live'); $('#chkVod').checked = UI.chKinds.includes('vod'); $('#chkSeries').checked = UI.chKinds.includes('series'); }
    renderChSaved();
  }catch(e){}
}

// ---------------- فحص دوري تلقائي ----------------
setInterval(() => {
  const h = +SET.auto_hours || 0; if(!h || running || !rows.length) return;
  if(Date.now() - (UI.lastAuto||0) >= h*3600*1000){ UI.lastAuto = Date.now(); msg('فحص دوري تلقائي...'); runChecks(rows.slice()); }
}, 60000);

// ---------------- اختصارات لوحة المفاتيح ----------------
document.addEventListener('keydown', e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName||'');
  if(e.key==='Escape'){
    if(document.body.classList.contains('nfs')){ $('#pvFs').click(); return; }
    if($('#fmenu').classList.contains('on')) return closeFMenu();
    if($('#ov').classList.contains('on')) return pvClose();
    for(const id of ['#uam','#dt','#ad','#st','#lg','#chm']) if($(id).classList.contains('on')){ closeOv(id); return; }
    return;
  }
  if((e.ctrlKey||e.metaKey) && e.key==='Enter' && !running){ e.preventDefault(); $('#btnGo').click(); return; }
  if(!pv || !$('#ov').classList.contains('on') || typing) return;
  const v = $('#pvVideo');
  if(e.key==='ArrowDown' || e.key==='PageDown'){ e.preventDefault(); pvStep(1); }
  else if(e.key==='ArrowUp' || e.key==='PageUp'){ e.preventDefault(); pvStep(-1); }
  else if(e.key===' '){ e.preventDefault(); v.paused ? v.play() : v.pause(); }
  else if(e.key==='f' || e.key==='F'){ $('#pvFs').click(); }
  else if(e.key==='m' || e.key==='M'){ v.muted = !v.muted; }
  else if(e.key==='ArrowLeft' && pv.cur && kindOf(pv.cur)!=='live'){ v.currentTime = Math.max(0, v.currentTime-10); }
  else if(e.key==='ArrowRight' && pv.cur && kindOf(pv.cur)!=='live'){ v.currentTime += 10; }
  else if(/^[1-9]$/.test(e.key)){ const items = pv.list.map((x,i)=>[x,i]).filter(([x])=>!x.h); const t = items[+e.key-1]; if(t) onItemClick(t[1]); }
});

$('#sort').onchange = render;
$('#q').oninput = scheduleRender;
$('#stats').onclick = e => { const s = e.target.closest('.stat'); if(s){ filter = s.dataset.f; render(); } };
['#deep','#countch','#noport'].forEach(s => { const el = $(s); if(el) el.addEventListener('change', render); });

(async function init(){
  await loadSettings();
  await restoreSession();
  if(UI.compact){ document.body.classList.add('compact'); $('#btnCompact').textContent = '⬍ إظهار خانة الإدخال'; }
  render(); fitTable();
  checkUpdate();
})();

</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MoIPTVChecker/" + VERSION

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

    @staticmethod
    def _tok_ok(q):
        return secrets.compare_digest(str(q.get('t') or ''), TOKEN)

    def do_GET(self):
        _touch()
        sp = urllib.parse.urlsplit(self.path)
        p = sp.path
        q = _query(sp.query)
        if p in ('/', '/index.html'):
            page = (HTML.replace('__VER__', VERSION).replace('__PROXYJSON__', json.dumps(_PROXY.get('url', '')))
                    .replace("__TOKVAL__", TOKEN))
            self._send(200, page, 'text/html; charset=utf-8')
        elif p.startswith('/lib/'):
            data = get_lib(p[5:])
            if data is None:
                return self._send(404, 'missing', 'text/plain; charset=utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write(data)
        elif p in ('/stream', '/relay', '/transcode'):
            if not self._tok_ok(q):
                return self._send(403, 'forbidden', 'text/plain; charset=utf-8')
            srv = _REG.get(q.get('s') or '')
            _TL.ua = (srv or {}).get('ua', '')
            if not srv:
                return self._send(400, 'unknown server', 'text/plain; charset=utf-8')
            try:
                if p == '/transcode':
                    self._transcode(srv, q)
                else:
                    self._serve(srv, self._upstream_url(srv, q), q)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log_event('stream', f'{p}: {e}')
                try:
                    self._send(502, str(e)[:200], 'text/plain; charset=utf-8')
                except Exception:
                    pass
        else:
            self._send(404, 'not found', 'text/plain; charset=utf-8')

    @staticmethod
    def _upstream_url(srv, q):
        if q.get('cmd') and srv['type'] == 'stalker':
            return stalker_link(srv['host'], srv['mac'], q['cmd'], srv['proxy'])
        if q.get('u'):
            return q['u']
        seg = {'live': 'live', 'vod': 'movie', 'movie': 'movie', 'series': 'series'}.get(q.get('kind') or 'live', 'live')
        ext = (q.get('ext') or 'ts').lstrip('.')
        sid = q.get('id') or ''
        return (f"{srv['host']}/{seg}/{urllib.parse.quote(srv['username'])}/"
                f"{urllib.parse.quote(srv['password'])}/{sid}.{ext}")

    def _serve(self, srv, url, q):
        """يمرّر البث من السيرفر إلى المشغّل (مع البروكسي الخاص بالسيرفر) ويعيد كتابة قوائم HLS."""
        if q.get('probe'):
            return self._send(200, json.dumps(probe_upstream(url, srv), ensure_ascii=False),
                              'application/json; charset=utf-8')
        up, err = open_upstream(url, srv, self.headers.get('Range'))
        if up is None:
            code = err.code if isinstance(err, urllib.error.HTTPError) else 502
            log_event('stream', f'{code} {url}')
            try:
                self.send_response(code)
                self.send_header('Content-Length', '0')
                self.end_headers()
            except Exception:
                pass
            return
        try:
            final = up.geturl()
            ctype = up.headers.get('Content-Type') or ''
            path = urllib.parse.urlsplit(final).path.lower()
            if 'mpegurl' in ctype.lower() or path.endswith(('.m3u8', '.m3u')):
                body = up.read(30_000_000).decode('utf-8', 'replace')
                if body.lstrip().startswith('#EXTM3U'):
                    sid = q.get('s')
                    text = rewrite_hls(body.lstrip(), final,
                                       lambda u: f"/relay?t={TOKEN}&s={sid}&u={urllib.parse.quote(u, safe='')}")
                    return self._send(200, text, 'application/vnd.apple.mpegurl')
                return self._send(200, body, ctype or 'text/plain; charset=utf-8')
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            self.send_response(getattr(up, 'status', 200) or 200)
            for h in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
                v = up.headers.get(h)
                if v:
                    self.send_header(h, v)
            if not up.headers.get('Content-Type'):
                self.send_header('Content-Type', 'video/mp2t' if path.endswith('.ts') else 'application/octet-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            rd = getattr(up, 'read1', None) or up.read     # read1: يرسل ما وصل فوراً بدل انتظار امتلاء الكتلة
            while True:
                chunk = rd(131072)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log_event('stream', f'تعطّل البث: {e}')
        finally:
            try:
                up.close()
            except Exception:
                pass

    def _transcode(self, srv, q):
        """تحويل لحظي عبر ffmpeg للترميزات التي لا يدعمها المتصفح (HEVC / AC3 ...)."""
        ff = find_ffmpeg()
        if not ff:
            return self._send(501, 'ffmpeg not found', 'text/plain; charset=utf-8')
        src = q.get('src') or ''
        if not (src.startswith('/stream?') or src.startswith('/relay?')):
            return self._send(400, 'bad src', 'text/plain; charset=utf-8')
        port = self.server.server_address[1]
        inp = f'http://127.0.0.1:{port}{src}'
        mode = q.get('mode') or 'full'
        fmt = q.get('fmt') or 'ts'
        args = [ff, '-hide_banner', '-loglevel', 'error', '-nostdin', '-fflags', '+genpts+discardcorrupt',
                '-i', inp, '-map', '0:v:0?', '-map', '0:a:0?']
        if mode == 'audio':
            args += ['-c:v', 'copy']
        else:
            args += ['-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency', '-pix_fmt', 'yuv420p',
                     '-vf', "scale=-2:'min(720,ih)'"]
        args += ['-c:a', 'aac', '-b:a', '128k', '-ac', '2']
        if fmt == 'mp4':
            args += ['-f', 'mp4', '-movflags', 'frag_keyframe+empty_moov+default_base_moof', 'pipe:1']
            ctype = 'video/mp4'
        else:
            args += ['-f', 'mpegts', '-muxdelay', '0', 'pipe:1']
            ctype = 'video/mp2t'
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        log_event('ffmpeg', f'بدء التحويل ({mode}/{fmt})')
        try:
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            while True:
                chunk = (getattr(proc.stdout, 'read1', None) or proc.stdout.read)(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
            try:
                proc.stdout.close()
            except Exception:
                pass

    def do_POST(self):
        raw = self.path
        path, _, qs = raw.partition('?')
        self.path = path
        # حماية: يقبل الطلبات من واجهة الأداة فقط (ترويسة خاصة أو رمز الجلسة)
        if self.headers.get('X-IPTV-Checker') != '1' and not self._tok_ok(_query(qs)):
            return self._send(403, 'forbidden', 'text/plain; charset=utf-8')
        try:
            n = int(self.headers.get('Content-Length') or 0)
            data = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            return self._send(400, 'bad json', 'text/plain; charset=utf-8')
        if not isinstance(data, dict):
            data = {}
        _TL.ua = str(data.get('ua') or '').strip()[:300]
        if path == '/api/bye':                          # النافذة أُغلقت
            _ALIVE['bye'] = time.time()
            return self._send(200, '{}', 'application/json; charset=utf-8')
        if self.headers.get('X-IPTV-Checker') == '1':  # الإرسال عند الإغلاق (beacon) لا يُعدّ نشاطاً
            _touch()
        if path == '/api/ping':
            return self._send(200, '{"ok":true}', 'application/json; charset=utf-8')
        op_ = _opener_for(str(data.get('proxy') or ''))
        try:
            out = self._route(path, data, op_)
        except KeyError:
            return self._send(400, 'missing fields', 'text/plain; charset=utf-8')
        except Exception as e:
            log_event('error', f'{path}: {e}')
            out = {'error': str(e)[:200]}
        if out is None:
            return
        self._send(200, json.dumps(out, ensure_ascii=False), 'application/json; charset=utf-8')

    def _route(self, path, data, op_):
        sget = lambda k, d='': str(data.get(k, d))
        if path == '/api/extract':
            return {'accounts': extract(data.get('text', ''))}
        if path == '/api/import_xlsx':
            try:
                return {'accounts': parse_xlsx(base64.b64decode(data.get('xlsx_b64', '') or ''))}
            except Exception as e:
                self._send(400, 'xlsx parse error: ' + str(e)[:120], 'text/plain; charset=utf-8')
                return None
        if path == '/api/reg':
            return {'s': reg_server(data)}
        if path == '/api/content':
            k = 'content|' + hist_key('xtream', data) + '|' + _cur_ua()
            return cached_list(k, lambda: get_content(sget('host'), sget('username'), sget('password'), opener=op_),
                               bool(data.get('force')), lambda r: bool((r.get('live') or {}).get('items')))
        if path == '/api/streams':
            kind = sget('kind', 'live')
            k = 'streams|' + kind + '|' + hist_key('xtream', data) + '|' + _cur_ua()
            return cached_list(k, lambda: get_streams(sget('host'), sget('username'), sget('password'), kind, opener=op_),
                               bool(data.get('force')), lambda r: bool(r.get('items')))
        if path == '/api/series':
            return get_series_info(sget('host'), sget('username'), sget('password'), data.get('series_id'), opener=op_)
        if path == '/api/vod_resolve':
            kind = 'series' if data.get('kind') == 'series' else 'vod'
            info = get_vod_info(sget('host'), sget('username'), sget('password'), data.get('vod_id'), opener=op_) \
                if kind == 'vod' else {}
            exts = [info.get('ext'), data.get('ext')] + ['mkv', 'mp4', 'avi', 'm4v', 'mov', 'ts', 'flv', 'wmv']
            with _dom_sem(sget('host')):
                return vod_resolve(data, data.get('vod_id'), exts, kind=kind)
        if path == '/api/vodinfo':
            return get_vod_info(sget('host'), sget('username'), sget('password'), data.get('vod_id'), opener=op_)
        if path == '/api/epg':
            return get_epg(sget('host'), sget('username'), sget('password'), data.get('stream_id'), opener=op_)
        if path == '/api/stalker':
            k = 'stalker|' + hist_key('stalker', data) + '|' + _cur_ua()
            return cached_list(k, lambda: stalker_channels(sget('portal'), sget('mac'), sget('proxy')),
                               bool(data.get('force')), lambda r: bool(r.get('items')))
        if path == '/api/stalker_link':
            try:
                return {'url': stalker_link(sget('portal'), sget('mac'), sget('cmd'), sget('proxy'))}
            except Exception as e:
                return {'url': '', 'error': str(e)[:160]}
        if path == '/api/deep':
            res = deep_check(sget('host'), sget('username'), sget('password'),
                             timeout=max(5, min(60, int(data.get('timeout') or DEFAULT_TIMEOUT))), proxy=sget('proxy'))
            log_event('deep', f"{res.get('quality')} {sget('host')} ({res.get('ok')}/{res.get('total')})")
            return res
        if path == '/api/details':
            return get_details(sget('host'), sget('username'), sget('password'), proxy=sget('proxy'))
        if path == '/api/caps':
            return get_caps()
        if path == '/api/settings':
            return {'settings': set_settings(data.get('data')) if data.get('op') == 'set' else get_settings(),
                    'caps': get_caps()}
        if path == '/api/log':
            if data.get('op') == 'clear':
                _LOG.clear()
            if data.get('op') == 'add':                      # أحداث المشغّل من الواجهة (للتشخيص)
                log_event('player', str(data.get('msg') or '')[:300])
                return {'ok': True}
            return {'items': list(_LOG)}
        if path == '/api/update_check':
            if data.get('force'):
                return update_check()
            return update_check() if get_settings().get('updates', True) else {'newer': False}
        if path == '/api/update_apply':
            return update_apply()
        if path == '/api/update_status':
            return dict(_UPD)
        if path == '/api/session':
            if data.get('op') == 'save':
                return {'ok': _write_json(_SESSION_PATH, data.get('data') or {})}
            try:
                with open(_SESSION_PATH, encoding='utf-8') as f:
                    return {'data': json.load(f)}
            except Exception:
                return {'data': None}
        if path == '/api/codec':
            src = sget('src')
            if not (src.startswith('/stream?') or src.startswith('/relay?')):
                return {'ok': False, 'reason': 'bad src'}
            return probe_codec(f'http://127.0.0.1:{self.server.server_address[1]}{src}')
        if path == '/api/proxy':
            url = sget('url').strip()
            try:
                apply_proxy(url)
                ip = test_proxy() if url else None
                save_cfg({'proxy': url})
                return {'ok': True, 'ip': ip, 'proxy': url}
            except Exception as e:
                return {'ok': False, 'reason': str(e)[:200]}
        if path == '/api/find_proxy':
            return find_working_proxy(sget('country', 'us'), apply=False)
        if path == '/api/find_proxies':
            try:
                n = max(1, min(10, int(data.get('count', 5))))
            except (TypeError, ValueError):
                n = 5
            return find_proxies(sget('country', 'us'), count=n)
        if path == '/api/autoproxy':
            return find_working_proxy(sget('country', 'any'))
        if path == '/api/export':
            try:
                blob = build_xlsx(data.get('rows') or [])
            except Exception as e:
                self._send(500, 'export error: ' + str(e)[:120], 'text/plain; charset=utf-8')
                return None
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
            return None
        if path == '/api/open':
            src = sget('path')
            if not (src.startswith('/stream?') or src.startswith('/relay?')):
                self._send(400, 'bad path', 'text/plain; charset=utf-8')
                return None
            st = get_settings()
            direct = sget('direct')
            url = direct if (direct and st.get('ext_direct')) else f'http://127.0.0.1:{self.server.server_address[1]}{src}'
            try:
                return {'ok': True, 'via': launch_external(url, st.get('ext_player', 'auto'), st.get('ext_player_path', ''))}
            except Exception as e:
                return {'ok': False, 'reason': str(e)[:160]}
        if path == '/api/ua_detect':
            typ = data.get('type') or 'xtream'
            hk = data.get('portal') if typ == 'stalker' else (data.get('url') if typ == 'm3u' else data.get('host'))
            with _dom_sem(hk):
                return ua_detect(data, max(4, min(30, int(data.get('timeout') or 10))), data.get('stream', True) is not False)
        if path == '/api/adult':
            typ = data.get('type') or 'xtream'
            hk = data.get('portal') if typ == 'stalker' else (data.get('url') if typ == 'm3u' else data.get('host'))
            with _dom_sem(hk):
                return adult_scan(data, max(5, min(120, int(data.get('timeout') or 20))))
        if path == '/api/chsearch':
            typ = data.get('type') or 'xtream'
            hk = data.get('portal') if typ == 'stalker' else (data.get('url') if typ == 'm3u' else data.get('host'))
            terms = data.get('terms') or []
            if isinstance(terms, str):
                terms = re.split(r'[,،;\n]+', terms)
            with _dom_sem(hk):
                return channel_search(data, terms, bool(data.get('verify')),
                                      max(5, min(120, int(data.get('timeout') or 25))), bool(data.get('force')),
                                      data.get('kinds') or ['live'])
        if path == '/api/check':
            t = max(3, min(120, int(data.get('timeout') or DEFAULT_TIMEOUT)))
            proxy = sget('proxy')
            typ = sget('type', 'xtream')
            hk = data.get('portal') if typ == 'stalker' else (data.get('url') if typ == 'm3u' else data.get('host'))
            with _dom_sem(hk):
                if typ == 'stalker':
                    out = check_stalker(sget('portal'), sget('mac'), timeout=t, proxy=proxy)
                elif typ == 'm3u':
                    out = check_m3u(sget('url'), timeout=t, proxy=proxy)
                else:
                    out = check(sget('host'), sget('username'), sget('password'), timeout=t,
                                count_channels=bool(data.get('count')), proxy=proxy)
            prev, hist = hist_record(hist_key(typ, data), out)
            out['prev'], out['hist'] = prev, hist
            log_event('check', f"{typ} {out.get('state')} {hk} {data.get('username', '')} {out.get('message', '')}")
            return out
        self._send(404, 'not found', 'text/plain; charset=utf-8')
        return None


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
    threading.Thread(target=_hist_saver, daemon=True).start()
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
    print(f"  Mo IPTV Checker v{VERSION} is running")
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

    # وضع المتصفح: افتح في المتصفح الافتراضي مباشرة (--browser)
    force_browser = '--browser' in sys.argv or '-b' in sys.argv
    # نافذة أصلية بوضع «التطبيق» من كروم/إيدج (محرك Chromium) — إغلاقها يُنهي البرنامج
    # محرك النافذة من الإعدادات: auto / webview (سفاري المستقلة) / chrome / browser
    try:
        eng = str(get_settings().get('window_engine') or 'auto')
    except Exception:
        eng = 'auto'
    if force_browser:
        eng = 'browser'
    order = {'webview': ['webview', 'chrome'], 'chrome': ['chrome', 'webview'], 'browser': [],
             'auto': ['webview', 'chrome']}.get(eng, ['webview', 'chrome'])
    for e in order:
        if e == 'webview' and os.environ.get('MOIPTV_NO_WEBVIEW') != '1' and _open_webview(url):
            return
        if e == 'chrome' and _open_chrome_app(url):
            return

    # المتصفح الافتراضي (الوضع العادي أو كبديل احتياطي)
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        _wait_for_ui(url, first_wait=120, idle=300)
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


_ALIVE = {'seen': 0.0, 'bye': 0.0}


def _touch():
    _ALIVE['seen'] = time.time()


def _wait_for_ui(url, proc=None, first_wait=40, idle=150):
    """يبقي الخادم يعمل طالما الواجهة مفتوحة (نبضة كل 10 ثوانٍ).
    لا نعتمد على انتهاء عملية المتصفح: على ماك يبقى كروم يعمل بعد إغلاق النافذة،
    وإن كان يعمل مسبقاً تنتهي عملية الإطلاق فوراً وتُسلَّم النافذة للنسخة القائمة."""
    start = time.time()
    fallback = False
    while True:
        time.sleep(1)
        now = time.time()
        seen, bye = _ALIVE['seen'], _ALIVE['bye']
        if not seen:
            if now - start > first_wait and not fallback:   # لم تتصل أي نافذة — افتح المتصفح الافتراضي
                fallback = True
                start = now
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
            elif fallback and now - start > first_wait * 3:
                return
            continue
        if bye and now - bye > 8 and _ALIVE['seen'] <= bye:  # أُغلقت النافذة ولم يُعِد أحد التحميل
            return
        if now - seen > idle:
            return


class _WebApi:
    """دوال تستدعيها الواجهة داخل النافذة المستقلة (pywebview)."""
    def __init__(self):
        self.win = None

    def save_file(self, name, b64):
        import base64
        try:
            import webview
            kind = getattr(getattr(webview, 'FileDialog', None), 'SAVE', None) or webview.SAVE_DIALOG
            res = self.win.create_file_dialog(kind, save_filename=name)
        except Exception as e:
            return {'ok': False, 'error': str(e)[:200]}
        if not res:
            return {'ok': False, 'cancel': True}
        path = res if isinstance(res, str) else res[0]
        with open(path, 'wb') as f:
            f.write(base64.b64decode(b64))
        return {'ok': True, 'path': path}

    def open_url(self, url):
        if str(url).startswith(('http://', 'https://')):
            webbrowser.open(url)
        return True

    def fullscreen(self):
        try:
            self.win.toggle_fullscreen()
        except Exception:
            pass
        return True


def _open_webview(url):
    """نافذة تطبيق مستقلة حقيقية (أيقونة خاصة في شريط المهام/Dock) عبر pywebview إن كانت متوفرة.
    على ماك تستخدم WKWebView المدمج في النظام؛ إغلاق النافذة يُنهي البرنامج."""
    try:
        import webview
    except Exception:
        return False
    try:
        api = _WebApi()
        api.win = webview.create_window(f'Mo IPTV Checker {VERSION}', url, js_api=api,
                                        width=1360, height=880, min_size=(900, 600),
                                        background_color='#0f1419')
        try:
            webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
        except Exception:
            pass
        webview.start(private_mode=False, storage_path=os.path.join(_DATA_DIR, 'webview'))
        return True
    except Exception as e:
        log_event('error', f'webview: {e}')
        return False


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
    except Exception:
        return False
    try:
        _wait_for_ui(url, proc)
        return True
    except Exception:
        return False


if __name__ == '__main__':
    main()
