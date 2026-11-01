import json, os, time, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------- settings you can change ----------------
SYMBOLS = {"WTI": "CL=F", "Brent": "BZ=F"}      # Yahoo symbols
TIMEFRAMES = [1, 2, 4]                           # hours
BREAKOUT_BARS = {1: 24, 2: 12, 4: 6}             # bars that make up the last 24 hours
OVERBOUGHT, OVERSOLD, REARM = 70, 30, 5          # RSI levels (re-arms at 65 / 35)
RIGA = ZoneInfo("Europe/Riga")
ANCHOR_HOUR = 0                                  # 2h/4h bars start at Riga midnight: 00:00, 04:00, 08:00 ...
MAX_AGE_MIN = 90                                 # news: only headlines published in the last 90 minutes
# ----------------------------------------------------------

STATE_FILE = "state.json"
UA = {"User-Agent": "Mozilla/5.0"}
TAGS = {
    "OPEC": ["opec", "saudi", "production cut", "output cut"],
    "INVENTORY": ["eia", "inventor", "stockpile", "api report", "crude stocks"],
    "GEOPOLITICS": ["iran", "russia", "ukraine", "sanction", "israel", "houthi", "red sea", "strait of hormuz", "libya"],
    "DEMAND": ["china", "demand", "recession", "gdp", "pmi"],
    "FED/USD": ["fed ", "rate cut", "rate hike", "inflation", "dollar"],
    "SUPPLY": ["refinery", "pipeline", "shale", "rig count", "outage", "hurricane"],
}

def now_riga():
    return datetime.now(RIGA).strftime("%H:%M")

def tg(text):
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chats:
        print("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID secret."); return
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    for chat in chats:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                           "disable_web_page_preview": "true"}).encode()
            urllib.request.urlopen(url, data=data, timeout=20)
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read().decode()).get("description", "")
            except Exception:
                why = ""
            print("Telegram send failed for chat ...%s: HTTP %s %s" % (chat[-3:], e.code, why))
        except Exception as e:
            print("Telegram send failed for chat ...%s: %s" % (chat[-3:], type(e).__name__))

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()

