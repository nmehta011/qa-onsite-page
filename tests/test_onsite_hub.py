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


def clear_the_screen(pg, timeout_ms=25000, settle_ms=14000):
    """Get any invitation or form off screen so a test can show one of its own.

    The SDK only ever displays one thing at a time, and an invitation has to be *declined* rather
    than closed: closeForm() leaves inviteShown true and the invitation still up, so the SDK will
    happily create the next form's iframe and then leave it 0x0 and never mark it shown — which
    reads as a broken showForm but is really an occupied screen.

    Waits for something to appear before deciding the screen is clear. An auto-showing invitation
    lands several seconds after the form registry is ready, so checking once returns "clear",
    the invitation then arrives, and it blocks whatever the test was about to show — which is
    exactly the false failure this helper exists to prevent.
    """
    try:
        pg.wait_for_function("() => isFormModalCurrentlyOpen()", timeout=settle_ms)
    except Exception:
        return True   # nothing auto-shows on this property, so there is nothing to dismiss

    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if not pg.evaluate("() => isFormModalCurrentlyOpen()"):
            return True
        for frame in pg.frames:
            if "medallia" in (frame.url or ""):
                try:
                    frame.evaluate("""() => {
                        const b = [...document.querySelectorAll('button,a,[role=button]')]
                            .filter(n => n.offsetParent)
                            .find(n => /not right now|no thanks|decline|dismiss/i.test(n.innerText || ''));
                        if (b) b.click();
                    }""")
                except Exception:
                    pass   # frame can navigate or detach mid-iteration
        pg.evaluate("""() => {
            try {
                (readPublishedFormRegistryFromSdk()||[]).forEach(f => {
                    if (f.state && f.state.shown) {
                        KAMPYLE_ONSITE_SDK.closeForm && KAMPYLE_ONSITE_SDK.closeForm(Number(f.formId));
                    }
                });
            } catch (e) {}
        }""")
        pg.wait_for_timeout(1500)
    return not pg.evaluate("() => isFormModalCurrentlyOpen()")


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


def skip_if_this_domain_is_not_allow_listed(pg):
    """Skip a test that needs a form to actually render, when the property forbids this domain.

    A Medallia property carries a domain allow-list (`domainsConfiguration`). When
    `allDomainsAllowed` is false and the page's host is not on the list, the SDK loads and
    reports its whole configuration normally but never renders a form — so a test that waits for
    one times out after 25s with nothing explaining why. That is a property setting, not a
    regression, and it is outside this suite's control: the list is edited in Medallia, and the
    host here is localhost.

    Read from the property's own configuration rather than hardcoded, so this stops skipping by
    itself the moment the domain is allow-listed.
    """
    config = pg.evaluate("""async () => {
        try {
            const res = await fetch(window.KAMPYLE_EMBED.getOnsiteDataLocation());
            return (await res.json()).domainsConfiguration || null;
        } catch (err) { return null; }
    }""")
    if not config or config.get("allDomainsAllowed"):
        return
    host = pg.evaluate("() => window.location.hostname")
    allowed = config.get("domainsNames") or []
    matches = any(host == d or (d.startswith("*.") and host.endswith(d[1:])) for d in allowed)
    if not matches:
        pytest.skip(f"property 165099 allow-lists {allowed} and this page is on {host}, "
                    f"so the SDK will never render a form here")


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
    skip_if_this_domain_is_not_allow_listed(page)
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
    skip_if_this_domain_is_not_allow_listed(page)
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


# Finds whichever form actually carries the rule rather than naming a form id. This property is a
# shared QA environment whose forms get republished — the invitation was 67209 and is now 30201,
# and hardcoding the old id turned a config change into a crash (decodeTargetingRulesForForm was
# handed undefined). Tests should track the behaviour, not the fixture.
SUBMITTED_QUARANTINE = """() => {
    const forms = readPublishedFormRegistryFromSdk() || [];
    for (const f of forms) {
        const r = decodeTargetingRulesForForm(f).find(x => x.name === 'DontInviteOnSubmitted');
        if (r) return {formId: f.formId, verdict: r.verdict, fix: r.fix};
    }
    return null;
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
        const forms = readPublishedFormRegistryFromSdk() || [];
        for (const f of forms) {
            const r = decodeTargetingRulesForForm(f).find(x => x.name === 'DontInviteOnSubmitted');
            if (r) return r.verdict === 'pass';
        }
        return false;
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
    evaluated them, so the two panels disagreed about the same rule.

    Finds whichever form carries a URL rule instead of assuming the button form does — that rule
    was removed from the button form mid-project. Skips rather than fails when the property
    publishes no URL rule at all: there is then nothing to assert about, and a red suite would be
    blaming this app for someone else's config change."""
    inject(page)
    target = page.evaluate("""() => {
        const forms = readPublishedFormRegistryFromSdk() || [];
        for (const f of forms) {
            const r = decodeTargetingRulesForForm(f).find(x => x.name === 'UrlInclude');
            if (r) return {formId: f.formId, pattern: r.configured, verdict: r.verdict};
        }
        return null;
    }""")
    if not target:
        pytest.skip("no form on this property currently publishes a UrlInclude rule")

    assert target["verdict"] == "block", \
        f"'/' does not match {target['pattern']!r} and that is knowable"

    page.evaluate("""(pattern) => {
        document.getElementById('simulated-url-input').value = pattern;
        applySimulatedUrlPath();
    }""", target["pattern"])
    page.wait_for_timeout(1500)

    after = page.evaluate("""() => {
        const forms = readPublishedFormRegistryFromSdk() || [];
        for (const f of forms) {
            const r = decodeTargetingRulesForForm(f).find(x => x.name === 'UrlInclude');
            if (r) return r.verdict;
        }
        return null;
    }""")
    assert after == "pass"


def test_code_triggered_form_can_be_shown_on_demand(page):
    """A code-triggered form has no automatic targeting, so the only way to test it is to fire it.

    Picks whichever code form the property publishes rather than naming one, and closes anything
    already on screen first. The property gained an auto-showing invitation mid-project, and the
    SDK will not mark a second form as shown while one is up (isAnyOtherFormAlreadyShown) — so the
    code form's iframe was being created while state.shown stayed false, which looked like a
    broken showForm but was really just an occupied screen."""
    inject(page)

    code_form = page.evaluate("""() => {
        const f = (readPublishedFormRegistryFromSdk()||[]).find(x => x.formType === 'code');
        return f ? f.formId : null;
    }""")
    if not code_form:
        pytest.skip("this property publishes no code-triggered form")

    if not clear_the_screen(page):
        pytest.skip("an invitation is holding the only visible-form slot and would not dismiss")

    page.evaluate("""(formId) => {
        document.getElementById('manual-form-id-input').value = formId;
        invokeManualSDKCommand('load');
    }""", code_form)
    page.wait_for_timeout(4000)
    page.evaluate("() => invokeManualSDKCommand('show')")

    page.wait_for_function("""(formId) => {
        const f = (readPublishedFormRegistryFromSdk()||[]).find(x => String(x.formId) === String(formId));
        return !!(f && f.state && f.state.shown);
    }""", arg=code_form, timeout=25000)


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
    overlay at z-index 5 which swallowed clicks on this app's own controls.

    The bug being guarded is an *invisible* overlay eating clicks. A visible invitation covering
    the page is the opposite — that is what a modal is supposed to do — so any auto-shown
    invitation is dismissed first. Without that this fails on kampyleInviteContainer and reports
    correct modal behaviour as the regression."""
    inject(page)
    clear_the_screen(page)
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
    updating WORKSPACE_BY_PANEL_HEADING silently drops the panel into 'always'.

    The workspace list is read from WORKSPACE_DEFINITIONS rather than written out here: this
    test used to carry its own copy, and that copy was still four workspaces long after Inspect
    shipped -- so the one panel in it was outside what the test looked at."""
    counts = page.evaluate("""() => {
        const o = {};
        WORKSPACE_DEFINITIONS.filter(w => w.key !== 'all').forEach(w => {
            o[w.key] = document.querySelectorAll(`#master-canvas-wrapper > [data-workspace="${w.key}"]`).length;
        });
        return o;
    }""")
    assert sum(counts.values()) == 25, f"expected 25 assigned panels, got {counts}"
    assert all(counts.values()), f"a workspace ended up with no panels at all: {counts}"


def test_no_uncaught_errors_during_a_full_session(page):
    inject(page)
    for workspace in ["targeting", "appearance", "traffic", "health", "config"]:
        page.evaluate(f"setActiveWorkspace('{workspace}')")
        page.wait_for_timeout(800)
    assert page.errors == []


# --------------------------------------------------------------------------------------------
# The property's own custom-parameter registry.
#
# Before this, the only way to set a custom parameter was to type its name by hand. That is
# quietly error-prone: the name shown in the Medallia admin UI (unique_name) is not necessarily
# the key the SDK reads (source_name), the source is per-parameter, and each type has a format
# the SDK will reject silently. Every test here pins one of those.
# --------------------------------------------------------------------------------------------

def open_parameter_registry(pg, timeout=35000):
    """Inject, switch to Setup and wait for the property's parameters to be listed.

    clear_the_screen() is not optional here. This property auto-shows an invitation, and the
    SDK's invitation iframe takes focus when it appears — so anything typed into the panel goes
    into the iframe instead of the field, and the value silently never lands. That is SDK
    behaviour, not a hub defect (the hub already warns about it: "A form is open, so it owns the
    keyboard"), but it makes every test that types into this panel fail depending on when the
    invitation happens to arrive.
    """
    inject(pg, timeout=timeout)
    clear_the_screen(pg)
    pg.evaluate("setActiveWorkspace('config')")
    pg.wait_for_function(
        "() => document.querySelectorAll('.property-param-row').length > 0", timeout=timeout)


