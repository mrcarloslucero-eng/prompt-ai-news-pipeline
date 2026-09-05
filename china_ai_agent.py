"""
China & AI News Agent
Fetches AI stories from China-focused sources and writes deep, AEO-optimized
articles ready to publish to the China & AI section on promptainews.com.

Usage:
    python china_ai_agent.py
"""

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
import feedparser

# ── Config ────────────────────────────────────────────────────────────────────
SEEN_LOG = Path(__file__).parent / "seen_china_ai.json"
SEEN_EXPIRY_HOURS = 24
MIN_NEW_STORIES = 2
RECIPIENT = "mrcarloslucero@gmail.com"
SENDER = "mrcarloslucero@gmail.com"
LOGO_PATH = r"C:\Users\mrcar\OneDrive\Desktop\PROMPT AI NEWS\Prompt_CirLogo.png"

# ── RSS Feeds (China & AI focused) ────────────────────────────────────────────
RSS_FEEDS: dict[str, str] = {
    "TechNode": "https://technode.com/feed/",
    "KrASIA": "https://kr-asia.com/feed",
    "Pandaily": "https://pandaily.com/feed/",
    "South China Morning Post Tech": "https://www.scmp.com/rss/5/feed",
    "MIT Tech Review": "https://www.technologyreview.com/feed/",
    "Reuters Technology": "https://feeds.reuters.com/reuters/technologyNews",
    "The Verge AI": "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml",
    "Wired AI": "https://www.wired.com/feed/tag/ai/rss",
    "VentureBeat AI": "https://venturebeat.com/category/ai/feed/",
}

