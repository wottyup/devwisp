package com.sevencnup.devwisp

import okhttp3.Call
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.InterruptedIOException
import java.security.MessageDigest
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

internal data class AppUpdateSpec(
    val version: String, val filename: String, val size: Long, val sha256: String, val url: String,
) {
    init {
        require(version.matches(Regex("\\d+\\.\\d+\\.\\d+(?:-(?:dev|beta)\\.\\d+)?")))
        require(filename == "devwisp-android-$version-debug.apk" && size > 0)
        require(sha256.matches(Regex("[0-9a-f]{64}")))
    }

    fun file(directory: File) = File(directory, filename.removeSuffix(".apk") + "-${sha256.take(12)}.apk")
}

internal enum class AppUpdatePhase { CONNECTING, DOWNLOADING, VERIFYING, READY, FAILED }
internal data class AppUpdateProgress(val phase: AppUpdatePhase, val bytes: Long, val total: Long, val error: String? = null)
internal class AppUpdateFailure(val messageKey: String) : IOException(messageKey)

/** Only completed update APKs may be exposed by the installer content provider. */
internal fun resolveUpdateApk(directory: File, name: String): File? {
    if (!name.matches(Regex("devwisp-android-\\d+\\.\\d+\\.\\d+(?:-(?:dev|beta)\\.\\d+)?-debug-[a-f0-9]{12}\\.apk"))) return null
    val file = File(directory, name).canonicalFile
    return file.takeIf { it.parentFile == directory.canonicalFile && it.isFile }
}

/** Runs as the app UID so the update check and APK transfer use the same network routing. */
internal class AppUpdateTransfer(private val client: OkHttpClient = CLIENT) {
    private val canceled = AtomicBoolean(false)
    @Volatile private var call: Call? = null

    fun cancel() {
        canceled.set(true)
        call?.cancel()
    }

    fun download(spec: AppUpdateSpec, directory: File, authorization: String, progress: (AppUpdateProgress) -> Unit): File {
        if (!directory.isDirectory && !directory.mkdirs()) throw AppUpdateFailure("无法创建更新目录，请检查存储空间")
        val target = spec.file(directory)
        val partial = File(directory, target.name + ".part")
        try {
            checkCanceled()
            if (target.isFile && matches(target, spec)) {
                progress(AppUpdateProgress(AppUpdatePhase.READY, spec.size, spec.size))
                return target
            }
            if (target.exists() && !target.delete()) throw AppUpdateFailure("无法写入更新文件，请检查存储空间")
            if (partial.length() > spec.size && !partial.delete()) throw AppUpdateFailure("无法写入更新文件，请检查存储空间")
            var bytes = partial.length()
            if (bytes < spec.size) {
                progress(AppUpdateProgress(AppUpdatePhase.CONNECTING, bytes, spec.size))
                val request = Request.Builder().url(spec.url)
                    .header("Authorization", authorization)
                    .header("Accept-Encoding", "identity")
                    .apply { if (bytes > 0) header("Range", "bytes=$bytes-") }
                    .build()
                val activeCall = client.newCall(request)
                call = activeCall
                checkCanceled()
                activeCall.execute().use { response ->
                    when (response.code) {
                        200 -> bytes = 0L // An ignored Range must restart, never append duplicate bytes.
                        206 -> {
                            val range = Regex("bytes (\\d+)-(\\d+)/(\\d+)").matchEntire(response.header("Content-Range").orEmpty())
                            val start = range?.groupValues?.get(1)?.toLongOrNull()
                            val end = range?.groupValues?.get(2)?.toLongOrNull()
                            val total = range?.groupValues?.get(3)?.toLongOrNull()
                            if (start != bytes || end == null || end < bytes || end >= spec.size || total != spec.size) {
                                throw AppUpdateFailure("服务器返回了无效的下载范围，请重新检查更新")
                            }
                        }
                        401, 403 -> throw AppUpdateFailure("配对已失效，请重新连接电脑")
                        404 -> throw AppUpdateFailure("安装包已不可用，请重新检查更新")
                        else -> throw AppUpdateFailure("更新服务器暂时不可用，请稍后重试")
                    }
                    val body = response.body ?: throw IOException("Empty update response")
                    if (body.contentLength() > spec.size - bytes) throw AppUpdateFailure("更新包大小不符，请重新检查更新")
                    FileOutputStream(partial, bytes > 0).use { output ->
                        body.byteStream().use { input ->
                            val buffer = ByteArray(64 * 1024)
                            progress(AppUpdateProgress(AppUpdatePhase.DOWNLOADING, bytes, spec.size))
                            while (true) {
                                checkCanceled()
                                val count = input.read(buffer)
                                if (count < 0) break
                                if (bytes + count > spec.size) throw AppUpdateFailure("更新包大小不符，请重新检查更新")
                                output.write(buffer, 0, count)
                                bytes += count
                                progress(AppUpdateProgress(AppUpdatePhase.DOWNLOADING, bytes, spec.size))
                            }
                        }
                    }
                }
            }
            checkCanceled()
            if (bytes != spec.size) throw IOException("Incomplete update transfer")
            progress(AppUpdateProgress(AppUpdatePhase.VERIFYING, bytes, spec.size))
            if (!matches(partial, spec)) {
                partial.delete()
                throw AppUpdateFailure("更新包校验失败，请重新下载")
            }
            checkCanceled()
            if (!partial.renameTo(target)) throw AppUpdateFailure("无法写入更新文件，请检查存储空间")
            progress(AppUpdateProgress(AppUpdatePhase.READY, bytes, spec.size))
            return target
        } finally {
            call = null
            if (canceled.get()) partial.delete()
        }
    }

    private fun matches(file: File, spec: AppUpdateSpec): Boolean {
        if (file.length() != spec.size) return false
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                checkCanceled()
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) } == spec.sha256
    }

    private fun checkCanceled() {
        if (canceled.get()) throw InterruptedIOException("Update canceled")
    }

    companion object {
        private val CLIENT = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS).callTimeout(10, TimeUnit.MINUTES).build()
    }
}

/** Does not retain an Activity. A live job survives Activity destruction; disk state supports process restart. */
internal object AppUpdateDownloads {
    private class Job(val id: Long, val spec: AppUpdateSpec, val directory: File) {
        val transfer = AppUpdateTransfer()
        @Volatile var progress = AppUpdateProgress(AppUpdatePhase.CONNECTING, 0, spec.size)
    }
    private val executor = Executors.newSingleThreadExecutor()
    @Volatile private var active: Job? = null

    fun progress(id: Long): AppUpdateProgress? = active?.takeIf { it.id == id }?.progress

    @Synchronized fun start(id: Long, spec: AppUpdateSpec, directory: File, authorization: String, retry: Boolean = false) {
        val previous = active
        if (previous?.id == id && (!retry || previous.progress.phase != AppUpdatePhase.FAILED)) return
        previous?.transfer?.cancel()
        val job = Job(id, spec, directory)
        active = job
        executor.execute {
            try {
                job.transfer.download(spec, directory, authorization) { job.progress = it }
            } catch (error: Exception) {
                job.progress = job.progress.copy(phase = AppUpdatePhase.FAILED,
                    error = (error as? AppUpdateFailure)?.messageKey ?: "下载中断，请检查网络后重试")
            }
        }
    }

    @Synchronized fun cancel(id: Long) {
        val job = active?.takeIf { it.id == id } ?: return
        active = null
        job.transfer.cancel()
        executor.execute {
            job.spec.file(job.directory).delete()
            File(job.directory, job.spec.file(job.directory).name + ".part").delete()
        }
    }
}