def registry_index(pg, unique_name):
    return pg.evaluate(
        "(n) => propertyCustomParamRegistry.findIndex(p => p.uniqueName === n)", unique_name)


def set_parameter(pg, unique_name, typed_value):
    """Fill a row and press its Set button.

    Clicked through JS rather than pg.click(): this property auto-shows an invitation whose
    iframe can sit over the panel, and Playwright then refuses the click as intercepted. That is
    an SDK overlay, not a hub defect — it has its own test — and it would make every test here
    intermittently fail for an unrelated reason.
    """
    i = registry_index(pg, unique_name)
    assert i >= 0, f"{unique_name} is not in this property's parameter registry"
    field = f"#property-param-input-{i}"
    if pg.eval_on_selector(field, "e => e.tagName") == "SELECT":
        pg.select_option(field, typed_value)   # a Boolean parameter offers true/false, not free text
    else:
        pg.fill(field, typed_value)
        landed = pg.eval_on_selector(field, "e => e.value")
        assert landed == typed_value, (
            f"typing into {unique_name} did not land (field holds {landed!r}); "
            f"focus is on {pg.evaluate('() => document.activeElement && document.activeElement.id')!r}")
    pg.evaluate(f"() => document.querySelector(\"button[data-param-index='{i}']\").click()")
    pg.wait_for_timeout(400)
    return i


def sdk_reads(pg, unique_name):
    """What the SDK itself now resolves for a parameter — the only oracle that matters."""
    return pg.evaluate("""(n) => {
        const p = window.CUSTOM_PARAMETERS.getAllCustomParams().find(x => x.unique_name === n);
        if (!p) return '__missing__';
        return window.CUSTOM_PARAMETERS.getCustomParamValue(
            {name: p.source_name, type: p.type, source: p.source});
    }""", unique_name)


def test_registry_lists_the_key_the_sdk_reads_not_just_the_display_name(page):
    """unique_name is what a tester sees in the admin UI; source_name is what the SDK reads, and
    they are free to differ (this property has TextURL_Qfield -> window.textURLQ_field). Typing
    the display name sets a key nothing looks at and fails silently, which is the whole reason
    this panel exists."""
    open_parameter_registry(page)
    params = page.evaluate(
        "() => propertyCustomParamRegistry.map(p => "
        "({u: p.uniqueName, s: p.source, sn: p.sourceName, t: p.type}))")
    assert len(params) >= 10, f"expected this property's full parameter list, got {params}"
    assert {p["s"] for p in params} <= {"var", "url", "cookie"}
    assert {p["t"] for p in params} <= {"Text", "Number", "Boolean", "Datetime"}

    divergent = [p for p in params if p["u"] != p["sn"]]
    assert divergent, "expected at least one parameter whose read key differs from its name"
    # and the divergence has to be on screen, not merely in memory
    assert page.evaluate(
        "(sn) => [...document.querySelectorAll('.property-param-key')]"
        ".some(e => e.innerText.includes(sn))", divergent[0]["sn"])


@pytest.mark.parametrize("unique_name,typed", [
    ("test", "hello-text"),            # var    / Text
    ("NumCP", "42"),                   # var    / Number
    ("Bool", "true"),                  # var    / Boolean
    ("TextURL_Qfield", "diverged"),    # var    / Text, read key differs from the display name
    ("CP2", "77"),                     # url    / Number
    ("CP3", "false"),                  # cookie / Boolean
    ("TextCookiefield", "ck"),         # cookie / Text
])
def test_a_value_set_here_is_the_value_the_sdk_reads(page, unique_name, typed):
    """The panel is only worth anything if the SDK agrees. Asserting that the hub wrote *a* value
    somewhere proves nothing — this asks the SDK's own resolver, across all three sources."""
    open_parameter_registry(page)
    set_parameter(page, unique_name, typed)

    got = sdk_reads(page, unique_name)
    expected = {"true": True, "false": False}.get(typed, typed)
    if str(typed).lstrip("-").isdigit():
        expected = float(typed)
        assert got is not None and float(got) == expected
    else:
        assert got == expected, f"{unique_name}: SDK read {got!r}, expected {expected!r}"


def test_datetime_is_written_as_epoch_millis_so_the_sdk_can_read_it(page):
    """Regression: a Datetime parameter was written as an ISO string. The SDK casts a Datetime
    with Number()/parseInt() on strings and rejects anything that is neither a string nor a Date,
    so an ISO string and a raw number both cast to null — the panel showed the date as set while
    the rule engine saw an empty parameter."""
    open_parameter_registry(page)
    set_parameter(page, "datetimevar", "2026-03-04T05:06")

    got = sdk_reads(page, "datetimevar")
    assert isinstance(got, (int, float)) and got > 0, f"SDK read {got!r}, expected epoch millis"
    assert got == page.evaluate("() => new Date('2026-03-04T05:06').getTime()")


def test_a_value_the_sdk_cannot_read_is_reported_rather_than_shown_as_set(page):
    """A value present on the page but rejected by the SDK's cast is the worst failure mode: the
    tester sees it set and targeting sees nothing. The row has to say so."""
    open_parameter_registry(page)
    page.evaluate("() => { window.NumCP = 'not-a-number'; refreshPropertyCustomParamLiveValues(); }")
    page.wait_for_timeout(600)

    warnings = page.evaluate(
        "() => [...document.querySelectorAll('.property-param-warning')].map(e => e.innerText)")
    assert any("reads this as empty" in w for w in warnings), warnings
    assert sdk_reads(page, "NumCP") is None


def test_filtering_never_points_set_at_a_different_parameter(page):
    """Rows are addressed by their index in the full registry. If Set used the row's position in
    the filtered view instead, filtering would silently write to the wrong parameter."""
    open_parameter_registry(page)
    expected_index = registry_index(page, "CP2")
    page.fill("#property-params-filter", "cp2")
    page.wait_for_timeout(300)

    rows = page.evaluate(
        "() => [...document.querySelectorAll('.property-param-row')].map(r => r.dataset.registryIndex)")
    assert rows == [str(expected_index)], f"filter showed {rows}, expected only {expected_index}"
    set_parameter(page, "CP2", "77")
    assert float(sdk_reads(page, "CP2")) == 77


def test_bulk_apply_ignores_parameters_hidden_by_the_filter(page):
    """"Set every filled value" must mean every value the tester can see. Applying a draft hidden
    behind a filter would change targeting with no visible cause."""
    open_parameter_registry(page)
    page.evaluate("() => { propertyCustomParamDraftValues['test'] = 'should-not-apply'; }")
    visible = registry_index(page, "CP1")
    page.fill("#property-params-filter", "cp1")
    page.wait_for_timeout(300)
    page.fill(f"#property-param-input-{visible}", "visible-one")
    page.evaluate(
        "() => [...document.querySelectorAll('#property-params-actions button')][0].click()")
    page.wait_for_timeout(600)

    assert sdk_reads(page, "CP1") == "visible-one"
    assert page.evaluate("() => window.test === undefined"), "a hidden draft was applied"


def test_parameters_set_here_join_the_shared_dashboard_and_are_purged_with_it(page):
    """A fetched parameter has to go through the same stores as a hand-added one, or it becomes a
    second invisible kind of parameter that Purge All does not clear and a reload does not keep."""
    open_parameter_registry(page)
    set_parameter(page, "test", "in-dashboard")
    set_parameter(page, "CP3", "true")

    listed = page.evaluate(
        "() => [...document.querySelectorAll('.dashboard-item')].map(e => e.innerText)")
    assert any("test" in row for row in listed), listed
    assert any("CP3" in row for row in listed), listed

    page.evaluate("() => clearAllSimulatedParameters()")
    page.wait_for_timeout(400)
    assert page.evaluate("() => window.test === undefined")
    assert sdk_reads(page, "test") is None


# --------------------------------------------------------------------------------------------
# Repro permalink: a link has to reproduce the state that decides the outcome.
# --------------------------------------------------------------------------------------------

def test_repro_link_carries_the_custom_parameters_that_decide_the_outcome(page, browser):
    """Regression: the link was built from `location.href.split('?')[0]`, which threw away the
    query string and never re-added the parameters — so it dropped every var/cookie parameter
    *and* the URL-sourced ones it had just written into the address bar. Since a custom parameter
    is frequently the whole reason a form does or does not show, the link could reproduce the
    opposite of the thing being reported.

    Opened in a brand-new context, not a reload: a reload passes trivially because the browser
    still holds the cookies and the window variables, which is exactly what hid the cookie half
    of this bug.
    """
    open_parameter_registry(page)
    set_parameter(page, "test", "repro-var")
    set_parameter(page, "CP2", "55")
    set_parameter(page, "TextCookiefield", "repro-cookie")
    page.evaluate("setConsentState('granted')")

    link = page.evaluate("() => buildReproPermalink()")
    assert "qa_params" in link, f"the link carries no parameters: {link}"
    assert "qa_consent=granted" in link

    fresh = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        other = fresh.new_page()
        other.goto(link)
        other.wait_for_function("() => !!window.KAMPYLE_ONSITE_SDK", timeout=35000)
        other.wait_for_function(
            "() => window.CUSTOM_PARAMETERS "
            "&& window.CUSTOM_PARAMETERS.getCustomParamValueByUniqueName('test') === 'repro-var'",
            timeout=20000)
        # Read back through the SDK's own resolver, once per source, because each is restored a
        # different way: a window property, a cookie write, and the address bar.
        assert other.evaluate("() => CUSTOM_PARAMETERS.getCustomParamValueByUniqueName('test')") == "repro-var"
        assert other.evaluate("() => CUSTOM_PARAMETERS.getCustomParamValueByUniqueName('TextCookiefield')") == "repro-cookie"
        assert other.evaluate("() => CUSTOM_PARAMETERS.getCustomParamValueByUniqueName('CP2')") == 55
        assert other.evaluate("() => getConsentState()") == "granted"
    finally:
        fresh.close()


