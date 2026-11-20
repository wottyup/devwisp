package com.sevencnup.devwisp

import java.net.IDN
import java.net.URI
import java.net.URLEncoder
import java.util.Locale
import org.json.JSONArray
import org.json.JSONObject

internal enum class BrowserSearchEngine(val label: String, private val endpoint: String) {
    GOOGLE("Google", "https://www.google.com/search?q="),
    BING("Bing", "https://www.bing.com/search?q="),
    DUCKDUCKGO("DuckDuckGo", "https://duckduckgo.com/?q=");

    fun search(query: String): String = endpoint + URLEncoder.encode(query, "UTF-8")
}

/** Address entry is separate from chat-link recognition: ordinary text becomes a search. */
internal object BrowserNavigation {
    private val scheme = Regex("^[a-zA-Z][a-zA-Z0-9+.-]*:")
    private val searchOperator = Regex("^(site|filetype|intitle|inurl|intext|before|after|related|cache):", RegexOption.IGNORE_CASE)
    private val percentEscape = Regex("%[0-9a-fA-F]{2}")

    /** Compare equivalent addresses without merging distinct queries or fragment-based app routes. */
    fun libraryKey(value: String): String? = runCatching {
        val uri = URI(webUrl(value) ?: return null)
        val protocol = uri.scheme.lowercase(Locale.ROOT)
        val host = uri.host?.lowercase(Locale.ROOT) ?: return null
        val port = uri.port.takeUnless { it == -1 || protocol == "http" && it == 80 || protocol == "https" && it == 443 }
        fun escapes(text: String): String = percentEscape.replace(text) { match ->
            val char = match.value.drop(1).toInt(16).toChar()
            if (char in 'a'..'z' || char in 'A'..'Z' || char in '0'..'9' || char in "-._~") char.toString()
            else match.value.uppercase(Locale.ROOT)
        }
        val path = escapes(uri.rawPath.orEmpty().ifEmpty { "/" })
        val query = uri.rawQuery?.let { "?${escapes(it)}" }.orEmpty()
        val fragment = uri.rawFragment?.takeIf { it.isNotEmpty() }?.let { "#${escapes(it)}" }.orEmpty()
        URI("$protocol://$host${port?.let { ":$it" }.orEmpty()}$path$query$fragment").normalize().toASCIIString()
    }.getOrNull()

    fun resolve(input: String, engine: BrowserSearchEngine): String? {
        val text = input.trim()
        if (text.isEmpty() || text.any { it.code < 32 }) return null
        if (searchOperator.containsMatchIn(text)) return engine.search(text)
        if (text.startsWith("//")) return webUrl("https:$text")
        if (text.startsWith("http://", true) || text.startsWith("https://", true)) return webUrl(text)
        val authority = text.substringBefore('/').substringBefore('?').substringBefore('#')
        val hostPort = Regex("^[^\\s:@]+:[0-9]+$").matches(authority)
        val protocol = scheme.find(text)?.value?.dropLast(1)?.lowercase(Locale.ROOT)
        if (protocol in setOf("http", "https", "javascript", "data", "file", "content", "intent", "about", "blob", "tel", "mailto", "ftp")) return null
        if (scheme.containsMatchIn(text) && !hostPort) return null
        val hostLike = text.none(Char::isWhitespace) &&
            (authority.contains('.') || authority == "localhost" || authority.startsWith('[') || hostPort)
        if (!hostLike) return engine.search(text)
        val local = authority == "localhost" || authority.startsWith("localhost:") ||
            Regex("^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+(?::[0-9]+)?$").matches(authority) || hostPort && !authority.contains('.')
        return webUrl("${if (local) "http" else "https"}://$text")
    }

