"""The worker and standard-library reporter share the same GitHub wire contract."""

import ast
import base64
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import dependabot_api as api


class ApiTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(api.Path, "is_file", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_newest_first_statuses_preserve_current_failures_and_drop_old_results(self):
        statuses = [
            {"context": "lint", "state": "success", "id": 4},
            {"context": "security", "state": "failure", "id": 3},
            {"context": "lint", "state": "failure", "id": 2},
            {"context": "build", "state": "success", "id": 1},
        ]
        result = api.latest_statuses(statuses)
        self.assertEqual(
            [
                ("lint", "success", 4),
                ("security", "failure", 3),
                ("build", "success", 1),
            ],
            [(item["context"], item["state"], item["id"]) for item in result],
        )
        self.assertEqual(4, len(statuses))

    def test_empty_status_history_stays_empty(self):
        self.assertEqual([], api.latest_statuses([]))

    def test_secret_gist_json_and_readback_preserve_boolean_types(self):
        expected = {
            "public": False,
            "files": {"help.md": {"content": "Readable report"}},
        }
        encoded = base64.b64encode(json.dumps(expected).encode()).decode()
        output = f"api_response:\n  body: {encoded}\n  truncated: false\n"
        with mock.patch.object(
            api,
            "run_command",
            return_value=subprocess.CompletedProcess([], 0, output),
        ) as run:
            self.assertEqual(expected, api.GitHub().request("POST", "/gists", expected))
        self.assertIs(False, json.loads(run.call_args.kwargs["input"])["public"])
        self.assertIn("--input", run.call_args.args[0])
        self.assertEqual(["gh", "cfs", "--", "npx"], run.call_args.args[0][:4])

    def test_gist_lookup_checks_every_page(self):
        first = [{"id": str(index)} for index in range(100)]
        last = [{"id": "managed"}]
        client = api.GitHub()
        with mock.patch.object(client, "request", side_effect=[first, last]) as request:
            self.assertEqual(first + last, client.pages("/gists"))
        self.assertEqual("/gists?per_page=100&page=2", request.call_args.args[1])

    def test_get_recovers_after_two_failed_commands(self):
        expected = {"permissions": {"push": True}}
        encoded = base64.b64encode(json.dumps(expected).encode()).decode()
        failed = subprocess.CompletedProcess(
            [],
            1,
            'error: "temporary lookup failure"',
            "cfs: command exited with status 1",
        )
        succeeded = subprocess.CompletedProcess(
            [], 0, f"api_response:\n  body: {encoded}\n  truncated: false\n", ""
        )
        with (
            mock.patch.object(
                api, "run_command", side_effect=[failed, failed, succeeded]
            ) as run,
            mock.patch("time.sleep") as sleep,
        ):
            self.assertEqual(
                expected, api.GitHub().request("GET", "/repos/example/repo")
            )
        self.assertEqual(3, run.call_count)
        self.assertEqual([mock.call(1), mock.call(2)], sleep.call_args_list)
        self.assertTrue(
            all(call == run.call_args_list[0] for call in run.call_args_list)
        )

    def test_get_stops_after_three_failures_and_preserves_both_streams(self):
        failures = [
            subprocess.CompletedProcess(
                [],
                1,
                f'error: "lookup failure {index}"',
                "cfs: command exited with status 1",
            )
            for index in range(1, 4)
        ]
        with (
            mock.patch.object(api, "run_command", side_effect=failures) as run,
            mock.patch("time.sleep") as sleep,
            self.assertRaises(api.AutomationError) as error,
        ):
            api.GitHub().request("GET", "/repos/example/repo")
        self.assertEqual(3, run.call_count)
        self.assertEqual(2, sleep.call_count)
        message = str(error.exception)
        self.assertIn("GET /repos/example/repo", message)
        self.assertIn("attempt 3", message)
        self.assertIn("exit status 1", message)
        self.assertIn("stderr: cfs: command exited with status 1", message)
        for index in range(1, 4):
            self.assertIn(f'stdout: error: "lookup failure {index}"', message)

    def test_get_recovers_after_timeout(self):
        output = "api_response:\n  body: e30=\n  truncated: false\n"
        with (
            mock.patch.object(
                api,
                "run_command",
                side_effect=[
                    subprocess.TimeoutExpired("gh", 90),
                    subprocess.CompletedProcess([], 0, output, ""),
                ],
            ) as run,
            mock.patch("time.sleep") as sleep,
        ):
            self.assertEqual({}, api.GitHub().request("GET", "/user"))
        self.assertEqual(2, run.call_count)
        sleep.assert_called_once_with(1)

    def test_failed_writes_never_retry_and_keep_private_diagnostics(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for failure in (
                subprocess.CompletedProcess(
                    [], 1, "upstream failure", "wrapper failure"
                ),
                subprocess.TimeoutExpired(
                    "gh", 90, output=b"upstream failure", stderr=b"wrapper failure"
                ),
            ):
                with (
                    self.subTest(method=method, failure=type(failure).__name__),
                    mock.patch.object(api, "run_command") as run,
                    mock.patch("time.sleep") as sleep,
                ):
                    if isinstance(failure, Exception):
                        run.side_effect = failure
                    else:
                        run.return_value = failure
                    with self.assertRaises(api.AutomationError) as error:
                        api.GitHub().request(
                            method, "/example", {"body": "private payload"}
                        )
                    run.assert_called_once()
                    sleep.assert_not_called()
                    self.assertIn("stdout: upstream failure", str(error.exception))
                    self.assertIn("stderr: wrapper failure", str(error.exception))
                    self.assertNotIn("private payload", str(error.exception))

    def test_timeout_exhaustion_remains_a_failure(self):
        with (
            mock.patch.object(
                api, "run_command", side_effect=subprocess.TimeoutExpired("gh", 90)
            ) as run,
            mock.patch("time.sleep") as sleep,
            self.assertRaises(api.AutomationError) as error,
        ):
            api.GitHub().request("GET", "/user")
        self.assertEqual(3, run.call_count)
        self.assertEqual(2, sleep.call_count)
        self.assertIn("timed out after 90 seconds", str(error.exception))

    def test_invalid_successful_responses_fail_without_retry(self):
        for output, error_type in (
            ("api_response:\n  body: e30=\n  truncated: true\n", api.AutomationError),
            ("api_response:\n  truncated: false\n", api.AutomationError),
            ("api_response:\n  body: !!!\n  truncated: false\n", ValueError),
            ("api_response:\n  body: bm90IGpzb24=\n  truncated: false\n", ValueError),
        ):
            with (
                self.subTest(output=output),
                mock.patch.object(
                    api,
                    "run_command",
                    return_value=subprocess.CompletedProcess([], 0, output, ""),
                ) as run,
                mock.patch("time.sleep") as sleep,
            ):
                with self.assertRaises(error_type):
                    api.GitHub().request("GET", "/user")
                run.assert_called_once()
                sleep.assert_not_called()

    def test_prepared_cli_avoids_cfs_and_npm_during_api_requests(self):
        output = "api_response:\n  body: e30=\n  truncated: false\n"
        with (
            mock.patch.object(api.Path, "is_file", return_value=True),
            mock.patch.object(
                api,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, output),
            ) as run,
        ):
            self.assertEqual({}, api.GitHub().request("GET", "/user"))
        command = run.call_args.args[0]
        self.assertTrue(
            command[0].endswith("/.dependabot-runtime/node/node_modules/.bin/gh-axi")
        )
        self.assertEqual("api", command[1])
        self.assertNotIn("cfs", command)
        self.assertNotIn("npx", command)

    def test_timeout_kills_descendants_before_collecting_output(self):
        process = mock.MagicMock()
        process.pid = 123
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("command", 90),
            ("out", "err"),
        ]
        with (
            mock.patch.object(api.subprocess, "Popen") as popen,
            mock.patch.object(api.os, "killpg") as kill,
        ):
            popen.return_value.__enter__.return_value = process
            with self.assertRaises(subprocess.TimeoutExpired) as error:
                api.run_command(["command"], input=None)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        kill.assert_called_once_with(123, api.signal.SIGKILL)
        self.assertEqual("out", error.exception.stdout)
        self.assertEqual("err", error.exception.stderr)

    def test_real_timeout_stops_a_descendant_holding_the_output_pipe(self):
        start = time.monotonic()
        command = [
            sys.executable,
            "-c",
            "import subprocess,sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); print('started', flush=True)",
        ]
        with self.assertRaises(subprocess.TimeoutExpired) as error:
            api.run_command(command, timeout=0.5)
        self.assertIn("started", error.exception.stdout)
        self.assertLess(time.monotonic() - start, 10)

    def test_real_command_preserves_json_streams_and_exit_status(self):
        result = api.run_command(
            [
                sys.executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)",
            ]
        )
        self.assertEqual(
            (7, "out\n", "err\n"), (result.returncode, result.stdout, result.stderr)
        )

    def test_api_module_supports_the_bootstrap_python_syntax(self):
        ast.parse(Path(api.__file__).read_text(), feature_version=(3, 8))


if __name__ == "__main__":
    unittest.main()
