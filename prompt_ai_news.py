"""
Prompt AI News — Unified Briefing Agent
Fetches AI, China & AI, and EV stories, reads the full articles, generates
briefs/articles with GLM (via OpenRouter), and emails ONE combined briefing.

Run times are controlled by Windows Task Scheduler (see register_task.bat):
06:00, 12:00, 18:00 daily. The email subject adapts (Morning/Midday/Evening).

Secrets live in C:\\Users\\mrcar\\.promptai\\.env (outside OneDrive sync).

Usage:
    python prompt_ai_news.py
    python prompt_ai_news.py --no-email --section ai   (test a single section)
"""

import argparse
import base64
import concurrent.futures
import html
import json
import os
import re
import smtplib
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from PIL import Image

BASE_DIR = Path(__file__).parent
ENV_PATH = Path(r"C:\Users\mrcar\.promptai\.env")
BRIEFS_DIR = BASE_DIR / "briefs"
LOGO_PATH = BASE_DIR / "Prompt_CirLogo.png"
USAGE_LOG = BASE_DIR / "usage_log.jsonl"

SEEN_EXPIRY_HOURS = 24
MAX_ARTICLES_PER_RUN = 45      # cap candidates sent to the model
FULLTEXT_CHARS = 3500          # full article text included per article
FETCH_WORKERS = 8
MIN_NEW_STORIES = 2

# ── Env ────────────────────────────────────────────────────────────────────────

def load_env() -> None:
    if not ENV_PATH.exists():
        sys.exit(f"FATAL: secrets file missing at {ENV_PATH}")
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

load_env()

GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
SENDER = os.environ.get("SENDER_EMAIL", "mrcarloslucero@gmail.com")
RECIPIENT = os.environ.get("RECIPIENT_EMAIL", SENDER)

# ── Section definitions ───────────────────────────────────────────────────────

AI_FEEDS = {
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "NY Times Tech": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "MIT Tech Review": "https://www.technologyreview.com/feed/",
    "Ars Technica AI": "https://arstechnica.com/tag/ai/feed/",
    "Reddit r/artificial": "https://www.reddit.com/r/artificial/.rss",
    "Reddit r/MachineLearning": "https://www.reddit.com/r/MachineLearning/.rss",
    "Reddit r/singularity": "https://www.reddit.com/r/singularity/.rss",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Wired": "https://www.wired.com/feed/rss",
    "Politico Tech": "https://www.politico.com/tech/rss",
    "The Guardian Tech": "https://www.theguardian.com/technology/rss",
    "EURACTIV AI": "https://www.euractiv.com/section/artificial-intelligence/feed/",
    "Pew Research": "https://www.pewresearch.org/feed/",
    "Hacker News": "https://news.ycombinator.com/rss",
    "OpenAI News": "https://openai.com/news/rss.xml",
    "HN AI": "https://hnrss.org/newest?q=AI",
    "The Decoder": "https://the-decoder.com/feed/",
    "Simon Willison": "https://simonwillison.net/atom/everything/",
    "Import AI": "https://jack-clark.net/feed/",
    "Interconnects": "https://www.interconnects.ai/feed",
    "Tech Policy Press": "https://www.techpolicy.press/feed/",
    "AI News": "https://artificialintelligence-news.com/feed/",
}

CHINA_FEEDS = {
    "TechNode": "https://technode.com/feed/",
    "KrASIA": "https://kr-asia.com/feed",
    "Pandaily": "https://pandaily.com/feed/",
    "SCMP Tech": "https://www.scmp.com/rss/5/feed",
    "Sixth Tone": "https://www.sixthtone.com/rss",
    "ChinaTalk": "https://chinatalk.substack.com/feed",
    "Asia Times": "https://asiatimes.com/feed/",
    "Caixin English": "https://english.caixin.com/rss/feed",
    "MIT Tech Review": "https://www.technologyreview.com/feed/",
}