def test_repro_link_does_not_destroy_a_deep_linked_route(page):
    """Restoring URL parameters rewrites the address bar, and the deep-link route is recovered
    from location.pathname in the same handler — doing the rewrite first erased /page3 before
    anything had read it."""
    page.goto(BASE + "page3", wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelectorAll('#workspace-bar .workspace-tab').length > 0",
                           timeout=15000)
    assert page.evaluate("() => document.getElementById('page3').classList.contains('active')"), \
        "the deep-linked page was lost during restore"


# --------------------------------------------------------------------------------------------
# Runtime exceptions: an error thrown inside the SDK is a finding, not a network failure.
# --------------------------------------------------------------------------------------------

SDK_STACK = ("TypeError\\n    at Ri (https://resources.digital-cloud-qa-web.medallia.com"
             "/websites/165099/onsite/generic1787233851548.js:0:162396)")


def test_an_exception_thrown_out_of_an_sdk_call_is_blamed_on_the_sdk(page):
    """Regression: window.onerror flattened every error to one string, kept only the file's
    basename, discarded the stack entirely and dispatched it as a *network* error — so an
    exception thrown inside the Medallia bundle was indistinguishable from a bug in this tool and
    was reported as a finding nowhere. Encountered for real: updatePageView() throws a TypeError
    out of generic*.js while its invitation is being torn down."""
    inject(page)
    page.evaluate("""() => {
        const err = new TypeError("Cannot read properties of null (reading 'style')");
        err.stack = %s;
        recordCaughtSdkException(err, 'KAMPYLE_ONSITE_SDK.updatePageView()');
    }""" % repr(SDK_STACK).replace("\\\\n", "\\n"))

    entries = page.evaluate("() => capturedRuntimeExceptions.map(e => ({o: e.origin, m: e.message}))")
    sdk = [e for e in entries if e["o"] == "sdk"]
    assert sdk, f"the SDK was not blamed for its own exception: {entries}"
    assert "updatePageView" in sdk[0]["m"]
    assert page.evaluate("() => countSdkAttributedExceptions()") >= 1

    # and it has to be visible without going looking for it
    page.wait_for_function(
        "() => [...document.querySelectorAll('.status-chip')].some(c => c.innerText.includes('SDK errors'))",
        timeout=6000)
    assert "## Runtime exceptions" in page.evaluate("() => buildSessionReportMarkdown()")


def test_a_cross_origin_script_error_is_reported_as_hidden_not_blamed_on_the_tool(page):
    """The browser blanks out an uncaught error raised inside a cross-origin script — message
    becomes "Script error.", no source, no stack. Guessing an owner from that is impossible, so
    it gets its own category. Blaming the tool would be wrong and blaming the SDK would be a
    claim this app cannot support.

    onerror is invoked with the argument shape the browser actually passes, rather than by
    loading a real cross-origin script. That shape was confirmed by hand against a genuine
    cross-origin script that throws at runtime (served from 127.0.0.1 while the page was on
    localhost). It cannot be reproduced from within this suite: qa-server rewrites unknown paths
    to the app's HTML, and a cross-origin *parse* error fires no onerror at all — only a runtime
    throw does — so the fixture would need a second server serving real JavaScript purely to
    re-observe a browser behaviour this test is not the thing verifying.
    """
    page.evaluate("() => window.onerror('Script error.', '', 0, 0, null)")

    blocked = page.evaluate("() => capturedRuntimeExceptions.find(e => e.origin === 'blocked')")
    assert blocked, page.evaluate("() => capturedRuntimeExceptions.map(e => e.origin)")
    assert page.evaluate("() => countSdkAttributedExceptions()") == 0, \
        "an unattributable error was counted as an SDK defect"
    # the panel has to say why it knows nothing, rather than showing an empty row
    page.evaluate("""() => {
        const head = [...document.querySelectorAll('.diag-finding-head')]
            .find(h => h.parentElement.innerText.includes('Script error'));
        if (head) head.click();
    }""")
    assert "withheld" in page.inner_text("#diagnostics-container")


def test_repeated_exceptions_are_grouped_rather_than_flooding_the_panel(page):
    """An SDK error on a timer can fire hundreds of times. Ungrouped it buries every other
    finding, which is how a panel meant to surface problems ends up hiding them."""
    for _ in range(3):
        page.evaluate("""() => {
            const err = new TypeError('the same failure again');
            err.stack = 'TypeError\\n    at sameFrame (http://localhost/x.js:1:1)';
            recordCaughtSdkException(err, 'KAMPYLE_ONSITE_SDK.updatePageView()');
        }""")
        page.wait_for_timeout(120)

    matching = page.evaluate(
        "() => capturedRuntimeExceptions.filter(e => e.message.includes('the same failure again'))")
    assert len(matching) == 1, f"expected one grouped row, got {len(matching)}"
    assert matching[0]["count"] == 3


def test_script_errors_no_longer_land_in_the_failed_network_requests_panel(page):
    """They were dispatched as network errors, which put script crashes under a heading about
    the network and inflated the network-error count the status bar reports."""
    page.evaluate("""() => { const s = document.createElement('script');
        s.textContent = "setTimeout(function hubFrame(){ window.__nope.x = 1; }, 10);";
        document.body.appendChild(s); }""")
    page.wait_for_function("() => capturedRuntimeExceptions.length > 0", timeout=8000)

    assert page.evaluate(
        "() => document.querySelectorAll('#network-error-stream-box .network-err-entry').length") == 0
    assert page.evaluate("() => capturedRuntimeExceptions[0].origin") == "hub", \
        "the page's own error should be attributed to this tool"


def test_setting_a_parameter_from_the_panel_makes_a_gated_form_appear(page):
    """The whole point of the parameter list, end to end: pick the parameter a form's rule names,
    type a value, and the form renders.

    Every other test here proves a link in that chain — the registry is read, the right key is
    written, the SDK resolves it. This proves the chain itself, which is the only claim a tester
    actually cares about. It needs a form to genuinely render, so it is gated on the same domain
    allow-list check as the other two.
    """
    open_parameter_registry(page)
    skip_if_this_domain_is_not_allow_listed(page)
    clear_the_screen(page)

    embedded_rule = page.evaluate("""() => {
        const f = (readPublishedFormRegistryFromSdk()||[]).find(x => x.formType === 'embedded');
        return f && f.onSiteData && f.onSiteData.genericRule
            ? collectCriteriaFromRuleGroup(f.onSiteData.genericRule)
                .filter(c => c.fieldOrigin === 'customParam')
                .map(c => ({name: c.fieldName, condition: c.condition, value: c.value}))
            : [];
    }""")
    if not embedded_rule:
        pytest.skip("the embedded form on this property carries no custom-parameter rule to satisfy")
    assert page.evaluate(EMBEDDED_SHOWN) is False, "precondition: the form must start hidden"

    # The count rule is satisfied directly — this test is about the parameter, and leaving another
    # rule blocking would hide a working parameter behind an unrelated failure. Seeded high
    # because the SDK increments the stored count on load before evaluating.
    page.evaluate("() => localStorage.setItem('kampyleUserSessionsCount', '5')")

    criterion = embedded_rule[0]
    set_parameter(page, criterion["name"], criterion["value"])

    page.wait_for_function(EMBEDDED_SHOWN, timeout=25000)
    assert page.evaluate(
        "(n) => CUSTOM_PARAMETERS.getCustomParamValueByUniqueName(n)", criterion["name"]
    ) == criterion["value"]


# --------------------------------------------------------------------------------------------
# Diagnostics: one place for everything wrong, explained rather than dumped.
# --------------------------------------------------------------------------------------------

def test_the_sdk_console_output_is_captured_at_all(page):
    """Regression: this app patched nothing on console, so everything the SDK said there was
    invisible to it. The bundle has twenty console call sites and several are the only notice
    that something is structurally wrong."""
    inject(page)
    page.evaluate("setActiveWorkspace('config')")   # reading the live config makes the SDK log
    page.wait_for_function("() => capturedSdkConsoleMessages.length > 0", timeout=20000)

    captured = page.evaluate("() => capturedSdkConsoleMessages.map(m => ({level: m.level, origin: m.origin}))")
    assert any(m["origin"] == "sdk" for m in captured), f"nothing attributed to the SDK: {captured}"
    # the capture must not eat the message: the real console still has to receive it
    assert page.evaluate("() => console.error.toString().includes('native code') === false"), \
        "console.error should be a wrapper, not replaced wholesale"


def test_console_capture_reports_the_caller_not_its_own_plumbing(page):
    """The captured stack begins inside this file's console wrapper. The frame a developer wants
    is whoever called console.error, so the wrapper's own frames are trimmed off the top."""
    inject(page)
    page.evaluate("setActiveWorkspace('config')")   # reading the live config makes the SDK log
    page.wait_for_function("() => capturedSdkConsoleMessages.length > 0", timeout=20000)

    stacks = page.evaluate("() => capturedSdkConsoleMessages.map(m => m.stack)")
    for stack in stacks:
        first = next((line for line in stack.split("\n") if line.strip()), "")
        assert "captureConsoleMessage" not in first and "console." not in first, \
            f"the stack still starts inside the capture wrapper: {first}"


