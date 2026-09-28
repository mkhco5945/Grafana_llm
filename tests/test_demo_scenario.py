import io
import json
import unittest

from demo import app


class DemoScenarioApiTests(unittest.TestCase):
    def setUp(self):
        app.reset_scenario()

    @staticmethod
    def handler(path, payload=None):
        handler = object.__new__(app.Handler)
        handler.path = path
        captured = {}
        handler._json = lambda status, value: captured.update(status=status, value=value)
        handler._send = lambda status, body, content_type: captured.update(
            status=status, body=body, content_type=content_type
        )
        if payload is not None:
            encoded = json.dumps(payload).encode()
            handler.rfile = io.BytesIO(encoded)
            handler.headers = {"Content-Length": str(len(encoded))}
        return handler, captured

    def test_get_current_scenario(self):
        handler, captured = self.handler("/scenario")
        handler.do_GET()
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["value"]["preset"], "normal")
        self.assertAlmostEqual(captured["value"]["error_rate"], 0.08)

    def test_valid_update_and_validation(self):
        handler, captured = self.handler(
            "/scenario", {"error_rate": 0.25, "latency_min_seconds": 0.3, "latency_max_seconds": 1.5}
        )
        handler.do_POST()
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["value"]["preset"], "custom")
        self.assertEqual(captured["value"]["error_rate"], 0.25)

        handler, captured = self.handler("/scenario", {"error_rate": 1.2})
        handler.do_POST()
        self.assertEqual(captured["status"], 400)
        handler, captured = self.handler(
            "/scenario", {"latency_min_seconds": 2, "latency_max_seconds": 1}
        )
        handler.do_POST()
        self.assertEqual(captured["status"], 400)

    def test_preset_and_reset(self):
        handler, captured = self.handler("/scenario/preset", {"name": "high-errors"})
        handler.do_POST()
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["value"]["preset"], "high-errors")
        self.assertGreaterEqual(captured["value"]["error_rate"], 0.25)
        handler, captured = self.handler("/scenario/reset", {})
        handler.do_POST()
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["value"]["preset"], "normal")


if __name__ == "__main__":
    unittest.main()