EV_FEEDS = {
    "Electrek": "https://electrek.co/feed/",
    "InsideEVs": "https://insideevs.com/feed/",
    "CleanTechnica": "https://cleantechnica.com/feed/",
    "Ars Technica Cars": "https://feeds.arstechnica.com/arstechnica/cars",
    "TechCrunch EVs": "https://techcrunch.com/tag/electric-vehicles/feed/",
    "Car and Driver": "https://www.caranddriver.com/rss/all.xml/",
    "Green Car Reports": "https://www.greencarreports.com/rss",
    "Teslarati": "https://www.teslarati.com/feed/",
    "Charged EVs": "https://chargedevs.com/feed/",
    "Autoweek": "https://www.autoweek.com/rss/all/",
}

AI_SYSTEM_PROMPT = """\
You are a sharp, engaging tech correspondent writing briefs for an AI-focused \
YouTube channel and live show. Your audience loves the cutting edge: new models, \
industry power moves, research breakthroughs, product launches, and policy shifts \
that actually affect how people use AI.

YOUR JOB:
1. From the article list provided (which includes the FULL TEXT of each article), \
select the 5 most compelling AI stories of the day.
2. For each story, write a tight, broadcast-ready brief that a host can read on air.

SELECTION CRITERIA (in order of priority):
- Story must be directly and substantively about AI, ML, LLMs, robotics with AI, \
or a major AI company's strategic move. "Software update mentions AI" does NOT qualify.
- Prefer stories with mass-market impact or that signal a meaningful industry shift.
- Prefer recency — a story from the last 12 hours beats one from yesterday.
- No duplicate angles: if three sources cover the same announcement, pick one.

BRIEF STYLE:
- 3 - 4 sentences per story. Lead with the most interesting fact, not the company name.
- Use specifics from the full article text — numbers, names, dates. Never invent facts \
that are not in the source material.
- Sound like a smart human wrote it — direct, opinionated, no filler phrases like \
"In a world where AI is changing everything..." or "This just in..."
- No bullet points inside a brief — flowing prose only.
- Each brief should leave the audience wanting to know more.

OUTPUT FORMAT — use exactly this markdown structure, nothing before the first ##:

## [Story Number]. [Punchy Headline You Write]
**Source:** [Publication name]
**Brief:** [3 -4 sentence broadcast-ready summary]
**Link:** [URL]

---

After the 5 stories, add a short (1 sentence) "Host Note" with a quick suggestion \
on which story to lead with and why, labeled:

**Host Note:** [your suggestion]
"""

CHINA_SYSTEM_PROMPT = """\
You are a senior technology journalist specializing in China's AI industry, writing \
for promptainews.com. Your articles are optimized for Answer Engine Optimization (AEO) \
— meaning AI systems like Perplexity, ChatGPT, and Claude will cite your work as a \
primary source because it is factually dense, authoritative, and precisely structured.

YOUR JOB:
1. From the article list provided (which includes the FULL TEXT of each article), \
select the 5 most significant stories involving China and AI. Stories must directly \
involve Chinese AI companies, researchers, government policy, US-China AI competition, \
or Chinese AI products/models. Generic Western AI stories do not qualify unless they \
directly reference China.
2. For each story, write a full publishable article — not a brief. Ground every claim \
in the full article text provided; never invent facts.

AEO WRITING RULES (follow these exactly):
- Lead with the most important fact as a direct declarative statement. Never start with \
a company name alone — start with the development.
- Use specific numbers, dates, company names, model names, and dollar figures wherever \
possible. Vague claims do not get cited by AI engines.
- Write corrective statements where the record needs setting: \
"Contrary to the narrative that China is merely copying Western AI, [specific fact]."
- No hedging language: never use "reportedly," "sources suggest," "it seems," or \
"many believe." State facts as facts.
- No AI slop: never write "In a world where AI is transforming everything," \
"game-changer," "revolutionary," or "unprecedented." Cut these on sight.
- Every article must contain at least one standalone quotable sentence — a single \
sentence that summarizes the key fact so precisely that an AI engine can extract \
and cite it verbatim.
- Structure: punchy headline → 1-sentence lede (the key fact) → 3-5 paragraph body \
→ 1-sentence "Why it matters" closing.

OUTPUT FORMAT — use exactly this markdown structure, nothing before the first ##:

## [Story Number]. [Direct, Factual Headline]
**Source:** [Publication name]
**Lede:** [1 sentence — the single most important fact, stated directly]
**Article:**
[3-5 paragraphs of AEO-optimized journalism]
**Why it matters:** [1 sentence — the strategic or geopolitical significance]
**Link:** [URL]

---

After the 5 stories, add:
**Editor Note:** [1 sentence on which story is most significant for the China & AI page and why]
"""

