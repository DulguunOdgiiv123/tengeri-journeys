#!/usr/bin/env python3
"""Check the Golden Jigu Naver cafe for changes the website hasn't picked up yet.

The tour operator publishes every tour on cafe.naver.com/goldenjigu: a cover photo, itinerary sheets and a price card,
all as images. The site keeps its own typed copy in data/tours.json, so when a post changes on Naver the copy goes stale.
This script compares the cafe with the site and with what it saw last time (data/naver-snapshot.json), then:

  fixes by itself   - a tour whose post was deleted and re-posted under a new number (same title): its link and cover
                    - a tour whose cover photo changed: the cover in img/cafe/
  lists for staff   - itinerary or price images that changed: retype them in the admin, then tick "done"
    (data/naver.json, - a new tour post the site doesn't have (its cover is saved to img/cafe/ ready to use)
     shown in the   - a tour whose post is gone from Naver
     admin)         - a new notice (공지사항), e.g. a promotion

Staff open "Naver шалгалт" in the admin. Ticking "Одоо шалгах" and saving runs this right away (GitHub Action
naver-check.yml); it also runs every morning. Ticking "Хийж дууссан" on an item and saving tells the next run the site
is up to date for it. No AI: the images are not read, only noticed.

  python3 .github/scripts/naver_check.py              check and update data/naver.json (+ fixes)
  python3 .github/scripts/naver_check.py --dry-run    print what it would do, change nothing
  python3 .github/scripts/naver_check.py --bootstrap  record the cafe as it is now as "in sync" (first run only)
"""
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATUS = os.path.join(ROOT, "data", "naver.json")             # edited in the admin
SNAP = os.path.join(ROOT, "data", "naver-snapshot.json")      # machine-only memory of what the site matches
TOURS = os.path.join(ROOT, "data", "tours.json")
COVERS = "img/cafe"

CLUB, CAFE = 31370398, "goldenjigu"
TOUR_MENUS = {2: "Говь", 3: "Төв бүс", 4: "Өвөл", 5: "Хөвсгөл"}
NOTICE_MENU = 9
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
UB = dt.timezone(dt.timedelta(hours=8))
DRY = "--dry-run" in sys.argv
BOOT = "--bootstrap" in sys.argv


# ---------- Naver ----------
def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": f"https://cafe.naver.com/{CAFE}"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 + 3 * attempt)


def get_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})   # no Referer: pstatic refuses other sites
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def list_menu(menu):
    url = (f"https://apis.naver.com/cafe-web/cafe2/ArticleListV2dot1.json?search.clubid={CLUB}&search.menuid={menu}"
           "&search.queryType=lastArticle&search.page=1&search.perPage=50")
    return [{"id": a["articleId"], "subject": html.unescape(a["subject"]).strip()}
            for a in get_json(url)["message"]["result"]["articleList"]]


def read_article(aid):
    a = get_json(f"https://apis.naver.com/cafe-web/cafe-articleapi/v2.1/cafes/{CLUB}/articles/{aid}?useCafeId=true")
    a = a["result"]["article"]
    c = a["contentHtml"]
    images = []
    for m in re.finditer(r"data-linkdata='([^']+)'", c):
        try:
            d = json.loads(html.unescape(m.group(1)))
        except ValueError:
            continue
        if "src" in d and "pstatic.net" in d["src"]:
            images.append({"src": d["src"].split("?")[0],
                           "w": int(d.get("originalWidth") or 0), "h": int(d.get("originalHeight") or 0)})
    if not images:   # older editor: plain <img> tags
        images = [{"src": html.unescape(u).split("?")[0], "w": 0, "h": 0}
                  for u in re.findall(r'<img[^>]+src="([^"]+pstatic\.net[^"]+)"', c)]
    text = re.sub(r"<[^>]+>", " ", c)
    text = re.sub(r"\s+", " ", html.unescape(text).replace("​", "")).strip()
    return {"id": aid, "subject": html.unescape(a["subject"]).strip(), "images": images,
            "text": hashlib.sha1(text.encode()).hexdigest()[:12]}


