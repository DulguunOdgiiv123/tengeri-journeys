#!/usr/bin/env python3
"""Fill traveller posts (data/posts.json) from their links and keep their cover photos in img/posts/.

Office staff add a post in the admin (/admin, "Аялагчдын нийтлэл") by pasting only its link and picking the region.
GitHub runs this script (.github/workflows/fill-posts.yml) after every save of data/posts.json. For each post it:
  - works out the source (Naver blog, Naver cafe, Brunch) from the link when none was chosen;
  - when the title or date is empty, reads the post page's public preview data (og:title, og:image, author,
    publish date) and fills only the fields that are still empty;
  - downloads the cover photo into img/posts/, so cards never go blank when Naver refuses other sites' requests.
Nothing staff typed is overwritten, and the region is always theirs. No AI: plain web requests and fixed patterns,
Python standard library plus Pillow for shrinking photos (optional: without it photos are kept as downloaded).

  python3 .github/scripts/fill_posts.py                 fill empty fields, copy photos, rewrite data/posts.json
  python3 .github/scripts/fill_posts.py --check         read every post's page, compare with what is stored, change nothing
  python3 .github/scripts/fill_posts.py --only minjokim  limit either mode to posts whose link contains this text
"""
import datetime as dt
import email.utils
import hashlib
import html
import http.cookiejar
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "data", "posts.json")
IMG_DIR = os.path.join(ROOT, "img", "posts")
ORDER = ["src", "url", "title", "by", "date", "region", "img"]   # field order of the admin form (admin/config.yml)
KST = dt.timezone(dt.timedelta(hours=9))
UA_DESK = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
UA_MOB = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/17.5 Mobile/15E148 Safari/604.1")
GENERIC_TITLES = {"네이버 블로그", "naver blog", "네이버 카페", "naver cafe", "브런치", "브런치스토리", "brunch", "brunch story"}
REFERER = {"blog": "https://blog.naver.com/", "cafe": "https://cafe.naver.com/"}

# Brunch answers the first request with a cookie and a redirect, so keep cookies between requests.
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
report = []


def log(line=""):
    print(line, flush=True)
    report.append(line)