EV_SYSTEM_PROMPT = """\
You are a senior automotive technology journalist covering the electric vehicle industry \
for promptainews.com. Your writing is optimized for Answer Engine Optimization (AEO) \
— AI systems like Perplexity, ChatGPT, and Claude cite your work because it is \
factually precise, direct, and structured for machine extraction.

YOUR JOB:
1. From the article list provided (which includes the FULL TEXT of each article), \
select the 5 most significant EV stories. Stories must be directly about electric \
vehicles, EV charging infrastructure, EV policy, EV battery technology, or major \
automaker EV moves. Generic car news does not qualify.
2. For each story, write a tight, AEO-optimized news brief. Ground every claim in the \
full article text provided; never invent facts.

AEO WRITING RULES (follow these exactly):
- Lead with the most important fact as a direct declarative statement.
- Use specific numbers, dates, company names, model names, range figures, and dollar \
amounts wherever possible. Vague claims do not get cited by AI engines.
- No hedging: never use "reportedly," "sources suggest," "it seems," or "many believe."
- No AI slop: never use "game-changer," "revolutionary," "unprecedented," \
"In a world where EVs are transforming everything," or any similar filler. Cut on sight.
- Write corrective statements where the record needs setting: \
"Despite headlines claiming X, the actual figure is Y."
- Every brief must contain at least one standalone quotable sentence that an AI engine \
can extract and cite verbatim.
- Structure: punchy headline → 3-4 sentences of AEO-optimized reporting → \
1-sentence "Bottom line" closing.

OUTPUT FORMAT — use exactly this markdown structure, nothing before the first ##:

## [Story Number]. [Direct, Factual Headline]
**Source:** [Publication name]
**Brief:** [3-4 sentences of AEO-optimized reporting]
**Bottom line:** [1 sentence — the key takeaway stated as a fact]
**Link:** [URL]

---

After the 5 stories, add:
**Editor Note:** [1 sentence on the most significant story and why]
"""

SECTIONS = [
    {
        "key": "ai",
        "name": "AI News",
        "feeds": AI_FEEDS,
        "system_prompt": AI_SYSTEM_PROMPT,
        "model": "z-ai/glm-5.3-flash",
        "max_tokens": 16384,
        "seen_log": BASE_DIR / "seen_stories.json",
        "theme": {"accent": "#3b82f6", "light": "#eff6ff", "border": "#bfdbfe",
                  "dark_text": "#0d1f40", "badge": "#3b82f6"},
    },
    {
        "key": "china_ai",
        "name": "China & AI",
        "feeds": CHINA_FEEDS,
        "system_prompt": CHINA_SYSTEM_PROMPT,
        "model": "z-ai/glm-5.3",
        "max_tokens": 20480,
        "seen_log": BASE_DIR / "seen_china_ai.json",
        "theme": {"accent": "#d97706", "light": "#fefce8", "border": "#fcd34d",
                  "dark_text": "#0d1f40", "badge": "#d97706"},
    },
    {
        "key": "ev",
        "name": "EV News",
        "feeds": EV_FEEDS,
        "system_prompt": EV_SYSTEM_PROMPT,
        "model": "z-ai/glm-5.3-flash",
        "max_tokens": 16384,
        "seen_log": BASE_DIR / "seen_ev_news.json",
        "theme": {"accent": "#059669", "light": "#ecfdf5", "border": "#6ee7b7",
                  "dark_text": "#0d1f40", "badge": "#059669"},
    },
]

