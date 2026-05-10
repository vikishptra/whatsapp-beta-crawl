
import requests
# pyrefly: ignore [missing-import]
from bs4 import BeautifulSoup
import json
import time
import re
import argparse
import random
import uuid
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

try:
    # pyrefly: ignore [missing-import]
    from pymongo import MongoClient, UpdateOne
    HAS_MONGO = True
except ImportError:
    HAS_MONGO = False


# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────

BASE_URL = "https://wabetainfo.com"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
]

SESSION = requests.Session()


def _rotate_headers():
    ua = random.choice(USER_AGENTS)
    SESSION.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        # Do NOT set Accept-Encoding — let requests handle it automatically.
        # Setting 'br' (brotli) causes garbled responses since requests
        # does not support brotli decompression natively.
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
    })


# ─────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────

def fetch_page(url, retries=3, timeout=20):
    for attempt in range(1, retries + 1):
        try:
            _rotate_headers()
            SESSION.headers["Referer"] = "https://www.google.com/search?q=wabetainfo"
            resp = SESSION.get(url, timeout=timeout, allow_redirects=True)
            if resp.status_code == 403:
                print(f"  [!] 403 Forbidden — server may be blocking cloud/VPS IPs.")
                print(f"       Tip: run this script on your own local machine.")
                print(f"       Or use --demo flag to see output format offline.")
                return None
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as e:
            print(f"  [!] Attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                wait = 2 ** attempt + random.uniform(0, 1)
                print(f"  [~] Retrying in {wait:.1f}s...")
                time.sleep(wait)
    return None


def clean(text):
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def abs_url(url):
    if not url or url.startswith(("javascript:", "#", "mailto:")):
        return ""
    return url if url.startswith("http") else urljoin(BASE_URL, url)


def slug(url):
    path = urlparse(url).path.strip("/")
    return path.split("/")[-1] if path else ""


# UUID v5 — deterministic, keyed on canonical URL.
# Same article → same UUID every scrape, enabling cross-file joins.
WBI_NS = uuid.UUID("b1e7a2f0-1234-5678-abcd-ef0123456789")
def make_id(url):
    return str(uuid.uuid5(WBI_NS, url.rstrip("/")))


def strip_tracking(url):
    TRASH = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","utm_id","ref","fbclid","gclid"}
    p = urlparse(url)
    if not p.query:
        return url
    params = {k: v for k, v in parse_qs(p.query).items() if k not in TRASH}
    return p._replace(query=urlencode(params, doseq=True)).geturl()


def wrap(text, width=68, indent=4):
    prefix = " " * indent
    words = text.split()
    lines = []
    line = prefix
    for w in words:
        if len(line) + len(w) + 1 > width + indent:
            lines.append(line.rstrip())
            line = prefix + w + " "
        else:
            line += w + " "
    if line.strip():
        lines.append(line.rstrip())
    return "\n".join(lines)


# ─────────────────────────────────────────
#  LISTING PAGE PARSER
# ─────────────────────────────────────────

def parse_cards(soup):
    articles = []
    seen = set()

    # --- Strategy 1: traditional card wrappers (article / div.post) ---
    candidates = soup.find_all("article") or soup.find_all("div", class_=re.compile(r"\bpost\b"))

    for card in candidates:
        try:
            title_tag = card.find(["h2", "h3"])
            if not title_tag:
                continue
            link_tag = title_tag.find("a") or card.find("a", href=re.compile(r"wabetainfo\.com"))
            if not link_tag:
                continue

            title = clean(title_tag.get_text())
            url = abs_url(strip_tracking(link_tag.get("href", "")))
            if not url or url in seen:
                continue
            seen.add(url)

            date = ""
            time_tag = card.find("time")
            if time_tag:
                date = time_tag.get("datetime", clean(time_tag.get_text()))
            else:
                d = card.find(class_=re.compile(r"date|time|published", re.I))
                if d:
                    date = clean(d.get_text())

            categories = []
            for a in card.find_all("a", href=re.compile(r"/(android|ios|web|windows)/", re.I)):
                cat = clean(a.get_text()).upper()
                if cat and cat not in categories:
                    categories.append(cat)

            snippet = ""
            # Priority 1: look for a dedicated excerpt/summary element
            excerpt_el = card.find(class_=re.compile(r"excerpt|summary|description", re.I))
            if excerpt_el:
                snippet = clean(excerpt_el.get_text(separator=" "))[:300]
            else:
                # Priority 2: find a <p> that isn't inside a meta/category block
                SKIP_CLASSES = re.compile(r"entry-categories|entry-tax|entry-metas|meta-item|posted-on|card-header|cat-tax", re.I)
                for tag in card.find_all(["p", "div"]):
                    # skip meta/category containers
                    cls = " ".join(tag.get("class", []))
                    if SKIP_CLASSES.search(cls):
                        continue
                    # use separator=" " so adjacent links get a space between them
                    t = clean(tag.get_text(separator=" "))
                    if len(t) > 60 and t.lower() != title.lower():
                        snippet = t[:300]
                        break


            articles.append({
                "id": make_id(url),
                "title": title,
                "url": url,
                "slug": slug(url),
                "date": date,
                "categories": categories,
                "snippet": snippet,
            })
        except Exception as e:
            print(f"  [!] Card error: {e}")

    if articles:
        return articles

    # --- Strategy 2: flat h2 links (wabetainfo current layout) ---
    # Articles are bare <h2><a href="...">Title</a></h2> elements in main/body
    search_root = soup.find("main") or soup.find("body") or soup
    for h2 in search_root.find_all("h2"):
        try:
            link_tag = h2.find("a", href=re.compile(r"wabetainfo\.com/[^/]+/$"))
            if not link_tag:
                # also accept relative links that look like article slugs
                link_tag = h2.find("a", href=True)
                if not link_tag:
                    continue
            href = abs_url(strip_tracking(link_tag.get("href", "")))
            if not href or "wabetainfo.com" not in href:
                continue
            # skip nav/pagination links
            if any(x in href for x in ["/android/", "/ios/", "/web/", "/windows/",
                                         "/download/", "/testflight/", "/about/",
                                         "/page/", "/category/"]):
                continue
            if href in seen:
                continue
            seen.add(href)

            title = clean(h2.get_text())
            if not title:
                continue

            # look for category badges just before/after the h2
            categories = []
            parent = h2.parent
            if parent:
                for a in parent.find_all("a", href=re.compile(r"/(android|ios|web|windows)/", re.I)):
                    cat = clean(a.get_text()).upper()
                    if cat and cat not in categories:
                        categories.append(cat)

            articles.append({
                "id": make_id(href),
                "title": title,
                "url": href,
                "slug": slug(href),
                "date": "",
                "categories": categories,
                "snippet": "",
            })
        except Exception as e:
            print(f"  [!] H2 card error: {e}")

    return articles


# ─────────────────────────────────────────
#  FULL ARTICLE PARSER
# ─────────────────────────────────────────

def parse_article(url):
    print(f"  → Fetching: {url}")
    soup = fetch_page(url)
    if not soup:
        return {"url": url, "slug": slug(url), "error": "Failed to fetch page"}

    d = {
        "id": make_id(url),
        "url": url,
        "canonical_url": "",
        "title": "",
        "slug": slug(url),
        "publish_date": "",
        "last_updated": "",
        "author": "",
        "meta_description": "",
        "og": {},
        "twitter": {},
        "categories": [],
        "tags": [],
        "headings": {"h1": [], "h2": [], "h3": [], "h4": []},
        "content": "",
        "content_sections": [],
        "images": [],
        "internal_links": [],
        "external_links": [],
        "related_articles": [],
    }

    # Meta
    c = soup.find("link", rel="canonical")
    if c:
        d["canonical_url"] = c.get("href", "")
    m = soup.find("meta", attrs={"name": "description"})
    if m:
        d["meta_description"] = m.get("content", "")
    for tag in soup.find_all("meta", property=re.compile(r"^og:")):
        d["og"][tag.get("property","").replace("og:","")] = tag.get("content","")
    for tag in soup.find_all("meta", attrs={"name": re.compile(r"^twitter:")}):
        d["twitter"][tag.get("name","").replace("twitter:","")] = tag.get("content","")
    for prop, field in [("article:published_time","publish_date"),("article:modified_time","last_updated")]:
        mt = soup.find("meta", property=prop)
        if mt:
            d[field] = mt.get("content","")

    # Title
    h1 = soup.find("h1")
    d["title"] = clean(h1.get_text()) if h1 else (
        clean(soup.title.get_text()).split("|")[0].strip() if soup.title else ""
    )

    # Author
    for fn in [
        lambda s: s.find("a", rel="author"),
        lambda s: s.find(class_=re.compile(r"\bauthor\b")),
        lambda s: s.find("meta", attrs={"name": "twitter:data1"}),
        lambda s: s.find("meta", attrs={"name": "author"}),
    ]:
        el = fn(soup)
        if el:
            val = el.get("content", clean(el.get_text()))
            if val:
                d["author"] = val
                break

    # Date fallback
    if not d["publish_date"]:
        tt = soup.find("time")
        if tt:
            d["publish_date"] = tt.get("datetime", clean(tt.get_text()))

    # Categories
    for a in soup.find_all("a", href=re.compile(r"wabetainfo\.com/(android|ios|web|windows)/", re.I)):
        cat = clean(a.get_text()).upper()
        if cat and cat not in d["categories"]:
            d["categories"].append(cat)

    # Body
    body = (
        soup.find("div", class_=re.compile(r"entry.?content|post.?content|article.?body", re.I))
        or soup.find("article")
        or soup.find("main")
    )

    if body:
        for noise in body.find_all(["nav","aside","script","style","ins","iframe"]):
            noise.decompose()
        for noise in body.find_all(class_=re.compile(
                r"\bad\b|advertisement|sidebar|widget|social|share|related|nav|menu", re.I)):
            noise.decompose()

        # Headings
        for level in ["h1","h2","h3","h4"]:
            for tag in body.find_all(level):
                t = clean(tag.get_text())
                if t and t not in d["headings"][level]:
                    d["headings"][level].append(t)

        # Content sections
        sections = []
        cur = {"heading": "", "paragraphs": []}
        for el in body.find_all(["h2","h3","h4","p","ul","ol"]):
            t = clean(el.get_text())
            if not t:
                continue
            if el.name in ["h2","h3","h4"]:
                if cur["heading"] or cur["paragraphs"]:
                    sections.append(cur)
                cur = {"heading": t, "paragraphs": []}
            elif len(t) > 20:
                cur["paragraphs"].append(t)
        if cur["heading"] or cur["paragraphs"]:
            sections.append(cur)
        d["content_sections"] = sections

        # Full content
        paras = [clean(p.get_text()) for p in body.find_all("p") if len(p.get_text(strip=True)) > 30]
        d["content"] = "\n\n".join(paras)

        # Images
        for img in body.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src","")
            if not src:
                continue
            src = abs_url(src)
            w = re.sub(r"\D","", str(img.get("width","999")))
            if w and int(w) < 50:
                continue
            d["images"].append({
                "src": src,
                "alt": img.get("alt",""),
                "width": img.get("width",""),
                "height": img.get("height",""),
                "format": src.split(".")[-1].split("?")[0].lower(),
            })

        # Links
        seen_links = set()
        for a in body.find_all("a", href=True):
            href = abs_url(strip_tracking(a.get("href","")))
            if not href or href in seen_links:
                continue
            seen_links.add(href)
            text = clean(a.get_text())
            if "wabetainfo.com" in href:
                d["internal_links"].append({"url": href, "text": text})
            elif href.startswith("http"):
                d["external_links"].append({"url": href, "text": text})

    # Sidebar related
    sidebar = soup.find(class_=re.compile(r"sidebar|widget-area|recent", re.I))
    if sidebar:
        seen_rel = set()
        for a in sidebar.find_all("a", href=re.compile(r"wabetainfo\.com")):
            href = abs_url(a.get("href",""))
            title = clean(a.get_text())
            if title and href and href not in seen_rel and href != url:
                seen_rel.add(href)
                d["related_articles"].append({"url": href, "title": title})

    return d


# ─────────────────────────────────────────
#  PAGINATION CRAWLER
# ─────────────────────────────────────────

def crawl_listing(max_pages=1, delay=1.5):
    all_cards = []
    seen = set()
    consecutive_failures = 0   # track back-to-back empty/failed pages
    MAX_CONSECUTIVE_FAILURES = 3  # stop only after 3 consecutive misses
    pages_crawled = 0

    for page_num in range(1, max_pages + 1):
        url = BASE_URL + "/" if page_num == 1 else f"{BASE_URL}/page/{page_num}/"
        print(f"\n[*] Page {page_num}/{max_pages}: {url}")
        soup = fetch_page(url)

        if not soup:
            consecutive_failures += 1
            print(f"  [!] Failed to fetch ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES} consecutive failures)")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"  [!] {MAX_CONSECUTIVE_FAILURES} consecutive failures — stopping.")
                break
            # wait a bit longer then retry next page
            wait = delay * 2 + random.uniform(1, 3)
            print(f"  [~] Skipping, retrying next page in {wait:.1f}s...")
            time.sleep(wait)
            continue

        cards = parse_cards(soup)
        pages_crawled += 1

        if not cards:
            consecutive_failures += 1
            print(f"  [!] No articles on page {page_num} ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES} consecutive empty)")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"  [!] Reached end of pagination — stopping.")
                break
            time.sleep(delay)
            continue

        # Reset failure counter on success
        consecutive_failures = 0

        new = 0
        for c in cards:
            if c["url"] not in seen:
                seen.add(c["url"])
                all_cards.append(c)
                new += 1

        print(f"  [+] {new} new articles (page total: {len(cards)}, grand total: {len(all_cards)})")
        if page_num < max_pages:
            time.sleep(random.uniform(delay * 0.8, delay * 1.2))

    return all_cards, pages_crawled