def get(url, ua=UA_DESK, referer=None, timeout=15):
    """(final url, content type, body) for a GET; empty type and body when anything goes wrong."""
    headers = {"User-Agent": ua, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6"}
    if referer:
        headers["Referer"] = referer
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            return r.geturl(), r.headers.get("Content-Type", ""), r.read(15_000_000)
    except Exception as e:   # one dead link must not stop the others
        log(f"    could not open {url} ({e.__class__.__name__}: {e})")
        return url, "", b""


def text(body):
    m = re.search(rb'charset=["\']?([\w-]+)', body[:4000])
    try:
        return body.decode(m.group(1).decode() if m else "utf-8", "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def meta(page, *names):
    """Content of the first non-empty <meta property|name="NAME" content="..."> among names (either attribute order)."""
    for n in names:
        n = re.escape(n)
        for pat in (rf'<meta\b[^>]*?\b(?:property|name)\s*=\s*["\']{n}["\'][^>]*?\bcontent\s*=\s*(["\'])(.*?)\1',
                    rf'<meta\b[^>]*?\bcontent\s*=\s*(["\'])(.*?)\1[^>]*?\b(?:property|name)\s*=\s*["\']{n}["\']'):
            m = re.search(pat, page, re.I | re.S)
            if m and clean(m.group(2)):
                return clean(m.group(2))
    return ""


def iso(y, m, d):
    try:
        return dt.date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return ""


def find_date(page):
    """Publish date as YYYY-MM-DD from the usual places in Naver and Brunch pages, or ""."""
    for n in ("article:published_time", "og:regDate", "og:article:published_time", "naverblog:createdate"):
        m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", meta(page, n))
        if m:
            return iso(*m.groups())
    for pat in (r'"datePublished"\s*:\s*"(\d{4})-(\d{2})-(\d{2})',
                r'se_publishDate[^>]*>\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.',
                r'blog_date[^>]*>\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.',
                r'"addDate"\s*:\s*"(\d{4})[.-]\s*(\d{1,2})[.-]\s*(\d{1,2})'):
        m = re.search(pat, page)
        if m and iso(*m.groups()):
            return iso(*m.groups())
    m = re.search(r'"writeDate(?:Timestamp)?"\s*:\s*(1\d{12})', page)   # milliseconds since 1970
    return dt.datetime.fromtimestamp(int(m.group(1)) / 1000, KST).date().isoformat() if m else ""


def parse(url):
    """(source, key, tidy link) for a post link. The key names the photo file, e.g. blog-gnuoyaneel_-224458798404."""
    u = urllib.parse.urlsplit(url)
    host, q = u.netloc.lower().split(":")[0], urllib.parse.parse_qs(u.query)
    parts = [p for p in u.path.split("/") if p]
    if host.endswith("blog.naver.com"):   # blog.naver.com/ID/NO, m.blog.naver.com/ID/NO, PostView.naver?blogId=ID&logNo=NO
        bid, no = (q.get("blogId") or [""])[0], (q.get("logNo") or [""])[0]
        if not (bid and no) and len(parts) >= 2 and parts[1].isdigit():
            bid, no = parts[0], parts[1]
        return ("blog", f"{bid}-{no}", f"https://blog.naver.com/{bid}/{no}") if bid and no else ("blog", "", url)
    if host.endswith("cafe.naver.com"):   # cafe.naver.com/CLUB/NO, .../cafes/CLUB/articles/NO, CLUB?iframe_url=...articleid=NO
        club, no = "", ""
        if "cafes" in parts and "articles" in parts:
            i, j = parts.index("cafes"), parts.index("articles")
            if i + 1 < len(parts) and j + 1 < len(parts):
                club, no = parts[i + 1], parts[j + 1]
        elif len(parts) >= 2 and parts[1].isdigit():
            club, no = parts[0], parts[1]
        else:
            inner = urllib.parse.unquote((q.get("iframe_url") or q.get("iframe_url_utf8") or [""])[0])
            m = re.search(r"articleid=(\d+)", inner, re.I)
            if m and parts:
                club, no = parts[0], m.group(1)
        if not (club and no):
            return "cafe", "", url
        tidy = f"https://cafe.naver.com/{club}/{no}" if not club.isdigit() else url
        return "cafe", f"{club}-{no}", tidy
    if host.endswith("brunch.co.kr") and len(parts) >= 2 and parts[0].startswith("@"):
        return "brunch", f"{parts[0][1:]}-{parts[1]}", f"https://brunch.co.kr/{parts[0]}/{parts[1]}"
    return ("brunch" if host.endswith("brunch.co.kr") else ""), "", url


def blog_rss(bid, no):
    """Title and date of a Naver blog post from the blog's RSS feed (holds its recent posts), or {}."""
    x = text(get(f"https://rss.blog.naver.com/{bid}.xml")[2])
    for item in re.findall(r"<item>(.*?)</item>", x, re.S):
        if f"/{no}" not in item:
            continue
        out = {}
        m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        if m:
            out["title"] = clean(m.group(1))
        m = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
        if m:
            try:
                out["date"] = email.utils.parsedate_to_datetime(m.group(1).strip()).astimezone(KST).date().isoformat()
            except (TypeError, ValueError):
                pass
        return out
    return {}


def cafe_api(club, no):
    """Subject, writer, date and first photo of a public Naver cafe article from the cafe web's JSON, or {}.
    Members-only articles return nothing here; staff type their title in the admin instead."""
    flag = "true" if club.isdigit() else "false"
    for u in (f"https://article.cafe.naver.com/gw/v4/cafes/{club}/articles/{no}?useCafeId={flag}",
              f"https://apis.naver.com/cafe-web/cafe-articleapi/v2.1/cafes/{club}/articles/{no}?useCafeId={flag}"):
        try:
            a = json.loads(get(u, referer=REFERER["cafe"])[2])["result"]["article"]
        except (ValueError, KeyError, TypeError):
            continue
        out = {"title": clean(str(a.get("subject") or "")), "by": clean(str((a.get("writer") or {}).get("nick") or ""))}
        wd = a.get("writeDate")
        if isinstance(wd, (int, float)) and wd > 1e12:
            out["date"] = dt.datetime.fromtimestamp(wd / 1000, KST).date().isoformat()
        m = re.search(r'<img[^>]+src="(https://[^"]+?pstatic\.net/[^"]+)"', str(a.get("contentHtml") or ""))
        if m:
            out["img"] = html.unescape(m.group(1))
        return out
    return {}


def read_post(src, key, url):
    """What the post's own pages say about it: {title, by, date, img}, each "" when not found."""
    pages, extra = [], {}
    if src == "blog" and key:
        bid, no = key.rsplit("-", 1)
        pages.append(get(f"https://blog.naver.com/PostView.naver?blogId={bid}&logNo={no}")[2])
        pages.append(get(f"https://m.blog.naver.com/{bid}/{no}", ua=UA_MOB)[2])
        extra = blog_rss(bid, no)
    elif src == "cafe" and key:
        club, no = key.rsplit("-", 1)
        extra = cafe_api(club, no)
        pages.append(get(url)[2])
    else:
        pages.append(get(url)[2])
    page = "\n".join(text(b) for b in pages)

    title = re.sub(r"\s*:\s*네이버\s*(블로그|카페)\s*$", "", meta(page, "og:title", "twitter:title"))
    if title.lower() in GENERIC_TITLES:
        title = ""
    by = ""
    for n in ("naverblog:nickname", "og:article:author", "article:media_name", "author", "dable:author"):
        v = meta(page, n)
        if v and not v.startswith("http"):
            by = v
            break
    return {
        "title": extra.get("title") or title,
        "by": extra.get("by") or by,
        "date": extra.get("date") or find_date(page),
        "img": extra.get("img") or meta(page, "og:image", "twitter:image"),
    }


def photo_candidates(u):
    """A card-sized copy first, then the link as given. Naver blog photos come in width w773 from postfiles
    (blogthumb has no such size); Naver cafe photos take w800; Brunch's Kakao CDN takes R640x0."""
    s = urllib.parse.urlsplit(u)
    if s.netloc in ("blogthumb.pstatic.net", "postfiles.pstatic.net"):
        return [urllib.parse.urlunsplit(("https", "postfiles.pstatic.net", s.path, "type=w773", "")), u]
    if s.netloc.endswith("pstatic.net"):
        return [urllib.parse.urlunsplit(("https", s.netloc, s.path, "type=w800", "")), u]
    if s.netloc.endswith("kakaocdn.net"):
        return [re.sub(r"/thumb/R\d+x\d+[^/]*/", "/thumb/R640x0.fjpg/", u), u]
    return [u]


def shrink(body):
    """The photo as a JPEG at most 960 px wide (cards are about 400 px), or None without Pillow or on a bad file.
    The workflow installs Pillow; without it the photo is kept as downloaded."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(body)))
        if im.mode != "RGB":   # flatten transparency onto white, not black
            rgba = im.convert("RGBA")
            im = Image.new("RGB", rgba.size, "white")
            im.paste(rgba, mask=rgba.getchannel("A"))
        im.thumbnail((960, 2880))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=82, optimize=True, progressive=True)
        return out.getvalue()
    except Exception as e:
        log(f"    could not resize the photo ({e.__class__.__name__}: {e}), keeping it as downloaded")
        return None


def fetch_photo(u, src):
    """(extension, bytes, url used) of the first candidate that is really an image, or None."""
    for c in dict.fromkeys(photo_candidates(u)):
        _, ctype, body = get(c, referer=REFERER.get(src))
        kind = ctype.split(";")[0].strip().lower()
        if kind.startswith("image/") and len(body) >= 2000:
            small = shrink(body)
            if small:
                return "jpg", small, c
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(kind, "jpg")
            return ext, body, c
    return None


def main():
    check = "--check" in sys.argv
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else ""
    raw = open(DATA, encoding="utf-8").read()
    data = json.loads(raw)
    posts = data.get("posts") or []
    today = dt.datetime.now(KST).date().isoformat()
    seen = {}
    log(f"{'Checking' if check else 'Filling'} {len(posts)} posts{f' (only links with {only!r})' if only else ''}")

    for i, p in enumerate(posts, 1):
        url = str(p.get("url") or "").strip()
        if not url or (only and only not in url):
            continue
        if urllib.parse.urlsplit(url).netloc.lower() == "naver.me":   # short links: follow to the real post
            url = get(url)[0]
        src, key, tidy = parse(url)
        if tidy in seen:
            log(f"#{i} is the same post as #{seen[tidy]}: {tidy}")
        seen.setdefault(tidy, i)
        photo_name = re.sub(r"[^A-Za-z0-9_-]", "", f"{src or 'post'}-{key or hashlib.sha1(tidy.encode()).hexdigest()[:12]}")
        empty = lambda k: not str(p.get(k) or "").strip()
        log(f"#{i} {tidy}")

        if check:
            found = read_post(src, key, tidy)
            for k in ("title", "by", "date"):
                stored = str(p.get(k) or "")
                mark = "same" if stored == found[k] else ("NOT FOUND" if not found[k] else "differs")
                log(f"    {k:5} {mark:9} stored: {stored[:60]!r}  found: {found[k][:60]!r}")
            img = str(p.get("img") or "")
            if img and not img.startswith("http"):
                log(f"    photo already in the site: {img}")
            else:
                pic = fetch_photo(img or found["img"], src) if (img or found["img"]) else None
                log("    photo " + (f"ok: {pic[0]}, {len(pic[1]) // 1024} KB from {pic[2][:90]}" if pic else "NOT DOWNLOADABLE"))
            continue

        if src and empty("src"):
            p["src"] = src
        if tidy != p["url"]:
            p["url"] = tidy
        found = {}
        if empty("title") or empty("date") or empty("img"):
            found = read_post(src, key, tidy)
            for k in ("title", "by", "date"):
                if empty(k) and found.get(k):
                    p[k] = found[k]
                    log(f"    {k}: {found[k]}")
            if empty("date"):
                p["date"] = today
                log(f"    date: not on the page, used today ({today})")
            if empty("title"):
                log("    title: NOT FOUND (members-only or removed?). Type it in the admin; the card stays hidden until then.")
        img = str(p.get("img") or "").strip()
        if not img and found.get("img"):
            img = p["img"] = found["img"]
        if img.startswith("http"):
            pic = fetch_photo(img, src)
            if pic:
                ext, body, used = pic
                os.makedirs(IMG_DIR, exist_ok=True)
                name = f"{photo_name}.{ext}"
                with open(os.path.join(IMG_DIR, name), "wb") as f:
                    f.write(body)
                p["img"] = f"img/posts/{name}"
                log(f"    photo: img/posts/{name} ({len(body) // 1024} KB)")
            else:
                log("    photo: could not download, keeping the link to the original")

    if not check:
        data["posts"] = [{**{k: p[k] for k in ORDER if k in p}, **{k: v for k, v in p.items() if k not in ORDER}}
                         for p in posts]
        out = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if out != raw:
            with open(DATA, "w", encoding="utf-8") as f:
                f.write(out)
            log("data/posts.json updated")
        else:
            log("Nothing to change")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:   # shown on the run's page in the Actions tab
        with open(summary, "a", encoding="utf-8") as f:
            f.write("```\n" + "\n".join(report) + "\n```\n")


if __name__ == "__main__":
    main()
