"""Deal Alert Bot — everything in one file, controlled from Telegram.

Send your bot a product name ("PS5 Pro under 650") and it finds the shops,
prices and stock, then checks every 15 minutes for restocks and price drops.
Send /trip to set up flight + hotel tracking. Send /help for everything else.

Needs two secrets: TELEGRAM_BOT_TOKEN and SERPAPI_KEY.
Run once:   python bot.py            (GitHub Actions does this every 15 min)
Keep going: python bot.py --loop     (PC / Raspberry Pi, instant replies)
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ═════════════════════════ SETTINGS (safe to change) ═════════════════════════
CURRENCY = "GBP"
COUNTRY = "uk"
TIMEZONE = "Europe/London"
QUIET_HOURS = (22, 7)          # alerts arrive silently 10pm–7am
SERPAPI_MONTHLY_LIMIT = 240    # free plan is ~250; stop before it runs out
CHECK_EVERY_MINUTES = 15       # product checks (shop pages read directly: free)
MIN_DROP_PERCENT = 2           # ignore product price wobbles smaller than this
REFRESH_SHOPS_EVERY_DAYS = 3   # re-search Google Shopping for sellers (2 searches)
MAX_SHOPS = 6                  # cheapest shops watched per product
TRIP_EVERY_HOURS = 24          # each trip check uses 1-2 searches per date option
TRIP_MIN_DROP = 15             # £ — ignore smaller flight drops
# ═════════════════════════════════════════════════════════════════════════════

ROOT = Path(__file__).resolve().parent
STATE_FILE = Path(os.environ.get("DEALBOT_STATE") or ROOT / "state.json")
SYMBOL = {"GBP": "£", "EUR": "€", "USD": "$"}.get(CURRENCY, CURRENCY + " ")


# ─────────────────────────────── small helpers ───────────────────────────────

def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def hours_since(iso):
    if not iso:
        return float("inf")
    return (now_utc() - dt.datetime.fromisoformat(iso)).total_seconds() / 3600


def money(v):
    if v is None:
        return "?"
    return f"{SYMBOL}{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{SYMBOL}{v:,.2f}"


def esc(text):
    return html.escape(str(text), quote=False)


def attr(text):
    return html.escape(str(text), quote=True)


def as_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v).strip())


def nice_date(d):
    return as_date(d).strftime("%a %-d %b %Y")


def parse_price(text):
    """'£1,299.99' -> 1299.99, '449,99 €' -> 449.99, None if there's no number."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = re.sub(r"[^\d.,]", "", str(text))
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = head.replace(",", "") + ("." if len(tail) == 2 else "") + tail
    try:
        return float(s)
    except ValueError:
        return None


def words(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def load_dotenv():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


# ───────────────────────────────── Telegram ──────────────────────────────────

def owner(state):
    return os.environ.get("TELEGRAM_CHAT_ID") or state.get("owner")


def quiet_now():
    start, end = QUIET_HOURS
    h = dt.datetime.now(ZoneInfo(TIMEZONE)).hour
    return (h >= start or h < end) if start > end else start <= h < end


def chunks(text, limit=3900):
    parts, cur = [], ""
    for block in text.split("\n\n"):
        cand = f"{cur}\n\n{block}" if cur else block
        if len(cand) > limit and cur:
            parts.append(cur)
            cur = block
        else:
            cur = cand
    return [p[:4096] for p in parts + ([cur] if cur else [])]


def send(text, state, dry_run=False):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), owner(state)
    if dry_run or not token or not chat:
        print("\n" + "=" * 50 + "\n" + text + "\n" + "=" * 50)
        return
    for part in chunks(text):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, json={
            "chat_id": chat, "text": part, "parse_mode": "HTML",
            "disable_web_page_preview": True, "disable_notification": quiet_now()})
        if not r.ok:
            print(f"Telegram error {r.status_code}: {r.text[:200]}")


def get_updates(token, offset=None, timeout=0):
    params = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}
    if offset:
        params["offset"] = offset
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params,
                     timeout=timeout + 15)
    r.raise_for_status()
    return r.json().get("result", [])


# ───────────────────────────── SerpApi (Google data) ─────────────────────────

class BudgetExceeded(Exception):
    pass


class SerpError(Exception):
    pass


class SerpApi:
    def __init__(self, key, state, fixtures=None):
        self.key, self.state = key, state
        self.limit = SERPAPI_MONTHLY_LIMIT
        self.fixtures = Path(fixtures) if fixtures else None

    @property
    def month(self):
        return now_utc().strftime("%Y-%m")

    @property
    def used(self):
        return self.state.get("serpapi_usage", {}).get(self.month, 0)

    def _count(self):
        usage = self.state.setdefault("serpapi_usage", {})
        if self.month not in usage:
            usage.clear()
        usage[self.month] = usage.get(self.month, 0) + 1

    def search(self, params):
        if self.fixtures:  # offline tests
            self._count()
            f = self.fixtures / f"{params['engine']}.json"
            return json.loads(f.read_text()) if f.exists() else {}
        if self.used >= self.limit:
            raise BudgetExceeded(f"monthly limit of {self.limit} searches reached")
        r = requests.get("https://serpapi.com/search.json", params={**params, "api_key": self.key},
                         timeout=90)
        self._count()
        try:
            data = r.json()
        except ValueError:
            raise SerpError(f"HTTP {r.status_code}")
        if "error" in data:
            if "hasn't returned any results" in data["error"] or "no results" in data["error"].lower():
                return {}
            raise SerpError(data["error"])
        return data


def make_api(state):
    if os.environ.get("DEALBOT_FIXTURES"):
        return SerpApi(None, state, os.environ["DEALBOT_FIXTURES"])
    key = os.environ.get("SERPAPI_KEY")
    return SerpApi(key, state) if key else None


