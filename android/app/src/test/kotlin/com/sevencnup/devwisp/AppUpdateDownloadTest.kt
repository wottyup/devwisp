package com.sevencnup.devwisp

import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.io.Closeable
import java.io.IOException
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.file.Files
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread

class AppUpdateDownloadTest {
    private val directory = Files.createTempDirectory("devwisp-update-test").toFile()
    private val payload = ByteArray(150_321) { (it % 251).toByte() }
    private val digest = MessageDigest.getInstance("SHA-256").digest(payload).joinToString("") { "%02x".format(it) }
    private val client = OkHttpClient.Builder().connectTimeout(2, TimeUnit.SECONDS)
        .readTimeout(2, TimeUnit.SECONDS).callTimeout(5, TimeUnit.SECONDS).build()
    private var server: FixtureServer? = null

    @After fun close() {
        server?.close()
        client.connectionPool.evictAll()
        client.dispatcher.executorService.shutdownNow()
        directory.deleteRecursively()
    }

    private fun fixture(handle: (FixtureExchange) -> Unit): AppUpdateSpec {
        val listener = FixtureServer(handle)
        server = listener
        return AppUpdateSpec("0.133.5-dev.1", "devwisp-android-0.133.5-dev.1-debug.apk", payload.size.toLong(), digest,
            "http://127.0.0.1:${listener.port}/update.apk")
    }

    private fun FixtureExchange.reply(code: Int, bytes: ByteArray) {
        sendResponseHeaders(code, bytes.size.toLong())
        responseBody.write(bytes)
    }

    private fun partial(spec: AppUpdateSpec) = File(directory, spec.file(directory).name + ".part")

    @Test fun appRequestSendsAuthenticationAndPublishesOnlyTheVerifiedCompleteApk() {
        val authorization = AtomicReference<String>()
        val encoding = AtomicReference<String>()
        val spec = fixture {
            authorization.set(it.requestHeaders.getFirst("Authorization"))
            encoding.set(it.requestHeaders.getFirst("Accept-Encoding"))
            it.reply(200, payload)
        }
        val phases = mutableListOf<AppUpdatePhase>()
        val result = AppUpdateTransfer(client).download(spec, directory, "Bearer fixture-update") {
            phases += it.phase
            if (it.phase != AppUpdatePhase.READY) assertFalse(spec.file(directory).exists())
        }
        assertEquals("Bearer fixture-update", authorization.get())
        assertEquals("identity", encoding.get())
        assertArrayEquals(payload, result.readBytes())
        assertFalse(partial(spec).exists())
        assertTrue(phases.contains(AppUpdatePhase.DOWNLOADING))
        assertEquals(listOf(AppUpdatePhase.VERIFYING, AppUpdatePhase.READY), phases.takeLast(2))
    }

    @Test fun interruptedTransfersResumeFromDiskWithAValidatedRange() {
        val attempts = AtomicInteger()
        val range = AtomicReference<String>()
        val prefix = 16_384
        val spec = fixture {
            if (attempts.incrementAndGet() == 1) {
                it.sendResponseHeaders(200, payload.size.toLong())
                it.responseBody.write(payload, 0, prefix)
            } else {
                range.set(it.requestHeaders.getFirst("Range"))
                it.responseHeaders.add("Content-Range", "bytes $prefix-${payload.lastIndex}/${payload.size}")
                it.reply(206, payload.copyOfRange(prefix, payload.size))
            }
        }
        assertThrows(IOException::class.java) { AppUpdateTransfer(client).download(spec, directory, "Bearer fixture") {} }
        assertFalse(spec.file(directory).exists())
        assertEquals(prefix.toLong(), partial(spec).length())
        val result = AppUpdateTransfer(client).download(spec, directory, "Bearer fixture") {}
        assertEquals("bytes=$prefix-", range.get())
        assertArrayEquals(payload, result.readBytes())
    }

    @Test fun serverIgnoringRangeRestartsInsteadOfAppendingTheFullFile() {
        val spec = fixture { it.reply(200, payload) }
        partial(spec).writeBytes(payload.copyOf(8192))
        assertArrayEquals(payload, AppUpdateTransfer(client).download(spec, directory, "Bearer fixture") {}.readBytes())
    }

    @Test fun anInvalidResumeRangeCannotPublishOrOverwritePartialData() {
        val spec = fixture {
            it.responseHeaders.add("Content-Range", "bytes 0-${payload.lastIndex}/${payload.size}")
            it.reply(206, payload)
        }
        partial(spec).writeBytes(payload.copyOf(8192))
        assertThrows(AppUpdateFailure::class.java) { AppUpdateTransfer(client).download(spec, directory, "Bearer fixture") {} }
        assertEquals(8192L, partial(spec).length())
        assertFalse(spec.file(directory).exists())
    }

