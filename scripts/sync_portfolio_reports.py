#!/usr/bin/env python3
"""
Mirrors new reports from the Portfolio-Summary repo into this repo's Report Library.

Portfolio-Summary has no aggregate index file. Each report lives in its own directory
under report-inbox/<kind>/<slug>/ with a manifest.json (date, title, ticker,
analysisType) beside the actual document, so this script walks those manifests and
treats them as the index.

Pipeline:
  1. Walk report-inbox/{research,weekly,daily}/*/manifest.json in the source checkout.
  2. Classify each report into a card type (research / portfolio / market / weekly /
     daily) and decide whether it is durable or part of a rotating series.
  3. Drop every rotating-series report except the newest edition of each series —
     the app keeps only the latest weekly, latest daily, etc. at any one time.
  4. Extract the document (report.html, a split gzip/base64 HTML, or report.pdf.b64)
     and skip anything that fails to reassemble into a complete file.
  5. Skip reports already mirrored, matched by CONTENT HASH rather than filename, so a
     report added by hand under a different name is never duplicated.
  6. Copy in what is new, insert matching rows into the `reports` array in index.html,
     and retire superseded rotating-series cards (row + file).

State lives in scripts/report-sync-state.json and records which files this script owns.
Only those files are ever retired — anything added by hand is left alone. That file also
carries an "ignored" list of source slugs the sync must never add, for reports already
in the library in a different format (where the content hash cannot match) and for
sources known to be damaged upstream.

Archive rule: every row carries a `key` (ticker for single-company reports, series name for
rotating series, slug for one-offs). When a newer report lands for a key, the older live
version is MOVED into archive/ and its row is flagged archived:true (the app's Archived
filter). A report that arrives older than what is already live for its key is archived on
arrival. Files already in archive/ are hashed too, so they are never re-added to the root.

Idempotent: a run with nothing new makes no changes and exits 0. Only stdlib is used
(no pip install step needed in the Action).
"""
import base64
import gzip
import hashlib
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(REPO_ROOT, 'index.html')
STATE_FILE = os.path.join(REPO_ROOT, 'scripts', 'report-sync-state.json')
ARCHIVE_DIR = os.path.join(REPO_ROOT, 'archive')

# Where the Portfolio-Summary checkout lives. The workflow checks it out into a
# sibling path; override with SUMMARY_REPO when running locally.
SOURCE_ROOT = os.environ.get(
    'SUMMARY_REPO', os.path.join(os.path.dirname(REPO_ROOT), 'portfolio-summary'))

# Recurring reports, keyed by the prefix of their source slug. Only the newest edition
# of each series is kept in the app; older ones are retired as new ones land.
# Everything else is durable and accumulates.
ROTATING_BY_SLUG = {
    'weekly-market-intelligence': ('market-intelligence', 'market'),
    'stocks-to-watch': ('stocks-to-watch', 'market'),
}
# The weekly/ and daily/ trees are rotating in the same way, one series each.
ROTATING_BY_KIND = {
    'weekly': ('weekly-portfolio', 'weekly'),
    'daily': ('daily-close', 'daily'),
}

# Series name -> the `key` the app groups on (must match the keys used in index.html).
SERIES_KEYS = {
    'market-intelligence': 'weekly-market-intel',
    'stocks-to-watch': 'stocks-to-watch',
    'weekly-portfolio': 'weekly-review',
    'daily-close': 'daily-close',
}

# Cap on generated filenames so a long report title doesn't produce an unwieldy path.
MAX_STEM = 60


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- source extraction

def _reassemble_split_html(report_dir, parts):
    """Rebuild HTML stored as one base64 stream split across .gz.b64.part.NNN files.

    The parts are a raw byte split of a single base64 document, so they are simply
    concatenated in name order before decoding. Returns None if the stream is damaged.
    """
    raw = b''.join(open(os.path.join(report_dir, p), 'rb').read() for p in sorted(parts))
    try:
        return gzip.decompress(base64.b64decode(raw))
    except Exception as e:
        log(f'    ! split HTML failed to reassemble ({e})')
        return None


