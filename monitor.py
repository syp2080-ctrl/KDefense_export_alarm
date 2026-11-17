#!/usr/bin/env python3
"""
한국 방산 수출 계약 뉴스 모니터 → 텔레그램 알림

- 수집: 네이버 뉴스 검색 API(키가 있으면) + Google 뉴스 RSS
- 1차 필터: 키워드 규칙 (방산 용어 + 계약/체결/수주 신호, 추측성 기사 제외)
- 2차 판정·요약: Claude API (키가 있으면). 없으면 규칙 필터 + 기사 요약문 사용
- 중복 제거: URL, 제목 유사도, (국가·업체) 조합
- 발송: 텔레그램 봇

사용법
  python monitor.py              # 1회 실행 (GitHub Actions / cron 용)
  python monitor.py --loop 300   # 300초마다 계속 실행 (상시 서버/PC 용)
  python monitor.py --test       # 텔레그램 연결 테스트 메시지 발송
"""
import argparse
import difflib
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup

# ───────────────────────── 설정 ─────────────────────────
KST = timezone(timedelta(hours=9))
STATE_FILE = os.getenv("STATE_FILE", "seen.json")

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
NAVER_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")

MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "24"))   # 이보다 오래된 기사는 무시
MAX_ANALYZE_PER_RUN = int(os.getenv("MAX_ANALYZE_PER_RUN", "15"))
TITLE_SIMILARITY = float(os.getenv("TITLE_SIMILARITY", "0.6"))
DEAL_DEDUP_HOURS = int(os.getenv("DEAL_DEDUP_HOURS", "72"))

# 검색어: 필요에 따라 자유롭게 추가/삭제
QUERIES = [
    "방산 수출 계약 체결",
    "방산 수출 수주",
    "무기 수출 계약",
    "K9 자주포 수출 계약",
    "K2 전차 수출 계약",
    "천무 수출 계약",
    "레드백 장갑차 계약",
    "FA-50 수출 계약",
    "KF-21 수출 계약",
    "천궁 수출 계약",
    "잠수함 수출 계약",
    "호위함 수출 계약",
    "한화에어로스페이스 수출 계약",
    "현대로템 수출 계약",
    "한국항공우주 KAI 수출 계약",
    "LIG넥스원 수출 계약",
    "한화오션 방산 수주",
    "HD현대중공업 함정 수주",
]

DEFENSE_TERMS = [
    "방산", "방위산업", "무기", "국방", "군수", "방위사업청", "방사청",
    "K9", "K10", "K2", "K239", "천무", "자주포", "전차", "장갑차", "레드백", "타이곤",
    "FA-50", "KF-21", "T-50", "KUH", "수리온", "전투기", "훈련기", "헬기",
    "천궁", "M-SAM", "L-SAM", "신궁", "비궁", "해궁", "미사일", "유도무기", "방공",
    "잠수함", "호위함", "구축함", "함정", "군함", "수상함", "탄약", "포탄",
    "한화에어로스페이스", "한화시스템", "한화오션", "현대로템", "한국항공우주", "KAI",
    "LIG넥스원", "HD현대중공업", "풍산", "SNT다이내믹스", "현대위아",
]
CONTRACT_TERMS = ["계약", "체결", "서명", "수주", "따냈", "따내", "공급 확정", "협약"]
STRONG_TERMS = ["체결", "서명", "수주", "따냈", "따내", "확정"]
SPECULATIVE_TERMS = [
    "전망", "가능성", "추진", "협상", "검토", "기대", "유력", "임박", "노린다", "노려",
    "도전", "관심", "나선다", "나서", "타진", "?", "설명회", "전시회", "시연",
]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

log = logging.getLogger("monitor")


