# Feature Reference — Onsite Validation Hub

Every panel in the sandbox, what it proves, and where its limits are.

The hub validates a Medallia onsite property end to end: which forms are eligible, whether
they render as designed, what the SDK reports, and how a submission is attributed. It is a
single static HTML file — everything below runs client-side against a real injected embed.

**A note on honesty.** Several things genuinely cannot be observed from this page, because the
feedback form runs in a cross-origin iframe. Those are labelled as reference or inconclusive
rather than being reported as passes or failures. Where a limit exists it is stated below.

---

## Getting started

```bash
python3 qa-server.py          # http://localhost:8080/
```

Paste a property embed into **🚀 Global Script Injection Engine** and press *Inject Property*.
The script is injected into the live page and every panel below begins populating.

Using `qa-server.py` rather than a plain static server additionally gives Vercel-style routing
(so `/page1` deep links work locally) and enables the one-click mobile launcher.

---

## Targeting and eligibility

### 🎯 Form Targeting Rules Matrix
Every published form the property exposes, read from the SDK's own registry, with each form's
configured targeting rules decoded into plain English **and evaluated live against current
storage**.

- Decodes `DeviceTypes`, `DontInviteOnDeclined`, `DontInviteOnSubmitted`, `InvitePerSession`,
  `UsersPercentage`, `NumberOfPagesViewed`, `NumberOfVisits`, `TimeOnPage`, `TimeInSession`,
  `UserAbandonment`, `GenericRule`, `UrlInclude`, `UrlExclude`
- Per-rule verdict: **eligible**, **blocked**, or **runtime** (rules that cannot be evaluated
  statically — see Journey Automation)
- Live per-form state pills: Loaded / Should Show / Shown / Invite Shown / Submitted
- The form currently on screen is highlighted and sorted to the top
- Filter by form ID, trigger type or display type; scope to rules-only, live-only, or all
- CSV export, one row per form-rule with its live verdict

Rules explain *why*, not just pass/fail — e.g. *"Invitation was declined 0.00 day(s) ago,
inside the 1-day window. Suppressed for another 1.00 day(s)."*

### 🗓️ Onsite Lifecycle State & Date Manager
The storage keys the SDK actually writes across the invitation and submission lifecycle:
`SUBMITTED_DATE`, `DECLINED_DATE`, `LAST_INVITATION_VIEW`, `kampyleUserSession`,
`kampyleInvitePresented`, `md_isSurveySubmittedInSession`, `kampyle_userid`.

Each shows the raw epoch plus a human-readable date, and every value is rewritable:

- Date picker, or presets: **Now / −1d / −7d / −30d / −90d**
- Toggle boolean flags, rewrite the user identifier, clear individual keys
- Purge all lifecycle keys to reset the browser to a first-visit respondent
- Copy the whole state as JSON

Back-dating is what makes quarantine windows testable — change `DECLINED_DATE` to 30 days ago
and the matching rule in the targeting matrix flips from blocked to eligible immediately, with
no waiting for real calendar time.

### 🧭 Journey Automation
Walks the scenario pages automatically on a configurable dwell and loop count, calling
`updatePageView()` at each hop so count- and time-based rules advance unattended.

Reports the session page counter and **any targeting verdict that flips** as it runs. These
are precisely the rules the matrix can only mark *runtime*, because they depend on accumulated
behaviour rather than static config.

### 📜 List of Enabled Provisions
The property's feature flags, read from the SDK's runtime memory store, showing how many of the
total are enabled. Useful for confirming a capability is switched on at property level before
chasing a bug in a form.

---

## Rendering and design

### 🎨 Design & Display Render Validator
Compares what targeting **configured** against what the SDK **actually rendered into the DOM**.
Colour values are normalised, so `#5081ff` and `rgb(80, 129, 255)` count as a match.

**Feedback button — fully asserted.** The button renders into this document, so every property
is readable: button type/shape (vertical, triangular, custom), text, background and text
colours, hover colours read from the SDK's own injected stylesheet, screen position, z-index,
corner margin, and visibility. Attribution to the owning form comes from the runtime event, not
from guessing registry order.

**Display mode — asserted.** Validates the configured `displayType` against the wrapper the SDK
actually rendered into, covering lightbox, animation, popup and embedded.

