"""
Prompt AI News — Weekly Cost & Activity Report
Aggregates usage_log.jsonl for the last 7 days and emails a summary.
Registered in Task Scheduler to run Sundays at 8:00 PM (see register_task.bat).

Usage:
    python weekly_report.py
"""

import json
import os
import smtplib
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
ENV_PATH = Path(r"C:\Users\mrcar\.promptai\.env")
USAGE_LOG = BASE_DIR / "usage_log.jsonl"

# Fallback per-token USD prices used only when OpenRouter doesn't report cost.
# (input/output per 1M tokens)
FALLBACK_PRICES = {
    "z-ai/glm-5.3-flash": (0.10, 0.40),
    "z-ai/glm-5.3": (0.60, 2.20),
}


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
SENDER = os.environ.get("SENDER_EMAIL", "mrcarloslucero@gmail.com")
RECIPIENT = os.environ.get("RECIPIENT_EMAIL", SENDER)


def estimate_cost(entry: dict) -> float | None:
    if entry.get("cost") is not None:
        return float(entry["cost"])
    prices = FALLBACK_PRICES.get(entry.get("model", ""))
    if not prices:
        return None
    in_price, out_price = prices
    return (entry.get("prompt_tokens", 0) * in_price +
            entry.get("completion_tokens", 0) * out_price) / 1_000_000


def collect(days: int = 7) -> list[dict]:
    if not USAGE_LOG.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    entries = []
    for line in USAGE_LOG.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
            ts = datetime.fromisoformat(e["ts"])
            if ts >= cutoff:
                entries.append(e)
        except Exception:
            continue
    return entries


def fetch_openrouter_credits() -> dict | None:
    """Live balance info from OpenRouter, if the key permits it."""
    try:
        r = requests.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("data")
    except Exception:
        pass
    return None


def build_report(entries: list[dict]) -> tuple[str, str]:
    now = datetime.now()
    if not entries:
        return ("[Prompt AI] Weekly Report — no activity",
                "<html><body style='font-family:Arial,sans-serif;padding:24px;'>"
                "<p>No briefing runs were recorded in the last 7 days.</p></body></html>")

    by_day = defaultdict(lambda: {"runs": 0, "tokens": 0, "cost": 0.0})
    by_section = defaultdict(lambda: {"runs": 0, "tokens": 0, "cost": 0.0, "articles": 0, "fulltext": 0})
    total_cost = 0.0
    known_cost = 0.0
    estimated = False

    for e in entries:
        day = e["ts"][:10]
        cost = estimate_cost(e)
        if cost is not None:
            total_cost += cost
            known_cost += e["cost"] if e.get("cost") is not None else 0
            if e.get("cost") is None:
                estimated = True
        by_day[day]["runs"] += 1
        by_day[day]["tokens"] += e.get("total_tokens", 0)
        by_day[day]["cost"] += cost or 0
        sec = by_section[e.get("section", "?")]
        sec["runs"] += 1
        sec["tokens"] += e.get("total_tokens", 0)
        sec["cost"] += cost or 0
        sec["articles"] += e.get("articles_fetched", 0)
        sec["fulltext"] += e.get("articles_with_full_text", 0)

    credits = fetch_openrouter_credits()
    credits_html = ""
    if credits:
        credits_html = (
            f"<tr><td style='padding:6px 12px;border-bottom:1px solid #e2e8f0;'>"
            f"OpenRouter balance</td><td style='padding:6px 12px;border-bottom:1px solid #e2e8f0;'>"
            f"${credits.get('total_credits', 0):.2f} total · "
            f"<b>${credits.get('total_usage', 0):.2f} used</b></td></tr>"
        )

    day_rows = "".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #e2e8f0;'>{d}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #e2e8f0;'>{v['runs']}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #e2e8f0;'>{v['tokens']:,}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #e2e8f0;'>${v['cost']:.4f}</td></tr>"
        for d, v in sorted(by_day.items())
    )
    section_rows = "".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #e2e8f0;'>{s}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #e2e8f0;'>{v['runs']}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #e2e8f0;'>{v['articles']}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #e2e8f0;'>{v['fulltext']}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #e2e8f0;'>{v['tokens']:,}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #e2e8f0;'>${v['cost']:.4f}</td></tr>"
        for s, v in sorted(by_section.items())
    )
    savings_note = f"Estimated Claude-equivalent cost at Sonnet pricing: <b>~${total_cost * 8:.2f}</b>"

    html = f"""<html><body style="font-family:Arial,sans-serif;color:#1e293b;padding:24px;background:#f8fafc;">
<div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:12px;padding:24px;border:1px solid #e2e8f0;">
<h2 style="margin:0 0 4px;color:#0d1f40;">Prompt AI News — Weekly Report</h2>
<p style="color:#64748b;font-size:13px;margin:0 0 18px;">{now.strftime('%B %d, %Y')} · last 7 days</p>
<table width="100%" style="border-collapse:collapse;font-size:13px;">
{credits_html}
</table>
<h3 style="margin:18px 0 8px;">Totals</h3>
<p style="font-size:14px;margin:0 0 6px;">Generation runs: <b>{len(entries)}</b></p>
<p style="font-size:14px;margin:0 0 6px;">Total tokens: <b>{sum(e.get('total_tokens',0) for e in entries):,}</b></p>
<p style="font-size:14px;margin:0 0 6px;">GLM spend: <b>${total_cost:.4f}</b>
{' <span style="color:#b45309;font-size:11px;">(partly estimated)</span>' if estimated else ''}</p>
<p style="font-size:14px;margin:0 0 6px;color:#059669;">{savings_note}</p>
<h3 style="margin:18px 0 8px;">By day</h3>
<table width="100%" style="border-collapse:collapse;font-size:13px;">
<tr style="background:#f1f5f9;"><th align="left" style="padding:6px 12px;">Day</th><th align="left" style="padding:6px 12px;">Runs</th><th align="left" style="padding:6px 12px;">Tokens</th><th align="left" style="padding:6px 12px;">Cost</th></tr>
{day_rows}
</table>
<h3 style="margin:18px 0 8px;">By section</h3>
<table width="100%" style="border-collapse:collapse;font-size:13px;">
<tr style="background:#f1f5f9;"><th align="left" style="padding:6px 12px;">Section</th><th align="left" style="padding:6px 12px;">Runs</th><th align="left" style="padding:6px 12px;">Articles</th><th align="left" style="padding:6px 12px;">Full text</th><th align="left" style="padding:6px 12px;">Tokens</th><th align="left" style="padding:6px 12px;">Cost</th></tr>
{section_rows}
</table>
</div></body></html>"""

    plain = (f"Prompt AI News Weekly Report — {now.strftime('%B %d, %Y')}\n"
             f"Runs: {len(entries)} | Tokens: {sum(e.get('total_tokens',0) for e in entries):,} | "
             f"GLM spend: ${total_cost:.4f}\n")
    subject = f"[Prompt AI] Weekly Report — {len(entries)} runs · ${total_cost:.4f}"
    return subject, html


def main() -> None:
    entries = collect(7)
    subject, html = build_report(entries)
    msg = MIMEText(html, "html")
    msg["Subject"] = subject
    msg["From"] = f"Prompt AI News <{SENDER}>"
    msg["To"] = RECIPIENT
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SENDER, GMAIL_APP_PASSWORD)
        server.sendmail(SENDER, RECIPIENT, msg.as_string())
    print(f"Weekly report sent to {RECIPIENT}: {subject}")


if __name__ == "__main__":
    main()
