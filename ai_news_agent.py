"""
AI News Brief Agent
Fetches top AI stories from multiple RSS feeds and generates
broadcast-ready summaries for YouTube/livestream hosts.

Usage:
    python ai_news_agent.py
    python ai_news_agent.py --output-dir ./briefs
    python ai_news_agent.py --feeds techcrunch reddit --no-save
"""

import argparse
import html
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TypedDict

import anthropic
import feedparser

# ── Seen-stories log ───────────────────────────────────────────────────────────
# Tracks URLs already sent so we don't repeat stories across 3-hour runs.
# Entries expire after 24 hours so stories can resurface the next day if still relevant.

SEEN_LOG = Path(__file__).parent / "seen_stories.json"
SEEN_EXPIRY_HOURS = 24
MIN_NEW_STORIES = 3   # If fewer than this many new stories exist, send "nothing new" email
NUM_STORIES = 5       # Number of stories Claude selects and includes in each brief


def load_seen() -> dict:
    """Load the seen-stories log, return empty dict if missing or corrupt."""
    if SEEN_LOG.exists():
        try:
            return json.loads(SEEN_LOG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_seen(seen: dict) -> None:
    """Save seen-stories log to disk."""
    SEEN_LOG.write_text(json.dumps(seen, indent=2), encoding="utf-8")


def prune_seen(seen: dict) -> dict:
    """Remove entries older than SEEN_EXPIRY_HOURS."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=SEEN_EXPIRY_HOURS)).isoformat()
    return {url: ts for url, ts in seen.items() if ts > cutoff}


def mark_seen(seen: dict, articles: list) -> dict:
    """Add sent article URLs to the seen log."""
    now = datetime.now(timezone.utc).isoformat()
    for a in articles:
        seen[a["url"]] = now
    return seen


def filter_new(articles: list, seen: dict) -> list:
    """Return only articles whose URL has not been seen yet."""
    return [a for a in articles if a["url"] not in seen]

LOGO_PATH = Path(__file__).parent / "Prompt_CirLogo.png"

# ── RSS feed registry ──────────────────────────────────────────────────────────

RSS_FEEDS: dict[str, str] = {
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "NY Times Tech": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "Reuters Technology": "https://feeds.reuters.com/reuters/technologyNews",
    "AP News Technology": "https://rsshub.app/apnews/topics/technology",
    "The Verge AI": "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml",
    "Wired AI": "https://www.wired.com/category/business/artificial-intelligence/feed/",
    "MIT Tech Review": "https://www.technologyreview.com/feed/",
    "VentureBeat AI": "https://venturebeat.com/category/ai/feed/",
    "Ars Technica AI": "https://arstechnica.com/tag/ai/feed/",
    "Reddit r/artificial": "https://www.reddit.com/r/artificial/.rss",
    "Reddit r/MachineLearning": "https://www.reddit.com/r/MachineLearning/.rss",
    "Reddit r/singularity": "https://www.reddit.com/r/singularity/.rss",
    "Data Center Knowledge": "https://www.datacenterknowledge.com/feed",
    "The Register": "https://www.theregister.com/data_centre/headlines.atom",
    "Politico Technology": "https://www.politico.com/rss/technology.xml",
    "EURACTIV AI": "https://www.euractiv.com/section/artificial-intelligence/feed/",
    "Pew Research": "https://www.pewresearch.org/feed/",
    "Vox Recode": "https://www.vox.com/recode/rss.xml",
    "The Guardian AI": "https://www.theguardian.com/technology/artificialintelligence/rss",
    "Hacker News": "https://news.ycombinator.com/rss",
    "Anthropic News": "https://www.anthropic.com/news/rss.xml",
    "OpenAI News": "https://openai.com/news/rss.xml",
    "HN AI": "https://hnrss.org/newest?q=AI",
}

# Stable system prompt — cached on every run after the first call
SYSTEM_PROMPT = f"""\
You are a sharp, engaging tech correspondent writing briefs for an AI-focused \
YouTube channel and live show. Your audience loves the cutting edge: new models, \
industry power moves, research breakthroughs, product launches, and policy shifts \
that actually affect how people use AI.

YOUR JOB:
1. From the article list provided, select the {NUM_STORIES} most compelling AI stories of the day.
2. For each story, write a tight, broadcast-ready brief that a host can read on air.

SELECTION CRITERIA (in order of priority):
- Story must be directly and substantively about AI, ML, LLMs, robotics with AI, \
or a major AI company's strategic move. "Software update mentions AI" does NOT qualify.
- Prefer stories with mass-market impact or that signal a meaningful industry shift.
- Prefer recency — a story from the last 12 hours beats one from yesterday.
- No duplicate angles: if three sources cover the same announcement, pick one.

BRIEF STYLE:
- 3 - 4 sentences per story. Lead with the most interesting fact, not the company name.
- Sound like a smart human wrote it — direct, opinionated, no filler phrases like \
"In a world where AI is changing everything..." or "This just in..."
- No jargon without a quick plain-English gloss.
- No bullet points inside a brief — flowing prose only.
- Each brief should leave the audience wanting to know more.

OUTPUT FORMAT — use exactly this markdown structure, nothing before the first ##:

## [Story Number]. [Punchy Headline You Write]
**Source:** [Publication name]
**Brief:** [3 -4 sentence broadcast-ready summary]
**Link:** [URL]

---

After the {NUM_STORIES} stories, add a short (1 sentence) "Host Note" with a quick suggestion \
on which story to lead with and why, labeled:

**Host Note:** [your suggestion]
"""


# ── Data types ─────────────────────────────────────────────────────────────────

class Article(TypedDict):
    title: str
    url: str
    source: str
    published: str
    snippet: str


# ── Feed fetching ──────────────────────────────────────────────────────────────

def fetch_articles(
    feeds: dict[str, str],
    max_per_feed: int = 12,
    max_age_hours: int = 24,
) -> list[Article]:
    """Parse RSS feeds and return a flat list of recent articles."""
    import re
    import time

    articles: list[Article] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    for source, url in feeds.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                if not title or not link:
                    continue

                # Never report on our own site
                if "promptainews.com" in link:
                    continue

                # Filter by age — skip articles older than max_age_hours
                published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                if published_parsed:
                    pub_dt = datetime.fromtimestamp(
                        time.mktime(published_parsed), tz=timezone.utc
                    )
                    if pub_dt < cutoff:
                        continue

                raw = (
                    entry.get("summary", "")
                    or entry.get("description", "")
                    or ""
                )
                snippet = re.sub(r"<[^>]+>", " ", raw)
                snippet = re.sub(r"\s+", " ", snippet).strip()[:400]

                articles.append(
                    Article(
                        title=title,
                        url=link,
                        source=source,
                        published=entry.get("published", ""),
                        snippet=snippet,
                    )
                )
        except Exception as exc:
            print(f"  ⚠  Could not fetch {source}: {exc}", file=sys.stderr)

    return articles


def format_articles_for_prompt(articles: list[Article]) -> str:
    """Render the article list as a numbered prompt block."""
    lines: list[str] = []
    for i, art in enumerate(articles, start=1):
        lines.append(
            f"[{i}] SOURCE: {art['source']}\n"
            f"    TITLE: {art['title']}\n"
            f"    URL: {art['url']}\n"
            f"    PUBLISHED: {art['published']}\n"
            f"    SNIPPET: {art['snippet']}\n"
        )
    return "\n".join(lines)


# ── Claude summarisation ───────────────────────────────────────────────────────

def generate_brief(
    articles: list[Article],
    client: anthropic.Anthropic,
) -> str:
    """
    Send the article list to Claude with a cached system prompt and return
    the formatted markdown brief.
    """
    articles_text = format_articles_for_prompt(articles)
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    user_message = (
        f"Today is {today}. Here are the latest articles from our monitored sources.\n"
        f"Select the top {NUM_STORIES} AI stories and write the broadcast briefs.\n\n"
        f"ARTICLES:\n{articles_text}"
    )

    # Prompt caching: cache_control on the system text so the stable system
    # prompt is cached after the first call (~1024+ token minimum for caching).
    response_text = ""
    with client.messages.stream(
        model="claude-sonnet-4-5",
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # cache the system prompt
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for chunk in stream.text_stream:
            print(chunk, end="", flush=True)
            response_text += chunk

    # Print usage so the user can see cache hits
    final = stream.get_final_message()
    usage = final.usage
    print(f"\n\n─── Token usage ───────────────────────────────────────────")
    print(f"  Input (uncached):  {usage.input_tokens:,}")
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    print(f"  Cache write:       {cache_create:,}")
    print(f"  Cache read:        {cache_read:,}")
    print(f"  Output tokens:     {usage.output_tokens:,}")
    print(f"───────────────────────────────────────────────────────────")

    return response_text


# ── Output ─────────────────────────────────────────────────────────────────────

def build_full_brief(brief_body: str, article_count: int) -> str:
    """Wrap the Claude output in a dated document header."""
    now = datetime.now(timezone.utc)
    header = (
        f"# AI News Brief — {now.strftime('%B %d, %Y')}\n"
        f"_Generated {now.strftime('%H:%M UTC')} · "
        f"{article_count} articles scanned_\n\n"
        f"---\n\n"
    )
    return header + brief_body.strip() + "\n"


def _parse_brief_to_stories(content: str) -> list[dict]:
    """Extract individual story blocks from the Claude markdown output."""
    import re
    stories = []
    blocks = re.split(r"\n---\n", content)
    story_pattern = re.compile(
        r"##\s*(.+?)\n"
        r"\*\*Source:\*\*\s*(.+?)\n"
        r"\*\*Brief:\*\*\s*(.+?)\n"
        r"\*\*Link:\*\*\s*(https?://\S+)",
        re.DOTALL,
    )
    host_note_pattern = re.compile(r"\*\*Host Note:\*\*\s*(.+)", re.DOTALL)

    for block in blocks:
        m = story_pattern.search(block)
        if m:
            stories.append(
                {
                    "headline": m.group(1).strip(),
                    "source": m.group(2).strip(),
                    "brief": m.group(3).strip(),
                    "link": m.group(4).strip(),
                    "host_note": False,
                }
            )
        hn = host_note_pattern.search(block)
        if hn and stories:
            stories[-1]["host_note_text"] = hn.group(1).strip()

    return stories


def _logo_b64() -> str:
    """Return the Prompt AI News circular logo as a base64 data URI, or empty string on failure.
    Resizes to 80x80 before encoding to keep the email under Gmail's 102KB clip limit."""
    import base64
    from io import BytesIO
    try:
        from PIL import Image
        img = Image.open(LOGO_PATH).convert("RGBA")
        img = img.resize((80, 80), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        data = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{data}"
    except FileNotFoundError:
        print(f"⚠  Logo not found at {LOGO_PATH} — email will send without it.", file=sys.stderr)
        return ""
    except Exception as exc:
        print(f"⚠  Logo failed to load: {exc}", file=sys.stderr)
        return ""


def _build_branded_html(content: str, article_count: int) -> str:
    """Render the brief as a fully branded Prompt AI News Channel HTML email."""
    import re

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%B %d, %Y")
    time_str = now.strftime("%I:%M %p UTC")
    stories = _parse_brief_to_stories(content)

    # Pull host note from last story if present
    host_note = ""
    if stories and "host_note_text" in stories[-1]:
        host_note = stories[-1].pop("host_note_text")

    logo_src = _logo_b64()
    logo_html = (
        f'<img src="{logo_src}" width="80" height="80" alt="Prompt AI News" '
        f'style="display:block;margin:0 auto 16px;border-radius:50%;" />'
        if logo_src else ""
    )

    story_cards = ""
    for i, s in enumerate(stories, start=1):
        brief_escaped = s["brief"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        brief_escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", brief_escaped)
        headline_safe = html.escape(s["headline"])
        source_safe = html.escape(s["source"])
        link_safe = s["link"] if s["link"].startswith(("http://", "https://")) else "#"
        story_cards += f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;border-radius:8px;overflow:hidden;border:1px solid #bfdbfe;">
          <tr>
            <td style="background:#eff6ff;padding:12px 20px;border-bottom:1px solid #bfdbfe;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="width:32px;vertical-align:middle;">
                    <span style="display:inline-block;width:26px;height:26px;background:#3b82f6;border-radius:50%;text-align:center;line-height:26px;font-family:Arial,sans-serif;font-weight:700;font-size:12px;color:#fff;">{i}</span>
                  </td>
                  <td style="padding-left:12px;vertical-align:middle;">
                    <span style="font-family:Arial,sans-serif;font-size:15px;font-weight:700;color:#0d1f40;line-height:1.3;">{headline_safe}</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="background:#ffffff;padding:16px 20px;">
              <p style="margin:0 0 12px;font-family:Georgia,serif;font-size:14px;line-height:1.75;color:#1e293b;">{brief_escaped}</p>
              <table cellpadding="0" cellspacing="0">
                <tr>
                  <td style="background:#eff6ff;border-radius:4px;padding:4px 10px;">
                    <span style="font-family:Arial,sans-serif;font-size:11px;color:#3b82f6;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">{source_safe}</span>
                  </td>
                  <td style="padding-left:14px;">
                    <a href="{link_safe}" style="font-family:Arial,sans-serif;font-size:12px;color:#2563eb;text-decoration:none;font-weight:600;">Read full story &rarr;</a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>"""

    host_note_block = ""
    if host_note:
        hn_escaped = host_note.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        host_note_block = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;margin-bottom:24px;border-radius:8px;overflow:hidden;border:1px solid #3b82f6;">
          <tr>
            <td style="background:#eff6ff;padding:16px 20px;">
              <p style="margin:0 0 6px;font-family:Arial,sans-serif;font-size:11px;color:#2563eb;font-weight:700;text-transform:uppercase;letter-spacing:1px;">&#127908; Host Note</p>
              <p style="margin:0;font-family:Georgia,serif;font-size:14px;color:#1e293b;line-height:1.65;">{hn_escaped}</p>
            </td>
          </tr>
        </table>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Prompt AI News</title></head>
<body style="margin:0;padding:0;background:#f0f4f8;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;">
    <tr>
      <td align="center" style="padding:30px 16px;">
        <table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(13,31,64,0.10);">

          <!-- Header -->
          <tr>
            <td style="background:#0d1f40;padding:36px 32px 28px;text-align:center;border-radius:12px 12px 0 0;">
              <h1 style="margin:0;font-family:Arial,sans-serif;font-size:38px;font-weight:900;letter-spacing:-1px;line-height:1.1;">
                <span style="color:#ffffff;">Prompt</span>&nbsp;<span style="color:#60a5fa;">AI News</span>
              </h1>
              <p style="margin:10px 0 0;font-family:Arial,sans-serif;font-size:12px;color:rgba(255,255,255,0.60);letter-spacing:0.5px;">AI News for Everyday People</p>
            </td>
          </tr>

          <!-- Date/Meta bar -->
          <tr>
            <td style="background:#eff6ff;padding:10px 32px;border-bottom:1px solid #bfdbfe;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="font-family:Arial,sans-serif;font-size:12px;color:#64748b;">
                    <span style="color:#0d1f40;font-weight:700;">{date_str}</span>
                    &nbsp;&bull;&nbsp;{time_str}
                  </td>
                  <td align="right" style="font-family:Arial,sans-serif;font-size:12px;color:#64748b;">
                    {article_count} sources &nbsp;&bull;&nbsp; <span style="color:#3b82f6;font-weight:700;">{NUM_STORIES} stories</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="background:#f8faff;padding:24px 28px 12px;">
              {story_cards}
              {host_note_block}
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background:#0d1f40;border-radius:0 0 12px 12px;padding:20px 32px;text-align:center;">
              <p style="margin:0 0 6px;font-family:Arial,sans-serif;font-size:13px;font-weight:700;">
                <span style="color:#ffffff;">Prompt</span>&nbsp;<span style="color:#60a5fa;">AI News</span>
              </p>
              <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:rgba(255,255,255,0.45);">Delivered every 3 hours &nbsp;&bull;&nbsp; Powered by Claude &nbsp;&bull;&nbsp; Built for creators</p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def send_email_brief(content: str, recipient: str, article_count: int = 0) -> None:
    """Email the brief via Gmail SMTP using an App Password from GMAIL_APP_PASSWORD env var."""
    import re

    sender = "mrcarloslucero@gmail.com"
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not password:
        print(
            "  ⚠  GMAIL_APP_PASSWORD env var not set — skipping email.",
            file=sys.stderr,
        )
        return

    now = datetime.now()
    subject = f"[Prompt AI] 7 Stories — {now.strftime('%b %d, %I:%M %p')}"

    html_body = _build_branded_html(content, article_count)

    # Plain-text fallback: inline **bold** → CAPS, strip markdown symbols
    plain = re.sub(r"\*\*(.+?)\*\*", lambda m: m.group(1).upper(), content)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Prompt AI News <{sender}>"
    msg["To"] = recipient
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())

    print(f"📧  Brief emailed to {recipient}")


def save_brief(content: str, output_dir: Path) -> Path:
    """Write the brief to a timestamped markdown file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"ai_brief_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    path = output_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an AI news brief for YouTube/livestream."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./briefs"),
        help="Directory to save the markdown brief (default: ./briefs)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Print the brief but don't save it to disk",
    )
    parser.add_argument(
        "--feeds",
        nargs="*",
        choices=[k.lower().replace(" ", "_") for k in RSS_FEEDS],
        default=None,
        help="Restrict to specific feed names (default: all feeds)",
    )
    parser.add_argument(
        "--max-per-feed",
        type=int,
        default=12,
        help="Max articles to pull per feed (default: 12)",
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=24,
        help="Only include articles published within this many hours (default: 24)",
    )
    parser.add_argument(
        "--email",
        type=str,
        default="mrcarloslucero@gmail.com",
        help="Recipient address for the emailed brief (default: mrcarloslucero@gmail.com)",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Skip emailing the brief (just save/print it)",
    )
    return parser.parse_args()


def resolve_feeds(feed_keys: list[str] | None) -> dict[str, str]:
    if not feed_keys:
        return RSS_FEEDS
    key_map = {k.lower().replace(" ", "_"): k for k in RSS_FEEDS}
    return {key_map[k]: RSS_FEEDS[key_map[k]] for k in feed_keys if k in key_map}


# ── Main ───────────────────────────────────────────────────────────────────────

def send_nothing_new_email(recipient: str) -> None:
    """Send a short 'nothing new' notification instead of a full digest."""
    sender = "mrcarloslucero@gmail.com"
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not password:
        print("  ⚠  GMAIL_APP_PASSWORD not set — skipping nothing-new email.", file=sys.stderr)
        return

    now = datetime.now()
    subject = f"[Prompt AI] Nothing New, Sir — {now.strftime('%b %d, %I:%M %p')}"

    html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f0f4f8;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;">
    <tr><td align="center" style="padding:30px 16px;">
      <table width="620" cellpadding="0" cellspacing="0"
             style="max-width:620px;width:100%;border-radius:12px;overflow:hidden;
                    box-shadow:0 4px 24px rgba(13,31,64,0.10);">
        <tr>
          <td style="background:#0d1f40;padding:36px 32px 28px;text-align:center;
                     border-radius:12px 12px 0 0;">
            <h1 style="margin:0;font-family:Arial,sans-serif;font-size:38px;font-weight:900;
                       letter-spacing:-1px;line-height:1.1;">
              <span style="color:#ffffff;">Prompt</span>&nbsp;
              <span style="color:#60a5fa;">AI News</span>
            </h1>
            <p style="margin:10px 0 0;font-family:Arial,sans-serif;font-size:12px;
                      color:rgba(255,255,255,0.60);letter-spacing:0.5px;">
              AI News for Everyday People
            </p>
          </td>
        </tr>
        <tr>
          <td style="background:#f8faff;padding:40px 32px;text-align:center;">
            <p style="font-family:Arial,sans-serif;font-size:40px;margin:0 0 12px;">🤫</p>
            <h2 style="font-family:Arial,sans-serif;font-size:22px;color:#0d1f40;
                       margin:0 0 12px;">Nothing New, Sir.</h2>
            <p style="font-family:Georgia,serif;font-size:15px;color:#475569;
                      line-height:1.7;margin:0;">
              No new AI stories since the last brief.<br>
              The feeds are quiet. Check back in 3 hours.
            </p>
          </td>
        </tr>
        <tr>
          <td style="background:#0d1f40;border-radius:0 0 12px 12px;padding:20px 32px;
                     text-align:center;">
            <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;
                      color:rgba(255,255,255,0.45);">
              Prompt AI News &nbsp;&bull;&nbsp; Powered by Claude &nbsp;&bull;&nbsp;
              Built for creators
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

    plain = "Nothing New, Sir.\n\nNo new AI stories since the last brief. Check back in 3 hours."

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Prompt AI News <{sender}>"
    msg["To"] = recipient
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())

    print(f"📭  'Nothing new' email sent to {recipient}")


def main() -> None:
    args = parse_args()
    feeds = resolve_feeds(args.feeds)

    # ── Load + prune seen-stories log ──────────────────────────────
    seen = prune_seen(load_seen())

    print(f"🔍  Fetching articles from {len(feeds)} sources...")
    articles = fetch_articles(feeds, max_per_feed=args.max_per_feed, max_age_hours=args.max_age)
    print(f"📰  {len(articles)} articles retrieved.")

    # ── Filter out already-seen stories ────────────────────────────
    new_articles = filter_new(articles, seen)
    print(f"🆕  {len(new_articles)} new stories (not seen in last {SEEN_EXPIRY_HOURS}h).\n")

    if not new_articles:
        print("No new stories — sending 'Nothing New' email.")
        if not args.no_email:
            send_nothing_new_email(args.email)
        save_seen(seen)
        return

    if len(new_articles) < MIN_NEW_STORIES:
        print(f"Only {len(new_articles)} new story/stories — below minimum of {MIN_NEW_STORIES}. Sending 'Nothing New'.")
        if not args.no_email:
            send_nothing_new_email(args.email)
        save_seen(seen)
        return

    print("🤖  Generating AI brief (streaming)...\n")
    print("=" * 60)

    client = anthropic.Anthropic()
    brief_body = generate_brief(new_articles, client)

    print("\n" + "=" * 60)

    full_brief = build_full_brief(brief_body, len(new_articles))

    if not args.no_save:
        saved_path = save_brief(full_brief, args.output_dir)
        print(f"\n✅  Brief saved to: {saved_path}")
    else:
        print("\n" + full_brief)

    # ── Mark sent stories as seen BEFORE sending email ─────────────
    # Save now so that even if SMTP fails, we don't re-send the same
    # stories on the next run.
    seen = mark_seen(seen, new_articles)
    save_seen(seen)

    if not args.no_email:
        send_email_brief(full_brief, args.email, article_count=len(new_articles))


if __name__ == "__main__":
    main()