# ── Seen-stories log ──────────────────────────────────────────────────────────

def load_seen(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_seen(path: Path, seen: dict) -> None:
    path.write_text(json.dumps(seen, indent=2), encoding="utf-8")


def prune_seen(seen: dict) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=SEEN_EXPIRY_HOURS)).isoformat()
    return {url: ts for url, ts in seen.items() if ts > cutoff}


# ── Feed fetching ─────────────────────────────────────────────────────────────

def _clean_html(raw: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw or "")).strip()


def fetch_articles(feeds: dict[str, str], max_age_hours: int) -> list[dict]:
    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    for source, url in feeds.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:12]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                if not title or not link or "promptainews.com" in link:
                    continue
                published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                if published_parsed:
                    pub_dt = datetime.fromtimestamp(time.mktime(published_parsed), tz=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                articles.append({
                    "title": title, "url": link, "source": source,
                    "published": entry.get("published", ""),
                    "snippet": _clean_html(entry.get("summary", "") or entry.get("description", ""))[:300],
                })
        except Exception as exc:
            print(f"  ! could not fetch {source}: {exc}", file=sys.stderr)
    return articles


def fetch_full_text(url: str) -> str:
    """Download the article page and extract readable text (best effort)."""
    try:
        resp = requests.get(
            url, timeout=12,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"},
        )
        if resp.status_code != 200 or not resp.text:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
            tag.decompose()
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = " ".join(p for p in paragraphs if len(p) > 40)
        return text[:FULLTEXT_CHARS]
    except Exception:
        return ""


def enrich_with_full_text(articles: list[dict]) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for art, text in zip(articles, pool.map(lambda a: fetch_full_text(a["url"]), articles)):
            art["full_text"] = text


def dedupe_by_title(articles: list[dict]) -> list[dict]:
    seen_titles = set()
    out = []
    for a in articles:
        key = re.sub(r"[^a-z0-9]", "", a["title"].lower())[:60]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        out.append(a)
    return out

# ── GLM generation (via OpenRouter) ───────────────────────────────────────────

def generate_section(section: dict, articles: list[dict]) -> tuple[str, dict]:
    """Return (markdown_content, usage_dict) for one section."""
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_API_KEY,
    )
    blocks = []
    for i, art in enumerate(articles, start=1):
        body = art["full_text"] or art["snippet"]
        blocks.append(
            f"[{i}] SOURCE: {art['source']}\n"
            f"    TITLE: {art['title']}\n"
            f"    URL: {art['url']}\n"
            f"    PUBLISHED: {art['published']}\n"
            f"    FULL TEXT: {body}\n"
        )
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    user_message = (
        f"Today is {today}. Here are the latest articles with their full text.\n"
        f"Select the 5 most significant stories and write the output exactly as specified.\n\n"
        f"ARTICLES:\n" + "\n".join(blocks)
    )

    response = client.chat.completions.create(
        model=section["model"],
        max_tokens=section["max_tokens"],
        temperature=0.6,
        messages=[
            {"role": "system", "content": section["system_prompt"]},
            {"role": "user", "content": user_message},
        ],
    )
    content = response.choices[0].message.content or ""
    usage = response.usage
    cost = None
    if usage and getattr(usage, "model_extra", None):
        cost = usage.model_extra.get("cost")
    usage_dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "section": section["key"],
        "model": section["model"],
        "prompt_tokens": usage.prompt_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
        "total_tokens": usage.total_tokens if usage else 0,
        "cost": cost,
        "articles_fetched": len(articles),
        "articles_with_full_text": sum(1 for a in articles if a.get("full_text")),
    }
    log_usage(usage_dict)
    return content, usage_dict


