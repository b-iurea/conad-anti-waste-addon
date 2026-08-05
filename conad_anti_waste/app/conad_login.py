"""Automated my.conad.it login.

The browser flow is carried over from the parent project's
`src/auth_interactive.py` — it is proven against the live site and the logic
here is deliberately unchanged. The only differences are that paths come from
config instead of `__file__`, and it runs unattended.

Why a browser at all
--------------------
Pure HTTP login is impossible. `POST /api/myconad/it-it.login.json` requires a
`protectionToken` that only the site's JavaScript can mint, and without it the
server answers:

    403  {"code": "WEB:INVALID_PROTECTION_TOKEN",
          "message": "Your protection token is not valid, Are you a bot?"}

(That is exactly what `src/auth.py::attempt_direct_login` demonstrates, and it
is why that function has never been usable as a login path.)

Why not headless
----------------
The protection flow is `protection.json?step=zero` (a client-declared `feBot`
check) followed by `step=one` (reCAPTCHA Enterprise, score-based and invisible).
Headless Chrome scores too low: both steps return `{"ok": false}` and no auth
cookie is ever issued. Measured, not assumed.

So the login runs a **real Chrome on a virtual X display**. Nothing is visible,
no human is involved, and to the anti-bot stack it is an ordinary browser. This
is what makes unattended login possible in a container.

The persistent profile matters: browsing history accumulates and improves the
captcha score on every run, so it lives on the data volume rather than in the
image.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from app.config import get_settings

log = logging.getLogger(__name__)

LOGIN_URL = "https://my.conad.it/login"

# A complete session carries all three. `ecAccess` alone is NOT enough: an
# expired session leaves it behind on its own, and treating that as "logged in"
# reports success for a session that fetches nothing. Measured against a live
# session (all three, HTTP 200) versus a dead one (ecAccess only, 302 -> /login).
AUTH_COOKIES = {"ssoTokenId", "ecUser", "ecAccess"}
REQUIRED_AUTH_COOKIE = "ssoTokenId"


class LoginError(RuntimeError):
    """Login could not be completed. Always surfaced, never swallowed."""


STEALTH_JS = """
// kill the most common automation signals
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['it-IT', 'it', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""

# step zero blocks on feBot==true; force false so the flow always reaches the
# invisible captcha step, which a real browser passes silently.
FETCH_REWRITE_JS = """
window.__protLog = [];
const _fetch = window.fetch.bind(window);
window.fetch = async (...a) => {
    const url = String(a[0]);
    if (url.includes('protection.json') && a[1] && typeof a[1].body === 'string') {
        try {
            const b = JSON.parse(a[1].body);
            if ('feBot' in b) b.feBot = false;
            a[1].body = JSON.stringify(b);
        } catch (e) {}
    }
    const res = await _fetch(...a);
    if (url.includes('protection.json')) {
        const text = await res.clone().text();
        window.__protLog.push({ step: url.split('=')[1], status: res.status,
                                body: text.slice(0, 200) });
    }
    return res;
};
"""


# --- virtual display -------------------------------------------------------

def _display_is_free(display: str) -> bool:
    return not Path(f"/tmp/.X{display.lstrip(':')}-lock").exists()


@contextmanager
def virtual_display() -> Iterator[Optional[str]]:
    """Run the block with a usable DISPLAY, starting Xvfb if there isn't one.

    Doing this in-process rather than via an `xvfb-run` wrapper means the same
    code path works from the API server, the bot, the CLI and a Kubernetes Job
    with no entrypoint gymnastics.
    """
    if os.environ.get("DISPLAY"):
        yield os.environ["DISPLAY"]  # a real desktop (or an outer xvfb-run)
        return

    if not shutil.which("Xvfb"):
        raise LoginError(
            "no DISPLAY and Xvfb is not installed. The Conad login needs a real "
            "browser on a virtual display — headless is rejected by their bot "
            "protection. Install xvfb (the container image already has it)."
        )

    s = get_settings()
    display = s.xvfb_display
    if not _display_is_free(display):
        for n in range(99, 130):
            if _display_is_free(f":{n}"):
                display = f":{n}"
                break

    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", s.xvfb_screen, "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    previous = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = display
    try:
        for _ in range(50):  # wait for the server to come up (max ~5s)
            if not _display_is_free(display):
                break
            if proc.poll() is not None:
                raise LoginError(f"Xvfb exited immediately (code {proc.returncode})")
            time.sleep(0.1)
        log.info("virtual display %s ready", display)
        yield display
    finally:
        if previous is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = previous
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _pick_channel() -> Optional[str]:
    """Prefer real Google Chrome (best captcha score), else bundled Chromium."""
    if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        return "chrome"
    return None


# --- the login flow --------------------------------------------------------

ORDERS_URL = "https://my.conad.it/i-miei-ordini"


async def _session_really_works(page) -> bool:
    """Does the current browser session actually reach a protected page?

    The presence of an auth cookie proves nothing — expired ones linger in the
    profile and look identical. Loading the orders page and checking we were not
    bounced to /login is the only honest test.
    """
    try:
        await page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
        return "/login" not in page.url
    except Exception:  # noqa: BLE001
        return False


async def _has_auth_cookie(context) -> bool:
    names = {c.get("name") for c in await context.cookies()}
    return REQUIRED_AUTH_COOKIE in names


async def _save_cookies(context, path: Path) -> int:
    cookies = {c["name"]: c["value"] for c in await context.cookies()
               if c.get("name") and c.get("value")}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic: a reader never sees a half-written file
    log.info("saved %d cookies to %s", len(cookies), path)
    return len(cookies)


async def login_once(email: str, password: str, cookies_file: Path,
                     profile_dir: Path, headless: bool = False) -> bool:
    """Log in and write cookies. Returns True only if auth cookies were issued."""
    from playwright.async_api import async_playwright

    cookies_file.parent.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    channel = _pick_channel()
    log.info("login: %s (headless=%s)", "Google Chrome" if channel else "Chromium", headless)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel=channel,
            headless=headless,
            locale="it-IT",
            timezone_id="Europe/Rome",
            viewport={"width": 1440, "height": 900},
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        await context.add_init_script(STEALTH_JS)
        await context.add_init_script(FETCH_REWRITE_JS)

        page = await context.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)  # let protection scripts settle

            # No login form usually means the profile is still signed in — but
            # "usually" is not good enough. A persistent profile often carries
            # EXPIRED auth cookies, which still suppress the form while being
            # useless for the API. Assuming success there writes a dead session
            # to disk and the app looks authenticated when it is not.
            # So verify against a protected page, and fall back to a real login.
            try:
                await page.wait_for_selector('input[name="email"]', timeout=8000)
            except Exception:
                if await _session_really_works(page):
                    log.info("profile session still valid — no login needed")
                    await _save_cookies(context, cookies_file)
                    return await _has_auth_cookie(context)
                log.info("profile carried a stale session — clearing it and logging in")
                await context.clear_cookies()
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(3000)
                await page.wait_for_selector('input[name="email"]', timeout=15000)

            try:  # OneTrust cookie banner
                accept = await page.query_selector("#onetrust-accept-btn-handler")
                if accept and await accept.is_visible():
                    await accept.click()
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

            email_input = await page.query_selector('input[name="email"]')
            pass_input = await page.query_selector('input[name="password"]')
            if email_input is None or pass_input is None:
                raise LoginError("login form not found — the page markup changed")
            await email_input.fill(email)
            await pass_input.fill(password)

            # Warm the protection token so the captcha is solved before submit.
            try:
                await page.evaluate(
                    "async () => { try { return await window.gpGetProtectionToken('login'); }"
                    "catch (e) { return 'FLOW_ERROR: ' + e; } }"
                )
            except Exception as e:  # noqa: BLE001
                log.debug("protection flow note: %s", e)

            submit = (await page.query_selector(".mw1-login__submit")
                      or await page.query_selector('button[type="submit"]'))
            if submit:
                await submit.click()
            else:
                await pass_input.press("Enter")

            for _ in range(30):
                await page.wait_for_timeout(1000)
                if "/login" not in page.url and "my.conad.it" in page.url:
                    break
                err = await page.query_selector(".mw1-login__error")
                if err and (await err.inner_text()).strip():
                    raise LoginError(f"login rejected: {(await err.inner_text()).strip()}")

            # Auth cookies land a moment after the dashboard renders; saving
            # before that exports a session that is not yet valid.
            for _ in range(10):
                if await _has_auth_cookie(context):
                    break
                await page.wait_for_timeout(1000)

            if not await _has_auth_cookie(context):
                prot = await page.evaluate("() => window.__protLog || []")
                detail = "; ".join(f"{e['step']}:{e['body'][:60]}" for e in prot)
                raise LoginError(
                    "no auth cookie issued — the bot check refused the session. "
                    f"protection steps: {detail or 'none'}"
                )
            # Prove the session works before writing it: cookies that exist but
            # do not authenticate are worse than none, because everything
            # downstream then reports a healthy session that fetches nothing.
            if not await _session_really_works(page):
                raise LoginError("logged in but the orders page still redirects to /login")

            await _save_cookies(context, cookies_file)
            return True
        finally:
            await context.close()


def login(cookies_file: Optional[Path] = None) -> bool:
    """Synchronous entry point: virtual display + browser login."""
    s = get_settings()
    if not s.can_auto_login:
        raise LoginError("CONAD_EMAIL / CONAD_PASSWORD are not set")

    cookies_file = cookies_file or s.conad_cookies_path
    with virtual_display():
        return asyncio.run(login_once(
            s.conad_email, s.conad_password, cookies_file, s.profile_dir, s.login_headless
        ))