def test_a_blocked_domain_is_explained_rather_than_left_as_a_raw_string(page):
    """The highest-value finding in the panel, and the reason it is not just a console mirror.

    A domain block makes the SDK load, publish its whole form registry and report targeting
    verdicts normally while never rendering anything — so every other panel looks healthy. It
    says "onsite origin domain is not allowed" on the console once, during init, which can land
    before any capture is listening. Deriving it from the configuration instead catches it
    regardless of what was captured in time.
    """
    inject(page)
    page.evaluate("""() => {
        lastKnownDomainsConfiguration = {allDomainsAllowed: false, domainsNames: ['www.example.com', '*.medallia.com']};
        renderDiagnosticsPanel();
    }""")

    finding = page.evaluate("() => buildDiagnosticFindings().find(f => f.severity === 'blocker')")
    assert finding, page.evaluate("() => buildDiagnosticFindings().map(f => f.severity)")
    assert "never render" in finding["title"]
    # the explanation must name the real allow-list and this host, not describe the idea of one
    assert "www.example.com" in finding["explanation"]
    assert page.evaluate("() => window.location.hostname") in finding["explanation"]
    assert finding["fix"], "a blocker with no stated remedy is only half a finding"
    # the remedy has to be on screen, not merely in the data
    page.evaluate("() => document.querySelector('.diag-finding-blocker .diag-finding-head').click()")
    panel_text = page.inner_text("#diagnostics-container")
    assert "What to do" in panel_text and "allowed domains in Medallia" in panel_text

    # and it must reach the status bar, or a tester looking at another workspace never learns
    page.wait_for_function(
        "() => [...document.querySelectorAll('.status-chip')].some(c => c.innerText.includes('Blocking issue'))",
        timeout=6000)


def test_a_wildcard_allow_list_entry_matches_its_subdomains(page):
    """The configuration writes subdomain wildcards as `*.example.com`. Treating one as a literal
    hostname would report a blocker on a domain that is actually allowed, and a false blocker is
    considerably worse than none — it sends someone to fix a setting that was already correct.

    Driven through the real check by swapping the configuration, rather than by restating the
    matching rule here: a test that reimplements the logic it is checking passes even when the
    app's copy is wrong.
    """
    inject(page)
    host = page.evaluate("() => window.location.hostname")   # 'localhost' under this server

    def blockers_for(domains):
        return page.evaluate("""(domains) => {
            lastKnownDomainsConfiguration = {allDomainsAllowed: false, domainsNames: domains};
            return buildDiagnosticFindings().filter(f => f.source === 'Property config').length;
        }""", domains)

    assert blockers_for([f"*.{host}"]) == 0, "a wildcard must match its own bare domain"
    assert blockers_for([host]) == 0, "an exact entry must match"
    assert blockers_for(["*.medallia.com"]) == 1, "an unrelated wildcard must not match"
    # the trap: endsWith() alone would let '*.host' match 'localhost'
    assert blockers_for(["*.host"]) == 1, "a wildcard must not match by bare suffix"

    # and a property that allows everything must never produce this finding
    assert page.evaluate("""() => {
        lastKnownDomainsConfiguration = {allDomainsAllowed: true, domainsNames: []};
        return buildDiagnosticFindings().filter(f => f.source === 'Property config').length;
    }""") == 0


def test_the_sdk_error_channel_becomes_a_finding(page):
    """The SDK reports structural failures through triggerError, which fires
    neb_eventDispatcherError carrying a message that names the real cause — richer than the
    console line. Those events already flowed through the event-bus tap, but landed unlabelled in
    the raw stream among every other event and were reported as a finding nowhere."""
    inject(page)
    page.evaluate("""() => KAMPYLE_EVENT_DISPATCHER.trigger('neb_eventDispatcherError',
        {errorMessage: 'qa.example.com domain was blocked! domainsConfiguration: {}'})""")
    page.wait_for_timeout(400)

    assert page.evaluate("() => capturedSdkErrorEvents.length") >= 1
    finding = page.evaluate(
        "() => buildDiagnosticFindings().find(f => f.source === 'SDK error channel')")
    assert finding, "the SDK's own error channel produced no finding"
    assert finding["severity"] == "blocker"
    assert "never render" in finding["title"], finding["title"]


def test_clearing_diagnostics_keeps_what_is_still_true(page):
    """Clear drops what has been *observed*. A finding derived from configuration is not history —
    the domain is still wrong after the list is emptied — so it must come straight back, and the
    raw network panel it shares a store with must not be left contradicting it."""
    inject(page)
    page.evaluate("""() => {
        lastKnownDomainsConfiguration = {allDomainsAllowed: false, domainsNames: ['nowhere.example']};
        renderDiagnosticsPanel();
    }""")
    assert page.evaluate("() => countBlockingDiagnostics()") >= 1

    page.evaluate("() => clearCapturedDiagnostics()")
    assert page.evaluate("() => capturedSdkConsoleMessages.length") == 0
    assert page.evaluate("() => countBlockingDiagnostics()") >= 1, \
        "the domain is still wrong, so its finding must survive a Clear"
    assert page.evaluate(
        "() => document.querySelectorAll('#network-error-stream-box .network-err-entry').length") == 0


# --------------------------------------------------------------------------------------------
# Field-level conditional display, and per-screenshot download.
# --------------------------------------------------------------------------------------------

def find_conditional_display_field(pg):
    """The first form on this property that has a field driven by a custom parameter."""
    return pg.evaluate("""() => {
        for (const form of (readPublishedFormRegistryFromSdk() || [])) {
            const fields = readConditionalDisplayFieldsForForm(form.formId);
            if (!fields) continue;
            for (const field of fields) {
                const condition = field.conditions.find(c => c.type === 'custom-param');
                if (condition) return {formId: form.formId, field, condition};
            }
        }
        return null;
    }""")


def test_conditional_display_fields_are_read_from_the_form_definition(page):
    """Targeting says whether a form appears; this is the layer below — whether a field inside it
    appears. A tester with the form open and a field missing had nothing to tell them why."""
    inject(page)
    # definitions arrive either from the SDK's own fetch or the formNodes fallback
    page.wait_for_function("""() => (readPublishedFormRegistryFromSdk() || [])
        .some(f => (readConditionalDisplayFieldsForForm(f.formId) || []).length > 0)""", timeout=20000)

    found = find_conditional_display_field(page)
    if not found:
        pytest.skip("no form on this property drives a field from a custom parameter")

    assert found["field"]["label"], "a field with no readable label is no use to a tester"
    described = page.evaluate("(c) => describeConditionalDisplayCondition(c)", found["condition"])
    assert found["condition"]["customParamName"] in described
    assert "custom param" in described


def test_a_conditional_display_fix_is_typed_for_the_parameter_it_writes(page):
    """Regression caught in review: "does not equal" appended "-qa" regardless of type, so for a
    Datetime parameter it produced an ISO string with a suffix — which the SDK casts to empty.
    The button would have reported success while the condition still failed, which is the exact
    trap the manual Datetime field fell into.

    Anything derived is put back through the real encoder, so a value the SDK would read as empty
    offers no button at all rather than a fix that silently does nothing.
    """
    open_parameter_registry(page)
    page.wait_for_function("""() => (readPublishedFormRegistryFromSdk() || [])
        .some(f => (readConditionalDisplayFieldsForForm(f.formId) || []).length > 0)""", timeout=20000)

    checked = page.evaluate("""() => {
        const out = [];
        for (const form of (readPublishedFormRegistryFromSdk() || [])) {
            for (const field of (readConditionalDisplayFieldsForForm(form.formId) || [])) {
                for (const condition of field.conditions) {
                    if (condition.type !== 'custom-param') continue;
                    const param = findPropertyCustomParamByName(condition.customParamName);
                    const value = deriveValueSatisfyingConditionalDisplay(condition, param);
                    if (value === null) continue;
                    out.push({type: param ? param.type : null, operator: condition.condition, value,
                              encodeError: !!encodePropertyCustomParamValueForSdk(
                                  value, param ? param.type : 'Text', param ? param.source : 'var').error});
                }
            }
        }
        return out;
    }""")
    if not checked:
        pytest.skip("no satisfiable custom-parameter condition on this property")

    for row in checked:
        assert row["encodeError"] is False, f"offered a value the SDK cannot read: {row}"
        if str(row["type"]).lower() == "datetime":
            assert row["value"].isdigit(), \
                f"a Datetime parameter needs epoch millis, got {row['value']!r} for {row['operator']!r}"


def test_setting_a_field_condition_writes_the_parameter_the_sdk_reads(page):
    """The button is only worth anything if it reaches the key the SDK resolves — which is
    source_name on the parameter's own source, not the display name in the condition."""
    open_parameter_registry(page)
    page.wait_for_function("""() => (readPublishedFormRegistryFromSdk() || [])
        .some(f => (readConditionalDisplayFieldsForForm(f.formId) || []).length > 0)""", timeout=20000)

    found = find_conditional_display_field(page)
    if not found or not page.evaluate("(n) => !!findPropertyCustomParamByName(n)",
                                      found["condition"]["customParamName"]):
        pytest.skip("no conditional field backed by a parameter in this property's registry")

    index = found["field"]["conditions"].index(found["condition"])
    page.evaluate("(a) => applyConditionalDisplayFix(a.formId, a.fieldId, a.index)",
                  {"formId": found["formId"], "fieldId": found["field"]["componentId"], "index": index})
    page.wait_for_timeout(500)

    # Resolved through a fresh descriptor rather than getCustomParamValueByUniqueName, which is
    # not a reliable oracle: the SDK caches the resolved value onto its own config object, and
    # extractCPValue only re-reads the source when that cached `value` is undefined. A parameter
    # the SDK had already resolved as empty keeps reporting empty from cache even once the cookie
    # exists. Passing a fresh object forces the real read, and is how the app's own live column
    # reads it.
    name = found["condition"]["customParamName"]
    resolved = page.evaluate("""(n) => {
        const p = CUSTOM_PARAMETERS.getAllCustomParams().find(x => x.unique_name === n);
        return p ? CUSTOM_PARAMETERS.getCustomParamValue({name: p.source_name, type: p.type, source: p.source}) : null;
    }""", name)
    assert resolved is not None, f"the SDK still reads {name} as empty after the fix was applied"


