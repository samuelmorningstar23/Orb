# Orb website assets

Everything here was captured from a running Orb appliance on the seeded demo database (no mock-ups).

## Module screenshots

`screenshots/dark/*.png` and `screenshots/light/*.png`, 1512×1024 logical pixels at 2× (3024×2048). `screenshots/index.json` lists every file with a title. `widgets/gallery.html` shows them all with a light/dark switch (open it next to the screenshots folder).

## Workflow demos

- **The ward watches itself** (`ward-is-alive`, 5 steps): `gifs/ward-is-alive.gif`, `video/ward-is-alive.webm`, player: `widgets/workflow-player.html?flow=ward-is-alive&autoplay=1`
- **The allergy interlock** (`allergy-interlock`, 5 steps): `gifs/allergy-interlock.gif`, `video/allergy-interlock.webm`, player: `widgets/workflow-player.html?flow=allergy-interlock&autoplay=1`
- **The wrong-room warning** (`wrong-room-warning`, 3 steps): `gifs/wrong-room-warning.gif`, `video/wrong-room-warning.webm`, player: `widgets/workflow-player.html?flow=wrong-room-warning&autoplay=1`
- **Ask Sage** (`sage-ask`, 4 steps): `gifs/sage-ask.gif`, `video/sage-ask.webm`, player: `widgets/workflow-player.html?flow=sage-ask&autoplay=1`
- **The cross-department refusal** (`sage-refusal`, 3 steps): `gifs/sage-refusal.gif`, `video/sage-refusal.webm`, player: `widgets/workflow-player.html?flow=sage-refusal&autoplay=1`
- **The nurse's shift** (`nurse-shift`, 5 steps): `gifs/nurse-shift.gif`, `video/nurse-shift.webm`, player: `widgets/workflow-player.html?flow=nurse-shift&autoplay=1`
- **Show your work** (`show-your-work`, 4 steps): `gifs/show-your-work.gif`, `video/show-your-work.webm`, player: `widgets/workflow-player.html?flow=show-your-work&autoplay=1`
- **Scribe** (`scribe-note`, 5 steps): `gifs/scribe-note.gif`, `video/scribe-note.webm`, player: `widgets/workflow-player.html?flow=scribe-note&autoplay=1`
- **Pharmacist verification** (`pharmacy-verify`, 3 steps): `gifs/pharmacy-verify.gif`, `video/pharmacy-verify.webm`, player: `widgets/workflow-player.html?flow=pharmacy-verify&autoplay=1`
- **Bridge, the patient portal** (`patient-portal`, 2 steps): `gifs/patient-portal.gif`, `video/patient-portal.webm`, player: `widgets/workflow-player.html?flow=patient-portal&autoplay=1`

## Embedding

Copy the `widgets/` folder (it is self-contained, frames included) and the `gifs/` and `video/` folders to the website, then:

```html
<!-- interactive player, one workflow, autoplaying -->
<iframe src="/assets/orb/widgets/workflow-player.html?flow=allergy-interlock&autoplay=1&theme=dark"
        width="100%" height="900" style="border:0;border-radius:14px" loading="lazy"
        title="Orb: the allergy interlock"></iframe>

<!-- animated GIF (captioned, 1200 px wide) -->
<img src="/assets/orb/gifs/allergy-interlock.gif" alt="Orb refuses to apply a sepsis bundle to a patient with a ceftriaxone anaphylaxis" width="1200">

<!-- WebM video (smallest, smoothest; Safari 16+, Chrome, Firefox, Edge) -->
<video src="/assets/orb/video/allergy-interlock.webm" autoplay muted loop playsinline width="1200"></video>
```

`theme=light` on the player URL flips the frame chrome; the frames themselves were captured in the app's dark theme.

## Notes on the captures

- Real logins on the seeded demo ward (DOC001, DOC004, NUR001, PHA001, ADM001, patient P001). Nothing is mocked; Sage, Scribe and Bridge answers came from medgemma:27b and medgemma:4b running locally through Ollama.
- The capture laptop has no Whisper runtime installed, so Scribe's "transcription engine unavailable" notice was hidden with a CSS rule during capture. Everything else is exactly what the app renders. The typed-SIGN confirmation in the Scribe demo is real: it appears whenever a note was not verified against audio.
- The admin MFA wall was lifted for the capture process only (ORB_MFA_REQUIRED_ROLES empty in that process); `.env` was not changed.
- Sage was switched to Fast mode in the Ask Sage demo; that preference is stored per user and shows in later Sage screenshots as the `Fast` pill.
- Clocks and dates in the screenshots are the capture day. The demo seed dates some progress notes one to two days after admission, which can read as a future date on a fresh seed.

## Regenerating

Reseed first so the ward looks fresh (the Sepsis Six clock and the alert times come from seed time), and start the backend with the admin MFA wall lifted for that process only, or the admin screens stop at the MFA enrolment prompt. Run the workflows before the gallery: signing the Scribe note is what fills the pharmacist verify queue.

```bash
cd server && set -a && . ../.env && set +a && .venv/bin/python scripts/reset_db_and_seed.py && cd ..
cd server && set -a && . ../.env && set +a && ORB_MFA_REQUIRED_ROLES= .venv/bin/python main.py &   # built SPA on :8000; run npm run build first if src changed
node docs/website-assets/capture.cjs --flows     # workflow frames + WebM (needs both models warm, about 10 min)
node docs/website-assets/capture.cjs --gallery   # module screenshots, dark and light (about 25 min)
server/.venv/bin/python docs/website-assets/build_assets.py   # GIFs, WebP frames, widgets, this README
./stop-orb.sh                                    # stop the backend and unload the models (add --ollama to quit the Ollama app too)
```

## The live demo on the website

orbsuite.com embeds the real front end with no backend: `vite build --config vite.demo.config.ts` builds the app in demo mode (demo/shim.ts answers every /api call from recordings, demo/fakeSocket.ts replays the realtime frames), and `node docs/website-assets/capture.cjs --record` writes those recordings, one JSON per persona, to docs/website-assets/fixtures while it drives the same flows and screens as the capture. Re-record after any API or seed change, then run `scripts/build_demo.sh` in the website repo, which builds the demo into public/demo and copies the recordings to public/demo-data.

The website shows scripted workflows rather than the whole product: `demo/scripts.ts` holds one script per workflow (the steps the demo performs and the caption for each), `?script=<id>` locks the app to it (no sidebar, no palette) and the site steps through it. Each workflow also gets its own small recording, `fixtures/flow-<id>.json`, which is all a scripted demo loads; the persona file is only the fallback for a request outside the workflow. Every recording carries `recorded_at`, and the demo pins its clock to the end of the recording, so the ages on screen read as they did on the day it was recorded.

```bash
ORB_THEMES=dark ORB_SHOTS_DIR=/tmp/rec-shots ORB_FRAMES_DIR=/tmp/rec-frames ORB_VIDEO_DIR=/tmp/rec-video node docs/website-assets/capture.cjs --record
```

Known cosmetic quirk in the seeded data: the Bridge follow-up calendar labels a week that straddles two months by the earlier month (the week of 30 Aug to 5 Sep reads Aug 2026). Screens that look sparse on seed data and would benefit from richer seeding: Front Desk (no OPD queue), Pharmacy overview and patient board (no pharmacy orders), Forecast admission patterns (no admission history), Admin ingestion (no ingestion events).
