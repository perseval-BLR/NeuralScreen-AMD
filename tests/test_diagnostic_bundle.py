r"""Deterministic, private and atomic diagnostic support bundles.

Run: runtime\python.exe tests\test_diagnostic_bundle.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import diagnostics  # noqa: E402


class DiagnosticBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)
        self.runtime = self.work / "private" / "nvngx_dlssnr.dll"
        self.runtime.parent.mkdir()
        self.runtime.write_bytes(b"test-runtime\x00\x01\xff")
        self.log = self.work / "logs" / "NeuralScreen.log"
        self.log.parent.mkdir()
        secret_tail = "\n".join(
            (
                "[failure] stage=first-evaluate kind=device-removed code=0x887A0005",
                r'user=<Alice> home="C:\Users\Alice\Saved Games\NeuralScreen"',
                r'custom="D:\Games\Secret Folder\capture.bin" after=private',
                r'unc="\\server\share\private\dump.bin"',
                "posix='/home/alice/private/report.txt'",
                "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuv",
                "Authorization: Bearer bearer-secret-value",
                "password=hunter2",
                "github_pat_abcdefghijklmnopqrstuvwxyz123456",
                "opaque=opaque-secret-value",
                "support=https://example.com/support/path",
            )
        )
        self.log.write_text(("old line\n" * 400) + secret_tail, encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self) -> diagnostics.DiagnosticBundleRequest:
        return diagnostics.DiagnosticBundleRequest(
            failure_stage="first-evaluate",
            failure_details={
                "hresult": "0x887A0005",
                "dred": {"message": r"dump at E:\Crash Dumps\gpu.dmp"},
                "api_token": "another-secret",
            },
            app_version="1.13.0",
            commit="0123456789abcdef0123456789abcdef01234567",
            runtime_path=self.runtime,
            log_path=self.log,
            system_snapshot={
                "os": {"system": "Windows", "version": "10.0.26200"},
                "gpus": [
                    {
                        "name": "NVIDIA GeForce RTX 4060",
                        "driver_version": "32.0.15.6164",
                        "debug_path": r"F:\Users\Alice\driver.txt",
                    }
                ],
                "displays": [
                    {
                        "name": r"\\.\DISPLAY2",
                        "width": 2560,
                        "height": 1440,
                        "primary": True,
                    }
                ],
            },
            runtime_signature={
                "status": "Valid",
                "subject": "CN=NVIDIA Corporation",
                "thumbprint": "AABBCCDD",
            },
            sensitive_values=("Alice", "opaque-secret-value"),
            max_log_bytes=1024,
        )

    @staticmethod
    def read_bundle(path: Path) -> tuple[dict, bytes, list[zipfile.ZipInfo]]:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            report = json.loads(archive.read("diagnostics.json"))
            log_tail = archive.read("log_tail.txt")
        return report, log_tail, infos

    def test_bundle_is_complete_private_bounded_and_deterministic(self) -> None:
        first = diagnostics.create_diagnostic_bundle(self.work / "first.zip", self.request())
        second = diagnostics.create_diagnostic_bundle(self.work / "second.zip", self.request())

        self.assertEqual(first.read_bytes(), second.read_bytes())
        report, log_tail, infos = self.read_bundle(first)
        self.assertEqual([info.filename for info in infos], ["diagnostics.json", "log_tail.txt"])
        self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos))

        self.assertEqual(report["schema"], "neuralscreen.diagnostics/v1")
        self.assertEqual(report["application"]["version"], "1.13.0")
        self.assertEqual(
            report["application"]["commit"],
            "0123456789abcdef0123456789abcdef01234567",
        )
        self.assertEqual(report["failure"]["stage"], "first-evaluate")
        self.assertEqual(report["failure"]["details"]["hresult"], "0x887A0005")
        self.assertEqual(report["failure"]["details"]["api_token"], "<REDACTED>")
        self.assertEqual(report["graphics"]["gpus"][0]["driver_version"], "32.0.15.6164")
        self.assertEqual(report["graphics"]["displays"][0]["name"], r"\\.\DISPLAY2")

        self.assertEqual(report["runtime"]["name"], "nvngx_dlssnr.dll")
        self.assertEqual(report["runtime"]["sha256"], hashlib.sha256(self.runtime.read_bytes()).hexdigest())
        self.assertEqual(report["runtime"]["signature"]["status"], "Valid")
        self.assertEqual(report["runtime"]["signature"]["subject"], "CN=NVIDIA Corporation")

        self.assertLessEqual(len(log_tail), 1024)
        self.assertEqual(report["log"]["bytes"], len(log_tail))
        self.assertTrue(report["log"]["included"])
        self.assertTrue(report["log"]["truncated"])
        text = json.dumps(report, ensure_ascii=False) + "\n" + log_tail.decode("utf-8")
        for private in (
            "Alice",
            "hunter2",
            "another-secret",
            "opaque-secret-value",
            "bearer-secret-value",
            "sk-proj-abcdefghijklmnopqrstuv",
            "github_pat_abcdefghijklmnopqrstuvwxyz123456",
            r"C:\Users",
            r"D:\Games",
            r"E:\Crash Dumps",
            r"F:\Users",
            r"\\server\share",
            "/home/alice",
            str(self.work),
        ):
            self.assertNotIn(private, text)
        self.assertIn("<REDACTED>", text)
        self.assertIn("<PATH>", text)
        self.assertIn("https://example.com/support/path", text)
        self.assertIn("code=0x887A0005", text)

    def test_existing_bundle_survives_publish_failure(self) -> None:
        destination = self.work / "diagnostics.zip"
        destination.write_bytes(b"previous-complete-bundle")
        with mock.patch.object(diagnostics.os, "replace", side_effect=OSError("publish failed")):
            with self.assertRaisesRegex(OSError, "publish failed"):
                diagnostics.create_diagnostic_bundle(destination, self.request())
        self.assertEqual(destination.read_bytes(), b"previous-complete-bundle")
        leftovers = list(self.work.glob(f".{destination.name}.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_manifest_identity_and_input_validation(self) -> None:
        fixture = self.work / "release"
        fixture.mkdir()
        fixture.joinpath("VERSION.txt").write_text(
            "NeuralScreen 1.13.0\n"
            "commit: abcdef0123456789abcdef0123456789abcdef01\n",
            encoding="utf-8",
        )
        self.assertEqual(
            diagnostics.discover_application_identity(fixture),
            {
                "name": "NeuralScreen",
                "version": "1.13.0",
                "commit": "abcdef0123456789abcdef0123456789abcdef01",
            },
        )

        invalid_stage = self.request()
        object.__setattr__(invalid_stage, "failure_stage", "")
        with self.assertRaisesRegex(ValueError, "failure_stage"):
            diagnostics.create_diagnostic_bundle(self.work / "bad.zip", invalid_stage)

        invalid_limit = self.request()
        object.__setattr__(invalid_limit, "max_log_bytes", diagnostics.MAX_LOG_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "max_log_bytes"):
            diagnostics.create_diagnostic_bundle(self.work / "bad.zip", invalid_limit)

        with self.assertRaisesRegex(ValueError, "end in .zip"):
            diagnostics.create_diagnostic_bundle(self.work / "bad.json", self.request())


if __name__ == "__main__":
    unittest.main(verbosity=2)
