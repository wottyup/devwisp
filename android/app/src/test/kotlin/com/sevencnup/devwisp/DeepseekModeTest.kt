package com.sevencnup.devwisp

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class DeepseekModeTest {
    @Test fun deepseekPayloadUsesOwnPreferencesAndGenericModelFields() {
        val values = mapOf("model" to "codex-model", "claudeModel" to "opus", "deepseekModel" to "mercury-3",
            "deepseekEffort" to "instant", "deepseekPermissionMode" to "plan")
        val payload = ChatEngines.applyPreferences(JSONObject(), ChatEngines.DEEPSEEK, values::get, "ptc")
        assertEquals("mercury-3", payload.getString("model"))
        assertEquals("instant", payload.getString("effort"))
        assertEquals("ptc", payload.getString("deepseekAgentPreset"))
        assertEquals("plan", payload.getString("deepseekPermissionMode"))
        assertFalse(payload.has("claudeModel"))
        assertFalse(payload.has("approvalPolicy"))
        assertFalse(payload.has("claudePermissionMode"))
    }

    @Test fun deepseekDefaultsDoNotInheritOtherEngineModelsOrPermissions() {
        val values = mapOf("model" to "codex-model", "effort" to "xhigh", "claudeModel" to "opus", "approvalPolicy" to "never")
        val payload = ChatEngines.applyPreferences(JSONObject(), ChatEngines.DEEPSEEK, values::get)
        assertFalse(payload.has("model"))
        assertFalse(payload.has("effort"))
        assertEquals("standard", payload.getString("deepseekAgentPreset"))
        assertEquals("workspace-write", payload.getString("deepseekPermissionMode"))
    }

    @Test fun existingEnginesRetainTheirPayloads() {
        val values = mapOf("model" to "codex-model", "effort" to "high", "claudeModel" to "sonnet", "claudeEffort" to "max")
        val codex = ChatEngines.applyPreferences(JSONObject(), "codex", values::get)
        val claude = ChatEngines.applyPreferences(JSONObject(), "claude-code", values::get)
        assertEquals("codex-model", codex.getString("model"))
        assertEquals("on-request", codex.getString("approvalPolicy"))
        assertEquals("sonnet", claude.getString("claudeModel"))
        assertEquals("max", claude.getString("claudeEffort"))
        assertFalse(claude.has("model"))
        assertFalse(codex.has("deepseekAgentPreset"))
    }

    @Test fun restoringDeepseekIsIsolatedBetweenConnections() {
        val values = mutableMapOf<String, String>()
        val selections = ConnectionChatSelections(values::get) { values.putAll(it) }
        selections.save("one", null, ChatEngines.DEEPSEEK, "deepseek:one")
        selections.save("two", null, "claude-code", "claude:two")
        assertEquals(ConnectionChatSelections.Selection(ChatEngines.DEEPSEEK, "deepseek:one", false), selections.restore("one", null, "codex"))
        assertEquals("claude-code", selections.restore("two", null, ChatEngines.DEEPSEEK).mode)
        selections.save("one", null, ChatEngines.DEEPSEEK, null)
        assertTrue(selections.restore("one", null, "codex").explicitlyEmpty)
    }

    @Test fun legacyPerModeDeepseekSelectionIsRecovered() {
        val values = mutableMapOf("one_native_active_thread_deepseek-harness" to "deepseek:old")
        val selections = ConnectionChatSelections(values::get) { values.putAll(it) }
        assertEquals(ConnectionChatSelections.Selection(ChatEngines.DEEPSEEK, "deepseek:old", false), selections.restore("one", null, "codex"))
    }

    @Test fun threadIdentifiersKeepDeepseekBrandWhenMetadataIsAbsent() {
        assertEquals(ChatEngines.DEEPSEEK, ChatEngines.fromThreadId("deepseek:123"))
        assertEquals("DeepSeek Harness", ChatEngines.label(ChatEngines.fromThreadId("deepseek:123")))
        val transcript = ThreadTranscript.create(JSONObject("""{"id":"deepseek:123","turns":[{"items":[{"type":"agentMessage","text":"Hello"}]}]}"""))
        assertTrue(transcript.contains("DeepSeek"))
    }
}
