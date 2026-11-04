"""Manager business flows against the existing controller with a fake phone."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import re
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from itsdangerous import TimestampSigner
from werkzeug.serving import make_server, WSGIRequestHandler

import test_controller_client as fixtures
from src.bark import IMAGE_MAX_AGE, screenshot_signer
from src.controller_client import ControllerClient, ControllerTransportError
from src.storage import Store, TZ, stamp
from src.tasks import Service
from src.web import create_app


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        config = fixtures.controller.Config(
            adb_exe=Path("unused"), api_key=fixtures.KEY, host="127.0.0.1", port=8765,
            default_serial=fixtures.SERIAL, default_package="com.example.default", default_delay=10,
            max_delay=120, command_timeout=20, data_dir=root / "controller", result_retention_hours=24,
        )
        self.adb = fixtures.FakeAdb()
        self.remote = fixtures.controller.Controller(config, adb=self.adb, sleeper=lambda seconds: None)
        self.addCleanup(self.remote.close)
        server = make_server("127.0.0.1", 0, fixtures.controller.create_app(config, controller=self.remote),
                             threaded=True, request_handler=QuietHandler)
        context = fixtures.running_server(server)
        self.url = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.store = Store(root / "manager")
        self.store.update({"controller_url": self.url, "controller_api_key": fixtures.KEY,
                           "package": "com.example.app", "wait_seconds": 0})
        self.at = datetime(2026, 11, 4, 8, 45, tzinfo=TZ)
        self.service = self.new_service()

    def new_service(self, **kwargs):
        service = Service(self.store, clock=lambda: self.at, **kwargs)
        service.poll_seconds = .01
        service.not_found_seconds = .02
        return service

    def web_browser(self):
        app = create_app(store=self.store, service=self.service, secure_cookie=False)
        app.config["TESTING"] = True
        browser = app.test_client()
        with browser.session_transaction() as session:
            session.update(authenticated=True, auth_version=self.store.get("_auth_version"), csrf="test-csrf")
        return browser, "test-csrf"

    def confirmation_sequence(self, response):
        if response.is_json:
            payload = response.get_json()
        else:
            match = re.search(rb'id="initial-run-confirmation">(.*?)</script>', response.data, re.S)
            self.assertIsNotNone(match, "The confirmation page must provide a digit challenge")
            payload = json.loads(match.group(1))
        question = payload["confirmation"]
        sequence = question["sequence"]
        self.assertRegex(sequence, r"^[1-9]{6}$")
        self.assertEqual(len(set(sequence)), 6)
        self.assertEqual(sorted(question["grid"]), list("123456789"))
        return sequence

    def automatic_run(self):
        self.service.planner.ensure(self.at.date(), self.at.date())
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT id FROM plans WHERE day=? AND slot='morning'", (self.at.date().isoformat(),)).fetchone()
            db.execute("UPDATE plans SET scheduled_at=? WHERE id=?", (stamp(self.at), row[0]))
        self.service.tick()
        with self.store.connect() as db:
            return db.execute("SELECT id FROM runs WHERE plan_id=?", (row[0],)).fetchone()[0]

    def test_manual_snapshot_duplicate_clicks_result_and_notification(self):
        self.service.probe()
        run_id = self.service.submit("full", "manual-1")
        self.assertEqual(run_id, self.service.submit("full", "manual-1"))
        self.assertEqual(run_id, self.service.submit("full", "another-click"))
        snapshot = self.store.dispatch(run_id)
        self.assertEqual(snapshot["params"], {"serial": "", "package": "com.example.app", "delay": 0})
        self.store.update({"controller_url": "http://127.0.0.1:1", "controller_api_key": "x" * 32,
                           "package": "com.example.changed", "serial": "different-phone"})
        self.service.execute(run_id)
        run = self.store.run(run_id)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["remote_result"]["package"], "com.example.app")
        self.assertEqual(run["device"]["serial"], fixtures.SERIAL)
        self.assertEqual((self.store.screenshots / run["screenshot"]).read_bytes(), fixtures.PNG)
        self.assertNotIn("dispatch", run)
        self.assertNotIn("base64", run["remote_result"]["screenshot"])
        self.assertNotIn("api_key", self.store.dispatch(run_id))
        notices = []
        self.service.sender = lambda settings, title, body, image="": (notices.append((title, body, image)) or ("sent", ""))
        self.service.send_notice("run", run_id)
        self.service.send_notice("run", run_id)  # Explicit resend does not repeat phone execution.
        self.assertEqual(self.store.run(run_id)["bark_status"], "sent")
        self.assertEqual(len(notices), 2)
        self.assertEqual(notices[0][2], "")  # A public URL is required to attach a screenshot.
        self.assertEqual(self.adb.captures, 1)

    def test_bark_image_payload_signed_access_expiry_and_resend(self):
        self.store.update({"bark_key": "test-bark-key", "public_url": "https://checkin.example/"})
        run_id = self.service.submit("full", "bark-image")
        self.service.execute(run_id)
        app = create_app(store=self.store, service=self.service, secure_cookie=False)
        browser = app.test_client()  # Bark downloads without a browser login cookie.

        with patch("src.tasks.urllib.request.urlopen") as send:
            send.return_value.__enter__.return_value.read.return_value = b'{"code":200}'
            self.service.send_notice("run", run_id)
            request = send.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(request.full_url, "https://api.day.app/push")
            self.assertNotIn("url", payload)
            self.assertEqual(payload["device_key"], "test-bark-key")
            self.assertEqual(self.store.run(run_id)["bark_status"], "sent")
            image = urlsplit(payload["image"])
            self.assertEqual(image.netloc, "checkin.example")
            self.assertNotIn("test-bark-key", image.path)
            self.assertNotIn(fixtures.KEY, image.path)
            with browser.get(image.path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "image/png")
                self.assertEqual(response.data, fixtures.PNG)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(browser.get("/screenshots/" + run_id).status_code, 303)
            self.assertEqual(browser.get("/runs/" + run_id).status_code, 303)

            token = image.path.rsplit("/", 1)[1][:-4]
            prefix, signature = token.rsplit(".", 1)
            invalid = prefix + "." + ("a" if signature[0] != "a" else "b") + signature[1:]
            self.assertEqual(browser.get("/bark/screenshots/" + invalid + ".png").status_code, 404)
            missing = screenshot_signer(self.store.get("_secret")).dumps("missing-run")
            self.assertEqual(browser.get("/bark/screenshots/" + missing + ".png").status_code, 404)
            self.assertEqual(browser.get("/bark/screenshots/invalid.png").status_code, 404)

            future = int(time.time()) + IMAGE_MAX_AGE + 1
            with patch.object(TimestampSigner, "get_timestamp", return_value=future):
                self.assertEqual(browser.get(image.path).status_code, 404)
                self.service.send_notice("run", run_id)
                resent = json.loads(send.call_args.args[0].data)
                self.assertNotEqual(resent["image"], payload["image"])
                with browser.get(urlsplit(resent["image"]).path) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.data, fixtures.PNG)
            self.assertEqual(self.adb.captures, 1)  # Resending only refreshes the image link.

        (self.store.screenshots / self.store.run(run_id)["screenshot"]).unlink()
        self.assertEqual(browser.get(image.path).status_code, 404)

    def test_bark_text_fallback_and_events_do_not_attach_images(self):
        self.store.update({"bark_key": "test-bark-key"})
        run_id = self.service.submit("screenshot", "bark-text-only")
        self.service.execute(run_id)
        with patch("src.tasks.urllib.request.urlopen") as send:
            send.return_value.__enter__.return_value.read.return_value = b'{"code":200}'
            self.service.send_notice("run", run_id)
            payload = json.loads(send.call_args.args[0].data)
            self.assertNotIn("image", payload)
            self.assertNotIn("url", payload)
            self.assertEqual(self.store.run(run_id)["bark_status"], "sent")

            self.store.update({"public_url": "https://checkin.example"})
            self.store.change_run(run_id, screenshot=None)
            self.service.send_notice("run", run_id)
            payload = json.loads(send.call_args.args[0].data)
            self.assertNotIn("image", payload)
            self.assertIn("本次未采集到截图。", payload["body"])
            self.assertNotIn("url", payload)

            notice_id = self.service.create_notice("连接测试", "文字通知")
            self.service.send_notice("event", notice_id)
            payload = json.loads(send.call_args.args[0].data)
            self.assertNotIn("image", payload)
            self.assertNotIn("url", payload)
            self.assertEqual(payload["body"], "文字通知")
            self.assertEqual(self.store.get("_notices")[0]["status"], "sent")

    def test_automatic_result_updates_plan_and_does_not_run_again(self):
        run_id = self.automatic_run()
        self.service.execute(run_id)
        self.service.tick()
        run = self.store.run(run_id)
        with self.store.connect() as db:
            plan = db.execute("SELECT * FROM plans WHERE id=?", (run["plan_id"],)).fetchone()
            self.assertEqual(plan["status"], "completed")
            self.assertEqual(db.execute("SELECT count(*) FROM runs WHERE plan_id=?", (plan["id"],)).fetchone()[0], 1)
        self.service.execute(run_id)
        self.assertEqual(self.adb.captures, 1)

    def test_empty_package_manual_run_uses_default_and_displays_lock_step(self):
        self.store.update({"package": "", "serial": ""})
        run_id = self.service.submit("full", "manual-default")
        self.assertNotIn("package", self.store.dispatch(run_id)["params"])
        self.service.execute(run_id)
        run = self.store.run(run_id)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["remote_result"]["package"], "com.example.default")
        self.assertEqual(run["steps"][-1], {"name": "锁定屏幕", "state": "completed", "error": ""})

    def test_empty_package_automatic_run_uses_default(self):
        self.store.update({"package": ""})
        run_id = self.automatic_run()
        self.assertEqual(self.store.run(run_id)["status"], "queued")
        self.assertIsNotNone(self.service.planner.next_plan())
        self.service.execute(run_id)
        run = self.store.run(run_id)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["remote_result"]["package"], "com.example.default")

    def test_pending_automatic_checks_pause_deadline_and_manual_priority(self):
        for change in ("pause", "late", "manual"):
            with self.subTest(change=change):
                # Reuse a single logical morning plan, resetting only this test's data.
                with self.store.connect(write=True) as db:
                    db.execute("DELETE FROM runs")
                    db.execute("DELETE FROM plans")
                self.service = self.new_service()
                self.store.update({"paused": False})
                run_id = self.automatic_run()
                if change == "pause":
                    self.store.update({"paused": True})
                elif change == "late":
                    self.at += timedelta(seconds=6)
                else:
                    self.service.submit("screenshot", "manual-priority")
                self.service.execute(run_id)
                self.assertEqual(self.store.run(run_id)["status"], "missed")
        self.assertEqual(self.adb.captures, 0)

    def test_restart_recovers_original_id_instead_of_posting_again(self):
        run_id = self.service.submit("screenshot", "restarted")
        dispatch = self.store.dispatch(run_id)
        dispatch["sent"] = True
        self.store.change_run(run_id, status="running", dispatch=dispatch)
        ControllerClient(self.url, fixtures.KEY).screenshot("restarted")
        self.store.update({"controller_url": "http://127.0.0.1:1"})
        restarted = self.new_service()
        restarted.recover()
        _, _, recovered_id = restarted.phone_queue.get_nowait()
        self.assertEqual(recovered_id, run_id)
        restarted.execute(recovered_id)
        self.assertEqual(self.store.run(run_id)["status"], "completed")
        self.assertEqual(self.adb.captures, 1)

    def test_lost_post_response_queries_and_saves_existing_result(self):
        class LostResponseClient(ControllerClient):
            def screenshot(self, request_id, **kwargs):
                super().screenshot(request_id, **kwargs)
                raise ControllerTransportError("模拟丢失响应", request_id=request_id, outcome_uncertain=True)

        service = self.new_service(client_factory=LostResponseClient)
        run_id = service.submit("screenshot", "lost-response")
        service.execute(run_id)
        self.assertEqual(self.store.run(run_id)["status"], "completed")
        self.assertTrue(self.store.run(run_id)["screenshot"])
        self.assertEqual(self.adb.captures, 1)

    def test_missing_request_and_expired_result_do_not_trigger_new_operations(self):
        missing = self.service.submit("screenshot", "never-accepted")
        dispatch = self.store.dispatch(missing)
        dispatch["sent"] = True
        self.store.change_run(missing, status="running", dispatch=dispatch)
        self.service.execute(missing)
        self.assertEqual(self.store.run(missing)["status"], "unknown")
        self.assertEqual(self.adb.captures, 0)
        expired = self.service.submit("screenshot", "expired-result")
        ControllerClient(self.url, fixtures.KEY).screenshot("expired-result")
        dispatch = self.store.dispatch(expired)
        dispatch["sent"] = True
        self.store.change_run(expired, status="running", dispatch=dispatch)
        with self.remote.store.connect() as db:
            db.execute("UPDATE requests SET expires_at=0 WHERE request_id='expired-result'")
        self.service.execute(expired)
        run = self.store.run(expired)
        self.assertEqual(run["status"], "completed")
        self.assertIn("过期", run["reason"])
        self.assertIsNone(run["screenshot"])
        self.assertEqual(self.adb.captures, 1)

    def test_busy_automatic_is_missed_and_manual_waits(self):
        self.adb.release_capture.clear()
        with ThreadPoolExecutor(max_workers=2) as executor:
            active = executor.submit(ControllerClient(self.url, fixtures.KEY).screenshot, "external-busy")
            try:
                self.assertTrue(self.adb.capture_started.wait(2))
                automatic = self.automatic_run()
                self.service.execute(automatic)
                self.assertEqual(self.store.run(automatic)["status"], "missed")
                manual = self.service.submit("full", "manual-waits")
                waiting = executor.submit(self.service.execute, manual)
                until = time.monotonic() + 2
                while time.monotonic() < until and "控制端忙" not in self.store.run(manual)["reason"]:
                    time.sleep(.01)
                self.assertIn("控制端忙", self.store.run(manual)["reason"])
            finally:
                self.adb.release_capture.set()
            active.result(2)
            waiting.result(2)
        self.assertEqual(self.store.run(manual)["status"], "completed")
        self.assertEqual(self.adb.captures, 2)

    def test_failure_keeps_local_screenshot_and_error_step(self):
        self.adb.fail_cleanup = True
        run_id = self.service.submit("full", "cleanup-failed")
        self.service.execute(run_id)
        run = self.store.run(run_id)
        self.assertEqual(run["status"], "failed")
        self.assertTrue(run["screenshot"])
        self.assertEqual(run["remote_result"]["error"]["step"], "home_after")
        self.assertEqual(len(run["remote_result"]["cleanup_errors"]), 3)

    def test_connection_changes_notify_once_and_offline_is_distinct_from_network(self):
        self.assertTrue(self.service.probe()["connected"])
        self.adb.devices = lambda: [{"serial": fixtures.SERIAL, "state": "unauthorized", "usable": False, "brand": None, "model": None}]
        current = self.service.probe()
        self.assertTrue(current["controller_connected"])
        self.assertFalse(current["connected"])
        self.assertIn("授权", current["reason"])
        count = len(self.store.get("_notices"))
        self.service.probe()
        self.assertEqual(len(self.store.get("_notices")), count)
        self.store.update({"controller_url": "http://127.0.0.1:1"})
        current = self.service.probe()
        self.assertFalse(current["controller_connected"])
        self.assertIsNone(current["connected"])
        self.assertEqual(len(self.store.get("_notices")), count + 1)

    def test_window_change_overrides_and_random_plans_stay_persistent(self):
        self.service.planner.ensure(self.at.date(), self.at.date())
        plans, _ = self.service.planner.plans(self.at.date(), self.at.date())
        first = plans[(self.at.date().isoformat(), "morning")]
        self.service.planner.ensure(self.at.date(), self.at.date())
        again, _ = self.service.planner.plans(self.at.date(), self.at.date())
        self.assertEqual(again[(first["day"], "morning")]["scheduled_at"], first["scheduled_at"])
        self.service.planner.override(self.at.date(), self.at.date(), "leave")
        plans, _ = self.service.planner.plans(self.at.date(), self.at.date())
        self.assertEqual(plans[(first["day"], "morning")]["status"], "cancelled")
        self.service.planner.override(self.at.date(), self.at.date(), "work")
        plans, _ = self.service.planner.plans(self.at.date(), self.at.date())
        self.assertNotEqual(plans[(first["day"], "morning")]["id"], first["id"])

    def test_legacy_database_upgrades_and_keeps_history(self):
        run_id = self.service.submit("screenshot", "old-local-run")
        self.store.update({"adb_path": "C:/old/adb.exe", "activity": ".OldActivity"})
        with self.store.connect(write=True) as db:
            db.execute("ALTER TABLE runs DROP COLUMN dispatch")
            db.execute("ALTER TABLE runs DROP COLUMN remote_result")
        upgraded = Store(self.store.directory)
        self.assertEqual(upgraded.run(run_id)["request_id"], "old-local-run")
        self.assertEqual(upgraded.dispatch(run_id), {})
        self.assertIsNone(upgraded.get("activity"))
        self.assertIsNone(upgraded.get("adb_path"))
        self.service.recover()
        self.assertEqual(upgraded.run(run_id)["status"], "cancelled")
        self.assertEqual(self.adb.captures, 0)

    def test_web_forms_pages_login_screenshot_and_secrets(self):
        app = create_app(store=self.store, service=self.service, secure_cookie=False)
        app.config["TESTING"] = True
        browser = app.test_client()
        self.assertEqual(browser.get("/screenshots/anything").status_code, 303)
        browser.get("/login")
        with browser.session_transaction() as session:
            csrf = session["csrf"]
        # A browser may fetch an unprotected page's favicon before submitting login.
        browser.get("/favicon.ico")
        self.assertEqual(browser.post("/login", data={"password": "tianhe", "csrf_token": csrf}).status_code, 303)
        with browser.session_transaction() as session:
            csrf = session["csrf"]
        self.service.probe()
        for page in ("/", "/settings/app", "/settings/device", "/settings/bark", "/settings/account", "/settings/schedule", "/plans", "/plans/edit", "/runs"):
            result = browser.get(page)
            self.assertEqual(result.status_code, 200, page)
            self.assertNotIn(fixtures.KEY.encode(), result.data)
            self.assertNotIn(b'href="/install"', result.data)
        self.assertEqual(browser.get("/install").status_code, 404)
        self.assertNotIn("桌面入口".encode(), browser.get("/").data)
        self.assertNotIn("添加到手机桌面".encode(), browser.get("/").data)
        result = browser.post("/settings/device", data={"controller_url": self.url + "/", "controller_api_key": "",
                              "serial": fixtures.SERIAL, "csrf_token": csrf, "request_id": "web-test", "action": "test-device"})
        run_id = result.location.rsplit("/", 1)[1]
        self.assertEqual(self.store.settings()["controller_api_key"], fixtures.KEY)
        self.service.execute(run_id)
        self.assertEqual(self.store.run(run_id)["status"], "completed")
        detail = browser.get(result.location)
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn(fixtures.KEY.encode(), detail.data)
        with browser.get("/screenshots/" + run_id) as image:
            self.assertEqual(image.data, fixtures.PNG)
        self.assertEqual(browser.post("/pause", data={"paused": "1"}).status_code, 400)
        browser.post("/settings/app", data={"package": "com.example.app", "wait_seconds": "0", "csrf_token": csrf})
        self.assertEqual(self.store.settings()["wait_seconds"], 0)
        self.assertNotIn(b"activity", browser.get("/settings/app").data)
        self.assertNotIn(b"adb_path", browser.get("/settings/device").data)
        browser.post("/settings/device", data={"controller_url": self.url, "serial": "not-yet-discovered", "csrf_token": csrf})
        self.assertEqual(self.store.settings()["serial"], "not-yet-discovered")
        browser.post("/settings/device", data={"controller_url": self.url, "serial": "", "csrf_token": csrf})
        self.assertEqual(self.store.settings()["serial"], "")
        browser.post("/settings/app", data={"package": "", "wait_seconds": "0", "csrf_token": csrf})
        data = {"kind": "full", "request_id": "web-default-package", "csrf_token": csrf}
        confirmation = browser.post("/runs", data=data)
        self.assertEqual(confirmation.status_code, 200)
        result = browser.post("/runs", data={**data, "answer": str(self.confirmation_sequence(confirmation))})
        run_id = result.location.rsplit("/", 1)[1]
        self.service.execute(run_id)
        self.assertEqual(self.store.run(run_id)["remote_result"]["package"], "com.example.default")
        self.assertIn("使用控制端默认包名".encode(), browser.get("/").data)
        self.assertNotIn("应用待配置".encode(), browser.get("/").data)

    def test_manual_confirmation_validates_six_digit_sequence_before_queueing(self):
        browser, csrf = self.web_browser()
        data = {"kind": "full", "request_id": "confirmed-manual", "csrf_token": csrf}
        self.assertEqual(browser.post("/runs", data={"kind": "full", "request_id": "no-csrf"}).status_code, 400)
        self.assertEqual(browser.application.test_client().post("/runs", data=data).status_code, 303)
        # A direct POST cannot supply its own challenge or skip the confirmation.
        with patch("src.web.secrets.SystemRandom") as rng:
            rng.return_value.sample.side_effect = [list("123456"), list("987654321")]
            result = browser.post("/runs", data={**data, "sequence": "2", "answer": "2", "confirmed": "1"})
        self.assertEqual(result.status_code, 400)
        answer = self.confirmation_sequence(result)
        self.assertEqual(answer, "123456")
        refreshed = browser.post("/runs", data=data)
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(self.confirmation_sequence(refreshed), answer)
        for invalid in ("", "12345", "1234567", "123465", "112345", "000000", "wrong", "9" * 5000):
            with self.subTest(answer=invalid[:20]):
                result = browser.post("/runs", data={**data, "answer": invalid, "sequence": invalid}, headers={"Accept": "application/json"})
                self.assertEqual(result.status_code, 400)
                self.assertEqual(self.confirmation_sequence(result), answer)
                self.assertIn("点选顺序不正确", result.get_json()["error"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.assertTrue(self.service.phone_queue.empty())
        self.assertEqual(self.adb.commands, [])
        accepted = browser.post("/runs", data={**data, "answer": answer})
        self.assertEqual(accepted.status_code, 303)
        run_id = accepted.location.rsplit("/", 1)[1]
        self.assertEqual(self.store.run(run_id)["status"], "queued")
        duplicate = browser.post("/runs", data={**data, "answer": answer})
        self.assertEqual(duplicate.location, accepted.location)
        self.assertEqual(self.service.phone_queue.qsize(), 1)
        self.service.execute(run_id)
        self.assertEqual(self.store.run(run_id)["status"], "completed")
        self.assertEqual(self.adb.captures, 1)
        result = browser.post("/runs", data={**data, "request_id": "next-manual", "answer": answer})
        self.assertEqual(result.status_code, 400)
        self.confirmation_sequence(result)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)

    def test_manual_confirmation_is_bound_to_session_request_and_can_be_cancelled(self):
        browser, csrf = self.web_browser()
        first = {"kind": "full", "request_id": "first-tab", "csrf_token": csrf}
        second = {**first, "request_id": "second-tab", "return_to": "https://untrusted.example"}
        with patch("src.web.secrets.SystemRandom") as rng:
            rng.return_value.sample.side_effect = [list("123456"), list("987654321"), list("654321"), list("123456789")]
            first_answer = self.confirmation_sequence(browser.post("/runs", data=first))
            second_answer = self.confirmation_sequence(browser.post("/runs", data=second))
        self.assertNotEqual(first_answer, second_answer)
        self.assertEqual(browser.post("/runs", data={**first, "answer": second_answer}).status_code, 400)
        self.assertEqual(browser.post("/runs", data={**first, "request_id": "changed-id", "answer": first_answer}).status_code, 400)
        other_browser, other_csrf = self.web_browser()
        self.assertEqual(other_browser.post("/runs", data={**first, "csrf_token": other_csrf, "answer": first_answer}).status_code, 400)
        cancelled = browser.post("/runs", data={**second, "action": "cancel"})
        self.assertEqual(cancelled.status_code, 303)
        self.assertEqual(cancelled.location, "/")
        result = browser.post("/runs", data={**second, "answer": second_answer})
        self.assertEqual(result.status_code, 400)
        self.confirmation_sequence(result)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.assertTrue(self.service.phone_queue.empty())
        self.assertEqual(self.adb.commands, [])
        accepted = browser.post("/runs", data={**first, "answer": first_answer})
        self.assertEqual(accepted.status_code, 303)
        self.assertEqual(self.service.phone_queue.qsize(), 1)

    def test_modal_json_challenge_confirm_cancel_and_old_challenge_upgrade(self):
        browser, csrf = self.web_browser()
        headers = {"Accept": "application/json"}
        data = {"kind": "full", "request_id": "modal-json", "csrf_token": csrf}
        result = browser.post("/runs", data=data, headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.is_json)
        answer = self.confirmation_sequence(result)
        self.assertNotIn(fixtures.KEY.encode(), result.data)
        partial = browser.post("/runs", data={**data, "answer": answer[:5]}, headers=headers)
        self.assertEqual(partial.status_code, 400)
        self.assertTrue(partial.is_json)
        self.assertTrue(self.service.phone_queue.empty())
        accepted = browser.post("/runs", data={**data, "answer": answer}, headers=headers)
        self.assertEqual(accepted.status_code, 200)
        location = accepted.get_json()["redirect"]
        self.assertTrue(location.startswith("/runs/"))
        duplicate = browser.post("/runs", data={**data, "answer": answer}, headers=headers)
        self.assertEqual(duplicate.get_json()["redirect"], location)
        self.assertEqual(self.service.phone_queue.qsize(), 1)
        run_id = location.rsplit("/", 1)[1]
        self.service.execute(run_id)
        self.assertEqual(self.adb.captures, 1)
        data["request_id"] = "cancel-json"
        answer = self.confirmation_sequence(browser.post("/runs", data=data, headers=headers))
        cancelled = browser.post("/runs", data={**data, "action": "cancel"}, headers=headers)
        self.assertEqual(cancelled.get_json(), {"cancelled": True})
        self.assertEqual(browser.post("/runs", data={**data, "answer": answer}, headers=headers).status_code, 400)
        with browser.session_transaction() as session:
            session["run_confirmations"] = {"legacy-question": {"left": 27, "right": 48, "back": "/"}}
        upgraded = browser.post("/runs", data={**data, "request_id": "legacy-question", "answer": "75"}, headers=headers)
        self.assertEqual(upgraded.status_code, 400)
        self.confirmation_sequence(upgraded)

    def test_history_retry_requires_confirmation_and_other_manual_kinds_stay_direct(self):
        original_id = self.service.submit("full", "original-history-run")
        self.service.execute(original_id)
        browser, csrf = self.web_browser()
        detail_path = "/runs/" + original_id
        detail = browser.get(detail_path)
        token = re.search(rb'name="request_id" value="([^"]+)"', detail.data).group(1).decode()
        self.assertNotEqual(token, "original-history-run")
        data = {"kind": "full", "request_id": token, "csrf_token": csrf, "return_to": detail_path}
        self.assertEqual(browser.post("/runs", data=data).status_code, 200)
        cancelled = browser.post("/runs", data={**data, "action": "cancel"})
        self.assertEqual(cancelled.location, detail_path)
        self.assertEqual(self.adb.captures, 1)
        answer = self.confirmation_sequence(browser.post("/runs", data=data))
        accepted = browser.post("/runs", data={**data, "answer": answer})
        new_id = accepted.location.rsplit("/", 1)[1]
        self.assertNotEqual(new_id, original_id)
        self.service.execute(new_id)
        self.assertEqual(self.store.run(original_id)["status"], "completed")
        self.assertEqual(self.adb.captures, 2)
        for kind in ("screenshot", "test"):
            result = browser.post("/runs", data={"kind": kind, "request_id": "direct-" + kind, "csrf_token": csrf})
            self.assertEqual(result.status_code, 303)
            run_id = result.location.rsplit("/", 1)[1]
            self.assertEqual(self.store.run(run_id)["kind"], kind)
            self.service.execute(run_id)
            self.assertEqual(self.store.run(run_id)["status"], "completed")

    def test_records_pagination_keeps_order_filters_and_requested_page_size(self):
        from flask import template_rendered

        with self.store.connect(write=True) as db:
            identifiers = [self.store.add_run(db, kind="full", status="completed", at=self.at + timedelta(seconds=index))
                           for index in range(17)]
        app = create_app(store=self.store, service=self.service, secure_cookie=False)
        browser = app.test_client()
        with browser.session_transaction() as session:
            session.update(authenticated=True, auth_version=self.store.get("_auth_version"))
        rendered = []

        def capture(sender, template, context, **extra):
            rendered.append(context)

        day = self.at.date().isoformat()
        with template_rendered.connected_to(capture, app):
            first = browser.get(f"/runs?per_page=7&from={day}&to={day}")
            self.assertEqual(first.status_code, 200)
            self.assertEqual([run["id"] for run in rendered[-1]["runs"]], list(reversed(identifiers))[:7])
            self.assertEqual(rendered[-1]["pages"], 3)
            self.assertIn(b"per_page=7", first.data)
            browser.get(f"/runs?page=2&per_page=7&from={day}&to={day}")
            self.assertEqual([run["id"] for run in rendered[-1]["runs"]], list(reversed(identifiers))[7:14])
            browser.get(f"/runs?page=99&per_page=7&from={day}&to={day}")
            self.assertEqual(rendered[-1]["page"], 3)
            self.assertEqual([run["id"] for run in rendered[-1]["runs"]], list(reversed(identifiers))[14:])
            tomorrow = (self.at.date() + timedelta(days=1)).isoformat()
            browser.get(f"/runs?per_page=7&from={tomorrow}&to={tomorrow}")
            self.assertEqual(rendered[-1]["runs"], [])
            self.assertEqual((rendered[-1]["page"], rendered[-1]["pages"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
