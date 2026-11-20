package com.sevencnup.devwisp

import org.json.JSONObject

internal object ChatEngines {
    const val DEEPSEEK = "deepseek-harness"
    val all = listOf("codex", "claude-code", DEEPSEEK)
    val preferenceKeys = listOf("model", "effort", "claudeModel", "claudeEffort", "approvalPolicy",
        "claudePermissionMode", "deepseekModel", "deepseekEffort", "deepseekAgentPreset", "deepseekPermissionMode")
    val agents = listOf("standard" to "Standard", "ptc" to "PTC", "minimal" to "Minimal", "cordis" to "Creator")
    val permissions = listOf("read-only" to "只读", "workspace-write" to "工作区写入",
        "full-access-ask" to "完全访问（需批准）", "danger-full-access" to "完全访问（不询问）", "plan" to "计划模式（审核后执行）")

    fun label(engine: String) = when (engine) {
        DEEPSEEK -> "DeepSeek Harness"
        "claude-code" -> "Claude Code"
        else -> "Codex"
    }

    fun fromThreadId(id: String) = when {
        id.startsWith("deepseek:") -> DEEPSEEK
        id.startsWith("claude:") -> "claude-code"
        else -> "codex"
    }

    fun modelKey(engine: String) = when (engine) {
        DEEPSEEK -> "deepseekModel"
        "claude-code" -> "claudeModel"
        else -> "model"
    }

    fun effortKey(engine: String) = when (engine) {
        DEEPSEEK -> "deepseekEffort"
        "claude-code" -> "claudeEffort"
        else -> "effort"
    }

    fun queueKey(key: String) = when (key) {
        "deepseekModel" -> "model"
        "deepseekEffort" -> "effort"
        else -> key
    }

    fun applyPreferences(payload: JSONObject, engine: String, read: (String) -> String?, agent: String = "standard"): JSONObject {
        listOf(modelKey(engine), effortKey(engine)).forEach { key ->
            read(key)?.takeIf { it.isNotBlank() && it != "default" }?.let { payload.put(queueKey(key), it) }
        }
        when (engine) {
            DEEPSEEK -> payload.put("deepseekAgentPreset", agent)
                .put("deepseekPermissionMode", read("deepseekPermissionMode") ?: "workspace-write")
            "claude-code" -> payload.put("claudePermissionMode", read("claudePermissionMode") ?: "default")
            else -> payload.put("approvalPolicy", read("approvalPolicy") ?: "on-request")
        }
        return payload
    }
}