def log_usage(entry: dict) -> None:
    with USAGE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    cost_str = f"${entry['cost']:.5f}" if entry["cost"] is not None else "n/a"
    print(f"    tokens: {entry['total_tokens']:,} | cost: {cost_str}")

# ── Parsing ───────────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return html.escape(text or "")


def _inline_bold(text: str) -> str:
    text = _esc(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)


def parse_ai(content: str) -> tuple[list[dict], str]:
    stories, host_note = [], ""
    for block in re.split(r"\n---\n", content):
        m = re.search(
            r"##\s*(.+?)\n\*\*Source:\*\*\s*(.+?)\n\*\*Brief:\*\*\s*(.+?)\n\*\*Link:\*\*\s*(https?://\S+)",
            block, re.DOTALL,
        )
        if m:
            stories.append({"headline": m[1].strip(), "source": m[2].strip(),
                            "body": m[3].strip(), "link": m[4].strip()})
        hn = re.search(r"\*\*Host Note:\*\*\s*(.+)", block, re.DOTALL)
        if hn:
            host_note = hn.group(1).strip()
    return stories, host_note


def parse_china(content: str) -> tuple[list[dict], str]:
    stories, editor_note = [], ""
    for block in re.split(r"\n---\n", content):
        m = re.search(
            r"##\s*(.+?)\n\*\*Source:\*\*\s*(.+?)\n\*\*Lede:\*\*\s*(.+?)\n"
            r"\*\*Article:\*\*\s*(.+?)\n\*\*Why it matters:\*\*\s*(.+?)\n\*\*Link:\*\*\s*(https?://\S+)",
            block, re.DOTALL,
        )
        if m:
            stories.append({"headline": m[1].strip(), "source": m[2].strip(),
                            "body": m[3].strip(), "article": m[4].strip(),
                            "extra_label": "WHY IT MATTERS", "extra": m[5].strip(),
                            "link": m[6].strip(), "is_lede_style": True})
        en = re.search(r"\*\*Editor Note:\*\*\s*(.+)", block, re.DOTALL)
        if en:
            editor_note = en.group(1).strip()
    return stories, editor_note


def parse_ev(content: str) -> tuple[list[dict], str]:
    stories, editor_note = [], ""
    for block in re.split(r"\n---\n", content):
        m = re.search(
            r"##\s*(.+?)\n\*\*Source:\*\*\s*(.+?)\n\*\*Brief:\*\*\s*(.+?)\n"
            r"\*\*Bottom line:\*\*\s*(.+?)\n\*\*Link:\*\*\s*(https?://\S+)",
            block, re.DOTALL,
        )
        if m:
            stories.append({"headline": m[1].strip(), "source": m[2].strip(),
                            "body": m[3].strip(),
                            "extra_label": "BOTTOM LINE", "extra": m[4].strip(),
                            "link": m[5].strip()})
        en = re.search(r"\*\*Editor Note:\*\*\s*(.+)", block, re.DOTALL)
        if en:
            editor_note = en.group(1).strip()
    return stories, editor_note

# ── Email rendering ───────────────────────────────────────────────────────────

