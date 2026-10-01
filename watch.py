"""One read-only schedule check. State records successful mail delivery per recipient."""
import argparse
import concurrent.futures
import hashlib
import html
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state.json'
REPORT = ROOT / 'last-check.json'
KST = ZoneInfo('Asia/Seoul')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36'


def http(url, payload=None, form=False, referer=None):
    headers = {'User-Agent': UA, 'Accept': 'application/json, text/html'}
    data = None
    if payload is not None:
        data = (urllib.parse.urlencode(payload) if form else json.dumps(payload)).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    if referer:
        headers['Referer'] = referer
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=20) as response:
        return response.read().decode('utf-8-sig')


def required_list(value, name):
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise ValueError(f'{name}: expected an array of objects; API may have changed')
    return value


def integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def same_date(value, date):
    return re.sub(r'\D', '', str(value)) == date


def session(key, time, screen, seats):
    if not key or not time or not screen:
        raise ValueError('Missing session ID/time/screen; refusing to guess')
    return {'id': str(key), 'time': str(time), 'screen': html.unescape(str(screen)), 'seats': seats}


def parse_cgv(body, target, config):
    if body.get('statusCode') not in (0, '0'):
        raise ValueError('CGV API did not report success')
    rows = required_list(body.get('data'), 'CGV data')
    found = []
    for r in rows:
        # The request is already filtered by movie; validate returned IDs where supplied.
        if str(r.get('siteNo', target['code'])) != target['code']:
            raise ValueError('CGV returned another theater')
        if not same_date(r.get('scnYmd', config['date']), config['date']):
            raise ValueError('CGV returned another date')
        if str(r.get('movNo', config['cgv_movie_no'])) != config['cgv_movie_no']:
            continue
        seats = integer(r.get('frSeatCnt'))
        if seats is None:
            raise ValueError('Remaining seat count missing; API may have changed')
        if seats <= 0:
            continue
        tm = str(r.get('scnsrtTm', ''))
        if re.fullmatch(r'\d{4}', tm):
            tm = tm[:2] + ':' + tm[2:]
        key = r.get('scnNo') or f"{r.get('scnsNo', '')}:{tm}"
        found.append(session(key, tm, r.get('expoScnsNm') or r.get('scnsNm'), seats))
    return found


def parse_mega(body, target, config):
    if body.get('statCd') not in (0, '0'):
        raise ValueError('Megabox API did not report success')
    rows = required_list(body.get('movieFormList'), 'Megabox movieFormList')
    found = []
    for r in rows:
        if str(r.get('brchNo')) != target['code'] or not same_date(r.get('playDe'), config['date']):
            raise ValueError('Megabox returned another theater/date')
        if config['movie_keyword'] not in html.unescape(str(r.get('movieNm', ''))):
            continue
        seats = integer(r.get('restSeatCnt'))
        if seats is None:
            raise ValueError('Megabox remaining seat count missing')
        if seats <= 0 or str(r.get('bokdAbleAt', 'Y')) == 'N':
            continue
        found.append(session(r.get('playSchdlNo'), r.get('playStartTime'), r.get('theabExpoNm'), seats))
    return found


def parse_lotte(body, target, config):
    if str(body.get('IsOK')).lower() != 'true':
        raise ValueError('Lotte Cinema API did not report success')
    rows = required_list(body.get('PlaySeqs', {}).get('Items'), 'Lotte PlaySeqs.Items')
    found = []
    for r in rows:
        if str(r.get('CinemaID')) != target['code'] or not same_date(r.get('PlayDt'), config['date']):
            raise ValueError('Lotte returned another theater/date')
        if config['movie_keyword'] not in str(r.get('MovieNameKR', '')):
            continue
        # BookingSeatCount is the remaining bookable seat count, per the site's UI.
        seats = integer(r.get('BookingSeatCount'))
        if seats is None:
            raise ValueError('Lotte remaining seat count missing')
        if r.get('IsBookingYN') != 'Y' or seats <= 0:
            continue
        key = f"{r.get('ScreenID', '')}:{r.get('PlaySequence', '')}"
        found.append(session(key, r.get('StartTime'), r.get('ScreenNameKR'), seats))
    return found