def test_each_captured_screenshot_can_be_saved_on_its_own(page):
    """Previously the only way to get one screenshot out was to file the whole report and open the
    evidence pack. The frames are already data URLs, so a plain download link writes one straight
    to disk with no re-encoding."""
    tiny_png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z"
                "8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    page.evaluate("""(png) => {
        capturedEvidenceFrames.set('f1', png);
        capturedIssueEvidence.set('k1', {title: 'Decline button contrast 1.94:1', area: 'WCAG Contrast',
                                         severity: 'block', frameId: 'f1', at: Date.now()});
        capturedIssueEvidence.set('k2', {title: 'No capture for this one', area: 'Layout',
                                         severity: 'warn', frameId: null, at: Date.now()});
        renderBugReportPanel();
    }""", tiny_png)

    links = page.eval_on_selector_all(
        "a.evidence-download-btn", "els => els.map(e => e.getAttribute('download'))")
    assert len(links) == 1, "exactly the row that has a frame should offer a download"
    # named after the issue so a folder of these is readable later, and typed from the data URL
    # rather than a hardcoded extension that would quietly start lying
    assert links[0].startswith("onsite-Decline-button-contrast")
    assert links[0].endswith(".png")
    # the row with no capture must not offer a dead link
    assert page.eval_on_selector_all("span.evidence-download-btn", "els => els.length") == 1

    with page.expect_download(timeout=8000) as download:
        page.evaluate("() => document.querySelector('a.evidence-download-btn').click()")
    assert download.value.suggested_filename.endswith(".png")


# --------------------------------------------------------------------------------------------
# Panel rail: 26 panels reachable without scrolling for them.
# --------------------------------------------------------------------------------------------

def test_the_rail_indexes_every_panel_exactly_once(page):
    """The rail is a view over the panels that exist, not a second list of them — it is built by
    reading the same [data-workspace] children assignPanelsToWorkspaces() labels. A hand-written
    index would drift from the panels it describes the first time one was renamed."""
    inject(page)
    page.wait_for_function("() => document.querySelectorAll('.rail-item').length > 0", timeout=15000)

    rail = page.eval_on_selector_all(".rail-item", "els => els.map(e => e.dataset.railTarget)")
    panels = page.evaluate("""() => [...document.querySelectorAll('#master-canvas-wrapper > [data-workspace]')]
        .filter(p => p.getAttribute('data-workspace') !== 'always' && p.querySelector('h3, h4'))
        .map(p => p.dataset.railId)""")
    assert sorted(rail) == sorted(panels), "the rail and the canvas disagree about what exists"
    assert len(rail) == len(set(rail)), "a panel is indexed twice"
    assert len(rail) >= 20, f"only {len(rail)} panels indexed"


def test_a_rail_label_is_the_panel_name_not_its_live_status(page):
    """Regression: the label came from heading.innerText, which swept in the status pills sharing
    that row — producing entries like "Journey Automation Idle" and "Config Drift Baseline No
    baseline saved" that changed as the panel's state changed. A navigation label has to hold
    still."""
    inject(page)
    page.wait_for_function("() => document.querySelectorAll('.rail-item').length > 0", timeout=15000)

    labels = page.eval_on_selector_all(".rail-label", "els => els.map(e => e.innerText.trim())")
    for label in labels:
        assert not label.endswith(("Idle", "—", "-", ":")), f"label carries live state: {label!r}"
        for leak in ("No baseline saved", "Awaiting property", "None captured", "Nothing wrong"):
            assert leak not in label, f"label carries a status pill: {label!r}"


def test_the_rail_reaches_a_panel_in_a_workspace_you_are_not_looking_at(page):
    """The whole point: any panel, one click, from anywhere. jumpToPanelTarget already switches
    workspace and expands a collapsed panel, so the rail reuses it rather than reimplementing the
    reveal — three places doing that differently is how they drift."""
    inject(page)
    page.evaluate("setActiveWorkspace('targeting')")
    page.wait_for_function("() => document.querySelectorAll('.rail-item').length > 0", timeout=15000)

    target = page.evaluate("""() => {
        const item = [...document.querySelectorAll('.rail-item')]
            .find(b => b.innerText.includes('Diagnostics'));
        item.click();
        return item.dataset.railTarget;
    }""")
    page.wait_for_timeout(700)

    assert page.evaluate("() => activeWorkspaceKey") == "health", "the workspace did not follow"
    assert page.evaluate("(id) => {const p = document.querySelector(`[data-rail-id='${id}']`); return !!(p && p.offsetParent);}", target)
    assert page.eval_on_selector_all(".rail-item-active .rail-label", "els => els.length") == 1


def test_the_rail_filter_narrows_to_matching_panels(page):
    """26 entries is past the point where scanning beats typing."""
    inject(page)
    page.wait_for_function("() => document.querySelectorAll('.rail-item').length > 0", timeout=15000)
    # set directly rather than typed: an SDK invitation can hold focus, which is a property of
    # the SDK rather than of the filter (see open_parameter_registry).
    page.evaluate("""() => {
        const f = document.getElementById('rail-filter-input');
        f.value = 'contrast'; f.dispatchEvent(new Event('input'));
    }""")
    page.wait_for_timeout(300)

    labels = page.eval_on_selector_all(".rail-label", "els => els.map(e => e.innerText)")
    assert labels and all("contrast" in l.lower() for l in labels), labels

    page.evaluate("""() => {
        const f = document.getElementById('rail-filter-input');
        f.value = 'zzzzz'; f.dispatchEvent(new Event('input'));
    }""")
    page.wait_for_timeout(200)
    assert "No panel matches" in page.inner_text("#rail-body")


def test_sticky_bars_sit_below_each_other_rather_than_underneath(page):
    """Regression: the sticky offsets were hardcoded at 62/104/146px while the nav wraps to a
    second row on a narrower desktop and the chip row grows as chips appear — so each bar tucked
    under the one above it and clipped it. The offsets are measured from the DOM now."""
    inject(page)
    page.set_viewport_size({"width": 1500, "height": 950})
    page.evaluate("() => window.scrollTo(0, 1400)")
    page.wait_for_timeout(600)

    geometry = page.evaluate("""() => {
        const nav = document.querySelector('nav').getBoundingClientRect();
        const bar = document.getElementById('global-status-bar').getBoundingClientRect();
        const rail = document.getElementById('panel-rail').getBoundingClientRect();
        return {navBottom: nav.bottom, barTop: bar.top, barBottom: bar.bottom, railTop: rail.top};
    }""")
    assert geometry["barTop"] >= geometry["navBottom"] - 1, \
        f"the status bar is under the nav: {geometry}"
    assert geometry["railTop"] >= geometry["barBottom"] - 1, \
        f"the rail is under the status bar: {geometry}"


def test_triage_holds_its_height_so_the_page_does_not_shift_under_the_reader(page):
    """Regression, caught by this app's own Performance panel: triage sits at the top of the
    document, so every time its height changed — the start card giving way to the findings list,
    then each audit landing a row a second later — it shoved everything below it down. Measured
    Cumulative Layout Shift since injection went from 0.027 to 0.103, past the 0.100 'good'
    threshold, in a tool that reports that number back to the tester as the property's own.

    A reserved min-height only got it to 0.093, because the list still grew past the reserve.
    Fixing the height removed the mechanism: findings past the first few scroll inside the card.
    """
    heights = []
    heights.append(page.eval_on_selector("#triage-view", "e => Math.round(e.getBoundingClientRect().height)"))
    inject(page)
    for _ in range(4):
        page.wait_for_timeout(1500)
        heights.append(page.eval_on_selector("#triage-view", "e => Math.round(e.getBoundingClientRect().height)"))

    assert len(set(heights)) == 1, f"the triage slot changed height and moved the page: {heights}"
    # it must still be scrollable rather than clipping findings out of existence
    assert page.eval_on_selector("#triage-items, .triage-items",
                                 "e => getComputedStyle(e).overflowY") in ("auto", "scroll")


def test_cumulative_layout_shift_stays_in_the_good_band(page):
    """The number this app reports as the property's, so its own chrome must not dominate it.
    Measured on main before the rail and triage landed: 0.027."""
    inject(page)
    page.wait_for_timeout(6000)

    cls = page.evaluate("() => {const r = buildPerformanceReport(); return r ? r.clsSinceInjection : null;}")
    assert cls is not None, "no performance report — layout-shift unsupported in this browser?"
    assert cls < 0.100, f"the dashboard's own layout shift is being reported as the property's: {cls:.3f}"


# --------------------------------------------------------------------------------------------
# Colour theme.
# --------------------------------------------------------------------------------------------

CONTRAST_SAMPLER = """() => {
    const parse = c => { const m = c.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);
        return m ? {r:+m[1], g:+m[2], b:+m[3], a:m[4] === undefined ? 1 : +m[4]} : null; };
    const hex = o => '#' + [o.r,o.g,o.b].map(v => Math.round(v).toString(16).padStart(2,'0')).join('');
    const bgOf = el => { let n = el;
        while (n && n !== document.documentElement) {
            const c = parse(getComputedStyle(n).backgroundColor);
            if (c && c.a > 0.85) return c;
            n = n.parentElement;
        }
        return parse(getComputedStyle(document.body).backgroundColor) || {r:255,g:255,b:255,a:1}; };
    const scope = 'nav, .global-status-bar, .panel-rail, #triage-view, .diag-panel, .suite-panel, '
                + '.lifecycle-state-panel, .lifecycle-timer-container, .injector-panel, .log-panel';
    const failures = [];
    document.querySelectorAll(scope + ', ' + scope.split(', ').map(s => s + ' *').join(', ')).forEach(el => {
        if (!el.offsetParent) return;
        const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join('');
        if (!own) return;
        const style = getComputedStyle(el);
        const fg = parse(style.color);
        if (!fg || fg.a < 0.5) return;
        const size = parseFloat(style.fontSize);
        const bold = (parseInt(style.fontWeight) || 400) >= 700;
        const large = size >= 24 || (size >= 18.66 && bold);
        const ratio = computeContrastRatio(hex(fg), hex(bgOf(el)));
        const need = large ? 3.0 : 4.5;
        if (ratio && ratio < need) failures.push({text: own.slice(0, 40), fg: hex(fg), bg: hex(bgOf(el)),
                                                 ratio: +ratio.toFixed(2), need});
    });
    return failures;
}"""


def open_with_theme(pg, theme):
    pg.evaluate("(t) => localStorage.setItem('qa_colour_theme', t)", theme)
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_function("() => document.querySelectorAll('#workspace-bar .workspace-tab').length > 0",
                         timeout=15000)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_the_hub_meets_the_contrast_standard_it_enforces(page, theme):
    """This app runs a WCAG contrast audit on other people's properties. A theme of its own that
    fails that audit would be the tool contradicting itself — so both themes are held to it,
    using the app's own computeContrastRatio rather than a second implementation."""
    open_with_theme(page, theme)
    inject(page)
    page.wait_for_timeout(1500)

    failures = page.evaluate(CONTRAST_SAMPLER)
    assert not failures, f"{theme} theme has {len(failures)} pair(s) below AA: {failures[:5]}"


