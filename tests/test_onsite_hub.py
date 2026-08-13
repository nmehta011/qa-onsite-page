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


@pytest.fixture(scope="session")
def browser(server):
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    # A fresh context per test rather than a fresh browser. A context already isolates
    # localStorage, sessionStorage and cookies — which is the entire reason the old fixture
    # cleared storage and reloaded — but costs milliseconds where a browser launch costs about a
    # second. Starting clean also means the app cannot auto-inject a saved script from a previous
    # test, so the reload that used to guard against that is gone too.
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = context.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server, wait_until="domcontentloaded")
    # Proves DOMContentLoaded init actually ran, not merely that the document parsed.
    pg.wait_for_function(
        "() => document.querySelectorAll('#workspace-bar .workspace-tab').length > 0",
        timeout=15000)
    pg.errors = errors
    yield pg
    context.close()


def inject(pg, timeout=35000):
    """Inject the real embed and wait until the app has finished reacting to it.

    This replaces a flat 13s sleep that was standing in for three separate things at once: the
    SDK publishing its form registry, the targeting matrix rendering from that registry, and the
    periodic audit panels completing a first pass. Waiting on those conditions directly is both
    faster (they are typically ready in 2-4s) and stricter — a fixed sleep that is slightly too
    short fails intermittently on a loaded machine, and one that is comfortably too long hides a
    real regression in how quickly the app responds.
    """
    pg.fill("#input-main-page", EMBED)
    pg.click("#btn-main-page")
    pg.wait_for_function("() => !!window.KAMPYLE_ONSITE_SDK", timeout=timeout)
    pg.wait_for_function(
        """() => (readPublishedFormRegistryFromSdk() || []).length >= 4
                 && document.querySelectorAll('#targeting-matrix-container .targeting-form-card').length >= 4
                 && document.querySelectorAll('#a11y-audit-container .a11y-check-row').length > 0""",
        timeout=timeout)


# --------------------------------------------------------------------------------------------
# Trust: the app must never claim success it hasn't verified.
# --------------------------------------------------------------------------------------------

def test_failed_injection_reports_failure_not_success(page):
    """Regression: appending a <script> that 404s used to render a green
    'deployed successfully' message while the status bar said 'No property injected'."""
    page.fill("#input-main-page",
              '<script src="https://resources.digital-cloud-qa-web.medallia.com/websites/999999/onsite/embed.js"></script>')
    page.click("#btn-main-page")

    page.wait_for_function(
        "() => document.getElementById('msg-main-page').innerText.includes('waiting for the SDK')",
        timeout=10000)

    # The app gives up after its own SDK_INIT_TIMEOUT_MS (25s), so this cannot be made quick —
    # but waiting for the verdict beats sleeping past it and hoping the timing still lines up.
    page.wait_for_function(
        "() => document.getElementById('msg-main-page').className.includes('error')",
        timeout=40000)
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
    """Asserts every trigger type is decoded, not that the property holds exactly four forms —
    it gained a fifth (68478, code-triggered) mid-project and the stricter equality check failed
    on a config change that was nobody's bug. A missing type still fails, which is the point."""
    inject(page)
    types = page.evaluate(
        "() => (readPublishedFormRegistryFromSdk()||[]).map(f => f.formType)")
    assert set(types) >= {"button", "code", "embedded", "invitation"}, \
        f"a trigger type stopped being decoded, got {sorted(set(types))}"