def parse_cineq(text, target, config):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(text, 'html.parser')
    # This marker exists even in the site's legitimate empty schedule response.
    if not soup.select_one('.priceclick') or 'each-movie-time' not in text:
        raise ValueError('CINE Q timetable markup missing; not treating it as empty')
    found = []
    for movie in soup.select('.each-movie-time'):
        title = movie.select_one('.title')
        if title is None:
            raise ValueError('CINE Q movie title missing')
        if config['movie_keyword'] not in title.get_text(' ', strip=True):
            continue
        for slot in movie.select('.time[data-screenplanid]'):
            if slot.get('data-theatercode') != target['code'] or not same_date(slot.get('data-playdate'), config['date']):
                raise ValueError('CINE Q returned another theater/date')
            anchor = slot.find('a')
            if anchor is None:
                continue
            screen = slot.find_parent(class_='screen')
            name = screen.select_one('.screen-name') if screen else None
            tm = re.search(r'\d{1,2}:\d{2}', anchor.get_text(' ', strip=True))
            seats_node = anchor.select_one('.seats-status')
            seats = None
            if seats_node:
                m = re.search(r'(\d+)\s*/', seats_node.get_text())
                seats = int(m.group(1)) if m else None
            if seats is None:
                raise ValueError('CINE Q remaining seat count missing')
            if seats <= 0:
                continue
            found.append(session(slot.get('data-screenplanid'), tm.group() if tm else '', name.get_text(' ', strip=True) if name else '', seats))
    return found


def fetch_other(target, config):
    date = config['date']
    if target['chain'] == 'megabox':
        p = {'arrMovieNo': '', 'playDe': date, 'brchNoListCnt': 1,
             'brchNo1': target['code'], 'areaCd1': target['area'], 'spclbYn1': 'N',
             'theabKindCd1': target['area'], 'brchAll': target['area'], 'brchSpcl': '', 'sellChnlCd': ''}
        for field in ('brchNo', 'areaCd', 'spclbYn', 'theabKindCd'):
            for n in range(2, 6):
                p[f'{field}{n}'] = ''
        for n in range(1, 4):
            p[f'movieNo{n}'] = ''
        body = json.loads(http('https://www.megabox.co.kr/on/oh/ohb/SimpleBooking/selectBokdList.do', p, referer='https://www.megabox.co.kr/booking'))
        return parse_mega(body, target, config)
    if target['chain'] == 'lotte':
        p = {'MethodName': 'GetPlaySequence', 'channelType': 'HO', 'osType': 'W', 'osVersion': UA,
             'playDate': f'{date[:4]}-{date[4:6]}-{date[6:]}',
             'cinemaID': f"1|{target['division']}|{target['code']}", 'representationMovieCode': ''}
        body = json.loads(http('https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx', {'ParamList': json.dumps(p)}, form=True, referer=target['url']))
        return parse_lotte(body, target, config)
    if target['chain'] == 'cineq':
        text = http('https://www.cineq.co.kr/Theater/MovieTable2', {'TheaterCode': target['code'], 'PlayDate': date}, form=True, referer=target['url'])
        return parse_cineq(text, target, config)
    raise ValueError('Unsupported cinema chain')


def fetch_cgv_browser(targets, config):
    result = {}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto('https://cgv.co.kr/cnm/cgvChart/movieChart/' + config['cgv_movie_no'], wait_until='domcontentloaded', timeout=30000)
                for t in targets:
                    try:
                        query = urllib.parse.urlencode({'coCd': 'A420', 'siteNo': t['code'], 'scnYmd': config['date'], 'movNo': config['cgv_movie_no'], 'rtctlScopCd': '08'})
                        data = page.evaluate('''async (url) => {
                          const controller = new AbortController();
                          const timer = setTimeout(() => controller.abort(), 20000);
                          try {
                            const r = await fetch(url, {signal: controller.signal, headers: {Accept: 'application/json'}});
                            if (!r.ok) throw new Error('CGV HTTP ' + r.status);
                            return await r.json();
                          } finally { clearTimeout(timer); }
                        }''', '/api/v1/booking/searchSchByMov?' + query)
                        result[t['code']] = {'sessions': parse_cgv(data, t, config)}
                    except Exception as exc:
                        result[t['code']] = {'error': str(exc)[:350]}
            finally:
                browser.close()
    except Exception as exc:
        for t in targets:
            result[t['code']] = {'error': str(exc)[:350]}
    return result