# ───────────────────────── 유틸 ─────────────────────────
def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def norm_title(t: str) -> str:
    t = re.sub(r"\[[^\]]*\]|【[^】]*】", " ", t)
    t = re.sub(r"[^\w%.]", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def norm_url(u: str) -> str:
    p = urlparse(u)
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", p.query, ""))


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def domain(u: str) -> str:
    return urlparse(u).netloc.replace("www.", "")


def now_ts() -> float:
    return time.time()


# ───────────────────────── 수집 ─────────────────────────
def fetch_naver(query: str):
    r = requests.get(
        "https://openapi.naver.com/v1/search/news.json",
        params={"query": query, "display": 50, "sort": "date"},
        headers={"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET},
        timeout=10,
    )
    r.raise_for_status()
    for it in r.json().get("items", []):
        link = it.get("originallink") or it.get("link")
        naver_link = it.get("link", "")
        yield {
            "title": clean(it.get("title")),
            "link": link,
            "text_url": naver_link if "n.news.naver.com" in naver_link else link,
            "desc": clean(it.get("description")),
            "published": parse_date(it.get("pubDate")),
            "source": domain(link),
        }


def fetch_google(query: str):
    url = (f"https://news.google.com/rss/search?q={quote(query + ' when:1d')}"
           f"&hl=ko&gl=KR&ceid=KR:ko")
    r = requests.get(url, headers=UA, timeout=10)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    for e in feed.entries:
        title = clean(e.get("title"))
        source = (e.get("source") or {}).get("title", "")
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        yield {
            "title": title,
            "link": e.get("link"),
            "text_url": None,  # Google 뉴스 링크는 JS 리다이렉트라 본문 수집 생략
            "desc": "",
            "published": parse_date(e.get("published")),
            "source": source or "Google 뉴스",
        }


def collect():
    items, seen_links = [], set()
    sources = []
    if NAVER_ID and NAVER_SECRET:
        sources.append(("naver", fetch_naver))
    sources.append(("google", fetch_google))
    for name, fn in sources:
        for q in QUERIES:
            try:
                for it in fn(q):
                    if not it["link"] or not it["title"]:
                        continue
                    k = norm_url(it["link"])
                    if k in seen_links:
                        continue
                    seen_links.add(k)
                    items.append(it)
            except Exception as e:
                log.warning("수집 실패 [%s] %s: %s", name, q, e)
            time.sleep(0.2)
    log.info("수집된 기사 %d건", len(items))
    return items


# ───────────────────────── 필터 ─────────────────────────
def rule_filter(it) -> bool:
    title = it["title"]
    text = f"{title} {it['desc']}"
    if not any(t in text for t in DEFENSE_TERMS):
        return False
    if not any(t in title for t in CONTRACT_TERMS):
        return False
    if any(t in title for t in SPECULATIVE_TERMS) and not any(t in title for t in ("체결", "서명", "따냈")):
        return False
    return True


def similar_to_recent(title: str, recent) -> bool:
    nt = norm_title(title)
    return any(difflib.SequenceMatcher(None, nt, r[0]).ratio() >= TITLE_SIMILARITY for r in recent)


# ───────────────────────── 본문·분석 ─────────────────────────
BODY_SELECTORS = [
    "#dic_area", "#newsct_article", "[itemprop=articleBody]", "#articleBodyContents",
    "#article-view-content-div", "#articleBody", "#article_body", ".article_body",
    ".article-body", ".news_body", ".news_cnt_detail_wrap", "article",
]


def fetch_article_text(url: str) -> str:
    if not url or "news.google.com" in url:
        return ""
    try:
        r = requests.get(url, headers=UA, timeout=10)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "noscript", "header", "footer", "nav", "aside", "figure"]):
            t.decompose()
        for sel in BODY_SELECTORS:
            el = soup.select_one(sel)
            if el:
                txt = clean(el.get_text(" "))
                if len(txt) > 200:
                    return txt[:4000]
        ps = [clean(p.get_text(" ")) for p in soup.find_all("p")]
        txt = " ".join(p for p in ps if len(p) > 40)
        return txt[:4000] if len(txt) > 200 else ""
    except Exception as e:
        log.debug("본문 수집 실패 %s: %s", url, e)
        return ""


PROMPT = """아래 뉴스가 "한국 정부 또는 한국 기업이 해외 국가/기업과 방산(무기체계, 탄약, 함정, 항공기, 방산 부품 등) 수출 계약을 실제로 체결·서명했거나 수주를 확정한 사실"을 보도하는지 판단하세요.
협상 중, 추진, 전망, 가능성, MOU 검토, 전시회 출품 등은 false입니다. 국내 군(방사청) 납품 계약도 false입니다. 이행계약·실행계약·추가 계약 체결은 true입니다.

[제목] {title}
[언론사] {source}
[내용]
{body}

반드시 아래 JSON 한 개만 출력하세요. 다른 말은 쓰지 마세요.
{{"is_export_deal": true/false,
 "country": "수입국 (모르면 빈 문자열)",
 "company": "한국 측 업체 (복수면 쉼표)",
 "item": "품목/무기체계",
 "value": "계약 규모 (원화·달러 등 기사 표기 그대로, 모르면 빈 문자열)",
 "summary": "핵심 내용 3문장 이내 한국어 요약 (계약 주체, 품목·수량, 규모, 인도 일정 등)"}}"""


def analyze(it):
    body = fetch_article_text(it.get("text_url")) or it["desc"] or "(본문 없음, 제목만으로 판단)"
    if not ANTHROPIC_KEY:
        return {"is_export_deal": True, "country": "", "company": "", "item": "", "value": "",
                "summary": it["desc"] or body[:300]}
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 700,
                  "messages": [{"role": "user", "content": PROMPT.format(
                      title=it["title"], source=it["source"], body=body)}]},
            timeout=60,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0))
        data["is_export_deal"] = bool(data.get("is_export_deal"))
        return data
    except Exception as e:
        log.warning("Claude 분석 실패 (규칙 판정으로 대체): %s", e)
        return {"is_export_deal": True, "country": "", "company": "", "item": "", "value": "",
                "summary": it["desc"] or body[:300]}