# ─────────────────────────── reading shop pages ──────────────────────────────

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
AVAILABLE = {"instock", "limitedavailability", "onlineonly", "preorder", "presale", "backorder"}
UNAVAILABLE = {"outofstock", "soldout", "discontinued", "instoreonly", "oos"}
BLOCK_MARKERS = ("captcha", "are you a robot", "access denied", "unusual traffic",
                 "verify you are human", "pardon our interruption")
PAGE_OUT = ("out of stock", "sold out", "currently unavailable", "temporarily unavailable",
            "notify me when", "email me when")
PAGE_IN = ("add to basket", "add to cart", "add to bag", "add to trolley", "buy now")


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    head = r.text[:5000].lower()
    if r.status_code in (401, 403, 429, 503) or (len(r.text) < 20000 and any(m in head for m in BLOCK_MARKERS)):
        return None, f"blocked (HTTP {r.status_code})"
    if not r.ok:
        return None, f"HTTP {r.status_code}"
    return r.text, None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _is(obj, name):
    t = obj.get("@type")
    return name in t if isinstance(t, list) else t == name


def _avail(value):
    return str(value).rstrip("/").rsplit("/", 1)[-1].replace(" ", "").replace("_", "").lower() if value else None


def _offers(product):
    raw = product.get("offers") or []
    out = []
    for o in raw if isinstance(raw, list) else [raw]:
        if isinstance(o, dict):
            inner = o.get("offers") if _is(o, "AggregateOffer") else None
            out += [i for i in (inner if isinstance(inner, list) else [inner]) if isinstance(i, dict)]
            out.append(o)
    for v in product.get("hasVariant") or []:
        if isinstance(v, dict):
            out += _offers(v)
    return out


def parse_page(page):
    """Read price + stock from a product page (schema.org data first, then page text)."""
    soup = BeautifulSoup(page, "html.parser")
    offers, title = [], None
    for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(re.sub(r"[\x00-\x1f]", " ", tag.string or tag.get_text() or ""))
        except json.JSONDecodeError:
            continue
        for obj in _walk(data):
            if _is(obj, "Product") or _is(obj, "ProductGroup"):
                title = title or obj.get("name")
                offers += _offers(obj)
    states, prices, live_prices = [], [], []
    for o in offers:
        token = _avail(o.get("availability"))
        price = parse_price(o.get("price") or o.get("lowPrice"))
        if price:
            prices.append(price)
        if token in AVAILABLE:
            states.append(True)
            live_prices += [price] if price else []
        elif token in UNAVAILABLE:
            states.append(False)
    info = {"in_stock": True if True in states else (False if states else None),
            "price": min(live_prices or prices) if (live_prices or prices) else None, "title": title}
    if info["price"] is None:
        tag = (soup.find(attrs={"itemprop": "price"}) or soup.find("meta", property="product:price:amount")
               or soup.find("meta", property="og:price:amount"))
        if tag:
            info["price"] = parse_price(tag.get("content") or tag.get_text())
    if info["in_stock"] is None:
        tag = soup.find(attrs={"itemprop": "availability"}) or soup.find(
            "meta", property=re.compile("(product|og):availability"))
        token = _avail(tag and (tag.get("href") or tag.get("content") or tag.get_text()))
        if token in AVAILABLE:
            info["in_stock"] = True
        elif token in UNAVAILABLE:
            info["in_stock"] = False
    if info["in_stock"] is None:
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        text = soup.get_text(" ").lower()
        has_out, has_in = any(w in text for w in PAGE_OUT), any(w in text for w in PAGE_IN)
        info["in_stock"] = False if has_out and not has_in else True if has_in and not has_out else None
    if not info["title"] and soup.title:
        info["title"] = soup.title.get_text(strip=True)
    return info


# ───────────────────────── products: find + watch ────────────────────────────

ACCESSORY = ["case", "cover", "skin", "sticker", "decal", "screen protector", "protector", "stand",
             "charger", "charging dock", "cable", "mount", "holder", "refurbished", "renewed",
             "pre owned", "preowned", "used", "replacement", "compatible", "faceplate", "carry bag"]
G_OUT = ("out of stock", "sold out", "unavailable", "no stock")
G_IN = ("in stock", "available", "limited stock", "pre-order", "preorder", "few left")


def parse_request(text):
    """'PS5 Pro under £650 -bundle' -> query, max_price, min_price, exclusions."""
    s = f" {text.strip()} "
    req = {"max_price": None, "min_price": None}
    for key, pat in (("max_price", r"under|below|max|less than|<|upto|up to"),
                     ("min_price", r"over|above|min|more than|>")):
        m = re.search(rf"\s(?:{pat})\s*£?\s*(\d+(?:\.\d+)?)", s, re.I)
        if m:
            req[key] = float(m.group(1))
            s = s.replace(m.group(0), " ")
    req["exclude"] = [w.lower() for w in re.findall(r"\s-(\w+)", s)]
    req["query"] = re.sub(r"\s+", " ", re.sub(r"\s-\w+", " ", s)).strip()
    return req


def relevance(title, req):
    """Share of your words in the listing title; -1 if it looks like an accessory."""
    norm, qw = f" {' '.join(words(title))} ", words(req["query"])
    for phrase in ACCESSORY + req.get("exclude", []):
        p = " ".join(words(phrase))
        if p and f" {p} " in norm and p not in " ".join(qw):
            return -1
    return sum(1 for w in qw if w in norm.split()) / len(qw) if qw else 0


def google_stock(details):
    text = " ".join(details or []).lower()
    return False if any(w in text for w in G_OUT) else True if any(w in text for w in G_IN) else None


def usable(link):
    host = urlparse(link or "").netloc.lower()
    return bool(host) and "google." not in host