def fetch_cgv_direct(target, config):
    """Read CGV's separate schedule API using its public web-client protocol.

    Protocol reference: wodn5515/cgv-megabox-movie-alarm/src/cgv_client.py.
    This static signing value belongs to the public client protocol; it is not
    the user's CGV login credential. API changes or access denial remain errors.
    """
    import base64
    import hmac
    import time
    path = '/cnm/atkt/searchMovScnInfo'
    stamp = str(int(time.time()))
    public_client_key = 'ydqXY0ocnFLmJGHr_zNzFcpjwAsXq_8JcBNURAkRscg'
    signature = base64.b64encode(hmac.new(
        public_client_key.encode(), f'{stamp}|{path}|'.encode(), hashlib.sha256
    ).digest()).decode()
    query = urllib.parse.urlencode({'coCd': 'A420', 'siteNo': target['code'],
                                   'scnYmd': config['date'], 'rtctlScopCd': '08'})
    headers = {'Accept': 'application/json', 'Accept-Language': 'ko-KR',
               'Origin': 'https://cgv.co.kr', 'Referer': 'https://cgv.co.kr/',
               'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
               'X-TIMESTAMP': stamp, 'X-SIGNATURE': signature}
    request = urllib.request.Request('https://api.cgv.co.kr' + path + '?' + query, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        body = json.load(response)
    rows = required_list(body.get('data'), 'CGV direct data')
    # This endpoint returns every movie at the theater. A film ID is mandatory:
    # never interpret an unidentified row as the requested movie.
    if any(not row.get('movNo') for row in rows):
        raise ValueError('CGV direct response is missing movie IDs')
    return parse_cgv(body, target, config)


def fetch_cgv(targets, config):
    result, failures = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {pool.submit(fetch_cgv_direct, target, config): target for target in targets}
        for future in concurrent.futures.as_completed(jobs):
            target = jobs[future]
            try:
                result[target['code']] = {'sessions': future.result()}
            except Exception as exc:
                failures[target['code']] = type(exc).__name__ + ': ' + str(exc)[:200]
    if failures:
        pending = [target for target in targets if target['code'] in failures]
        fallback = fetch_cgv_browser(pending, config)
        for code, item in fallback.items():
            if 'error' in item:
                item['error'] = 'Direct API: ' + failures[code] + '; browser: ' + item['error'][:250]
        result.update(fallback)
    return result

def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def mail_settings():
    sender = os.environ.get('MAIL_FROM', '').strip()
    password = os.environ.get('MAIL_APP_PASSWORD', '').replace(' ', '').strip()
    recipients = list(dict.fromkeys(x.strip().lower() for x in os.environ.get('MAIL_TO', '').split(',') if x.strip()))
    if not sender or not password or not recipients:
        raise ValueError('Set MAIL_FROM, MAIL_APP_PASSWORD and MAIL_TO in GitHub Actions Secrets')
    for address in [sender] + recipients:
        if parseaddr(address)[1] != address or not re.fullmatch(r'[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+', address):
            raise ValueError('Invalid email address in mail configuration')
    return sender, password, recipients


def send_mail(settings, recipient, subject, body):
    sender, password, _ = settings
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = sender, recipient, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30, context=ssl.create_default_context()) as smtp:
        smtp.login(sender, password)
        refused = smtp.send_message(msg)
        if refused:
            raise RuntimeError('SMTP rejected recipient')


def recipient_key(recipient):
    # Do not publish actual recipient addresses in a public repository state file.
    return hashlib.sha256(recipient.encode()).hexdigest()


def unsent_sessions(state, recipient, target_key, sessions):
    known = state.get('delivered', {}).get(recipient_key(recipient), {}).get(target_key, [])
    return [s for s in sessions if s['id'] not in known]


