package com.sevencnup.devwisp

import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.OffsetDateTime
import java.util.Locale
import kotlin.math.abs

internal object TurnTiming {
    private val numericTimestamp = Regex("^-?\\d+(?:\\.\\d+)?$")

    fun timestamp(value: Any?): Long? {
        val text = (value as? String)?.trim()
        val numeric = when {
            value is Number -> value.toDouble()
            text != null && numericTimestamp.matches(text) -> text.toDoubleOrNull()
            else -> null
        }
        if (numeric != null) {
            val millis = if (numeric > 10_000_000_000.0) numeric else numeric * 1000.0
            return if (millis.isFinite() && abs(millis) <= 8_640_000_000_000_000.0) millis.toLong() else null
        }
        if (text.isNullOrBlank()) return null
        return runCatching { Instant.parse(text).toEpochMilli() }.getOrNull()
            ?: runCatching { OffsetDateTime.parse(text).toInstant().toEpochMilli() }.getOrNull()
    }

    /** Same priority and first-valid fallback as the web's latestTurnState. */
    fun startedAt(turn: JSONObject): Long? {
        firstTimestamp(turn, "startedAt", "started_at", "startedAtMs", "started_at_ms", "createdAt", "created_at")
            ?.let { return it }
        val items = turn.optJSONArray("items") ?: return null
        for (index in items.length() - 1 downTo 0) {
            val item = items.optJSONObject(index) ?: continue
            if (item.optString("type").replace("-", "").replace("_", "").lowercase(Locale.ROOT) == "usermessage") {
                return firstTimestamp(item, "createdAt", "created_at", "sentAt", "sent_at", "timestamp", "timestampMs", "timestamp_ms")
            }
        }
        return null
    }

    fun activeTurn(turns: JSONArray, pendingTurnId: String?): JSONObject {
        if (!pendingTurnId.isNullOrBlank()) {
            for (index in turns.length() - 1 downTo 0) {
                val turn = turns.optJSONObject(index) ?: continue
                if (turn.optString("id") == pendingTurnId) return turn
            }
            // A live start can arrive before history. Do not borrow the previous turn's time.
            return JSONObject().put("id", pendingTurnId)
        }
        return turns.optJSONObject(turns.length() - 1) ?: JSONObject()
    }

    private fun firstTimestamp(value: JSONObject, vararg keys: String): Long? {
        for (key in keys) timestamp(value.opt(key))?.let { return it }
        return null
    }
}

internal data class TurnTimerKey(val endpoint: String, val engine: String, val threadId: String, val turnId: String)

/** Retain a turn's fallback across rerenders, while allowing history to correct its start. */
internal class TurnElapsedTimer {
    private data class State(val startedAtMs: Long?, val monotonicStartMs: Long)
    private val states = object : LinkedHashMap<TurnTimerKey, State>(128, .75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<TurnTimerKey, State>?): Boolean = size > 128
    }

    fun observe(key: TurnTimerKey, startedAtMs: Long?, elapsedRealtimeMs: Long, wallTimeMs: Long, serverTimeMs: Long?) {
        val previous = states[key]
        if (previous != null && (startedAtMs == null || startedAtMs == previous.startedAtMs)) return
        val elapsed = startedAtMs?.let { ((serverTimeMs ?: wallTimeMs) - it).coerceAtLeast(0L) } ?: 0L
        states[key] = State(startedAtMs, elapsedRealtimeMs - elapsed)
    }

    fun elapsedMillis(key: TurnTimerKey, elapsedRealtimeMs: Long, serverTimeMs: Long?): Long {
        val state = states[key] ?: return 0L
        return if (state.startedAtMs != null && serverTimeMs != null) {
            (serverTimeMs - state.startedAtMs).coerceAtLeast(0L)
        } else {
            (elapsedRealtimeMs - state.monotonicStartMs).coerceAtLeast(0L)
        }
    }
}
