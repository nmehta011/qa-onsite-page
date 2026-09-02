# 🔍 Onsite Validation Hub

A single-file browser tool for testing **Medallia / Kampyle onsite feedback forms before they
ship** — targeting, design, accessibility, performance, privacy and security, all verified live
against a real property rather than mocked.

Paste a property's embed snippet, click **Inject Property**, and everything downstream is computed
from what the real SDK is doing on the page. Where a check can't be answered confidently (usually
because content renders inside a cross-origin iframe), it says so plainly instead of guessing.

| | |
| --- | --- |
| **0** external dependencies | no CDN, no fonts, no framework |
| **25** diagnostic panels | grouped into 6 task-based workspaces |
| **111** regression tests | driven against a live property, no mocks |
| **~600 KB** | one HTML file, runs from `file://` if you have to |

---

## 📘 Start with the guide

A full walkthrough of every feature — with a 3½-minute captioned video and 33 screenshots, all
captured live — lives in [`docs/`](docs/).

| Format | File | Best for |
| --- | --- | --- |
| **Web (interactive)** | [`docs/onsite-hub-guide.html`](docs/onsite-hub-guide.html) | Reading it properly — the video plays inline |
| **PDF** | [`docs/onsite-hub-guide.pdf`](docs/onsite-hub-guide.pdf) | Sharing by email/Slack, printing, offline |
| **Video only** | [`docs/onsite-hub-walkthrough.mp4`](docs/onsite-hub-walkthrough.mp4) | A 3½-minute overview with no reading |

### Opening the HTML guide

GitHub does **not** render HTML from the file view or a `raw.githubusercontent.com` link — it
serves it as plain text. Pick one of these instead:

- **Share a live link (best):** enable GitHub Pages once — repo **Settings → Pages → Source:
  *Deploy from a branch* → Branch: `main`, Folder: `/docs`**. The guide is then permanently at
  `https://<owner>.github.io/<repo>/`, and `docs/index.html` redirects the bare URL straight to
  it. Re-run nothing on future updates; pushing to `main` republishes it.
  *(On a private repo, Pages requires a paid GitHub plan — use the PDF instead.)*
- **No setup:** download `docs/onsite-hub-guide.pdf` and share that. It is fully self-contained.
- **Locally:** clone the repo and open `docs/onsite-hub-guide.html` in a browser.

> Every image and the video are embedded directly in the HTML and the PDF, so both files work
> standalone — no folder of assets to keep alongside them.

<details>
<summary><strong>Hosting the HTML yourself (S3, or any static bucket)</strong></summary>

The guide declares `<meta charset="utf-8">`, so it renders correctly even when a server sends no
charset — which is what S3 does by default. If you upload it and see `â€"` where an em dash
should be, or `Â½` in "3½-minute", the file was uploaded with the wrong `Content-Type`. Set it
explicitly:

```bash
aws s3 cp docs/onsite-hub-guide.html s3://your-bucket/onsite-hub-guide.html \
  --content-type "text/html; charset=utf-8"
```

Do the same for `docs/index.html`. The mp4 wants `--content-type "video/mp4"` so it streams
rather than downloads.

</details>

---

## ▶️ Running the tool

```bash
python3 qa-server.py          # http://localhost:8080/
```

Use this rather than `python3 -m http.server`. It rewrites unknown paths to the app exactly as
`vercel.json` does, so `/page1` deep links and reloads behave the same locally as in production,
and it exposes the local API that powers the one-click mobile launch below. It binds to
`127.0.0.1` only.

A plain static server still works — the app detects the missing helper and falls back to showing
the command instead of a button.

**Then:** paste a property embed snippet into the box on the dashboard and click **Inject
Property**. The SDK typically connects in under two seconds.

---

## 🧭 What's in it

Panels are grouped by the question you're trying to answer, not by feature category. Switch with
the workspace tabs, or press <kbd>⌘</kbd><kbd>K</kbd> for the command palette.

### 🎯 Targeting — *will this form show, and if not, why not?*