def test_the_theme_is_applied_before_the_first_paint(page):
    """A stored light preference that only lands once the app's own script runs shows a dark
    flash first. The theme is set by a small inline script at the top of <body>, ahead of
    everything else."""
    page.evaluate("() => localStorage.setItem('qa_colour_theme', 'light')")
    page.reload(wait_until="commit")
    # read it at the earliest point the document exists, before load handlers have run
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "light"


def test_switching_theme_sticks_across_a_reload(page):
    open_with_theme(page, "dark")
    page.evaluate("() => toggleColourTheme()")
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "light"

    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelectorAll('#workspace-bar .workspace-tab').length > 0",
                           timeout=15000)
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "light"


def test_with_no_stored_choice_the_os_preference_decides(page, browser):
    """A tester whose whole desktop is light should not have to find a toggle first."""
    for scheme, expected in (("light", "light"), ("dark", "dark")):
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
        try:
            other = context.new_page()
            other.goto(BASE, wait_until="domcontentloaded")
            assert other.evaluate("() => document.documentElement.getAttribute('data-theme')") == expected
        finally:
            context.close()


# --------------------------------------------------------------------------------------------
# Provision overrides and the page console.
# --------------------------------------------------------------------------------------------

def open_provisions(pg):
    inject(pg)
    pg.evaluate("setActiveWorkspace('config')")
    pg.wait_for_function("() => document.querySelectorAll('.provision-row').length > 0", timeout=20000)


def test_a_provision_override_is_what_the_sdk_actually_reads(page):
    """The whole point, and the thing that is easy to fake: the panel could show a flipped value
    while the SDK carries on reading the original. Asserted through the SDK's own
    checkProvision, not through this app's rendering of it.

    It works because checkProvision is getOnsiteConfiguration().provisions[name] evaluated on
    every call, against an object the SDK hands back by reference.
    """
    open_provisions(page)
    name = page.evaluate("""() => {
        const c = readLiveOnsiteConfiguration();
        return Object.keys(c.provisions).find(k => !(c.provisions[k] === true || c.provisions[k] === 'true'));
    }""")
    assert name, "this property has no provision that is currently off"
    assert page.evaluate("(n) => KAMPYLE_FUNC.checkProvision(n)", name) is False

    page.evaluate("(n) => document.querySelector(`[data-provision-set='${n}']`).click()", name)
    page.wait_for_timeout(400)
    assert page.evaluate("(n) => KAMPYLE_FUNC.checkProvision(n)", name) is True, \
        "the panel reported an override the SDK never saw"

    page.evaluate("(n) => document.querySelector(`[data-provision-release='${n}']`).click()", name)
    page.wait_for_timeout(400)
    assert page.evaluate("(n) => KAMPYLE_FUNC.checkProvision(n)", name) is False, \
        "releasing did not restore the property's own value"


def test_the_live_configuration_is_never_held_across_calls(page):
    """Regression: the configuration object was fetched once and cached. The SDK swaps it when
    its own data finishes loading, so the cached reference was a detached copy — writes to it
    succeeded, the panel showed the new value, and checkProvision kept returning the old one. An
    override that reports success and does nothing is worse than one that fails."""
    open_provisions(page)
    same = page.evaluate("""() => {
        const a = readLiveOnsiteConfiguration();
        return a === (window.MDIGITAL.CONFIGURATION.getOnsiteConfiguration());
    }""")
    assert same, "readLiveOnsiteConfiguration is not returning the SDK's live object"


def test_every_provision_switch_lands_in_the_same_column(page):
    """The switch moved to the right-hand end of the row: 98 monospace names of wildly different
    lengths were what the eye had to get past to reach the control, and the name is what you are
    looking for. Right-aligned it only helps if it is a real column, which needs two things --
    the switch last in the row, and its On/Off label width reserved, or the track slides a few
    pixels between an On row and an Off row."""
    inject(page)
    page.evaluate("setActiveWorkspace('config')")
    page.wait_for_function("() => document.querySelectorAll('.provision-row').length > 0", timeout=20000)

    # an overridden row carries two extra children, which is exactly the case a right-aligned
    # column has to survive
    name = page.evaluate("""() => {
        const c = readLiveOnsiteConfiguration();
        return Object.keys(c.provisions).find(k => !(c.provisions[k] === true || c.provisions[k] === 'true'));
    }""")
    page.evaluate("(n) => setProvisionOverride(n, true)", name)
    page.wait_for_timeout(400)

    geometry = page.evaluate("""() => {
        const rows = [...document.querySelectorAll('.provision-row')].slice(0, 60);
        const round = n => Math.round(n);
        return {
            rows: rows.length,
            lastChildIsTheSwitch: rows.every(r => r.lastElementChild.classList.contains('provision-switch')),
            switchRightEdges: [...new Set(rows.map(r => round(r.querySelector('.provision-switch').getBoundingClientRect().right)))],
            trackLeftEdges: [...new Set(rows.map(r => round(r.querySelector('.provision-switch-track').getBoundingClientRect().left)))],
            states: [...new Set(rows.map(r => r.querySelector('.provision-switch').getAttribute('aria-checked')))],
            overriddenRows: document.querySelectorAll('.provision-row-overridden').length
        };
    }""")

    assert geometry["rows"] > 10 and geometry["overriddenRows"] == 1, f"thin sample: {geometry}"
    assert set(geometry["states"]) == {"true", "false"}, \
        f"both an On and an Off row are needed to catch a sliding track: {geometry['states']}"
    assert geometry["lastChildIsTheSwitch"], "a switch is no longer the last thing in its row"
    assert len(geometry["switchRightEdges"]) == 1, f"the switches are ragged: {geometry['switchRightEdges']}"
    assert len(geometry["trackLeftEdges"]) == 1, \
        f"the track shifts between On and Off rows: {geometry['trackLeftEdges']}"

    page.evaluate("(n) => releaseProvisionOverride(n)", name)


def test_overrides_are_pinned_so_they_survive_a_re_injection(page):
    """A provision the SDK consults once at startup cannot be changed after the fact — that needs
    a re-inject, which is the only reason pinning exists."""
    open_provisions(page)
    name = page.evaluate("""() => {
        const c = readLiveOnsiteConfiguration();
        return Object.keys(c.provisions).find(k => !(c.provisions[k] === true || c.provisions[k] === 'true'));
    }""")
    page.evaluate("(n) => setProvisionOverride(n, true)", name)
    stored = page.evaluate("() => JSON.parse(localStorage.getItem('qa_provision_overrides_v1') || '{}')")
    assert stored.get(name) is True

    page.evaluate("() => reapplyStoredProvisionOverrides()")
    assert page.evaluate("(n) => KAMPYLE_FUNC.checkProvision(n)", name) is True

    page.evaluate("() => releaseAllProvisionOverrides()")
    assert page.evaluate("() => localStorage.getItem('qa_provision_overrides_v1')") is None


def test_console_capture_attributes_the_sdk_separately_from_this_tool(page):
    """A developer's first question about a console line is whose it is. Every level is kept for
    the inspector's console subject; Diagnostics reads only the errors and warnings out of the
    same capture and explains what it recognises."""
    inject(page)
    page.evaluate("setActiveWorkspace('config')")   # reading the live config makes the SDK log
    page.wait_for_function("() => capturedSdkConsoleMessages.some(m => m.origin === 'sdk')", timeout=20000)
    page.evaluate("() => { console.log('a plain log'); console.error('a page error'); }")
    page.wait_for_timeout(600)

    captured = page.evaluate("() => capturedSdkConsoleMessages.map(m => ({level: m.level, origin: m.origin}))")
    assert any(m["origin"] == "sdk" for m in captured), f"nothing attributed to the SDK: {captured}"
    assert any(m["origin"] == "hub" for m in captured), f"nothing attributed to this tool: {captured}"
    assert {m["level"] for m in captured} & {"log", "error"}, "levels beyond warn are not captured"

    # the chatter must not leak into the findings list
    sources = page.evaluate("() => buildDiagnosticFindings().map(f => f.source)")
    assert "console.log" not in sources and "console.info" not in sources, sources


def test_console_capture_does_not_swallow_the_real_console(page):
    """Wrapped, never replaced — DevTools has to keep showing everything exactly as before."""
    seen = []
    page.on("console", lambda m: seen.append(m.text))
    page.evaluate("() => console.warn('this must still reach devtools')")
    page.wait_for_timeout(300)
    assert any("must still reach devtools" in t for t in seen), seen


# --------------------------------------------------------------------------------------------
# SDK Inspector, and the configuration override it allows.
# --------------------------------------------------------------------------------------------