def bars(symbol):
    """1-hour bars from Yahoo: (time, open, high, low, close)."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1h&range=3mo"
           % urllib.parse.quote(symbol))
    res = json.loads(get(url))["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(res["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None not in (o, h, l, c):
            rows.append((t, o, h, l, c))
    return rows

def rsi(closes, n=14):
    if len(closes) <= n: return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

def resample(rows, hours):
    """Join 1-hour bars into 2h or 4h bars, lined up with Riga time (blocks start at ANCHOR_HOUR)."""
    if hours == 1:
        return rows
    out, cur, key = [], None, None
    for t, o, h, l, c in rows:
        d = datetime.fromtimestamp(t, RIGA) - timedelta(hours=ANCHOR_HOUR)
        k = (d.date(), d.hour // hours)
        if k != key:
            if cur: out.append(tuple(cur))
            cur, key = [t, o, h, l, c], k
        else:
            cur[2] = max(cur[2], h); cur[3] = min(cur[3], l); cur[4] = c
    if cur: out.append(tuple(cur))
    return out

def check_markets(zones, last_sent):
    for name, sym in SYMBOLS.items():
        try:
            rows = bars(sym)
        except Exception as e:
            print(name, "price data failed:", type(e).__name__); continue
        if len(rows) < 100 or time.time() - rows[-1][0] > 7200:
            print(name, "market closed or no fresh data"); continue
        price, rsi_lines, brk_lines, shown = rows[-1][4], [], [], []
        for hrs in TIMEFRAMES:
            tf = resample(rows, hrs)
            # --- RSI overbought / oversold ---
            r = rsi([b[4] for b in tf])
            key = "%s|%d" % (name, hrs)
            zone = zones.get(key)
            if r >= OVERBOUGHT: new = "OVERBOUGHT"
            elif r <= OVERSOLD: new = "OVERSOLD"
            elif zone == "OVERBOUGHT" and r > OVERBOUGHT - REARM: new = zone
            elif zone == "OVERSOLD" and r < OVERSOLD + REARM: new = zone
            else: new = None
            if new and new != zone:
                rsi_lines.append("%dH RSI %.0f: %s" % (hrs, r, new))
            zones[key] = new
            # --- price passes the last 24 hours' high or low ---
            n = BREAKOUT_BARS[hrs]
            prev = tf[-n - 1:-1]
            kind = None
            if len(prev) == n:
                hi, lo = max(b[2] for b in prev), min(b[3] for b in prev)
                kind = "UP" if price > hi else "DOWN" if price < lo else None
                if kind:
                    k2 = "%s|%d|%s" % (name, hrs, kind)
                    if time.time() - last_sent.get(k2, 0) > hrs * 3600:
                        last_sent[k2] = int(time.time())
                        if kind == "UP":
                            brk_lines.append("%dH BREAKOUT UP: above the last 24h high (%.2f)" % (hrs, hi))
                        else:
                            brk_lines.append("%dH BREAKOUT DOWN: below the last 24h low (%.2f)" % (hrs, lo))
            shown.append("%dh RSI %.0f%s" % (hrs, r, (" break" + kind.lower()) if kind else ""))
        print(name, "%.2f" % price, "|", " | ".join(shown))
        if brk_lines or rsi_lines:
            tg("%s %.2f (%s Riga)\n%s" % (name, price, now_riga(), "\n".join(brk_lines + rsi_lines)))

def source_of(it, link):
    s = it.findtext("source")
    if s and s.strip():
        return s.strip()
    host = urllib.parse.urlparse(link).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if "yahoo" in host:
        return "Yahoo Finance"
    return host or "unknown source"

def headlines():
    q = urllib.parse.quote("WTI crude oil OR Brent crude OR OPEC OR EIA crude inventories")
    feeds = [
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US" % urllib.parse.quote("CL=F"),
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US" % urllib.parse.quote("BZ=F"),
        "https://news.google.com/rss/search?q=%s+when:2h&hl=en-US&gl=US&ceid=US:en" % q,
    ]
    items, seen = [], set()
    now = datetime.now(timezone.utc)
    for url in feeds:
        try:
            root = ET.fromstring(get(url))
        except Exception as e:
            print("(a news source failed: %s)" % type(e).__name__); continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = it.findtext("link") or ""
            src = source_of(it, link)
            if title.endswith(" - " + src):
                title = title[:-len(" - " + src)].strip()
            key = title.lower()[:60]
            if not title or key in seen:
                continue
            try:
                dt = parsedate_to_datetime(it.findtext("pubDate"))
            except Exception:
                continue                      # no publish time: cannot prove it is fresh
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_min = (now - dt).total_seconds() / 60
            if age_min > MAX_AGE_MIN or age_min < -10:
                continue
            seen.add(key)
            items.append((dt, title, src))
    items.sort(key=lambda x: x[0], reverse=True)
    return items

def tag(title):
    low = " " + title.lower() + " "
    return [t for t, words in TAGS.items() if any(w in low for w in words)]

def check_news(seen):
    new = [h for h in headlines() if h[1] not in seen]
    for dt, title, src in reversed(new):
        seen[title] = int(time.time())
    tagged = [(dt, t, s) for dt, t, s in new if tag(t)]
    for dt, title, src in list(reversed(tagged))[-5:]:
        tg("NEWS %s Riga [%s]\n%s\nSource: %s"
           % (dt.astimezone(RIGA).strftime("%H:%M"), ", ".join(tag(title)), title, src))
    print("fresh headlines: %d new, %d sent" % (len(new), min(len(tagged), 5)))

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)

def main():
    st = load_state()
    first = st is None
    if first:
        st = {"seen": {}, "zones": {}, "last_sent": {}}
        for dt, title, src in headlines():          # headlines already out are not re-sent
            st["seen"][title] = int(time.time())
        tg("Oil alerts (cloud version) started. Watching WTI and Brent: 24h breakouts and RSI on "
           "1h, 2h and 4h, plus oil headlines. Signals are arithmetic on past prices, not advice.")
    print("--- check", now_riga(), "Riga")
    check_markets(st["zones"], st["last_sent"])
    check_news(st["seen"])
    now = time.time()
    st["seen"] = {t: ts for t, ts in st["seen"].items() if now - ts < 6 * 3600}
    st["last_sent"] = {k: ts for k, ts in st["last_sent"].items() if now - ts < 48 * 3600}
    save_state(st)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Run failed:", type(e).__name__, e)
