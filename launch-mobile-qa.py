#!/usr/bin/env python3
"""
launch-mobile-qa.py — open the Onsite Validation Hub in a Chrome window that a feedback form
genuinely records as a mobile device.

WHY NOT JUST A USER AGENT STRING
--------------------------------
The Medallia form runs in a cross-origin iframe and reports its technical info from the
User-Agent Client Hints API (navigator.userAgentData), not from the UA string. Chrome's
--user-agent flag rewrites the string only, so the iframe still reports
{mobile: false, platform: "macOS"} and the Inbox records "Mobile Device: No" alongside the
real desktop Chrome version — even though every UA string on the page says iPhone.

Client Hints can only be overridden through the DevTools Protocol
(Emulation.setUserAgentOverride with userAgentMetadata), which is exactly what the DevTools
device toolbar uses. Playwright drives that protocol, so this launcher applies full device
emulation: UA string, Client Hints, viewport, touch and device scale factor together, in
every frame including the cross-origin form.

USAGE
    python3 launch-mobile-qa.py [ios|android|tablet] [url]

    python3 launch-mobile-qa.py
    python3 launch-mobile-qa.py android
    python3 launch-mobile-qa.py ios https://your-app.vercel.app/

Requires Playwright:  pip3 install playwright  &&  python3 -m playwright install chromium
"""
import os
import sys
import tempfile

DEVICE_PROFILES = {
    "ios": {"playwright_device": "iPhone 13", "label": "iPhone 13 (iOS)"},
    "android": {"playwright_device": "Pixel 5", "label": "Pixel 5 (Android)"},
    "tablet": {"playwright_device": "iPad (gen 7)", "label": "iPad (tablet)"},
}
DEFAULT_URL = "http://localhost:8080/niraj-onsite.html"


def main():
    device_key = (sys.argv[1] if len(sys.argv) > 1 else "ios").lower()
    target_url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_URL

    if device_key not in DEVICE_PROFILES:
        print("error: unknown device '%s' (expected: %s)" % (device_key, " | ".join(DEVICE_PROFILES)),
              file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("error: Playwright is required for full device emulation.", file=sys.stderr)
        print("       pip3 install playwright && python3 -m playwright install chromium", file=sys.stderr)
        print("", file=sys.stderr)
        print("       Without it, only the UA string can be spoofed, which leaves the form", file=sys.stderr)
        print("       reporting 'Mobile Device: No'. Use the DevTools device toolbar instead:", file=sys.stderr)
        print("       open the sandbox, press Cmd+Shift+M, pick a phone, then reload.", file=sys.stderr)
        return 1

    profile = DEVICE_PROFILES[device_key]
    # A persistent profile keeps the injected embed and storage between runs, which matters
    # because targeting depends on lifecycle keys accumulated across visits.
    shared_profile_dir = os.path.join(tempfile.gettempdir(), "qa-onsite-emulated-%s" % device_key)
    os.makedirs(shared_profile_dir, exist_ok=True)

    with sync_playwright() as playwright:
        device = playwright.devices[profile["playwright_device"]]

        print("Launching %s" % profile["label"])
        print("  url        : %s" % target_url)
        print("  user agent : %s" % device["user_agent"])
        print("  emulation  : Client Hints + UA string + viewport + touch (DevTools protocol)")

        launch_options = dict(device)
        launch_options.pop("default_browser_type", None)

        def start(profile_dir):
            return playwright.chromium.launch_persistent_context(
                profile_dir, channel="chrome", headless=False, args=["--no-first-run"],
                **launch_options,
            )

        try:
            context = start(shared_profile_dir)
            print("  profile    : %s" % shared_profile_dir)
        except Exception:
            # Chrome refuses to reuse a profile directory that another window already holds —
            # it hands the URL to that instance instead, which Playwright cannot drive. Falling
            # back to a throwaway profile means a second emulated window still opens with full
            # emulation rather than the run failing outright.
            disposable_profile_dir = tempfile.mkdtemp(prefix="qa-onsite-emulated-%s-" % device_key)
            try:
                context = start(disposable_profile_dir)
            except Exception as err:
                print("error: could not launch Chrome: %s" % err, file=sys.stderr)
                return 1
            print("  profile    : %s" % disposable_profile_dir)
            print("  note       : an emulated %s window was already open, so this one uses a" % profile["label"])
            print("               fresh profile and starts without previously stored state.")
        print()

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(target_url)

        print("Chrome is open with full device emulation.")
        print("Inject your embed, confirm 'Feedback Submission Scope' is green, then submit —")
        print("the record is attributed to %s, with Mobile Device: Yes." % profile["label"])
        print("\nClose the browser window to end this session.")

        # Block until the tester closes the window; the emulated context dies with this process.
        try:
            context.wait_for_event("close", timeout=0)
        except KeyboardInterrupt:
            print("\ninterrupted — closing.")
        except Exception:
            pass
        finally:
            try:
                context.close()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
