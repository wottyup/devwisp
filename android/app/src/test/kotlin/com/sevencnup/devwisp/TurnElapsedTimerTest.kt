package com.sevencnup.devwisp

import org.junit.Assert.*
import org.junit.Test

class TurnElapsedTimerTest {
    private val key = TurnTimerKey("http://computer", "codex", "thread", "turn")
    private val epoch = 1_800_000_000_000L

    @Test fun phoneClockAheadOrBehindDoesNotAffectCalibratedElapsedTime() {
        for (skew in listOf(-15_000L, 0L, 15_000L)) {
            val timer = TurnElapsedTimer()
            timer.observe(key, epoch - 10_000L, 50_000L, epoch + skew, epoch)
            assertEquals(10_000L, timer.elapsedMillis(key, 50_000L, epoch))
            assertEquals(11_000L, timer.elapsedMillis(key, 51_000L, epoch + 1_000L))
        }
    }

    @Test fun calibrationArrivingAfterFirstRenderCorrectsThePhoneClockOffset() {
        val timer = TurnElapsedTimer()
        timer.observe(key, epoch - 10_000L, 50_000L, epoch + 15_000L, null)
        assertEquals(25_000L, timer.elapsedMillis(key, 50_000L, null))
        assertEquals(11_000L, timer.elapsedMillis(key, 51_000L, epoch + 1_000L))
    }

    @Test fun delayedHistoryCorrectsMissingOrWrongStartForTheSameTurn() {
        val timer = TurnElapsedTimer()
        timer.observe(key, null, 50_000L, epoch, epoch)
        assertEquals(2_000L, timer.elapsedMillis(key, 52_000L, epoch + 2_000L))
        timer.observe(key, epoch - 10_000L, 52_000L, epoch + 2_000L, epoch + 2_000L)
        assertEquals(12_000L, timer.elapsedMillis(key, 52_000L, epoch + 2_000L))
        timer.observe(key, epoch - 5_000L, 53_000L, epoch + 3_000L, epoch + 3_000L)
        assertEquals(8_000L, timer.elapsedMillis(key, 53_000L, epoch + 3_000L))
        timer.observe(key, null, 53_000L, epoch + 3_000L, epoch + 3_000L)
        assertEquals(9_000L, timer.elapsedMillis(key, 54_000L, epoch + 4_000L))
    }

    @Test fun rerendersAndWallClockChangesDoNotResetMonotonicFallbacks() {
        val timer = TurnElapsedTimer()
        timer.observe(key, null, 50_000L, epoch, null)
        timer.observe(key, null, 52_000L, epoch + 500_000L, null)
        assertEquals(2_000L, timer.elapsedMillis(key, 52_000L, null))
        // A delayed UI tick/background pause must include real elapsed time without counting ticks.
        assertEquals(360_000L, timer.elapsedMillis(key, 410_000L, null))
        val known = key.copy(turnId = "known")
        timer.observe(known, epoch - 5_000L, 50_000L, epoch, null)
        timer.observe(known, epoch - 5_000L, 52_000L, epoch - 500_000L, null)
        assertEquals(7_000L, timer.elapsedMillis(known, 52_000L, null))
    }

    @Test fun backToBackTurnsAndThreadSwitchesRetainTheirOwnFallbacks() {
        val timer = TurnElapsedTimer()
        timer.observe(key, epoch - 30_000L, 50_000L, epoch, epoch)
        val next = key.copy(turnId = "next")
        timer.observe(next, null, 50_000L, epoch, epoch)
        assertEquals(0L, timer.elapsedMillis(next, 50_000L, epoch))
        assertEquals(30_000L, timer.elapsedMillis(key, 50_000L, epoch))
        for (other in listOf(next.copy(threadId = "other"), next.copy(engine = "deepseek"),
            next.copy(engine = "claude-code"), next.copy(endpoint = "http://other"))) {
            timer.observe(other, null, 55_000L, epoch + 5_000L, null)
            assertEquals(0L, timer.elapsedMillis(other, 55_000L, null))
        }
        timer.observe(next, null, 57_000L, epoch + 7_000L, epoch + 7_000L)
        assertEquals(7_000L, timer.elapsedMillis(next, 57_000L, epoch + 7_000L))
    }

    @Test fun futureStartsNeverDisplayNegativeElapsedTime() {
        val timer = TurnElapsedTimer()
        timer.observe(key, epoch + 1_000L, 50_000L, epoch, epoch)
        assertEquals(0L, timer.elapsedMillis(key, 50_000L, epoch))
        assertEquals(1_000L, timer.elapsedMillis(key, 52_000L, epoch + 2_000L))
    }
}