def open_inspector(pg, subject="onsite-config"):
    inject(pg)
    pg.evaluate("setActiveWorkspace('inspect')")
    pg.wait_for_function("() => document.querySelectorAll('.inspector-subject').length > 0", timeout=20000)
    pg.evaluate("(s) => document.querySelector(`[data-inspector-subject='${s}']`).click()", subject)
    pg.wait_for_timeout(400)


def test_the_inspector_shows_the_sdk_own_objects_not_this_apps_reading_of_them(page):
    """Every other panel interprets the configuration. This is the source those readings come
    from, for the case where the reading is the thing you doubt."""
    open_inspector(page)
    text = page.eval_on_selector("#inspector-output", "e => e.innerText")
    assert text.strip().startswith("{"), text[:120]
    assert "provisions" in text

    # each subject has to resolve to something real, or it is a dead entry in the list
    populated = page.evaluate("""() => INSPECTOR_SUBJECTS.map(s => {
        const v = readInspectorSubjectValue(s);
        return {id: s.id, empty: v === null || v === undefined};
    })""")
    dead = [s["id"] for s in populated if s["empty"]]
    assert not dead, f"subjects with nothing behind them: {dead}"


def test_inspector_search_highlights_without_letting_config_inject_markup(page):
    """The output is configuration read off the network, so it is escaped before the highlight
    markup goes in — never the other way round."""
    open_inspector(page)
    page.evaluate("""() => {
        const f = document.getElementById('inspector-search-input');
        f.value = 'provisions'; f.dispatchEvent(new Event('input'));
    }""")
    page.wait_for_timeout(300)
    assert page.eval_on_selector_all("#inspector-output mark", "els => els.length") > 0
    assert "matching line" in page.inner_text("#inspector-match-count")

    # a value containing markup must render as text, not as an element
    page.evaluate("""() => {
        readLiveOnsiteConfiguration().__qa_xss__ = '<img src=x onerror=alert(1)>';
        renderInspectorOutput();
    }""")
    page.wait_for_timeout(200)
    assert page.eval_on_selector_all("#inspector-output img", "els => els.length") == 0
    page.evaluate("() => { delete readLiveOnsiteConfiguration().__qa_xss__; }")


def test_applying_edited_json_reaches_the_sdk_and_can_be_restored(page):
    """The whole feature in one pass: preview names the changes, applying reaches the SDK's own
    checkProvision, and Restore puts the property's values back."""
    open_inspector(page, "provisions")
    name = page.evaluate("""() => {
        const c = readLiveOnsiteConfiguration();
        return Object.keys(c.provisions).find(k => !(c.provisions[k] === true || c.provisions[k] === 'true'));
    }""")
    assert page.evaluate("(n) => KAMPYLE_FUNC.checkProvision(n)", name) is False

    page.evaluate("""(n) => {
        const editor = document.getElementById('inspector-editor');
        const parsed = JSON.parse(editor.value);
        parsed[n] = true; parsed.__qa_probe__ = 'hello';
        editor.value = JSON.stringify(parsed, null, 2);
        editor.dispatchEvent(new Event('input'));
    }""", name)
    page.evaluate("() => previewInspectorJsonApply()")
    page.wait_for_timeout(300)

    diff = page.inner_text("#inspector-diff")
    assert name in diff and "__qa_probe__" in diff, diff[:200]

    page.evaluate("() => applyInspectorJson()")
    page.wait_for_timeout(400)
    assert page.evaluate("(n) => KAMPYLE_FUNC.checkProvision(n)", name) is True, \
        "the editor reported an apply the SDK never saw"
    assert page.evaluate("() => readLiveOnsiteConfiguration().provisions.__qa_probe__") == "hello"

    page.evaluate("() => restorePropertyConfiguration()")
    page.wait_for_timeout(400)
    assert page.evaluate("(n) => KAMPYLE_FUNC.checkProvision(n)", name) is False
    assert page.evaluate("() => readLiveOnsiteConfiguration().provisions.__qa_probe__ === undefined"), \
        "restore left a key the property never had"


def test_an_overridden_configuration_is_impossible_to_forget(page):
    """The real hazard is not data safety — it is debugging a state you created yourself. The
    banner is sticky and follows into every workspace until the property's values are back."""
    open_inspector(page, "provisions")
    assert page.eval_on_selector("#config-override-banner", "e => getComputedStyle(e).display") == "none"

    name = page.evaluate("""() => {
        const c = readLiveOnsiteConfiguration();
        return Object.keys(c.provisions).find(k => !(c.provisions[k] === true || c.provisions[k] === 'true'));
    }""")
    page.evaluate("(n) => setProvisionOverride(n, true)", name)
    page.wait_for_timeout(300)

    for workspace in ("targeting", "appearance", "traffic", "health"):
        page.evaluate("(w) => setActiveWorkspace(w)", workspace)
        page.wait_for_timeout(250)
        assert page.eval_on_selector("#config-override-banner", "e => getComputedStyle(e).display") != "none", \
            f"the override banner vanished in the {workspace} workspace"
    assert "not what the property ships" in page.inner_text("#config-override-banner")

    page.evaluate("() => restorePropertyConfiguration()")
    page.wait_for_timeout(300)
    assert page.eval_on_selector("#config-override-banner", "e => getComputedStyle(e).display") == "none"


def test_the_editor_is_not_wiped_by_the_inspectors_own_refresh(page):
    """Regression: the inspector re-reads the SDK every few seconds while it is on screen, and
    that refilled the editor from the live configuration — discarding an edit half-typed. Same
    class of bug as the parameter panel's drafts."""
    open_inspector(page, "provisions")
    page.evaluate("""() => {
        const editor = document.getElementById('inspector-editor');
        editor.value = '{"typed_by_hand": true}';
        editor.dispatchEvent(new Event('input'));
    }""")
    page.wait_for_timeout(3600)   # longer than the refresh interval
    assert "typed_by_hand" in page.eval_on_selector("#inspector-editor", "e => e.value"), \
        "the auto-refresh discarded the edit"


def test_no_configuration_ever_leaves_the_browser(page):
    """The claim the override feature rests on: the SDK fetches configuration with GET and has no
    write path, so an override is local to this tab and reaches neither Medallia nor anyone else.
    Asserted rather than assumed, because the whole feature is only safe if it holds."""
    writes = []
    page.on("request", lambda r: writes.append((r.method, r.url))
            if r.method != "GET" and "localhost" not in r.url else None)

    open_inspector(page, "provisions")
    name = page.evaluate("""() => {
        const c = readLiveOnsiteConfiguration();
        return Object.keys(c.provisions).find(k => !(c.provisions[k] === true || c.provisions[k] === 'true'));
    }""")
    page.evaluate("(n) => setProvisionOverride(n, true)", name)
    page.wait_for_timeout(2500)

    config_writes = [(m, u) for m, u in writes
                     if any(part in u for part in ("onsiteData", "formData", "provision", "domains-configuration"))]
    assert not config_writes, f"configuration was sent somewhere: {config_writes}"
    # analytics is the one thing that does leave, and it is a POST — so the check above is real
    assert all(m == "POST" and "api/web/events" in u for m, u in writes), \
        f"an unexpected non-GET request was made: {writes}"
    page.evaluate("() => restorePropertyConfiguration()")


def test_every_workspace_is_reachable_from_the_command_palette(page):
    """Regression: the palette's workspace entries were hand-written, so adding the Inspect
    workspace shipped it unreachable by ⌘K — the list said five while the app had six, and
    nothing compared them. They are derived from WORKSPACE_DEFINITIONS now; this is the check
    that says so, because the next workspace will be added by someone who has not read that
    commit."""
    entries = page.evaluate("""() => buildCommandPaletteActions()
        .filter(a => a.label.includes('workspace'))
        .map(a => ({label: a.label, hint: a.hint}))""")
    definitions = page.evaluate("() => WORKSPACE_DEFINITIONS.map(w => ({key: w.key, label: w.label}))")

    assert len(entries) == len(definitions), \
        f"{len(definitions)} workspaces but {len(entries)} palette entries: {entries}"
    # the number key and the palette hint have to agree, or the hint teaches the wrong shortcut
    assert [e["hint"] for e in entries] == [str(i + 1) for i in range(len(definitions))]

    for definition in definitions:
        name = definition["label"].split(" ", 1)[-1]     # drop the emoji
        assert any(name in e["label"] for e in entries), \
            f"no palette entry for the {name} workspace: {entries}"

    # and the entry must actually switch, not merely exist
    page.evaluate("""() => buildCommandPaletteActions()
        .find(a => a.label.includes('Inspect')).run()""")
    page.wait_for_timeout(300)
    assert page.evaluate("() => activeWorkspaceKey") == "inspect"


def test_the_palette_hint_counts_the_workspaces_that_exist(page):
    """The button's tooltip advertised "workspaces 1-5" after a sixth was added. It is written
    from the definitions at load rather than typed into the markup."""
    title = page.eval_on_selector("#command-palette-btn", "e => e.title")
    count = page.evaluate("() => WORKSPACE_DEFINITIONS.length")
    assert f"1-{count}" in title, f"tooltip says {title!r} for {count} workspaces"


def test_the_number_key_range_follows_the_workspace_list(page):
    """The handler was hardcoded to '1'-'5', then to '1'-'6'. It reads the list's length now, so
    the last workspace is always reachable by its number."""
    count = page.evaluate("() => WORKSPACE_DEFINITIONS.length")
    last_key = page.evaluate("() => WORKSPACE_DEFINITIONS[WORKSPACE_DEFINITIONS.length - 1].key")
    page.evaluate("() => setActiveWorkspace('targeting')")
    page.keyboard.press(str(count))
    page.wait_for_timeout(300)
    assert page.evaluate("() => activeWorkspaceKey") == last_key