def test_count_rule_honours_its_comparison_operator(page):
    """Regression: the rule ships as {numberOfRepeats: 2, compareString: 'greaterThan'} and the
    app read the 2 alone, comparing with >=. So at exactly 2 visits it called the form eligible,
    offered "Set visits to 2" as the one-click fix, and then reported nothing blocked while the
    form stayed hidden — the tool disagreeing with the SDK about the thing it exists to predict.

    Confirmed against the live property by seeding the counter and watching whether the embedded
    form actually renders: 1 -> hidden, 2 -> hidden, 3 -> rendered."""
    inject(page)

    def verdict_at(visits):
        return page.evaluate(f"""() => {{
            localStorage.setItem('kampyleUserSessionsCount', '{visits}');
            const f = (readPublishedFormRegistryFromSdk()||[]).find(x => x.formType === 'embedded');
            const r = decodeTargetingRulesForForm(f).find(x => x.name === 'NumberOfVisits');
            return {{verdict: r.verdict, configured: r.configured, fix: r.fix}};
        }}""")

    at_two = verdict_at(2)
    assert at_two["verdict"] == "block", "2 does not satisfy 'greater than 2' — this was the bug"
    assert "more than 2" in at_two["configured"], \
        f"the operator must be visible in the rule, got {at_two['configured']!r}"

    # The offered fix has to actually satisfy the rule, or the app walks the tester into the
    # same false 'nothing blocked' it used to report on its own.
    assert at_two["fix"]["value"] == "3", f"fix must clear the threshold, got {at_two['fix']['value']!r}"

    assert verdict_at(3)["verdict"] == "pass"
    assert verdict_at(1)["verdict"] == "block"


def test_count_rule_operators_match_the_sdk_comparison_table(page):
    """The operator table mirrors kampyleCompareByOperator in the property's own generic*.js:
    lowercased before matching (config ships camelCase), and anything unrecognised falls through
    its switch to false rather than being guessed at."""
    inject(page)
    results = page.evaluate("""() => {
        const call = (actual, want, op) => {
            const r = evaluateCountRuleAgainstOperator(actual, want, op);
            return r === null ? 'unreadable' : (r.satisfied ? 'pass' : 'block') + ':' + r.targetValue;
        };
        return {
            gt_above:   call(3, 2, 'greaterThan'),
            gt_equal:   call(2, 2, 'greaterThan'),
            gt_below:   call(1, 2, 'greaterThan'),
            lt_below:   call(1, 2, 'smallerThan'),
            lt_equal:   call(2, 2, 'smallerThan'),
            eq_match:   call(2, 2, 'equals'),
            eq_miss:    call(3, 2, 'equals'),
            ne_differs: call(3, 2, 'doesNotEqual'),
            ne_same:    call(2, 2, 'doesNotEqual'),
            uppercased: call(3, 2, 'GREATERTHAN'),
            unknown:    call(3, 2, 'bogusOperator'),
            unreachable: call(1, 0, 'smallerThan'),
        };
    }""")
    assert results == {
        "gt_above": "pass:3", "gt_equal": "block:3", "gt_below": "block:3",
        "lt_below": "pass:1", "lt_equal": "block:1",
        "eq_match": "pass:2", "eq_miss": "block:2",
        "ne_differs": "pass:3", "ne_same": "block:3",
        "uppercased": "pass:3",
        "unknown": "unreadable",
        # A count cannot go below zero, so "fewer than 0" can never be satisfied and must not
        # offer a fix that writes a negative value.
        "unreachable": "block:null",
    }, results


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


EMBEDDED_TONE = """() => {
    const f = (readPublishedFormRegistryFromSdk()||[]).find(x => x.formType === 'embedded');
    return buildFormShowVerdict(f, decodeTargetingRulesForForm(f)).tone;
}"""

EMBEDDED_SHOWN = """() => {
    const f = (readPublishedFormRegistryFromSdk()||[]).find(x => x.formType === 'embedded');
    return !!(f && f.state && f.state.shown);
}"""


def test_one_click_fix_actually_makes_the_form_appear(page):
    """Regression: the fix wrote the value and the matrix flipped to 'nothing blocking', but the
    SDK only re-reads storage on updatePageView() and nothing called it — so the form stayed
    absent while the app reported its own remedy as a success. The URL fix already called it;
    the storage fix never had.

    The previous version of this test is why that survived: it asserted only that the app's own
    verdict stopped saying 'block'. The app agreeing with itself proves nothing about the page,
    so this now waits on the SDK reporting the form shown."""
    inject(page)
    page.evaluate("setActiveWorkspace('targeting')")

    assert page.evaluate(EMBEDDED_TONE) == "block"
    assert page.evaluate(EMBEDDED_SHOWN) is False, "precondition: the form must start hidden"

    # Clicks whatever the matrix offers rather than the visits fix by name: the property gained a
    # rule-engine condition on this form mid-project, so naming one rule made the test depend on
    # a config that changes. Every offered fix is applied until none remain.
    applied = []
    for _ in range(6):
        clicked = page.evaluate("""() => {
            const b = document.querySelector('.form-verdict-fix');
            if (!b) return null;
            const label = b.innerText.trim();
            b.click();
            return label;
        }""")
        if not clicked:
            break
        applied.append(clicked)
        page.wait_for_timeout(2500)
    assert applied, "the matrix offered no fix at all for a blocked form"

    # Ground truth: the SDK has to report the form shown. Asserting the hub's verdict alone is
    # what let the broken version through.
    page.wait_for_function(EMBEDDED_SHOWN, timeout=25000)
    assert page.evaluate(EMBEDDED_TONE) != "block"