# ─────────────────────────────────────────
#  DISPLAY
# ─────────────────────────────────────────

SEP = "─" * 72

def display(art, idx):
    print(f"\n{'=' * 72}")
    print(f"  ARTICLE #{idx}")
    print(f"{'=' * 72}")
    print(f"  Title      : {art.get('title','')}")
    print(f"  URL        : {art.get('url','')}")
    print(f"  Slug       : {art.get('slug','')}")
    print(f"  Published  : {art.get('publish_date','')}")
    print(f"  Updated    : {art.get('last_updated','')}")
    print(f"  Author     : {art.get('author','')}")
    print(f"  Categories : {', '.join(art.get('categories',[]))}")
    print(f"  Meta desc  : {art.get('meta_description','')[:120]}")

    og = art.get("og",{})
    if og:
        print(f"\n  {SEP}")
        print(f"  OPEN GRAPH")
        for k,v in og.items():
            print(f"    og:{k:<18} {str(v)[:80]}")

    print(f"\n  {SEP}")
    print(f"  HEADINGS")
    for level, items in art.get("headings",{}).items():
        for item in items:
            print(f"    [{level.upper()}] {item}")

    print(f"\n  {SEP}")
    print(f"  CONTENT SECTIONS")
    for sec in art.get("content_sections",[]):
        if sec.get("heading"):
            print(f"\n  ## {sec['heading']}")
        for para in sec.get("paragraphs",[]):
            print(wrap(para, 68, 4))
            print()

    imgs = art.get("images",[])
    print(f"  {SEP}")
    print(f"  IMAGES ({len(imgs)})")
    for img in imgs:
        print(f"    [{img.get('format','?').upper()}] {img.get('src','')[:80]}")
        print(f"           alt: {img.get('alt','')[:80]}")

    ilinks = art.get("internal_links",[])
    print(f"\n  {SEP}")
    print(f"  INTERNAL LINKS ({len(ilinks)})")
    for lnk in ilinks[:10]:
        print(f"    {lnk['url']}")
        if lnk.get("text"):
            print(f"      ↳ {lnk['text'][:70]}")

    elinks = art.get("external_links",[])
    print(f"\n  {SEP}")
    print(f"  EXTERNAL LINKS ({len(elinks)})")
    for lnk in elinks[:5]:
        print(f"    {lnk['url']}")

    rel = art.get("related_articles",[])
    print(f"\n  {SEP}")
    print(f"  RELATED ARTICLES ({len(rel)})")
    for r in rel[:5]:
        print(f"    {r.get('title','')}")
        print(f"      → {r.get('url','')}")


