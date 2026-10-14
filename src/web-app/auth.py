"""Shared-password gate: one password for everyone, remembered in a signed, HttpOnly cookie."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque

COOKIE = "vt_auth"
TTL_SECONDS = 12 * 3600
MAX_FAILURES = 5
FAILURE_WINDOW = 60.0


def _key(password: str) -> bytes:
    return hashlib.sha256(b"voicetester-v1|" + password.encode()).digest()


def password_matches(given: str, expected: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(expected.encode()).digest())


def make_token(password: str, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + TTL_SECONDS)
    sig = hmac.new(_key(password), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def token_valid(token: str | None, password: str, now: float | None = None) -> bool:
    """A token is only valid for the password it was signed with, so changing the password signs everyone out."""
    if not token or not password:
        return False
    expires, _, sig = token.partition(".")
    if not expires.isdigit() or not sig:
        return False
    if int(expires) < (now if now is not None else time.time()):
        return False
    expected = hmac.new(_key(password), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


class LoginThrottle:
    """Allows ``MAX_FAILURES`` wrong passwords per client within ``FAILURE_WINDOW`` seconds."""

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, client: str, now: float) -> deque[float]:
        q = self._failures[client]
        while q and now - q[0] > FAILURE_WINDOW:
            q.popleft()
        if not q:
            self._failures.pop(client, None)
            return deque()
        return q

    def blocked(self, client: str, now: float | None = None) -> bool:
        return len(self._prune(client, now if now is not None else time.monotonic())) >= MAX_FAILURES

    def record_failure(self, client: str, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self._prune(client, now)
        self._failures[client].append(now)

    def reset(self, client: str) -> None:
        self._failures.pop(client, None)


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Call center agent tester</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0f1419; color: #e6edf3;
         font: 15px system-ui, -apple-system, "Segoe UI", sans-serif; }
  form { width: min(360px, 90vw); background: #161c24; border: 1px solid #2a3441; border-radius: 12px; padding: 28px; }
  h1 { font-size: 18px; margin: 0 0 4px; } p { color: #8b98a8; margin: 0 0 18px; font-size: 13px; }
  input { width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px; border: 1px solid #2a3441;
          background: #1c2430; color: inherit; font: inherit; }
  button { width: 100%; margin-top: 12px; padding: 10px; border: 0; border-radius: 8px; background: #3b9eff; color: #04121f;
           font: inherit; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: .5; } #err { color: #f85149; min-height: 20px; margin-top: 10px; font-size: 13px; }
</style></head>
<body><form id="f">
  <h1>&#9742; Call center agent tester</h1><p>Enter the access password to continue.</p>
  <input id="pw" type="password" placeholder="Password" autocomplete="current-password" autofocus required>
  <button id="go">Enter</button><div id="err"></div>
</form>
<script>
document.getElementById("f").onsubmit = async (e) => {
  e.preventDefault();
  const go = document.getElementById("go"), err = document.getElementById("err");
  go.disabled = true; err.textContent = "";
  try {
    const r = await fetch("/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: document.getElementById("pw").value }) });
    if (r.ok) { location.href = "/"; return; }
    err.textContent = r.status === 429 ? "Too many attempts. Wait a minute and try again." : "Wrong password.";
  } catch (_) { err.textContent = "Could not reach the server."; }
  go.disabled = false;
};
</script></body></html>
"""
