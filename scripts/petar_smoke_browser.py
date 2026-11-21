"""Chromium adapter for the pinned plugin; all mutations go through UI events."""

from contextlib import contextmanager
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

from petar_smoke import ACTIONS, BROWSER, CHROMIUM, PLAYWRIGHT, Failure, check, ops, valid_id


WIKI_PAGE = "/doku.php?id=petar_check"
AJAX = "/lib/exe/ajax.php"
LIMIT = 18_000  # The pinned plugin aborts AJAX at 15 seconds.


@contextmanager
def browser_step(step):
    from playwright.sync_api import Error, TimeoutError
    try:
        yield
    except TimeoutError:
        raise Failure(f"browser timed out during {step}") from None
    except Error as error:
        categories = ("ERR_ABORTED", "ERR_BLOCKED_BY_CLIENT", "ERR_CONNECTION_REFUSED",
                      "ERR_EMPTY_RESPONSE", "ERR_TUNNEL_CONNECTION_FAILED")
        reason = next((kind for kind in categories if f"net::{kind}" in error.message), "navigation or UI error")
        raise Failure(f"browser could not complete {step} ({reason})") from None


@contextmanager
def chromium(op):
    check(Path("/etc/fonts/fonts.conf").is_file(), "Chromium requires the system Fontconfig configuration")
    ops.trusted_dir(BROWSER.parent)
    ops.canonical(BROWSER)
    check(BROWSER.is_file() and BROWSER.stat().st_uid == 0 and not BROWSER.stat().st_mode & 0o022,
          "expected root-installed Chromium")
    result = op.cmd([BROWSER, "--version"], timeout=8)
    check(result.stdout.strip().split()[-1:] == [CHROMIUM.encode()], "unexpected Chromium version")
    check(version("playwright") == PLAYWRIGHT, "unexpected Playwright version")
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="petar-smoke-", dir="/dev/shm") as scratch:
        os.environ.update(HOME=scratch, TMPDIR=scratch, XDG_CACHE_HOME=scratch, XDG_CONFIG_HOME=scratch)
        with sync_playwright() as manager:
            browser = manager.chromium.launch(executable_path=str(BROWSER), chromium_sandbox=True,
                                             timeout=20_000, args=[
                                                 "--proxy-server=http://127.0.0.1:1",
                                                 "--proxy-bypass-list=<-loopback>;127.0.0.1:" +
                                                 op.settings["WIKI_PORT"],
                                             ])
            try:
                check(browser.version == CHROMIUM, "unexpected running Chromium version")
                ops.say(f"{op.profile}: browser prerequisite: Chromium {CHROMIUM}, sandbox enabled")
                yield Wiki(browser, op)
            finally:
                browser.close()


class Wiki:
    def __init__(self, browser, op):
        self.browser, self.op = browser, op

    @contextmanager
    def session(self):
        origin = f"http://127.0.0.1:{self.op.settings['WIKI_PORT']}"
        with self.browser.new_context(service_workers="block", accept_downloads=False) as context:
            page = context.new_page()
            page.set_default_timeout(LIMIT)
            page.set_default_navigation_timeout(LIMIT)
            session = Session(page, origin)
            session.login(self.op.local["WIKI_CHECK_PASSWORD"])
            yield session
            check(session.requests == session.expected_submits, "unexpected browser submit request count")