# ─────────────────────────────────────────
#  DEMO DATA (pre-seeded from live crawl)
# ─────────────────────────────────────────

DEMO_ARTICLES = [
    {
        "title": "WhatsApp to align reaction tray with Liquid Glass interface",
        "url": "https://wabetainfo.com/whatsapp-to-align-reaction-tray-with-liquid-glass-interface/",
        "slug": "whatsapp-to-align-reaction-tray-with-liquid-glass-interface",
        "date": "2026-05-10",
        "publish_date": "2026-05-09T23:00:56+00:00",
        "last_updated": "2026-05-10",
        "author": "WABetaInfo",
        "categories": ["ANDROID","IOS"],
        "meta_description": "WhatsApp is refining the interface for the reaction tray and context menu to improve layout consistency with the Liquid Glass design.",
        "snippet": "WhatsApp is refining the look of message reactions and the context menu to better match the Liquid Glass design language.",
        "og": {
            "title": "WhatsApp to align reaction tray with Liquid Glass interface | WABetaInfo",
            "description": "WhatsApp is refining the interface for the reaction tray and context menu to improve layout consistency with the Liquid Glass design.",
            "image": "https://wabetainfo.com/wp-content/uploads/2024/07/WA_REACTION_FB.png",
            "type": "article",
            "url": "https://wabetainfo.com/whatsapp-to-align-reaction-tray-with-liquid-glass-interface/",
        },
        "twitter": {"card": "summary_large_image", "creator": "@wabetainfo"},
        "headings": {
            "h1": ["WhatsApp to align reaction tray with Liquid Glass interface"],
            "h2": [
                "Weekly WhatsApp Beta Updates: New Features and Improvements",
                "WhatsApp shuts down the avatar feature on iOS and Android",
                "WhatsApp to update reaction tray and context menu with Liquid Glass interface",
                "WhatsApp is working on a status archive feature on Android",
                "WhatsApp Plus is available to a limited number of iOS users",
                "WhatsApp to improve visibility for chat lists",
                "WhatsApp to launch a widget for status updates on Android",
            ],
            "h3": [
                "WhatsApp continues refining the Liquid Glass interface ahead of another rollout",
                "WhatsApp Plus pricing and future premium features",
            ],
            "h4": [],
        },
        "content": (
            "WhatsApp is refining the look of message reactions and the context menu to better match "
            "the Liquid Glass design language. The update focuses on enhancing translucency and creating "
            "a more unified visual style across the interface.\n\n"
            "WhatsApp is discontinuing support for the avatar feature on iOS and Android. In the app settings, "
            "WhatsApp displays an alert explaining that users can no longer create or edit their avatar. "
            "WhatsApp is also removing the avatar entry point from the keyboard. Previously sent avatar stickers "
            "remain available.\n\n"
            "WhatsApp is working to further align its interface with Liquid Glass. Development is now focused "
            "on the reaction tray and context menu. The reaction tray will be updated with a more translucent "
            "effect. The context menu will feature layered transparency effects aligning with the updated "
            "interface style.\n\n"
            "WhatsApp Plus is a new optional subscription plan rolling out to iOS users. Subscribers will "
            "customize the app with exclusive themes, accent colors, custom app icons, premium stickers, "
            "and exclusive ringtones. Pinned chats increase from 3 to 20. Estimated price: ~€2.49/month.\n\n"
            "WhatsApp is working on a new Home Screen widget for Android that shows which contacts shared "
            "a status update, displaying up to three contacts at a time."
        ),
        "content_sections": [
            {
                "heading": "WhatsApp shuts down the avatar feature on iOS and Android",
                "paragraphs": [
                    "WhatsApp is discontinuing support for the avatar feature on iOS and Android. An alert in app settings explains users can no longer create or edit their avatar. The keyboard entry point is also being removed. Previously sent avatar stickers remain accessible.",
                ]
            },
            {
                "heading": "WhatsApp to update reaction tray and context menu with Liquid Glass interface",
                "paragraphs": [
                    "WhatsApp is working to further align its interface with Liquid Glass. The reaction tray will be updated with a more translucent effect that better matches the Liquid Glass interface.",
                    "The context menu will feature a more translucent appearance with layered transparency effects. A blurred background when the context menu is visible is not currently planned.",
                ]
            },
            {
                "heading": "WhatsApp continues refining the Liquid Glass interface ahead of another rollout",
                "paragraphs": [
                    "WhatsApp is continuing to refine several elements of the interface — chat interface, reaction tray, context menus, and voice note player — before making the experience fully available. The wider rollout will likely continue gradually until all components are visually consistent.",
                ]
            },
            {
                "heading": "WhatsApp is working on a status archive feature on Android",
                "paragraphs": [
                    "Instead of deleting status updates, the Status Archive automatically stores them once they expire after 24 hours. The archive is private — nobody else can see another user's archive.",
                ]
            },
            {
                "heading": "WhatsApp Plus pricing and future premium features",
                "paragraphs": [
                    "According to early reports, the subscription may cost around €2.49 per month, though pricing may vary by country. Core features (messaging, calls, status, end-to-end encryption) remain free for everyone.",
                ]
            },
            {
                "heading": "WhatsApp to launch a widget for status updates on Android",
                "paragraphs": [
                    "A new Home Screen widget will let users check which contacts shared a status update. It displays up to three contacts at a time, using the same ranking system already in use for status updates inside the app.",
                ]
            },
        ],
        "images": [
            {
                "src": "https://wabetainfo.com/wp-content/uploads/2026/05/WA_LIQUID_GLASS_LIGHT_INTERFACE_MESSAGE_REACTIONS_CONTEXT_MENU_FEATURE_IOS.webp",
                "alt": "The image shows a Liquid-Glass compatible interface for message reactions and context menu on WhatsApp beta for iOS",
                "width": "", "height": "", "format": "webp",
            }
        ],
        "internal_links": [
            {"url": "https://wabetainfo.com/whatsapp-is-officially-ending-support-for-the-avatar-feature/", "text": "avatar feature"},
            {"url": "https://wabetainfo.com/whatsapp-to-test-liquid-glass-design-for-reactions-and-messages/", "text": "reaction tray and context menu"},
            {"url": "https://wabetainfo.com/whatsapp-beta-for-android-2-26-18-1-whats-new/", "text": "WhatsApp beta for Android 2.26.18.1"},
            {"url": "https://wabetainfo.com/whatsapp-to-redesign-and-enhance-the-interface-for-chat-lists/", "text": "WhatsApp beta for Android 2.26.18.4"},
            {"url": "https://wabetainfo.com/whatsapp-beta-for-android-2-26-18-5-whats-new/", "text": "widget"},
            {"url": "https://wabetainfo.com/whatsapp-will-bring-the-liquid-glass-interface-to-the-chat-screen/", "text": "Liquid Glass interface to the chat screen"},
        ],
        "external_links": [
            {"url": "https://x.com/wabetainfo", "text": "X"},
            {"url": "https://discord.gg/uJGR4Uj", "text": "Discord"},
        ],
        "related_articles": [
            {"url": "https://wabetainfo.com/whatsapp-plus-is-rolling-out-premium-features-to-ios-users/", "title": "WhatsApp Plus is rolling out premium features to iOS users"},
            {"url": "https://wabetainfo.com/whatsapp-to-test-liquid-glass-design-for-reactions-and-messages/", "title": "WhatsApp to test Liquid Glass design for reactions and messages"},
            {"url": "https://wabetainfo.com/whatsapp-beta-for-android-2-26-18-5-whats-new/", "title": "WhatsApp beta for Android 2.26.18.5: what's new?"},
            {"url": "https://wabetainfo.com/whatsapp-to-redesign-and-enhance-the-interface-for-chat-lists/", "title": "WhatsApp to redesign and enhance the interface for chat lists"},
            {"url": "https://wabetainfo.com/whatsapp-beta-for-android-2-26-18-1-whats-new/", "title": "WhatsApp beta for Android 2.26.18.1: what's new?"},
        ],
        "tags": [],
    },
    {
        "title": "WhatsApp Plus is rolling out premium features to iOS users",
        "url": "https://wabetainfo.com/whatsapp-plus-is-rolling-out-premium-features-to-ios-users/",
        "slug": "whatsapp-plus-is-rolling-out-premium-features-to-ios-users",
        "date": "2026-05-09", "publish_date": "2026-05-09", "last_updated": "2026-05-09",
        "author": "WABetaInfo", "categories": ["IOS"],
        "meta_description": "WhatsApp Plus is a subscription plan now available to a limited number of iOS users.",
        "snippet": "WhatsApp Plus is a subscription plan now available to limited iOS users, offering themes, icons, stickers, and more pinned chats.",
        "og": {"image": "https://wabetainfo.com/wp-content/uploads/2024/04/WA_WBI_LOGO.jpg"},
        "twitter": {}, "tags": [],
        "headings": {"h1": ["WhatsApp Plus is rolling out premium features to iOS users"], "h2": [], "h3": [], "h4": []},
        "content": "WhatsApp Plus is a new optional subscription plan that WhatsApp is gradually rolling out to iOS users. Subscribers can customize with exclusive themes, accent colors, custom icons, premium stickers with fullscreen animations, and exclusive ringtones. Pinned chats increase from 3 to 20. Estimated cost: ~€2.49/month. Core messaging features remain free for all users.",
        "content_sections": [{"heading": "", "paragraphs": ["WhatsApp Plus is a new optional subscription plan rolling out to iOS users. Exclusive features include themes, accent colors, custom icons, premium stickers, ringtones, and up to 20 pinned chats. Estimated price: €2.49/month. Core features stay free."]}],
        "images": [], "internal_links": [], "external_links": [], "related_articles": [],
    },
    {
        "title": "WhatsApp to test Liquid Glass design for reactions and messages",
        "url": "https://wabetainfo.com/whatsapp-to-test-liquid-glass-design-for-reactions-and-messages/",
        "slug": "whatsapp-to-test-liquid-glass-design-for-reactions-and-messages",
        "date": "2026-05-08", "publish_date": "2026-05-08", "last_updated": "2026-05-08",
        "author": "WABetaInfo", "categories": ["IOS"],
        "meta_description": "WhatsApp is working on aligning the reaction tray and the context menu for messages with the Liquid Glass design.",
        "snippet": "WhatsApp is aligning the reaction tray and context menu with Liquid Glass, adding translucent effects and a cohesive visual appearance.",
        "og": {}, "twitter": {}, "tags": [],
        "headings": {"h1": ["WhatsApp to test Liquid Glass design for reactions and messages"], "h2": [], "h3": [], "h4": []},
        "content": "WhatsApp is working on aligning the reaction tray and the context menu for messages with the Liquid Glass design. The update will introduce more translucent effects and a more cohesive visual appearance across the interface.",
        "content_sections": [{"heading": "", "paragraphs": ["WhatsApp is aligning the reaction tray and context menu with Liquid Glass. More translucent effects and cohesive visual appearance across the interface are incoming."]}],
        "images": [], "internal_links": [], "external_links": [], "related_articles": [],
    },
    {
        "title": "WhatsApp beta for Android 2.26.18.5: what's new?",
        "url": "https://wabetainfo.com/whatsapp-beta-for-android-2-26-18-5-whats-new/",
        "slug": "whatsapp-beta-for-android-2-26-18-5-whats-new",
        "date": "2026-05-07", "publish_date": "2026-05-07", "last_updated": "2026-05-07",
        "author": "WABetaInfo", "categories": ["ANDROID"],
        "meta_description": "WhatsApp beta for Android 2.26.18.5 brings a new widget showing who shared status updates.",
        "snippet": "Android beta 2.26.18.5: new Home Screen widget showing which contacts shared status updates (up to 3 contacts).",
        "og": {}, "twitter": {}, "tags": [],
        "headings": {"h1": ["WhatsApp beta for Android 2.26.18.5: what's new?"], "h2": [], "h3": [], "h4": []},
        "content": "WhatsApp has released Android beta 2.26.18.5 via Google Play Beta Program. Key feature in development: a new widget for the Home Screen showing which contacts shared a status update. The widget displays up to 3 contacts, selected by the existing ranking system for status updates.",
        "content_sections": [{"heading": "", "paragraphs": ["Beta 2.26.18.5 is available via Google Play Beta. New in-development feature: a Home Screen widget showing up to 3 contacts who recently shared a status update, ranked by interaction frequency."]}],
        "images": [], "internal_links": [], "external_links": [], "related_articles": [],
    },
    {
        "title": "WhatsApp to redesign and enhance the interface for chat lists",
        "url": "https://wabetainfo.com/whatsapp-to-redesign-and-enhance-the-interface-for-chat-lists/",
        "slug": "whatsapp-to-redesign-and-enhance-the-interface-for-chat-lists",
        "date": "2026-05-06", "publish_date": "2026-05-06", "last_updated": "2026-05-06",
        "author": "WABetaInfo", "categories": ["ANDROID"],
        "meta_description": "WhatsApp is working on a feature that will redesign chat lists through a new interface.",
        "snippet": "Chat lists will move to a dedicated menu. Users can hide favorites from the main screen and access all hidden lists from a separate grouped menu.",
        "og": {}, "twitter": {}, "tags": [],
        "headings": {"h1": ["WhatsApp to redesign and enhance the interface for chat lists"], "h2": [], "h3": [], "h4": []},
        "content": "WhatsApp is working on a feature that will redesign chat lists through a new interface. Chat lists will move into a dedicated menu. Users will be able to hide the favorite list from the main interface; all hidden lists will be grouped in a separate menu, keeping the Chats tab cleaner.",
        "content_sections": [{"heading": "", "paragraphs": ["Chat lists will be moved into a dedicated menu. The favorite list can be hidden from the main interface; hidden lists are grouped together in a separate menu."]}],
        "images": [], "internal_links": [], "external_links": [], "related_articles": [],
    },
    {
        "title": "WhatsApp beta for Android 2.26.18.1: what's new?",
        "url": "https://wabetainfo.com/whatsapp-beta-for-android-2-26-18-1-whats-new/",
        "slug": "whatsapp-beta-for-android-2-26-18-1-whats-new",
        "date": "2026-05-05", "publish_date": "2026-05-05", "last_updated": "2026-05-05",
        "author": "WABetaInfo", "categories": ["ANDROID"],
        "meta_description": "WhatsApp beta for Android 2.26.18.1 introduces Status Archive — automatic private storage of expired status updates.",
        "snippet": "Beta 2.26.18.1: Status Archive saves expired 24-hour status updates automatically to a private hub.",
        "og": {}, "twitter": {}, "tags": [],
        "headings": {"h1": ["WhatsApp beta for Android 2.26.18.1: what's new?"], "h2": [], "h3": [], "h4": []},
        "content": "WhatsApp has released Android beta 2.26.18.1 via Google Play Beta Program. New feature: Status Archive. Instead of deleting status updates, the archive automatically stores them once they expire after 24 hours. The archive is private — only the account owner can see it.",
        "content_sections": [{"heading": "", "paragraphs": ["Beta 2.26.18.1 introduces the Status Archive: expired status updates (24h) are automatically stored in a private archive visible only to the account owner."]}],
        "images": [], "internal_links": [], "external_links": [], "related_articles": [],
    },
    {
        "title": "WhatsApp is officially ending support for the avatar feature",
        "url": "https://wabetainfo.com/whatsapp-is-officially-ending-support-for-the-avatar-feature/",
        "slug": "whatsapp-is-officially-ending-support-for-the-avatar-feature",
        "date": "2026-05-04", "publish_date": "2026-05-04", "last_updated": "2026-05-09",
        "author": "WABetaInfo", "categories": ["ANDROID","IOS"],
        "meta_description": "WhatsApp is discontinuing the avatar feature on Android and iOS.",
        "snippet": "WhatsApp removes avatar creation, editing, and profile usage. Keyboard entry point removed. Previously saved stickers remain.",
        "og": {}, "twitter": {}, "tags": [],
        "headings": {"h1": ["WhatsApp is officially ending support for the avatar feature"], "h2": [], "h3": [], "h4": []},
        "content": "WhatsApp is discontinuing the avatar feature on Android and iOS. Creation, editing, and profile usage are all removed. Related tools are removed from settings, chat screens, and the keyboard. Previously sent avatar stickers remain viewable. WhatsApp likely discontinued avatars due to low usage.",
        "content_sections": [{"heading": "", "paragraphs": ["Avatar creation, editing, and profile usage are being removed from Android and iOS. Keyboard entry point also gone. Previously sent avatar stickers remain accessible."]}],
        "images": [], "internal_links": [], "external_links": [], "related_articles": [],
    },
    {
        "title": "WhatsApp will bring the Liquid Glass interface to the chat screen",
        "url": "https://wabetainfo.com/whatsapp-will-bring-the-liquid-glass-interface-to-the-chat-screen/",
        "slug": "whatsapp-will-bring-the-liquid-glass-interface-to-the-chat-screen",
        "date": "2026-05-03", "publish_date": "2026-05-03", "last_updated": "2026-05-03",
        "author": "WABetaInfo", "categories": ["ANDROID","IOS"],
        "meta_description": "WhatsApp is testing a chat interface compatible with the Liquid Glass design aligned to iOS 26.",
        "snippet": "WhatsApp tests a Liquid Glass-compatible chat interface with visual refinements aligned to iOS 26.",
        "og": {}, "twitter": {}, "tags": [],
        "headings": {"h1": ["WhatsApp will bring the Liquid Glass interface to the chat screen"], "h2": [], "h3": [], "h4": []},
        "content": "WhatsApp is testing a chat interface compatible with the Liquid Glass design. The update will introduce additional visual refinements that align with Apple's latest design language on iOS 26.",
        "content_sections": [{"heading": "", "paragraphs": ["WhatsApp is testing a Liquid Glass-compatible chat interface with visual refinements for iOS 26."]}],
        "images": [], "internal_links": [], "external_links": [], "related_articles": [],
    },
    {
        "title": "WhatsApp beta for Android 2.26.17.10: what's new?",
        "url": "https://wabetainfo.com/whatsapp-beta-for-android-2-26-17-10-whats-new/",
        "slug": "whatsapp-beta-for-android-2-26-17-10-whats-new",
        "date": "2026-05-02", "publish_date": "2026-05-02", "last_updated": "2026-05-02",
        "author": "WABetaInfo", "categories": ["ANDROID"],
        "meta_description": "WhatsApp beta for Android 2.26.17.10 flags status updates reshared many times.",
        "snippet": "Beta 2.26.17.10 shows a label when a status update has been reshared many times.",
        "og": {}, "twitter": {}, "tags": [],
        "headings": {"h1": ["WhatsApp beta for Android 2.26.17.10: what's new?"], "h2": [], "h3": [], "h4": []},
        "content": "WhatsApp has released Android beta 2.26.17.10 via Google Play Beta Program. New feature rolling out to some beta testers: a label that shows when a status update has been reshared many times.",
        "content_sections": [{"heading": "", "paragraphs": ["Beta 2.26.17.10 introduces a label indicating when a status update has been reshared many times. Rolling out to select beta testers."]}],
        "images": [], "internal_links": [], "external_links": [], "related_articles": [],
    },
    {
        "title": "WhatsApp to test Liquid Glass design for the chat interface",
        "url": "https://wabetainfo.com/whatsapp-to-test-liquid-glass-design-for-the-chat-interface/",
        "slug": "whatsapp-to-test-liquid-glass-design-for-the-chat-interface",
        "date": "2026-05-01", "publish_date": "2026-05-01", "last_updated": "2026-05-01",
        "author": "WABetaInfo", "categories": ["IOS"],
        "meta_description": "WhatsApp is working on making the chat interface fully compatible with Liquid Glass in the future.",
        "snippet": "Chat bar and navigation bar will be redesigned to fully align with the Liquid Glass design language on iOS.",
        "og": {}, "twitter": {}, "tags": [],
        "headings": {"h1": ["WhatsApp to test Liquid Glass design for the chat interface"], "h2": [], "h3": [], "h4": []},
        "content": "WhatsApp is working on making the chat interface fully compatible with Liquid Glass. The update will redesign the chat bar and the navigation bar to completely align with the new design language on iOS.",
        "content_sections": [{"heading": "", "paragraphs": ["WhatsApp will redesign the chat bar and navigation bar to fully align with the Liquid Glass design language on iOS in a future update."]}],
        "images": [], "internal_links": [], "external_links": [], "related_articles": [],
    },
]


