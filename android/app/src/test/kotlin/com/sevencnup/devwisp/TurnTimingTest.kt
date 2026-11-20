package com.sevencnup.devwisp

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class TurnTimingTest {
    private val start = 1_800_000_000_125L

    @Test fun timestampsKeepFractionalSecondsAndAcceptWebUnitsAndIsoOffsets() {
        for (value in listOf(1_800_000_000.125, "1800000000.125", start, " $start ",
            "2027-01-15T08:00:00.125Z", "2027-01-15T16:00:00.125+08:00")) {
            assertEquals(value.toString(), start, TurnTiming.timestamp(value))
        }
        for (value in listOf(null, JSONObject.NULL, "", "invalid", true, Double.NaN, Double.POSITIVE_INFINITY,
            "99999999999999999999999999999")) assertNull(TurnTiming.timestamp(value))
    }

    @Test fun startFieldsUseTheSameFirstValidPriorityAsTheWeb() {
        val turn = JSONObject().put("startedAt", "invalid").put("started_at", JSONObject.NULL)
            .put("startedAtMs", start).put("createdAt", start - 20_000L)
        assertEquals(start, TurnTiming.startedAt(turn))
        turn.put("startedAt", start + 1_000L)
        assertEquals(start + 1_000L, TurnTiming.startedAt(turn))
        for (field in listOf("startedAt", "started_at", "startedAtMs", "started_at_ms", "createdAt", "created_at")) {
            assertEquals(field, start, TurnTiming.startedAt(JSONObject().put(field, start)))
        }
    }

    @Test fun startFallsBackToTheLatestUserMessageWithEveryWebTimestampAlias() {
        for (field in listOf("createdAt", "created_at", "sentAt", "sent_at", "timestamp", "timestampMs", "timestamp_ms")) {
            val items = JSONArray().put(JSONObject().put("type", "userMessage").put("createdAt", start - 20_000))
                .put(JSONObject().put("type", "user_message").put(field, start))
                .put(JSONObject().put("type", "agentMessage").put("createdAt", start + 5_000))
            assertEquals(field, start, TurnTiming.startedAt(JSONObject().put("items", items)))
        }
        // Like the web, don't use an earlier user's timestamp when the latest user has none.
        val items = JSONArray().put(JSONObject().put("type", "userMessage").put("createdAt", start))
            .put(JSONObject().put("type", "user-message"))
        assertNull(TurnTiming.startedAt(JSONObject().put("items", items)))
    }

    @Test fun liveStartBeforeHistoryDoesNotInheritThePreviousTurn() {
        val previous = JSONObject().put("id", "old").put("startedAt", start).put("status", "completed")
        val history = JSONArray().put(previous)
        val current = TurnTiming.activeTurn(history, "new")
        assertEquals("new", current.optString("id"))
        assertNull(TurnTiming.startedAt(current))
        val newHistory = JSONObject().put("id", "new").put("startedAt", start + 30_000L)
        history.put(newHistory)
        assertSame(newHistory, TurnTiming.activeTurn(history, "new"))
        assertSame(newHistory, TurnTiming.activeTurn(history, null))
        assertNull(TurnTiming.startedAt(TurnTiming.activeTurn(JSONArray(), null)))
    }
}