def fingerprint(art):
    return {"subject": art["subject"], "images": [i["src"] for i in art["images"]], "text": art["text"]}


def kind_of(img, i):
    """Name an image for staff from its shape: the sheets are portrait A4, the price cards 4:5."""
    if i == 0:
        return "нүүр зураг"
    if img["w"] and img["h"]:
        r = img["h"] / img["w"]
        if 1.35 <= r <= 1.48:
            return "хөтөлбөрийн зураг"
        if 1.18 <= r < 1.35:
            return "үнийн зураг"
    return "зураг"


def save_cover(art, name):
    """Download the post's first image as img/cafe/<name>.jpg, cropped to a landscape card that keeps the title."""
    if not art["images"]:
        return None
    from PIL import Image
    body = None
    for q in ("?type=w1600", "?type=w2000", ""):
        try:
            body = get_bytes(art["images"][0]["src"] + q)
            break
        except Exception:
            continue
    if body is None:
        return None
    im = Image.open(io.BytesIO(body)).convert("RGB")
    if im.width > 1600:
        im = im.resize((1600, round(im.height * 1600 / im.width)), Image.LANCZOS)
    if im.height / im.width > 0.75:   # portrait cover: keep the middle band, where the cafe puts the title
        h = round(im.width * 0.695)
        y = (im.height - h) // 2
        im = im.crop((0, y, im.width, y + h))
    rel = f"{COVERS}/{name}.jpg"
    if not DRY:
        os.makedirs(os.path.join(ROOT, COVERS), exist_ok=True)
        im.save(os.path.join(ROOT, rel), "JPEG", quality=86, optimize=True, progressive=True)
    return rel


# ---------- files ----------
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def dump(path, data):
    if DRY:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def norm(s):
    return re.sub(r"\s+", "", s or "")


def article_of(tour):
    m = re.search(r"cafe\.naver\.com/" + CAFE + r"/(\d+)", tour.get("cafe") or "")
    return int(m.group(1)) if m else None


