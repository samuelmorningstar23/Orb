#!/usr/bin/env python3
"""Turn the raw captures into website assets.

    server/.venv/bin/python docs/website-assets/build_assets.py

Reads  frames/<flow>/manifest.json + PNG frames   (from capture.cjs --flows)
       screenshots/<theme>/*.png + index.json     (from capture.cjs --gallery)
Writes gifs/<flow>.gif                 captioned, 1200 px wide, one frame per demo beat
       widgets/frames/<flow>/NN.webp   the same frames for the player widget
       widgets/workflow-player.html    self-contained embeddable player (?flow=<id>&autoplay=1)
       widgets/gallery.html            every module screenshot with a light/dark switch
       README.md                       what each file is and how to embed it

Pillow only; no ffmpeg needed. The WebM videos next to the frames come straight from
Playwright and are left as they are.
"""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FRAMES = ROOT / "frames"
SHOTS = ROOT / "screenshots"
GIFS = ROOT / "gifs"
WIDGETS = ROOT / "widgets"
WFRAMES = WIDGETS / "frames"
GIF_WIDTH = 1200
GIF_BAR = 118
WEBP_WIDTH = 1512

FONT_CANDIDATES = [
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
]


def font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=fnt) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def captioned(frame: Image.Image, caption: str, n: int, total: int, title: str) -> Image.Image:
    """Resize to GIF width and append a caption bar UNDER the screenshot (never over the UI)."""
    w, h = frame.size
    nh = round(h * GIF_WIDTH / w)
    img = frame.convert("RGB").resize((GIF_WIDTH, nh), Image.Resampling.LANCZOS)
    out = Image.new("RGB", (GIF_WIDTH, nh + GIF_BAR), (16, 18, 24))
    out.paste(img, (0, 0))
    d = ImageDraw.Draw(out)
    d.rectangle([0, nh, GIF_WIDTH, nh + 2], fill=(74, 158, 142))  # Orb teal rule
    f_body = font(23)
    f_meta = font(17)
    meta = f"{title}  ·  step {n} of {total}"
    d.text((28, nh + 14), meta, font=f_meta, fill=(150, 156, 168))
    lines = wrap(d, caption, f_body, GIF_WIDTH - 56)[:2]
    if len(wrap(d, caption, f_body, GIF_WIDTH - 56)) > 2:
        lines[1] = lines[1][: max(0, len(lines[1]) - 1)].rstrip(" ,.;:") + "…"
    y = nh + 42
    for ln in lines:
        d.text((28, y), ln, font=f_body, fill=(245, 244, 238))
        y += 31
    return out


def build_gif(flow_dir: Path, manifest: dict) -> Path | None:
    steps = manifest.get("steps") or []
    frames = []
    durations = []
    for s in steps:
        p = flow_dir / s["file"]
        if not p.exists():
            continue
        with Image.open(p) as im:
            frames.append(captioned(im, s["caption"], s["n"], len(steps), manifest["title"]))
        durations.append(int(s.get("hold", 3200)))
    if not frames:
        return None
    # one shared palette so colours do not flicker between frames
    strip = Image.new("RGB", (frames[0].width, frames[0].height * len(frames)))
    for i, f in enumerate(frames):
        strip.paste(f, (0, i * frames[0].height))
    palette_img = strip.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    q = [f.quantize(palette=palette_img, dither=Image.Dither.NONE) for f in frames]
    GIFS.mkdir(parents=True, exist_ok=True)
    out = GIFS / f"{manifest['id']}.gif"
    q[0].save(out, save_all=True, append_images=q[1:], duration=durations, loop=0, optimize=False)
    return out


def export_webp(flow_dir: Path, manifest: dict) -> list[str]:
    dst = WFRAMES / manifest["id"]
    dst.mkdir(parents=True, exist_ok=True)
    files = []
    for s in manifest.get("steps") or []:
        p = flow_dir / s["file"]
        if not p.exists():
            continue
        with Image.open(p) as im:
            img = im.convert("RGB")
            if img.width > WEBP_WIDTH:
                img = img.resize((WEBP_WIDTH, round(img.height * WEBP_WIDTH / img.width)), Image.Resampling.LANCZOS)
            name = Path(s["file"]).with_suffix(".webp").name
            img.save(dst / name, "WEBP", quality=84, method=6)
            files.append(f"frames/{manifest['id']}/{name}")
    return files


