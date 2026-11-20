package com.sevencnup.devwisp

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class HarnessRunMetricsTest {
    private fun snapshot(usage: JSONObject = JSONObject(), phase: String = "completed") = JSONObject()
        .put("id", "telemetry-turn").put("type", "harnessTelemetry").put("phase", phase)
        .put("continuity", "reused").put("usage", usage)

    @Test fun providerCountersStaySeparateWithoutRecountingReasoningOrCache() {
        val raw = snapshot(JSONObject().put("inputTokens", 100).put("outputTokens", 40)
            .put("cacheReadTokens", 80).put("cacheWriteTokens", 5).put("reasoningTokens", 30).put("totalTokens", 220))
            .put("usagePartial", true).put("children", JSONArray().put(JSONObject()
                .put("usage", JSONObject().put("totalTokens", 9999))))
        val metrics = HarnessRunMetrics.parse(raw)!!
        assertEquals(100L, metrics.usage["inputTokens"])
        assertEquals(40L, metrics.usage["outputTokens"])
        assertEquals(220L, metrics.usage["totalTokens"])
        assertEquals(30L, metrics.usage["reasoningTokens"])
        assertEquals(80L, metrics.usage["cacheReadTokens"])
        assertEquals(5L, metrics.usage["cacheWriteTokens"])
        assertTrue(metrics.usagePartial)
    }

    @Test fun absentUsageIsNotPresentedAsZeroAndTotalsAreNeverInvented() {
        val absent = HarnessRunMetrics.parse(snapshot().removeUsage())!!
        assertTrue(absent.usage.isEmpty())
        assertNull(absent.pressureTokens)
        assertNull(absent.contextWindow)
        assertNull(absent.contextPercent)
        val outputOnly = HarnessRunMetrics.parse(snapshot(JSONObject().put("outputTokens", 7)))!!
        assertEquals(mapOf("outputTokens" to 7L), outputOnly.usage)
        val zero = HarnessRunMetrics.parse(snapshot(JSONObject().put("inputTokens", 0).put("outputTokens", 0)))!!
        assertEquals(mapOf("inputTokens" to 0L, "outputTokens" to 0L), zero.usage)
    }

    @Test fun countersRequireNonnegativeSafeIntegersLikeTheWeb() {
        val raw = snapshot(JSONObject().put("inputTokens", -1).put("outputTokens", 1.5)
            .put("cacheReadTokens", "25").put("cacheWriteTokens", true)
            .put("reasoningTokens", Long.MAX_VALUE).put("totalTokens", 9_007_199_254_740_991L))
            .put("context", JSONObject().put("pressureTokens", "100").put("contextWindow", -2))
        val metrics = HarnessRunMetrics.parse(raw)!!
        assertEquals(mapOf("totalTokens" to 9_007_199_254_740_991L), metrics.usage)
        assertNull(metrics.pressureTokens)
        assertNull(metrics.contextWindow)
        assertNull(metrics.contextPercent)
    }

    @Test fun requestContextUsesOnlyReportedCapacityAndMatchesWebRounding() {
        fun context(pressure: Long, capacity: Long?) = HarnessRunMetrics.parse(snapshot().put("context",
            JSONObject().put("pressureTokens", pressure).putOpt("contextWindow", capacity)))!!
        assertEquals(13L, context(125, 1000).contextPercent)
        assertEquals(120L, context(1200, 1000).contextPercent)
        assertEquals(0L, context(0, 1000).contextPercent)
        assertNull(context(100, null).contextPercent)
        assertNull(context(100, 0).contextPercent)
        assertNull(HarnessRunMetrics.parse(snapshot().put("context", JSONObject().put("contextWindow", 1000)))!!.pressureTokens)
    }

    @Test fun historySelectsTheLatestSnapshotInsteadOfSummingUpdates() {
        val first = snapshot(JSONObject().put("totalTokens", 12), "responding")
        val latest = snapshot(JSONObject().put("totalTokens", 20))
        val turn = JSONObject().put("items", JSONArray().put(first).put(JSONObject().put("type", "agentMessage")
            .put("text", "Done")).put(latest).put(JSONObject().put("type", "commandExecution")))
        assertEquals(20L, HarnessRunMetrics.fromTurn(turn)!!.usage["totalTokens"])
        assertEquals("completed", HarnessRunMetrics.fromTurn(turn)!!.phase)
        val otherTurn = JSONObject().put("items", JSONArray().put(snapshot(JSONObject().put("totalTokens", 3))))
        assertEquals(3L, HarnessRunMetrics.fromTurn(otherTurn)!!.usage["totalTokens"])
        assertEquals(20L, HarnessRunMetrics.fromTurn(JSONObject(turn.toString()))!!.usage["totalTokens"])
    }

    @Test fun unknownOrMalformedTelemetryDoesNotCreateAFalseDetailsEntry() {
        assertNull(HarnessRunMetrics.parse(JSONObject()))
        assertNull(HarnessRunMetrics.parse(snapshot(phase = "untrusted phase text")))
        assertNull(HarnessRunMetrics.fromTurn(JSONObject().put("items", JSONArray()
            .put(snapshot()).put(snapshot(phase = "unknown")))))
        assertNull(HarnessRunMetrics.fromTurn(JSONObject().put("items", JSONArray().put(JSONObject().put("type", "agentMessage")))))
        assertNull(HarnessRunMetrics.fromTurn(JSONObject()))
        val sanitized = HarnessRunMetrics.parse(snapshot().put("continuity", "provider diagnostics").put("usagePartial", "true"))!!
        assertEquals("new", sanitized.continuity)
        assertFalse(sanitized.usagePartial)
    }

    @Test fun everySupportedLifecyclePhaseSurvivesHistoryAndReconnect() {
        HarnessRunMetrics.phaseLabels.keys.forEach { phase ->
            val turn = JSONObject().put("items", JSONArray().put(snapshot(phase = phase)))
            assertEquals(phase, HarnessRunMetrics.fromTurn(JSONObject(turn.toString()))!!.phase)
        }
        assertEquals("restored", HarnessRunMetrics.parse(snapshot().put("continuity", "restored"))!!.continuity)
    }

    private fun JSONObject.removeUsage(): JSONObject = apply { remove("usage") }
}
