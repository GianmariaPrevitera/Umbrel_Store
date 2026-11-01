#!/usr/bin/env python3
"""Converte playlist .m3u (HLS) in mp4 con ffmpeg: video alla massima risoluzione,
audio e sottotitoli italiano/inglese. Stessa logica dello script shell originale."""
import collections, json, os, queue, re, subprocess, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUT_DIR = os.environ.get('OUT_DIR', '/downloads')
MAX_JOBS = int(os.environ.get('MAX_JOBS', '3'))
PORT = int(os.environ.get('PORT', '8080'))
# ffmpeg può aprire solo questi protocolli: niente file locali
WL = 'http,https,tcp,tls,crypto'
FINISHED = ('done', 'error', 'skipped', 'cancelled')

jobs = collections.OrderedDict()
lock = threading.Lock()
pending = queue.Queue()


def safe_name(n):
    n = os.path.basename(str(n).replace('\\', '/'))
    n = re.sub(r'\.m3u8?$', '', n, flags=re.I)
    n = re.sub(r'[\x00-\x1f<>:"|?*]', '_', n).strip(' .')
    return (n or 'video')[:150]


def is_http(u):
    return bool(u) and re.match(r'^https?://', u, re.I) is not None


def parse_playlist(text):
    lines = [l.strip() for l in text.splitlines()]
    best = None
    for i, l in enumerate(lines):
        if l.startswith('#EXT-X-STREAM-INF'):
            m = re.search(r'RESOLUTION=(\d+)x(\d+)', l)
            if not m:
                continue
            url = next((x for x in lines[i + 1:] if x and not x.startswith('#')), None)
            if not url:
                continue
            key = (int(m.group(1)), int(m.group(2)))
            if best is None or key > best[0]:
                best = (key, url)

    def media(kind, lang):
        for l in lines:
            if ('TYPE=' + kind) in l and lang in l.lower():
                m = re.search(r'URI="([^"]*)"', l)
                if m and m.group(1):
                    return m.group(1)
        return None

    return {
        'video': best[1] if best else None,
        'audio_it': media('AUDIO', 'italian'),
        'audio_en': media('AUDIO', 'english'),
        'subs_it': media('SUBTITLES', 'italian'),
        'subs_en': media('SUBTITLES', 'english'),
    }


def build_cmd(p, tmp):
    cmd = ['ffmpeg', '-hide_banner', '-nostdin', '-n', '-nostats', '-progress', 'pipe:1']
    inputs = [p['video']]
    maps = ['-map', '0:v:0']
    codecs = ['-c:v', 'copy']
    idx, ai, si = 1, 0, 0

    if p['audio_it']:
        inputs.append(p['audio_it'])
        maps += ['-map', '%d:a:0' % idx]
        idx += 1
    else:
        maps += ['-map', '0:a:0']
    maps += ['-metadata:s:a:%d' % ai, 'language=ita', '-metadata:s:a:%d' % ai, 'title=Italiano']
    codecs += ['-c:a:%d' % ai, 'copy']
    ai += 1

    if p['audio_en']:
        inputs.append(p['audio_en'])
        maps += ['-map', '%d:a:0' % idx, '-metadata:s:a:%d' % ai, 'language=eng',
                 '-metadata:s:a:%d' % ai, 'title=English']
        codecs += ['-c:a:%d' % ai, 'copy']
        idx += 1
        ai += 1

    for url, lang, title in ((p['subs_it'], 'ita', 'Italiano'), (p['subs_en'], 'eng', 'English')):
        if not url:
            continue
        inputs.append(url)
        maps += ['-map', str(idx), '-metadata:s:s:%d' % si, 'language=' + lang,
                 '-metadata:s:s:%d' % si, 'title=' + title]
        codecs += ['-c:s:%d' % si, 'mov_text']
        idx += 1
        si += 1

    for u in inputs:
        cmd += ['-protocol_whitelist', WL, '-i', u]
    return cmd + maps + codecs + ['-movflags', '+faststart', '-f', 'mp4', tmp]


def public(j):
    return {k: j[k] for k in ('id', 'name', 'status', 'percent', 'eta', 'message')}


def fail(job, msg):
    with lock:
        job['status'] = 'error'
        job['message'] = msg