def deal_signature(res):
    c = re.sub(r"\s+", "", (res.get("country") or "")).lower()
    co = re.sub(r"\s+", "", (res.get("company") or "")).lower()
    it = re.sub(r"\s+", "", (res.get("item") or "")).lower()
    return f"{c}|{co}|{it}" if c and (co or it) else ""


# ───────────────────────── 텔레그램 ─────────────────────────
def format_message(it, res) -> str:
    e = html.escape
    lines = ["🛡️ <b>방산 수출 계약 소식</b>", "", f"<b>{e(it['title'])}</b>", ""]
    meta = []
    if res.get("country"):
        meta.append(f"🌍 수입국: {e(res['country'])}")
    if res.get("company"):
        meta.append(f"🏢 업체: {e(res['company'])}")
    if res.get("item"):
        meta.append(f"📦 품목: {e(res['item'])}")
    if res.get("value"):
        meta.append(f"💰 규모: {e(res['value'])}")
    if meta:
        lines += meta + [""]
    if res.get("summary"):
        lines += ["📝 " + e(res["summary"]), ""]
    when = it["published"].astimezone(KST).strftime("%m/%d %H:%M") if it.get("published") else ""
    lines.append(f"🔗 <a href=\"{e(it['link'], quote=True)}\">원문 보기</a> · {e(it['source'])} {when}")
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    if not (TG_TOKEN and TG_CHAT):
        print("─" * 40 + "\n[텔레그램 미설정: 출력만]\n" + text)
        return True
    for attempt in range(3):
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": False},
            timeout=15,
        )
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
            continue
        if r.ok:
            return True
        log.error("텔레그램 발송 실패 %s: %s", r.status_code, r.text[:300])
        return False
    return False


# ───────────────────────── 상태 ─────────────────────────
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    now = now_ts()
    state["seen"] = {k: v for k, v in state["seen"].items() if now - v < 7 * 86400}
    state["recent"] = [r for r in state["recent"] if now - r[1] < 3 * 86400]
    state["deals"] = [d for d in state["deals"] if now - d[1] < DEAL_DEDUP_HOURS * 3600]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0)


# ───────────────────────── 실행 ─────────────────────────
def run_once(send_existing=False):
    state = load_state()
    first_run = state is None
    state = state or {"seen": {}, "recent": [], "deals": []}
    state.setdefault("deals", [])

    now = datetime.now(timezone.utc)
    candidates = []
    for it in collect():
        key = hashlib.sha1(norm_url(it["link"]).encode()).hexdigest()
        if key in state["seen"]:
            continue
        state["seen"][key] = now_ts()
        if it["published"] and now - it["published"] > timedelta(hours=MAX_AGE_HOURS):
            continue
        if not rule_filter(it):
            continue
        if similar_to_recent(it["title"], state["recent"]):
            continue
        state["recent"].append([norm_title(it["title"]), now_ts()])
        candidates.append(it)

    log.info("1차 필터 통과 %d건", len(candidates))

    if first_run and not send_existing:
        save_state(state)
        send_telegram("✅ 방산 수출 계약 뉴스 모니터링을 시작합니다.\n"
                      f"(기존 기사 {len(candidates)}건은 건너뛰고, 이후 새 기사부터 알려드립니다)")
        return

    candidates.sort(key=lambda x: x["published"] or now)
    sent = 0
    for it in candidates[:MAX_ANALYZE_PER_RUN]:
        res = analyze(it)
        if not res.get("is_export_deal"):
            log.info("제외(계약 아님): %s", it["title"])
            continue
        sig = deal_signature(res)
        if sig and any(d[0] == sig for d in state["deals"]):
            log.info("제외(같은 계약 중복): %s", it["title"])
            continue
        if send_telegram(format_message(it, res)):
            sent += 1
            if sig:
                state["deals"].append([sig, now_ts()])
        time.sleep(1)
    save_state(state)
    log.info("발송 %d건", sent)


def main():
    ap = argparse.ArgumentParser(description="방산 수출 계약 뉴스 → 텔레그램")
    ap.add_argument("--loop", type=int, metavar="SEC", help="SEC초 간격으로 계속 실행")
    ap.add_argument("--send-existing", action="store_true", help="첫 실행 때도 기존 기사를 발송")
    ap.add_argument("--test", action="store_true", help="텔레그램 테스트 메시지 발송")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.test:
        ok = send_telegram("🔔 방산 수출 모니터 테스트 메시지입니다.")
        sys.exit(0 if ok else 1)

    if args.loop:
        while True:
            try:
                run_once(args.send_existing)
            except Exception as e:
                log.exception("실행 오류: %s", e)
            args.send_existing = False
            time.sleep(args.loop)
    else:
        run_once(args.send_existing)


if __name__ == "__main__":
    main()