PLAYER_HTML = r"""<!doctype html>
<html lang="en" data-theme="__THEME__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Orb workflow player</title>
<style>
  :root { --bg:#0b0d12; --panel:#121620; --line:#242a36; --text:#f5f4ee; --muted:#9aa1ad; --accent:#4a9e8e; --accent-ink:#0b0d12; }
  :root[data-theme="light"] { --bg:#f6f5f1; --panel:#ffffff; --line:#e3e1da; --text:#15171c; --muted:#5f6672; --accent:#2f7f70; --accent-ink:#ffffff; }
  * { box-sizing:border-box }
  html,body { margin:0; background:var(--bg); color:var(--text); font:15px/1.45 "Inter Variable", Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
  .player { max-width:1200px; margin:0 auto; padding:16px; }
  .head { display:flex; gap:12px; align-items:flex-start; justify-content:space-between; flex-wrap:wrap; margin-bottom:12px; }
  .head h1 { font-size:18px; margin:0 0 2px; font-weight:650; letter-spacing:-0.01em }
  .head p { margin:0; color:var(--muted); font-size:14px }
  .flows { display:flex; gap:6px; flex-wrap:wrap }
  .flows button { border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:999px; padding:6px 12px; font:inherit; font-size:13px; cursor:pointer }
  .flows button[aria-current="true"] { background:var(--accent); color:var(--accent-ink); border-color:var(--accent) }
  .stage { position:relative; background:var(--panel); border:1px solid var(--line); border-radius:14px; overflow:hidden; aspect-ratio:1512/1024; }
  .stage img { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; opacity:0; transition:opacity .35s ease }
  .stage img.on { opacity:1 }
  .stage .badge { position:absolute; top:12px; left:12px; background:rgba(11,13,18,.72); color:#f5f4ee; font-size:12px; padding:4px 9px; border-radius:999px; backdrop-filter:blur(6px) }
  .caption { display:grid; grid-template-columns:1fr auto; gap:12px; align-items:start; margin-top:12px; padding:14px 16px; background:var(--panel); border:1px solid var(--line); border-radius:12px; min-height:84px }
  .caption p { margin:0; font-size:16px }
  .caption .step { color:var(--muted); font-size:13px; white-space:nowrap }
  .bar { height:3px; background:var(--line); border-radius:2px; margin-top:10px; overflow:hidden; grid-column:1/-1 }
  .bar i { display:block; height:100%; width:0; background:var(--accent); transition:width .25s linear }
  .controls { display:flex; gap:8px; align-items:center; margin-top:10px }
  .controls button { border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:10px; padding:8px 14px; font:inherit; cursor:pointer }
  .controls button.primary { background:var(--accent); color:var(--accent-ink); border-color:var(--accent) }
  .dots { display:flex; gap:6px; margin-left:auto }
  .dots button { width:10px; height:10px; padding:0; border-radius:50%; border:1px solid var(--muted); background:transparent }
  .dots button[aria-current="true"] { background:var(--accent); border-color:var(--accent) }
  .foot { color:var(--muted); font-size:12px; margin-top:10px }
  @media (prefers-reduced-motion: reduce) { .stage img { transition:none } }
  @media (max-width:640px) { .caption { grid-template-columns:1fr } .caption .step { white-space:normal } }
</style>
</head>
<body>
<div class="player" id="player">
  <div class="head">
    <div><h1 id="title"></h1><p id="subtitle"></p></div>
    <div class="flows" id="flows" role="tablist" aria-label="Workflows"></div>
  </div>
  <div class="stage" id="stage"><span class="badge" id="badge"></span></div>
  <div class="caption"><p id="caption"></p><span class="step" id="step"></span><div class="bar"><i id="bar"></i></div></div>
  <div class="controls">
    <button id="prev" aria-label="Previous step">‹ Prev</button>
    <button id="play" class="primary" aria-label="Play">Play</button>
    <button id="next" aria-label="Next step">Next ›</button>
    <div class="dots" id="dots"></div>
  </div>
  <p class="foot">Real screens from a running Orb appliance on seeded demo data. Keyboard: ← → to step, space to play/pause.</p>
</div>
<script id="manifest" type="application/json">__MANIFEST__</script>
<script>
(function(){
  const flows = JSON.parse(document.getElementById('manifest').textContent);
  const q = new URLSearchParams(location.search);
  const wanted = q.get('flow');
  const auto = q.get('autoplay') === '1';
  if (q.get('theme')) document.documentElement.setAttribute('data-theme', q.get('theme'));
  let flow = flows.find(f => f.id === wanted) || flows[0];
  let i = 0, playing = false, timer = null, tick = null, t0 = 0;
  const $ = id => document.getElementById(id);
  const stage = $('stage'), flowsEl = $('flows'), dotsEl = $('dots');

  function renderFlows(){
    flowsEl.innerHTML = '';
    for (const f of flows){
      const b = document.createElement('button'); b.textContent = f.title; b.setAttribute('role','tab');
      b.setAttribute('aria-current', f === flow ? 'true' : 'false');
      b.onclick = () => { flow = f; i = 0; load(); if (history.replaceState) history.replaceState(null,'', '?flow=' + f.id + (auto?'&autoplay=1':'')); };
      flowsEl.appendChild(b);
    }
  }
  function load(){
    $('title').textContent = flow.title; $('subtitle').textContent = flow.subtitle || '';
    for (const img of stage.querySelectorAll('img')) img.remove();
    flow.steps.forEach((s, k) => { const img = new Image(); img.src = s.image; img.alt = s.caption; img.loading = k < 2 ? 'eager' : 'lazy'; stage.appendChild(img); });
    dotsEl.innerHTML = '';
    flow.steps.forEach((s, k) => { const d = document.createElement('button'); d.setAttribute('aria-label', 'Step ' + (k+1)); d.onclick = () => { go(k); }; dotsEl.appendChild(d); });
    renderFlows(); show();
  }
  function show(){
    const imgs = stage.querySelectorAll('img');
    imgs.forEach((img, k) => img.classList.toggle('on', k === i));
    const s = flow.steps[i];
    $('caption').textContent = s.caption; $('step').textContent = 'Step ' + (i+1) + ' of ' + flow.steps.length;
    $('badge').textContent = flow.user ? 'signed in as ' + flow.user : '';
    dotsEl.querySelectorAll('button').forEach((d, k) => d.setAttribute('aria-current', k === i ? 'true' : 'false'));
    $('bar').style.width = '0%';
    if (playing) arm();
  }
  function go(k){ i = (k + flow.steps.length) % flow.steps.length; show(); }
  function arm(){
    clearTimeout(timer); clearInterval(tick);
    const hold = flow.steps[i].hold || 3200; t0 = Date.now();
    tick = setInterval(() => { $('bar').style.width = Math.min(100, (Date.now()-t0)/hold*100) + '%'; }, 100);
    timer = setTimeout(() => { go(i+1); }, hold);
  }
  function setPlaying(p){ playing = p; $('play').textContent = p ? 'Pause' : 'Play'; $('play').setAttribute('aria-label', p ? 'Pause' : 'Play'); if (p) arm(); else { clearTimeout(timer); clearInterval(tick); } }
  $('prev').onclick = () => go(i-1); $('next').onclick = () => go(i+1); $('play').onclick = () => setPlaying(!playing);
  document.addEventListener('keydown', e => { if (e.key === 'ArrowRight') go(i+1); else if (e.key === 'ArrowLeft') go(i-1); else if (e.key === ' ') { e.preventDefault(); setPlaying(!playing); } });
  load(); if (auto) setPlaying(true);
})();
</script>
</body>
</html>
"""

