"""
EV News Agent
Fetches electric vehicle stories from top EV sources and generates
AEO-optimized email digests every 3 hours.

Usage:
    python ev_news_agent.py
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
SEEN_LOG = Path(__file__).parent / "seen_ev_news.json"
SEEN_EXPIRY_HOURS = 24
MIN_NEW_STORIES = 2
RECIPIENT = "mrcarloslucero@gmail.com"
SENDER = "mrcarloslucero@gmail.com"
LOGO_PATH = r"C:\Users\mrcar\OneDrive\Desktop\PROMPT AI NEWS\Prompt_CirLogo.png"

# ── RSS Feeds (EV focused) ─────────────────────────────────────────────────────
RSS_FEEDS: dict[str, str] = {
    "Electrek": "https://electrek.co/feed/",
    "InsideEVs": "https://insideevs.com/rss/articles/",
    "CleanTechnica": "https://cleantechnica.com/feed/",
    "The Verge Transportation": "https://www.theverge.com/transportation/rss/index.xml",
    "Ars Technica Cars": "https://feeds.arstechnica.com/arstechnica/cars",
    "TechCrunch EVs": "https://techcrunch.com/tag/electric-vehicles/feed/",
    "Reuters Autos": "https://feeds.reuters.com/reuters/businessNews",
    "Car and Driver EV": "https://www.caranddriver.com/rss/all.xml/",
}

# ── System Prompt (AEO-optimized) ─────────────────────────────────────────────
SYSTEM_PROMPT = """\
You are a senior automotive technology journalist covering the electric vehicle industry \
for promptainews.com. Your writing is optimized for Answer Engine Optimization (AEO) \
— AI systems like Perplexity, ChatGPT, and Claude cite your work because it is \
factually precise, direct, and structured for machine extraction.

YOUR JOB:
1. From the article list, select the 5 most significant EV stories. Stories must be \
   directly about electric vehicles, EV charging infrastructure, EV policy, EV battery \
   technology, or major automaker EV moves. Generic car news does not qualify.