def test_rule_engine_criteria_offers_a_working_one_click_fix(page):
    """The embedded form carries a rule-builder condition — custom param "test" contains "test".
    The matrix used to dump it as raw JSON with no fix and call it something "only real behaviour
    can satisfy", which was wrong twice over: it is readable, and it is satisfiable from here.

    The SDK resolves a parameter through fetchCPValue from one of `var`, `url` or `cookie`, and
    the configuration naming which is not exposed on any SDK global, so the fix writes the window
    variable and the cookie. Confirmed against this rule: window.test is what it reads."""
    inject(page)
    # Clear the visit rule so the criteria condition is the remaining dependency.
    page.evaluate("() => commitLifecycleStorageWrite('kampyleUserSessionsCount', '9', 'test setup')")

    page.wait_for_function("""() => [...document.querySelectorAll('.form-verdict-fix')]
        .some(b => /custom param/i.test(b.innerText))""", timeout=20000)

    rule = page.evaluate("""() => {
        const f = (readPublishedFormRegistryFromSdk()||[]).find(x => String(x.formId) === '67251');
        return decodeTargetingRulesForForm(f).find(r => r.name === 'GenericRule');
    }""")
    assert rule["configured"] == 'custom param "test" contains "test"', \
        f"the rule must read as English, not raw JSON — got {rule['configured']!r}"
    assert rule["fix"]["customParams"] == [{"name": "test", "value": "test"}]

    clicked = page.evaluate("""() => {
        const b = [...document.querySelectorAll('.form-verdict-fix')].find(x => /custom param/i.test(x.innerText));
        if (!b) return null;
        const label = b.innerText.trim();
        b.click();
        return label;
    }""")
    assert clicked == "Set custom param test = test", clicked

    page.wait_for_function(EMBEDDED_SHOWN, timeout=25000)
    assert page.evaluate("() => String(window.test)") == "test"


def test_criteria_rules_are_described_and_only_fixed_when_derivable(page):
    """Nested groups, OR, and negation are all satisfiable by setting every named parameter — that
    holds for OR as well as AND, so the conjunction needs no special case. A regex has no single
    derivable value and two conditions contradicting each other on one parameter cannot both hold;
    both offer no fix rather than one that quietly fails."""
    inject(page)
    shapes = page.evaluate("""() => {
        const cp = (fieldName, condition, value) =>
            ({type:'criteria', fieldOrigin:'customParam', fieldName, condition, value});
        const group = (conjunction, ...kids) =>
            ({type:'criteriaGroup', conjunction, childrenCriterias:kids});
        const shape = r => ({desc: describeCriteriaRule(r), fix: buildCriteriaRuleFix(r)});
        return {
            nested: shape(group('AND', cp('a','equals','1'),
                                group('OR', cp('b','equals','2'), cp('c','endsWith','3')))),
            negation: shape(group('AND', cp('plan','doesNotEqual','free'))),
            hasvalue: shape(group('AND', cp('email','hasValue',''))),
            regex: shape(group('AND', cp('sku','regex','^A.*Z$'))),
            contradictory: shape(group('AND', cp('x','equals','1'), cp('x','equals','2'))),
        };
    }""")

    assert shapes["nested"]["desc"] == \
        '(custom param "a" equals "1" AND (custom param "b" equals "2" OR custom param "c" ends with "3"))'
    assert shapes["nested"]["fix"]["customParams"] == [
        {"name": "a", "value": "1"}, {"name": "b", "value": "2"}, {"name": "c", "value": "3"}]

    # A negation is satisfied by any other value, not by the configured one.
    assert shapes["negation"]["fix"]["customParams"] == [{"name": "plan", "value": "free-qa"}]
    assert shapes["hasvalue"]["fix"]["customParams"] == [{"name": "email", "value": "qa-value"}]

    assert shapes["regex"]["fix"] is None, "a regex pattern has no single satisfying value"
    assert shapes["contradictory"]["fix"] is None, "contradictory conditions must not be half-applied"