# --------------------------------------------------------------------------------------------
# Splitting Activity.
#
# Activity was twelve panels — the most crowded workspace by a wide margin, and measurably about
# ten screens of scroll before the rail landed. It was also two jobs under one label: watching
# the stream go past (Traffic) versus asking whether anything in it is wrong (Health). These
# tests hold the split in place and cover the two lists that had to be edited by hand to make it.
# --------------------------------------------------------------------------------------------


def test_neither_half_of_the_split_activity_is_crowded_again(page):
    """The point of the split was the panel count, so that is what is asserted. Eight leaves
    room to add a panel to either half without a test edit, and still catches a slide back
    towards the twelve that made Activity unusable."""
    counts = page.evaluate("""() => {
        const o = {};
        WORKSPACE_DEFINITIONS.filter(w => w.key !== 'all').forEach(w => {
            o[w.key] = document.querySelectorAll(`#master-canvas-wrapper > [data-workspace="${w.key}"]`).length;
        });
        return o;
    }""")
    assert "activity" not in counts, "the split did not happen"
    assert counts["traffic"] and counts["health"], f"a half of the split is empty: {counts}"
    crowded = {k: v for k, v in counts.items() if v > 8}
    assert not crowded, f"a workspace is back to being a scroll: {crowded} (all: {counts})"


def test_the_rail_groups_every_workspace_the_app_defines(page):
    """RAIL_WORKSPACE_ORDER is the last hand-kept copy of the workspace list — it stays separate
    because the rail orders and labels them differently and omits 'all'. Splitting Activity meant
    editing it by hand, which is exactly how the palette's copy went stale. A workspace missing
    here has its panels indexed nowhere: the rail renders only the groups it names."""
    order = page.evaluate("() => RAIL_WORKSPACE_ORDER")
    labels = page.evaluate("() => RAIL_WORKSPACE_LABELS")
    keys = page.evaluate("() => WORKSPACE_DEFINITIONS.filter(w => w.key !== 'all').map(w => w.key)")

    assert sorted(order) == sorted(keys), f"the rail groups {order} for workspaces {keys}"
    assert all(labels.get(key) for key in order), f"a rail group has no label: {labels}"

    # and every panel actually reaches a group, rather than being silently dropped
    indexed = page.evaluate("() => collectRailEntries().length")
    rendered = page.eval_on_selector_all(".rail-item", "els => els.length")
    assert indexed == rendered, f"{indexed} panels indexed but {rendered} shown in the rail"


def test_a_link_to_the_retired_activity_workspace_still_opens_its_panels(page):
    """Repro permalinks carry the workspace as ?qa_view=, so links made before the split name a
    workspace that no longer exists. An unknown key falls back to Targeting, which would drop
    someone somewhere unrelated to what they were sent; the retired key resolves to the half
    that kept the event panels instead."""
    page.goto(BASE + "?qa_view=activity", wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelectorAll('#workspace-bar .workspace-tab').length > 0",
                           timeout=15000)

    assert page.evaluate("() => activeWorkspaceKey") == "traffic", "an old permalink lost its workspace"
    assert page.evaluate(
        "() => !!document.querySelector('[data-rail-id=\"live-analytics-events-tracking\"]').offsetParent"), \
        "the panels the link was pointing at are not on screen"
    # the restore deliberately does not rewrite the address bar (that would erase a deep-linked
    # route before it is read), so the retired key is still in the URL here -- the next workspace
    # switch is what normalises it
    page.evaluate("() => setActiveWorkspace('health')")
    page.wait_for_timeout(300)
    assert "qa_view=health" in page.evaluate("() => location.search")


# --------------------------------------------------------------------------------------------
# Folding panels that have nothing to report.
#
# Most panels most of the time have found nothing, and each one still cost a screen of scroll.
# The risk in doing this automatically is not the folding, it is the three ways it could go
# wrong: folding a check that has not finished (so "found nothing" means "looked at nothing"),
# leaving a panel folded after a finding appears in it, and fighting the reader's own button.
# One test each.
# --------------------------------------------------------------------------------------------


def _fold_state(pg):
    return pg.evaluate("""() => {
        const snapshot = buildGlobalStatusSnapshot();
        const o = {};
        Object.keys(PANEL_FOLD_STATE).forEach(k => {
            const v = PANEL_FOLD_STATE[k](snapshot);
            o[k] = {collapsed: !!collapsedPanelState[k], auto: !!panelFoldedAutomatically[k],
                    quiet: v ? v.quiet : None_};
        });
        return o;
    }""".replace("None_", "null"))


def test_a_panel_with_nothing_to_report_folds_and_one_with_findings_does_not(page):
    """The whole point, and its inverse in the same assertion: a panel is folded because its
    check ran and found nothing, never because it is quiet-looking."""
    inject(page)
    page.wait_for_timeout(6000)     # the fold pass runs on the 1800ms render tick
    state = _fold_state(page)

    folded = {k for k, v in state.items() if v["collapsed"]}
    assert folded, f"nothing folded at all on a clean-ish property: {state}"

    for key, panel in state.items():
        if panel["quiet"] is None:
            assert not panel["auto"], f"{key} was folded before its check produced anything"
        elif panel["quiet"] is False:
            assert not panel["collapsed"] or not panel["auto"], \
                f"{key} has findings but was folded away: {panel}"

    # targeting is deliberately never folded — it is the panel the tester came for
    assert not state["targeting"]["collapsed"], "the targeting matrix folded itself"


def test_a_folded_panel_reopens_when_it_starts_reporting_something(page):
    """The failure that would make this feature worse than not having it. Driven through the
    real pass with a doctored snapshot rather than by waiting for a genuine mismatch, because
    the branch has to hold for every panel, not just whichever one happens to fail today."""
    inject(page)
    page.wait_for_timeout(6000)
    assert page.evaluate("() => !!collapsedPanelState.design && !!panelFoldedAutomatically.design"), \
        "design did not fold, so there is nothing to reopen"

    page.evaluate("""() => foldQuietPanels(
        Object.assign(buildGlobalStatusSnapshot(), {designMismatches: 3, designChecked: 11}))""")

    assert not page.evaluate("() => collapsedPanelState.design"), "a new finding stayed folded away"
    assert not page.eval_on_selector_all('[data-panel-key="design"] [data-panel-body]',
                                         "els => els.some(e => e.classList.contains('panel-body-collapsed'))"), \
        "the panel reopened in name only — its body is still hidden"

    # and folded again with findings present, the one line it shows says so rather than
    # repeating the stale all-clear it was folded with
    page.evaluate("""() => setPanelCollapsed('design', true,
        Object.assign(buildGlobalStatusSnapshot(), {designMismatches: 3, designChecked: 11}))""")
    assert "3 mismatch" in page.inner_text('[data-panel-key="design"] [data-panel-summary]')


def test_the_fold_never_argues_with_the_collapse_button(page):
    """Expanding a folded panel by hand has to stick. The pass runs every 1.8s, so a fold that
    ignored the button would close the panel again while it was being read."""
    inject(page)
    page.wait_for_timeout(6000)
    quiet = page.evaluate("""() => Object.keys(PANEL_FOLD_STATE)
        .find(k => collapsedPanelState[k] && panelFoldedAutomatically[k])""")
    assert quiet, "no panel folded itself, so there is nothing to argue with"

    page.evaluate("(k) => togglePanelCollapsed(k)", quiet)      # the reader expands it
    page.wait_for_timeout(5000)                                  # two full passes
    assert not page.evaluate("(k) => collapsedPanelState[k]", quiet), \
        f"{quiet} was folded again after being opened by hand"

    # and the reverse: a panel the reader collapsed stays collapsed even when it has findings
    page.evaluate("() => togglePanelCollapsed('diagnostics')")
    page.evaluate("""() => foldQuietPanels(
        Object.assign(buildGlobalStatusSnapshot(), {diagnosticFindings: 4, blockingDiagnostics: 2}))""")
    assert page.evaluate("() => collapsedPanelState.diagnostics"), \
        "a panel the reader collapsed was reopened over their head"


def test_folding_a_panel_above_the_viewport_does_not_move_the_page(page):
    """Folding something the reader has already scrolled past pulls everything below it upwards.
    The pass adds the lost height back to the scroll position; without that, text jumps under
    the eyes of someone reading further down the page."""
    inject(page)
    page.evaluate("setActiveWorkspace('all')")
    page.wait_for_timeout(6000)

    key = page.evaluate("""() => Object.keys(PANEL_FOLD_STATE)
        .find(k => collapsedPanelState[k] && panelFoldedAutomatically[k])""")
    assert key, "no folded panel to re-fold"

    # put it back, scroll well past it, and note where a landmark below it sits on screen
    page.evaluate("(k) => { panelFoldedAutomatically[k] = false; setPanelCollapsed(k, false); }", key)
    page.wait_for_timeout(400)
    before = page.evaluate("""(k) => {
        const panel = document.querySelector(`[data-panel-key="${k}"]`);
        window.scrollTo(0, panel.getBoundingClientRect().bottom + window.scrollY + 400);
        return null;
    }""", key)
    page.wait_for_timeout(300)
    landmark = page.evaluate("""(k) => {
        const panel = document.querySelector(`[data-panel-key="${k}"]`);
        if (panel.getBoundingClientRect().bottom > 0) return null;   // must be fully above
        const below = document.querySelector('footer') || document.body.lastElementChild;
        return {top: Math.round(below.getBoundingClientRect().top)};
    }""", key)
    assert landmark, "could not get the panel above the viewport"

    page.evaluate("() => foldQuietPanels()")
    page.wait_for_timeout(200)
    after = page.evaluate("""() => {
        const below = document.querySelector('footer') || document.body.lastElementChild;
        return Math.round(below.getBoundingClientRect().top);
    }""")
    assert page.evaluate("(k) => collapsedPanelState[k]", key), "the panel did not fold"
    assert abs(after - landmark["top"]) <= 4, \
        f"the page moved {after - landmark['top']}px under the reader"