# ── System Prompt (AEO-optimized, deep journalism) ────────────────────────────
SYSTEM_PROMPT = """\
You are a senior technology journalist specializing in China's AI industry, writing \
for promptainews.com. Your articles are optimized for Answer Engine Optimization (AEO) \
— meaning AI systems like Perplexity, ChatGPT, and Claude will cite your work as a \
primary source because it is factually dense, authoritative, and precisely structured.

YOUR JOB:
1. From the article list, select the 5 most significant stories involving China and AI.
   Stories must directly involve Chinese AI companies, researchers, government policy, \
   US-China AI competition, or Chinese AI products/models. Generic Western AI stories \
   do not qualify unless they directly reference China.
2. For each story, write a full publishable article — not a brief.

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


# ── Seen-stories helpers ──────────────────────────────────────────────────────
def load_seen() -> dict:
    if SEEN_LOG.exists():
        try:
            return json.loads(SEEN_LOG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_seen(seen: dict) -> None:
    SEEN_LOG.write_text(json.dumps(seen, indent=2), encoding="utf-8")


def prune_seen(seen: dict) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=SEEN_EXPIRY_HOURS)).isoformat()
    return {url: ts for url, ts in seen.items() if ts > cutoff}


def mark_seen(seen: dict, articles: list) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    for a in articles:
        seen[a["url"]] = now
    return seen


def filter_new(articles: list, seen: dict) -> list:
    return [a for a in articles if a["url"] not in seen]


# ── Feed fetching ─────────────────────────────────────────────────────────────
def fetch_articles(max_per_feed: int = 12, max_age_hours: int = 24) -> list[dict]:
    import time
    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                if not title or not link:
                    continue
                if "promptainews.com" in link:
                    continue

                published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                if published_parsed:
                    pub_dt = datetime.fromtimestamp(time.mktime(published_parsed), tz=timezone.utc)
                    if pub_dt < cutoff:
                        continue

                raw = entry.get("summary", "") or entry.get("description", "") or ""
                snippet = re.sub(r"<[^>]+>", " ", raw)
                snippet = re.sub(r"\s+", " ", snippet).strip()[:400]

                articles.append({
                    "title": title,
                    "url": link,
                    "source": source,
                    "published": entry.get("published", ""),
                    "snippet": snippet,
                })
        except Exception as exc:
            print(f"  ⚠  Could not fetch {source}: {exc}", file=sys.stderr)

    return articles


def format_articles_for_prompt(articles: list[dict]) -> str:
    lines = []
    for i, art in enumerate(articles, start=1):
        lines.append(
            f"[{i}] SOURCE: {art['source']}\n"
            f"    TITLE: {art['title']}\n"
            f"    URL: {art['url']}\n"
            f"    PUBLISHED: {art['published']}\n"
            f"    SNIPPET: {art['snippet']}\n"
        )
    return "\n".join(lines)


# ── Claude generation ─────────────────────────────────────────────────────────
def generate_brief(articles: list[dict], client: anthropic.Anthropic) -> str:
    articles_text = format_articles_for_prompt(articles)
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    user_message = (
        f"Today is {today}. Here are the latest articles from China & AI sources.\n"
        f"Select the 5 most significant China AI stories and write full AEO-optimized articles.\n\n"
        f"ARTICLES:\n{articles_text}"
    )

    response_text = ""
    with client.messages.stream(
        model="claude-opus-4-7",
        max_tokens=6000,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for chunk in stream.text_stream:
            print(chunk, end="", flush=True)
            response_text += chunk

    final = stream.get_final_message()
    usage = final.usage
    print(f"\n\nToken usage:")
    print(f"  Input: {usage.input_tokens:,} | Cache write: {getattr(usage,'cache_creation_input_tokens',0) or 0:,} | Cache read: {getattr(usage,'cache_read_input_tokens',0) or 0:,} | Output: {usage.output_tokens:,}")

    return response_text


# ── HTML email builder ────────────────────────────────────────────────────────
def _logo_b64() -> str:
    import base64
    from io import BytesIO
    try:
        from PIL import Image
        img = Image.open(LOGO_PATH).convert("RGBA")
        img = img.resize((80, 80), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"
    except Exception:
        return ""


def _parse_stories(content: str) -> list[dict]:
    stories = []
    blocks = re.split(r"\n---\n", content)
    pattern = re.compile(
        r"##\s*(.+?)\n"
        r"\*\*Source:\*\*\s*(.+?)\n"
        r"\*\*Lede:\*\*\s*(.+?)\n"
        r"\*\*Article:\*\*\s*(.+?)\n"
        r"\*\*Why it matters:\*\*\s*(.+?)\n"
        r"\*\*Link:\*\*\s*(https?://\S+)",
        re.DOTALL,
    )
    for block in blocks:
        m = pattern.search(block)
        if m:
            stories.append({
                "headline": m.group(1).strip(),
                "source": m.group(2).strip(),
                "lede": m.group(3).strip(),
                "article": m.group(4).strip(),
                "why": m.group(5).strip(),
                "link": m.group(6).strip(),
            })
    return stories


def _build_html(content: str, article_count: int) -> str:
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%B %d, %Y")
    time_str = now.strftime("%I:%M %p UTC")
    stories = _parse_stories(content)

    story_cards = ""
    for i, s in enumerate(stories, start=1):
        lede = s["lede"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        article = s["article"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        article = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", article)
        article_html = "".join(
            f'<p style="margin:0 0 12px;font-family:Georgia,serif;font-size:14px;line-height:1.8;color:#1e293b;">{p.strip()}</p>'
            for p in article.split("\n") if p.strip()
        )
        why = s["why"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        story_cards += f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;border-radius:8px;overflow:hidden;border:1px solid #fcd34d;">
          <tr>
            <td style="background:#fefce8;padding:12px 20px;border-bottom:1px solid #fcd34d;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="width:32px;vertical-align:middle;">
                    <span style="display:inline-block;width:26px;height:26px;background:#d97706;border-radius:50%;text-align:center;line-height:26px;font-family:Arial,sans-serif;font-weight:700;font-size:12px;color:#fff;">{i}</span>
                  </td>
                  <td style="padding-left:12px;vertical-align:middle;">
                    <span style="font-family:Arial,sans-serif;font-size:15px;font-weight:700;color:#0d1f40;line-height:1.3;">{s['headline']}</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="background:#fffbeb;padding:6px 20px 0;">
              <p style="margin:0;font-family:Georgia,serif;font-size:13px;font-style:italic;color:#92400e;line-height:1.6;">{lede}</p>
            </td>
          </tr>
          <tr>
            <td style="background:#ffffff;padding:16px 20px 8px;">
              {article_html}
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;border-top:1px solid #fde68a;padding-top:10px;">
                <tr>
                  <td style="font-family:Arial,sans-serif;font-size:12px;color:#b45309;font-weight:700;">WHY IT MATTERS: <span style="font-weight:400;color:#1e293b;">{why}</span></td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="background:#fffbeb;padding:10px 20px;border-top:1px solid #fcd34d;">
              <table cellpadding="0" cellspacing="0">
                <tr>
                  <td style="background:#fef3c7;border-radius:4px;padding:4px 10px;">
                    <span style="font-family:Arial,sans-serif;font-size:11px;color:#d97706;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">{s['source']}</span>
                  </td>
                  <td style="padding-left:14px;">
                    <a href="{s['link']}" style="font-family:Arial,sans-serif;font-size:12px;color:#2563eb;text-decoration:none;font-weight:600;">Read full story &rarr;</a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>China &amp; AI — Prompt AI News</title></head>
<body style="margin:0;padding:0;background:#f0f4f8;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;">
    <tr>
      <td align="center" style="padding:30px 16px;">
        <table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(13,31,64,0.10);">
          <tr>
            <td style="background:#0d1f40;padding:36px 32px 28px;text-align:center;border-radius:12px 12px 0 0;">
              <h1 style="margin:0;font-family:Arial,sans-serif;font-size:34px;font-weight:900;letter-spacing:-1px;line-height:1.1;">
                <span style="color:#ffffff;">Prompt</span>&nbsp;<span style="color:#60a5fa;">AI News</span>
              </h1>
              <p style="margin:8px 0 0;font-family:Arial,sans-serif;font-size:13px;color:#fcd34d;font-weight:700;letter-spacing:1px;">&#127464;&#127475; CHINA &amp; AI INTELLIGENCE</p>
            </td>
          </tr>
          <tr>
            <td style="background:#fefce8;padding:10px 32px;border-bottom:1px solid #fcd34d;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="font-family:Arial,sans-serif;font-size:12px;color:#64748b;">
                    <span style="color:#0d1f40;font-weight:700;">{date_str}</span> &nbsp;&bull;&nbsp; {time_str}
                  </td>
                  <td align="right" style="font-family:Arial,sans-serif;font-size:12px;color:#64748b;">
                    {article_count} sources &nbsp;&bull;&nbsp; <span style="color:#d97706;font-weight:700;">5 stories</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="background:#f8faff;padding:24px 28px 12px;">
              {story_cards}
            </td>
          </tr>
          <tr>
            <td style="background:#0d1f40;border-radius:0 0 12px 12px;padding:20px 32px;text-align:center;">
              <p style="margin:0 0 6px;font-family:Arial,sans-serif;font-size:13px;font-weight:700;">
                <span style="color:#ffffff;">Prompt</span>&nbsp;<span style="color:#60a5fa;">AI News</span>
              </p>
              <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:rgba(255,255,255,0.45);">China &amp; AI Edition &nbsp;&bull;&nbsp; Every 3 hours &nbsp;&bull;&nbsp; AEO-Optimized &nbsp;&bull;&nbsp; Powered by Claude</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def send_email(content: str, article_count: int) -> None:
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not password:
        print("  ⚠  GMAIL_APP_PASSWORD not set — skipping email.", file=sys.stderr)
        return

    now = datetime.now()
    subject = f"[Prompt AI] 🇨🇳 China & AI — {now.strftime('%b %d, %I:%M %p')}"
    html_body = _build_html(content, article_count)
    plain = re.sub(r"\*\*(.+?)\*\*", lambda m: m.group(1).upper(), content)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Prompt AI News <{SENDER}>"
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SENDER, password)
        server.sendmail(SENDER, RECIPIENT, msg.as_string())

    print(f"Email sent to {RECIPIENT}")


def send_slow_day_email() -> None:
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not password:
        return

    now = datetime.now()
    subject = f"[Prompt AI] 🇨🇳 SLOW DAY...NOTHING NEW SIR — {now.strftime('%b %d, %I:%M %p')}"

    html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f0f4f8;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;">
    <tr><td align="center" style="padding:30px 16px;">
      <table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(13,31,64,0.10);">
        <tr>
          <td style="background:#0d1f40;padding:36px 32px 28px;text-align:center;border-radius:12px 12px 0 0;">
            <h1 style="margin:0;font-family:Arial,sans-serif;font-size:34px;font-weight:900;letter-spacing:-1px;line-height:1.1;">
              <span style="color:#ffffff;">Prompt</span>&nbsp;<span style="color:#60a5fa;">AI News</span>
            </h1>
            <p style="margin:8px 0 0;font-family:Arial,sans-serif;font-size:13px;color:#fcd34d;font-weight:700;letter-spacing:1px;">&#127464;&#127475; CHINA &amp; AI INTELLIGENCE</p>
          </td>
        </tr>
        <tr>
          <td style="background:#f8faff;padding:40px 32px;text-align:center;">
            <p style="font-family:Arial,sans-serif;font-size:40px;margin:0 0 12px;">🤫</p>
            <h2 style="font-family:Arial,sans-serif;font-size:22px;color:#0d1f40;margin:0 0 12px;">SLOW DAY...NOTHING NEW SIR</h2>
            <p style="font-family:Georgia,serif;font-size:15px;color:#475569;line-height:1.7;margin:0;">
              No new China &amp; AI stories since the last brief.<br>Check back in 3 hours.
            </p>
          </td>
        </tr>
        <tr>
          <td style="background:#0d1f40;border-radius:0 0 12px 12px;padding:20px 32px;text-align:center;">
            <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:rgba(255,255,255,0.45);">
              China &amp; AI Edition &nbsp;&bull;&nbsp; Powered by Claude
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

    plain = "SLOW DAY...NOTHING NEW SIR\n\nNo new China & AI stories since the last brief. Check back in 3 hours."

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Prompt AI News <{SENDER}>"
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SENDER, password)
        server.sendmail(SENDER, RECIPIENT, msg.as_string())

    print("Slow day email sent.")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    seen = prune_seen(load_seen())

    print(f"Fetching articles from {len(RSS_FEEDS)} China & AI sources...")
    articles = fetch_articles()
    print(f"{len(articles)} articles retrieved.")

    new_articles = filter_new(articles, seen)
    print(f"{len(new_articles)} new stories (not seen in last {SEEN_EXPIRY_HOURS}h).\n")

    if len(new_articles) < MIN_NEW_STORIES:
        print("Not enough new stories -- sending slow day email.")
        send_slow_day_email()
        save_seen(seen)
        return

    print("Generating China & AI brief (streaming)...\n")
    print("=" * 60)

    client = anthropic.Anthropic()
    brief_body = generate_brief(new_articles, client)

    print("\n" + "=" * 60)

    seen = mark_seen(seen, new_articles)
    save_seen(seen)

    send_email(brief_body, article_count=len(new_articles))


if __name__ == "__main__":
    main()