def discover(api, req):
    """Google Shopping → best-matching product → the shops selling it (2 searches)."""
    data = api.search({"engine": "google_shopping", "q": req["query"], "gl": COUNTRY, "hl": "en"})
    cands = []
    for pos, r in enumerate(data.get("shopping_results") or []):
        if req.get("min_price") and (r.get("extracted_price") or 0) < req["min_price"]:
            continue
        score = relevance(r.get("title"), req)
        if score >= 0:
            cands.append((score, pos, r))
    good = sorted((c for c in cands if c[0] >= 0.6), key=lambda c: (-c[0], c[1]))
    ranked = good or cands  # "PS5" vs "PlayStation 5": fall back to Google's own order
    if not ranked:
        return None
    best = ranked[0][2]
    offers = []
    if best.get("immersive_product_page_token"):
        detail = api.search({"engine": "google_immersive_product", "more_stores": "true",
                             "page_token": best["immersive_product_page_token"]})
        for s in (detail.get("product_results") or {}).get("stores") or []:
            price = s.get("extracted_price") or s.get("extracted_total")
            if price:
                offers.append({"store": s.get("name") or "Shop", "link": s.get("link"),
                               "price": float(price), "in_stock": google_stock(s.get("details_and_offers"))})
    if not offers:
        offers = [{"store": r.get("source") or "Shop", "link": r.get("link") or r.get("product_link"),
                   "price": float(r["extracted_price"]), "in_stock": None}
                  for _, _, r in ranked if r.get("extracted_price") and r.get("title") == best.get("title")]
    seen, unique = set(), []
    for o in sorted(offers, key=lambda o: o["price"]):
        if o["store"].lower() not in seen:
            seen.add(o["store"].lower())
            unique.append(o)
    others = [{"title": r.get("title"), "price": r.get("extracted_price")}
              for _, _, r in ranked[1:4] if r.get("title") != best.get("title")]
    return {"title": best.get("title") or req["query"], "offers": unique[:MAX_SHOPS], "others": others}


def live_check(offers, fetcher=None):
    """Open each shop page directly (free). If a shop blocks us, keep Google's data."""
    fetcher = fetcher or fetch
    for o in offers:
        if not usable(o.get("link")):
            continue
        try:
            page, error = fetcher(o["link"])
        except Exception:
            page, error = None, "error"
        if error or not page:
            o["blocked"] = True
            continue
        o["blocked"] = False
        info = parse_page(page)
        if info["in_stock"] is not None:
            o["in_stock"] = info["in_stock"]
        p = info["price"]
        if p and (o.get("price") is None or 0.5 * o["price"] <= p <= 2 * o["price"]):
            o["price"] = p  # (ignores a wildly different price = probably another variant)
    return offers


def best_in_stock(offers):
    live = [o for o in offers if o.get("in_stock") is True and o.get("price")]
    return min(live, key=lambda o: o["price"]) if live else None


def summary(w, heading):
    offers = sorted(w.get("offers") or [], key=lambda o: o.get("price") or 1e12)
    live = [o for o in offers if o.get("in_stock") is True]
    best = best_in_stock(offers)
    lines = [heading, f"<b>{esc(w['title'])}</b>"]
    if best:
        lines.append(f"In stock at {len(live)} shop{'s' * (len(live) != 1)} · cheapest "
                     f"<b>{money(best['price'])}</b> at {esc(best['store'])}")
    elif offers:
        lines.append("Not confirmed in stock anywhere right now.")
    for o in offers:
        mark = {True: "✅", False: "❌ out of stock", None: "❓ check"}[o.get("in_stock")]
        store = f'<a href="{attr(o["link"])}">{esc(o["store"])}</a>' if o.get("link") else esc(o["store"])
        lines.append(f"• {store} {money(o.get('price'))} {mark}")
    if w.get("max_price"):
        lines.append(f"🎯 Your target: {money(w['max_price'])}")
    if w.get("lowest") and (not best or w["lowest"] < best["price"]):
        lines.append(f"Lowest seen: {money(w['lowest'])}")
    return "\n".join(lines)


def new_watch(req, found):
    best = best_in_stock(found["offers"])
    return {**req, "title": found["title"], "offers": found["offers"],
            "last_refresh": now_utc().isoformat(), "any_in_stock": bool(best),
            "best": best["price"] if best else None, "lowest": best["price"] if best else None}


def check_watch(api, state, report=False, fetcher=None):
    """The every-15-minutes product check. Returns alert messages."""
    blocks = []
    for w in state.get("watch", []):
        if api and not w.get("url") and hours_since(w.get("last_refresh")) >= REFRESH_SHOPS_EVERY_DAYS * 24:
            try:
                found = discover(api, w)
                if found:
                    w["offers"], w["title"] = found["offers"], found["title"]
                w["last_refresh"] = now_utc().isoformat()
            except (BudgetExceeded, SerpError) as e:
                print(f"Shop refresh skipped for {w['query']}: {e}")
        live_check(w.get("offers") or [], fetcher)
        best = best_in_stock(w.get("offers") or [])
        price = best["price"] if best else None
        reasons = []
        if best and w.get("any_in_stock") is False:
            reasons.append("🟢 BACK IN STOCK")
        if price and w.get("best") and price <= w["best"] * (1 - MIN_DROP_PERCENT / 100):
            reasons.append(f"🔻 Down to {money(price)} at {esc(best['store'])} (was {money(w['best'])})")
        target = w.get("max_price")
        if price and target and price <= target and not (w.get("best") and w["best"] <= target):
            reasons.append(f"🎯 Under your {money(target)}")
        if report and not reasons:
            reasons.append("📋 Status")
        w["lowest"] = min([x for x in (w.get("lowest"), price) if x], default=None)
        if reasons:
            blocks.append(summary(w, " · ".join(reasons)))
        if best or any(o.get("in_stock") is False for o in w.get("offers") or []):
            w["any_in_stock"] = bool(best)  # only flip when we actually know
        w["best"] = price
    return blocks


def find_watch(state, query):
    q = " ".join(words(query))
    return next((w for w in state.get("watch", []) if " ".join(words(w["query"])) == q), None)