def extract_document(report_dir):
    """Return (bytes, extension) for a report directory, or (None, None).

    Prefers HTML and falls back to the base64-encoded PDF. Truncated or corrupt
    documents are rejected here rather than published half-complete.
    """
    files = os.listdir(report_dir)

    content = None
    if 'report.html' in files:
        content = open(os.path.join(report_dir, 'report.html'), 'rb').read()
    else:
        parts = [f for f in files if '.gz.b64.part.' in f]
        if parts:
            content = _reassemble_split_html(report_dir, parts)

    if content is not None:
        # A report missing its closing tag was truncated somewhere upstream. Publishing
        # a partial document is worse than skipping it, so refuse it either way.
        if b'</html>' not in content.lower():
            log('    ! HTML is truncated (no closing </html>) — skipping')
            return None, None
        return content, 'html'

    if 'report.pdf.b64' in files:
        try:
            pdf = base64.b64decode(open(os.path.join(report_dir, 'report.pdf.b64'), 'rb').read())
        except Exception as e:
            log(f'    ! PDF failed to decode ({e})')
            return None, None
        if not pdf.startswith(b'%PDF'):
            log('    ! decoded PDF has no %PDF header — skipping')
            return None, None
        return pdf, 'pdf'

    log('    ! no usable report.html or report.pdf.b64')
    return None, None


# ---------------------------------------------------------------- classification

MONTHS = ['', 'January', 'February', 'March', 'April', 'May', 'June', 'July',
          'August', 'September', 'October', 'November', 'December']


def long_date(iso):
    """2026-09-17 -> September 17, 2026, matching how the cards read elsewhere."""
    m = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', iso)
    if not m:
        return iso
    y, mo, d = m.groups()
    return f'{MONTHS[int(mo)]} {int(d)}, {y}'


def html_title(content):
    """Pull the <title> out of a report, normalised to the app's style.

    Report titles often end in a raw ISO date ("Daily Portfolio Close - 2026-09-17");
    spell that out so the card reads like the hand-written entries around it.
    """
    m = re.search(rb'<title>(.*?)</title>', content, re.S | re.I)
    if not m:
        return ''
    title = re.sub(r'\s+', ' ', m.group(1).decode('utf-8', 'replace')).strip()
    title = title.replace(' - ', ' — ')
    return re.sub(r'\d{4}-\d{2}-\d{2}$', lambda x: long_date(x.group(0)), title)


def classify(kind, slug, manifest):
    """Map a source report onto a card spec for the Report Library.

    Returns a dict with the series it belongs to (None when durable), the badge type,
    and the ticker/assetType fields the research cards use.
    """
    spec = {'series': None, 'type': 'research', 'ticker': '', 'assetType': ''}

    if kind in ROTATING_BY_KIND:
        spec['series'], spec['type'] = ROTATING_BY_KIND[kind]
        return spec

    for prefix, (series, card_type) in ROTATING_BY_SLUG.items():
        if slug.startswith(prefix):
            spec['series'], spec['type'] = series, card_type
            return spec

    analysis = (manifest.get('analysisType') or '').lower()
    ticker = (manifest.get('ticker') or '').strip()
    if analysis == 'etf-comparison':
        spec['assetType'] = 'fund'
    elif analysis == 'portfolio':
        # Portfolio-level work (rankings, screens, conference read-throughs) is not
        # about a single name, so it gets its own badge instead of research styling.
        spec['type'] = 'portfolio'
    elif ticker:
        spec['assetType'] = 'equity'
    spec['ticker'] = ticker
    return spec


def key_for(spec, slug, manifest):
    """Supersession key: one live report per key. Mirrors the keys assigned in index.html."""
    if spec['series']:
        return SERIES_KEYS[spec['series']]
    analysis = (manifest.get('analysisType') or '').lower()
    if spec['ticker'] and spec['type'] == 'research' and analysis != 'etf-comparison':
        return spec['ticker'].upper()
    return 'doc-' + re.sub(r'-?\d{4}-\d{2}-\d{2}$', '', slug)


def filename_for(title, date, ext):
    # Drop any trailing date already carried by the title, so the generated name does
    # not end up with it twice ("Daily_Portfolio_Close_2026_09_17_2026-09-17.html").
    stem = re.sub(r'[^A-Za-z0-9]+', '_', title).strip('_')
    stem = re.sub(r'_?(\d{4}_\d{2}_\d{2}|[A-Z][a-z]+_\d{1,2}_\d{4})$', '', stem)
    stem = stem[:MAX_STEM].strip('_')
    return f'{stem or "Report"}_{date}.{ext}'