The core of the tool. Every published form gets a plain-English verdict evaluated live against the
real stored state, plus a **one-click fix** that writes exactly the value the rule needs and then
calls `updatePageView()` so the SDK actually re-targets.

Fixes are offered for visit counts, page-view counts, user-percentile sampling, URL rules, the
submitted/declined date quarantines, the once-per-session cap, and rule-engine criteria (custom
parameter conditions, including nested AND/OR groups).

Also here: full **Onsite Lifecycle State** editing (every storage key the SDK reads, with
back-date shortcuts) and **Journey Automation**.

### 🎨 Appearance — *does it look and behave right?*

- **Design & Display Render Validator** — configured vs. actually rendered
- **WCAG Contrast Audit** — measured from real rendered pixel colours, not from config
- **Full Accessibility Audit** — accessible names, keyboard reachability, dialog semantics
- **Live Device Preview** — a real device-sized viewport

### 📡 Traffic — *what did the SDK actually do?*

- **Live Analytics Events Tracking** — every outbound payload, with full JSON per event
- **Native SDK Custom Event Bus** — the SDK's own lifecycle broadcasts, ahead of the batched analytics
- **Feedback Submission Inspector** — the whole journey with real timings, including the
  **feedbackUUID** you can search in the Medallia Inbox to open that exact response
- **Unified Session Timeline** — every stream above on one clock
- **Page containers & stored values** — what the page holds, plus the failed-request stream
- **Activity log** — the running execution log

### 🩺 Health — *is anything wrong with it?*

- **Diagnostics** — what is broken and what to do about it
- **Performance & Core Web Vitals** — scoped to since-injection, so an idle tab can't inflate it
- **Privacy / PII Leak Scan** — Luhn-validated, results masked
- **Security Posture** — mixed content, CSP violations, injection hygiene
- **What Changed Since Last Run**, **Bug Report & Evidence**

### 🛠️ Setup — *configuration and manual control*

Custom parameters and variables, manual `loadForm` / `showForm` / `closeForm`, URL simulation,
viewport controls, config drift baselines, environment A/B comparison, and **Provisions &
overrides** — every provision the property ships, each one switchable at runtime.

### 🐞 Debugger — *the raw objects, not this tool's reading of them*

Every other workspace interprets the SDK's configuration. This one shows it: property config,
provisions, published forms, form definitions, **component roles**, raw targeting rules, custom
parameters, SDK memory, browser storage and console output — as the JSON the SDK is actually
running on, with search and copy. Edits are previewed as a path-level diff, applied to the live
object in the page, and reversible; nothing is ever sent to Medallia.

**Component roles** lists every field on every form with the role its type renders as, the key it
submits under, its validation and what a screen reader is given to announce — flagging required
fields with no error message, missing autocomplete attributes and answer ids that collide.

### 🧪 Scenario pages

Five fixture pages reproduce conditions an empty sandbox can't: hostile layout (sticky footers,
cookie banners at max z-index, promo popups), async interception, adaptive viewport, consent
gating, and behavioural rule isolation.

---

## ✅ Tests

```bash
pip3 install playwright pytest && python3 -m playwright install chromium
python3 -m pytest tests/ -q          # ~2 minutes
```

Every test drives the real app in a real browser against a real Medallia property — there are no
mocks, because almost every bug this app has had came from an assumption about the SDK that turned
out to be wrong. Each one exists because the behaviour it covers broke at some point.

Tests find forms by the rule or trigger type under test rather than by form ID, and skip rather
than fail when the property publishes nothing to assert on. **A red suite should mean this app
broke, not that someone edited a form.**

---

## 📱 Mobile device validation

The hub emulates a device in two different scopes, and it matters which one a test needs:

| Scope | How | Reaches the cross-origin form iframe? |
| --- | --- | --- |
| **Targeting** (does the form show on mobile?) | Device dropdown in the nav bar | No — not required |
| **Submission attribution** (does the record say mobile?) | `./launch-mobile-qa.sh` or DevTools device mode | Yes — required |

