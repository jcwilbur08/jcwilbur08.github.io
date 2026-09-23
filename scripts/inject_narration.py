#!/usr/bin/env python3
"""Injects a browser-native narration ("Listen") widget into any published
report HTML file in this repo that doesn't already have one.

This is the universal fallback for reports that don't come from
Portfolio-Summary's own report_template.html (which already bakes the same
kind of widget in at build time): ChatGPT's direct-to-GitHub reports, and
anything pasted in manually. It works on any HTML file's structure using a
plain heading/paragraph/list-item selector rather than any report-specific
class names, since third-party reports use their own markup conventions.

Idempotent: skips any file that already references speechSynthesis
anywhere in its source, on the assumption that means some narration
mechanism (this one, or a per-report one baked in elsewhere) is already
present. Safe to run repeatedly and on a schedule.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_FILES = {"index.html"}
SKIP_MARKER = "speechSynthesis"

WIDGET = """
<!-- PCC-NARRATE:START -- universal narration widget, injected by scripts/inject_narration.py.
     Reads the page aloud via the browser's built-in Web Speech API. Idempotent: this script
     will not inject a second copy into a file that already references speechSynthesis. -->
<style>
  #pcc-narrate-bar{
    position:fixed; bottom:18px; right:18px; z-index:2147483000;
    display:flex; align-items:center; gap:6px;
    background:#12395B; color:#fff; border-radius:999px; padding:8px 8px;
    box-shadow:0 8px 24px rgba(10,22,40,.35);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  }
  #pcc-narrate-bar button{
    background:rgba(255,255,255,.10); border:1px solid rgba(255,255,255,.20); color:#fff;
    border-radius:999px; padding:7px 12px; font-size:12px; font-weight:700; cursor:pointer;
    display:flex; align-items:center; gap:5px; line-height:1;
  }
  #pcc-narrate-bar button:hover{background:rgba(255,255,255,.20);}
  #pcc-narrate-bar button:disabled{opacity:.4; cursor:default;}
  #pcc-narrate-bar .pcc-narrate-speed{min-width:40px; justify-content:center;}
  #pcc-narrate-bar .pcc-narrate-label{font-size:11px; color:#bcd0ea; padding:0 4px 0 6px; white-space:nowrap;}
  .pcc-narrate-highlight{
    background:rgba(59,130,246,.18) !important; outline:2px solid rgba(59,130,246,.35);
    border-radius:6px; transition:background .15s ease;
  }
  @media print{ #pcc-narrate-bar{display:none !important;} }
</style>
<div id="pcc-narrate-bar" style="display:none">
  <button type="button" id="pcc-narrate-play" aria-label="Play">&#9654; Listen</button>
  <button type="button" id="pcc-narrate-stop" disabled aria-label="Stop">&#9632;</button>
  <button type="button" id="pcc-narrate-speed" class="pcc-narrate-speed" disabled aria-label="Playback speed">1x</button>
  <span class="pcc-narrate-label" id="pcc-narrate-status"></span>
</div>
<script>
(function(){
  if (!('speechSynthesis' in window)) return;
  var SEL = 'h1,h2,h3,h4,h5,p,li';
  var EXCLUDE_ANCESTOR = 'table';
  var RATES = [1, 1.25, 1.5, 0.85];
  var bar = document.getElementById('pcc-narrate-bar');
  var playBtn = document.getElementById('pcc-narrate-play');
  var stopBtn = document.getElementById('pcc-narrate-stop');
  var speedBtn = document.getElementById('pcc-narrate-speed');
  var statusEl = document.getElementById('pcc-narrate-status');
  var nodes = [], idx = 0, rateIdx = 0, playing = false;

  function collect(){
    var all = Array.prototype.slice.call(document.querySelectorAll(SEL));
    var seen = [];
    return all.filter(function(el){
      if (el.closest(EXCLUDE_ANCESTOR)) return false;
      if (el.closest('#pcc-narrate-bar')) return false;
      var text = (el.innerText || '').trim();
      if (!text) return false;
      var prev = seen[seen.length - 1];
      if (prev && prev.contains(el)) return false;
      seen.push(el);
      return true;
    });
  }
  function clearHighlight(){
    var h = document.querySelector('.pcc-narrate-highlight');
    if (h) h.classList.remove('pcc-narrate-highlight');
  }
  function setStatus(){ statusEl.textContent = nodes.length ? (idx + 1) + ' / ' + nodes.length : ''; }
  function speakNext(){
    if (idx >= nodes.length) { stopAll(); return; }
    var el = nodes[idx];
    var text = el.innerText.trim();
    clearHighlight();
    el.classList.add('pcc-narrate-highlight');
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setStatus();
    var u = new SpeechSynthesisUtterance(text);
    u.rate = RATES[rateIdx];
    u.onend = function(){ if (playing) { idx++; speakNext(); } };
    window.speechSynthesis.speak(u);
  }
  function play(){
    if (!nodes.length) nodes = collect();
    if (!nodes.length) return;
    playing = true;
    playBtn.innerHTML = '&#9208; Pause';
    stopBtn.disabled = false;
    speedBtn.disabled = false;
    if (!window.speechSynthesis.speaking) speakNext();
    else window.speechSynthesis.resume();
  }
  function pause(){ playing = false; window.speechSynthesis.pause(); playBtn.innerHTML = '&#9654; Resume'; }
  function stopAll(){
    playing = false; window.speechSynthesis.cancel(); idx = 0; clearHighlight();
    playBtn.innerHTML = '&#9654; Listen'; stopBtn.disabled = true; speedBtn.disabled = true; statusEl.textContent = '';
  }
  playBtn.addEventListener('click', function(){ if (playing) pause(); else play(); });
  stopBtn.addEventListener('click', stopAll);
  speedBtn.addEventListener('click', function(){ rateIdx = (rateIdx + 1) % RATES.length; speedBtn.textContent = RATES[rateIdx] + 'x'; });
  window.addEventListener('beforeunload', function(){ window.speechSynthesis.cancel(); });
  bar.style.display = 'flex';
})();
</script>
<!-- PCC-NARRATE:END -->
"""


def process(path: pathlib.Path) -> bool:
    try:
        html = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if SKIP_MARKER in html:
        return False
    if "</body>" not in html:
        return False
    new_html = html.replace("</body>", WIDGET + "\n</body>", 1)
    path.write_text(new_html, encoding="utf-8")
    return True


def main() -> None:
    changed = []
    for p in sorted(ROOT.rglob("*.html")):
        rel = p.relative_to(ROOT)
        if rel.name in SKIP_FILES:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue  # skip .git and any dotdirs
        if process(p):
            changed.append(str(rel))

    if changed:
        print(f"Injected narration into {len(changed)} file(s):")
        for name in changed:
            print(" -", name)
        sys.exit(0)
    print("No files needed narration injection.")


if __name__ == "__main__":
    main()
