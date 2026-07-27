# 🔍 Creative Onsite Validation Hub

A high-performance, single-file testing sandbox engineered for client-side digital property validations and third-party tag management auditing (e.g., Medallia/Kampyle). 

This workbench simulates standard production multi-page configurations (`/page1`, `/page2`) natively over **Vercel's Edge Server Infrastructure** without relying on a dynamic backend framework.

📖 **[Feature Reference](FEATURES.md)** — every panel, what it proves, and where its limits are
🧭 **[How-To Guide](GUIDE.md)** — step-by-step recipes for common validation tasks

---

## 🏗️ Project Architecture & Directory Structure

To deploy successfully to Vercel, organize your local project folder exactly like this before pushing your codebase:

```text
your-qa-repo/
├── niraj-onsite.html        # The main, optimized testing application code
├── qa-server.py             # Local dev server: Vercel-style routing + mobile launch API
├── launch-mobile-qa.sh      # Opens the hub in Chrome with a genuine mobile user agent
└── vercel.json       # Vercel-specific absolute path routing instructions
```

---

## ▶️ Running Locally

```bash
python3 qa-server.py          # http://localhost:8080/
```

Use this rather than `python3 -m http.server`. It rewrites unknown paths to the app exactly as
`vercel.json` does, so `/page1` deep links and reloads behave the same locally as in production,
and it exposes the local API that powers the one-click mobile launch below. It binds to
`127.0.0.1` only.

A plain static server still works — the app detects the missing helper and falls back to
showing the command instead of a button.

---

## 📱 Mobile Device Validation

The hub emulates a device in two different scopes, and it matters which one a test needs:

| Scope | How | Reaches the cross-origin form iframe? |
| --- | --- | --- |
| **Targeting** (does the form show on mobile?) | Device dropdown in the nav bar | No — not required |
| **Submission attribution** (does the record say mobile?) | `./launch-mobile-qa.sh` or DevTools device mode | Yes — required |

The onsite SDK reads `navigator.userAgentData` (User-Agent Client Hints) first and falls back
to `navigator.userAgent`. A page-level override fixes the former in this document only — it
cannot cross into the Medallia form iframe, which is where a submission is attributed.

**Spoofing the UA string alone is not enough.** The form reports its technical info from
Client Hints, so a UA-string-only approach (including Chrome's `--user-agent` flag) still
records `Mobile Device: No` with the real desktop OS and Chrome version. Client Hints can only
be overridden through the DevTools Protocol, which is what the launcher and the DevTools device
toolbar both use.

**From the UI** (requires `qa-server.py`): pick **🚀 iPhone — launch Chrome** from the device
menu in the nav bar. A browser cannot start a process by itself, so this posts to the local
helper, which runs the launcher for you.

**From a terminal** (works with any server):

```bash
./launch-mobile-qa.sh              # iPhone against localhost
./launch-mobile-qa.sh android      # Pixel 5
./launch-mobile-qa.sh ios https://your-app.vercel.app/
```

Both forms run `launch-mobile-qa.py`, which needs Playwright:

```bash
pip3 install playwright && python3 -m playwright install chromium
```

Without Playwright the launcher explains itself and points at the DevTools device toolbar
(`⌘⇧M`), which achieves the same result manually.

The in-page **Feedback Submission Scope** panel reports which scope is currently active, so a
desktop-attributed submission is never mistaken for a mobile one.

> Native mobile app SDK properties cannot be validated from a browser at all — those need the
> app running on a real device, a local simulator (Xcode / Android Studio), or a device cloud.

```text
