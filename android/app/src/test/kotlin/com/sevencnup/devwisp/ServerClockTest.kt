package com.sevencnup.devwisp

import org.junit.Assert.*
import org.junit.Test

class ServerClockTest {
    private val endpoint = "http://computer"
    private val date = "Fri, 15 Jan 2027 08:00:00 GMT"
    private val epoch = 1_800_000_000_000L

    @Test fun timeAdvancesWithMonotonicTimeWithoutReadingThePhoneClock() {
        val clock = ServerClock()
        clock.observe(endpoint, date, 10_000L, 10_100L)
        assertEquals(epoch + 550L, clock.nowMillis(endpoint, 10_100L))
        assertEquals(epoch + 10_550L, clock.nowMillis(endpoint, 20_100L))
        assertEquals(epoch + 550L, clock.nowMillis("$endpoint/", 10_100L))
        assertNull(clock.nowMillis(endpoint, 9_000L))
    }

    @Test fun missingInvalidAndSlowResponsesDoNotReplaceAGoodEstimate() {
        val clock = ServerClock()
        assertNull(clock.nowMillis(endpoint, 10_000L))
        clock.observe(endpoint, date, 10_000L, 10_100L)
        clock.observe(endpoint, null, 11_000L, 11_050L)
        clock.observe(endpoint, "not a date", 12_000L, 12_050L)
        clock.observe(endpoint, date, 13_000L, 16_000L)
        clock.observe(endpoint, date, 17_000L, 16_999L)
        assertEquals(epoch + 10_550L, clock.nowMillis(endpoint, 20_100L))
    }

    @Test fun lowLatencySamplesWinUntilTheRefreshWindowExpires() {
        val clock = ServerClock()
        clock.observe(endpoint, date, 10_000L, 10_100L)
        clock.observe(endpoint, date, 11_000L, 11_200L)
        assertEquals(epoch + 1_650L, clock.nowMillis(endpoint, 11_200L))
        clock.observe(endpoint, date, 12_000L, 12_050L)
        assertEquals(epoch + 525L, clock.nowMillis(endpoint, 12_050L))
        clock.observe(endpoint, "Fri, 15 Jan 2027 08:01:00 GMT", 73_000L, 73_200L)
        assertEquals(epoch + 60_600L, clock.nowMillis(endpoint, 73_200L))
        // An older request finishing out of order cannot overwrite this sample.
        clock.observe(endpoint, date, 12_000L, 12_010L)
        assertEquals(epoch + 60_600L, clock.nowMillis(endpoint, 73_200L))
    }

    @Test fun responsesFromAnotherEndpointCannotChangeTheActiveServersTime() {
        val clock = ServerClock()
        clock.observe(endpoint, date, 10_000L, 10_100L)
        assertNull(clock.nowMillis("http://other", 10_100L))
        clock.observe("http://other", "Fri, 15 Jan 2027 08:00:15 GMT", 10_000L, 10_100L)
        assertEquals(epoch + 550L, clock.nowMillis(endpoint, 10_100L))
        assertEquals(epoch + 15_550L, clock.nowMillis("http://other", 10_100L))
    }
}
