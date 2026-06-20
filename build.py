#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Setouchi Factory View — 静的サイトビルダー
=====================================================
NotionをCMS（管理画面）として使い、公式Notion APIから内容を取得して
「このデザインのまま」の複数ページHTMLを public/ に生成します。

GitHub Actions から実行され、生成物（public/）を GitHub Pages へデプロイします。

必要な環境変数（GitHub Secrets / .env）:
  NOTION_TOKEN     : Notionインテグレーションのシークレット (ntn_...)
  NEWS_DB_ID       : 「お知らせ」データベースID
  PAGES_DB_ID      : 「イベント紹介ページ」データベースID
  SCHEDULE_DB_ID   : 「プログラム一覧（スケジュール）」データベースID
  EXHIBITORS_DB_ID : （任意）「出展企業一覧」データベースID。未設定の場合はタイトル検索で自動解決。

使い方:  python3 build.py
"""
import os, re, sys, json, html, hashlib, mimetypes, pathlib, urllib.parse, urllib.request

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
ROOT = pathlib.Path(__file__).resolve().parent
TPL  = ROOT / "templates" / "index.html"   # 現行トップページ（デザイン・ロゴ埋め込み済み）
OUT  = ROOT / "public"                      # 出力先（GitHub Pages 公開ディレクトリ）
IMG  = OUT / "assets" / "img"               # ダウンロード画像の保存先

def clean_id(raw):
    """貼り付けられた値（URL/タイトル付き/ハイフン有無）から32桁のNotion IDだけを取り出す。"""
    raw = (raw or "").strip().split("?")[0]
    compact = raw.replace("-", "")
    m = re.findall(r"[0-9a-fA-F]{32}", compact)
    return m[-1] if m else raw

NOTION_TOKEN   = os.environ.get("NOTION_TOKEN", "")
NEWS_DB_ID     = os.environ.get("NEWS_DB_ID", "")
PAGES_DB_ID    = os.environ.get("PAGES_DB_ID", "")
SCHEDULE_DB_ID = os.environ.get("SCHEDULE_DB_ID", "")
EXHIBITORS_DB_ID = os.environ.get("EXHIBITORS_DB_ID", "")
NEWS_DB_ID     = clean_id(NEWS_DB_ID)
PAGES_DB_ID    = clean_id(PAGES_DB_ID)
SCHEDULE_DB_ID = clean_id(SCHEDULE_DB_ID)
EXHIBITORS_DB_ID = clean_id(EXHIBITORS_DB_ID)
NOTION_VERSION = "2025-09-03"
API = "https://api.notion.com/v1"

# Notion プロパティ名（管理パネルのスキーマと一致させる）
P = {
    "news_title": "タイトル",   "news_date": "公開日",   "news_cat": "カテゴリ",
    "news_summary": "本文（概要）", "news_cover": "カバー画像", "news_pub": "公開",
    "pg_title": "ページタイトル", "pg_slug": "スラッグ（URL用）", "pg_cover": "カバー写真",
    "pg_gallery": "ギャラリー写真", "pg_summary": "概要", "pg_area": "エリア",
    "pg_order": "並び順", "pg_pub": "公開",
    "sc_title": "プログラム", "sc_date": "日程", "sc_cat": "カテゴリ",
    "sc_area": "エリア", "sc_venue": "会場", "sc_reserve": "要予約", "sc_fee": "料金",
    "ex_name": "会社名", "ex_area": "エリア", "ex_city": "市町村", "ex_industry": "業種",
    "ex_size": "会社規模", "ex_join": "参加形態", "ex_intro": "会社紹介文",
    "ex_address": "住所", "ex_image": "画像", "ex_pub": "公開",
}

# ---------------------------------------------------------------------------
# Notion API ヘルパー
# ---------------------------------------------------------------------------
def _req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {NOTION_TOKEN}")
    req.add_header("Notion-Version", NOTION_VERSION)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        print("[Notion API error] %s %s %s\n%s" % (e.code, method, url, detail), file=sys.stderr)
        raise

_DS_CACHE = {}
def query_url(db_id):
    """新API（データソース）対応のクエリURLを返す。旧仕様DBにもフォールバック。"""
    if db_id not in _DS_CACHE:
        ds_id = None
        try:
            d = _req("GET", f"{API}/databases/{db_id}")
            dss = d.get("data_sources") or []
            if dss:
                ds_id = dss[0].get("id")
        except Exception as e:
            print("[warn] resolve data source failed for %s: %s" % (db_id, e), file=sys.stderr)
        _DS_CACHE[db_id] = ds_id
    ds_id = _DS_CACHE[db_id]
    if ds_id:
        return f"{API}/data_sources/{ds_id}/query"
    return f"{API}/databases/{db_id}/query"

def _search_query_url(title_kw):
    """インテグレーションに共有されたDB/データソースをタイトルで探し、クエリURLを返す。"""
    try:
        d = _req("POST", f"{API}/search", {"query": title_kw})
    except Exception as e:
        print("[warn] search failed for %s: %s" % (title_kw, e), file=sys.stderr)
        return ""
    for r in d.get("results", []):
        obj = r.get("object")
        t = r.get("title")
        if isinstance(t, list):
            name = "".join(x.get("plain_text", "") for x in t)
        elif isinstance(r.get("name"), str):
            name = r.get("name")
        else:
            name = ""
        if title_kw not in name:
            continue
        if obj == "data_source":
            return f"{API}/data_sources/{r['id']}/query"
        if obj == "database":
            return query_url(r["id"])
    return ""

def query_paginate(qurl):
    """クエリURLをページネーションしながら全件取得。"""
    results, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor: body["start_cursor"] = cursor
        d = _req("POST", qurl, body)
        results += d.get("results", [])
        if d.get("has_more"):
            cursor = d.get("next_cursor")
        else:
            break
    return results

def query_db(db_id, sorts=None):
    """データベースの全ページを取得（ページネーション対応）。"""
    results, cursor = [], None
    while True:
        body = {"page_size": 100}
        if sorts: body["sorts"] = sorts
        if cursor: body["start_cursor"] = cursor
        d = _req("POST", query_url(db_id), body)
        results += d.get("results", [])
        if d.get("has_more"):
            cursor = d.get("next_cursor")
        else:
            break
    return results

def get_blocks(block_id):
    """ブロックの子要素を取得（ページネーション対応）。"""
    results, cursor = [], None
    while True:
        u = f"{API}/blocks/{block_id}/children?page_size=100"
        if cursor: u += "&start_cursor=" + urllib.parse.quote(cursor)
        d = _req("GET", u)
        results += d.get("results", [])
        if d.get("has_more"):
            cursor = d.get("next_cursor")
        else:
            break
    return results

# ---------------------------------------------------------------------------
# プロパティ抽出ヘルパー
# ---------------------------------------------------------------------------
def props(page): return page.get("properties", {})

def p_title(pr, name):
    v = pr.get(name, {}).get("title", [])
    return "".join(t.get("plain_text", "") for t in v).strip()

def p_text(pr, name):
    v = pr.get(name, {}).get("rich_text", [])
    return "".join(t.get("plain_text", "") for t in v).strip()

def p_select(pr, name):
    s = pr.get(name, {}).get("select")
    return s.get("name") if s else None

def p_multi(pr, name):
    return [o.get("name", "") for o in pr.get(name, {}).get("multi_select", []) if o.get("name")]

def p_check(pr, name):
    return bool(pr.get(name, {}).get("checkbox"))

def p_number(pr, name):
    return pr.get(name, {}).get("number")

def p_date(pr, name):
    d = pr.get(name, {}).get("date")
    if not d: return (None, None)
    return (d.get("start"), d.get("end"))

def p_files(pr, name):
    out = []
    for f in pr.get(name, {}).get("files", []):
        if f.get("type") == "file":
            out.append(f["file"]["url"])
        elif f.get("type") == "external":
            out.append(f["external"]["url"])
    return out

# ---------------------------------------------------------------------------
# 画像ダウンロード（Notionの画像URLは一時的なのでビルド時に保存）
# ---------------------------------------------------------------------------
def download_image(url, prefix="img"):
    if not url: return ""
    base = url.split("?")[0]
    ext = os.path.splitext(base)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"):
        ext = ".jpg"
    h = hashlib.sha1(base.encode()).hexdigest()[:16]
    fname = f"{prefix}-{h}{ext}"
    dest = IMG / fname
    if not dest.exists():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req) as r, open(dest, "wb") as fp:
                fp.write(r.read())
        except Exception as e:
            print(f"  ! 画像取得失敗: {url[:60]} ({e})", file=sys.stderr)
            return ""
    return f"assets/img/{fname}"

# ---------------------------------------------------------------------------
# リッチテキスト / ブロック → HTML
# ---------------------------------------------------------------------------
def rich_to_html(rich):
    out = []
    for t in rich:
        txt = html.escape(t.get("plain_text", ""))
        a = t.get("annotations", {})
        if a.get("code"):          txt = f"<code>{txt}</code>"
        if a.get("bold"):          txt = f"<strong>{txt}</strong>"
        if a.get("italic"):        txt = f"<em>{txt}</em>"
        if a.get("strikethrough"): txt = f"<s>{txt}</s>"
        if a.get("underline"):     txt = f"<u>{txt}</u>"
        href = t.get("href")
        if href: txt = f'<a href="{html.escape(href)}" target="_blank" rel="noopener">{txt}</a>'
        out.append(txt)
    return "".join(out)

def blocks_to_html(blocks, depth="page"):
    """代表的なブロック型をHTML化。連続リスト項目は<ul>/<ol>でまとめる。"""
    out, i = [], 0
    while i < len(blocks):
        b = blocks[i]; bt = b.get("type")
        if bt in ("bulleted_list_item", "numbered_list_item"):
            tag = "ul" if bt == "bulleted_list_item" else "ol"
            out.append(f"<{tag}>")
            while i < len(blocks) and blocks[i].get("type") == bt:
                out.append("<li>" + rich_to_html(blocks[i][bt].get("rich_text", [])) + "</li>")
                i += 1
            out.append(f"</{tag}>")
            continue
        if bt == "paragraph":
            txt = rich_to_html(b[bt].get("rich_text", []))
            out.append(f"<p>{txt}</p>" if txt else "<p>&nbsp;</p>")
        elif bt in ("heading_1", "heading_2", "heading_3"):
            hn = {"heading_1": "h2", "heading_2": "h3", "heading_3": "h4"}[bt]
            out.append(f"<{hn}>{rich_to_html(b[bt].get('rich_text', []))}</{hn}>")
        elif bt == "to_do":
            chk = "checked" if b[bt].get("checked") else ""
            out.append(f'<label class="todo"><input type="checkbox" disabled {chk}> {rich_to_html(b[bt].get("rich_text", []))}</label>')
        elif bt == "quote":
            out.append(f"<blockquote>{rich_to_html(b[bt].get('rich_text', []))}</blockquote>")
        elif bt == "callout":
            icon = b[bt].get("icon", {})
            emoji = icon.get("emoji", "") if icon else ""
            out.append(f'<div class="callout"><span class="callout-ic">{html.escape(emoji)}</span><div>{rich_to_html(b[bt].get("rich_text", []))}</div></div>')
        elif bt == "divider":
            out.append("<hr>")
        elif bt == "image":
            img = b[bt]
            url = img.get("file", {}).get("url") or img.get("external", {}).get("url", "")
            rel = download_image(url, "body")
            cap = rich_to_html(img.get("caption", []))
            prefix = "../" if depth == "sub" else ""
            if rel:
                out.append(f'<figure><img src="{prefix}{rel}" alt="{html.escape(cap)}" loading="lazy">' + (f"<figcaption>{cap}</figcaption>" if cap else "") + "</figure>")
        i += 1
    return "\n".join(out)

# ---------------------------------------------------------------------------
# テンプレート（トップのheahd/CSSとnav/footerを再利用してサブページを生成）
# ---------------------------------------------------------------------------
def extract(tpl, start, end):
    s = tpl.find(start); e = tpl.find(end, s)
    return tpl[s:e+len(end)] if s != -1 and e != -1 else ""

def build_subpage_legacy(tpl_html, head_html, title, body_html):
    """（旧）サブページ生成。現在は下部の正規定義を使用。"""
    nav = extract(tpl_html, '<nav class="sfv-nav">', "</nav>")
    foot = extract(tpl_html, "<footer", "</footer>")
    nav = nav.replace('href="#', 'href="../index.html#')
    nav = nav.replace('href="../index.html#top"', 'href="../index.html"')
    extra = ('<a href="../news/">News</a>'
             '<a href="../factories/">紹介ページ</a>')
    nav = nav.replace('<a class="sfv-navcta"', extra + '<a class="sfv-navcta"')
    foot = foot.replace('href="#', 'href="../index.html#')
    return ""

# ---------------------------------------------------------------------------
# スケジュール → トップページの PROGRAMS を置換
# ---------------------------------------------------------------------------
CAT_ORDER = ["工場見学", "体験・ワークショップ", "トーク", "特別", "ショップ"]

def build_index(tpl_html):
    rows = query_db(SCHEDULE_DB_ID, sorts=[{"property": P["sc_date"], "direction": "ascending"}])
    programs = []
    for pg in rows:
        pr = props(pg)
        s, e = p_date(pr, P["sc_date"])
        if not s: continue
        programs.append({
            "s": s, "e": e,
            "t": p_title(pr, P["sc_title"]),
            "cat": p_select(pr, P["sc_cat"]),
            "area": p_select(pr, P["sc_area"]),
            "venue": p_select(pr, P["sc_venue"]),
            "reserve": p_check(pr, P["sc_reserve"]),
            "fee": p_select(pr, P["sc_fee"]),
        })
    js = "const PROGRAMS=" + json.dumps(programs, ensure_ascii=False) + ";"
    html_out = re.sub(r"const PROGRAMS=\[.*?\];", lambda m: js, tpl_html, count=1, flags=re.S)
    # トップのナビに News / 出展企業 / 紹介ページ へのリンクを追加
    html_out = html_out.replace(
        '<a class="sfv-navcta"',
        '<a href="#news">News</a><a href="companies/">出展企業</a><a href="factories/">紹介ページ</a><a class="sfv-navcta"', 1)
    # === Notio: トップページに「お知らせ」ブロックを追加（個別ページへリンク） ===
    news_rows = query_db(NEWS_DB_ID, sorts=[{"property": P["news_date"], "direction": "descending"}])
    news_pub = [p for p in news_rows if p_check(props(p), P["news_pub"])]
    _items = []
    for pg in news_pub[:5]:
        pr = props(pg); pid = pg["id"].replace("-", "")
        ntitle = html.escape(p_title(pr, P["news_title"]))
        ncat = html.escape(p_select(pr, P["news_cat"]) or "")
        ndate = (p_date(pr, P["news_date"])[0] or "").replace("-", ".")
        _cat = f'<span class="nx-c">{ncat}</span>' if ncat else ''
        _items.append(
            f'<li><a href="news/{pid}.html">'
            f'<span class="nx-d">{ndate}</span>{_cat}'
            f'<span class="nx-t">{ntitle}</span></a></li>')
    _list = "".join(_items) if _items else '<li class="nx-empty">現在お知らせはありません。</li>'
    _news_sec = (
        '<aside class="nx-news" id="news"><div class="nx-news-wrap">'
        '<div class="nx-news-head"><span class="k">News</span><span class="t">お知らせ</span>'
        '<a class="nx-news-all" href="news/">一覧 →</a></div>'
        f'<ul class="nx-news-list">{_list}</ul>'
        '</div></aside>')
    html_out = html_out.replace(
        '<section class="sfv-section" id="concept">',
        _news_sec + '<section class="sfv-section" id="concept">', 1)
    _news_css = '<style>.nx-news{border-bottom:1px solid var(--sfv-line,rgba(0,0,0,.08));background:var(--sfv-card,#fff);}.nx-news-wrap{max-width:1120px;margin:0 auto;padding:20px clamp(16px,5vw,48px);}.nx-news-head{display:flex;align-items:baseline;gap:12px;margin-bottom:12px;}.nx-news-head .k{font-size:12px;letter-spacing:.12em;color:var(--sfv-blue,#3fa0d6);font-weight:700;text-transform:uppercase;}.nx-news-head .t{font-size:14px;font-weight:700;}.nx-news-all{margin-left:auto;font-size:13px;color:var(--sfv-blue,#3fa0d6);text-decoration:none;font-weight:600;}.nx-news-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;}.nx-news-list li{margin:0;border-top:1px solid var(--sfv-line,rgba(0,0,0,.06));}.nx-news-list li:first-child{border-top:0;}.nx-news-list a{display:flex;align-items:baseline;gap:12px;text-decoration:none;color:inherit;padding:8px 0;font-size:14px;line-height:1.6;}.nx-news-list a:hover .nx-t{color:var(--sfv-blue,#3fa0d6);text-decoration:underline;}.nx-d{flex:0 0 auto;color:var(--sfv-ink-soft,#6b6b66);font-size:13px;font-variant-numeric:tabular-nums;min-width:84px;}.nx-c{flex:0 0 auto;font-size:11px;color:var(--sfv-blue-deep,#2f8fc6);border:1px solid var(--sfv-line,rgba(0,0,0,.14));border-radius:999px;padding:1px 9px;}.nx-t{flex:1 1 auto;}.nx-empty{color:var(--sfv-ink-soft,#6b6b66);font-size:14px;padding:8px 0;}@media(max-width:640px){.nx-d{min-width:0;}.nx-news-list a{flex-wrap:wrap;gap:4px 10px;}.nx-t{flex-basis:100%;}}</style>'
    html_out = html_out.replace("</head>", _news_css + "</head>", 1)
    # === Notio auto-fix: モバイルでイベント名表示 + 初期表示を週に ===
    _ov_css = '<style>@media(max-width:720px){.ev-bar{font-size:10px!important;height:18px!important;line-height:18px!important;padding:0 6px!important;border-radius:4px!important;box-shadow:none!important;overflow:hidden!important;text-overflow:ellipsis!important;white-space:nowrap!important;}.ev-wk-day{min-height:104px!important;}}</style>'
    html_out = html_out.replace("</head>", _ov_css + "</head>", 1)
    # === Notio: ライトテーマ固定（スマホのダークモードで背景が黒くなるのを防止） ===
    _theme_css = '<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light"><style>:root{color-scheme:light only}html,body{background-color:#fff!important}</style>'
    html_out = html_out.replace("</head>", _theme_css + "</head>", 1)
    _ov_js = '<script>document.addEventListener("DOMContentLoaded",function(){try{var PS=new Date(2026,10,PERIOD_START),PE=new Date(2026,10,PERIOD_END);var t=new Date();var ws=weekStart(t);var we=new Date(ws.getFullYear(),ws.getMonth(),ws.getDate()+6);var inP=!(we<PS||ws>PE);state.view="week";state.ref=inP?t:PS;var vm=document.getElementById("viewMonth"),vw=document.getElementById("viewWeek");if(vm)vm.classList.remove("on");if(vw)vw.classList.add("on");render();}catch(e){}});</script>'
    html_out = html_out.replace("</body>", _ov_js + "</body>", 1)
    print(f"  スケジュール: {len(programs)} 件")
    return html_out

# ---------------------------------------------------------------------------
# お知らせ
# ---------------------------------------------------------------------------
def build_news(tpl_html, head_html):
    rows = query_db(NEWS_DB_ID, sorts=[{"property": P["news_date"], "direction": "descending"}])
    pub = [p for p in rows if p_check(props(p), P["news_pub"])]
    cards = []
    for pg in pub:
        pr = props(pg); pid = pg["id"].replace("-", "")
        title = p_title(pr, P["news_title"])
        cat = p_select(pr, P["news_cat"]) or ""
        date = (p_date(pr, P["news_date"])[0] or "")
        summary = p_text(pr, P["news_summary"])
        covers = p_files(pr, P["news_cover"])
        cover_rel = download_image(covers[0], "newscover") if covers else ""
        body = blocks_to_html(get_blocks(pg["id"]), depth="sub")
        # 個別ページ
        hero = f'<figure class="sfv-hero"><img src="../{cover_rel}" alt="{html.escape(title)}"></figure>' if cover_rel else ""
        art = (f'<a class="sfv-back" href="./">← お知らせ一覧</a>'
               f'<div class="sfv-meta">{html.escape(date)}</div>'
               f'<span class="sfv-tag">{html.escape(cat)}</span>'
               f'<h1>{html.escape(title)}</h1>'
               f'{hero}<div class="sfv-article">{body}</div>')
        write(OUT / "news" / f"{pid}.html", build_subpage(tpl_html, head_html, title, art))
        ph_div = f'<div class="ph" style="background-image:url(../{cover_rel})"></div>' if cover_rel else ""
        cards.append(
            f'<a class="sfv-card" href="{pid}.html">{ph_div}<div class="bd">'
            f'<div class="sfv-meta">{html.escape(date)}</div>'
            f'<span class="sfv-tag">{html.escape(cat)}</span>'
            f'<h3>{html.escape(title)}</h3><p>{html.escape(summary)}</p></div></a>')
    listing = (f'<a class="sfv-back" href="../index.html">← トップへ</a>'
               f'<h1>News</h1><p class="lead">お知らせ・新着情報</p>'
               f'<div class="sfv-grid">{"".join(cards) or "<p>現在お知らせはありません。</p>"}</div>')
    write(OUT / "news" / "index.html", build_subpage(tpl_html, head_html, "News", listing))
    print(f"  お知らせ: {len(pub)} 件公開")

# ---------------------------------------------------------------------------
# 出展企業
# ---------------------------------------------------------------------------
def build_companies(tpl_html, head_html):
    qurl = query_url(EXHIBITORS_DB_ID) if EXHIBITORS_DB_ID else ""
    if not qurl:
        qurl = _search_query_url("出展企業")
    if not qurl:
        print("  出展企業: データソースが見つかりません（インテグレーションへの共有を確認してください）", file=sys.stderr)
        return
    rows = query_paginate(qurl)
    pub = [p for p in rows if p_check(props(p), P["ex_pub"])]
    pub.sort(key=lambda pg: p_title(props(pg), P["ex_name"]))
    cards = []
    for pg in pub:
        pr = props(pg); pid = pg["id"].replace("-", "")
        name = p_title(pr, P["ex_name"])
        area = p_select(pr, P["ex_area"]) or ""
        city = p_select(pr, P["ex_city"]) or ""
        industry = p_text(pr, P["ex_industry"])
        size = p_select(pr, P["ex_size"]) or ""
        joins = p_multi(pr, P["ex_join"])
        intro = p_text(pr, P["ex_intro"])
        address = p_text(pr, P["ex_address"])
        covers = p_files(pr, P["ex_image"])
        cover_rel = download_image(covers[0], "company") if covers else ""
        body = blocks_to_html(get_blocks(pg["id"]), depth="sub")
        tags = []
        if city: tags.append(city)
        if area: tags.append(area)
        tags += joins
        tag_html = "".join(f'<span class="sfv-tag">{html.escape(t)}</span>' for t in tags)
        hero = f'<figure class="sfv-hero"><img src="../{cover_rel}" alt="{html.escape(name)}"></figure>' if cover_rel else ""
        info = []
        if industry: info.append(("業種", industry))
        if size: info.append(("会社規模", size))
        if address: info.append(("住所", address))
        info_html = "".join(f'<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>' for k, v in info)
        info_block = f'<table class="sfv-info">{info_html}</table>' if info else ""
        art = (f'<a class="sfv-back" href="./">← 出展企業一覧</a>'
               f'<div class="sfv-tags">{tag_html}</div>'
               f'<h1>{html.escape(name)}</h1>'
               f'{hero}'
               + (f'<p class="lead">{html.escape(intro)}</p>' if intro else "")
               + info_block
               + (f'<div class="sfv-article">{body}</div>' if body else ""))
        write(OUT / "companies" / f"{pid}.html", build_subpage(tpl_html, head_html, name, art))
        ph_div = f'<div class="ph" style="background-image:url(../{cover_rel})"></div>' if cover_rel else ""
        cards.append(
            f'<a class="sfv-card" href="{pid}.html">{ph_div}<div class="bd">'
            f'<div class="sfv-tags">{tag_html}</div>'
            f'<h3>{html.escape(name)}</h3>'
            + (f'<p class="sfv-ind">{html.escape(industry)}</p>' if industry else "")
            + (f'<p>{html.escape(intro)}</p>' if intro else "")
            + '</div></a>')
    listing = (f'<a class="sfv-back" href="../index.html">← トップへ</a>'
               f'<h1>出展企業</h1><p class="lead">瀬戸内ファクトリービュー2026 出展企業一覧</p>'
               f'<div class="sfv-grid">{"".join(cards) or "<p>現在公開中の出展企業はありません。</p>"}</div>')
    write(OUT / "companies" / "index.html", build_subpage(tpl_html, head_html, "出展企業", listing))
    print(f"  出展企業: {len(pub)} 件公開")

# ---------------------------------------------------------------------------
# イベント紹介ページ
# ---------------------------------------------------------------------------
def slugify(s, fallback):
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9\-]+", "-", s).strip("-")
    return s or fallback

def build_factories(tpl_html, head_html):
    rows = query_db(PAGES_DB_ID, sorts=[{"property": P["pg_order"], "direction": "ascending"}])
    pub = [p for p in rows if p_check(props(p), P["pg_pub"])]
    cards = []
    for pg in pub:
        pr = props(pg); pid = pg["id"].replace("-", "")
        title = p_title(pr, P["pg_title"])
        area = p_select(pr, P["pg_area"]) or ""
        summary = p_text(pr, P["pg_summary"])
        slug = slugify(p_text(pr, P["pg_slug"]), pid)
        covers = p_files(pr, P["pg_cover"]); gallery = p_files(pr, P["pg_gallery"])
        cover_rel = download_image(covers[0], "cover") if covers else ""
        body = blocks_to_html(get_blocks(pg["id"]), depth="sub")
        gal = ""
        if gallery:
            imgs = "".join(f'<img src="../{download_image(u, "gal")}" loading="lazy" alt="">' for u in gallery if download_image(u, "gal"))
            gal = f'<div class="sfv-gallery">{imgs}</div>'
        hero = f'<figure><img src="../{cover_rel}" alt="{html.escape(title)}"></figure>' if cover_rel else ""
        art = (f'<a class="sfv-back" href="./">← 紹介ページ一覧</a>'
               f'<span class="sfv-tag">{html.escape(area)}</span>'
               f'<h1>{html.escape(title)}</h1>'
               f'<p class="lead">{html.escape(summary)}</p>'
               f'{hero}<div class="sfv-article">{body}</div>{gal}')
        write(OUT / "factories" / f"{slug}.html", build_subpage(tpl_html, head_html, title, art))
        ph = f'style="background-image:url(../{cover_rel})"' if cover_rel else ""
        cards.append(
            f'<a class="sfv-card" href="{slug}.html"><div class="ph" {ph}></div><div class="bd">'
            f'<span class="sfv-tag">{html.escape(area)}</span>'
            f'<h3>{html.escape(title)}</h3><p>{html.escape(summary)}</p></div></a>')
    listing = (f'<a class="sfv-back" href="../index.html">← トップへ</a>'
               f'<h1>工場・イベント紹介</h1><p class="lead">参加工場と体験プログラムの紹介</p>'
               f'<div class="sfv-grid">{"".join(cards) or "<p>現在紹介ページはありません。</p>"}</div>')
    write(OUT / "factories" / "index.html", build_subpage(tpl_html, head_html, "紹介ページ", listing))
    print(f"  紹介ページ: {len(pub)} 件公開")

# ---------------------------------------------------------------------------
# 出力ユーティリティ
# ---------------------------------------------------------------------------
def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

def main():
    missing = [k for k, v in {"NOTION_TOKEN": NOTION_TOKEN, "NEWS_DB_ID": NEWS_DB_ID,
                              "PAGES_DB_ID": PAGES_DB_ID, "SCHEDULE_DB_ID": SCHEDULE_DB_ID}.items() if not v]
    if missing:
        sys.exit("環境変数が未設定です: " + ", ".join(missing))
    tpl_html = TPL.read_text(encoding="utf-8")
    # === Notio: ダークモード用CSSを丸ごと無効化（昼夜で見た目を完全統一） ===
    tpl_html = re.sub(r"@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)\s*\{(?:[^{}]|\{[^{}]*\})*\}", "", tpl_html)
    tpl_html = tpl_html.replace("color-scheme: light dark", "color-scheme: light").replace("color-scheme:light dark", "color-scheme:light")
    head_html = extract(tpl_html, "<head", "</head>")
    # <head ...> の中身だけを取り出す（開始タグとtitle/charset重複を適宜除去）
    head_inner = re.sub(r"^<head[^>]*>", "", head_html); head_inner = re.sub(r"</head>$", "", head_inner)
    OUT.mkdir(parents=True, exist_ok=True)
    IMG.mkdir(parents=True, exist_ok=True)
    print("ビルド開始…")
    write(OUT / "index.html", build_index(tpl_html))
    build_news(tpl_html, head_inner)
    build_companies(tpl_html, head_inner)
    build_factories(tpl_html, head_inner)
    (OUT / ".nojekyll").write_text("")  # GitHub Pages で _ 始まりファイルを配信
    print("完了: public/ を生成しました。")

if __name__ == "__main__":
    main()


# ===========================================================================
# build_subpage の正規定義— テンプレートはトークン置換、CSSは単一波括弧
# ===========================================================================
SUBPAGE_CSS = """
:root{color-scheme:light only}
html,body{background-color:#fff!important}
.sfv-sub{max-width:900px;margin:0 auto;padding:120px 24px 80px}
.sfv-sub h1{font-size:clamp(28px,5vw,44px);margin:0 0 8px;line-height:1.2}
.sfv-sub .lead{color:var(--sfv-ink-soft,#6b6b66);margin:0 0 32px;font-size:17px}
.sfv-back{display:inline-block;margin-bottom:24px;color:var(--sfv-blue,#3fa0d6);text-decoration:none;font-weight:600}
.sfv-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:24px}
.sfv-card{display:block;text-decoration:none;color:inherit;border:1px solid var(--sfv-line,rgba(0,0,0,.1));border-radius:16px;overflow:hidden;background:var(--sfv-card,#fff);transition:transform .2s,box-shadow .2s}
.sfv-card:hover{transform:translateY(-4px);box-shadow:0 12px 30px rgba(0,0,0,.10)}
.sfv-card .ph{aspect-ratio:4/3;background:var(--sfv-tint,#eef5fa);background-size:cover;background-position:center}
.sfv-card .bd{padding:16px 18px}
.sfv-card .bd h3{margin:0 0 6px;font-size:18px}
.sfv-card .bd p{margin:0;color:var(--sfv-ink-soft,#6b6b66);font-size:14px;line-height:1.6}
.sfv-tag{display:inline-block;font-size:12px;padding:2px 10px;border-radius:999px;background:var(--sfv-tint,#eef5fa);color:var(--sfv-blue-deep,#2f8fc6);margin:0 4px 4px 0}
.sfv-tags{display:flex;flex-wrap:wrap;gap:4px;margin:0 0 10px}
.sfv-ind{font-size:13px;color:var(--sfv-blue-deep,#2f8fc6);font-weight:600;margin:0 0 6px}
.sfv-info{border-collapse:collapse;width:100%;margin:18px 0;font-size:15px}
.sfv-info th{text-align:left;padding:8px 12px;width:110px;color:var(--sfv-ink-soft,#6b6b66);white-space:nowrap;vertical-align:top;border-bottom:1px solid var(--sfv-line,rgba(0,0,0,.08))}
.sfv-info td{padding:8px 12px;border-bottom:1px solid var(--sfv-line,rgba(0,0,0,.08))}
.sfv-meta{font-size:13px;color:var(--sfv-ink-soft,#6b6b66);margin-bottom:6px}
.sfv-article{font-size:16px;line-height:1.9}
.sfv-article :is(h2,h3,h4){margin-top:1.8em}
.sfv-hero{margin:0 0 20px}.sfv-hero img{width:100%;max-height:440px;object-fit:cover;border-radius:12px;display:block}
.sfv-article img{max-width:100%;border-radius:12px}
.sfv-article .callout{display:flex;gap:10px;padding:14px 16px;border-radius:12px;background:var(--sfv-tint,#eef5fa);margin:16px 0}
.sfv-article .callout-ic{flex:0 0 auto}
.sfv-article figure{margin:18px 0}
.sfv-article figcaption{font-size:13px;color:var(--sfv-ink-soft,#6b6b66);text-align:center;margin-top:6px}
.sfv-gallery{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:24px 0}
.sfv-gallery img{width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:12px}
@media(max-width:640px){.sfv-sub{padding:96px 18px 64px}}
"""

SUBPAGE_TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>__TITLE__ — Setouchi Factory View</title>
__HEAD__
<style>
__CSS__
</style>
</head>
<body>
__NAV__
<main class="sfv-sub">
__BODY__
</main>
__FOOT__
</body>
</html>"""

def build_subpage(tpl_html, head_html, title, body_html):
    nav = extract(tpl_html, '<nav class="sfv-nav">', "</nav>")
    foot = extract(tpl_html, "<footer", "</footer>")
    nav = nav.replace('href="#', 'href="../index.html#')
    nav = nav.replace('href="../index.html#top"', 'href="../index.html"')
    extra = '<a href="../news/">News</a><a href="../companies/">出展企業</a><a href="../factories/">紹介ページ</a>'
    nav = nav.replace('<a class="sfv-navcta"', extra + '<a class="sfv-navcta"')
    foot = foot.replace('href="#', 'href="../index.html#')
    page = SUBPAGE_TEMPLATE
    page = page.replace("__TITLE__", html.escape(title))
    page = page.replace("__HEAD__", head_html)
    page = page.replace("__CSS__", SUBPAGE_CSS)
    page = page.replace("__NAV__", nav)
    page = page.replace("__BODY__", body_html)
    page = page.replace("__FOOT__", foot)
    return page