def run_job(job):
    if job['cancel']:
        with lock:
            job['status'] = 'cancelled'
        return
    out = os.path.join(OUT_DIR, job['name'] + '.mp4')
    tmp = out + '.part'
    if os.path.exists(out):
        with lock:
            job['status'] = 'skipped'
            job['message'] = 'File già presente'
        return

    p = parse_playlist(job['content'])
    if not p['video']:
        return fail(job, 'Nessuno stream video trovato')
    for u in p.values():
        if u and not is_http(u):
            return fail(job, 'Sono supportati solo indirizzi http/https')

    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-protocol_whitelist', WL, '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', p['video']],
            capture_output=True, text=True, timeout=90)
        duration = int(float(r.stdout.strip().splitlines()[0]))
    except Exception:
        duration = 0
    if duration <= 0:
        return fail(job, 'Durata video non valida')

    with lock:
        job['status'] = 'running'
        job['message'] = ''
    start = time.time()
    try:
        proc = subprocess.Popen(build_cmd(p, tmp), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
    except Exception as e:
        return fail(job, str(e))
    job['proc'] = proc
    errbuf = collections.deque(maxlen=5)

    def drain():
        for l in proc.stderr:
            errbuf.append(l.strip())
    threading.Thread(target=drain, daemon=True).start()

    for line in proc.stdout:
        m = re.match(r'out_time_(?:us|ms)=(\d+)', line)
        if not m:
            continue
        done = int(m.group(1)) / 1000000.0
        pct = max(0, min(100, int(100 * done / duration)))
        elapsed = time.time() - start
        eta = int(elapsed * 100 / pct - elapsed) if pct > 0 else -1
        with lock:
            job['percent'] = pct
            job['eta'] = eta
    rc = proc.wait()
    job['proc'] = None

    if job['cancel']:
        status, msg = 'cancelled', ''
    elif rc == 0 and os.path.exists(tmp):
        os.replace(tmp, out)
        status, msg = 'done', ''
    else:
        status, msg = 'error', (errbuf[-1] if errbuf else 'ffmpeg ha restituito un errore')
    if status != 'done':
        try:
            os.remove(tmp)
        except OSError:
            pass
    with lock:
        job['status'] = status
        job['message'] = msg
        if status == 'done':
            job['percent'] = 100
    job['content'] = ''


def worker():
    while True:
        job = pending.get()
        try:
            run_job(job)
        except Exception as e:
            fail(job, str(e))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 3 * 1024 * 1024:
            raise ValueError('File troppo grande')
        return json.loads(self.rfile.read(n) or b'{}')

    def do_GET(self):
        if self.path.split('?')[0] == '/jobs':
            with lock:
                return self.reply({'jobs': [public(j) for j in jobs.values()]})
        self.reply({'error': 'not found'}, 404)

    def do_POST(self):
        path = self.path.split('?')[0]
        try:
            d = self.body()
        except Exception as e:
            return self.reply({'error': str(e)}, 400)
        if path == '/upload':
            content = d.get('content')
            if not isinstance(content, str) or '#EXTM3U' not in content:
                return self.reply({'error': 'Non sembra una playlist .m3u'}, 400)
            job = {'id': uuid.uuid4().hex[:10], 'name': safe_name(d.get('name', 'video')),
                   'content': content, 'status': 'queued', 'percent': 0, 'eta': -1,
                   'message': '', 'cancel': False, 'proc': None}
            with lock:
                jobs[job['id']] = job
                while len(jobs) > 100:
                    old = next((k for k, v in jobs.items() if v['status'] in FINISHED), None)
                    if old is None:
                        break
                    del jobs[old]
            pending.put(job)
            return self.reply({'id': job['id']})
        if path == '/cancel':
            with lock:
                j = jobs.get(d.get('id'))
            if j and j['status'] not in FINISHED:
                j['cancel'] = True
                pr = j.get('proc')
                if pr:
                    pr.terminate()
                elif j['status'] == 'queued':
                    with lock:
                        j['status'] = 'cancelled'
            return self.reply({'ok': True})
        if path == '/remove':
            with lock:
                j = jobs.get(d.get('id'))
                if j and j['status'] in FINISHED:
                    del jobs[j['id']]
            return self.reply({'ok': True})
        if path == '/clear':
            with lock:
                for k in [k for k, v in jobs.items() if v['status'] in FINISHED]:
                    del jobs[k]
            return self.reply({'ok': True})
        self.reply({'error': 'not found'}, 404)


if __name__ == '__main__':
    for _ in range(MAX_JOBS):
        threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