def track(text, state, api, fetcher=None):
    """Someone typed a product name (or pasted a link)."""
    if re.match(r"https?://", text.strip()):
        return track_link(text.strip().split()[0], state, fetcher)
    req = parse_request(text)
    if len(req["query"]) < 2:
        return HELP
    existing = find_watch(state, req["query"])
    if existing:
        if req.get("max_price"):
            existing["max_price"] = req["max_price"]
        return summary(existing, "👀 Already watching this:")
    if api is None:
        return "⚠️ I need a SerpApi key to search shops. Add the SERPAPI_KEY secret on GitHub."
    try:
        found = discover(api, req)
    except BudgetExceeded:
        return "⏸ Monthly search limit reached, so I can't add new products until next month."
    except SerpError as e:
        return f"⚠️ Search failed: {esc(e)}"
    if not found:
        return f"🤷 Couldn't find “{esc(req['query'])}” on Google Shopping. Try a different name."
    live_check(found["offers"], fetcher)
    w = new_watch(req, found)
    state.setdefault("watch", []).append(w)
    n = len(state["watch"])
    out = summary(w, f"🔎 “{esc(req['query'])}” → watching as #{n}")
    alts = " · ".join(f"{esc(o['title'])} {money(o['price'])}" for o in found["others"] if o.get("price"))
    if alts:
        out += f"\n\nOther matches: {alts}"
    return out + (f"\n\nI'll check every {CHECK_EVERY_MINUTES} min and ping you on restocks or price "
                  f"drops. Wrong item? /stop {n} and send a more specific name.")


def track_link(url, state, fetcher=None):
    if any(w.get("url") == url for w in state.get("watch", [])):
        return "👀 Already watching that page. /list to see it."
    try:
        page, error = (fetcher or fetch)(url)
    except requests.RequestException:
        page, error = None, "error"
    if error or not page:
        return ("🚫 I can't read that page (the shop blocks automated checks). Send me the product "
                "<b>name</b> instead and I'll track it across shops using Google Shopping.")
    info = parse_page(page)
    store = urlparse(url).netloc.replace("www.", "")
    offer = {"store": store, "link": url, "price": info["price"], "in_stock": info["in_stock"]}
    live = offer["price"] if info["in_stock"] else None
    w = {"query": url, "url": url, "title": info["title"] or store, "offers": [offer], "max_price": None,
         "any_in_stock": info["in_stock"] is True, "best": live, "lowest": live}
    state.setdefault("watch", []).append(w)
    note = "" if offer["in_stock"] is not None else (
        "\n\n⚠️ I can't tell this page's stock status, so I'll only track its price.")
    return summary(w, f"🔗 Watching this page as #{len(state['watch'])}") + note


# ─────────────────────── trips: flights + hotel vs package ───────────────────

STOPS = {"any": "0", "nonstop": "1", "direct": "1", "max1": "2", "1": "2", "max2": "3"}
MEDALS = ["🥇", "🥈", "🥉"]
TRIP_TEMPLATE = ("/trip Summer holiday\n"
                 "from: EMA 0, BHX 40, MAN 70\n"
                 "to: PMI\n"
                 "dates: 24/07/2027-31/07/2027, 31/07/2027-07/08/2027\n"
                 "people: 2 adults, kids 7\n"
                 "stops: max1\n"
                 "hotel: Alcudia, Majorca | 4.0 | 3\n"
                 "extras: 150\n"
                 "package: Jet2holidays 2150 half board\n"
                 "alert: 900")
TRIP_HELP = (
    "✈️ <b>Copy this, change it, send it back:</b>\n\n<code>" + esc(TRIP_TEMPLATE) + "</code>\n\n"
    "• <b>from</b>: airport codes + what it costs you to get there (parking/fuel), so I rank by true cost\n"
    "• <b>to</b>: airport code(s), e.g. PMI or PMI, AGP\n"
    "• <b>dates</b>: out-back pairs, as many options as you like (each costs searches)\n"
    "• <b>people</b>: adults, then each child's age\n"
    "• <b>stops</b>: any, direct or max1\n"
    "• <b>hotel</b>: area | min Google rating | min stars (or 'none')\n"
    "• <b>extras</b>: bags/transfers a package would include\n"
    "• <b>package</b>: a quote you've seen (repeat the line for more)\n"
    "• <b>alert</b>: ping me if flights drop under this\n\n"
    "Only from, to and dates are required. Airport codes: Google “&lt;airport&gt; IATA code”.")
DATE_RE = r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}"


def parse_date(s):
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d %b %Y", "%d %B %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"I couldn't read the date “{s}”")