def _logo_src() -> str:
    try:
        img = Image.open(LOGO_PATH).convert("RGBA")
        img = img.resize((80, 80), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"
    except Exception:
        return ""


def _story_card(i: int, s: dict, theme: dict) -> str:
    body_html = ""
    if s.get("is_lede_style"):
        body_html += (
            f'<p style="margin:0 0 12px;font-family:Georgia,serif;font-size:13px;font-style:italic;'
            f'color:#92400e;line-height:1.6;">{_inline_bold(s["body"])}</p>'
        )
        body_html += "".join(
            f'<p style="margin:0 0 12px;font-family:Georgia,serif;font-size:14px;line-height:1.8;color:#1e293b;">{_inline_bold(p.strip())}</p>'
            for p in (s.get("article") or "").split("\n") if p.strip()
        )
    else:
        body_html += (
            f'<p style="margin:0 0 10px;font-family:Georgia,serif;font-size:14px;line-height:1.75;color:#1e293b;">'
            f'{_inline_bold(s["body"])}</p>'
        )
    extra_html = ""
    if s.get("extra_label"):
        extra_html = (
            f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;border-top:1px solid {theme["border"]};padding-top:10px;">'
            f'<tr><td style="font-family:Arial,sans-serif;font-size:12px;color:{theme["accent"]};font-weight:700;">'
            f'{s["extra_label"]}: <span style="font-weight:400;color:#1e293b;">{_inline_bold(s["extra"])}</span></td></tr></table>'
        )
    link = s["link"] if s["link"].startswith(("http://", "https://")) else "#"
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;border-radius:8px;overflow:hidden;border:1px solid {theme['border']};">
      <tr><td style="background:{theme['light']};padding:12px 20px;border-bottom:1px solid {theme['border']};">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="width:32px;vertical-align:middle;">
            <span style="display:inline-block;width:26px;height:26px;background:{theme['badge']};border-radius:50%;text-align:center;line-height:26px;font-family:Arial,sans-serif;font-weight:700;font-size:12px;color:#fff;">{i}</span>
          </td>
          <td style="padding-left:12px;vertical-align:middle;">
            <span style="font-family:Arial,sans-serif;font-size:15px;font-weight:700;color:{theme['dark_text']};line-height:1.3;">{_esc(s['headline'])}</span>
          </td>
        </tr></table>
      </td></tr>
      <tr><td style="background:#ffffff;padding:16px 20px 8px;">
        {body_html}{extra_html}
        <table cellpadding="0" cellspacing="0" style="margin-top:10px;"><tr>
          <td style="background:{theme['light']};border-radius:4px;padding:4px 10px;">
            <span style="font-family:Arial,sans-serif;font-size:11px;color:{theme['accent']};font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">{_esc(s['source'])}</span>
          </td>
          <td style="padding-left:14px;">
            <a href="{link}" style="font-family:Arial,sans-serif;font-size:12px;color:#2563eb;text-decoration:none;font-weight:600;">Read full story &rarr;</a>
          </td>
        </tr></table>
      </td></tr>
    </table>"""


def _note_block(label: str, text: str, theme: dict) -> str:
    if not text:
        return ""
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;margin-bottom:24px;border-radius:8px;overflow:hidden;border:1px solid {theme['accent']};">
      <tr><td style="background:{theme['light']};padding:16px 20px;">
        <p style="margin:0 0 6px;font-family:Arial,sans-serif;font-size:11px;color:{theme['accent']};font-weight:700;text-transform:uppercase;letter-spacing:1px;">{label}</p>
        <p style="margin:0;font-family:Georgia,serif;font-size:14px;color:#1e293b;line-height:1.65;">{_inline_bold(text)}</p>
      </td></tr>
    </table>"""


def build_section_html(section: dict, stories: list[dict], note_label: str, note_text: str) -> str:
    theme = section["theme"]
    cards = "".join(_story_card(i, s, theme) for i, s in enumerate(stories, start=1))
    return f"""
    <tr><td style="background:{theme['accent']};padding:10px 32px;">
      <span style="font-family:Arial,sans-serif;font-size:15px;font-weight:900;color:#ffffff;letter-spacing:1px;">{section['name'].upper()}</span>
      <span style="float:right;font-family:Arial,sans-serif;font-size:11px;color:rgba(255,255,255,0.75);">{len(stories)} stories</span>
    </td></tr>
    <tr><td style="background:#f8faff;padding:20px 28px 4px;">
      {cards}{_note_block(note_label, note_text, theme)}
    </td></tr>"""


def build_unified_email(results: list[dict]) -> str:
    now = datetime.now()
    date_str = now.strftime("%B %d, %Y")
    time_str = now.strftime("%I:%M %p")
    logo_src = _logo_src()
    logo_html = (
        f'<img src="{logo_src}" width="80" height="80" alt="Prompt AI News" '
        f'style="display:block;margin:0 auto 16px;border-radius:50%;" />' if logo_src else ""
    )
    sections_html = "".join(
        build_section_html(r["section"], r["stories"], r["note_label"], r["note"])
        for r in results
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Prompt AI News Briefing</title></head>
<body style="margin:0;padding:0;background:#f0f4f8;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;">
    <tr><td align="center" style="padding:30px 16px;">
      <table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(13,31,64,0.10);">
        <tr><td style="background:#0d1f40;padding:36px 32px 28px;text-align:center;border-radius:12px 12px 0 0;">
          {logo_html}
          <h1 style="margin:0;font-family:Arial,sans-serif;font-size:36px;font-weight:900;letter-spacing:-1px;line-height:1.1;">
            <span style="color:#ffffff;">Prompt</span>&nbsp;<span style="color:#60a5fa;">AI News</span>
          </h1>
          <p style="margin:10px 0 0;font-family:Arial,sans-serif;font-size:12px;color:rgba(255,255,255,0.60);letter-spacing:0.5px;">Your Daily Briefing</p>
        </td></tr>
        <tr><td style="background:#eff6ff;padding:10px 32px;border-bottom:1px solid #bfdbfe;">
          <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td style="font-family:Arial,sans-serif;font-size:12px;color:#64748b;">
              <span style="color:#0d1f40;font-weight:700;">{date_str}</span> &nbsp;&bull;&nbsp; {time_str}
            </td>
          </tr></table>
        </td></tr>
        {sections_html}
        <tr><td style="background:#0d1f40;border-radius:0 0 12px 12px;padding:20px 32px;text-align:center;">
          <p style="margin:0 0 6px;font-family:Arial,sans-serif;font-size:13px;font-weight:700;">
            <span style="color:#ffffff;">Prompt</span>&nbsp;<span style="color:#60a5fa;">AI News</span>
          </p>
          <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:rgba(255,255,255,0.45);">
            Three briefings daily &nbsp;&bull;&nbsp; Powered by GLM &nbsp;&bull;&nbsp; Built for creators
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

# ── Email sending ─────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str, plain_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Prompt AI News <{SENDER}>"
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SENDER, GMAIL_APP_PASSWORD)
        server.sendmail(SENDER, RECIPIENT, msg.as_string())
    print(f"Email sent to {RECIPIENT}: {subject}")


def send_failure_alert(error_summary: str) -> None:
    try:
        now = datetime.now().strftime("%b %d, %I:%M %p")
        body = (f"<p style='font-family:Arial,sans-serif;font-size:14px;color:#1e293b;'>"
                f"The scheduled Prompt AI briefing run at {now} failed.</p>"
                f"<pre style='background:#fef2f2;border:1px solid #fecaca;border-radius:8px;"
                f"padding:12px;font-size:12px;color:#991b1b;white-space:pre-wrap;'>{html.escape(error_summary[-3000:])}</pre>")
        send_email(f"[Prompt AI] ALERT — briefing run failed ({now})",
                   f"<html><body>{body}</body></html>", error_summary[-3000:])
    except Exception as exc:
        print(f"Could not send failure alert: {exc}", file=sys.stderr)

# ── Archiving ─────────────────────────────────────────────────────────────────

def archive_section(section: dict, content: str, article_count: int) -> None:
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    path = BRIEFS_DIR / f"{section['key']}_brief_{now.strftime('%Y%m%d_%H%M')}.md"
    header = (f"# {section['name']} — {now.strftime('%B %d, %Y %I:%M %p')}\n"
              f"_{article_count} articles scanned · model {section['model']}_\n\n---\n\n")
    path.write_text(header + content.strip() + "\n", encoding="utf-8")
    print(f"Archived: {path.name}")

# ── Main ──────────────────────────────────────────────────────────────────────

PARSERS = {"ai": parse_ai, "china_ai": parse_china, "ev": parse_ev}
NOTE_LABELS = {"ai": "Host Note", "china_ai": "Editor Note", "ev": "Editor Note"}


def run_section(section: dict, max_age_hours: int) -> dict | None:
    print(f"\n=== {section['name']} ===")
    seen = prune_seen(load_seen(section["seen_log"]))
    articles = fetch_articles(section["feeds"], max_age_hours=max_age_hours)
    print(f"{len(articles)} articles retrieved")
    articles = [a for a in articles if a["url"] not in seen]
    articles = dedupe_by_title(articles)[:MAX_ARTICLES_PER_RUN]
    print(f"{len(articles)} new after dedupe")
    if len(articles) < MIN_NEW_STORIES:
        print("Not enough new stories — skipping section.")
        save_seen(section["seen_log"], seen)
        return None

    enrich_with_full_text(articles)
    got = sum(1 for a in articles if a.get("full_text"))
    print(f"Full text retrieved for {got}/{len(articles)} articles")

    content, usage = generate_section(section, articles)
    archive_section(section, content, len(articles))
    stories, note = PARSERS[section["key"]](content)
    if not stories:
        print("WARNING: could not parse any stories from model output", file=sys.stderr)
        return None
    for s in stories:
        seen[s["link"]] = datetime.now(timezone.utc).isoformat()
    save_seen(section["seen_log"], seen)
    return {"section": section, "stories": stories, "note": note,
            "note_label": NOTE_LABELS[section["key"]], "usage": usage}


def run_slot_name() -> str:
    hour = datetime.now().hour
    if hour < 11:
        return "Morning"
    if hour < 17:
        return "Midday"
    return "Evening"


def main() -> None:
    ap = argparse.ArgumentParser(description="Unified Prompt AI News briefing")
    ap.add_argument("--section", choices=["ai", "china_ai", "ev"], help="Run only one section")
    ap.add_argument("--max-age", type=int, default=8, help="Max article age in hours (default 8)")
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()

    sections = SECTIONS if not args.section else [s for s in SECTIONS if s["key"] == args.section]
    results = []
    try:
        for section in sections:
            try:
                r = run_section(section, args.max_age)
                if r:
                    results.append(r)
            except Exception as exc:
                print(f"SECTION FAILED ({section['name']}): {exc}", file=sys.stderr)
                traceback.print_exc()

        if not results:
            print("No sections produced stories — sending quiet-day note.")
            if not args.no_email:
                send_email(
                    f"[Prompt AI] {run_slot_name()} Briefing — all quiet",
                    "<html><body style='font-family:Arial,sans-serif;padding:24px;'>"
                    "<p>Nothing new across AI, China &amp; AI, or EV feeds since the last briefing.</p></body></html>",
                    "Nothing new across AI, China & AI, or EV feeds since the last briefing.",
                )
            return

        email_html = build_unified_email(results)
        slot = run_slot_name()
        section_keys = " + ".join(r["section"]["key"] for r in results)
        subject = f"[Prompt AI] {slot} Briefing ({section_keys}) — {datetime.now().strftime('%b %d, %I:%M %p')}"
        plain = "\n\n".join(
            f"{r['section']['name'].upper()}\n" +
            "\n\n".join(f"{i}. {s['headline']} ({s['source']})\n{s['body']}\n{s['link']}"
                        for i, s in enumerate(r["stories"], start=1))
            for r in results
        )
        if not args.no_email:
            send_email(subject, email_html, plain)
        else:
            print("\n--no-email mode; would have sent:", subject)

    except Exception as exc:
        traceback.print_exc()
        send_failure_alert(f"{exc}\n\n{traceback.format_exc()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