SUBMITTED_QUARANTINE = """() => {
    const f = (readPublishedFormRegistryFromSdk()||[]).find(x => String(x.formId) === '67209');
    const r = decodeTargetingRulesForForm(f).find(x => x.name === 'DontInviteOnSubmitted');
    return r ? {verdict: r.verdict, fix: r.fix} : null;
}"""


def test_submitted_date_quarantine_offers_a_working_one_click_fix(page):
    """The invitation is suppressed for 2 days after a submission. The matrix showed that but
    offered nothing, so a tester had to go find SUBMITTED_DATE in the Lifecycle panel by hand —
    the one rule most likely to be blocking them right after they tested a submission."""
    inject(page)
    page.evaluate("() => localStorage.setItem('SUBMITTED_DATE', String(Date.now()))")
    page.evaluate("renderFormTargetingRulesMatrix()")

    blocked = page.evaluate(SUBMITTED_QUARANTINE)
    assert blocked and blocked["verdict"] == "block", "setup: a fresh submit must quarantine it"
    assert blocked["fix"], "a blocking quarantine rule must offer a fix"

    # Click the button a tester clicks, not the function behind it.
    clicked = page.evaluate("""() => {
        const b = [...document.querySelectorAll('.form-verdict-fix')].find(x => /back-date/i.test(x.innerText));
        if (!b) return null;
        const label = b.innerText.trim();
        b.click();
        return label;
    }""")
    assert clicked, "the fix must render as a clickable button in the matrix"

    page.wait_for_function("""() => {
        const f = (readPublishedFormRegistryFromSdk()||[]).find(x => String(x.formId) === '67209');
        const r = decodeTargetingRulesForForm(f).find(x => x.name === 'DontInviteOnSubmitted');
        return r && r.verdict === 'pass';
    }""", timeout=15000)

    # Back-dated past the window rather than cleared: clearing would prove "this never happened",
    # which is a different rule from "the window expires".
    age_days = page.evaluate(
        "() => (Date.now() - Number(localStorage.getItem('SUBMITTED_DATE'))) / 86400000")
    assert age_days > 2, f"must land outside the 2-day window, got {age_days:.2f} days"


def test_quarantine_and_session_cap_fixes_are_offered_for_rules_this_property_does_not_configure(page):
    """DECLINED_DATE and the once-per-session cap are decoded from config this property doesn't
    set (no form declares `declined`, and inviteOncePerSession is false), so there is no live form
    to watch flip. Driven through the decoder with synthetic config instead — honest unit coverage
    rather than a ground-truth claim the property can't support."""
    inject(page)
    result = page.evaluate("""() => {
        const apply = (form, ruleName) => {
            const before = decodeTargetingRulesForForm(form).find(r => r.name === ruleName);
            if (!before || !before.fix) return {verdict: before && before.verdict, fix: null};
            localStorage.setItem(before.fix.key, before.fix.value);
            const after = decodeTargetingRulesForForm(form).find(r => r.name === ruleName);
            return {verdict: before.verdict, fixLabel: before.fix.label, after: after.verdict};
        };
        localStorage.setItem('DECLINED_DATE', String(Date.now()));
        localStorage.setItem('kampyleInvitePresented', 'true');
        return {
            declined: apply({onSiteData: {declined: {days: '3'}}}, 'DontInviteOnDeclined'),
            perSession: apply({onSiteData: {kampyleInvitePerSession: {inviteOncePerSession: 'true'}}},
                              'InvitePerSession'),
        };
    }""")
    assert result["declined"]["verdict"] == "block" and result["declined"]["after"] == "pass", result
    assert result["perSession"]["verdict"] == "block" and result["perSession"]["after"] == "pass", result


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