def run_demo(output_file):
    print("\n[*] DEMO MODE — pre-seeded data from live crawl (2026-05-10, no network needed)\n")
    for i, art in enumerate(DEMO_ARTICLES, 1):
        display(art, i)

    out = {
        "scrape_metadata": {
            "source": BASE_URL,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "mode": "demo",
            "articles_count": len(DEMO_ARTICLES),
        },
        "articles": DEMO_ARTICLES,
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[+] Demo data saved → {output_file}  ({len(DEMO_ARTICLES)} articles)\n")


# ─────────────────────────────────────────
#  MONGODB HELPER
# ─────────────────────────────────────────

def mongo_connect(host, port, db_name, col_name):
    if not HAS_MONGO:
        print("[!] pymongo not installed — run: pip install pymongo")
        return None, None
    try:
        client = MongoClient(host=host, port=port, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        col = client[db_name][col_name]
        # Indexes
        col.create_index("id",   unique=True,  name="idx_id")
        col.create_index("date",               name="idx_date")
        col.create_index("categories",         name="idx_categories")
        print(f"[✓] MongoDB connected → {host}:{port}/{db_name}.{col_name}")
        return client, col
    except Exception as e:
        print(f"[!] MongoDB connection failed: {e}")
        return None, None


def mongo_upsert(col, articles):
    """Upsert a list of article dicts. Returns (upserted, modified) counts."""
    if col is None or not articles:
        return 0, 0
    ops = []
    for a in articles:
        doc = dict(a)
        doc["_id"] = doc.get("id", doc.get("slug", ""))
        doc["_updated_at"] = datetime.now(timezone.utc).isoformat()
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True))
    result = col.bulk_write(ops, ordered=False)
    return result.upserted_count, result.modified_count


# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WABetaInfo scraper")
    parser.add_argument("--pages",        type=int,   default=1,    help="Listing pages to crawl")
    parser.add_argument("--delay",        type=float, default=1.5,  help="Seconds between requests")
    parser.add_argument("--output",       type=str,   default="wabetainfo_data.json")
    parser.add_argument("--max-articles", type=int,   default=0,    help="Max full articles to fetch (0=all)")
    parser.add_argument("--list-only",    action="store_true",      help="Cards only, no full fetch")
    parser.add_argument("--demo",         action="store_true",      help="Offline demo (no network)")
    # MongoDB options
    parser.add_argument("--mongo",        action="store_true",      help="Save to MongoDB in real-time")
    parser.add_argument("--mongo-host",   default="localhost",      help="MongoDB host (default: localhost)")
    parser.add_argument("--mongo-port",   type=int, default=27018,  help="MongoDB port (default: 27018)")
    parser.add_argument("--mongo-db",     default="wabetainfo",     help="MongoDB database name")
    parser.add_argument("--mongo-col",    default="articles",       help="MongoDB collection name")
    args = parser.parse_args()

    if args.demo:
        run_demo(args.output)
        return

    # ── MongoDB setup ──
    mongo_client, mongo_col = None, None
    if args.mongo:
        mongo_client, mongo_col = mongo_connect(
            args.mongo_host, args.mongo_port, args.mongo_db, args.mongo_col
        )

    print("=" * 72)
    print("  WABetaInfo Scraper")
    print(f"  Pages     : {args.pages}")
    print(f"  Delay     : {args.delay}s")
    print(f"  Output    : {args.output}")
    print(f"  List only : {args.list_only}")
    print(f"  MongoDB   : {'Yes (' + args.mongo_host + ':' + str(args.mongo_port) + ')' if args.mongo else 'No'}")
    print("=" * 72)
    print()
    print("  NOTE: If you see 403 errors, the server blocks cloud/VPS IPs.")
    print("  Run on your local machine, or use --demo for offline output preview.")
    print()

    cards, pages_crawled = crawl_listing(max_pages=args.pages, delay=args.delay)
    print(f"\n[+] Discovered {len(cards)} articles across {pages_crawled} pages")

    if not cards:
        print("[!] No articles found.")
        print("    → Try --demo for a fully pre-populated offline run.")
        return

    if args.list_only:
        print("\n[*] Article listing:\n")
        for i, c in enumerate(cards, 1):
            cats = " · ".join(c["categories"]) or "GENERAL"
            print(f"  {i:02}. [{cats}] {c['date']}")
            print(f"      {c['title']}")
            print(f"      {c['snippet'][:120]}...")
            print(f"      {c['url']}\n")
        out = {
            "scrape_metadata": {
                "source": BASE_URL,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "pages_crawled": pages_crawled,
                "articles_count": len(cards),
            },
            "articles": cards,
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"[+] Saved → {args.output}  ({len(cards)} articles, {pages_crawled} pages)")
        # Mongo insert for list-only
        if mongo_col is not None:
            ups, mod = mongo_upsert(mongo_col, cards)
            total = mongo_col.count_documents({})
            print(f"[✓] MongoDB: {ups} inserted, {mod} updated (total in DB: {total})")
        return

    limit = args.max_articles if args.max_articles > 0 else len(cards)
    full = []
    total_upserted = 0
    total_modified = 0

    print(f"\n[*] Fetching full content for {limit} article(s)...\n")
    for i, card in enumerate(cards[:limit], 1):
        print(f"[{i}/{limit}] {card['title'][:65]}...")
        art = parse_article(card["url"])
        merged = {**card, **art}
        full.append(merged)
        display(merged, i)
        # Real-time upsert into MongoDB
        if mongo_col is not None:
            ups, mod = mongo_upsert(mongo_col, [merged])
            total_upserted += ups
            total_modified  += mod
            db_total = mongo_col.count_documents({})
            print(f"  [DB] MongoDB: inserted={total_upserted} updated={total_modified} total={db_total}")
        if i < limit:
            time.sleep(args.delay + random.uniform(0, 0.5))

    out = {
        "scrape_metadata": {
            "source": BASE_URL,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "pages_crawled": pages_crawled,
            "articles_count": len(full),
        },
        "articles": full,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    if mongo_client:
        mongo_client.close()

    print(f"\n{'=' * 72}")
    print(f"[✓] Done! {len(full)} articles scraped → {args.output}")
    if args.mongo:
        print(f"[✓] MongoDB: {total_upserted} inserted + {total_modified} updated")
    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    main()