def parse_trip(text):
    """Turn the /trip message into a trip dict. Raises ValueError with a friendly reason."""
    lines = text.strip().splitlines()
    name = re.sub(r"^/trip(@\w+)?", "", lines[0]).strip() or "Trip"
    trip = {"name": name, "adults": 2, "children_ages": [], "stops": "any", "hotel": None,
            "diy_extras": 0, "package_quotes": [], "alert_below": None}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if not value:
            continue
        if key == "from":
            trip["from_airports"] = {}
            for part in value.split(","):
                m = re.match(r"\s*([A-Za-z]{3})\b\s*£?\s*(\d+(?:\.\d+)?)?", part)
                if not m:
                    raise ValueError(f"“{part.strip()}” isn't an airport code (3 letters)")
                trip["from_airports"][m.group(1).upper()] = float(m.group(2) or 0)
        elif key == "to":
            codes = re.findall(r"\b[A-Za-z]{3}\b", value)
            if not codes:
                raise ValueError("‘to’ needs an airport code, e.g. PMI")
            trip["to"] = ",".join(c.upper() for c in codes)
        elif key == "dates":
            found = [parse_date(d) for d in re.findall(DATE_RE, value)]
            if len(found) < 2 or len(found) % 2:
                raise ValueError("‘dates’ needs pairs: out-back, e.g. 24/07/2027-31/07/2027")
            trip["dates"] = [[found[i].isoformat(), found[i + 1].isoformat()] for i in range(0, len(found), 2)]
            for out_d, back_d in zip(found[::2], found[1::2]):
                if back_d <= out_d:
                    raise ValueError(f"the return {back_d:%d/%m/%Y} is before the outbound date")
        elif key in ("people", "who", "passengers"):
            m = re.search(r"(\d+)\s*adult", value, re.I)
            trip["adults"] = int(m.group(1)) if m else trip["adults"]
            m = re.search(r"(kids?|child(?:ren)?|ages?)\b(.*)", value, re.I)
            trip["children_ages"] = [int(a) for a in re.findall(r"\d+", m.group(2))] if m else []
        elif key == "stops":
            trip["stops"] = value.lower().replace(" ", "")
        elif key == "hotel":
            if value.lower() in ("none", "no", "off"):
                continue
            parts = [p.strip() for p in value.split("|")]
            trip["hotel"] = {"location": parts[0],
                             "min_rating": float(parts[1]) if len(parts) > 1 and parts[1] else 0,
                             "min_stars": int(float(parts[2])) if len(parts) > 2 and parts[2] else 0}
        elif key == "extras":
            trip["diy_extras"] = parse_price(value) or 0
        elif key == "alert":
            trip["alert_below"] = parse_price(value)
        elif key == "package":
            m = re.match(r"(.+?)\s+£?(\d[\d,]*(?:\.\d+)?)\s*(.*)", value)
            if not m:
                raise ValueError("‘package’ looks like: Jet2holidays 2150")
            trip["package_quotes"].append({"site": m.group(1), "price": parse_price(m.group(2)),
                                           "note": m.group(3) or None})
    for need in ("from_airports", "to", "dates"):
        if not trip.get(need):
            raise ValueError(f"the ‘{need.split('_')[0]}’ line is missing")
    return trip


def party(trip):
    ages = [int(a) for a in trip.get("children_ages") or [] if int(a) < 18]
    adults = int(trip.get("adults", 1))
    return {"adults": adults, "ages": ages,
            "flight_adults": adults + sum(a >= 12 for a in ages),  # airlines charge 12+ as adults
            "children": sum(2 <= a <= 11 for a in ages), "infants": sum(a < 2 for a in ages)}


def party_text(p):
    kids = len(p["ages"])
    return f"{p['adults']} adult{'s' * (p['adults'] != 1)}" + (f", {kids} child{'ren' * (kids != 1)}" if kids else "")


def flight_links(o, d, out_d, back_d, adults, ages):
    kids2 = [a for a in ages if a >= 2]
    kayak = (f"{o}-{d}/{out_d}/{back_d}/{adults}adults"
             + (f"/children-{'-'.join(map(str, kids2))}" if kids2 else "") + "?sort=price_a")
    sky = {"adultsv2": adults, "cabinclass": "economy", "rtn": 1}
    if ages:
        sky["childrenv2"] = "|".join(map(str, ages))
    q = f"Flights to {d} from {o} on {out_d} through {back_d}"
    pax = f"adults:{adults}" + (f",children:{len(kids2)}[{';'.join(map(str, kids2))}]" if kids2 else "") \
        + (",infantinlap:Y" if any(a < 2 for a in ages) else "")
    leg = lambda a, b, day: f"from:{a},to:{b},departure:{day:%d/%m/%Y}TANYT"
    return {
        "Kayak": f"https://www.kayak.co.uk/flights/{kayak}",
        "Skyscanner": f"https://www.skyscanner.net/transport/flights/{o.lower()}/{d.lower()}/"
                      f"{out_d:%y%m%d}/{back_d:%y%m%d}/?" + urlencode(sky),
        "Google Flights": "https://www.google.com/travel/flights?" + urlencode(
            {"q": q, "curr": CURRENCY, "hl": "en-GB", "gl": "uk"}),
        "Expedia": "https://www.expedia.co.uk/Flights-Search?" + urlencode(
            {"trip": "roundtrip", "leg1": leg(o, d, out_d), "leg2": leg(d, o, back_d),
             "passengers": pax, "options": "cabinclass:economy", "mode": "search"}),
        "Momondo": f"https://www.momondo.co.uk/flight-search/{kayak}",
    }


def hotel_links(loc, ci, co, adults, ages):
    booking = [("ss", loc), ("checkin", ci), ("checkout", co), ("group_adults", adults),
               ("group_children", len(ages)), ("no_rooms", 1), ("selected_currency", CURRENCY)]
    return {"Booking.com": "https://www.booking.com/searchresults.en-gb.html?"
                           + urlencode(booking + [("age", a) for a in ages]),
            "Google Hotels": "https://www.google.com/travel/search?" + urlencode(
                {"q": f"hotels in {loc} {ci} to {co}", "hl": "en-GB", "gl": "uk", "curr": CURRENCY})}


PACKAGE_SITES = {"Jet2holidays": "https://www.jet2holidays.com", "TUI": "https://www.tui.co.uk",
                 "easyJet holidays": "https://www.easyjet.com/en/holidays",
                 "loveholidays": "https://www.loveholidays.com", "On the Beach": "https://www.onthebeach.co.uk"}


def link_line(links):
    return " · ".join(f'<a href="{attr(u)}">{n}</a>' for n, u in links.items())


