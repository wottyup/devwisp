package com.sevencnup.devwisp

import org.json.JSONObject
import kotlin.math.roundToLong

/** Provider-reported root metrics only. Missing counters remain absent, including totals. */
internal data class HarnessRunMetrics(
    val phase: String,
    val continuity: String,
    val usage: Map<String, Long>,
    val usagePartial: Boolean,
    val pressureTokens: Long?,
    val contextWindow: Long?,
) {
    val contextPercent: Long?
        get() = if (pressureTokens != null && contextWindow != null && contextWindow > 0)
            (100.0 * pressureTokens / contextWindow).roundToLong() else null

    companion object {
        val phaseLabels = mapOf(
            "starting" to "正在启动 DeepSeek", "thinking" to "正在思考", "responding" to "正在回复",
            "tool" to "正在调用工具", "waiting-approval" to "等待权限批准", "waiting-answer" to "等待你的回答",
            "completed" to "已完成", "failed" to "执行失败", "stopped" to "已停止",
        )
        val usageLabels = linkedMapOf(
            "inputTokens" to "输入（未缓存）", "outputTokens" to "输出", "cacheReadTokens" to "缓存读取",
            "cacheWriteTokens" to "缓存写入", "reasoningTokens" to "思考 Token", "totalTokens" to "总计",
        )

        fun fromTurn(turn: JSONObject): HarnessRunMetrics? {
            val items = turn.optJSONArray("items") ?: return null
            for (index in items.length() - 1 downTo 0) {
                val item = items.optJSONObject(index) ?: continue
                if (item.optString("type") == "harnessTelemetry") return parse(item)
            }
            return null
        }

        fun parse(raw: JSONObject): HarnessRunMetrics? {
            val phase = raw.opt("phase") as? String ?: return null
            if (phase !in phaseLabels) return null
            val usage = raw.optJSONObject("usage")
            val context = raw.optJSONObject("context")
            return HarnessRunMetrics(
                phase,
                raw.optString("continuity").takeIf { it == "reused" || it == "restored" } ?: "new",
                usageLabels.keys.mapNotNull { key -> counter(usage, key)?.let { key to it } }.toMap(),
                raw.opt("usagePartial") == true,
                counter(context, "pressureTokens"), counter(context, "contextWindow"),
            )
        }

        private fun counter(raw: JSONObject?, key: String): Long? {
            val number = raw?.opt(key) as? Number ?: return null
            val value = number.toDouble()
            return if (value.isFinite() && value >= 0 && value <= 9_007_199_254_740_991.0 &&
                value == value.toLong().toDouble()) value.toLong() else null
        }
    }
}
