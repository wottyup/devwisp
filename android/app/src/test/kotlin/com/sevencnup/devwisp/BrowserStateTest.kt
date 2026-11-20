package com.sevencnup.devwisp

import org.junit.Assert.*
import org.junit.Test

class BrowserStateTest {
    private fun resolve(text: String) = BrowserNavigation.resolve(text, BrowserSearchEngine.BING)

    @Test fun addressesAndLocalDevelopmentServersStayAddresses() {
        assertEquals("https://example.com/docs?q=a%2Bb#part", resolve(" example.com/docs?q=a%2Bb#part "))
        assertEquals("https://example.com", resolve("HTTPS://EXAMPLE.COM"))
        assertEquals("https://example.com/path", resolve("//example.com/path"))
        assertEquals("http://localhost:3000/a", resolve("localhost:3000/a"))
        assertEquals("http://10.126.126.2:8080", resolve("10.126.126.2:8080"))
        assertEquals("http://devbox:5173", resolve("devbox:5173"))
        assertEquals("http://[::1]:8080/a", resolve("http://[::1]:8080/a"))
        assertEquals("https://[2001:db8::1]/", resolve("[2001:db8::1]/"))
        assertEquals("https://xn--fsqu00a.xn--0zwm56d/", resolve("例子.测试/"))
    }

    @Test fun searchTermsAndOperatorsAreEncodedForTheChosenEngine() {
        assertEquals("https://www.bing.com/search?q=hello+world+%26+Kotlin", resolve("hello world & Kotlin"))
        assertEquals("https://www.bing.com/search?q=site%3Aexample.com+webview", resolve("site:example.com webview"))
        assertEquals("https://duckduckgo.com/?q=%E4%B8%AD%E6%96%87", BrowserNavigation.resolve("中文", BrowserSearchEngine.DUCKDUCKGO))
        assertEquals("https://www.google.com/search?q=hello", BrowserNavigation.resolve("hello", BrowserSearchEngine.GOOGLE))
        assertEquals("https://www.bing.com/search?q=release+1.2+notes", resolve("release 1.2 notes"))
    }

    @Test fun unsafeOrMalformedAddressesDoNotBecomeNavigationOrSearch() {
        listOf("", "javascript:alert(1)", "javascript:123", "data:text/html,x", "file:///tmp/a", "content://files/a",
            "intent://x", "custom://host", "mailto:user@example.com", "https:443", "https://user:password@example.com",
            "https://example.com:0", "https://example.com:65536", "https://example.com:abc", "https://example.com:",
            "https://example.com/a b", "https://example.com/%zz", "https://", "https://bad..host", "https://999.1.1.1",
            "https://example.com\\@evil.test", "hello\nworld").forEach { assertNull(it, resolve(it)) }
    }

    @Test fun switchingAndRemovingPagesRetainsTheActualObjects() {
        data class Page(val title: String)
        val pages = BrowserPages<Page>()
        val first = Page("same")
        val second = Page("same")
        val third = Page("third")
        pages.add(first); pages.add(second); pages.add(third)
        assertTrue(pages.select(first))
        assertSame(first, pages.active)
        assertTrue(pages.remove(second))
        assertSame(first, pages.active)
        assertTrue(pages.remove(first))
        assertSame(third, pages.active)
        assertFalse(pages.select(second))
        assertEquals(listOf(third), pages.clear())
        assertNull(pages.active)
        assertTrue(pages.items.isEmpty())
        assertFalse(pages.remove(first))
    }

    @Test fun pageLimitDoesNotEvictOrChangeAnExistingPage() {
        val pages = BrowserPages<Any>(2)
        val first = Any(); val second = Any()
        assertTrue(pages.add(first)); assertTrue(pages.add(second))
        assertFalse(pages.add(Any()))
        assertSame(second, pages.active)
        assertEquals(2, pages.items.size)
        assertTrue(pages.add(first))
        assertSame(first, pages.active)
        assertEquals(2, pages.items.size)
        pages.remove(first); pages.remove(second)
        assertNull(pages.active)
    }

    @Test fun historyIsRecentFirstDeduplicatedAndBounded() {
        val library = BrowserLibrary()
        repeat(210) { library.visit("https://example.com/$it", "Page $it", it.toLong()) }
        library.visit("https://example.com/100", "Updated", 999)
        assertEquals(200, library.history.size)
        assertEquals(BrowserLibraryEntry("https://example.com/100", "Updated", 999), library.history.first())
        assertEquals(1, library.history.count { it.url == "https://example.com/100" })
        assertFalse(library.history.any { it.url == "https://example.com/0" })
        library.visit("about:blank", "Internal")
        library.visit("file:///secret", "Private")
        assertEquals(200, library.history.size)
    }