    fun webUrl(value: String): String? = runCatching {
        if (value.any { it.isWhitespace() || it.code < 32 } || '\\' in value) return null
        val uri = URI(value)
        if (uri.scheme?.lowercase(Locale.ROOT) !in setOf("http", "https")) return null
        val authority = uri.rawAuthority ?: return null
        if ('@' in authority || authority.isBlank()) return null
        val host: String
        val port: String?
        if (authority.startsWith('[')) {
            val end = authority.indexOf(']')
            if (end < 0 || uri.host == null) return null
            host = authority.substring(0, end + 1)
            val suffix = authority.substring(end + 1)
            if (suffix.isNotEmpty() && !suffix.startsWith(':')) return null
            port = if (suffix.isEmpty()) null else suffix.drop(1)
        } else {
            if (authority.count { it == ':' } > 1) return null
            port = if (':' in authority) authority.substringAfter(':') else null
            host = IDN.toASCII(authority.substringBefore(':'), IDN.USE_STD3_ASCII_RULES).lowercase(Locale.ROOT)
            if (host.length > 253 || host.trimEnd('.').split('.').any { it.isEmpty() || it.length > 63 }) return null
            if (Regex("^[0-9.]+$").matches(host) &&
                (host.split('.').size != 4 || host.split('.').any { (it.toIntOrNull() ?: -1) !in 0..255 })) return null
        }
        if (port != null && (port.isEmpty() || port.any { !it.isDigit() } || (port.toIntOrNull() ?: -1) !in 1..65535)) return null
        "${uri.scheme.lowercase(Locale.ROOT)}://$host${port?.let { ":$it" }.orEmpty()}${uri.rawPath.orEmpty()}" +
            (uri.rawQuery?.let { "?$it" }.orEmpty()) + (uri.rawFragment?.let { "#$it" }.orEmpty())
    }.getOrNull()
}

internal data class BrowserLibraryEntry(val url: String, val title: String, val visitedAt: Long)

/** Local only; bounded, de-duplicated lists. Never store failed or internal WebView pages. */
internal class BrowserLibrary(bookmarksJson: String = "[]", historyJson: String = "[]") {
    val bookmarks = decode(bookmarksJson, BOOKMARK_LIMIT).toMutableList()
    val history = decode(historyJson, HISTORY_LIMIT).toMutableList()

    fun visit(url: String, title: String, now: Long = System.currentTimeMillis()) {
        val valid = BrowserNavigation.webUrl(url) ?: return
        val key = BrowserNavigation.libraryKey(valid) ?: return
        history.removeAll { BrowserNavigation.libraryKey(it.url) == key }
        history.add(0, BrowserLibraryEntry(valid, title.take(512), now))
        while (history.size > HISTORY_LIMIT) history.removeAt(history.lastIndex)
    }

    fun isBookmarked(url: String): Boolean {
        val key = BrowserNavigation.libraryKey(url) ?: return false
        return bookmarks.any { BrowserNavigation.libraryKey(it.url) == key }
    }

    fun toggleBookmark(url: String, title: String): Boolean {
        val valid = BrowserNavigation.webUrl(url) ?: return false
        val key = BrowserNavigation.libraryKey(valid) ?: return false
        if (bookmarks.removeAll { BrowserNavigation.libraryKey(it.url) == key }) return false
        bookmarks.add(0, BrowserLibraryEntry(valid, title.take(512), System.currentTimeMillis()))
        while (bookmarks.size > BOOKMARK_LIMIT) bookmarks.removeAt(bookmarks.lastIndex)
        return true
    }

    fun encode(entries: List<BrowserLibraryEntry>): String = JSONArray().also { array ->
        entries.distinctBy { BrowserNavigation.libraryKey(it.url) }.forEach {
            array.put(JSONObject().put("url", it.url).put("title", it.title).put("at", it.visitedAt))
        }
    }.toString()

    private fun decode(json: String, limit: Int): List<BrowserLibraryEntry> = runCatching {
        val array = JSONArray(json)
        (0 until array.length()).asSequence().mapNotNull { index ->
            val item = array.optJSONObject(index) ?: return@mapNotNull null
            val url = BrowserNavigation.webUrl(item.optString("url")) ?: return@mapNotNull null
            BrowserLibraryEntry(url, item.optString("title").take(512), item.optLong("at"))
        }.distinctBy { BrowserNavigation.libraryKey(it.url) }.take(limit).toList()
    }.getOrDefault(emptyList())

    companion object {
        const val BOOKMARK_LIMIT = 100
        const val HISTORY_LIMIT = 200
    }
}

/** Owns references, never reconstructs them on selection. The caller disposes removed resources. */
internal class BrowserPages<T : Any>(private val limit: Int = 8) {
    val items = mutableListOf<T>()
    var active: T? = null
        private set

    fun add(page: T): Boolean {
        if (items.any { it === page }) return select(page)
        if (items.size >= limit) return false
        items.add(page)
        active = page
        return true
    }

    fun select(page: T): Boolean {
        if (items.none { it === page }) return false
        active = page
        return true
    }

    fun remove(page: T): Boolean {
        val index = items.indexOfFirst { it === page }
        if (index < 0) return false
        items.removeAt(index)
        if (active === page) active = items.getOrNull(index.coerceAtMost(items.lastIndex))
        return true
    }

    fun clear(): List<T> = items.toList().also { items.clear(); active = null }
}