def js_string(value):
    """Quote a title for the JS array, preferring single quotes like the existing rows."""
    value = value.replace('\\', '\\\\')
    if "'" in value:
        return '"' + value.replace('"', '\\"') + '"'
    return "'" + value + "'"


# ---------------------------------------------------------------- index.html editing

ARRAY_RE = re.compile(r'(const reports = \[\n)(.*?)(\n\];)', re.S)


def read_rows(html):
    m = ARRAY_RE.search(html)
    if not m:
        raise SystemExit('ERROR: could not find the `const reports = [` array in index.html')
    rows = [r for r in m.group(2).split('\n') if r.strip()]
    return m, rows


def row_date(row):
    m = re.search(r"date:'([^']+)'", row)
    return m.group(1) if m else ''


def row_file(row):
    m = re.search(r"file:'([^']+)'", row)
    return m.group(1) if m else ''


def build_row(spec, title, date, filename, key, archived=False):
    parts = [f"  {{date:'{date}'", f'title:{js_string(title)}',
             f"type:'{spec['type']}'", f"file:'{filename}'"]
    if spec['ticker']:
        parts.append(f"ticker:'{spec['ticker']}'")
    if spec['assetType']:
        parts.append(f"assetType:'{spec['assetType']}'")
    parts.append(f"key:'{key}'")
    if archived:
        parts.append('archived:true')
    return ', '.join(parts) + '},'


def row_key(row):
    m = re.search(r"key:'([^']+)'", row)
    return m.group(1) if m else None


def row_archived(row):
    return 'archived:true' in row


def move_to_archive(name):
    """Move a root file into archive/ (non-destructive). Returns its new repo-relative path."""
    base = os.path.basename(name)
    src = os.path.join(REPO_ROOT, name)
    if os.path.isfile(src):
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        os.replace(src, os.path.join(ARCHIVE_DIR, base))
    return 'archive/' + base


def archive_older(rows, key, keep_file, date, hashes):
    """Archive every live row for `key` that is strictly older than `date`."""
    moved = []
    for i, row in enumerate(rows):
        if row_key(row) != key or row_archived(row) or row_file(row) == keep_file:
            continue
        if row_date(row) >= date:
            continue
        old = row_file(row)
        new = move_to_archive(old)
        row = row.replace(f"file:'{old}'", f"file:'{new}'")
        rows[i] = re.sub(r"\},?$", ", archived:true},", row)
        for h, name in list(hashes.items()):
            if name == old:
                hashes[h] = new
        moved.append(old)
    return moved


def insert_row(rows, new_row, date):
    """Insert ahead of the first row with an older date, keeping existing order intact."""
    for i, row in enumerate(rows):
        if row_date(row) < date:
            rows.insert(i, new_row)
            return
    rows.append(new_row)


# ---------------------------------------------------------------- main

def repo_hash_index():
    """sha256 -> repo-relative path for every document in the repo root and in archive/."""
    index = {}
    for folder, prefix in ((REPO_ROOT, ''), (ARCHIVE_DIR, 'archive/')):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if (folder == REPO_ROOT and name == 'index.html') or not name.lower().endswith(('.html', '.pdf')):
                continue
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                index[hashlib.sha256(open(path, 'rb').read()).hexdigest()] = prefix + name
    return index


