package com.sevencnup.devwisp

internal class TerminalScreenBuffer(private var columns: Int, private var rows: Int,
                                    private val historyLimit: Int = 2_000,
                                    private val historyCharacterLimit: Int = 500_000) {
    val columnCount: Int get() = columns
    val rowCount: Int get() = rows
    private var screen = MutableList(rows) { CharArray(columns) { ' ' } }
    private var cursorRow = 0
    private var cursorColumn = 0
    private var savedRow = 0
    private var savedColumn = 0
    private var scrollTop = 0
    private var scrollBottom = rows - 1
    private var state = 0
    private var receivedOutput = false
    private var cursorVisible = true
    private var bracketedPasteEnabled = false
    private val sequence = StringBuilder()
    private val history = ArrayDeque<String>()
    private var historyCharacters = 0
    val historyLineCount: Int get() = history.size
    var discardedHistoryLines: Long = 0
        private set

    fun append(value: String) {
        if (value.isNotEmpty()) receivedOutput = true
        value.forEach(::consume)
    }

    fun render(hideStatusLine: Boolean = false, waitingText: String = "等待终端输出…"): String =
        if (!receivedOutput && screen.all { line -> line.all { it == ' ' } }) waitingText
        else (history.asSequence() + screen.take(if (hideStatusLine) rows - 1 else rows).asSequence().map { line ->
            val text = String(line).trimEnd()
            if (hideStatusLine && text.startsWith("[codex-rem")) "" else text
        }).joinToString("\n")

    fun cursorPosition(hideStatusLine: Boolean = false): Pair<Int, Int>? =
        if (!receivedOutput || !cursorVisible || hideStatusLine && cursorRow >= rows - 1) null
        else (history.size + cursorRow) to cursorColumn.coerceIn(0, columns - 1)

    fun prepareForReconnect() {
        state = 0
        sequence.setLength(0)
        bracketedPasteEnabled = false
    }

    fun pasteInput(value: String): String {
        val normalized = value.replace("\r\n", "\n").replace('\n', '\r')
        return if (bracketedPasteEnabled && normalized.isNotEmpty()) "\u001b[200~$normalized\u001b[201~" else normalized
    }

    fun resize(newColumns: Int, newRows: Int) {
        if (newColumns == columns && newRows == rows) return
        val next = MutableList(newRows) { CharArray(newColumns) { ' ' } }
        val contentRows = minOf(rows - 1, newRows - 1)
        val maxSourceStart = (rows - 1 - contentRows).coerceAtLeast(0)
        val sourceStart = (cursorRow.coerceAtMost(rows - 2) - contentRows + 1).coerceIn(0, maxSourceStart)
        val copyColumns = minOf(columns, newColumns)
        for (row in 0 until sourceStart) remember(screen[row])
        for (row in 0 until contentRows) screen[sourceStart + row].copyInto(next[row], endIndex = copyColumns)
        screen[rows - 1].copyInto(next[newRows - 1], endIndex = copyColumns)
        cursorRow = if (cursorRow == rows - 1) newRows - 1 else (cursorRow - sourceStart).coerceIn(0, newRows - 2)
        savedRow = if (savedRow == rows - 1) newRows - 1 else (savedRow - sourceStart).coerceIn(0, newRows - 2)
        cursorColumn = cursorColumn.coerceAtMost(newColumns - 1)
        savedColumn = savedColumn.coerceAtMost(newColumns - 1)
        columns = newColumns
        rows = newRows
        screen = next
        scrollTop = 0
        scrollBottom = rows - 1
    }

    private fun consume(character: Char) {
        when (state) {
            0 -> when (character) {
                '\u001b' -> state = 1
                '\r' -> cursorColumn = 0
                '\n' -> newLine()
                '\b' -> cursorColumn = (cursorColumn - 1).coerceAtLeast(0)
                '\t' -> cursorColumn = ((cursorColumn / 8 + 1) * 8).coerceAtMost(columns - 1)
                '\u0000', '\u0007' -> Unit
                else -> if (character >= ' ') put(character)
            }
            1 -> when (character) {
                '[' -> { sequence.setLength(0); state = 2 }
                ']' -> { sequence.setLength(0); state = 3 }
                '7' -> { savedRow = cursorRow; savedColumn = cursorColumn; state = 0 }
                '8' -> { cursorRow = savedRow; cursorColumn = savedColumn; state = 0 }
                'c' -> { clear(resetCursor = true); state = 0 }
                'D' -> { newLine(resetColumn = false); state = 0 }
                'E' -> { newLine(); state = 0 }
                'M' -> { reverseIndex(); state = 0 }
                '(', ')', '#', '%' -> state = 4
                else -> state = 0
            }
            2 -> {
                if (character in '@'..'~') {
                    applyCsi(character, sequence.toString())
                    state = 0
                } else if (sequence.length < 64) sequence.append(character) else state = 0
            }
            3 -> when {
                character == '\u0007' -> state = 0
                character == '\u001b' -> state = 5
                sequence.length < 512 -> sequence.append(character)
                else -> state = 0
            }
            4 -> state = 0
            5 -> state = if (character == '\\') 0 else 3
        }
    }

    private fun put(character: Char) {
        if (cursorColumn >= columns) newLine()
        screen[cursorRow][cursorColumn] = character
        cursorColumn += 1
    }

    private fun newLine(resetColumn: Boolean = true) {
        if (cursorRow == scrollBottom) scrollUp(1)
        else if (cursorRow < rows - 1) cursorRow++
        else scrollUp(1, 0, rows - 1)
        if (resetColumn) cursorColumn = 0
    }

    private fun reverseIndex() {
        if (cursorRow == scrollTop) scrollDown(1)
        else cursorRow = (cursorRow - 1).coerceAtLeast(0)
    }

    private fun scrollUp(count: Int, top: Int = scrollTop, bottom: Int = scrollBottom) {
        repeat(count.coerceAtMost(bottom - top + 1)) {
            val removed = screen.removeAt(top)
            // tmux reserves the last row for its status bar. Other partial
            // scrolling regions are application widgets, not shell history.
            if (top == 0 && bottom >= rows - 2) remember(removed)
            screen.add(bottom, CharArray(columns) { ' ' })
        }
    }

    private fun remember(line: CharArray) {
        val text = String(line).trimEnd()
        history.addLast(text)
        historyCharacters += text.length + 1
        while (history.size > historyLimit || historyCharacters > historyCharacterLimit) {
            historyCharacters -= history.removeFirst().length + 1
            discardedHistoryLines++
        }
    }

    private fun clearHistory() {
        discardedHistoryLines += history.size
        history.clear()
        historyCharacters = 0
    }

    private fun scrollDown(count: Int) {
        repeat(count.coerceAtMost(scrollBottom - scrollTop + 1)) {
            screen.removeAt(scrollBottom)
            screen.add(scrollTop, CharArray(columns) { ' ' })
        }
    }

    private fun applyCsi(command: Char, raw: String) {
        val cleaned = raw.trimStart('?', '>', '!', '=')
        val values = cleaned.split(';').map { it.toIntOrNull() ?: 0 }
        if (raw.startsWith('?') && (command == 'h' || command == 'l')) {
            if (25 in values) cursorVisible = command == 'h'
            if (2004 in values) bracketedPasteEnabled = command == 'h'
            return
        }
        fun value(index: Int, fallback: Int = 1): Int = values.getOrNull(index)?.takeIf { it > 0 } ?: fallback
        when (command) {
            'A' -> cursorRow = (cursorRow - value(0)).coerceAtLeast(0)
            'B' -> cursorRow = (cursorRow + value(0)).coerceAtMost(rows - 1)
            'C' -> cursorColumn = (cursorColumn + value(0)).coerceAtMost(columns - 1)
            'D' -> cursorColumn = (cursorColumn - value(0)).coerceAtLeast(0)
            'E' -> { cursorRow = (cursorRow + value(0)).coerceAtMost(rows - 1); cursorColumn = 0 }
            'F' -> { cursorRow = (cursorRow - value(0)).coerceAtLeast(0); cursorColumn = 0 }
            'G' -> cursorColumn = (value(0) - 1).coerceIn(0, columns - 1)
            'H', 'f' -> {
                cursorRow = (value(0) - 1).coerceIn(0, rows - 1)
                cursorColumn = (value(1) - 1).coerceIn(0, columns - 1)
            }
            'd' -> cursorRow = (value(0) - 1).coerceIn(0, rows - 1)
            'J' -> eraseDisplay(values.firstOrNull() ?: 0)
            'K' -> eraseLine(values.firstOrNull() ?: 0)
            'P' -> deleteCharacters(value(0))
            '@' -> insertCharacters(value(0))
            'X' -> repeat(value(0).coerceAtMost(columns - cursorColumn)) { screen[cursorRow][cursorColumn + it] = ' ' }
            'S' -> scrollUp(value(0))
            'T' -> scrollDown(value(0))
            'r' -> {
                val top = value(0)
                val bottom = value(1, rows)
                if (top < bottom && bottom <= rows) {
                    scrollTop = top - 1
                    scrollBottom = bottom - 1
                    cursorRow = 0
                    cursorColumn = 0
                }
            }
            's' -> { savedRow = cursorRow; savedColumn = cursorColumn }
            'u' -> { cursorRow = savedRow; cursorColumn = savedColumn }
        }
    }

    private fun eraseDisplay(mode: Int) {
        when (mode) {
            1 -> {
                for (row in 0 until cursorRow) screen[row].fill(' ')
                for (column in 0..cursorColumn.coerceAtMost(columns - 1)) screen[cursorRow][column] = ' '
            }
            2 -> screen.forEach { it.fill(' ') }
            3 -> clearHistory()
            else -> {
                for (column in cursorColumn until columns) screen[cursorRow][column] = ' '
                for (row in cursorRow + 1 until rows) screen[row].fill(' ')
            }
        }
    }

    private fun eraseLine(mode: Int) {
        when (mode) {
            1 -> for (column in 0..cursorColumn.coerceAtMost(columns - 1)) screen[cursorRow][column] = ' '
            2 -> screen[cursorRow].fill(' ')
            else -> for (column in cursorColumn until columns) screen[cursorRow][column] = ' '
        }
    }

    private fun deleteCharacters(count: Int) {
        val line = screen[cursorRow]
        val amount = count.coerceAtMost(columns - cursorColumn)
        for (column in cursorColumn until columns - amount) line[column] = line[column + amount]
        for (column in columns - amount until columns) line[column] = ' '
    }

    private fun insertCharacters(count: Int) {
        val line = screen[cursorRow]
        val amount = count.coerceAtMost(columns - cursorColumn)
        for (column in columns - 1 downTo cursorColumn + amount) line[column] = line[column - amount]
        for (column in cursorColumn until cursorColumn + amount) line[column] = ' '
    }

    private fun clear(resetCursor: Boolean = false) {
        screen.forEach { it.fill(' ') }
        if (resetCursor) {
            clearHistory()
            cursorRow = 0
            cursorColumn = 0
            cursorVisible = true
            bracketedPasteEnabled = false
            scrollTop = 0
            scrollBottom = rows - 1
        }
    }
}
