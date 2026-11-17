from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from devwisp.ssh_mcp_settings import SshMcpSettingsError, SshMcpSettingsStore


class SshMcpSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SshMcpSettingsStore(Path(directory.name))

    @staticmethod
    def profile(**changes):
        return {
            "name": "test-server", "host": "192.0.2.1", "port": 22,
            "user": "test-user", "auth": "password", "group": "dev",
            "role": "viewer", "readOnly": True, "approvalPolicy": "ask-all",
            **changes,
        }

    def test_password_permissions_survive_save_reopen_and_runtime_config(self) -> None:
        profiles = [
            self.profile(
                name=f"{role}-{policy}", role=role, readOnly=role == "viewer",
                approvalPolicy=policy,
            )
            for role in ("viewer", "operator", "admin")
            for policy in ("auto", "ask-all", "ask-destructive", "deny")
        ]
        passwords = {profile["name"]: "dummy-test-password" for profile in profiles}
        result = self.store.save(profiles, passwords)
        self.assertEqual(result["profiles"], [{**profile, "hasPassword": True} for profile in profiles])

        # Submitting settings again without a password keeps both credentials and policy.
        reopened = SshMcpSettingsStore(self.store.directory.parent)
        saved = reopened.save(result["profiles"])
        self.assertEqual(saved["profiles"], result["profiles"])
        self.assertEqual(reopened.runtime_password_environment(), {
            f"SSH_MCP_{profile['name'].upper().replace('-', '_')}_PASSWORD": "dummy-test-password"
            for profile in profiles
        })

        # Exercise the same config generation used when a Codex conversation opens.
        self.assertIsNotNone(reopened.codex_server_config())
        config_text = reopened.config_path.read_text(encoding="utf-8")
        generated = tomllib.loads(config_text)["profiles"]
        self.assertEqual(generated, profiles)
        self.assertNotIn("dummy-test-password", config_text)
        self.assertNotIn("dummy-test-password", reopened.profile_path.read_text(encoding="utf-8"))
        self.assertNotIn("dummy-test-password", json.dumps(saved))

    def test_existing_auth_group_and_extra_readonly_restrictions_are_preserved(self) -> None:
        profiles = [
            self.profile(name="password-prod", group="prod", role="admin", readOnly=False, approvalPolicy="deny"),
            self.profile(name="agent-staging", auth="agent", group="staging", role="operator", readOnly=False),
            self.profile(name="key-readonly", auth="key", keyRef="/dummy/key", role="admin", readOnly=True),
        ]
        self.store.save(profiles, {"password-prod": "dummy-test-password"})
        saved = self.store.save(self.store.list_profiles())
        self.assertEqual(saved["profiles"], [
            {**profile, "hasPassword": profile["auth"] == "password"} for profile in profiles
        ])
        self.assertIsNotNone(self.store.codex_server_config())
        self.assertEqual(tomllib.loads(self.store.config_path.read_text(encoding="utf-8"))["profiles"], profiles)

    def test_changing_password_does_not_reset_selected_permissions(self) -> None:
        profile = self.profile(role="operator", readOnly=False, approvalPolicy="ask-destructive")
        self.store.save([profile], {profile["name"]: "dummy-old-password"})
        result = self.store.save([profile], {profile["name"]: "dummy-new-password"})
        self.assertEqual(result["profiles"], [{**profile, "hasPassword": True}])
        self.assertEqual(self.store.runtime_password_environment(), {"SSH_MCP_TEST_SERVER_PASSWORD": "dummy-new-password"})

    def test_new_password_profile_still_requires_a_password(self) -> None:
        with self.assertRaises(SshMcpSettingsError):
            self.store.save([self.profile()])
        self.assertFalse(self.store.profile_path.exists())
        self.assertFalse(self.store.credentials_path.exists())

    def test_display_name_changes_preserve_credentials_and_runtime_identity(self) -> None:
        from devwisp.app import SshMcpProfileRequest

        original = self.profile()
        self.store.save([original], {original["name"]: "dummy-test-password"})
        credentials = self.store.runtime_password_environment()
        for display_name in ("家里 NAS", "测试服务器", ""):
            request = SshMcpProfileRequest.model_validate({**original, "displayName": display_name})
            profile = request.model_dump(exclude_none=True, exclude={"password"})
            saved = self.store.save([profile])
            reopened = SshMcpSettingsStore(self.store.directory.parent)
            self.assertEqual(reopened.list_profiles(), saved["profiles"])
            self.assertEqual(saved["profiles"][0].get("displayName"), display_name or None)
            self.assertTrue(saved["profiles"][0]["hasPassword"])
            self.assertEqual(reopened.runtime_password_environment(), credentials)
            self.assertIsNotNone(reopened.codex_server_config())
            # ssh-mcp rejects unknown TOML fields; UI names must stay in app metadata.
            config = tomllib.loads(reopened.config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["defaults"]["defaultProfile"], original["name"])
            self.assertEqual(config["profiles"], [original])

    def test_remove_only_target_and_its_password_from_latest_saved_settings(self) -> None:
        target = self.profile(name="remove-me")
        keep = self.profile(name="keep-me")
        self.store.save([target, keep], {"remove-me": "dummy-remove-password", "keep-me": "dummy-keep-password"})

        # A newer save must survive deletion from a page with an older snapshot.
        updated = {**keep, "displayName": "最新名称", "approvalPolicy": "deny"}
        added = self.profile(name="added-later", auth="agent")
        writer = SshMcpSettingsStore(self.store.directory.parent)
        writer.save([target, updated, added])
        result = self.store.remove(target["name"])
        self.assertEqual(result["profiles"], [
            {**updated, "hasPassword": True}, {**added, "hasPassword": False},
        ])
        reopened = SshMcpSettingsStore(self.store.directory.parent)
        self.assertEqual(reopened.list_profiles(), result["profiles"])
        self.assertEqual(reopened.runtime_password_environment(), {"SSH_MCP_KEEP_ME_PASSWORD": "dummy-keep-password"})
        self.assertNotIn("dummy-remove-password", reopened.credentials_path.read_text(encoding="utf-8"))
        self.assertIsNotNone(reopened.codex_server_config())
        runtime_names = [profile["name"] for profile in tomllib.loads(reopened.config_path.read_text(encoding="utf-8"))["profiles"]]
        self.assertEqual(runtime_names, ["keep-me", "added-later"])

    def test_removing_last_profile_clears_settings_and_stays_empty_after_reopen(self) -> None:
        profile = self.profile()
        self.store.save([profile], {profile["name"]: "dummy-test-password"})
        self.assertIsNotNone(self.store.codex_server_config())
        result = self.store.remove(profile["name"])
        self.assertEqual(result["profiles"], [])
        self.assertFalse(result["configured"])
        reopened = SshMcpSettingsStore(self.store.directory.parent)
        self.assertEqual(reopened.list_profiles(), [])
        self.assertEqual(reopened.runtime_password_environment(), {})
        self.assertIsNone(reopened.codex_server_config())
        self.assertEqual(reopened.remove(profile["name"])["profiles"], [])
        for path in (reopened.profile_path, reopened.credentials_path, reopened.config_path):
            self.assertFalse(path.exists())

    def test_removing_missing_profile_leaves_existing_files_unchanged(self) -> None:
        profile = self.profile()
        self.store.save([profile], {profile["name"]: "dummy-test-password"})
        self.assertIsNotNone(self.store.codex_server_config())
        paths = (self.store.profile_path, self.store.credentials_path, self.store.config_path)
        before = [path.read_bytes() for path in paths]
        result = self.store.remove("already-removed")
        self.assertEqual(result["profiles"], [{**profile, "hasPassword": True}])
        self.assertEqual([path.read_bytes() for path in paths], before)


if __name__ == "__main__":
    unittest.main()
