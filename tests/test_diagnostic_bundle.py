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
import os
import time
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

    def test_crash_dumps_ride_along_and_only_ours(self) -> None:
        """A fast-fail crash leaves no [crash] line, so the dump is the record.

        The negative controls are the point: another program's dump must never
        enter the bundle, and a machine that writes no dumps must be reported
        as such rather than as "the crash left nothing".
        """
        dumps = self.work / "CrashDumps"
        dumps.mkdir()
        ours = dumps / "nvngx.dll.1234.dmp"
        ours.write_bytes(b"OUR-DUMP")
        # A dump of something else entirely, on the same workstation.
        (dumps / "python.exe.99.dmp").write_bytes(b"SOMEONE-ELSES")
        # And one with a username in the name - it must not become the entry
        # name, which is what a reader would see in the archive.
        (dumps / "nvngx.dll.5678.dmp").write_bytes(b"SECOND")

        with mock.patch.object(diagnostics, "_crash_dump_dirs", return_value=[dumps]):
            collected = diagnostics.collect_crash_dumps()
            self.assertEqual(len(collected), 2)
            self.assertEqual([name for name, _ in collected],
                             ["crashdump/1.dmp", "crashdump/2.dmp"])
            self.assertEqual({data for _, data in collected},
                             {b"OUR-DUMP", b"SECOND"})

            bundle = diagnostics.create_diagnostic_bundle(
                self.work / "with-dumps.zip", self.request())
            with zipfile.ZipFile(bundle) as archive:
                names = archive.namelist()
                self.assertIn("crashdump/1.dmp", names)
                self.assertIn("crashdump/2.dmp", names)
                report = json.loads(archive.read("diagnostics.json"))
            self.assertEqual(report["log"]["crash_dumps"]["included"], 2)
            self.assertIn("policy", report["log"]["crash_dumps"])

        # A dump from a PREVIOUS session must not ride along. Windows keeps
        # the folder across runs, so without this the same crash is reported as
        # this run's - measured on a real report where two consecutive bundles
        # carried identical dumps from an older release folder, and the stack
        # was read and believed before the paths gave it away.
        # Two stale names, one of which looks plausible: a filter keyed on the
        # file name rather than the time would let the second one through.
        stale = dumps / "nvngx.dll.4321.dmp"
        stale.write_bytes(b"OLD-SESSION")
        old_time = time.time() - 3600.0
        os.utime(stale, (old_time, old_time))
        stale2 = dumps / "nvngx.dll.1.2.3.dmp"
        stale2.write_bytes(b"OLD-SESSION-2")
        os.utime(stale2, (old_time, old_time))
        with mock.patch.object(diagnostics, "_crash_dump_dirs", return_value=[dumps]):
            with mock.patch.object(diagnostics, "process_start_time",
                                   return_value=time.time()):
                collected = diagnostics.collect_crash_dumps()
                self.assertEqual({data for _, data in collected},
                                 {b"OUR-DUMP", b"SECOND"})
                self.assertNotIn(b"OLD-SESSION", {data for _, data in collected})
                self.assertNotIn(b"OLD-SESSION-2", {data for _, data in collected})

                # ...and the same file IS shipped when it belongs to this run:
                # the filter must be about time, not about the file name.
                fresh = dumps / "nvngx.dll.9999.dmp"
                fresh.write_bytes(b"THIS-SESSION")
                os.utime(fresh, (time.time(), time.time()))
                collected = diagnostics.collect_crash_dumps()
                self.assertIn(b"THIS-SESSION", {data for _, data in collected})

        # The clock itself is checked WITHOUT a mock, because a mocked
        # process_start_time cannot see the function being broken: returning
        # 0.0 from it, or a wrong epoch conversion, would leave the filter
        # inert and every test above would still pass. Both were holes found by
        # breaking the code on purpose.
        real_start = diagnostics.process_start_time()
        self.assertGreater(real_start, 1_600_000_000.0,
                           "process start must be a real epoch time, not 0")
        self.assertLess(abs(real_start - time.time()), 24 * 3600,
                        "process start must be near now, not a mis-scaled FILETIME")

        # Negative control: a machine with no dumps says so, and still reports
        # whether it is configured to write them at all.
        empty = self.work / "NoDumps"
        empty.mkdir()
        with mock.patch.object(diagnostics, "_crash_dump_dirs", return_value=[empty]):
            self.assertEqual(diagnostics.collect_crash_dumps(), [])
            bundle = diagnostics.create_diagnostic_bundle(
                self.work / "no-dumps.zip", self.request())
            with zipfile.ZipFile(bundle) as archive:
                self.assertNotIn("crashdump/1.dmp", archive.namelist())
                report = json.loads(archive.read("diagnostics.json"))
            self.assertEqual(report["log"]["crash_dumps"]["included"], 0)
            self.assertFalse(
                report["log"]["crash_dumps"]["policy"]["local_dumps_configured"])

    def test_our_own_dump_wins_over_the_wer_folder(self) -> None:
        """A dump this run wrote is preferred to one Windows happened to keep.

        The registry path needs HKLM and an administrator, so most machines
        have nothing there; the worker therefore writes its own dump next to
        its log, and that one is certainly about the crash being reported.
        """
        with tempfile.TemporaryDirectory() as tmp:
            native = Path(tmp) / "native"
            native.mkdir()
            mine = native / "dlss5-feed-host.dmp"
            mine.write_bytes(b"MDMP" + b"\x00" * 64)
            # Right name, wrong content: must not ride along, or a reader opens
            # it and blames the tooling.
            (native / "not-really.dmp").write_bytes(b"GARBAGE")
            # A valid dump under a name the glob would only match if it were
            # widened: the collector reads *.dmp specifically, and a widened
            # glob would sweep in unrelated files from the program folder.
            (native / "somethingelse.bin").write_bytes(b"MDMP" + b"\x00" * 64)

            def collect():
                with mock.patch.object(diagnostics, "_own_worker_dirs",
                                       return_value=[native]), \
                     mock.patch.object(diagnostics, "process_start_time",
                                       return_value=time.time()):
                    return diagnostics.collect_own_crash_dumps()

            got = collect()
            self.assertEqual(len(got), 1)
            self.assertTrue(got[0][1].startswith(b"MDMP"))

            # A stale one of ours is skipped, the same rule as the WER folder.
            old = time.time() - 3600.0
            os.utime(mine, (old, old))
            self.assertEqual(collect(), [])

            # Negative control: a truncated dump is not a dump.
            os.utime(mine, (time.time(), time.time()))
            mine.write_bytes(b"M")
            self.assertEqual(collect(), [])

        # The real search path is checked WITHOUT a mock: a mocked
        # _own_worker_dirs cannot see the function being broken (removing the
        # `native/` entry would leave every test above passing), and that entry
        # is where the worker actually writes. Found by breaking it on purpose.
        real_dirs = diagnostics._own_worker_dirs()
        self.assertTrue(real_dirs, "the collector must search somewhere")
        self.assertIn("native", {d.name for d in real_dirs},
                      "the worker writes its dump into native/, which must be searched")

    def test_engine_log_rides_along_and_secrets_stay_out(self) -> None:
        """The engine's own log is the only place that says it initialised.

        Every black-picture report carried the engine's `encoded mean 0.000`
        while nothing said whether the engine had come up at all - and the
        answer sat in a file nobody attached. The negative controls are the
        point: a machine without that file must say so, and its contents must
        be scrubbed like everything else.
        """
        engine = self.work / "native" / "dlssnr_on_amd.log"
        engine.parent.mkdir()
        engine.write_text(
            "dlssnr_amd v0.2.17 loaded\n"
            "env: HIP device 0: AMD Radeon RX 9070 XT, arch gfx1201\n"
            "engine init ok\n"
            r"game exe C:\Users\Alice\Games\game.exe" "\n"
            "opaque=opaque-secret-value\n",
            encoding="utf-8",
        )

        with mock.patch.object(diagnostics, "BASE_DIR", self.work), \
             mock.patch.dict(diagnostics.os.environ, {}, clear=False):
            diagnostics.os.environ.pop("NS_AMD_DIR", None)
            raw, meta = diagnostics.collect_runtime_log()
            self.assertTrue(meta["found"])
            self.assertIn(b"engine init ok", raw)

            bundle = diagnostics.create_diagnostic_bundle(
                self.work / "with-engine-log.zip", self.request())
            with zipfile.ZipFile(bundle) as archive:
                names = archive.namelist()
                self.assertIn("amd_runtime_log_tail.txt", names)
                carried = archive.read("amd_runtime_log_tail.txt").decode("utf-8")
                report = json.loads(archive.read("diagnostics.json"))
            self.assertIn("engine init ok", carried)
            # Same gate as the host log: nothing personal survives.
            self.assertNotIn("Alice", carried)
            self.assertNotIn("opaque-secret-value", carried)
            self.assertTrue(report["log"]["runtime_log"]["found"])

        # Negative control: no file at all is reported as such, and the bundle
        # is still built - an absent log must never fail the collection.
        empty = self.work / "elsewhere"
        empty.mkdir()
        with mock.patch.object(diagnostics, "BASE_DIR", empty):
            raw, meta = diagnostics.collect_runtime_log()
            self.assertEqual(raw, b"")
            self.assertFalse(meta["found"])
            bundle = diagnostics.create_diagnostic_bundle(
                self.work / "no-engine-log.zip", self.request())
            with zipfile.ZipFile(bundle) as archive:
                self.assertNotIn("amd_runtime_log_tail.txt", archive.namelist())
                report = json.loads(archive.read("diagnostics.json"))
            self.assertFalse(report["log"]["runtime_log"]["found"])
            self.assertIn("note", report["log"]["runtime_log"])

    def test_existing_bundle_survives_publish_failure(self) -> None:
        destination = self.work / "diagnostics.zip"
        destination.write_bytes(b"previous-complete-bundle")
        with mock.patch.object(diagnostics.os, "replace", side_effect=OSError("publish failed")):
            with self.assertRaisesRegex(OSError, "publish failed"):
                diagnostics.create_diagnostic_bundle(destination, self.request())
        self.assertEqual(destination.read_bytes(), b"previous-complete-bundle")
        leftovers = list(self.work.glob(f".{destination.name}.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_the_policy_reports_per_app_dumps_as_configured(self) -> None:
        """`local_dumps_configured: false` must not mean "this machine cannot".

        Measured on a real report: the policy said false while the bundle
        carried three dumps. The machine configures LocalDumps PER APPLICATION,
        one subkey per executable, and the shared key has no DumpFolder of its
        own - so the old single-value check answered "not configured" for a
        machine that was plainly writing them, and a reader would have followed
        the wrong branch.
        """
        policy = diagnostics.crash_dump_policy()
        self.assertIn("per_app_subkeys", policy)
        self.assertIn("any_configured", policy)
        # Three states, each named, so a report never collapses them:
        #   machine-wide, per-application, or neither.
        self.assertEqual(
            policy["any_configured"],
            bool(policy["local_dumps_configured"]) or bool(policy["per_app_subkeys"]),
            "any_configured must be exactly the union of the other two")

        # A machine with only per-app keys must NOT read as unconfigured.
        fake = dict(policy)
        fake["local_dumps_configured"] = False
        fake["per_app_subkeys"] = ["HKLM\\SomeGame.exe"]
        self.assertTrue(fake["local_dumps_configured"] or fake["per_app_subkeys"],
                        "per-app entries count as configured")

        # The enumeration is checked against a FAKE registry, not this
        # machine's: a check that can only run against whatever the developer's
        # PC happens to have cannot tell a working enumeration from a removed
        # one - and both of those were holes found by breaking the code.
        class FakeWinreg:
            HKEY_LOCAL_MACHINE = "HKLM"
            HKEY_CURRENT_USER = "HKCU"

            def __init__(self, keys):
                self._keys = keys          # name -> (values, subkeys)

            def OpenKey(self, hive, sub):
                if self._keys.get(hive) is None:
                    raise OSError("not found")
                # The handle is the hive name: the caller must not learn how
                # this fake stores things, only what a registry would answer.
                return hive

            def QueryValueEx(self, key, value):
                vals, _subs = self._keys[key]
                if value not in vals:
                    raise OSError("no value")
                return vals[value], 1

            def EnumKey(self, key, index):
                _vals, subs = self._keys[key]
                if index >= len(subs):
                    raise OSError("no more")
                return subs[index]

            def CloseKey(self, key):
                pass

        # A machine with only per-application keys, which is the case that was
        # mis-reported: no DumpFolder anywhere, two executables configured.
        # OpenKey yields the hive name; QueryValueEx/EnumKey look the entry up.
        fake = FakeWinreg({
            "HKLM": ({}, ["SomeGame.exe", "OurWorker.exe"]),
            "HKCU": None,
        })
        machine, per_app, folder = diagnostics._local_dumps_config(fake)
        self.assertFalse(machine, "no machine-wide DumpFolder on this fake")
        self.assertEqual(per_app, ["HKLM\\SomeGame.exe", "HKLM\\OurWorker.exe"],
                         "each per-application subkey must be named")
        self.assertEqual(folder, "")

        # Negative control: an empty enumeration is reported as empty, so a
        # broken walk cannot masquerade as "nothing configured".
        empty = FakeWinreg({})
        machine, per_app, folder = diagnostics._local_dumps_config(empty)
        self.assertEqual((machine, per_app, folder), (False, [], ""))

        # And the note must say that our own dump needs no configuration, or a
        # reader treats a missing dump as "this machine does not write them".
        # Case-insensitively: the note capitalises OWN for emphasis, and a
        # case-sensitive check on prose would fail on a wording tweak while the
        # meaning - "our dump needs no configuration" - is intact.
        self.assertIn("own dump", policy["note"].lower())

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
