package com.sevencnup.devwisp

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.text.Selection
import android.text.Spannable
import android.view.ActionMode
import android.view.Menu
import android.view.MenuItem
import android.view.MotionEvent
import android.view.ViewConfiguration
import android.widget.TextView

internal class TerminalOutputView(context: Context) : TextView(context) {
    var onSelectionStarted: (() -> Unit)? = null
    var onSelectionFinished: (() -> Unit)? = null
    private var selectionMode: ActionMode? = null
    val isSelecting: Boolean get() = selectionMode != null || hasSelection()
    private var selectionScroll = false
    private var selectionScrollMoved = false
    private var touchX = 0f
    private var touchY = 0f
    private var initialScrollX = 0
    private var initialScrollY = 0
    private val touchSlop = ViewConfiguration.get(context).scaledTouchSlop

    init {
        setTextIsSelectable(true)
        isVerticalScrollBarEnabled = true
        customSelectionActionModeCallback = object : ActionMode.Callback {
            override fun onCreateActionMode(mode: ActionMode, menu: Menu): Boolean {
                selectionMode = mode
                updateCursor(null)
                onSelectionStarted?.invoke()
                return true
            }
            override fun onPrepareActionMode(mode: ActionMode, menu: Menu) = false
            override fun onActionItemClicked(mode: ActionMode, item: MenuItem) = false
            override fun onDestroyActionMode(mode: ActionMode) {
                selectionMode = null
                (text as? Spannable)?.let(Selection::removeSelection)
                post { onSelectionFinished?.invoke() }
            }
        }
    }

    /** The terminal keeps receiving output while the selected snapshot stays fixed. */
    fun renderSnapshot(value: String, cursor: Pair<Int, Int>?): Boolean {
        if (isSelecting) return false
        if (text.toString() != value) text = value
        updateCursor(cursor)
        return true
    }

    fun finishSelection() {
        selectionMode?.finish()
        (text as? Spannable)?.let(Selection::removeSelection)
        clearFocus()
        onSelectionFinished?.invoke()
    }

    fun selectedOrAllText(): String {
        val value = text.toString()
        val start = minOf(selectionStart, selectionEnd)
        val end = maxOf(selectionStart, selectionEnd)
        return if (start >= 0 && end > start && end <= value.length) value.substring(start, end) else value
    }

    private fun maximumScrollY(): Int = ((layout?.height ?: 0) - height + compoundPaddingTop + compoundPaddingBottom).coerceAtLeast(0)
    fun isAtBottom(): Boolean = scrollY >= maximumScrollY() - paint.fontSpacing
    fun scrollToBottom() = scrollTo(0, maximumScrollY())
    fun scrollToPosition(x: Int, y: Int) {
        val maxX = ((layout?.let { value -> (0 until value.lineCount).maxOfOrNull { value.getLineRight(it) } } ?: 0f)
            - width + compoundPaddingLeft + compoundPaddingRight).toInt().coerceAtLeast(0)
        scrollTo(x.coerceIn(0, maxX), y.coerceIn(0, maximumScrollY()))
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (event.actionMasked == MotionEvent.ACTION_DOWN && isSelecting) {
            selectionScroll = true
            selectionScrollMoved = false
            touchX = event.x
            touchY = event.y
            initialScrollX = scrollX
            initialScrollY = scrollY
            parent?.requestDisallowInterceptTouchEvent(true)
            return true
        }
        if (selectionScroll) {
            when (event.actionMasked) {
                MotionEvent.ACTION_MOVE -> {
                    val dx = touchX - event.x
                    val dy = touchY - event.y
                    if (kotlin.math.abs(dx) > touchSlop || kotlin.math.abs(dy) > touchSlop) selectionScrollMoved = true
                    if (selectionScrollMoved) scrollToPosition(initialScrollX + dx.toInt(), initialScrollY + dy.toInt())
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    selectionScroll = false
                    parent?.requestDisallowInterceptTouchEvent(false)
                    selectionMode?.invalidateContentRect()
                }
            }
            return true
        }
        return super.onTouchEvent(event)
    }

    private var cursorRow = -1
    private var cursorColumn = -1
    private var cursorOn = true
    private val cursorPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val blink = object : Runnable {
        override fun run() {
            if (cursorRow < 0 || !isAttachedToWindow) return
            cursorOn = !cursorOn
            invalidate()
            postDelayed(this, 550L)
        }
    }

    fun updateCursor(position: Pair<Int, Int>?) {
        val nextRow = position?.first ?: -1
        val nextColumn = position?.second ?: -1
        if (nextRow == cursorRow && nextColumn == cursorColumn) return
        cursorRow = nextRow
        cursorColumn = nextColumn
        cursorOn = true
        removeCallbacks(blink)
        if (cursorRow >= 0 && isAttachedToWindow) postDelayed(blink, 550L)
        invalidate()
    }

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        if (cursorRow >= 0) postDelayed(blink, 550L)
    }

    override fun onDetachedFromWindow() {
        removeCallbacks(blink)
        super.onDetachedFromWindow()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        if (!cursorOn || cursorRow < 0) return
        val textLayout = layout ?: return
        if (cursorRow >= textLayout.lineCount) return
        val start = textLayout.getLineStart(cursorRow)
        val end = textLayout.getLineEnd(cursorRow)
        val contentEnd = if (end > start && text[end - 1] == '\n') end - 1 else end
        val textColumn = cursorColumn.coerceAtMost(contentEnd - start)
        val density = resources.displayMetrics.density
        val cursorWidth = density * 2.5f
        val x = (compoundPaddingLeft + textLayout.getPrimaryHorizontal(start + textColumn) +
            (cursorColumn - textColumn) * paint.measureText(" "))
            .coerceAtMost(width - compoundPaddingRight - cursorWidth)
        val top = compoundPaddingTop + textLayout.getLineTop(cursorRow) + density * 2f
        val bottom = compoundPaddingTop + textLayout.getLineBottom(cursorRow) - density * 2f
        if (bottom <= top) return
        cursorPaint.color = currentTextColor
        canvas.drawRoundRect(x, top, x + cursorWidth, bottom, density, density, cursorPaint)
    }
}