class Session:
    def __init__(self, page, origin):
        self.page, self.origin = page, origin
        self.widget = page.locator(".plugin_petar")
        self.form = self.widget.locator("form")
        self.log = page.locator("[data-petar-jobs]")
        self.table = self.log.locator(".petar-jobs-table")
        self.card = self.log.locator(".petar-job-card")
        self.requests = self.expected_submits = 0
        page.on("request", self.on_request)

    def query(self, url):
        if url.startswith(self.origin + "/"):
            parsed = urlsplit(url)
            if parsed.path == AJAX:
                query = parse_qs(parsed.query)
                if query.get("call") == ["plugin_petar"]:
                    return query
        return {}

    def on_request(self, request):
        if self.query(request.url).get("action") == ["submit"]:
            self.requests += 1

    def login(self, password):
        with browser_step("login page navigation"):
            response = self.page.goto(self.origin + WIKI_PAGE + "&do=login", wait_until="commit")
            check(response.status == 200, f"wiki login page HTTP {response.status}")
        with browser_step("login form discovery"):
            login = self.page.locator("form#dw__login")
            login.wait_for()
            check(login.evaluate("(e) => e.method === 'post' && new URL(e.action).origin === location.origin")
                  and self.page.url.startswith(self.origin + "/"), "unexpected login form target")
        with browser_step("login credential entry"):
            login.locator("[name=u]").fill("petar_check")
            login.locator("[name=p]").fill(password)
        with browser_step("login form submit"):
            with self.page.expect_response(lambda r: r.url.startswith(self.origin + "/doku.php")
                                           and r.request.method == "POST") as pending:
                login.locator("[type=submit]").click()
            check(pending.value.status == 302, "wiki login did not redirect")
        with browser_step("authenticated page and plugin discovery"):
            self.page.goto(self.origin + WIKI_PAGE, wait_until="commit")
            self.form.wait_for()
            self.log.wait_for()
        with browser_step("wiki journal and action catalog initialization"):
            self.log.locator('.petar-jobs-table[aria-busy="false"]').wait_for(state="attached")
            self.page.locator('.plugin_petar[data-state="ready"]').wait_for()
            options = self.form.locator("select[name=tool] option").all_text_contents()
        names = [re.search(r"\(([^ ]+) v1\)$", opt) for opt in options]
        check(all(names) and sorted(m.group(1) for m in names) == list(ACTIONS), "unexpected wiki action catalog")

    def click(self, locator, action, *, method="GET", timeout=LIMIT, job_id=None):
        def matches(response):
            query = self.query(response.url)
            return (query.get("action") == [action] and response.request.method == method
                    and (job_id is None or query.get("job_id") == [job_id]))

        with self.page.expect_response(matches, timeout=timeout) as pending:
            locator.click(timeout=timeout)
        return pending.value

    def submit(self, job):
        option = self.form.locator("select[name=tool] option").filter(has_text=re.compile(rf"\({job.action} v1\)$"))
        self.form.locator("select[name=tool]").select_option(value=option.get_attribute("value"))
        data = {"message": job.value} if job.action == "echo-test" else {"text": "один два три"}
        self.form.locator("textarea[name=message]").fill(job.value if job.action == "echo-test"
                                                       else json.dumps(data, ensure_ascii=False))
        self.expected_submits += 1
        response = self.click(self.form.locator('button[type="submit"]'), "submit", method="POST")
        check(response.status == 201, f"submit HTTP {response.status}; expected 201")
        created = response.json()
        for key in ("input_id", "process_id"):
            value = created.get(key)
            if valid_id(value):
                setattr(job, key, value)
        check(job.input_id and job.process_id and created.get("status") == "new", "invalid creation response")
        sent = parse_qs(response.request.post_data or "")
        check(sent.get("action_name") == [job.action] and sent.get("version") == ["1"]
              and json.loads(sent["data"][0]) == data, "unexpected form submission")
        self.widget.locator(f'.petar-result[data-input-id="{job.input_id}"]'
                            f'[data-process-id="{job.process_id}"]').wait_for()
        check(self.requests == self.expected_submits, "unexpected browser submit request count")

    def journal(self, deadline, jobs):
        wanted = [job.process_id for job in jobs]
        button = self.log.locator(".petar-jobs-refresh")
        more = self.log.locator(".petar-jobs-more")
        for _ in range(25):  # Disallow partial scans masquerading as unique matches.
            timeout = min(LIMIT, max(1, int((deadline - time.monotonic()) * 1000)))
            check(timeout > 1, "timed out reading the smoke journal")
            response = self.click(button, "jobs", timeout=timeout)
            check(response.status == 200, f"wiki journal HTTP {response.status}")
            self.log.locator('.petar-jobs-table[aria-busy="false"]').wait_for(state="attached", timeout=timeout)
            page = response.json()
            check(isinstance(page.get("jobs"), list) and "next_cursor" in page, "invalid journal response")
            for row in page["jobs"]:
                if row.get("process_id") in wanted and row.get("status") == "failed":
                    # Let the controller correlate and report the failed row immediately.
                    return self.own_rows(wanted)
            if page["next_cursor"] is None:
                check(not more.is_visible(), "journal pagination mismatch")
                return self.own_rows(wanted)
            check(more.is_visible(), "journal pagination unavailable")
            button = more
        check(False, "journal exceeds the bounded scan")

    def own_rows(self, ids):
        # No contents of another user's/process's row leave the browser.
        return self.table.locator("tbody tr").evaluate_all("""
            (rows, ids) => rows.filter(r => ids.includes(r.cells[1]?.textContent))
                               .map(r => Array.from(r.cells).slice(0, 4).map(c => c.textContent))
        """, ids)

    def publication(self, job):
        row = self.table.locator("tbody tr").filter(has=self.page.get_by_role("cell", name=job.job_id, exact=True))
        response = self.click(row.locator("button"), "job_result", job_id=job.job_id)
        check(response.status == 200, f"wiki job_result HTTP {response.status}")
        self.card.wait_for()
        for key, expected in (("job_id", job.job_id), ("process_id", job.process_id),
                              ("tool", job.action), ("status", "Completed")):
            check(self.card.locator(f'[data-job-field="{key}"]').text_content() == expected,
                  "result card identity mismatch")
        if job.action == "echo-test":
            special = self.card.locator(".petar-job-special")
            special.wait_for()
            check(special.text_content() == job.value, "incorrect rendered echo")
            self.card.locator(".petar-job-json-toggle").click()
        shown = self.card.locator(".petar-job-public")
        shown.wait_for()
        rendered = json.loads(shown.text_content())
        payload = response.json()
        check(payload.get("job_id") == job.job_id and payload.get("result") == rendered,
              "rendered card differs from wiki publication")
        self.card.locator(".petar-job-close").click()
        return rendered
