#!/usr/bin/env python3
"""Validate an owner-provided token and optionally update one private remote.

The token never enters argv, stdout or Git. No API write methods are used.
"""
from __future__ import annotations

import argparse
import configparser
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import urllib.error
import urllib.request


class ConfigurationError(Exception):
    pass


class AccessError(Exception):
    def __init__(self, status: int | None = None):
        super().__init__("Yandex read-only API validation failed")
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an OAuth header to a redirect destination.
        return None


def read_private(path: Path) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ConfigurationError("Private inputs must be regular owner-only files")
        if metadata.st_size > 65536:
            raise ConfigurationError("Private configuration exceeds size limit")
        return stream.read(65537), metadata


def read_token_file(path: Path) -> dict[str, str]:
    content, _ = read_private(path)
    allowed = {"YANDEX_ACCESS_TOKEN", "YANDEX_CLIENT_ID", "YANDEX_EXPIRES_IN"}
    values = {}
    for line in content.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError("Expected KEY=value in the private token file")
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if name not in allowed or name in values:
            raise ConfigurationError("Unexpected or duplicate token-file key")
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        values[name] = value
    token = values.get("YANDEX_ACCESS_TOKEN", "")
    client_id = values.get("YANDEX_CLIENT_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,4096}", token):
        raise ConfigurationError("Missing or invalid access token")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", client_id):
        raise ConfigurationError("Missing or invalid owner application client ID")
    expiry = values.get("YANDEX_EXPIRES_IN")
    if expiry is not None and (not expiry.isdigit() or not 1 <= int(expiry) <= 10 * 366 * 86400):
        raise ConfigurationError("Invalid expires_in from OAuth response")
    return values


def get_metadata(token: str, directory: bool = False) -> dict:
    opener = urllib.request.build_opener(NoRedirect())
    endpoint = "https://cloud-api.yandex.net/v1/disk"
    if directory:
        endpoint += "/resources?path=disk%3A%2F&limit=0"
    request = urllib.request.Request(endpoint, headers={"Authorization": "OAuth " + token})
    try:
        with opener.open(request, timeout=20) as response:
            if response.status != 200:
                raise AccessError(response.status)
            body = response.read(1048577)
            data = json.loads(body)
            if len(body) > 1048576 or not isinstance(data, dict) or "error" in data:
                raise AccessError()
            if directory:
                embedded = data.get("_embedded")
                if data.get("type") != "dir" or not isinstance(embedded, dict) or not isinstance(embedded.get("items"), list):
                    raise AccessError()
            elif type(data.get("total_space")) is not int or type(data.get("used_space")) is not int:
                raise AccessError()
            return data
    except urllib.error.HTTPError as error:
        # Do not include the response body, URL, headers or token in errors.
        status = error.code
        error.close()
        raise AccessError(status) from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise AccessError() from None


def get_account_uid(token: str) -> str:
    user = get_metadata(token).get("user")
    uid = user.get("uid") if isinstance(user, dict) else None
    # Disk's stable uid is a decimal string. Do not coerce numbers, booleans,
    # empty values or display names into an account identity.
    if not isinstance(uid, str) or not re.fullmatch(r"[0-9]{1,128}", uid):
        raise AccessError()
    return uid


def validate_access(token: str) -> str:
    uid = get_account_uid(token)
    get_metadata(token, directory=True)
    return uid


def update_configuration(config_path: Path, remote: str, values: dict[str, str], apply: bool) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", remote):
        raise ConfigurationError("Invalid remote name")
    original, metadata = read_private(config_path)
    configuration = configparser.RawConfigParser()
    configuration.read_string(original.decode("utf-8"))
    if remote not in configuration or configuration[remote].get("type") != "yandex":
        raise ConfigurationError("Selected remote must already be type=yandex")
    try:
        old_token = json.loads(configuration[remote].get("token", ""))
    except ValueError:
        raise ConfigurationError("Selected remote must contain a valid token") from None
    old_access = old_token.get("access_token") if isinstance(old_token, dict) else None
    if not isinstance(old_access, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", old_access):
        raise ConfigurationError("Selected remote must contain a valid access token")
    # The old account may still expose /disk identity while /resources is 410.
    # Never request its root; require the new token to refer to this same account
    # before creating even a backup or a replacement configuration file.
    old_uid = get_account_uid(old_access)
    new_uid = validate_access(values["YANDEX_ACCESS_TOKEN"])
    if new_uid != old_uid:
        raise AccessError()
    if not apply:
        return {"ok": True, "read_only_api_checks": 3, "configuration_changed": False}
    token = {"access_token": values["YANDEX_ACCESS_TOKEN"], "token_type": "OAuth"}
    if "YANDEX_EXPIRES_IN" in values:
        expires = datetime.now(timezone.utc) + timedelta(seconds=int(values["YANDEX_EXPIRES_IN"]))
        token["expiry"] = expires.isoformat().replace("+00:00", "Z")
    # A refresh token/secret belonging to the old application must not be reused.
    configuration[remote]["client_id"] = values["YANDEX_CLIENT_ID"]
    configuration[remote].pop("client_secret", None)
    configuration[remote]["token"] = json.dumps(token, separators=(",", ":"))
    buffer = io.StringIO()
    configuration.write(buffer)
    payload = buffer.getvalue().encode("utf-8")
    backup = None
    temporary = None
    try:
        # Save the old credential-bearing file privately before replacing it.
        with tempfile.NamedTemporaryFile(prefix=config_path.name + ".before-oauth-", dir=config_path.parent, delete=False) as stream:
            backup = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        with tempfile.NamedTemporaryFile(prefix=".oauth-update-", dir=config_path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Reject a concurrent edit rather than overwriting refreshed credentials.
        current, _ = read_private(config_path)
        if current != original:
            raise ConfigurationError("Configuration changed during validation; retry")
        os.replace(temporary, config_path)
        temporary = None
        directory_fd = os.open(config_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"ok": True, "read_only_api_checks": 3, "configuration_changed": True, "private_backup": str(backup)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--remote", default="yandex_main")
    parser.add_argument("--apply", action="store_true", help="replace configuration after read-only access and same-account checks")
    args = parser.parse_args(argv)
    try:
        result = update_configuration(args.config, args.remote, read_token_file(args.token_file), args.apply)
    except AccessError as error:
        print(json.dumps({"ok": False, "error": "read_only_api_access_failed", "http_status": error.status, "configuration_changed": False}))
        return 1
    except (ConfigurationError, OSError, UnicodeError, configparser.Error):
        print(json.dumps({"ok": False, "error": "private_configuration_invalid_or_unwritable"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