def mark_sent(state, recipient, target_key, sessions):
    record = state.setdefault('delivered', {}).setdefault(recipient_key(recipient), {}).setdefault(target_key, [])
    record[:] = sorted(set(record) | {s['id'] for s in sessions})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['check', 'test_email', 'diagnose'], default='check')
    parser.add_argument('--chains', default='', help='Optional comma-separated chains for local diagnostics')
    args = parser.parse_args()
    config = json.loads((ROOT / 'config.json').read_text())
    datetime.strptime(config['date'], '%Y%m%d')
    if args.mode == 'test_email':
        settings = mail_settings()
        for recipient in settings[2]:
            send_mail(settings, recipient, '[영화 알림] 테스트 메일', '이 메일은 발송 설정 테스트입니다. 예매 오픈 알림이 아닙니다.\n대상: 2026.10.10 / 극장판 치이카와 / 지정한 9개 지점')
        print('Test email accepted by Gmail SMTP. Check inbox/spam folder.')
        return 0
    now = datetime.now(KST)
    if args.mode == 'check' and now.strftime('%Y%m%d') > config['date']:
        print('Target date passed; monitoring stopped. Disable external cron job too.')
        return 0
    settings = mail_settings() if args.mode == 'check' else None
    state = json.loads(STATE.read_text()) if STATE.exists() else {'delivered': {}, 'health': {}}
    targets = config['targets']
    if args.chains:
        targets = [t for t in targets if t['chain'] in args.chains.split(',')]
    results = {}
    others = [t for t in targets if t['chain'] != 'cgv']
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {pool.submit(fetch_other, t, config): t for t in others}
        for future in concurrent.futures.as_completed(jobs):
            t = jobs[future]
            key = t['chain'] + ':' + t['code']
            try:
                results[key] = {'sessions': future.result()}
            except Exception as exc:
                results[key] = {'error': str(exc)[:350]}
    cgv = [t for t in targets if t['chain'] == 'cgv']
    if cgv:
        results.update({'cgv:' + k: v for k, v in fetch_cgv(cgv, config).items()})
    errors = []
    for t in targets:
        key = t['chain'] + ':' + t['code']
        result = results[key]
        result['name'] = t['name']
        if 'error' in result:
            errors.append(t['name'] + ': ' + result['error'])
            print('ERROR', t['name'], result['error'])
            continue
        print('OK', t['name'], len(result['sessions']), 'bookable matching sessions')
        if settings:
            identity = f"{config['date']}:{config['cgv_movie_no']}:{key}"
            for recipient in settings[2]:
                fresh = unsent_sessions(state, recipient, identity, result['sessions'])
                if not fresh:
                    continue
                lines = [config['movie_name'], '상영일: 2026.10.10(토)', t['name'], '', '예매 가능한 새 회차를 확인했습니다.']
                for s in fresh:
                    seats = f" / 잔여 {s['seats']}석" if s['seats'] is not None else ''
                    lines.append(f"{s['time']} / {s['screen']}{seats}")
                lines += ['', t['url'], '', '확인 시점: ' + now.isoformat(), '회차와 좌석은 변경될 수 있습니다. 예매 화면에서 확인해주세요.']
                try:
                    send_mail(settings, recipient, f"[치이카와 10/10] {t['name']} 새 회차 확인", '\n'.join(lines))
                    mark_sent(state, recipient, identity, fresh)
                    atomic_json(STATE, state)
                except Exception as exc:
                    errors.append(t['name'] + ': email delivery failed: ' + type(exc).__name__)
    report = {'checked_at_kst': now.isoformat(), 'date': config['date'], 'results': results, 'errors': errors}
    atomic_json(REPORT, report)
    if settings:
        # Daily heartbeat plus one extra health update if the set of failed branches changes.
        failures = sorted(k for k, r in results.items() if 'error' in r)
        health_key = now.strftime('%Y-%m-%d') + ':' + hashlib.sha256(json.dumps(failures).encode()).hexdigest()[:12]
        for recipient in settings[2]:
            rk = recipient_key(recipient)
            if health_key in state.setdefault('health', {}).get(rk, []):
                continue
            summary = ['자동 실행 확인: ' + now.isoformat(), '상영일: 2026.10.10', '']
            for t in targets:
                r = results[t['chain'] + ':' + t['code']]
                summary.append(t['name'] + ': ' + ('조회 오류 (미오픈 여부를 판단할 수 없음)' if 'error' in r else f"조회 성공 / 예매 가능한 치이카와 회차 {len(r['sessions'])}개"))
            summary += ['', '조회 오류가 있으면 GitHub Actions 로그를 확인해주세요.']
            if os.environ.get('GITHUB_REPOSITORY'):
                summary.append('https://github.com/' + os.environ['GITHUB_REPOSITORY'] + '/actions')
            try:
                send_mail(settings, recipient, '[영화 알림] 자동 실행·조회 상태 확인', '\n'.join(summary))
                state['health'].setdefault(rk, []).append(health_key)
                atomic_json(STATE, state)
            except Exception as exc:
                errors.append('Health email failed: ' + type(exc).__name__)
    if errors:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
