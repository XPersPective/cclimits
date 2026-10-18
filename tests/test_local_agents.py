"""Offline regression checks for local adapters and routing output."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import local_agents as L
import cclimits as C


class LocalAdapters(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        patch = mock.patch.object(Path, "home", return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.dict("os.environ", {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, name, value):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_enabled_zai_route_and_opencode_discovery(self):
        self.write(".zcode/v2/config.json", {"provider": {"builtin:zai-coding-plan": {
            "enabled": False, "options": {"baseURL": "https://api.z.ai/api/coding/paas/v4", "apiKey": "secret"}}}})
        self.assertIsNone(L.zai_key())
        self.write(".local/share/opencode/auth.json", {"zai": {"type": "api", "key": "secret"}})
        self.assertEqual(L.zai_key(), "secret")

    def test_inventory_keeps_catalog_separate_from_access_and_hides_secrets(self):
        self.write(".qwen/settings.json", {"env": {"API_KEY": "never-print"}, "model": {
            "name": "chosen", "baseUrl": "https://provider.example/v1"}, "modelProviders": {"openai": [
            {"id": "chosen", "baseUrl": "https://provider.example/v1"},
            {"id": "other-account", "baseUrl": "https://other.example/v1"}]}})
        with mock.patch.object(L.shutil, "which", side_effect=lambda name: "qwen.cmd" if name == "qwen" else None):
            report = L.inventory()
        self.assertEqual(report["cli_agents"]["qwen"]["models"], ["chosen"])
        self.assertEqual(report["cli_agents"]["qwen"]["access_status"], "unverified")
        self.assertNotIn("never-print", json.dumps(report))

    def test_brain_preserves_exhausted_weekly_and_rejects_stale(self):
        now = datetime.now(timezone.utc).isoformat()
        data = {"status": "ok", "observed_at": now, "quota_windows": [L.window("5h", 90), L.window("week", 0)]}
        self.assertEqual(L.brain_snapshot("test", data)["remaining_pct"], 0)
        for change in [{"stale_fallback": True}, {"observed_at": "2000-01-01T00:00:00Z"},
                       {"observed_at": "2099-01-01T00:00:00Z"}, {"quota_windows": []},
                       {"quota_windows": [L.window("x", 10), L.window("x", 10)]}]:
            with self.assertRaises(ValueError):
                L.brain_snapshot("test", dict(data, **change))
        for value in [True, "nan", "inf", -1, 101, None]:
            with self.assertRaises(ValueError):
                L.window("test", value)

    def test_kimi_selected_expired_account_never_calls_network(self):
        path = self.home / ".kimi-code/config.toml"
        path.parent.mkdir()
        path.write_text('[providers."managed:kimi-code".oauth]\nkey = "oauth/selected"\n', encoding="utf-8")
        self.write(".kimi-code/credentials/selected.json", {"access_token": "never-print", "expires_at": 1})
        request = mock.Mock()
        result = L.get_kimi_code_usage(request)
        self.assertEqual(result["error"], "Token expired")
        request.assert_not_called()
        self.assertNotIn("never-print", json.dumps(result))

    def test_kimi_keeps_all_reported_windows(self):
        request = mock.Mock(return_value=(200, {"usages": {
            "limit_5h": {"used_ratio": 0.2}, "limit_7d": {"used_ratio": 1.0}}}))
        with mock.patch.object(L, "kimi_code_credentials", return_value={"access_token": "secret"}):
            result = L.get_kimi_code_usage(request)
        self.assertEqual([w["remaining_pct"] for w in result["quota_windows"]], [80, 0])

    def test_minimax_uses_personal_billing_group_and_native_remaining_semantics(self):
        self.write(".minimax/local-runtime.auth.json", {"auth": {"accessToken": "never-print", "realUserID": "user"}})
        post = mock.Mock(return_value=(200, {"workspaces": [{"workspace_type": 0, "op_group_id": "personal"}]}))
        get = mock.Mock(return_value=(200, {"base_resp": {"status_code": 0}, "model_remains": [{
            "model_name": "MiniMax", "current_interval_total_count": 100,
            "current_interval_usage_count": 25, "current_weekly_remaining_percent": 0}]}))
        result = L.get_minimax_usage(get, post)
        self.assertEqual(get.call_args.args[1]["X-Group-Id"], "personal")
        self.assertEqual([w["remaining_pct"] for w in result["quota_windows"]], [25, 0])
        self.assertNotIn("never-print", json.dumps(result))
        post.return_value = (403, {})
        get.reset_mock()
        self.assertEqual(L.get_minimax_usage(get, post)["quota_status"], "unavailable")
        get.assert_not_called()

    def test_zai_keeps_multiple_windows_and_excludes_mcp(self):
        response = {"success": True, "data": {"limits": [
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 99},
            {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 5},
            {"type": "TIME_LIMIT", "percentage": 100}]}}
        with mock.patch.object(C, "get_zai_credentials", return_value="secret"), \
             mock.patch.object(C, "http_get", side_effect=[(200, response), (200, {})]):
            data = C.get_zai_usage()
        self.assertEqual(len(data["quota_windows"]), 2)
        self.assertEqual(data["token_quota"]["percentage"], 99)
        self.assertEqual(min(w["remaining_pct"] for w in data["quota_windows"]), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