def collect_candidates():
    """Every source report worth considering, after applying the latest-only rule."""
    inbox = os.path.join(SOURCE_ROOT, 'report-inbox')
    if not os.path.isdir(inbox):
        raise SystemExit(f'ERROR: no report-inbox under {SOURCE_ROOT!r} — is the source checked out?')

    candidates = []
    for kind in sorted(os.listdir(inbox)):
        kind_dir = os.path.join(inbox, kind)
        if not os.path.isdir(kind_dir):
            continue
        for slug in sorted(os.listdir(kind_dir)):
            report_dir = os.path.join(kind_dir, slug)
            manifest_path = os.path.join(report_dir, 'manifest.json')
            if not os.path.isfile(manifest_path):
                continue
            try:
                manifest = json.load(open(manifest_path, encoding='utf-8'))
            except Exception as e:
                log(f'  ! {kind}/{slug}: unreadable manifest ({e})')
                continue
            date = (manifest.get('reportDate') or '').strip()
            if not date:
                log(f'  ! {kind}/{slug}: manifest has no reportDate — skipping')
                continue
            spec = classify(kind, slug, manifest)
            candidates.append({
                'kind': kind, 'slug': slug, 'dir': report_dir,
                'date': date, 'manifest': manifest, 'spec': spec,
            })

    # Keep only the newest edition of each rotating series.
    newest = {}
    for c in candidates:
        series = c['spec']['series']
        if series and (series not in newest or c['date'] > newest[series]['date']):
            newest[series] = c
    return [c for c in candidates
            if not c['spec']['series'] or newest[c['spec']['series']] is c]


def main():
    state = {'managed': {}}
    if os.path.isfile(STATE_FILE):
        state = json.load(open(STATE_FILE, encoding='utf-8'))
    managed = state.setdefault('managed', {})
    ignored = set(state.setdefault('ignored', {}))

    html = open(INDEX_HTML, encoding='utf-8').read()
    match, rows = read_rows(html)
    linked = {row_file(r) for r in rows}
    hashes = repo_hash_index()

    added, retired, archived = [], [], []

    for c in collect_candidates():
        label = f"{c['kind']}/{c['slug']}"
        if c['slug'] in ignored:
            continue
        content, ext = extract_document(c['dir'])
        if content is None:
            log(f'  skip {label}: no usable document')
            continue

        digest = hashlib.sha256(content).hexdigest()
        existing = hashes.get(digest)
        if existing and (existing in linked or ('archive/' + existing) in linked or existing.startswith('archive/')):
            continue  # already mirrored (or deliberately archived), possibly under a different filename

        title = (c['manifest'].get('title') or '').strip() or html_title(content)
        if not title:
            log(f'  skip {label}: no title in manifest or document')
            continue

        key = key_for(c['spec'], c['slug'], c['manifest'])
        series = c['spec']['series']
        # A durable report that is not newer than what is already live for its key goes
        # straight to the archive instead of onto the main page.
        stale = (not series) and any(
            row_key(r) == key and not row_archived(r) and row_date(r) >= c['date'] for r in rows)

        filename = existing or filename_for(title, c['date'], ext)
        if not existing:
            with open(os.path.join(REPO_ROOT, filename), 'wb') as fh:
                fh.write(content)
            hashes[digest] = filename

        if filename not in linked:
            if stale:
                filename = move_to_archive(filename)
                hashes[digest] = filename
                log(f'  ~ {label} -> {filename} (older than the live {key} report; archived on arrival)')
            insert_row(rows, build_row(c['spec'], title, c['date'], filename, key, archived=stale), c['date'])
            linked.add(filename)
            added.append(f"{filename}  [{c['spec']['type']}]")
            log(f'  + {label} -> {filename}')
            if not series and not stale:
                for old in archive_older(rows, key, filename, c['date'], hashes):
                    archived.append(old)
                    log(f'  > archived {old} (superseded by {filename})')
        if series:
            # Retire the edition this one supersedes, but only if the sync added it.
            previous = managed.get(series)
            if previous and previous['file'] != filename:
                old = previous['file']
                rows[:] = [r for r in rows if row_file(r) != old]
                linked.discard(old)
                old_path = os.path.join(REPO_ROOT, old)
                if os.path.isfile(old_path):
                    os.remove(old_path)
                retired.append(old)
                log(f'  - retired {old} (superseded by {filename})')
            managed[series] = {'file': filename, 'date': c['date']}

    if not added and not retired and not archived:
        log('No new reports — nothing to do.')
        return 0

    html = html[:match.start(2)] + '\n'.join(rows) + html[match.end(2):]
    with open(INDEX_HTML, 'w', encoding='utf-8') as fh:
        fh.write(html)
    with open(STATE_FILE, 'w', encoding='utf-8') as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write('\n')

    log(f'\nAdded {len(added)}, retired {len(retired)}, archived {len(archived)}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