def search_flights(api, trip, out_d, back_d):
    p = party(trip)
    params = {"engine": "google_flights", "type": "1", "departure_id": ",".join(trip["from_airports"]),
              "arrival_id": trip["to"], "outbound_date": out_d.isoformat(), "return_date": back_d.isoformat(),
              "adults": p["flight_adults"], "children": p["children"], "infants_on_lap": p["infants"],
              "currency": CURRENCY, "gl": COUNTRY, "hl": "en", "sort_by": "2", "show_hidden": "true",
              "stops": STOPS.get(str(trip.get("stops", "any")), "0")}
    data = api.search(params)
    options = []
    for group in ("best_flights", "other_flights"):
        for o in data.get(group) or []:
            legs = o.get("flights") or []
            if o.get("price") and legs:
                airlines = list(dict.fromkeys(l.get("airline") for l in legs if l.get("airline")))
                options.append({"origin": legs[0]["departure_airport"]["id"],
                                "dest": legs[-1]["arrival_airport"]["id"], "price": float(o["price"]),
                                "airlines": airlines, "stops": len(legs) - 1,
                                "depart": (legs[0]["departure_airport"].get("time") or "")[-5:]})
    return options, data.get("price_insights") or {}


def search_hotel(api, trip, out_d, back_d):
    h, p = trip["hotel"], party(trip)
    params = {"engine": "google_hotels", "q": h["location"], "check_in_date": out_d.isoformat(),
              "check_out_date": back_d.isoformat(), "adults": p["adults"], "currency": CURRENCY,
              "gl": COUNTRY, "hl": "en", "sort_by": "3"}
    if p["ages"]:
        params.update(children=len(p["ages"]), children_ages=",".join(str(max(1, a)) for a in p["ages"]))
    rating = next((c for t, c in ((4.5, "9"), (4.0, "8"), (3.5, "7")) if h.get("min_rating", 0) >= t), None)
    if rating:
        params["rating"] = rating
    if h.get("min_stars", 0) >= 2:
        params["hotel_class"] = ",".join(str(s) for s in range(h["min_stars"], 6))
    best = None
    for prop in api.search(params).get("properties") or []:
        total = (prop.get("total_rate") or {}).get("extracted_lowest")
        stars = prop.get("extracted_hotel_class") or 0
        if not total or (prop.get("overall_rating") or 0) < h.get("min_rating", 0) \
                or (h.get("min_stars") and stars and stars < h["min_stars"]):
            continue
        if not best or total < best["total"]:
            best = {"name": prop.get("name"), "total": float(total), "stars": stars,
                    "rating": prop.get("overall_rating"), "link": prop.get("link")}
    return best


def check_trip(api, trip, state, report=False):
    blocks, notes = [], []
    extras = {k.upper(): float(v or 0) for k, v in trip["from_airports"].items()}
    p, memory = party(trip), state.setdefault("trip_prices", {})
    for out_s, back_s in trip["dates"]:
        out_d, back_d = as_date(out_s), as_date(back_s)
        key, label = f"{trip['name']}|{out_d}|{back_d}", f"{esc(trip['name'])} {nice_date(out_d)}"
        prev = memory.get(key, {})
        try:
            options, insights = search_flights(api, trip, out_d, back_d)
        except BudgetExceeded as e:
            notes.append(f"⏸ {esc(trip['name'])}: skipped, {esc(e)}.")
            break
        except SerpError as e:
            notes.append(f"⚠️ {label}: flight search failed ({esc(e)}).")
            continue
        best = {}
        for o in options:
            if o["origin"] in extras and (o["origin"] not in best or o["price"] < best[o["origin"]]["price"]):
                best[o["origin"]] = o
        if not best:
            if report or not prev.get("warned_empty"):
                notes.append(f"🤷 {label}: no fares found (too far ahead, or try stops: any).")
            memory[key] = {**prev, "warned_empty": True}
            continue
        rows = sorted(({**o, "extra": extras[o["origin"]], "true": o["price"] + extras[o["origin"]]}
                       for o in best.values()), key=lambda r: r["true"])
        top, cheapest = rows[0], min(r["price"] for r in rows)
        hotel = None
        if trip.get("hotel"):
            try:
                hotel = search_hotel(api, trip, out_d, back_d)
            except (BudgetExceeded, SerpError) as e:
                notes.append(f"⚠️ Hotel search skipped ({esc(e)}).")
        diy = top["true"] + hotel["total"] + float(trip.get("diy_extras") or 0) if hotel else None

        reasons, lowest, level = [], prev.get("lowest"), insights.get("price_level")
        if not prev.get("last"):
            reasons.append("👀 Now tracking")
        elif lowest and top["true"] <= lowest - TRIP_MIN_DROP:
            reasons.append(f"🔻 New lowest price (was {money(lowest)})")
        elif top["true"] <= prev["last"] - TRIP_MIN_DROP:
            reasons.append(f"🔻 Down from {money(prev['last'])} last check")
        target = trip.get("alert_below")
        if target and cheapest <= target and not (prev.get("last_raw") and prev["last_raw"] <= target):
            reasons.append(f"🎯 Under your {money(target)}")
        if level == "low" and prev.get("level") != "low" and prev.get("last"):
            reasons.append("📉 Google rates this price LOW")
        if diy and prev.get("diy_lowest") and diy <= prev["diy_lowest"] - TRIP_MIN_DROP:
            reasons.append(f"🏨 Holiday total down to {money(diy)}")
        if report and not reasons:
            reasons.append("📋 Report")
        if reasons:
            blocks.append(trip_message(trip, p, out_d, back_d, rows, extras, insights, hotel, diy, reasons))
        memory[key] = {"last": top["true"], "last_raw": cheapest, "level": level,
                       "lowest": min(x for x in (lowest, top["true"]) if x is not None),
                       "diy_lowest": min([x for x in (prev.get("diy_lowest"), diy) if x], default=None)}
    return blocks, notes


