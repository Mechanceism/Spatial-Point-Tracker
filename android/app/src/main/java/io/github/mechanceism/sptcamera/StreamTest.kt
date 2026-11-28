package io.github.mechanceism.sptcamera

import android.annotation.SuppressLint
import android.graphics.ImageFormat
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.params.OutputConfiguration
import android.hardware.camera2.params.SessionConfiguration
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Range
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executor
import java.util.concurrent.TimeUnit

/**
 * Streams YUV frames from one or more lenses at once and measures the real frame
 * rate of each stream. Frames are only counted, never stored or sent anywhere.
 *
 * Every combination runs for a few seconds under the thermal watchdog; the sweep
 * waits for the phone to cool between combinations and gives up if it stays warm.
 */
class StreamTest(
    private val cm: CameraManager,
    private val thermal: Thermal,
    private val log: (String) -> Unit,
    private val stopRequested: () -> Boolean,
) {
    /** One session: `lenses` holds a physical camera ID per stream (null = the logical camera). */
    data class Combo(val lenses: List<String?>, val width: Int, val height: Int, val raw: Boolean = false) {
        val label: String
            get() = "${height}p [" + lenses.joinToString("+") { it ?: "logical" } + "]" + if (raw) " raw" else ""
    }

    private class FrameStats {
        @Volatile var measuring = false
        val stamps = ArrayList<Long>()

        @Synchronized fun add(ns: Long) { if (measuring) stamps.add(ns) }

        @Synchronized fun json(): JSONObject {
            val o = JSONObject().put("frames", stamps.size)
            if (stamps.size >= 2) {
                val gaps = stamps.zipWithNext { a, b -> (b - a) / 1e6 }.sorted()
                o.put("fps", round((stamps.size - 1) * 1e9 / (stamps.last() - stamps.first()), 2))
                o.put("median_gap_ms", round(gaps[gaps.size / 2], 2))
                o.put("max_gap_ms", round(gaps.last(), 2))
            }
            return o
        }
    }

    private val thread = HandlerThread("spt-camera").apply { start() }
    private val handler = Handler(thread.looper)
    private val executor = Executor { handler.post(it) }
    @Volatile private var lost: String? = null

    fun sweep(logicalId: String, combos: List<Combo>, seconds: Double): JSONObject {
        val result = JSONObject().put("format", 1).put("logical_id", logicalId).put("seconds", seconds)
            .put("thermal_start", thermal.snapshot().json())
        val runs = JSONArray()
        result.put("runs", runs)
        val device = open(logicalId)
        if (device == null) {
            result.put("error", "could not open camera $logicalId: $lost")
            thread.quitSafely()
            return result
        }
        try {
            val chars = cm.getCameraCharacteristics(logicalId)
            for (combo in combos) {
                if (stopRequested()) { result.put("stopped", "by user"); break }
                if (lost != null) { result.put("stopped", "camera lost: $lost"); break }
                if (!coolDown()) { result.put("stopped", "phone stayed warm"); break }
                val run = runCombo(device, chars, combo, seconds)
                runs.put(run)
                log(line(combo, run))
            }
        } finally {
            device.close()
            thread.quitSafely()
        }
        result.put("thermal_end", thermal.snapshot().json())
        return result
    }

    /** Waits until the phone is cool enough; false if it is still warm after the limit. */
    private fun coolDown(maxSeconds: Int = 180): Boolean {
        if (thermal.coolEnough()) return true
        log("cooling down: ${thermal.snapshot().text()}")
        val end = SystemClock.elapsedRealtime() + maxSeconds * 1000L
        while (SystemClock.elapsedRealtime() < end) {
            if (stopRequested()) return false
            SystemClock.sleep(1000)
            if (thermal.coolEnough()) { log("cool again: ${thermal.snapshot().text()}"); return true }
        }
        log("still warm after $maxSeconds s - stopping the test")
        return false
    }

    @SuppressLint("MissingPermission") // checked by MainActivity before any test runs
    private fun open(id: String): CameraDevice? {
        val latch = CountDownLatch(1)
        var opened: CameraDevice? = null
        cm.openCamera(id, executor, object : CameraDevice.StateCallback() {
            override fun onOpened(d: CameraDevice) { opened = d; latch.countDown() }
            override fun onDisconnected(d: CameraDevice) { lost = "disconnected"; d.close(); latch.countDown() }
            override fun onError(d: CameraDevice, error: Int) { lost = "error $error"; d.close(); latch.countDown() }
        })
        if (!latch.await(10, TimeUnit.SECONDS)) lost = "open timed out"
        return if (lost == null) opened else null
    }

    private fun runCombo(device: CameraDevice, chars: CameraCharacteristics, combo: Combo, seconds: Double): JSONObject {
        val run = JSONObject().put("label", combo.label).put("width", combo.width).put("height", combo.height)
            .put("lenses", JSONArray(combo.lenses.map { it ?: "logical" }))
        val before = thermal.snapshot()
        run.put("thermal_before", before.json())
        val readers = combo.lenses.map { ImageReader.newInstance(combo.width, combo.height, ImageFormat.YUV_420_888, 4) }
        val stats = readers.map { FrameStats() }
        readers.forEachIndexed { i, reader ->
            reader.setOnImageAvailableListener({ r ->
                r.acquireLatestImage()?.let { img -> stats[i].add(img.timestamp); img.close() }
            }, handler)
        }
        var session: CameraCaptureSession? = null
        val configured = CountDownLatch(1)
        val closed = CountDownLatch(1)
        try {
            val outputs = readers.mapIndexed { i, r ->
                OutputConfiguration(r.surface).apply { combo.lenses[i]?.let { setPhysicalCameraId(it) } }
            }
            val config = SessionConfiguration(SessionConfiguration.SESSION_REGULAR, outputs, executor,
                object : CameraCaptureSession.StateCallback() {
                    override fun onConfigured(s: CameraCaptureSession) { session = s; configured.countDown() }
                    override fun onConfigureFailed(s: CameraCaptureSession) { configured.countDown() }
                    override fun onClosed(s: CameraCaptureSession) { closed.countDown() }
                })
            // The device's own answer to "is this combination guaranteed to work?"
            run.put("supported_query", try { device.isSessionConfigurationSupported(config) } catch (e: Exception) { "n/a: ${e.javaClass.simpleName}" })
            device.createCaptureSession(config)
            if (!configured.await(10, TimeUnit.SECONDS) || session == null) {
                run.put("configured", false)
                return run
            }
            run.put("configured", true)
            val s = session!!
            s.setRepeatingRequest(request(device, chars, readers, combo).build(), null, handler)

            var stopReason = waitWhileStreaming(1.5) // warm-up: auto exposure settles
            if (stopReason == null) {
                stats.forEach { it.measuring = true }
                stopReason = waitWhileStreaming(seconds)
            }
            stats.forEach { it.measuring = false }
            if (stopReason != null) run.put("stopped", stopReason)
            try { s.stopRepeating() } catch (_: Exception) {}
            s.close()
            closed.await(3, TimeUnit.SECONDS)
        } catch (e: Exception) {
            run.put("error", "${e.javaClass.simpleName}: ${e.message}")
            session?.close()
        } finally {
            readers.forEach { it.close() }
        }
        run.put("streams", JSONArray(stats.mapIndexed { i, st -> st.json().put("lens", combo.lenses[i] ?: "logical") }))
        if (stats.size > 1) {
            // Timestamp offset of each other stream from stream 0 (nearest frame, median).
            val sync = JSONObject()
            for (i in 1 until stats.size) offsetMs(stats[0].stamps, stats[i].stamps)?.let {
                sync.put(combo.lenses[i] ?: "logical", it)
            }
            run.put("offset_ms_vs_${combo.lenses[0] ?: "logical"}", sync)
        }
        run.put("thermal_after", thermal.snapshot().json())
        return run
    }

    /**
     * Grabs one frame per lens from a single session (the first frame of each stream
     * after a 2 s warm-up) and saves each as a JPEG in `dir`. Returns the file names,
     * timestamps and offsets.
     */
    fun snapshot(logicalId: String, combo: Combo, dir: java.io.File): JSONObject {
        val result = JSONObject().put("label", combo.label)
        val device = open(logicalId)
        if (device == null) { thread.quitSafely(); return result.put("error", "could not open camera: $lost") }
        val readers = combo.lenses.map { ImageReader.newInstance(combo.width, combo.height, ImageFormat.YUV_420_888, 4) }
        val held = arrayOfNulls<android.media.Image>(readers.size)
        val all = CountDownLatch(readers.size)
        val armed = java.util.concurrent.atomic.AtomicBoolean(false)
        var session: CameraCaptureSession? = null
        try {
            readers.forEachIndexed { i, reader ->
                reader.setOnImageAvailableListener({ r ->
                    val img = r.acquireNextImage() ?: return@setOnImageAvailableListener
                    if (armed.get() && held[i] == null) { held[i] = img; all.countDown() } else img.close()
                }, handler)
            }
            val configured = CountDownLatch(1)
            val outputs = readers.mapIndexed { i, r ->
                OutputConfiguration(r.surface).apply { combo.lenses[i]?.let { setPhysicalCameraId(it) } }
            }
            device.createCaptureSession(SessionConfiguration(SessionConfiguration.SESSION_REGULAR, outputs, executor,
                object : CameraCaptureSession.StateCallback() {
                    override fun onConfigured(s: CameraCaptureSession) { session = s; configured.countDown() }
                    override fun onConfigureFailed(s: CameraCaptureSession) { configured.countDown() }
                }))
            if (!configured.await(10, TimeUnit.SECONDS) || session == null) return result.put("error", "session not configured")
            val chars = cm.getCameraCharacteristics(logicalId)
            session!!.setRepeatingRequest(request(device, chars, readers, combo).build(), null, handler)
            if (waitWhileStreaming(2.0) != null) return result.put("error", "stopped during warm-up")
            armed.set(true)
            if (!all.await(5, TimeUnit.SECONDS)) result.put("warning", "not every lens delivered a frame")
            try { session?.stopRepeating() } catch (_: Exception) {}
            val files = JSONArray()
            val t0 = held.firstOrNull()?.timestamp
            held.forEachIndexed { i, img ->
                if (img == null) return@forEachIndexed
                val lens = combo.lenses[i] ?: "logical"
                val name = "snap_${lens}_${combo.width}x${combo.height}${if (combo.raw) "_raw" else ""}.jpg"
                java.io.FileOutputStream(java.io.File(dir, name)).use { out ->
                    android.graphics.YuvImage(nv21(img), ImageFormat.NV21, img.width, img.height, null)
                        .compressToJpeg(android.graphics.Rect(0, 0, img.width, img.height), 92, out)
                }
                files.put(JSONObject().put("lens", lens).put("file", name).put("width", img.width).put("height", img.height)
                    .put("timestamp_ns", img.timestamp)
                    .put("offset_ms", if (t0 == null) JSONObject.NULL else round((img.timestamp - t0) / 1e6, 3)))
                log("saved files/$name  (${img.width}x${img.height}, offset ${if (t0 == null) "-" else round((img.timestamp - t0) / 1e6, 3)} ms)")
            }
            result.put("frames", files)
        } catch (e: Exception) {
            result.put("error", "${e.javaClass.simpleName}: ${e.message}")
        } finally {
            held.forEach { it?.close() }
            session?.close()
            device.close()
            readers.forEach { it.close() }
            thread.quitSafely()
        }
        return result
    }

    /**
     * The repeating request. With `raw`, distortion correction is switched off and each
     * physical lens is asked for its whole active array: by default the ultrawide's
     * physical stream came out at ~84° instead of 104° (corrected and cropped).
     */
    private fun request(device: CameraDevice, chars: CameraCharacteristics, readers: List<ImageReader>, combo: Combo) =
        run {
            val physical = combo.lenses.filterNotNull().toSet()
            val b = if (combo.raw && physical.isNotEmpty()) device.createCaptureRequest(CameraDevice.TEMPLATE_RECORD, physical)
            else device.createCaptureRequest(CameraDevice.TEMPLATE_RECORD)
            if (combo.raw) {
                val off = CameraMetadata.DISTORTION_CORRECTION_MODE_OFF
                if (hasMode(chars, off)) b.set(CaptureRequest.DISTORTION_CORRECTION_MODE, off)
                for (id in physical) {
                    val pc = cm.getCameraCharacteristics(id)
                    pc[CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE]?.let { active ->
                        b.setPhysicalCameraKey(CaptureRequest.SCALER_CROP_REGION, android.graphics.Rect(0, 0, active.width(), active.height()), id)
                    }
                    if (hasMode(pc, off)) b.setPhysicalCameraKey(CaptureRequest.DISTORTION_CORRECTION_MODE, off, id)
                }
            }
            b
        }.apply {
            readers.forEach { addTarget(it.surface) }
            set(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE, CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF)
            if ((chars[CameraCharacteristics.LENS_INFO_AVAILABLE_OPTICAL_STABILIZATION] ?: IntArray(0)).contains(1)) {
                set(CaptureRequest.LENS_OPTICAL_STABILIZATION_MODE, CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF)
            }
            fixed30(chars)?.let { set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, it) }
        }

    private fun hasMode(c: CameraCharacteristics, mode: Int) =
        (c[CameraCharacteristics.DISTORTION_CORRECTION_AVAILABLE_MODES] ?: IntArray(0)).contains(mode)

    /** YUV_420_888 (any strides) -> NV21 for YuvImage. */
    private fun nv21(img: android.media.Image): ByteArray {
        val w = img.width
        val h = img.height
        val out = ByteArray(w * h * 3 / 2)
        val y = img.planes[0]
        val yBuf = y.buffer
        for (row in 0 until h) {
            yBuf.position(row * y.rowStride)
            yBuf.get(out, row * w, w)
        }
        val u = img.planes[1]
        val v = img.planes[2]
        var pos = w * h
        for (row in 0 until h / 2) {
            for (col in 0 until w / 2) {
                out[pos++] = v.buffer.get(row * v.rowStride + col * v.pixelStride)
                out[pos++] = u.buffer.get(row * u.rowStride + col * u.pixelStride)
            }
        }
        return out
    }

    private fun offsetMs(a: List<Long>, b: List<Long>): Double? {
        if (a.isEmpty() || b.isEmpty()) return null
        val sb = b.sorted()
        // Signed: nearest frame of b minus the frame of a.
        val diffs = a.map { t ->
            val k = sb.binarySearch(t)
            if (k >= 0) 0L else {
                val i = -k - 1
                listOfNotNull(sb.getOrNull(i - 1), sb.getOrNull(i)).minBy { kotlin.math.abs(it - t) } - t
            }
        }.sorted()
        return round(diffs[diffs.size / 2] / 1e6, 3)
    }

    /** Sleeps for `seconds` unless stopped; returns why it stopped early, or null. */
    private fun waitWhileStreaming(seconds: Double): String? {
        val end = SystemClock.elapsedRealtime() + (seconds * 1000).toLong()
        while (SystemClock.elapsedRealtime() < end) {
            if (stopRequested()) return "by user"
            lost?.let { return "camera lost: $it" }
            if (thermal.tooHot()) return "too warm: ${thermal.snapshot().text()}"
            SystemClock.sleep(100)
        }
        return null
    }

    /** A fixed 30 fps auto-exposure range if offered, so exposure can't lower the rate. */
    private fun fixed30(chars: CameraCharacteristics): Range<Int>? {
        val ranges = chars[CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES] ?: return null
        return ranges.firstOrNull { it.lower == 30 && it.upper == 30 }
            ?: ranges.filter { it.upper == 30 }.maxByOrNull { it.lower }
    }

    private fun line(combo: Combo, run: JSONObject): String {
        val sb = StringBuilder(combo.label.padEnd(24))
        sb.append(" supported=").append(run.opt("supported_query"))
        if (!run.optBoolean("configured")) return sb.append("  NOT CONFIGURED").append(run.optString("error").let { if (it.isEmpty()) "" else " ($it)" }).toString()
        run.optJSONArray("streams")?.let { streams ->
            for (i in 0 until streams.length()) {
                val st = streams.getJSONObject(i)
                sb.append("  ").append(st.getString("lens")).append(": ")
                sb.append(if (st.has("fps")) "${st.get("fps")} fps (max gap ${st.get("max_gap_ms")} ms)" else "${st.getInt("frames")} frames")
            }
        }
        run.keys().asSequence().firstOrNull { it.startsWith("offset_ms_vs_") }?.let { key ->
            val o = run.getJSONObject(key)
            sb.append("  offset vs ").append(key.removePrefix("offset_ms_vs_")).append(": ")
                .append(o.keys().asSequence().joinToString(", ") { "$it ${o.get(it)} ms" })
        }
        run.optString("stopped").takeIf { it.isNotEmpty() }?.let { sb.append("  STOPPED: ").append(it) }
        run.optString("error").takeIf { it.isNotEmpty() }?.let { sb.append("  ERROR: ").append(it) }
        val b = run.getJSONObject("thermal_before").optDouble("battery_c")
        val a = run.getJSONObject("thermal_after").optDouble("battery_c")
        sb.append("  battery $b -> $a °C")
        return sb.toString()
    }

    companion object {
        val SIZES = listOf(1920 to 1080, 3840 to 2160)

        /** Logical alone, each lens alone, every pair, all three; 1080p first, 4K last. */
        fun combos(physical: List<String>): List<Combo> {
            val sets = mutableListOf<List<String?>>(listOf(null))
            physical.forEach { sets.add(listOf(it)) }
            for (i in physical.indices) for (j in i + 1 until physical.size) sets.add(listOf(physical[i], physical[j]))
            if (physical.size >= 3) sets.add(physical.take(3))
            return SIZES.flatMap { (w, h) -> sets.map { Combo(it, w, h) } }
        }
    }
}