The onsite SDK reads `navigator.userAgentData` (User-Agent Client Hints) first and falls back to
`navigator.userAgent`. A page-level override fixes the former in this document only — it cannot
cross into the Medallia form iframe, which is where a submission is attributed.

**Spoofing the UA string alone is not enough.** The form reports its technical info from Client
Hints, so a UA-string-only approach (including Chrome's `--user-agent` flag) still records
`Mobile Device: No` with the real desktop OS and Chrome version. Client Hints can only be
overridden through the DevTools Protocol, which is what the launcher and the DevTools device
toolbar both use.

**From the UI** (requires `qa-server.py`): pick **🚀 iPhone — launch Chrome** from the device menu
in the nav bar. A browser cannot start a process by itself, so this posts to the local helper,
which runs the launcher for you.

**From a terminal** (works with any server):

```bash
./launch-mobile-qa.sh              # iPhone against localhost
./launch-mobile-qa.sh android      # Pixel 5
./launch-mobile-qa.sh ios https://your-app.vercel.app/
```

Both forms run `launch-mobile-qa.py`, which needs Playwright (see the test setup above). Without
it the launcher explains itself and points at the DevTools device toolbar (<kbd>⌘</kbd><kbd>⇧</kbd><kbd>M</kbd>),
which achieves the same result manually.

The in-page **Feedback Submission Scope** panel reports which scope is currently active, so a
desktop-attributed submission is never mistaken for a mobile one.

---

## ⚠️ Known limits

These are structural, not bugs. The tool states them rather than guessing — the full list is in
the guide's *Known limits* section.

- **Cross-origin content.** The invitation and form render in a cross-origin iframe. This page can
  confirm the iframe exists and whether focus moved into it, but cannot read what a respondent
  typed or verify a handler bound inside that frame.
- **`customEventsBroadcast`.** The SDK Custom Event Bus panel only populates when that provision is
  enabled for the property. The panel detects this and says so.
- **Behavioural timers.** `TimeOnPage` lives in the SDK's in-memory state with no storage behind
  it, so it can't be fast-forwarded. `TimeInSession` is anchored to a stored timestamp and can be.
- **Device targeting** comes from the SDK's reading of the real user agent at init — use the
  device dropdown's tab/launch options rather than trying to fake it in place.
- **Native mobile app SDK properties** can't be validated from a browser at all — those need a real
  device, a simulator, or a device cloud.

---

## 🏗️ Project structure

```text
qa-onsite-page/
├── niraj-onsite.html              # The tool — one self-contained file
├── qa-server.py                   # Local dev server: Vercel-style routing + mobile launch API
├── launch-mobile-qa.sh / .py      # Opens the hub in a genuinely emulated mobile Chrome
├── vercel.json                    # Path rewrites so /page1 deep links survive a reload
├── tests/
│   └── test_onsite_hub.py         # Live-property regression suite
└── docs/
    ├── index.html                 # Redirect, so GitHub Pages serves the guide at the site root
    ├── onsite-hub-guide.html      # Team guide (video + screenshots embedded)
    ├── onsite-hub-guide.pdf       # Same guide, shareable/printable
    └── onsite-hub-walkthrough.mp4 # 3½-minute captioned walkthrough
```

## 🚀 Deployment

Currently deployed on **Vercel**, which is the right fit: one static file plus a single rewrite
rule. `vercel.json` maps every path back to the app so `/page1` deep links and repro permalinks
survive a reload.

If it ever has to move, the constraint to preserve is that rewrite — repro permalinks embed a path
(`/Page1?qa_script=…`), so a host without one will 404 them. **AWS Amplify Hosting** is the closest
equivalent inside AWS (rewrite rules, free TLS, built-in password protection); raw **S3 +
CloudFront** works too, with a viewer-request function doing the rewrite. Plain S3 website hosting
alone does not.

The mobile-launch helper (`/api/health`, `/api/launch-mobile`) only exists when running locally via
`qa-server.py` — a browser can't start a process by itself. It degrades correctly everywhere else.