**Invitation — partly asserted.** Display type, direction and geometry are measured. Colours
are verified by pixel sampling (below). Text remains a visual check.

Values that exist only while a modal is on screen are **latched automatically** the instant the
surface appears, and replayed after it closes — with provenance showing when they were captured
and how many times the invitation has been presented. Without this, the measurement would only
exist while the invitation covered the dashboard, and would be gone by the time you could read
it.

### 🎨 Auto-Capture Invitation Colours
The invitation renders cross-origin, so its DOM is unreadable — but its **pixels** can be
sampled through a user-granted tab capture.

Because `getDisplayMedia()` requires a user gesture, it cannot be invoked at the moment an
invitation appears. Instead the permission is granted **once**, the stream is held open, and
every subsequent invitation is sampled automatically.

Each configured colour is reported as:

- **rendered** — found in the region, with pixel count and coverage
- **inconclusive** — too few matching pixels to conclude, which happens with small
  anti-aliased text
- **not found** — genuinely absent, so the rendered colour differs from configuration

An empty capture frame is retried rather than reported as a mismatch.

*Limits:* proves a colour is present in the region, not which element carries it. Text content
cannot be verified this way.

---

## Events and telemetry

### 📊 Live Analytics Events Tracking
Every `nebula_*` event the SDK sends to its collector, captured across XHR, `fetch` and
`sendBeacon`. Batched payloads are fully expanded — each event in a batch is surfaced, with its
complete ~27-field envelope rather than just a name.

Filter by name, form ID or trigger type; click any event to expand its full JSON; per-name
chips with counts; CSV export across 18 telemetry columns including `mdData` dates, session and
user IDs, form version and correlation UUID.

### 🛰️ Native SDK Custom Event Bus
The SDK's own client-side lifecycle broadcasts, captured directly from its internal dispatcher
and its `window` CustomEvent rebroadcast — **no network sniffing**. These fire the moment the
SDK reaches each stage, ahead of the batched analytics payloads.

Captured events include `neb_showInvitation`, `neb_inviteLoaded`, `neb_beforeFormShown`,
`neb_formShown`, `neb_formReady`, `neb_formPageShown`, `neb_afterHttpGetRequest`,
`MDigital_Form_Displayed`, `MDigital_Invite_Displayed`.

Kept deliberately separate from the network stream, so it is always clear which layer an event
came from. Each entry shows its emitter source and expands to full JSON.

### 🧾 Feedback Submission Inspector
One trace per form journey, built from the lifecycle the form reports back over `postMessage`.

- **Feedback UUID** — the record identifier, copyable, to find that exact response in the Inbox
  instead of hunting by timestamp
- Correlation UUID, form language, pages displayed, auto-submitted flag
- Screen capture capability (the property-level provision — not whether a form contains the
  component)
- Journey track: Loaded → Invite accepted → Page shown → Submitted → Thank-you, each timestamped
- Elapsed time from first page to submit
- Incomplete journeys stay amber, so forms opened but never submitted are visible too
- CSV export

*Limits:* the submitted answer values are **not** available. They travel from the form straight
to Medallia in a cross-origin request that this page cannot observe. Only identifiers and
lifecycle are obtainable client-side; answer content requires the Medallia API, keyed by the
Feedback UUID above.

### 🔴 Background Server Network Error Console
Failed requests and uncaught runtime errors, including console errors and unhandled promise
rejections, piped into one stream.

### 💻 Real-Time QA Execution Timeline Log
A timestamped narrative of everything the hub observes and every action taken, exportable and
clearable.

---

## Device and submission scope

### 📱 Device emulation (nav bar)
One control with two distinct modes, because they do different things:

- **Preview UI in this page** — hosts the sandbox in a device-sized frame so the form renders
  at genuine handset dimensions. Changes layout only.
- **Emulated tab** — opens a tab with the device's user agent and Client Hints overridden, so
  **SDK targeting** classifies the session as mobile or tablet.

### 📲 Live Device Preview
The sandbox re-hosted inside a phone chassis at real device dimensions. Because the SDK inside
measures the iframe's viewport, `position: fixed` anchors, responsive breakpoints and mobile
invitation layout all resolve as they would on-device.