2. For each story, write a tight, AEO-optimized news brief.

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
        f"Today is {today}. Here are the latest articles from EV sources.\n"
        f"Select the 5 most significant EV stories and write AEO-optimized briefs.\n\n"
        f"ARTICLES:\n{articles_text}"
    )

    response_text = ""
    with client.messages.stream(
        model="claude-opus-4-7",
        max_tokens=4096,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for chunk in stream.text_stream:
            print(chunk, end="", flush=True)
            response_text += chunk

    final = stream.get_final_message()
    usage = final.usage
    print("\n\nToken usage:")
    print(f"  Input: {usage.input_tokens:,} | Cache write: {getattr(usage,'cache_creation_input_tokens',0) or 0:,} | Cache read: {getattr(usage,'cache_read_input_tokens',0) or 0:,} | Output: {usage.output_tokens:,}")

    return response_text


# ── HTML email builder ────────────────────────────────────────────────────────
def _parse_stories(content: str) -> list[dict]:
    stories = []
    blocks = re.split(r"\n---\n", content)
    pattern = re.compile(
        r"##\s*(.+?)\n"
        r"\*\*Source:\*\*\s*(.+?)\n"
        r"\*\*Brief:\*\*\s*(.+?)\n"
        r"\*\*Bottom line:\*\*\s*(.+?)\n"
        r"\*\*Link:\*\*\s*(https?://\S+)",
        re.DOTALL,
    )
    editor_pattern = re.compile(r"\*\*Editor Note:\*\*\s*(.+)", re.DOTALL)
    editor_note = ""
    for block in blocks:
        m = pattern.search(block)
        if m:
            stories.append({
                "headline": m.group(1).strip(),
                "source": m.group(2).strip(),
                "brief": m.group(3).strip(),
                "bottom_line": m.group(4).strip(),
                "link": m.group(5).strip(),
            })
        en = editor_pattern.search(block)
        if en:
            editor_note = en.group(1).strip()
    return stories, editor_note


def _build_html(content: str, article_count: int) -> str:
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%B %d, %Y")
    time_str = now.strftime("%I:%M %p UTC")
    stories, editor_note = _parse_stories(content)

    story_cards = ""
    for i, s in enumerate(stories, start=1):
        brief = s["brief"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        brief = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", brief)
        brief_html = "".join(
            f'<p style="margin:0 0 10px;font-family:Georgia,serif;font-size:14px;line-height:1.75;color:#1e293b;">{p.strip()}</p>'
            for p in brief.split("\n") if p.strip()
        )
        bottom = s["bottom_line"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        story_cards += f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;border-radius:8px;overflow:hidden;border:1px solid #6ee7b7;">
          <tr>
            <td style="background:#ecfdf5;padding:12px 20px;border-bottom:1px solid #6ee7b7;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="width:32px;vertical-align:middle;">
                    <span style="display:inline-block;width:26px;height:26px;background:#059669;border-radius:50%;text-align:center;line-height:26px;font-family:Arial,sans-serif;font-weight:700;font-size:12px;color:#fff;">{i}</span>
                  </td>
                  <td style="padding-left:12px;vertical-align:middle;">
                    <span style="font-family:Arial,sans-serif;font-size:15px;font-weight:700;color:#0d1f40;line-height:1.3;">{s['headline']}</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="background:#ffffff;padding:16px 20px 8px;">
              {brief_html}
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;border-top:1px solid #d1fae5;padding-top:10px;">
                <tr>
                  <td style="font-family:Arial,sans-serif;font-size:12px;color:#065f46;font-weight:700;">BOTTOM LINE: <span style="font-weight:400;color:#1e293b;">{bottom}</span></td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="background:#ecfdf5;padding:10px 20px;border-top:1px solid #6ee7b7;">
              <table cellpadding="0" cellspacing="0">
                <tr>
                  <td style="background:#d1fae5;border-radius:4px;padding:4px 10px;">
                    <span style="font-family:Arial,sans-serif;font-size:11px;color:#059669;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">{s['source']}</span>
                  </td>
                  <td style="padding-left:14px;">
                    <a href="{s['link']}" style="font-family:Arial,sans-serif;font-size:12px;color:#2563eb;text-decoration:none;font-weight:600;">Read full story &rarr;</a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>"""

    editor_block = ""
    if editor_note:
        en_esc = editor_note.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        editor_block = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;margin-bottom:24px;border-radius:8px;overflow:hidden;border:1px solid #059669;">
          <tr>
            <td style="background:#ecfdf5;padding:16px 20px;">
              <p style="margin:0 0 6px;font-family:Arial,sans-serif;font-size:11px;color:#059669;font-weight:700;text-transform:uppercase;letter-spacing:1px;">&#9889; Editor Note</p>
              <p style="margin:0;font-family:Georgia,serif;font-size:14px;color:#1e293b;line-height:1.65;">{en_esc}</p>
            </td>
          </tr>
        </table>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EV News — Prompt AI News</title></head>
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
              <p style="margin:8px 0 0;font-family:Arial,sans-serif;font-size:13px;color:#6ee7b7;font-weight:700;letter-spacing:1px;">&#9889; EV INTELLIGENCE</p>
            </td>
          </tr>
          <tr>
            <td style="background:#ecfdf5;padding:10px 32px;border-bottom:1px solid #6ee7b7;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="font-family:Arial,sans-serif;font-size:12px;color:#64748b;">
                    <span style="color:#0d1f40;font-weight:700;">{date_str}</span> &nbsp;&bull;&nbsp; {time_str}
                  </td>
                  <td align="right" style="font-family:Arial,sans-serif;font-size:12px;color:#64748b;">
                    {article_count} sources &nbsp;&bull;&nbsp; <span style="color:#059669;font-weight:700;">5 stories</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="background:#f8faff;padding:24px 28px 12px;">
              {story_cards}
              {editor_block}
            </td>
          </tr>
          <tr>
            <td style="background:#0d1f40;border-radius:0 0 12px 12px;padding:20px 32px;text-align:center;">
              <p style="margin:0 0 6px;font-family:Arial,sans-serif;font-size:13px;font-weight:700;">
                <span style="color:#ffffff;">Prompt</span>&nbsp;<span style="color:#60a5fa;">AI News</span>
              </p>
              <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:rgba(255,255,255,0.45);">EV Edition &nbsp;&bull;&nbsp; Every 3 hours &nbsp;&bull;&nbsp; AEO-Optimized &nbsp;&bull;&nbsp; Powered by Claude</p>
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
    subject = f"[Prompt AI] ⚡ EV News — {now.strftime('%b %d, %I:%M %p')}"
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
    subject = f"[Prompt AI] ⚡ SLOW DAY...NOTHING NEW SIR — {now.strftime('%b %d, %I:%M %p')}"

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
            <p style="margin:8px 0 0;font-family:Arial,sans-serif;font-size:13px;color:#6ee7b7;font-weight:700;letter-spacing:1px;">&#9889; EV INTELLIGENCE</p>
          </td>
        </tr>
        <tr>
          <td style="background:#f8faff;padding:40px 32px;text-align:center;">
            <p style="font-family:Arial,sans-serif;font-size:40px;margin:0 0 12px;">🤫</p>
            <h2 style="font-family:Arial,sans-serif;font-size:22px;color:#0d1f40;margin:0 0 12px;">SLOW DAY...NOTHING NEW SIR</h2>
            <p style="font-family:Georgia,serif;font-size:15px;color:#475569;line-height:1.7;margin:0;">
              No new EV stories since the last brief.<br>Check back in 3 hours.
            </p>
          </td>
        </tr>
        <tr>
          <td style="background:#0d1f40;border-radius:0 0 12px 12px;padding:20px 32px;text-align:center;">
            <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:rgba(255,255,255,0.45);">
              EV Edition &nbsp;&bull;&nbsp; Powered by Claude
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

    plain = "SLOW DAY...NOTHING NEW SIR\n\nNo new EV stories since the last brief. Check back in 3 hours."

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

    print("Slow day EV email sent.")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    seen = prune_seen(load_seen())

    print(f"Fetching articles from {len(RSS_FEEDS)} EV sources...")
    articles = fetch_articles()
    print(f"{len(articles)} articles retrieved.")

    new_articles = filter_new(articles, seen)
    print(f"{len(new_articles)} new stories (not seen in last {SEEN_EXPIRY_HOURS}h).\n")

    if len(new_articles) < MIN_NEW_STORIES:
        print("Not enough new stories -- sending slow day email.")
        send_slow_day_email()
        save_seen(seen)
        return

    print("Generating EV brief (streaming)...\n")
    print("=" * 60)

    client = anthropic.Anthropic()
    brief_body = generate_brief(new_articles, client)

    print("\n" + "=" * 60)

    seen = mark_seen(seen, new_articles)
    save_seen(seen)

    send_email(brief_body, article_count=len(new_articles))


if __name__ == "__main__":
    main()
