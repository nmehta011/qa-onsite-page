# How-To Guide — Onsite Validation Hub

Task-oriented recipes. For what each panel does and where its limits are, see
[FEATURES.md](FEATURES.md).

---

## Contents

1. [Setup](#1-setup)
2. [First run — inject a property](#2-first-run--inject-a-property)
3. [Why isn't my form showing?](#3-why-isnt-my-form-showing)
4. [Test a re-invite quarantine without waiting days](#4-test-a-re-invite-quarantine-without-waiting-days)
5. [Exercise page-count and time-based rules](#5-exercise-page-count-and-time-based-rules)
6. [Validate the feedback button design](#6-validate-the-feedback-button-design)
7. [Validate the invitation design and colours](#7-validate-the-invitation-design-and-colours)
8. [Validate mobile targeting](#8-validate-mobile-targeting)
9. [Validate a mobile-attributed submission](#9-validate-a-mobile-attributed-submission)
10. [Find a test submission in the Inbox](#10-find-a-test-submission-in-the-inbox)
11. [Compare configuration between environments](#11-compare-configuration-between-environments)
12. [Reset between test runs](#12-reset-between-test-runs)
13. [Deploying](#13-deploying)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. Setup

```bash
cd qa-onsite-page
python3 qa-server.py          # http://localhost:8080/
```

Use this rather than `python3 -m http.server`. It adds two things that matter:

- **Vercel-style routing** — `/page1` deep links and reloads behave exactly as in production
- **The mobile launch API** — turns the mobile launcher into a button instead of a terminal command

A plain static server still works; the app detects the missing helper and falls back gracefully.

For the mobile launcher, install Playwright once:

```bash
pip3 install playwright && python3 -m playwright install chromium
```

---

## 2. First run — inject a property

1. Open the hub
2. Paste your embed into **🚀 Global Script Injection Engine**:
   ```html
   <script type="text/javascript" src="https://resources.<tenant>.com/.../embed.js" async></script>
   ```
3. Press **Inject Property**

Within a few seconds you should see the **🎯 Form Targeting Rules Matrix** report *Synced · N
forms*, the provisions list populate, and events appear in **📊 Live Analytics Events Tracking**.

The injected script persists across the five scenario pages and survives a Hot Reload, so you
only paste it once per session.

> **Tip:** save frequently used embeds into a profile slot under **⚙️ Workspace Config** so you
> can restore them with one click.

---

## 3. Why isn't my form showing?

1. Open **🎯 Form Targeting Rules Matrix**
2. Set the scope selector to **All published forms** and filter by your form ID
3. Expand the form

Every configured rule is listed with a verdict and a plain-English explanation:

| Verdict | Meaning |
| --- | --- |
| ✓ eligible | This rule permits the form right now |
| ✕ blocked | **This rule is what's stopping it** |
| ⓘ runtime | Depends on accumulated behaviour — see [recipe 5](#5-exercise-page-count-and-time-based-rules) |

Any red **blocked** row is your answer, and the explanation states the actual values involved —
for example *"Invitation was declined 0.00 day(s) ago, inside the 1-day window."*

If the form has no rules at all, it relies purely on its trigger type (button, invitation,
embedded or code).

---

## 4. Test a re-invite quarantine without waiting days

A form configured with *don't invite for N days after decline/submit* would normally need N days
of real time to retest.

1. Decline or submit the form once, so the date key is written
2. Open **🗓️ Onsite Lifecycle State & Date Manager**
3. Find `DECLINED_DATE` (or `SUBMITTED_DATE`)
4. Press a preset — **−7d**, **−30d**, **−90d** — or pick an exact date and press **Set**

The value is rewritten and targeting is re-evaluated immediately. Check the targeting matrix:
the matching rule flips from **blocked** to **eligible**, with the explanation updating to
*"...past the 1-day window. Form is eligible again."*

To simulate a brand-new respondent instead, press **🧹 Purge All Lifecycle Keys**.

---

## 5. Exercise page-count and time-based rules

Rules like `NumberOfPagesViewed`, `TimeOnPage` and `TimeInSession` show as **ⓘ runtime** because
they can't be evaluated statically — they need actual browsing.

1. Open **🧭 Journey Automation**
2. Set **Dwell per page** (e.g. `8` seconds) and **Full loops**
3. Press **▶ Run Journey**

It walks every scenario page, calling `updatePageView()` at each hop, and logs the session page
counter plus **any rule verdict that flips** as it goes. Press **■ Stop** to halt early.

---

## 6. Validate the feedback button design

1. Inject the property and let the feedback button render
2. Click it once, so the hub can attribute the button to the correct form via its runtime event
3. Open **🎨 Design & Display Render Validator**

The button group compares configured vs actually-rendered for shape, text, background and text
colours, hover colours, position, z-index and visibility. Colour formats are normalised, so
`#5081ff` matching `rgb(80, 129, 255)` is a pass.

Anything marked **✕ mismatch** is a genuine difference between targeting config and the DOM.

> Rows marked **ⓘ reference** are informational — for example, hover colours when the SDK emits
> no hover rule for that button type.

---

## 7. Validate the invitation design and colours

The invitation renders cross-origin, so its DOM is unreadable — colours are verified from pixels
instead.

**Arm the capture before the invitation appears:**

1. Open **🎨 Design & Display Render Validator**
2. Press **🎨 Auto-Capture Invitation Colours**
3. Grant screen capture when prompted (choose this tab)

That's the only click needed. Every invitation from then on is sampled automatically the moment
it renders — you don't need to interact while it's on screen.

Each configured colour then reports:

| Result | Meaning |
| --- | --- |
| ✓ rendered | Found in the invitation region, with pixel count and coverage |
| ⓘ inconclusive | Too few matching pixels — normal for small anti-aliased text |
| ✕ not found | The rendered colour differs from configuration |

Geometry, display type and direction are asserted separately, and are **latched** — they remain
visible after the invitation closes, labelled with when they were captured.

Press **⏹ Stop Colour Auto-Capture** when finished; results are retained.

---

## 8. Validate mobile targeting

Use this to answer *"does this form show on mobile / not on desktop?"*

1. In the nav bar, open the device menu
2. Under **Emulated tab**, choose **📱 iPhone**, **Android** or **Tablet**
3. A new tab opens — inject your property there

The SDK now classifies the session as mobile, and `DeviceTypes` rules evaluate against it. A
form restricted to mobile becomes eligible there and stays blocked in your desktop tab.

To check mobile *layout* without changing targeting, use **Preview UI in this page** instead —
it renders the sandbox at true handset dimensions inside a phone frame.

---

## 9. Validate a mobile-attributed submission

Targeting emulation is **not** enough to make a submission register as mobile in the Inbox. The
form reports its device from User-Agent Client Hints inside a cross-origin iframe, which a page
cannot override.

**Either** use the launcher:

```bash
./launch-mobile-qa.sh ios          # or android / tablet
```

**or**, with `qa-server.py` running, pick **🚀 iPhone — launch Chrome** from the device menu.

**or** use Chrome DevTools: `⌘⇧M` → pick a phone → reload.

Then, before submitting, confirm **🧾 Feedback Submission Scope** is green:

> ✅ Browser-level emulation active — submissions are genuinely mobile

If it's red, the submission will still be recorded as desktop. Submit only once it's green.

> ⚠️ Chrome's `--user-agent` flag alone is **not** sufficient — it changes the UA string but not
> Client Hints, so the Inbox still shows *Mobile Device: No*.

---

## 10. Find a test submission in the Inbox

1. Submit feedback as normal
2. Open **🧾 Feedback Submission Inspector**
3. Copy the **Feedback UUID** from the newest (green) card
4. Search that UUID in the Medallia Inbox

The card also shows the journey with timings — Loaded → Invite accepted → Page shown →
Submitted → Thank-you — and the elapsed time from first page to submit.

Amber cards are journeys that were opened but **never submitted**, which is useful on its own
for spotting abandonment or a form erroring before completion.

> The submitted answer values are not shown — they go straight from the form to Medallia in a
> request this page can't see. The UUID is how you reach them.

---

## 11. Compare configuration between environments

To catch config drift between QA and Production, or an unintended change:

**Capture a baseline**

1. Inject the property in the first environment
2. Open **🧭 Config Drift Baseline** → **💾 Save Baseline**
3. Optionally **📤 Export JSON** to keep or commit it

**Compare**

1. Inject the other environment's property (or re-inject after a config change)
2. **📥 Import JSON** if you're using an exported baseline
3. Press **🔍 Compare Now**

Differences are listed by scope with before/after values — changed rules, changed button design,
forms added or removed, provisions toggled. Identical config reports **No drift detected**.

Baselines survive the Hard Purge, so a reset between runs won't lose your reference.

---

## 12. Reset between test runs

| Goal | Action |
| --- | --- |
| Fresh respondent, keep the injected script | **🧹 Purge All Lifecycle Keys** |
| Re-run the SDK against the same script | **⚡ Hot Reload Form** |
| Completely clean slate | **🔄 Hard Purge & Reset** in the nav bar |
| Clear one captured stream | **Clear** in that panel's toolbar |

**Hard Purge** clears all storage and cookies and reloads — but deliberately preserves your saved
config baseline.

---

## 13. Deploying

Pushing to `main` triggers a Vercel production deploy. Any other branch gets its own preview
deployment automatically — no merge needed — and a pull request gets the preview link posted on
it.

`vercel.json` rewrites all paths to the app, which is what makes `/page1`-style routes work in
production. `qa-server.py` mirrors that locally.

---

## 14. Troubleshooting

**Nothing appears after injecting**
Check **🔴 Background Server Network Error Console** for a failed embed request, and confirm the
script tag is complete. The targeting matrix pill should read *Synced · N forms* once the SDK is
up.

**Submission Inspector stays empty**
It populates from form lifecycle messages, so it only fills once a form actually loads. If a form
is on screen and it's still empty, check the browser console for errors.

**Invitation colours all show "not found"**
The capture may have grabbed an empty frame. Stop and re-arm **🎨 Auto-Capture Invitation
Colours**, and make sure you shared *this tab* rather than another window.

**Inbox says Mobile Device: No despite emulating**
Expected unless browser-level emulation is active — see [recipe 9](#9-validate-a-mobile-attributed-submission).
Confirm **🧾 Feedback Submission Scope** is green *before* submitting.

**The device-menu launch button only shows a command**
The local helper isn't running. Start the hub with `python3 qa-server.py` and reload. On Vercel
this is expected — a hosted page can't start a browser on your machine.

**`./launch-mobile-qa.sh` reports Playwright missing**
```bash
pip3 install playwright && python3 -m playwright install chromium
```
Or use the DevTools device toolbar instead, which achieves the same result manually.

**`/page1` 404s locally**
You're on a plain static server. Use `python3 qa-server.py`, which rewrites paths the way Vercel
does.
