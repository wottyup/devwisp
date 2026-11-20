package com.sevencnup.devwisp

import org.junit.Assert.*
import org.junit.Test

class TerminalScreenBufferTest {
    @Test fun onlyTheEmptyStateIsTranslatedWhileRealOutputStaysUntouched() {
        val buffer = TerminalScreenBuffer(40, 2)
        val waiting = UiEnglish.translate("等待终端输出…")
        assertEquals("Waiting for terminal output…", buffer.render(waitingText = waiting))
        buffer.append("等待终端输出…")
        assertEquals("等待终端输出…", buffer.render(waitingText = waiting).lines().first())
    }

    @Test fun pasteKeepsUnicodeAndWhitespaceAndDoesNotAppendEnter() {
        val buffer = TerminalScreenBuffer(40, 2)
        assertEquals("  你好 😀\r\tsecond\rthird  ", buffer.pasteInput("  你好 😀\r\n\tsecond\nthird  "))
        assertEquals("echo hello", buffer.pasteInput("echo hello"))
        assertEquals("", buffer.pasteInput(""))
    }

    @Test fun pasteFollowsTheRemoteModeEvenWhenTheModeSequenceIsSplit() {
        val buffer = TerminalScreenBuffer(40, 2)
        buffer.append("\u001b[?25;20")
        buffer.append("04h")
        assertEquals("\u001b[200~first\rsecond\u001b[201~", buffer.pasteInput("first\nsecond"))
        assertEquals("", buffer.pasteInput(""))
        buffer.append("\u001b[?2004l")
        assertEquals("first\rsecond", buffer.pasteInput("first\nsecond"))
        buffer.append("\u001b[2004h")
        assertEquals("plain", buffer.pasteInput("plain"))
    }

    @Test fun reconnectAndTerminalResetDiscardStalePasteModes() {
        val buffer = TerminalScreenBuffer(40, 2)
        buffer.append("\u001b[?2004h")
        buffer.prepareForReconnect()
        assertEquals("plain", buffer.pasteInput("plain"))
        buffer.append("\u001b[?2004h")
        assertEquals("\u001b[200~again\u001b[201~", buffer.pasteInput("again"))
        buffer.append("\u001bc")
        assertEquals("plain", buffer.pasteInput("plain"))
    }

    @Test fun outputThatScrollsOffScreenRemainsAvailableForLongCopies() {
        val buffer = TerminalScreenBuffer(20, 3)
        buffer.append((1..20).joinToString("\r\n") { "line-$it" })
        assertEquals((1..20).map { "line-$it" }, buffer.render().lines())
        assertEquals(17, buffer.historyLineCount)
        assertEquals(19 to 7, buffer.cursorPosition())
    }

    @Test fun tmuxScrollingRegionRetainsOutputAndExcludesItsStatusBar() {
        val buffer = TerminalScreenBuffer(30, 4)
        buffer.append("\u001b[4;1H[codex-remote status]\u001b[1;3r\u001b[H")
        buffer.append((1..9).joinToString("\r\n") { "command-$it" })
        assertEquals((1..9).map { "command-$it" }, buffer.render(hideStatusLine = true).lines())
        assertEquals(8 to 9, buffer.cursorPosition(hideStatusLine = true))
        assertFalse(buffer.render(hideStatusLine = true).contains("status"))
    }

    @Test fun historyIsBoundedWithoutDroppingTheLiveScreen() {
        val buffer = TerminalScreenBuffer(20, 3, historyLimit = 4)
        buffer.append((1..20).joinToString("\r\n") { "line-$it" })
        assertEquals((14..20).map { "line-$it" }, buffer.render().lines())
        assertEquals(4, buffer.historyLineCount)
        assertEquals(13L, buffer.discardedHistoryLines)
        assertEquals(6 to 7, buffer.cursorPosition())
    }

    @Test fun characterLimitAlsoBoundsVeryWideTerminalHistory() {
        val buffer = TerminalScreenBuffer(20, 2, historyLimit = 100, historyCharacterLimit = 18)
        buffer.append((1..9).joinToString("\r\n") { "long-line-$it" })
        assertEquals(listOf("long-line-7", "long-line-8", "long-line-9"), buffer.render().lines())
        assertEquals(1, buffer.historyLineCount)
    }

    @Test fun replacingProgressTextDoesNotAddDuplicateHistory() {
        val buffer = TerminalScreenBuffer(20, 3)
        buffer.append("progress 1%\r\u001b[2Kprogress 100%")
        assertEquals("progress 100%", buffer.render().lines()[0])
        assertEquals(0, buffer.historyLineCount)
    }

    @Test fun scrollingAnApplicationSubregionDoesNotPolluteShellHistory() {
        val buffer = TerminalScreenBuffer(20, 5)
        buffer.append("header\r\nA\r\nB\r\nC\r\nfooter\u001b[2;4r\u001b[4;1H\n\n")
        assertEquals(0, buffer.historyLineCount)
        assertEquals("header", buffer.render().lines()[0])
        assertEquals("footer", buffer.render().lines()[4])
    }

    @Test fun screenClearKeepsHistoryWhileExplicitHistoryClearAndResetRemoveIt() {
        val buffer = TerminalScreenBuffer(20, 2)
        buffer.append("first\r\nsecond\r\nthird")
        buffer.append("\u001b[2J\u001b[Hcurrent")
        assertEquals(listOf("first", "current", ""), buffer.render().lines())
        buffer.append("\u001b[3J")
        assertEquals(listOf("current", ""), buffer.render().lines())
        buffer.append("\r\nmore\r\nlast\u001bc")
        assertEquals(0, buffer.historyLineCount)
        assertTrue(buffer.render().isBlank())
    }

    @Test fun reconnectDiscardsAnIncompleteEscapeWithoutDiscardingOutput() {
        val buffer = TerminalScreenBuffer(20, 2)
        buffer.append("one\r\ntwo\r\nthree\u001b[")
        buffer.prepareForReconnect()
        buffer.append("!\r\nfour")
        assertEquals(listOf("one", "two", "three!", "four"), buffer.render().lines())
    }

    @Test fun resizePreservesRowsThatLeaveTheViewport() {
        val buffer = TerminalScreenBuffer(20, 5)
        buffer.append("one\r\ntwo\r\nthree\r\nfour")
        buffer.resize(20, 3)
        assertEquals(listOf("one", "two", "three", "four", ""), buffer.render().lines())
        assertEquals(3, buffer.rowCount)
        assertEquals(2, buffer.historyLineCount)
    }

    @Test fun copySnapshotRetainsUnicodeAndItsRangeWhileOutputAndHistoryAdvance() {
        val buffer = TerminalScreenBuffer(30, 2, historyLimit = 2)
        buffer.append("start\r\n你好 😀\r\nselected end")
        val snapshot = buffer.render()
        val start = snapshot.indexOf("你好")
        val end = snapshot.length
        buffer.append((1..20).joinToString("\r\n", prefix = "\r\n") { "new-$it" })
        assertEquals("你好 😀\nselected end", snapshot.substring(start, end))
        assertFalse(buffer.render().contains("selected end"))
        assertTrue(buffer.render().contains("new-20"))
    }
}
