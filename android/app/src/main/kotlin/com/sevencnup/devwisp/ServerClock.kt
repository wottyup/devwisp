package com.sevencnup.devwisp

import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter

/** HTTP dates have second precision. Never use the phone wall clock to advance a sample. */
internal class ServerClock {
    private data class Sample(val serverAtReceiveMs: Long, val receivedMs: Long, val roundTripMs: Long)
    private val samples = object : LinkedHashMap<String, Sample>(16, .75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Sample>?): Boolean = size > 16
    }

    @Synchronized
    fun observe(endpoint: String, date: String?, sentMs: Long, receivedMs: Long) {
        val roundTripMs = receivedMs - sentMs
        if (roundTripMs !in 0L..2_000L || date == null) return
        val serverMs = runCatching {
            ZonedDateTime.parse(date, DateTimeFormatter.RFC_1123_DATE_TIME).toInstant().toEpochMilli()
        }.getOrNull() ?: return
        val key = endpoint.trimEnd('/')
        val previous = samples[key]
        if (previous != null && (receivedMs < previous.receivedMs ||
                receivedMs - previous.receivedMs < 60_000L && roundTripMs >= previous.roundTripMs)) return
        // Use the middle of the date's one-second bucket and approximate one-way latency.
        samples[key] = Sample(serverMs + 500L + roundTripMs / 2L, receivedMs, roundTripMs)
    }

    @Synchronized
    fun nowMillis(endpoint: String, elapsedRealtimeMs: Long): Long? {
        val sample = samples[endpoint.trimEnd('/')] ?: return null
        if (elapsedRealtimeMs < sample.receivedMs) return null
        return sample.serverAtReceiveMs + elapsedRealtimeMs - sample.receivedMs
    }
}
