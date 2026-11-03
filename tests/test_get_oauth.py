"""Code-exchange and private output boundary; no real OAuth calls."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse


class CodeExchangeTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "tools" / "get_oauth.py"
        spec = importlib.util.spec_from_file_location("ydb_get_oauth", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory(prefix="ydb-code-exchange-")
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "credentials.env"
        self.client = "synthetic_client_123456789"
        self.secret = "synthetic_client_secret_123456789"
        self.code = "synthetic_code_123456789"
        self.payload = {"access_token": "synthetic_access_token_123456789",
                        "refresh_token": "1:synthetic_refresh_token_123456789",
                        "expires_in": 3600, "token_type": "bearer"}
        self.response = Mock()
        self.response.__enter__ = Mock(return_value=self.response)
        self.response.__exit__ = Mock(return_value=None)
        self.response.status = 200
        self.response.read.side_effect = lambda limit: json.dumps(self.payload).encode()[:limit]
        self.opener = Mock()
        self.opener.open.return_value = self.response

    def test_exchange_posts_only_to_yandex_and_preserves_refresh_credentials(self):
        with patch.object(self.module.urllib.request, "build_opener", return_value=self.opener) as builder:
            values = self.module.exchange_code(self.client, self.secret, self.code)
        self.assertIsInstance(builder.call_args.args[0], self.module.NoRedirect)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://oauth.yandex.ru/token")
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 25)
        self.assertEqual(urllib.parse.parse_qs(request.data.decode()),
                         {"grant_type": ["authorization_code"], "code": [self.code],
                          "client_id": [self.client], "client_secret": [self.secret]})
        self.assertEqual(values["YANDEX_REFRESH_TOKEN"], self.payload["refresh_token"])
        self.assertEqual(values["YANDEX_CLIENT_SECRET"], self.secret)
        self.assertEqual(values["YANDEX_EXPIRES_IN"], "3600")
        self.assertIsNone(self.module.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://untrusted.invalid"))

    def test_incomplete_invalid_and_oversized_token_responses_are_rejected(self):
        valid = dict(self.payload)
        for missing in ("refresh_token", "access_token", "expires_in"):
            with self.subTest(missing=missing):
                self.payload = {k: v for k, v in valid.items() if k != missing}
                with patch.object(self.module.urllib.request, "build_opener", return_value=self.opener):
                    with self.assertRaises(ValueError):
                        self.module.exchange_code(self.client, self.secret, self.code)
        for expiry in (True, "3600", 0, -1, 10**12):
            self.payload = dict(valid, expires_in=expiry)
            with patch.object(self.module.urllib.request, "build_opener", return_value=self.opener):
                with self.assertRaises(ValueError):
                    self.module.exchange_code(self.client, self.secret, self.code)
        self.response.read.side_effect = lambda limit: b"x" * limit
        with patch.object(self.module.urllib.request, "build_opener", return_value=self.opener):
            with self.assertRaises(ValueError):
                self.module.exchange_code(self.client, self.secret, self.code)
        self.assertFalse(self.output.exists())

    def test_private_output_is_atomic_owner_only_and_readable_by_update_helper(self):
        with patch.object(self.module.urllib.request, "build_opener", return_value=self.opener):
            values = self.module.exchange_code(self.client, self.secret, self.code)
        self.module.write_private(self.output, values)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.output.parent.glob(".yandex-oauth-*")), [])
        path = Path(__file__).resolve().parents[1] / "tools" / "update_oauth.py"
        spec = importlib.util.spec_from_file_location("ydb_update_for_exchange", path)
        updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater)
        self.assertEqual(updater.read_token_file(self.output), values)

    def test_existing_file_and_symlink_are_preserved(self):
        self.output.write_text("existing private data")
        for output in (self.output, self.output.parent / "link.env"):
            if output != self.output:
                output.symlink_to(self.output)
            with self.assertRaises(FileExistsError):
                self.module.write_private(output, {"test": "new"})
            self.assertEqual(self.output.read_text(), "existing private data")
            self.assertEqual(list(self.output.parent.glob(".yandex-oauth-*")), [])

    def test_publish_failure_leaves_no_partial_secret_file(self):
        with patch.object(self.module.os, "link", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                self.module.write_private(self.output, {"test": self.secret})
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.output.parent.iterdir()), [])

    def test_cli_never_echoes_code_secret_or_tokens(self):
        with patch.object(self.module.getpass, "getpass", side_effect=[self.secret, self.code]), \
                patch.object(self.module.urllib.request, "build_opener", return_value=self.opener), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = self.module.main(["--client-id", self.client, "--output", str(self.output)])
        self.assertEqual(code, 0)
        self.assertTrue(self.output.exists())
        self.assertIn("response_type=code", output.getvalue())
        for private in (self.secret, self.code, self.payload["access_token"], self.payload["refresh_token"]):
            self.assertNotIn(private, output.getvalue())

    def test_expired_code_error_does_not_echo_provider_body_or_make_file(self):
        self.opener.open.side_effect = urllib.error.HTTPError(
            "https://oauth.yandex.ru/token", 400, self.secret, {}, io.BytesIO(self.code.encode()))
        with patch.object(self.module.getpass, "getpass", side_effect=[self.secret, self.code]), \
                patch.object(self.module.urllib.request, "build_opener", return_value=self.opener), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = self.module.main(["--client-id", self.client, "--output", str(self.output)])
        self.assertEqual(code, 1)
        self.assertFalse(self.output.exists())
        self.assertIn('"http_status": 400', output.getvalue())
        for private in (self.secret, self.code):
            self.assertNotIn(private, output.getvalue())


if __name__ == "__main__":
    unittest.main()