    @Test fun bookmarksPersistIndependentlyAndCanBeRemoved() {
        val library = BrowserLibrary()
        assertTrue(library.toggleBookmark("https://EXAMPLE.com/", "Title"))
        library.visit("https://other.test", "Other", 123)
        val restored = BrowserLibrary(library.encode(library.bookmarks), library.encode(library.history))
        assertTrue(restored.isBookmarked("https://example.com/"))
        assertEquals("Other", restored.history.first().title)
        assertFalse(restored.toggleBookmark("https://example.com/", "New title"))
        assertTrue(restored.bookmarks.isEmpty())
        assertFalse(restored.toggleBookmark("javascript:x", "Invalid"))
        assertEquals(1, restored.history.size)
    }

    @Test fun corruptOrExcessivePersistedDataDoesNotBreakTheLibrary() {
        assertTrue(BrowserLibrary("not json", "{}").bookmarks.isEmpty())
        val json = """[{"url":"javascript:x"},{"url":"https://example.com","title":"First"},{"url":"https://example.com","title":"Duplicate"},null]"""
        val library = BrowserLibrary(json, json)
        assertEquals(1, library.bookmarks.size)
        assertEquals("First", library.bookmarks.first().title)
        repeat(105) { library.toggleBookmark("https://example.com/$it", "x".repeat(600)) }
        val restored = BrowserLibrary(library.encode(library.bookmarks))
        assertEquals(100, restored.bookmarks.size)
        assertEquals(512, restored.bookmarks.first().title.length)
    }

    @Test fun equivalentBookmarkAddressesShareOneIdentity() {
        val library = BrowserLibrary()
        assertTrue(library.toggleBookmark("https://EXAMPLE.com:443", "Home"))
        assertTrue(library.isBookmarked("https://example.com/"))
        assertFalse(library.toggleBookmark("https://example.com/#", "Home again"))
        assertTrue(library.bookmarks.isEmpty())

        assertTrue(library.toggleBookmark("http://example.com:80/a/../%7euser?q=%41#part", "Profile"))
        assertTrue(library.isBookmarked("http://example.com/~user?q=A#part"))
        assertFalse(library.toggleBookmark("http://example.com/~user?q=A#part", "Profile again"))
        assertTrue(library.bookmarks.isEmpty())
        assertEquals(BrowserNavigation.libraryKey("https://example.com/中文"), BrowserNavigation.libraryKey("https://example.com/%e4%b8%ad%e6%96%87"))
        assertEquals(BrowserNavigation.libraryKey("http://[::1]:80"), BrowserNavigation.libraryKey("http://[::1]/"))
    }

    @Test fun bookmarkIdentityPreservesDifferentPagesAndAppRoutes() {
        val library = BrowserLibrary()
        val addresses = listOf("https://example.com/Docs", "https://example.com/docs", "https://example.com/docs/",
            "https://example.com/docs?q=one", "https://example.com/docs?q=two", "https://example.com/docs#/one",
            "https://example.com/docs#/two", "http://example.com/docs", "https://example.com:8443/docs",
            "https://example.com/a%2Fb", "https://example.com/a/b")
        addresses.forEach { assertTrue(it, library.toggleBookmark(it, it)) }
        assertEquals(addresses.size, library.bookmarks.size)
        addresses.forEach { assertTrue(it, library.isBookmarked(it)) }
        assertNull(BrowserNavigation.libraryKey("javascript:alert(1)"))
    }

    @Test fun persistedDuplicateBookmarksAreCleanedWithoutLosingDistinctRoutes() {
        val json = """[{"url":"https://EXAMPLE.com:443","title":"First"},{"url":"https://example.com/","title":"Duplicate"},{"url":"https://example.com/#","title":"Another duplicate"},{"url":"https://example.com/#/chat","title":"Chat"}]"""
        val library = BrowserLibrary(json, json)
        assertEquals(2, library.bookmarks.size)
        assertEquals("First", library.bookmarks.first().title)
        assertEquals(2, library.history.size)
        val restored = BrowserLibrary(library.encode(library.bookmarks))
        assertEquals(2, restored.bookmarks.size)
        assertTrue(restored.isBookmarked("https://example.com/"))
        assertTrue(restored.isBookmarked("https://example.com/#/chat"))
    }

    @Test fun repeatedEquivalentBookmarkTapsNeverAccumulateDuplicates() {
        val library = BrowserLibrary()
        val variants = listOf("https://example.com", "https://EXAMPLE.com:443/", "https://example.com/#")
        repeat(60) { index ->
            library.toggleBookmark(variants[index % variants.size], "Home")
            assertEquals(if (index % 2 == 0) 1 else 0, library.bookmarks.size)
        }
        library.visit(variants[0], "First visit", 1)
        library.visit(variants[1], "Latest visit", 2)
        assertEquals(1, library.history.size)
        assertEquals("Latest visit", library.history.first().title)
    }
}
