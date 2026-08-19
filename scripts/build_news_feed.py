#!/usr/bin/env python3
"""
Builds news-feed.json for the Portfolio Command Center's News tab.

Pipeline:
  1. Pull today's day % change for every tracked ticker from Finnhub (no daily call
     cap on the free tier — safe to check all ~36 tickers every run).
  2. Rank by absolute day % change, keep the top N movers.
  3. Fetch news for those movers in ONE Alpha Vantage NEWS_SENTIMENT call (the free
     tier caps at 25 calls/day total, so batching into a single call matters —
     4 of those 25 are already spent daily on mutual fund NAVs).
  4. Ask the Claude API to turn each mover's article into a short headline + "why"
     blurb in the app's house style, paraphrased (not quoted) from the source.
  5. Write news-feed.json to the repo root. Movers with no findable news are simply
     dropped rather than backfilled with invented text.

Only stdlib is used (no pip install step needed in the Action).
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

# Same key already used client-side in index.html for live quotes (free tier, already
# public in the page source — no need to treat it as a secret here).
FINNHUB_API_KEY = 'd9o9n09r01qt6o9atckgd9o9n09r01qt6o9atcl0'
ALPHA_VANTAGE_KEY = os.environ.get('ALPHA_VANTAGE_KEY', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# Tracked tickers — mirrors the `holdings` array in index.html, minus CASH and the four
# mutual funds (FSMDX, FSSNX, TLXIX, VFFSX have no Finnhub intraday quote and rarely have
# single-day news pegged to them the way individual equities/ETFs do).
# NOTE: update this list when holdings materially change (new position added/exited).
# A stale entry just means that ticker won't be considered for movers — low-risk drift.
TICKERS = [
    'GOOG', 'AMZN', 'AXP', 'AMT', 'AAPL', 'AVUV', 'BMY', 'AVGO', 'KO', 'XOM',
    'GRID', 'GEV', 'SOXQ', 'IJH', 'JPM', 'MA', 'META', 'MSFT', 'NFLX', 'PEP',
    'PM', 'QCOM', 'CRM', 'SCHD', 'SO', 'GS', 'DIS', 'UNH', 'VTWO', 'VXUS', 'V',
    'BATRA', 'TSM', 'ASML', 'VIS', 'GEHC',
]

TOP_N_MOVERS = 10
CLAUDE_MODEL = 'claude-haiku-4-5-20251001'


def http_get_json(url, timeout=20, retries=2, backoff=2.0):
    req = urllib.request.Request(url, headers={'User-Agent': 'portfolio-command-center-news-bot'})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(backoff * (attempt + 1))  # 2s, then 4s
                continue
            raise


def fetch_movers():
    """Day % change for every tracked ticker via Finnhub, sorted by |% move| descending.
    Returns the FULL ranked list (not pre-truncated) — truncation to the tickers we can
    actually find news for happens later, after we know which ones have coverage."""
    movers = []
    for i, ticker in enumerate(TICKERS):
        if i > 0:
            time.sleep(0.3)  # stay well clear of Finnhub's burst limit, not just the per-minute cap
        url = f'https://finnhub.io/api/v1/quote?symbol={urllib.parse.quote(ticker)}&token={FINNHUB_API_KEY}'
        try:
            data = http_get_json(url)
            c, pc = data.get('c'), data.get('pc')
            if isinstance(c, (int, float)) and isinstance(pc, (int, float)) and c > 0 and pc > 0:
                movers.append({'ticker': ticker, 'day_pct': (c - pc) / pc * 100})
        except Exception as e:
            print(f'WARN: Finnhub quote failed for {ticker}: {e}', file=sys.stderr)
    movers.sort(key=lambda m: abs(m['day_pct']), reverse=True)
    return movers


def fetch_news_sentiment(tickers):
    """One batched Alpha Vantage NEWS_SENTIMENT call; returns {ticker: best_article}."""
    if not ALPHA_VANTAGE_KEY:
        print('WARN: ALPHA_VANTAGE_KEY not set — skipping news fetch.', file=sys.stderr)
        return {}
    ticker_param = ','.join(tickers)
    url = (
        'https://www.alphavantage.co/query?function=NEWS_SENTIMENT'
        f'&tickers={urllib.parse.quote(ticker_param)}&limit=50&apikey={ALPHA_VANTAGE_KEY}'
    )
    try:
        data = http_get_json(url, timeout=30)
    except Exception as e:
        print(f'WARN: Alpha Vantage NEWS_SENTIMENT call failed: {e}', file=sys.stderr)
        return {}

    feed = data.get('feed', [])
    if not feed:
        print(f'WARN: Alpha Vantage NEWS_SENTIMENT returned no feed for {len(tickers)} tickers '
              f'(items field: {data.get("items")}). Response keys: {list(data.keys())}', file=sys.stderr)
    else:
        print(f'Alpha Vantage returned {len(feed)} articles for {len(tickers)} queried tickers.')

    best = {}
    for article in feed:
        for ts in article.get('ticker_sentiment', []):
            t = ts.get('ticker')
            if t not in tickers:
                continue
            relevance = float(ts.get('relevance_score', 0) or 0)
            if t not in best or relevance > best[t]['relevance']:
                best[t] = {
                    'relevance': relevance,
                    'title': article.get('title', ''),
                    'summary': article.get('summary', ''),
                    'source': article.get('source', ''),
                    'url': article.get('url', ''),
                }
    return best


def call_claude(movers, articles):
    """One Claude API call synthesizing headline + why for every mover with an article."""
    if not ANTHROPIC_API_KEY:
        print('WARN: ANTHROPIC_API_KEY not set — skipping synthesis.', file=sys.stderr)
        return {}

    items = []
    for m in movers:
        art = articles.get(m['ticker'])
        if not art:
            continue
        items.append({
            'ticker': m['ticker'],
            'day_pct': round(m['day_pct'], 2),
            'article_title': art['title'],
            'article_summary': art['summary'],
            'source': art['source'],
        })
    if not items:
        return {}

    system_prompt = (
        "You write short market-mover blurbs for a personal investment dashboard's News "
        "tab, senior-analyst tone, no fluff. For each ticker in the input, produce:\n"
        "- \"headline\": one sentence, present tense, stating the day's % move and the "
        "single biggest apparent cause. Example style: 'ExxonMobil rose 2.54% Tuesday as "
        "Energy led sector performance and the company advanced its Mozambique LNG "
        "project.'\n"
        "- \"why\": 1-3 sentences of supporting detail, written entirely in your own "
        "words from the article_summary provided. Never quote the source verbatim. Never "
        "invent facts, figures, or events not present in the input data.\n\n"
        "Respond with ONLY a JSON array, no markdown code fences, no preamble or "
        "commentary, in exactly this shape:\n"
        '[{"ticker": "XOM", "headline": "...", "why": "..."}, ...]'
    )
    body = json.dumps({
        'model': CLAUDE_MODEL,
        'max_tokens': 2000,
        'system': system_prompt,
        'messages': [{'role': 'user', 'content': json.dumps(items, indent=2)}],
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=body,
        headers={
            'Content-Type': 'application/json',
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print(f'ERROR: Anthropic API call failed: {e.code} {e.read().decode()}', file=sys.stderr)
        return {}
    except Exception as e:
        print(f'ERROR: Anthropic API call failed: {e}', file=sys.stderr)
        return {}

    raw_text = ''.join(b['text'] for b in result.get('content', []) if b.get('type') == 'text').strip()
    if raw_text.startswith('```'):
        raw_text = raw_text.strip('`')
        if raw_text.lower().startswith('json'):
            raw_text = raw_text[4:].strip()
    try:
        parsed = json.loads(raw_text)
    except Exception as e:
        print(f'ERROR: Could not parse Claude response as JSON: {e}\nRaw (first 500 chars): {raw_text[:500]}', file=sys.stderr)
        return {}

    return {item['ticker']: item for item in parsed if 'ticker' in item}


def main():
    all_movers = fetch_movers()
    if not all_movers:
        print('ERROR: No movers found — Finnhub fetch likely failed entirely. '
              'Aborting without touching news-feed.json.', file=sys.stderr)
        sys.exit(1)

    # Query news for the FULL tracked universe (one AV call, no extra cost) rather than
    # just the top-N movers — several tracked tickers are passive ETFs that rarely get
    # individual news coverage, so restricting the query to only the biggest movers risked
    # coming back empty on days when those movers happened to be exactly those ETFs.
    all_tickers = [m['ticker'] for m in all_movers]
    articles = fetch_news_sentiment(all_tickers)

    # Now rank by |day % move|, but only among tickers we actually found news for.
    coverable_movers = [m for m in all_movers if m['ticker'] in articles]
    print(f'{len(coverable_movers)} of {len(all_movers)} tracked tickers have news coverage today.')
    top_movers = coverable_movers[:TOP_N_MOVERS]

    if not top_movers:
        print('WARN: Zero tracked tickers had news coverage today — writing an empty feed '
              '(client falls back to static headlines).', file=sys.stderr)
        out = {
            'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
            'movers': [],
        }
        with open('news-feed.json', 'w') as f:
            json.dump(out, f, indent=2)
        print('Wrote news-feed.json with 0 movers.')
        return

    synthesized = call_claude(top_movers, articles)

    output_movers = []
    for m in top_movers:
        t = m['ticker']
        art, synth = articles.get(t), synthesized.get(t)
        if not art or not synth:
            continue  # no synthesis for this ticker — skip, don't fabricate
        output_movers.append({
            'ticker': t,
            'day_pct': round(m['day_pct'], 2),
            'headline': synth.get('headline', ''),
            'why': synth.get('why', ''),
            'source': art.get('source', ''),
            'url': art.get('url', ''),
        })

    if not output_movers:
        print('WARN: Had news coverage but synthesis produced nothing — writing an empty '
              'feed rather than failing (client falls back to static headlines).', file=sys.stderr)

    out = {
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'movers': output_movers,
    }
    with open('news-feed.json', 'w') as f:
        json.dump(out, f, indent=2)
    print(f'Wrote news-feed.json with {len(output_movers)} movers.')


if __name__ == '__main__':
    main()
