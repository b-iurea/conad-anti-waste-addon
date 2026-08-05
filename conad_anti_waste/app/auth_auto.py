"""Self-healing Conad session.

`ensure_session()` is the only way the rest of the app gets a Conad session. It
returns an authenticated one, logging in by itself when the cookies have died —
which is what makes the app deployable to a cluster where nobody is around to
run a browser by hand.

Two things this has to get right:

**Never log in twice at once.** The server and the bot are separate containers
sharing one volume. Both may notice a dead session in the same second, and two
concurrent Chrome logins racing on the same profile directory would corrupt it.
A file lock on the volume serialises them; the loser re-checks the cookie file
afterwards and almost always finds the winner already fixed it.

**Never hammer the login.** Conad's protection flow is score-based, and a burst
of failed attempts is exactly what looks like an attack. After a failure the
next attempt is held off by a cooldown, and the caller is told to alert a human
instead of retrying forever.
"""

import fcntl
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from app.auth import ConadHttpSession
from app.config import get_settings
from app.conad_login import LoginError, login

log = logging.getLogger(__name__)

LOCK_NAME = "conad-login.lock"
_last_failure: Optional[datetime] = None


def _cooldown() -> timedelta:
    return timedelta(hours=get_settings().login_cooldown_hours)


class SessionUnavailable(RuntimeError):
    """No usable session and none could be obtained. Tell a human."""


@contextmanager
def _login_lock(timeout: float = 180.0) -> Iterator[bool]:
    """Cross-process lock. Yields True if we hold it, False if we timed out."""
    s = get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = s.data_dir / LOCK_NAME
    handle = lock_path.open("w")
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(1.0)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _session() -> ConadHttpSession:
    return ConadHttpSession(cookies_file=get_settings().conad_cookies_path)


def _in_cooldown() -> Optional[timedelta]:
    if _last_failure is None:
        return None
    cooldown = _cooldown()
    elapsed = datetime.now(timezone.utc) - _last_failure
    return (cooldown - elapsed) if elapsed < cooldown else None


def ensure_session(force_login: bool = False) -> ConadHttpSession:
    """An authenticated Conad session, re-logging in if necessary.

    Raises SessionUnavailable when it cannot get one — callers surface that to
    the user rather than importing nothing and looking healthy.
    """
    global _last_failure
    s = get_settings()

    if not force_login:
        session = _session()
        if session.is_authenticated():
            return session
        log.info("conad session invalid — attempting automatic login")

    if not s.can_auto_login:
        raise SessionUnavailable(
            "Conad session expired and automatic login is not configured. "
            "Set CONAD_EMAIL and CONAD_PASSWORD, or refresh the cookies by hand."
        )

    remaining = _in_cooldown()
    if remaining:
        raise SessionUnavailable(
            f"login failed recently; waiting {int(remaining.total_seconds() // 60)} more "
            f"minutes before retrying — rapid repeat attempts lower the captcha score "
            f"and make the next one likelier to fail too"
        )

    with _login_lock() as acquired:
        if not acquired:
            # Someone else is logging in. Their result is as good as ours.
            log.info("another process holds the login lock; using its result")
            session = _session()
            if session.is_authenticated():
                return session
            raise SessionUnavailable("a concurrent login was in progress and did not succeed")

        # Re-check inside the lock: we may have been queued behind the process
        # that already fixed it.
        if not force_login:
            session = _session()
            if session.is_authenticated():
                return session

        last_error: Optional[Exception] = None
        for attempt in range(1, s.login_max_attempts + 1):
            try:
                log.info("conad login attempt %d/%d", attempt, s.login_max_attempts)
                if login():
                    session = _session()
                    if session.is_authenticated():
                        _last_failure = None
                        log.info("conad login succeeded")
                        return session
                    last_error = LoginError("cookies written but the session is still invalid")
                else:
                    last_error = LoginError("login did not produce an auth cookie")
            except Exception as e:  # noqa: BLE001
                last_error = e
                log.warning("login attempt %d failed: %s", attempt, e)
            if attempt < s.login_max_attempts:
                time.sleep(5)

    _last_failure = datetime.now(timezone.utc)
    raise SessionUnavailable(f"automatic Conad login failed: {last_error}")


def session_status() -> dict:
    """Non-throwing snapshot for /api/health and the doctor command."""
    s = get_settings()
    cookies = s.conad_cookies_path
    status = {
        "cookies_present": cookies.exists(),
        "auto_login_configured": s.can_auto_login,
        "authenticated": False,
        "cookies_age_hours": None,
    }
    if cookies.exists():
        age = time.time() - cookies.stat().st_mtime
        status["cookies_age_hours"] = round(age / 3600, 1)
        try:
            status["authenticated"] = _session().is_authenticated()
        except Exception as e:  # noqa: BLE001
            status["error"] = str(e)
    remaining = _in_cooldown()
    if remaining:
        status["retry_in_minutes"] = int(remaining.total_seconds() // 60)
    return status