GALLERY_HTML = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Orb modules</title>
<style>
  :root { --bg:#0b0d12; --panel:#121620; --line:#242a36; --text:#f5f4ee; --muted:#9aa1ad; --accent:#4a9e8e; }
  :root[data-theme="light"] { --bg:#f6f5f1; --panel:#fff; --line:#e3e1da; --text:#15171c; --muted:#5f6672; --accent:#2f7f70; }
  * { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--text); font:15px/1.45 "Inter Variable", Inter, -apple-system, system-ui, sans-serif }
  .wrap { max-width:1280px; margin:0 auto; padding:24px 16px }
  header { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:18px }
  h1 { font-size:22px; margin:0; letter-spacing:-0.01em } header p { margin:4px 0 0; color:var(--muted) }
  .switch button { border:1px solid var(--line); background:var(--panel); color:var(--text); padding:7px 12px; border-radius:999px; font:inherit; cursor:pointer }
  .switch button[aria-pressed="true"] { background:var(--accent); color:#fff; border-color:var(--accent) }
  .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(360px, 1fr)); gap:18px }
  figure { margin:0; background:var(--panel); border:1px solid var(--line); border-radius:14px; overflow:hidden }
  figure img { display:block; width:100%; aspect-ratio:1512/1024; object-fit:cover; object-position:top; background:#000 }
  figcaption { padding:10px 12px 12px; font-size:14px } figcaption small { display:block; color:var(--muted); margin-top:2px; font-size:12px }
  h2 { font-size:15px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:26px 0 10px }
</style>
</head>
<body><div class="wrap">
<header><div><h1>Orb Hospital OS: every module</h1><p>Captured from a running appliance on seeded demo data.</p></div>
<div class="switch"><button data-t="dark" aria-pressed="true">Dark</button><button data-t="light" aria-pressed="false">Light</button></div></header>
<div id="root"></div>
</div>
<script id="shots" type="application/json">__SHOTS__</script>
<script>
(function(){
  const shots = JSON.parse(document.getElementById('shots').textContent);
  const groups = [['Clinical', /^(today|vigil|ward-round|orders|relay|sage|scribe|lens|pulse|keyboard|command-palette|admit|mfa|downtime|handoff)/],
                  ['Nurse mode', /^nurse/], ['Pharmacy', /^pharmacy/], ['Surgery', /^surgical/],
                  ['Forecast and command', /^(forecast|command-center|capacity|revenue|appointments|dietary)/],
                  ['Hospital modules', /^module-/], ['Admin and governance', /^admin-/], ['Patient portal', /^bridge/], ['Sign-in', /^login/]];
  let theme = 'dark';
  function render(){
    document.documentElement.setAttribute('data-theme', theme);
    document.querySelectorAll('.switch button').forEach(b => b.setAttribute('aria-pressed', b.dataset.t === theme ? 'true' : 'false'));
    const root = document.getElementById('root'); root.innerHTML = '';
    const used = new Set();
    for (const [label, re] of groups){
      const items = shots.filter(s => s.theme === theme && re.test(s.name) && !used.has(s.name));
      if (!items.length) continue;
      items.forEach(s => used.add(s.name));
      const h = document.createElement('h2'); h.textContent = label; root.appendChild(h);
      const g = document.createElement('div'); g.className = 'grid';
      for (const s of items){ const f = document.createElement('figure'); f.innerHTML = '<a href="../' + s.file + '" target="_blank" rel="noopener"><img loading="lazy" src="../' + s.file + '" alt="' + s.title.replace(/"/g,'&quot;') + '"></a><figcaption>' + s.title + '<small>' + s.file.split('/').pop() + '</small></figcaption>'; g.appendChild(f); }
      root.appendChild(g);
    }
  }
  document.querySelectorAll('.switch button').forEach(b => b.onclick = () => { theme = b.dataset.t; render(); });
  render();
})();
</script>
</body></html>
"""


def main() -> None:
    flows_out = []
    ORDER = ["ward-is-alive", "allergy-interlock", "wrong-room-warning", "sage-ask", "sage-refusal",
             "nurse-shift", "show-your-work", "scribe-note", "pharmacy-verify", "patient-portal"]
    manifests = sorted(FRAMES.glob("*/manifest.json"),
                       key=lambda m: (ORDER.index(m.parent.name) if m.parent.name in ORDER else 99, m.parent.name))
    for mpath in manifests:
        manifest = json.loads(mpath.read_text())
        flow_dir = mpath.parent
        gif = build_gif(flow_dir, manifest)
        webps = export_webp(flow_dir, manifest)
        steps = []
        for s, img in zip([s for s in manifest.get("steps") or [] if (flow_dir / s["file"]).exists()], webps):
            steps.append({"caption": s["caption"], "image": img, "hold": int(s.get("hold", 3200))})
        flows_out.append({"id": manifest["id"], "title": manifest["title"], "subtitle": manifest.get("subtitle", ""),
                          "user": manifest.get("user"), "steps": steps, "gif": f"gifs/{manifest['id']}.gif" if gif else None,
                          "video": manifest.get("video")})
        print(f"{manifest['id']}: {len(steps)} frames, gif={gif.name if gif else 'none'} ({gif.stat().st_size // 1024} KB)" if gif else f"{manifest['id']}: no frames")

    WIDGETS.mkdir(parents=True, exist_ok=True)
    if flows_out:
        html = PLAYER_HTML.replace("__MANIFEST__", json.dumps(flows_out).replace("</", "<\\/")).replace("__THEME__", "dark")
        (WIDGETS / "workflow-player.html").write_text(html)
        print("wrote widgets/workflow-player.html")

    idx = SHOTS / "index.json"
    shots = json.loads(idx.read_text()) if idx.exists() else []
    if shots:
        (WIDGETS / "gallery.html").write_text(GALLERY_HTML.replace("__SHOTS__", json.dumps(shots).replace("</", "<\\/")))
        print(f"wrote widgets/gallery.html ({len(shots)} screenshots)")

    readme = ["# Orb website assets", "",
              "Everything here was captured from a running Orb appliance on the seeded demo database (no mock-ups).", "",
              "## Module screenshots", "",
              "`screenshots/dark/*.png` and `screenshots/light/*.png`, 1512×1024 logical pixels at 2× (3024×2048). "
              "`screenshots/index.json` lists every file with a title. `widgets/gallery.html` shows them all with a light/dark switch (open it next to the screenshots folder).", "",
              "## Workflow demos", ""]
    for f in flows_out:
        readme.append(f"- **{f['title']}** (`{f['id']}`, {len(f['steps'])} steps): `gifs/{f['id']}.gif`" + (f", `{f['video']}`" if f.get('video') else "") + f", player: `widgets/workflow-player.html?flow={f['id']}&autoplay=1`")
    readme += ["", "## Embedding", "",
               "Copy the `widgets/` folder (it is self-contained, frames included) and the `gifs/` and `video/` folders to the website, then:", "",
               "```html", '<!-- interactive player, one workflow, autoplaying -->',
               '<iframe src="/assets/orb/widgets/workflow-player.html?flow=allergy-interlock&autoplay=1&theme=dark"',
               '        width="100%" height="900" style="border:0;border-radius:14px" loading="lazy"',
               '        title="Orb: the allergy interlock"></iframe>', "",
               '<!-- animated GIF (captioned, 1200 px wide) -->',
               '<img src="/assets/orb/gifs/allergy-interlock.gif" alt="Orb refuses to apply a sepsis bundle to a patient with a ceftriaxone anaphylaxis" width="1200">', "",
               '<!-- WebM video (smallest, smoothest; Safari 16+, Chrome, Firefox, Edge) -->',
               '<video src="/assets/orb/video/allergy-interlock.webm" autoplay muted loop playsinline width="1200"></video>', "```", "",
               "`theme=light` on the player URL flips the frame chrome; the frames themselves were captured in the app's dark theme.", "",
               "## Notes on the captures", "",
               "- Real logins on the seeded demo ward (DOC001, DOC004, NUR001, PHA001, ADM001, patient P001). Nothing is mocked; Sage, Scribe and Bridge answers came from medgemma:27b and medgemma:4b running locally through Ollama.",
               "- The capture laptop has no Whisper runtime installed, so Scribe's \"transcription engine unavailable\" notice was hidden with a CSS rule during capture. Everything else is exactly what the app renders. The typed-SIGN confirmation in the Scribe demo is real: it appears whenever a note was not verified against audio.",
               "- The admin MFA wall was lifted for the capture process only (ORB_MFA_REQUIRED_ROLES empty in that process); `.env` was not changed.",
               "- Sage was switched to Fast mode in the Ask Sage demo; that preference is stored per user and shows in later Sage screenshots as the `Fast` pill.",
               "- Clocks and dates in the screenshots are the capture day. The demo seed dates some progress notes one to two days after admission, which can read as a future date on a fresh seed.", "",
               "## Regenerating", "",
               "Reseed first so the ward looks fresh (the Sepsis Six clock and the alert times come from seed time), and start the backend with the admin MFA wall lifted for that process only, or the admin screens stop at the MFA enrolment prompt. Run the workflows before the gallery: signing the Scribe note is what fills the pharmacist verify queue.", "",
               "```bash", "cd server && set -a && . ../.env && set +a && .venv/bin/python scripts/reset_db_and_seed.py && cd ..",
               "cd server && set -a && . ../.env && set +a && ORB_MFA_REQUIRED_ROLES= .venv/bin/python main.py &   # built SPA on :8000; run npm run build first if src changed",
               "node docs/website-assets/capture.cjs --flows     # workflow frames + WebM (needs both models warm, about 10 min)",
               "node docs/website-assets/capture.cjs --gallery   # module screenshots, dark and light (about 25 min)",
               "server/.venv/bin/python docs/website-assets/build_assets.py   # GIFs, WebP frames, widgets, this README",
               "./stop-orb.sh                                    # stop the backend and unload the models (add --ollama to quit the Ollama app too)", "```", "",
               "## The live demo on the website", "",
               "orbsuite.com embeds the real front end with no backend: `vite build --config vite.demo.config.ts` builds the app in demo mode (demo/shim.ts answers every /api call from recordings, demo/fakeSocket.ts replays the realtime frames), and `node docs/website-assets/capture.cjs --record` writes those recordings, one JSON per persona, to docs/website-assets/fixtures while it drives the same flows and screens as the capture. Re-record after any API or seed change, then run `scripts/build_demo.sh` in the website repo, which builds the demo into public/demo and copies the recordings to public/demo-data.", "",
               "The website shows scripted workflows rather than the whole product: `demo/scripts.ts` holds one script per workflow (the steps the demo performs and the caption for each), `?script=<id>` locks the app to it (no sidebar, no palette) and the site steps through it. Each workflow also gets its own small recording, `fixtures/flow-<id>.json`, which is all a scripted demo loads; the persona file is only the fallback for a request outside the workflow. Every recording carries `recorded_at`, and the demo pins its clock to the end of the recording, so the ages on screen read as they did on the day it was recorded.", "",
               "```bash", "# backend started with ORB_MFA_REQUIRED_ROLES= and ORB_EXTRACT_TIMEOUT_SECONDS=420 (the note extraction that fills the pharmacist queue can take over two minutes on a busy GPU)", "ORB_THEMES=dark ORB_SHOTS_DIR=/tmp/rec-shots ORB_FRAMES_DIR=/tmp/rec-frames ORB_VIDEO_DIR=/tmp/rec-video node docs/website-assets/capture.cjs --record", "# one flow again, appended to the persona's existing recording:", "node docs/website-assets/capture.cjs --record --merge --only scribe", "```", "",
               "Known cosmetic quirk in the seeded data: the Bridge follow-up calendar labels a week that straddles two months by the earlier month (the week of 30 Aug to 5 Sep reads Aug 2026). Screens that look sparse on seed data and would benefit from richer seeding: Front Desk (no OPD queue), Pharmacy overview and patient board (no pharmacy orders), Forecast admission patterns (no admission history), Admin ingestion (no ingestion events).", ""]
    (ROOT / "README.md").write_text("\n".join(readme))
    print("wrote README.md")


if __name__ == "__main__":
    main()