# ---------- main ----------
def main():
    now = dt.datetime.now(UB)
    today = now.strftime("%Y-%m-%d")
    status = load(STATUS, {})
    snap = load(SNAP, {"articles": {}, "notices": [], "dismissed": []})
    tours_doc = load(TOURS, {"tours": []})
    tours = tours_doc["tours"]
    old_snap = json.dumps(snap, ensure_ascii=False, sort_keys=True)
    old_tours = json.dumps(tours_doc, ensure_ascii=False, sort_keys=True)
    log = list(status.get("log") or [])
    todo_in = {t.get("key"): t for t in (status.get("todo") or []) if t.get("key")}
    covers_written = []

    def note(line):
        log.insert(0, f"{today} · {line}")
        print("fixed:", line)

    # 1. The cafe as it is now
    current, menu_of = {}, {}
    for menu, region in TOUR_MENUS.items():
        for a in list_menu(menu):
            current[a["id"]] = read_article(a["id"])
            menu_of[a["id"]] = region
            time.sleep(0.3)
    notices = list_menu(NOTICE_MENU)
    if not current:
        sys.exit("The cafe returned no tour posts: not touching anything (Naver down or blocking?)")

    arts = snap["articles"]
    if BOOT:
        for aid, art in current.items():
            arts[str(aid)] = fingerprint(art)
        snap["notices"] = sorted({n["id"] for n in notices})
        print(f"bootstrap: {len(current)} posts and {len(notices)} notices recorded as in sync")

    # 2. Items staff ticked as done since last time
    dismissed = set(snap.get("dismissed") or [])
    for key, t in todo_in.items():
        if not t.get("done"):
            continue
        kind, _, ref = key.partition("-")
        if kind == "changed" and ref.isdigit() and int(ref) in current:
            arts[ref] = fingerprint(current[int(ref)])          # the site now matches this version
        elif kind == "notice" and ref.isdigit():
            snap["notices"] = sorted(set(snap["notices"]) | {int(ref)})
        else:                                                   # new / gone: staff decided to leave it
            dismissed.add(key)
        print("done:", key)

    # 3. Which tour shows which post
    by_article = {}
    for i, t in enumerate(tours):
        aid = article_of(t)
        if aid is not None:
            by_article.setdefault(aid, []).append(i)

    todo = {}

    # 4. Tours whose post is gone: re-posted under the same title -> relink, else ask staff
    for aid in sorted(by_article):
        if aid in current:
            continue
        old = arts.get(str(aid))
        match = None
        if old:
            cands = [n for n, a in current.items() if norm(a["subject"]) == norm(old["subject"]) and n not in by_article]
            match = cands[0] if len(cands) == 1 else None
        for i in by_article[aid]:
            t = tours[i]
            if match:
                t["cafe"] = f"https://cafe.naver.com/{CAFE}/{match}"
                if (t.get("img") or "").lstrip("/") == f"{COVERS}/{aid}.jpg" and (rel := save_cover(current[match], match)):
                    t["img"] = rel
                    covers_written.append(rel)
                note(f"{t.get('name')}: Naver дээр дахин нийтэлсэн тул холбоосыг {aid} → {match} болгож сольсон")
            elif f"gone-{aid}" not in dismissed:
                todo[f"gone-{aid}"] = {
                    "title": (old or {}).get("subject") or f"Нийтлэл {aid}", "tour": t.get("name", ""),
                    "link": f"https://cafe.naver.com/{CAFE}/{aid}",
                    "what": "Энэ аяллын Naver нийтлэл устсан тул сайт дээрх холбоос ажиллахгүй байна. "
                            "Naver дээр шинэ нийтлэл байвал «Аялалууд» хэсэгт холбоосыг нь сольно уу, "
                            "үгүй бол аяллыг сайтаас хасна уу. Аль нэгийг хийхэд энэ мөр өөрөө арилна."}
        if match:
            arts[str(match)] = old            # compare the new post with what the site had
            arts.pop(str(aid), None)
            by_article[match] = by_article.pop(aid)

    # 5. Tours whose post changed
    for aid, idx in sorted(by_article.items()):
        if aid not in current:
            continue
        art, key = current[aid], str(aid)
        old = arts.get(key)
        if old is None:                       # first time this post is linked: staff typed it from what's there now
            arts[key] = fingerprint(art)
            continue
        new_imgs, old_imgs = [i["src"] for i in art["images"]], old["images"]
        cover_changed = bool(new_imgs) and (not old_imgs or new_imgs[0] != old_imgs[0])
        if cover_changed:
            for i in idx:
                t = tours[i]
                if (t.get("img") or "").lstrip("/") == f"{COVERS}/{aid}.jpg":
                    if (rel := save_cover(art, aid)) and rel not in covers_written:
                        covers_written.append(rel)
                        note(f"{t.get('name')}: нүүр зургийг Naver дээрхээр шинэчилсэн")
            old_imgs = new_imgs[:1] + old_imgs[1:]
            old["images"] = old_imgs
        changed = [kind_of(img, n) + f" ({n + 1})" for n, img in enumerate(art["images"])
                   if n > 0 and img["src"] not in old_imgs]
        removed = len([s for s in old_imgs[1:] if s not in new_imgs])
        parts = []
        if changed:
            parts.append("шинэ/солигдсон: " + ", ".join(changed))
        if removed:
            parts.append(f"{removed} зураг хасагдсан")
        if art["text"] != old["text"]:
            parts.append("тайлбар бичвэр өөрчлөгдсөн")
        if norm(art["subject"]) != norm(old["subject"]):
            parts.append(f"гарчиг «{old['subject']}» → «{art['subject']}»")
        if parts:
            todo[f"changed-{aid}"] = {
                "title": art["subject"], "tour": ", ".join(tours[i].get("name", "") for i in idx),
                "link": f"https://cafe.naver.com/{CAFE}/{aid}",
                "what": "Naver дээр өөрчлөгдсөн: " + "; ".join(parts) + ". Нийтлэлийг нээж, «Аялалууд» хэсэгт энэ аяллын "
                        "хөтөлбөр, «Үнэ (хүний тоогоор)»-г шинэчлээд «Хийж дууссан»-ыг чагтлаад хадгална уу."}

    # 6. Posts the site doesn't show
    for aid, art in sorted(current.items()):
        key = f"new-{aid}"
        if aid in by_article or key in dismissed:
            continue
        rel = f"{COVERS}/{aid}.jpg"
        if not os.path.exists(os.path.join(ROOT, rel)) and save_cover(art, aid):
            covers_written.append(rel)
        todo[key] = {
            "title": art["subject"], "tour": "", "link": f"https://cafe.naver.com/{CAFE}/{aid}",
            "what": f"Naver дээр шинэ аялал ({menu_of[aid]}), сайтад байхгүй. «Аялалууд» хэсэгт шинэ аялал нэмж, "
                    f"Naver cafe холбоос талбарт https://cafe.naver.com/{CAFE}/{aid} гэж бичнэ үү. Нүүр зураг бэлэн: "
                    f"{rel}. Холбоос орсны дараа энэ мөр өөрөө арилна. Сайтад нэмэхгүй бол «Хийж дууссан»-ыг чагтална."}

    # 7. New notices (promotions etc.)
    known = set(snap.get("notices") or [])
    for n in notices:
        if n["id"] not in known:
            todo[f"notice-{n['id']}"] = {
                "title": n["subject"], "tour": "", "link": f"https://cafe.naver.com/{CAFE}/{n['id']}",
                "what": "Naver-т шинэ мэдэгдэл (공지사항) нийтлэгдсэн. Урамшуулал, үнэ, дүрэм зэрэг сайтад хэрэгтэй "
                        "мэдээлэл байвал тусгаад «Хийж дууссан»-ыг чагтална уу."}

    # 8. Write
    order = {"changed": 0, "new": 1, "gone": 2, "notice": 3}
    items = []
    for key in sorted(todo, key=lambda k: (order[k.split("-")[0]], -int(k.split("-")[1]))):
        items.append({"key": key, "kind": key.split("-")[0], **todo[key], "done": False})   # ticked ones were handled in step 2
    snap["dismissed"] = sorted(dismissed)
    new_status = {
        "check_now": False,
        "checked_at": status.get("checked_at", ""),
        "summary": (f"{len(items)} зүйл шалгах шаардлагатай." if items else "Бүх аялал Naver дээрхтэй таарч байна."),
        "todo": items,
        "log": log[:30],
    }
    content_changed = (json.dumps({**new_status, "checked_at": ""}, ensure_ascii=False, sort_keys=True)
                       != json.dumps({**status, "checked_at": "", "check_now": False}, ensure_ascii=False, sort_keys=True))
    snap_changed = json.dumps(snap, ensure_ascii=False, sort_keys=True) != old_snap
    tours_changed = json.dumps(tours_doc, ensure_ascii=False, sort_keys=True) != old_tours
    asked = bool(status.get("check_now"))
    if asked or content_changed or snap_changed or tours_changed or BOOT or not os.path.exists(STATUS):
        new_status["checked_at"] = now.strftime("%Y-%m-%d %H:%M") + " (УБ цаг)"
        dump(STATUS, new_status)
        dump(SNAP, snap)
        if tours_changed:
            dump(TOURS, tours_doc)
    for t in items:
        print(f"todo: [{t['kind']}] {t['title']}  {t['link']}")
    print(new_status["summary"], "(dry run, nothing written)" if DRY else "")


if __name__ == "__main__":
    main()
