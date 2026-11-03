#!/usr/bin/env python3
"""Obtain refreshable Yandex credentials without exposing secrets in argv."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def exchange_code(client_id: str, client_secret: str, code: str) -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", client_id):
        raise ValueError("Invalid client ID")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,4096}", client_secret):
        raise ValueError("Invalid client secret")
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,1024}", code):
        raise ValueError("Invalid authorization code")
    body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                  "client_id": client_id, "client_secret": client_secret}).encode()
    request = urllib.request.Request("https://oauth.yandex.ru/token", data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"},
                                     method="POST")
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise ValueError("Invalid OAuth response")
        data = json.loads(raw)
    if not isinstance(data, dict) or "error" in data:
        raise ValueError("Invalid OAuth response")
    access, refresh, expiry = data.get("access_token"), data.get("refresh_token"), data.get("expires_in")
    if not isinstance(access, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,4096}", access):
        raise ValueError("Missing access token")
    if not isinstance(refresh, str) or not re.fullmatch(r"[!-~]{20,4096}", refresh):
        raise ValueError("Missing refresh token")
    if type(expiry) is not int or not 1 <= expiry <= 10 * 366 * 86400:
        raise ValueError("Missing token lifetime")
    return {"YANDEX_ACCESS_TOKEN": access, "YANDEX_CLIENT_ID": client_id,
            "YANDEX_REFRESH_TOKEN": refresh, "YANDEX_CLIENT_SECRET": client_secret,
            "YANDEX_EXPIRES_IN": str(expiry)}


def write_private(path: Path, values: dict[str, str]) -> None:
    # Publish atomically without replacing an existing file or following symlinks.
    fd, name = tempfile.mkstemp(prefix=".yandex-oauth-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write("".join(key + "=" + value + "\n" for key, value in values.items()))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--output", type=Path, required=True, help="new private file; existing files are preserved")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", args.client_id):
        print(json.dumps({"ok": False, "error": "invalid_client_id"}))
        return 1
    if args.output.exists() or args.output.is_symlink():
        print(json.dumps({"ok": False, "error": "output_already_exists"}))
        return 1
    try:
        secret = getpass.getpass("Client secret (hidden): ").strip()
        print("Open in your browser: https://oauth.yandex.ru/authorize?" +
              urllib.parse.urlencode({"response_type": "code", "client_id": args.client_id,
                                     "redirect_uri": "https://oauth.yandex.ru/verification_code"}))
        code = getpass.getpass("Authorization code (hidden, valid for 10 minutes): ").strip()
        values = exchange_code(args.client_id, secret, code)
        write_private(args.output, values)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        print(json.dumps({"ok": False, "error": "oauth_exchange_failed", "http_status": status}))
        return 1
    except (ValueError, TypeError, OSError, EOFError, urllib.error.URLError):
        print(json.dumps({"ok": False, "error": "oauth_exchange_or_private_file_failed"}))
        return 1
    print(json.dumps({"ok": True, "private_token_file": str(args.output), "refresh_token_received": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