### 🧾 Feedback Submission Scope
Shows exactly how far device emulation carries, so a submission is never mistaken for a genuine
mobile one:

| Scope | What it drives |
| --- | --- |
| Sandbox document | SDK device targeting |
| SDK classification | Which forms are eligible |
| Feedback form iframe | **How the submission is attributed in the Inbox** |

A page-level override cannot reach the cross-origin form iframe. The panel detects whether
browser-level emulation is active and turns green only when submissions genuinely register as
mobile — otherwise it warns and gives the exact steps.

### Mobile launcher
```bash
./launch-mobile-qa.sh ios          # or android / tablet
```
Opens Chrome with full DevTools-protocol device emulation — user agent **and** User-Agent
Client Hints together, in every frame including the cross-origin form.

This matters: the form reports its technical info from Client Hints, so spoofing the UA string
alone (including Chrome's `--user-agent` flag) still records **Mobile Device: No** with the real
desktop OS and browser version.

With `qa-server.py` running, the same thing is available as a button in the device menu.

---

## Configuration integrity

### 🧭 Config Drift Baseline
Snapshots every form's configured targeting rules and button design, plus the enabled
provisions, then diffs a later run against it.

Detects changed rule values, changed button design, forms added or removed, and provisions
toggling on or off:

```
provisions     someRetiredProvision      enabled  →  disabled          removed
form 463112    rule: DontInviteOnDeclined  7 day(s) → 1 day(s)         changed
form 626741    button: backgroundColor     #ff0000  → #5081ff          changed
form 916151    (entire form)               not in baseline → added     added
```

Baselines export and import as JSON, so one environment can be compared against another or a
baseline committed alongside the repo. A saved baseline survives the Hard Purge, which exists to
reset SDK respondent state rather than QA tooling.

Snapshots record **configured values only, never live verdicts** — verdicts depend on stored
dates and wall-clock time, so including them would report drift on every run and bury the real
changes.

---

## Simulation and workspace

### 🎯 Live Custom Parameter Builder
Injects URL query parameters, global JavaScript variables and cookies to satisfy targeting
conditions, with typed values (text, number, boolean, datetime, random). Active parameters are
listed and individually removable, and URL parameters are reflected in the address bar.

### 💻 Manual Code Form Injection
Calls `loadForm()` and `showForm()` directly against a form ID, with the SDK's response
surfaced — for exercising a form without satisfying its targeting.

### 📥 Live Onsite Embed Dropzones
Three monitored containers for embedded forms, with a mutation observer that announces when the
SDK populates one.

### 🔎 Container DOM & Storage Inspector
Live status of each dropzone and the SDK's numeric storage keys
(`kampyleUserPercentile`, `kampyleSessionPageCounter`, `kampyleUserSessionsCount`), each
directly overridable.

### ⚙️ Workspace Config & Responsive Viewport Scale
Viewport scaling from 50% to 400% to validate fluid layout, plus named profile slots for saving
and restoring embed snippets.

### 📂 Validation Scenarios Registry
Five scenario pages for multi-page targeting. An injected property persists across all of them,
so page-count and time-in-session rules can be exercised by navigating.

---

## Reference

| Need | Panel |
| --- | --- |
| Why didn't this form show? | Form Targeting Rules Matrix |
| Test a quarantine window without waiting | Lifecycle State & Date Manager |
| Advance page/time-based rules | Journey Automation |
| Does the button match its design? | Design & Display Render Validator |
| Did the invitation render its colours? | Auto-Capture Invitation Colours |
| Find this submission in the Inbox | Feedback Submission Inspector → Feedback UUID |
| Will it show on mobile? | Device emulation (emulated tab) |
| Will it be attributed as mobile? | Feedback Submission Scope + mobile launcher |
| Did config change between environments? | Config Drift Baseline |
| What did the SDK actually emit? | Analytics Tracking + Native SDK Custom Event Bus |

### Known limits

| Not available | Why |
| --- | --- |
| Submitted answer values | Sent from the cross-origin form iframe |
| Submit request status / errors | Same — the page cannot observe that request |
| Invitation text verification | Rendered cross-origin; pixels can be sampled, text cannot |
| Inbox confirmation | Requires the Medallia API, not a browser |
| Native mobile app SDK | Needs a real device, simulator or device cloud |