def trip_message(trip, p, out_d, back_d, rows, extras, insights, hotel, diy, reasons):
    lines = [f"✈️ <b>{esc(trip['name'])}</b> · {' · '.join(reasons)}",
             f"{nice_date(out_d)} → {nice_date(back_d)} · {party_text(p)}"]
    typical = insights.get("typical_price_range") or []
    if insights.get("price_level"):
        rng = f" (usually {money(typical[0])}–{money(typical[1])})" if len(typical) == 2 else ""
        lines.append(f"Google rates today's price: <b>{esc(insights['price_level'])}</b>{rng}")
    lines.append("\n<u>Airports by true cost</u> (return, whole party + getting there):")
    for i, r in enumerate(rows):
        stops = "direct" if r["stops"] == 0 else f"{r['stops']} stop{'s' * (r['stops'] > 1)}"
        price = f"<b>{money(r['true'])}</b>" if i == 0 else money(r["true"])
        detail = f" = {money(r['price'])} + {money(r['extra'])}" if r["extra"] else ""
        dest = f" → {r['dest']}" if "," in trip["to"] else ""
        lines.append(f"{MEDALS[i] if i < 3 else '▫️'} {r['origin']}{dest} {price}{detail} · "
                     f"{esc(', '.join(r['airlines']))} · {stops}" + (f" · dep {r['depart']}" if r["depart"] else ""))
    missing = [a for a in extras if a not in {r["origin"] for r in rows}]
    if missing:
        lines.append(f"▫️ {', '.join(missing)}: no fares found")
    top = rows[0]
    if hotel:
        name = esc(hotel["name"])
        if hotel.get("link"):
            name = f'<a href="{attr(hotel["link"])}">{name}</a>'
        stars = f" {int(hotel['stars'])}★" if hotel.get("stars") else ""
        lines.append(f"\n🏨 Cheapest match: <b>{name}</b>{stars} · {hotel['rating']}/5 — "
                     f"{money(hotel['total'])} for {(back_d - out_d).days} nights")
        extra = f" + extras {money(trip['diy_extras'])}" if trip.get("diy_extras") else ""
        lines.append(f"🧾 Booking separately: <b>{money(diy)}</b> (flights from {top['origin']} "
                     f"{money(top['true'])} + hotel {money(hotel['total'])}{extra})")
        for q in trip.get("package_quotes") or []:
            diff = diy - q["price"]
            note = f" ({esc(q['note'])})" if q.get("note") else ""
            verdict = (f"package is <b>{money(diff)} cheaper</b> 📦" if diff > 0
                       else f"booking separately saves <b>{money(-diff)}</b> 🧾")
            lines.append(f"📦 vs {esc(q['site'])} {money(q['price'])}{note}: {verdict}")
        if trip.get("package_quotes"):
            lines.append("<i>Packages are ATOL protected and usually include bags and transfers; "
                         "separate bookings generally aren't protected the same way.</i>")
    lines.append("\nCheck flights: " + link_line(flight_links(top["origin"], top["dest"], out_d, back_d,
                                                              p["adults"], p["ages"])))
    if trip.get("hotel"):
        lines.append("Hotels: " + link_line(hotel_links(trip["hotel"]["location"], out_d.isoformat(),
                                                        back_d.isoformat(), p["adults"], p["ages"])))
        lines.append("Packages: " + link_line(PACKAGE_SITES))
    return "\n".join(lines)


def trip_summary(trip, n):
    p = party(trip)
    dates = ", ".join(f"{as_date(a):%d %b}–{as_date(b):%d %b %Y}" for a, b in trip["dates"])
    airports = ", ".join(f"{k}" + (f" (+{money(v)})" if v else "") for k, v in trip["from_airports"].items())
    hotel = (f"\nHotel: {esc(trip['hotel']['location'])}, {trip['hotel'].get('min_rating') or 'any'}+ rating, "
             f"{trip['hotel'].get('min_stars') or 'any'}★+" if trip.get("hotel") else "\nHotel: not compared")
    per_check = len(trip["dates"]) * (2 if trip.get("hotel") else 1)
    return (f"✅ Trip #{n} saved: <b>{esc(trip['name'])}</b>\nFrom {airports} → {trip['to']}\n"
            f"Dates: {dates}\n{party_text(p)}{hotel}\n\nI'll check every {TRIP_EVERY_HOURS}h "
            f"({per_check} searches each time). First results coming up.")


# ───────────────────────────────── commands ──────────────────────────────────

HELP = ("🛒 <b>Send me any product name</b> and I'll find which shops have it, the prices and "
        "stock, then check every 15 minutes.\n\n"
        "• <code>PS5 Pro</code>\n• <code>AirPods Pro 2 under 180</code> → alert at £180 or less\n"
        "• <code>Nintendo Switch 2 -bundle</code> → skip listings with that word\n"
        "• or paste a link to one shop's product page\n\n"
        "/list — products I'm watching\n/stop 2 — stop watching product 2\n"
        "/refresh 2 — re-search the shops for product 2\n\n"
        "✈️ /trip — set up flight + hotel tracking\n/trips — your trips\n"
        "/deltrip 1 — delete trip 1\n/package 1 TUI 2300 — add a package quote to trip 1\n\n"
        "/report — full status of everything now\n/usage — searches used this month")


def pick(items, arg, field):
    if arg.isdigit() and 1 <= int(arg) <= len(items):
        return int(arg) - 1
    return next((i for i, x in enumerate(items) if arg and arg.lower() in str(x.get(field, "")).lower()), None)


