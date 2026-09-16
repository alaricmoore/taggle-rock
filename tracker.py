"""
The signed connection to the tracker on the Pi: read notes, send tags, undo a run.

Every request is signed with api_signing.py (copied from private-track) using
Qwen's secret from config.json. The secret itself never goes over the wire;
see private-track notes/auth-hardening.md.
"""

import json
import urllib.error
import urllib.request

import api_signing

# Cloudflare refuses Python's default User-Agent with error 1010.
USER_AGENT = "taggle-rock/0.1"


class TrackerError(Exception):
    """The tracker couldn't be reached or refused a request. The message says which."""


class Tracker:
    def __init__(self, server: str, client_id: str, secret: str, user_id: int, timeout: int = 60,
                 skip_fields=()):
        self.server = server.rstrip("/")
        self.client_id = client_id
        self.secret = secret
        self.user_id = user_id
        self.timeout = timeout
        # Boxes never worth tagging: one with no subject of its own collects
        # whatever the model can reach. tag_run.py leaves them alone.
        self.skip_fields = tuple(skip_fields or ())

    @classmethod
    def from_config(cls, path: str = "config.json") -> "Tracker":
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError) as e:
            raise TrackerError(f"can't read {path}: {e}") from None
        missing = [k for k in ("server", "client_id", "secret", "user_id") if not cfg.get(k)]
        if missing:
            raise TrackerError(f"{path} is missing {missing}")
        skip = cfg.get("skip_fields") or ()
        if not (isinstance(skip, (list, tuple)) and all(isinstance(f, str) and f.strip() for f in skip)):
            raise TrackerError(f'{path}: "skip_fields" must be a list of box names, like ["notes"]')
        return cls(cfg["server"], cfg["client_id"], cfg["secret"], int(cfg["user_id"]),
                   skip_fields=skip)

    def _request(self, method: str, url: str, body: dict = None) -> dict:
        """`url` is the path and query, e.g. "/api/notes?user_id=1"."""
        raw = b"" if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = api_signing.signed_headers(self.client_id, self.secret, method, url, raw)
        headers["User-Agent"] = USER_AGENT
        if raw:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.server + url, data=raw or None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            raise TrackerError(f"{method} {url.split('?')[0]}: HTTP {e.code} {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TrackerError(f"could not reach {self.server}: {e}") from None
        except ValueError:
            raise TrackerError(f"{method} {url.split('?')[0]}: the reply was not JSON") from None

    def notes(self, since: str = None) -> list:
        """Every non-empty note: {date, field, text, sha256, tagged_sha256}."""
        url = f"/api/notes?user_id={self.user_id}" + (f"&since={since}" if since else "")
        return self._request("GET", url)["notes"]

    def post_tags(self, run_id: str, model: str, vocab_version: str, notes: list) -> dict:
        """Send a batch. Returns the tracker's {counts, results}."""
        return self._request("POST", "/api/tags", {
            "user_id": self.user_id, "run_id": run_id, "model": model,
            "vocab_version": vocab_version, "notes": notes})

    def undo(self, run_id: str) -> int:
        """Remove one of Qwen's runs. Returns how many notes it had tagged."""
        return self._request("POST", "/api/tags/undo",
                             {"user_id": self.user_id, "run_id": run_id})["notes_removed"]
