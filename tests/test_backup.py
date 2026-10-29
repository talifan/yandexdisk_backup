"""Fault injection against the actual shell entrypoint; no real remote or data.

Set YDB_SHELL_TEST_IMAGE to the pinned runtime image to repeat shell cases in
BusyBox with --network none. Python helper cases always mock the HTTP boundary.
"""
import configparser
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ydb-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("backup", "versions", "state", "bin", "outside"):
            (self.root / name).mkdir()
        self.calls = self.root / "calls"
        self.env = {
            "PATH": str(self.root / "bin") + ":/usr/bin:/bin",
            "TEST_ROOT": str(self.root), "CALLS_LOG": str(self.calls),
            "BACKUP_DIR": str(self.root / "backup"), "VERSIONS_DIR": str(self.root / "versions"),
            "STATE_DIR": str(self.root / "state"), "REMOTE_NAME": "test_remote", "ACCOUNT_NAME": "test",
            "RETENTION_DAYS": "1", "PREFLIGHT_TIMEOUT_SECONDS": "2", "MAX_RUN_SECONDS": "2",
            "STOP_KILL_SECONDS": "1", "RETRY_INITIAL_SECONDS": "1", "RETRY_MAX_SECONDS": "4",
        }
        self.command("mountpoint", '[ "${FAKE_UNMOUNTED:-0}" != 1 ]')
        self.command("stat", '[ "${FAKE_STAT_FAIL:-0}" != 1 ] || exit 1; echo "${FAKE_FS_MAGIC:-6969}"')
        self.command("find", '''
if [ "${FAKE_FIND_FAIL:-0}" = 1 ]; then
  echo "$VERSIONS_DIR/20200101T000000Z"; exit 1
fi
if [ "${FAKE_UNEXPECTED_PATH:-0}" = 1 ]; then
  echo "$TEST_ROOT/outside/20200101T000000Z"; exit 0
fi
exec /usr/bin/find "$@"
''')
        self.command("rm", '''
printf 'rm %s\\n' "$*" >> "$CALLS_LOG"
for arg in "$@"; do
  case "$arg" in -*) continue ;; "$TEST_ROOT"/*) ;; *) exit 88 ;; esac
  case "$arg" in */"${FAKE_RM_FAIL_BASENAME:-__none__}") exit 1 ;; esac
done
exec /bin/rm "$@"
''')
        self.command("rclone", '''
printf 'rclone %s\\n' "$*" >> "$CALLS_LOG"
case "$1:${FAKE_LIST_MODE:-ok}" in
  lsf:gone) echo 'error listing: [410 - GoneError] Gone' >&2; exit 1 ;;
  lsf:denied) echo 'error listing: [403 - Forbidden] Denied' >&2; exit 1 ;;
  lsf:slow) sleep 30; exit 0 ;;
esac
if [ "$1" = sync ]; then
  case "${FAKE_SYNC_MODE:-ok}" in
    fail) echo 'synthetic sync failure' >&2; exit 1 ;;
    slow) echo $$ > "$TEST_ROOT/active-child"; trap 'exit 130' INT TERM; sleep 30 ;;
  esac
fi
exit 0
''')

    def command(self, name, body):
        file = self.root / "bin" / name
        file.write_text("#!/bin/sh\nset -u\n" + body + "\n")
        file.chmod(0o700)

    def command_line(self, script="run-loop.sh", arguments=("--once",)):
        image = os.environ.get("YDB_SHELL_TEST_IMAGE")
        if not image:
            return ["/bin/sh", str(ROOT / "scripts" / script), *arguments], self.env
        command = ["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
                   "--mount", f"type=bind,source={self.root},target={self.root}",
                   "--mount", f"type=bind,source={ROOT / 'scripts'},target={ROOT / 'scripts'},readonly"]
        for key, value in self.env.items():
            command += ["--env", key + "=" + value]
        command += ["--entrypoint", "/bin/sh", image, str(ROOT / "scripts" / script), *arguments]
        return command, os.environ.copy()

    def run_script(self, script="run-loop.sh", arguments=("--once",)):
        command, env = self.command_line(script, arguments)
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)

    def marker(self, name, value):
        (self.root / "state" / name).write_text(value + "\n")

    def old_version(self, name):
        p = self.root / "versions" / name
        p.mkdir()
        (p / "payload").write_text("synthetic quarantine payload")
        epoch = time.time() - 10 * 86400
        os.utime(p, (epoch, epoch))
        return p

    def assert_failed_without_new_success(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "state" / "last-success").read_text().strip(), "20200101T000000Z")
        self.assertTrue((self.root / "state" / "last-error").is_file())

    def test_failed_find_does_not_delete_partial_enumeration_or_report_success(self):
        version = self.old_version("20200101T000000Z")
        self.marker("last-success", "20200101T000000Z")
        self.env["FAKE_FIND_FAIL"] = "1"
        result = self.run_script()
        self.assert_failed_without_new_success(result)
        self.assertTrue(version.is_dir())
        self.assertNotIn("rm -rf", self.calls.read_text())
        self.assertIn("class=retention", (self.root / "state" / "last-error").read_text())

    def test_first_rm_failure_is_not_hidden_by_later_success(self):
        first = self.old_version("20200101T000000Z")
        second = self.old_version("20200102T000000Z")
        self.marker("last-success", "20200101T000000Z")
        self.env["FAKE_RM_FAIL_BASENAME"] = first.name
        result = self.run_script()
        self.assert_failed_without_new_success(result)
        self.assertTrue(first.is_dir())
        self.assertFalse(second.exists())

    def test_unexpected_quarantine_path_is_preserved(self):
        outside = self.root / "outside" / "20200101T000000Z"
        outside.mkdir()
        self.marker("last-success", "20200101T000000Z")
        self.env["FAKE_UNEXPECTED_PATH"] = "1"
        self.assert_failed_without_new_success(self.run_script())
        self.assertTrue(outside.is_dir())

    def test_success_prunes_only_expired_versions_and_uses_docker_logs(self):
        old = self.old_version("20200101T000000Z")
        unknown = self.root / "versions" / "operator-notes"
        unknown.mkdir()
        self.marker("last-error", "previous failure")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(unknown.exists())
        self.assertFalse((self.root / "state" / "last-error").exists())
        self.assertIn("completed", (self.root / "state" / "last-status").read_text())
        self.assertNotIn("--log-file", self.calls.read_text())
        self.assertEqual(self.run_script("healthcheck.sh", ()).returncode, 0)
        self.assertEqual(list((self.root / "state").glob(".preflight.*")), [])

    def test_zero_retention_preserves_all_versions(self):
        old = self.old_version("20200101T000000Z")
        self.env["RETENTION_DAYS"] = "0"
        self.assertEqual(self.run_script().returncode, 0)
        self.assertTrue(old.is_dir())

    def test_sync_failure_preserves_quarantine_and_previous_success(self):
        old = self.old_version("20200101T000000Z")
        self.marker("last-success", "20200101T000000Z")
        self.env["FAKE_SYNC_MODE"] = "fail"
        self.assert_failed_without_new_success(self.run_script())
        self.assertTrue(old.is_dir())

    def test_provider_and_auth_denial_never_start_sync(self):
        for mode, kind in (("gone", "provider_access"), ("denied", "authentication")):
            with self.subTest(mode=mode):
                self.marker("last-success", "20200101T000000Z")
                self.env["FAKE_LIST_MODE"] = mode
                self.assert_failed_without_new_success(self.run_script())
                self.assertNotIn("rclone sync", self.calls.read_text())
                self.assertIn("class=" + kind, (self.root / "state" / "last-error").read_text())
                self.assertNotEqual(self.run_script("healthcheck.sh", ()).returncode, 0)

    def test_missing_or_wrong_filesystem_fails_closed_before_remote_and_prune(self):
        for option, value in (("FAKE_UNMOUNTED", "1"), ("FAKE_FS_MAGIC", "ef53"), ("FAKE_STAT_FAIL", "1")):
            with self.subTest(option=option):
                self.env[option] = value
                self.marker("last-success", "20200101T000000Z")
                self.assert_failed_without_new_success(self.run_script())
                self.assertNotIn("rclone", self.calls.read_text() if self.calls.exists() else "")
                del self.env[option]

    def test_preflight_and_sync_have_hard_deadlines(self):
        for key, mode in (("FAKE_LIST_MODE", "slow"), ("FAKE_SYNC_MODE", "slow")):
            with self.subTest(key=key):
                self.env[key] = mode
                self.env["PREFLIGHT_TIMEOUT_SECONDS"] = self.env["MAX_RUN_SECONDS"] = "1"
                self.marker("last-success", "20200101T000000Z")
                start = time.monotonic()
                self.assert_failed_without_new_success(self.run_script())
                self.assertLess(time.monotonic() - start, 8)
                del self.env[key]

    def test_stale_missing_future_and_invalid_success_are_unhealthy(self):
        self.assertNotEqual(self.run_script("healthcheck.sh", ()).returncode, 0)
        for stamp in ("20200101T000000Z", "garbage", "20990101T000000Z", "20269999T999999Z"):
            self.marker("last-success", stamp)
            self.assertNotEqual(self.run_script("healthcheck.sh", ()).returncode, 0)
        self.marker("last-success", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        self.assertEqual(self.run_script("healthcheck.sh", ()).returncode, 0)
        self.marker("last-error", "latest attempt failed")
        self.assertNotEqual(self.run_script("healthcheck.sh", ()).returncode, 0)

    def test_provider_backoff_is_bounded_without_fast_retry(self):
        # Fake sleep ends the loop after observing the chosen backoff.
        self.command("sleep", 'echo "$1" > "$TEST_ROOT/backoff"; kill -TERM "$PPID"; exit 0')
        self.env["FAKE_LIST_MODE"] = "gone"
        result = self.run_script(arguments=())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "backoff").read_text().strip(), "4")
        self.assertEqual(self.calls.read_text().count("rclone lsf"), 1)

    def test_invalid_configuration_exits_before_remote(self):
        self.env["INTERVAL_SECONDS"] = "0"
        self.assertEqual(self.run_script().returncode, 2)
        self.assertFalse(self.calls.exists())

    def test_stop_during_sync_cannot_report_success_or_prune(self):
        self.env["FAKE_SYNC_MODE"] = "slow"
        self.env["MAX_RUN_SECONDS"] = "30"
        self.marker("last-success", "20200101T000000Z")
        old = self.old_version("20200101T000000Z")
        command, env = self.command_line()
        name = None
        if os.environ.get("YDB_SHELL_TEST_IMAGE"):
            name = "ydb-stop-test-" + uuid.uuid4().hex[:12]
            command[2:2] = ["--name", name]
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            until = time.monotonic() + 8
            while not (self.root / "active-child").exists() and time.monotonic() < until:
                time.sleep(.02)
            self.assertTrue((self.root / "active-child").exists())
            if name:
                subprocess.run(["docker", "kill", "--signal=TERM", name], check=True, capture_output=True)
            else:
                process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=8)
            self.assertNotEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual((self.root / "state" / "last-success").read_text().strip(), "20200101T000000Z")
            self.assertTrue(old.exists())
            self.assertIn("class=interrupted", (self.root / "state" / "last-error").read_text())
        finally:
            if process.poll() is None:
                if name:
                    subprocess.run(["docker", "kill", name], capture_output=True)
                else:
                    process.kill()
            process.communicate(timeout=5)


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ydb-oauth-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        spec = importlib.util.spec_from_file_location("ydb_oauth", ROOT / "tools" / "update_oauth.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.config = self.root / "rclone.conf"
        self.original = '[yandex_main]\ntype = yandex\nclient_secret = old-test-secret\ntoken = {"access_token":"old-test-token","refresh_token":"old-refresh"}\n\n[secondary]\ntype = local\n'
        self.config.write_text(self.original)
        self.config.chmod(0o600)
        self.token = self.root / "new-oauth.env"
        self.token.write_text("YANDEX_ACCESS_TOKEN=synthetic_new_token_123456789\nYANDEX_CLIENT_ID=synthetic_client_123456789\nYANDEX_EXPIRES_IN=3600\n")
        self.token.chmod(0o600)

    def test_read_only_check_does_not_change_configuration(self):
        values = self.module.read_token_file(self.token)
        with patch.object(self.module, "validate_access") as probe:
            result = self.module.update_configuration(self.config, "yandex_main", values, False)
        probe.assert_called_once_with(values["YANDEX_ACCESS_TOKEN"])
        self.assertFalse(result["configuration_changed"])
        self.assertEqual(self.config.read_text(), self.original)
        self.assertEqual(len(list(self.root.glob("rclone.conf.before-oauth-*"))), 0)

    def test_apply_is_atomic_preserves_other_remote_and_makes_private_exact_backup(self):
        values = self.module.read_token_file(self.token)
        with patch.object(self.module, "validate_access"):
            result = self.module.update_configuration(self.config, "yandex_main", values, True)
        configuration = configparser.RawConfigParser()
        configuration.read(self.config)
        self.assertEqual(configuration["secondary"]["type"], "local")
        self.assertNotIn("client_secret", configuration["yandex_main"])
        token = json.loads(configuration["yandex_main"]["token"])
        self.assertNotIn("refresh_token", token)
        self.assertEqual(token["access_token"], values["YANDEX_ACCESS_TOKEN"])
        self.assertEqual(configuration["yandex_main"]["client_id"], values["YANDEX_CLIENT_ID"])
        expiry = datetime.fromisoformat(token["expiry"].replace("Z", "+00:00"))
        self.assertLess(abs((expiry - datetime.now(timezone.utc)).total_seconds() - 3600), 5)
        backup = Path(result["private_backup"])
        self.assertEqual(backup.read_text(), self.original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_failed_root_listing_keeps_old_credentials_and_does_not_echo_secret(self):
        with patch.object(self.module, "validate_access", side_effect=self.module.AccessError(410)):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = self.module.main(["--config", str(self.config), "--token-file", str(self.token), "--apply"])
        self.assertEqual(result, 1)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertIn('"http_status": 410', output.getvalue())
        self.assertNotIn("synthetic_new_token", output.getvalue())

    def test_failed_atomic_replace_preserves_old_file_and_cleans_temporary_secret(self):
        values = self.module.read_token_file(self.token)
        with patch.object(self.module, "validate_access"), patch.object(self.module.os, "replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaises(OSError):
                self.module.update_configuration(self.config, "yandex_main", values, True)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertEqual(list(self.root.glob(".oauth-update-*")), [])
        backups = list(self.root.glob("rclone.conf.before-oauth-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original)

    def test_world_readable_and_symlink_token_files_are_rejected(self):
        self.token.chmod(0o644)
        with self.assertRaises(self.module.ConfigurationError):
            self.module.read_token_file(self.token)
        self.token.chmod(0o600)
        alias = self.root / "link.env"
        alias.symlink_to(self.token)
        with self.assertRaises(OSError):
            self.module.read_token_file(alias)

    def test_duplicate_keys_unknown_keys_and_invalid_expiry_are_rejected(self):
        original = self.token.read_text()
        for extra in ("YANDEX_ACCESS_TOKEN=duplicate", "OTHER_SECRET=invalid", "YANDEX_EXPIRES_IN=10"):
            self.token.write_text(original + extra + "\n")
            with self.assertRaises(self.module.ConfigurationError):
                self.module.read_token_file(self.token)

    def test_api_checks_use_get_and_do_not_follow_redirects(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.status = 200
        response.read.side_effect = [b'{"total_space": 1000, "used_space": 1}', b'{"type":"dir","_embedded":{"items":[]}}']
        opener = Mock()
        opener.open.return_value = response
        with patch.object(self.module.urllib.request, "build_opener", return_value=opener) as builder:
            self.module.validate_access("synthetic_token")
        self.assertIsInstance(builder.call_args.args[0], self.module.NoRedirect)
        self.assertEqual(opener.open.call_count, 2)
        for call in opener.open.call_args_list:
            request = call.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertTrue(request.full_url.startswith("https://cloud-api.yandex.net/v1/disk"))
        self.assertIn("resources?", opener.open.call_args.args[0].full_url)
        self.assertIsNone(self.module.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://untrusted.invalid"))

    def test_http_200_without_resource_metadata_does_not_validate_access(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.status = 200
        response.read.side_effect = [b'{"total_space":1000,"used_space":1}', b'{}']
        opener = Mock()
        opener.open.return_value = response
        with patch.object(self.module.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(self.module.AccessError):
                self.module.validate_access("synthetic_token")


if __name__ == "__main__":
    unittest.main()
