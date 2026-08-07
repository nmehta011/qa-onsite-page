"""
Regression tests for the Onsite Validation Hub.

Every test here exists because the behaviour it covers was broken at some point, was fixed, and
must not silently regress again. They drive the real app in a real browser against a real
Medallia property — there are no mocks, because almost every bug this app has had came from an
assumption about the SDK that turned out to be wrong.

Run:
    pip install playwright pytest && playwright install chromium
    python3 -m pytest tests/ -v

The server is started and stopped automatically (qa-server.py, not `python -m http.server`:
the app's Hard Purge navigates to "/", which only qa-server.py rewrites to the HTML file).
"""

import json
import socket
import subprocess
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = 8937
BASE = f"http://localhost:{PORT}/"

# A real published property with all four form types: embedded, invitation (animation),
# button (lightbox, URL-targeted to /Page1) and code-triggered. Several tests below assert on
# that specific mix, so swapping this constant will legitimately fail them.
EMBED = ('<script type="text/javascript" '
         'src="https://resources.digital-cloud-qa-web.medallia.com/websites/165099/onsite/embed.js" '
         'async></script>')


def _port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def server():
    proc = subprocess.Popen(["python3", "qa-server.py", str(PORT)], cwd=REPO_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if _port_open(PORT):
            break
        time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail(f"qa-server.py did not start on port {PORT}")
    yield BASE
    proc.terminate()


@pytest.fixture
def page(server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(server)
        pg.wait_for_load_state("networkidle")
        pg.evaluate("localStorage.clear(); sessionStorage.clear();")
        pg.reload()
        pg.wait_for_load_state("networkidle")
        pg.errors = errors
        yield pg
        browser.close()


def inject(pg, wait_ms=13000):
    pg.fill("#input-main-page", EMBED)
    pg.click("#btn-main-page")
    pg.wait_for_function("() => !!window.KAMPYLE_ONSITE_SDK", timeout=30000)
    pg.wait_for_timeout(wait_ms)


# --------------------------------------------------------------------------------------------
# Trust: the app must never claim success it hasn't verified.
# --------------------------------------------------------------------------------------------

def test_failed_injection_reports_failure_not_success(page):
    """Regression: appending a <script> that 404s used to render a green
    'deployed successfully' message while the status bar said 'No property injected'."""
    page.fill("#input-main-page",
              '<script src="https://resources.digital-cloud-qa-web.medallia.com/websites/999999/onsite/embed.js"></script>')
    page.click("#btn-main-page")

    page.wait_for_timeout(2000)
    assert "waiting for the SDK" in page.locator("#msg-main-page").inner_text()

    page.wait_for_timeout(25000)
    message = page.locator("#msg-main-page").inner_text()
    assert "error" in page.evaluate("document.getElementById('msg-main-page').className")
    assert "did not load" in message
    assert not page.evaluate("!!window.KAMPYLE_ONSITE_SDK")


def test_successful_injection_reports_the_property_id(page):
    inject(page)
    message = page.locator("#msg-main-page").inner_text()
    assert "loaded and running" in message
    assert page.evaluate("sdkReadyElapsedMs") > 0


# --------------------------------------------------------------------------------------------
# The core question: will this form show, and if not, why not.
# --------------------------------------------------------------------------------------------

def test_all_four_form_types_are_enumerated(page):
    inject(page)
    types = page.evaluate(
        "() => (readPublishedFormRegistryFromSdk()||[]).map(f => f.formType).sort()")
    assert types == ["button", "code", "embedded", "invitation"]


def test_visit_count_rule_is_computed_not_deferred(page):
    """Regression: NumberOfVisits reported 'info' (unknowable at runtime) even though both the
    requirement and the live counter were already in hand."""
    inject(page)
    rule = page.evaluate("""() => {
        const f = (readPublishedFormRegistryFromSdk()||[]).find(x => x.formType === 'embedded');
        return decodeTargetingRulesForForm(f).find(r => r.name === 'NumberOfVisits');
    }""")
    assert rule["verdict"] == "block", "first visit should be a definite block, not 'info'"
    assert rule["fix"], "a computable block should offer a one-click fix"


def test_one_click_fix_unblocks_the_form(page):
    inject(page)
    page.evaluate("setActiveWorkspace('targeting')")
    page.wait_for_timeout(1200)

    before = page.evaluate("() => buildFormShowVerdict("
                           "(readPublishedFormRegistryFromSdk()||[]).find(f=>f.formType==='embedded'),"
                           "decodeTargetingRulesForForm((readPublishedFormRegistryFromSdk()||[]).find(f=>f.formType==='embedded'))).tone")
    assert before == "block"

    buttons = page.locator(".form-verdict-fix")
    for i in range(buttons.count()):
        if "visits" in buttons.nth(i).inner_text():
            buttons.nth(i).click()
            break
    page.wait_for_timeout(2000)

    after = page.evaluate("() => buildFormShowVerdict("
                          "(readPublishedFormRegistryFromSdk()||[]).find(f=>f.formType==='embedded'),"
                          "decodeTargetingRulesForForm((readPublishedFormRegistryFromSdk()||[]).find(f=>f.formType==='embedded'))).tone")
    assert after != "block"


def test_url_rule_is_evaluated_consistently_and_simulation_satisfies_it(page):
    """Regression: the Targeting Matrix hardcoded 'info' for URL rules while the per-page panel
    evaluated them, so the two panels disagreed about the same rule."""
    inject(page)
    rule = page.evaluate("""() => {
        const f = (readPublishedFormRegistryFromSdk()||[]).find(x => x.formType === 'button');
        return decodeTargetingRulesForForm(f).find(r => r.name === 'UrlInclude');
    }""")
    assert rule["verdict"] == "block", "'/' does not match '/Page1' and that is knowable"

    page.evaluate("() => { document.getElementById('simulated-url-input').value = '/Page1'; applySimulatedUrlPath(); }")
    page.wait_for_timeout(1500)
    assert page.evaluate("location.pathname") == "/Page1"

    after = page.evaluate("""() => {
        const f = (readPublishedFormRegistryFromSdk()||[]).find(x => x.formType === 'button');
        return decodeTargetingRulesForForm(f).find(r => r.name === 'UrlInclude').verdict;
    }""")
    assert after == "pass"


def test_code_triggered_form_can_be_shown_on_demand(page):
    """A code-triggered form has no automatic targeting, so the only way to test it is to fire it."""
    inject(page)
    page.evaluate("() => { document.getElementById('manual-form-id-input').value = '19105'; invokeManualSDKCommand('load'); }")
    page.wait_for_timeout(4000)
    page.evaluate("() => invokeManualSDKCommand('show')")
    page.wait_for_timeout(5000)
    shown = page.evaluate("() => (readPublishedFormRegistryFromSdk()||[])"
                          ".filter(f => f.state && f.state.shown).map(f => f.formId)")
    assert "19105" in shown


# --------------------------------------------------------------------------------------------
# Signal quality: findings must be real.
# --------------------------------------------------------------------------------------------

def test_timestamps_are_not_reported_as_credit_cards(page):
    """Regression: Luhn alone passes ~1 in 10 random digit strings, so 13-digit epoch
    millisecond sessionIds were being reported as 'possible credit card' on every event."""
    inject(page)
    findings = page.evaluate("() => (buildPiiScanReport()||[]).filter(f => f.type === 'Possible credit card')")
    assert findings == []

    checks = page.evaluate("""() => ({
        real_visa: luhnCheckDigits('4111111111111111') && matchesKnownCardIssuerPrefix('4111111111111111'),
        real_amex: luhnCheckDigits('340000000000009') && matchesKnownCardIssuerPrefix('340000000000009'),
        epoch_ms:  matchesKnownCardIssuerPrefix('1785925959160'),
    })""")
    assert checks["real_visa"] and checks["real_amex"], "must still catch real card numbers"
    assert not checks["epoch_ms"]


def test_hidden_feedback_button_is_not_reported_as_an_accessibility_failure(page):
    """Regression: the SDK hides the button while an invitation is open, and innerText returns ''
    for hidden elements, which produced two phantom blockers."""
    inject(page)
    result = page.evaluate("""() => {
        const b = locateFeedbackButtonElement();
        if (!b) return null;
        b.style.setProperty('display', 'none', 'important');
        const checks = buildAccessibilityAuditReport();
        const by = {}; checks.forEach(c => by[c.id] = c);
        return { name: by['btn-name'].verdict, reach: by['btn-reachable'].verdict };
    }""")
    if result:
        assert result["reach"] == "info", "hidden is 'cannot evaluate', never a failure"
        assert result["name"] in ("pass", "info")


def test_consent_violations_require_opting_into_consent_testing(page):
    """Regression: consent defaulted to 'denied', so a tester who never opened the consent page
    got every SDK request logged as a violation."""
    inject(page)
    assert page.evaluate("() => consentGateViolations.length") == 0


def test_performance_window_is_bounded_so_an_idle_tab_cannot_inflate_its_own_score(page):
    """Regression: blocking time was summed over every long task since injection, forever. An idle
    tab kept climbing — partly on long tasks caused by this dashboard's own render loops — so the
    same injection drifted towards 'Poor' the longer you looked at it."""
    inject(page)

    def push_task_at(offset_ms):
        page.evaluate(f"""() => capturedLongTasks.push({{
            duration: 300, at: Date.now(),
            startTime: perfCaptureBaseline.at + {offset_ms}, attribution: []
        }})""")

    push_task_at(2000)      # inside the 10s window
    inside = page.evaluate("() => buildPerformanceReport().totalBlockingTimeMs")

    push_task_at(30000)     # long after the window closed
    push_task_at(60000)
    after = page.evaluate("() => buildPerformanceReport().totalBlockingTimeMs")

    assert after == inside, "tasks outside the measurement window must not change the score"
    assert page.evaluate("() => buildPerformanceReport().windowSeconds") == 10


def test_repeated_measurement_does_not_spawn_new_evidence_entries(page):
    """Regression: the finding title carried the live number and the evidence key was derived from
    the title, so every re-measurement looked like a brand-new issue and captured another
    screenshot — evidence grew without bound while the page sat idle."""
    inject(page)
    page.evaluate("() => { seenFindingKeysForEvidence = new Set();"
                  " capturedIssueEvidence.clear(); capturedEvidenceFrames.clear(); }")

    counts = []
    for offset in (1000, 2000, 3000, 4000):
        page.evaluate(f"""() => capturedLongTasks.push({{
            duration: 400, at: Date.now(),
            startTime: perfCaptureBaseline.at + {offset}, attribution: []
        }})""")
        page.evaluate("() => captureEvidenceForNewlyAppearedFindings()")
        counts.append(page.evaluate(
            "() => [...capturedIssueEvidence.values()].filter(e => e.area === 'Performance').length"))

    assert max(counts) <= 1, f"performance evidence must not accumulate per measurement: {counts}"


# --------------------------------------------------------------------------------------------
# The tool must meet the standards it enforces.
# --------------------------------------------------------------------------------------------

def test_every_control_has_an_accessible_name(page):
    page.evaluate("setActiveWorkspace('all'); ensureAccessibleNamesOnControls();")
    page.wait_for_timeout(500)
    unnamed = page.evaluate("""() => [...document.querySelectorAll('input,select,textarea')].filter(i => {
        const l = i.id ? document.querySelector(`label[for="${CSS.escape(i.id)}"]`) : null;
        return !l && !i.getAttribute('aria-label') && !i.closest('label');
    }).map(i => i.id || i.className)""")
    assert unnamed == []


def test_page_has_a_main_landmark_and_a_lang(page):
    assert page.evaluate("document.querySelectorAll('main').length") == 1
    assert page.evaluate("document.documentElement.lang") == "en"


def test_dashboard_stays_clickable_under_a_full_page_sdk_overlay(page):
    """Regression: the SDK injects #kampyle_abandon_zone as a fixed, full-page, 33.5-million-px
    overlay at z-index 5 which swallowed clicks on this app's own controls."""
    inject(page)
    page.evaluate("setActiveWorkspace('targeting')")
    page.wait_for_timeout(1200)
    blocked = page.evaluate("""() => {
        const btn = document.querySelector('.form-verdict-fix');
        if (!btn) return 'no-button';
        btn.scrollIntoView({ block: 'center' });
        const r = btn.getBoundingClientRect();
        const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
        return top && btn.contains(top) === false && !btn.isSameNode(top) ? (top.id || top.className) : null;
    }""")
    assert blocked in (None, "no-button"), f"a control is covered by: {blocked}"


def test_no_workspace_panel_is_orphaned_by_a_heading_rename(page):
    """Panels are assigned to workspaces by matching heading text, so renaming a heading without
    updating WORKSPACE_BY_PANEL_HEADING silently drops the panel into 'always'."""
    counts = page.evaluate("""() => {
        const o = {};
        ['targeting','appearance','activity','config'].forEach(w => {
            o[w] = document.querySelectorAll(`#master-canvas-wrapper > [data-workspace="${w}"]`).length;
        });
        return o;
    }""")
    assert sum(counts.values()) == 22, f"expected 22 assigned panels, got {counts}"


def test_no_uncaught_errors_during_a_full_session(page):
    inject(page)
    for workspace in ["targeting", "appearance", "activity", "config"]:
        page.evaluate(f"setActiveWorkspace('{workspace}')")
        page.wait_for_timeout(800)
    assert page.errors == []