def command(text, state, api, fetcher=None):
    word, _, arg = text.strip().partition(" ")
    word, arg = word.lower().lstrip("/").split("@")[0], arg.strip()
    watch, trips = state.setdefault("watch", []), state.setdefault("trip_list", [])

    if word in ("start", "help"):
        return HELP
    if word == "list":
        if not watch:
            return "Nothing watched yet. Send me a product name to start."
        out = ["👀 <b>Watching</b>"]
        for i, w in enumerate(watch, 1):
            best = best_in_stock(w.get("offers") or [])
            status = f"{money(best['price'])} at {esc(best['store'])} ✅" if best else "not in stock"
            target = f" · target {money(w['max_price'])}" if w.get("max_price") else ""
            out.append(f"{i}. {esc(w['title'])} — {status}{target}")
        return "\n".join(out)
    if word == "stop":
        i = pick(watch, arg, "query")
        return "Which one? Send /list, then e.g. /stop 2" if i is None else \
            f"🛑 Stopped watching {esc(watch.pop(i)['title'])}."
    if word == "refresh":
        i = pick(watch, arg, "query")
        if i is None:
            return "Which one? Send /list, then e.g. /refresh 2"
        watch[i]["last_refresh"] = None
        return "🔄 I'll re-search the shops for that on the next check."
    if word == "trip":
        if "\n" not in text.strip():
            return TRIP_HELP
        try:
            trip = parse_trip(text)
        except ValueError as e:
            return f"⚠️ Couldn't save that trip: {esc(e)}.\n\nSend /trip for the template."
        trips[:] = [t for t in trips if t["name"].lower() != trip["name"].lower()]  # same name = update
        trips.append(trip)
        state.setdefault("trip_runs", {}).pop(trip["name"], None)  # check it straight away
        return trip_summary(trip, len(trips))
    if word == "trips":
        if not trips:
            return "No trips yet. Send /trip to set one up."
        return "\n\n".join(trip_summary(t, i).split("\n\nI'll")[0].replace("✅ Trip", "Trip").replace(" saved", "")
                           for i, t in enumerate(trips, 1))
    if word == "deltrip":
        i = pick(trips, arg, "name")
        return "Which one? Send /trips, then e.g. /deltrip 1" if i is None else \
            f"🗑 Deleted trip {esc(trips.pop(i)['name'])}."
    if word == "package":
        m = re.match(r"(\d+)\s+(.+?)\s+£?(\d[\d,]*(?:\.\d+)?)\s*(.*)", arg)
        if not m or not (1 <= int(m.group(1)) <= len(trips)):
            return "Use: /package 1 Jet2holidays 2150 half board (trip number from /trips)"
        trips[int(m.group(1)) - 1].setdefault("package_quotes", []).append(
            {"site": m.group(2), "price": parse_price(m.group(3)), "note": m.group(4) or None})
        return "📦 Added. I'll compare it on the next trip check (or send /report)."
    if word == "report":
        state["pending_report"] = True
        return "📋 Full report coming up."
    if word == "usage":
        used = api.used if api else 0
        return f"SerpApi searches used this month: {used}/{SERPAPI_MONTHLY_LIMIT}"
    return track(text, state, api, fetcher)


def handle_messages(state, api, dry_run=False, timeout=0, fetch_updates=None, fetcher=None):
    """Answer messages sent to the bot. The first person to message it becomes its owner."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return 0
    try:
        updates = (fetch_updates or get_updates)(token, state.get("tg_offset"), timeout)
    except requests.RequestException as e:
        print(f"Telegram poll failed: {e}")
        return 0
    handled = 0
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        chat, text = str((msg.get("chat") or {}).get("id")), (msg.get("text") or "").strip()
        if not owner(state) and chat != "None":
            state["owner"] = chat
            send("👋 Hi! This bot is now linked to you (it ignores everyone else).\n\n" + HELP, state, dry_run)
            if text.lower().lstrip("/").startswith(("start", "help")):
                handled += 1
                continue
        if chat != str(owner(state)) or not text:
            continue
        reply = command(text, state, api, fetcher)
        if reply:
            send(reply, state, dry_run)
        handled += 1
    return handled


# ─────────────────────────────────── main ────────────────────────────────────

def is_due(state, key, minutes, force=False):
    last = state.get("last_run", {}).get(key)
    return force or hours_since(last) * 60 >= minutes - min(10, minutes * 0.2)


def cycle(state, report=False, dry_run=False, poll=True):
    api = make_api(state)
    if poll:
        handle_messages(state, api, dry_run)
    report = report or state.pop("pending_report", False)
    blocks, notes, before = [], [], (api.used if api else 0)

    if is_due(state, "products", CHECK_EVERY_MINUTES, report):
        try:
            blocks += check_watch(api, state, report)
        except Exception as e:
            notes.append(f"⚠️ Product check crashed: {esc(type(e).__name__)}: {esc(e)}")
        state.setdefault("last_run", {})["products"] = now_utc().isoformat()

    for trip in state.get("trip_list", []):
        runs = state.setdefault("trip_runs", {})
        if not report and hours_since(runs.get(trip["name"])) * 60 < TRIP_EVERY_HOURS * 60 - 10:
            continue
        if api is None:
            notes.append("⚠️ Trips need the SERPAPI_KEY secret.")
            break
        try:
            b, n = check_trip(api, trip, state, report)
        except Exception as e:
            b, n = [], [f"⚠️ {esc(trip['name'])} crashed: {esc(type(e).__name__)}: {esc(e)}"]
        blocks, notes = blocks + b, notes + n
        runs[trip["name"]] = now_utc().isoformat()

    if blocks or notes:
        footer = [f"<i>Searches used this month: {api.used}/{api.limit}</i>"] if api and api.used != before else []
        send("\n\n".join(blocks + notes + footer), state, dry_run)
    elif report:
        send("📋 Nothing to report yet. Send me a product name, or /trip to add a trip.", state, dry_run)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deal Alert Bot")
    ap.add_argument("--report", action="store_true", help="send a full report now")
    ap.add_argument("--loop", action="store_true", help="keep running (PC / Raspberry Pi)")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = ap.parse_args(argv)
    load_dotenv()
    state = load_state()
    if not args.loop:
        try:
            cycle(state, args.report, args.dry_run)
        finally:
            save_state(state)
        return 0
    print("Running. Messages are answered straight away. Ctrl+C to stop.")
    while True:
        try:
            handle_messages(state, make_api(state), args.dry_run, timeout=25)
            cycle(state, args.report, args.dry_run, poll=False)
            args.report = False
            save_state(state)
            if not os.environ.get("TELEGRAM_BOT_TOKEN"):
                time.sleep(30)
        except KeyboardInterrupt:
            save_state(state)
            return 0
        except Exception as e:
            print(f"Error: {e}, retrying in 60s")
            save_state(state)
            time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