def test_open_form_modal_is_not_reported_as_a_keyboard_failure(page):
    """Regression: while a lightbox form is open the SDK sets tabindex="-1" and aria-hidden="true"
    on the feedback button while leaving it visible behind the overlay. That is correct modal
    behaviour — background controls must not be tabbable behind a dialog — but the audit called it
    a blocker, and it reached the status bar, collectAllCurrentFindings() and the filed bug report.
    So the tool told testers to file an accessibility bug against the SDK for doing the right
    thing, on the most ordinary path through the app: open a form, then read the audit."""
    inject(page)

    # The feedback button only renders on /Page1, and it is the element under test.
    page.evaluate("() => { document.getElementById('simulated-url-input').value = '/Page1'; applySimulatedUrlPath(); }")
    page.wait_for_function("() => !!locateFeedbackButtonElement()", timeout=25000)

    page.evaluate("() => window.KAMPYLE_ONSITE_SDK.showForm(19105)")

    # Wait for the precise state the regression was about — out of the tab order *and* still on
    # screen — and require it to hold for a full second before trusting it. Measured: while the
    # lightbox opens the SDK flips the button between visibility:hidden and visible for roughly
    # 1.5s, so a single-shot check catches a transient hidden frame, and a hidden button already
    # returned 'info' before this fix. Asserting on that frame would pass without ever exercising
    # the bug. If this state never settles the SDK's behaviour has changed and this test should
    # fail loudly rather than quietly prove nothing.
    # Polls isFormModalCurrentlyOpen() rather than the SDK's isSurveyDisplayed() directly: that
    # method emits a nebula_code_survey_displayed analytics event on every call that returns false,
    # so a polling wait on it would pump hundreds of junk events into the property mid-test — and
    # it would no longer be exercising the code path the app actually uses.
    page.wait_for_function("""() => {
        const b = locateFeedbackButtonElement();
        const ok = !!b && b.tabIndex === -1 && isElementCurrentlyVisible(b)
                   && isFormModalCurrentlyOpen();
        if (!ok) { window.__modalStableSince = null; return false; }
        window.__modalStableSince = window.__modalStableSince || Date.now();
        return Date.now() - window.__modalStableSince >= 1000;
    }""", timeout=30000)

    reach = page.evaluate("""() => {
        const by = {}; buildAccessibilityAuditReport().forEach(c => by[c.id] = c);
        return by['btn-reachable'].verdict;
    }""")
    assert reach == "info", "correct modal behaviour must not be reported as a failure"


def test_modal_detection_does_not_emit_analytics_events(page):
    """Regression: isFormModalCurrentlyOpen() used to call the SDK's isSurveyDisplayed(), which is
    not a passive getter — when nothing is showing it fires neb_sdkSurveyDisplayed, recorded by the
    property as `nebula_code_survey_displayed`. Because this app polls modal state on a timer, that
    pushed ~120 fabricated events per minute into the customer's real analytics and buried this
    app's own event stream (it hit its 1000-event cap in about a minute).

    Measured 1:1 at the time — 10 calls to isSurveyDisplayed() produced exactly 10 events, while 30
    reads of the form registry produced none. A QA tool must not contaminate the data it exists to
    observe, so modal state now comes from the registry."""
    inject(page)

    def code_survey_count():
        return page.evaluate("""() => {
            const src = (typeof collectedAnalyticsEventsCacheList !== 'undefined')
                ? collectedAnalyticsEventsCacheList : [];
            return src.filter(e => (e.eventName || (e.payload && e.payload.eventName))
                                   === 'nebula_code_survey_displayed').length;
        }""")

    before = code_survey_count()
    page.evaluate("() => { for (let i = 0; i < 40; i++) isFormModalCurrentlyOpen(); }")
    page.wait_for_timeout(2500)
    assert code_survey_count() == before, \
        "polling modal state must not emit analytics events into the property"

    # The app's own timers must stay quiet too, not just direct calls.
    idle_start = code_survey_count()
    page.wait_for_timeout(6000)
    drift = code_survey_count() - idle_start
    assert drift == 0, f"app emitted {drift} nebula_code_survey_displayed event(s) while idle"

    leaked = [f for f in page.evaluate("() => collectAllCurrentFindings()")
              if f["severity"] == "block" and "keyboard-reachable" in f["title"]]
    assert not leaked, "the false blocker must not reach the list the bug report is built from"


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