    @Test fun checksumAndOversizedResponsesNeverBecomeInstallableFiles() {
        val content = AtomicReference(payload.copyOf().apply { this[0] = 42 })
        val spec = fixture { it.reply(200, content.get()) }
        assertThrows(AppUpdateFailure::class.java) { AppUpdateTransfer(client).download(spec, directory, "Bearer fixture") {} }
        assertFalse(partial(spec).exists())
        assertFalse(spec.file(directory).exists())
        content.set(payload + byteArrayOf(1))
        assertThrows(AppUpdateFailure::class.java) { AppUpdateTransfer(client).download(spec, directory, "Bearer fixture") {} }
        assertFalse(spec.file(directory).exists())
    }

    @Test fun expiredPairingIsReportedWithoutWritingTheErrorBodyAsAnApk() {
        val spec = fixture { it.reply(401, "unauthorized".toByteArray()) }
        val failure = assertThrows(AppUpdateFailure::class.java) { AppUpdateTransfer(client).download(spec, directory, "Bearer fixture") {} }
        assertEquals("配对已失效，请重新连接电脑", failure.messageKey)
        assertFalse(spec.file(directory).exists())
        assertFalse(partial(spec).exists())
    }

    @Test fun cancellationDuringStreamingStopsTheTransferAndRemovesPartialData() {
        val spec = fixture { it.reply(200, payload) }
        val transfer = AppUpdateTransfer(client)
        assertThrows(IOException::class.java) {
            transfer.download(spec, directory, "Bearer fixture") { if (it.bytes > 0) transfer.cancel() }
        }
        assertFalse(spec.file(directory).exists())
        assertFalse(partial(spec).exists())
    }

    @Test fun alreadyVerifiedDownloadsCanBeRecoveredWithoutNetworkAfterProcessRestart() {
        val attempts = AtomicInteger()
        val spec = fixture { attempts.incrementAndGet(); it.reply(200, payload) }
        spec.file(directory).writeBytes(payload)
        assertArrayEquals(payload, AppUpdateTransfer(client).download(spec, directory, "Bearer fixture") {}.readBytes())
        assertEquals(0, attempts.get())
        spec.file(directory).delete()
        partial(spec).writeBytes(payload)
        assertArrayEquals(payload, AppUpdateTransfer(client).download(spec, directory, "Bearer fixture") {}.readBytes())
        assertEquals(0, attempts.get())
    }

    @Test fun installerResolutionRejectsPartialFilesTraversalAndSymlinksOutsideTheUpdateDirectory() {
        val spec = fixture { it.reply(200, payload) }
        spec.file(directory).writeBytes(payload)
        assertEquals(spec.file(directory).canonicalFile, resolveUpdateApk(directory, spec.file(directory).name))
        assertNull(resolveUpdateApk(directory, "../" + spec.file(directory).name))
        assertNull(resolveUpdateApk(directory, spec.file(directory).name + ".part"))
        val outside = Files.createTempFile("devwisp-update-outside", ".apk")
        try {
            spec.file(directory).delete()
            Files.createSymbolicLink(spec.file(directory).toPath(), outside)
            assertNull(resolveUpdateApk(directory, spec.file(directory).name))
        } finally { Files.deleteIfExists(outside) }
    }

    private class FixtureHeaders : LinkedHashMap<String, String>() {
        fun getFirst(name: String): String? = entries.firstOrNull { it.key.equals(name, ignoreCase = true) }?.value
        fun add(name: String, value: String) { put(name, value) }
    }

    private class FixtureExchange(private val socket: Socket) {
        val requestHeaders = FixtureHeaders()
        val responseHeaders = FixtureHeaders()
        val responseBody get() = socket.getOutputStream()

        init {
            val reader = socket.getInputStream().bufferedReader(Charsets.US_ASCII)
            reader.readLine() ?: throw IOException("Missing fixture request")
            while (true) {
                val line = reader.readLine() ?: break
                if (line.isEmpty()) break
                val separator = line.indexOf(':')
                if (separator > 0) requestHeaders.add(line.substring(0, separator), line.substring(separator + 1).trim())
            }
        }

        fun sendResponseHeaders(code: Int, length: Long) {
            val headers = buildString {
                append("HTTP/1.1 $code Fixture\r\nContent-Length: $length\r\nConnection: close\r\n")
                responseHeaders.forEach { (name, value) -> append("$name: $value\r\n") }
                append("\r\n")
            }
            responseBody.write(headers.toByteArray(Charsets.US_ASCII))
        }
    }

    private class FixtureServer(handle: (FixtureExchange) -> Unit) : Closeable {
        private val listener = ServerSocket(0, 16, InetAddress.getByName("127.0.0.1"))
        val port get() = listener.localPort
        private val worker = thread(name = "update-download-fixture", isDaemon = true) {
            while (!listener.isClosed) {
                val peer = try { listener.accept() } catch (_: IOException) { break }
                try {
                    peer.soTimeout = 3_000
                    peer.use { handle(FixtureExchange(it)) }
                } catch (_: IOException) { peer.close() }
            }
        }

        override fun close() {
            listener.close()
            worker.join(4_000)
        }
    }
}
