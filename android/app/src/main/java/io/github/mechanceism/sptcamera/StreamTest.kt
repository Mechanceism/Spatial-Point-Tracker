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
    /** One stream: a physical lens (null = the logical camera) at a requested size. */
    data class Stream(val lens: String?, val width: Int, val height: Int) {
        val name: String get() = lens ?: "logical"
    }

    /** One session with one or more streams. */
    data class Combo(val streams: List<Stream>, val raw: Boolean = false, val zoom: Float? = null) {
        constructor(lenses: List<String?>, width: Int, height: Int, raw: Boolean = false, zoom: Float? = null) :
            this(lenses.map { Stream(it, width, height) }, raw, zoom)

        val lenses: List<String?> get() = streams.map { it.lens }
        private val uniform: Boolean get() = streams.all { it.width == streams[0].width && it.height == streams[0].height }
        val label: String
            get() = (if (uniform) "${streams[0].height}p [" + streams.joinToString("+") { it.name } + "]"
            else "[" + streams.joinToString("+") { "${it.name}@${it.height}p" } + "]") + (if (raw) " raw" else "") +
                (zoom?.let { " zoom $it" } ?: "")
    }

    private class FrameStats {
        @Volatile var measuring = false
        val stamps = ArrayList<Long>()
        var delivered: String? = null // the size the phone actually sends

        @Synchronized fun add(img: android.media.Image) {
            if (delivered == null) delivered = "${img.width}x${img.height}"
            if (measuring) stamps.add(img.timestamp)
        }

        @Synchronized fun json(): JSONObject {
            val o = JSONObject().put("frames", stamps.size).put("delivered", delivered ?: JSONObject.NULL)
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
        var device = open(logicalId)
        if (device == null) {
            result.put("error", "could not open camera $logicalId: $lost")
            thread.quitSafely()
            return result
        }
        var reopens = 0
        try {
            val chars = cm.getCameraCharacteristics(logicalId)
            for (combo in combos) {
                if (stopRequested()) { result.put("stopped", "by user"); break }
                if (lost != null) {
                    // A combination the phone can't really run can kill the camera device
                    // (S23: main 4K + tele 1440p). Close it, reopen and go on.
                    log("camera lost ($lost) - reopening")
                    device?.close()
                    if (++reopens > 3) { result.put("stopped", "camera lost too often"); break }
                    SystemClock.sleep(1000)
                    lost = null
                    device = open(logicalId)
                    if (device == null) { result.put("stopped", "could not reopen: $lost"); break }
                }
                if (!coolDown()) { result.put("stopped", "phone stayed warm"); break }
                val run = runCombo(device!!, chars, combo, seconds)
                if (lost != null) run.put("camera_lost", lost)
                runs.put(run)
                log(line(combo, run))
            }
        } finally {
            device?.close()
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
        val run = JSONObject().put("label", combo.label)
            .put("requested", JSONArray(combo.streams.map { "${it.name}:${it.width}x${it.height}" }))
        val before = thermal.snapshot()
        run.put("thermal_before", before.json())
        val readers = combo.streams.map { ImageReader.newInstance(it.width, it.height, ImageFormat.YUV_420_888, 4) }
        val stats = readers.map { FrameStats() }
        readers.forEachIndexed { i, reader ->
            reader.setOnImageAvailableListener({ r ->
                r.acquireLatestImage()?.let { img -> stats[i].add(img); img.close() }
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
        val readers = combo.streams.map { ImageReader.newInstance(it.width, it.height, ImageFormat.YUV_420_888, 4) }
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
                val req = combo.streams[i]
                val tag = combo.lenses.joinToString("") { it ?: "L" } + (combo.zoom?.let { "_z$it" } ?: "") + if (combo.raw) "_raw" else ""
                val name = "snap_${lens}_${req.width}x${req.height}_s$tag.jpg"
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

    /** Per-lens numbers for one focus phase. */
    private class PhaseStats {
        val sharp = HashMap<String, MutableList<Double>>()
        val focus = HashMap<String, MutableList<Float>>()
        @Synchronized fun addSharp(lens: String, v: Double) { sharp.getOrPut(lens) { ArrayList() }.add(v) }
        @Synchronized fun addFocus(lens: String, v: Float) { focus.getOrPut(lens) { ArrayList() }.add(v) }
        @Synchronized fun json(): JSONObject {
            val o = JSONObject()
            for (lens in (sharp.keys + focus.keys).sorted()) {
                val l = JSONObject()
                sharp[lens]?.sorted()?.let { if (it.isNotEmpty()) l.put("sharpness", round(it[it.size / 2], 1)).put("frames", it.size) }
                focus[lens]?.sorted()?.let {
                    if (it.isNotEmpty()) l.put("focus_d", round(it[it.size / 2].toDouble(), 3))
                        .put("focus_min_d", round(it.first().toDouble(), 3)).put("focus_max_d", round(it.last().toDouble(), 3))
                }
                o.put(lens, l)
            }
            return o
        }
    }

    /**
     * Manual focus check inside one session. Phase "af": continuous autofocus (where it
     * settles tells how far away the scene is). Then a sweep of fixed focus distances
     * (diopters = 1 / metres; 0 = infinity), then a 5 s hold at each lens's sharpest
     * setting. Records the focus distance each lens reports and a sharpness score
     * (variance of the Laplacian over the image centre) for every stream.
     */
    fun focusSweep(logicalId: String, combo: Combo, focusLenses: List<String>, diopters: List<Float>): JSONObject {
        val result = JSONObject().put("format", 1).put("label", combo.label).put("focus_lenses", JSONArray(focusLenses))
        val device = open(logicalId)
        if (device == null) { thread.quitSafely(); return result.put("error", "could not open camera: $lost") }
        val chars = cm.getCameraCharacteristics(logicalId)
        val physKeys = chars.availablePhysicalCameraRequestKeys ?: emptyList()
        val perLens = physKeys.contains(CaptureRequest.LENS_FOCUS_DISTANCE) && physKeys.contains(CaptureRequest.CONTROL_AF_MODE)
        result.put("per_lens_focus_keys", perLens)
        log("focus can be set per lens: $perLens")
        val readers = combo.streams.map { ImageReader.newInstance(it.width, it.height, ImageFormat.YUV_420_888, 4) }
        val current = java.util.concurrent.atomic.AtomicReference<PhaseStats?>(null)
        val phases = JSONArray()
        result.put("phases", phases)
        var session: CameraCaptureSession? = null
        try {
            readers.forEachIndexed { i, reader ->
                val name = combo.streams[i].name
                reader.setOnImageAvailableListener({ r ->
                    r.acquireLatestImage()?.let { img ->
                        current.get()?.addSharp(name, sharpness(img))
                        img.close()
                    }
                }, handler)
            }
            val configured = CountDownLatch(1)
            val outputs = readers.mapIndexed { i, r ->
                OutputConfiguration(r.surface).apply { combo.streams[i].lens?.let { setPhysicalCameraId(it) } }
            }
            device.createCaptureSession(SessionConfiguration(SessionConfiguration.SESSION_REGULAR, outputs, executor,
                object : CameraCaptureSession.StateCallback() {
                    override fun onConfigured(s: CameraCaptureSession) { session = s; configured.countDown() }
                    override fun onConfigureFailed(s: CameraCaptureSession) { configured.countDown() }
                }))
            if (!configured.await(10, TimeUnit.SECONDS) || session == null) return result.put("error", "session not configured")
            val s = session!!
            val callback = object : CameraCaptureSession.CaptureCallback() {
                override fun onCaptureCompleted(cs: CameraCaptureSession, req: CaptureRequest, res: android.hardware.camera2.TotalCaptureResult) {
                    val ph = current.get() ?: return
                    res.get(android.hardware.camera2.CaptureResult.LENS_FOCUS_DISTANCE)?.let { ph.addFocus("logical", it) }
                    for ((id, pr) in res.physicalCameraTotalResults) {
                        pr.get(android.hardware.camera2.CaptureResult.LENS_FOCUS_DISTANCE)?.let { ph.addFocus(id, it) }
                    }
                }
            }
            val physical = combo.lenses.filterNotNull().toSet()

            // settings: lens -> diopters (null = continuous AF); "logical" sets the logical request.
            fun focusRequest(settings: Map<String, Float?>): CaptureRequest {
                val b = if (perLens) device.createCaptureRequest(CameraDevice.TEMPLATE_RECORD, physical)
                else device.createCaptureRequest(CameraDevice.TEMPLATE_RECORD)
                readers.forEach { b.addTarget(it.surface) }
                b.set(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE, CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF)
                if ((chars[CameraCharacteristics.LENS_INFO_AVAILABLE_OPTICAL_STABILIZATION] ?: IntArray(0)).contains(1)) {
                    b.set(CaptureRequest.LENS_OPTICAL_STABILIZATION_MODE, CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF)
                }
                fixed30(chars)?.let { b.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, it) }
                // A distance of exactly 0 is ignored by the S23 (the lens stays put): use 100 m.
                val logicalD = (if (settings.containsKey("logical")) settings["logical"] else settings.values.firstOrNull())?.coerceAtLeast(0.01f)
                if (logicalD == null) {
                    b.set(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
                } else {
                    b.set(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_OFF)
                    b.set(CaptureRequest.LENS_FOCUS_DISTANCE, logicalD)
                }
                if (perLens) for (lens in focusLenses) {
                    val d = (if (settings.containsKey(lens)) settings[lens] else logicalD)?.coerceAtLeast(0.01f)
                    if (d == null) {
                        b.setPhysicalCameraKey(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_VIDEO, lens)
                    } else {
                        b.setPhysicalCameraKey(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_OFF, lens)
                        b.setPhysicalCameraKey(CaptureRequest.LENS_FOCUS_DISTANCE, d, lens)
                    }
                }
                return b.build()
            }

            fun runPhase(name: String, settings: Map<String, Float?>, settle: Double, measure: Double): JSONObject? {
                s.setRepeatingRequest(focusRequest(settings), callback, handler)
                if (waitWhileStreaming(settle) != null) return null
                val ph = PhaseStats()
                current.set(ph)
                val stop = waitWhileStreaming(measure)
                current.set(null)
                val o = JSONObject().put("phase", name).put("lenses", ph.json())
                phases.put(o)
                log(focusLine(name, o.getJSONObject("lenses")))
                return if (stop == null) o else null
            }

            runPhase("af", emptyMap(), 3.0, 1.0) ?: return result.put("stopped", "during the AF phase")
            val best = HashMap<String, Pair<Float, Double>>()
            for (d in diopters) {
                val o = runPhase("d=%.2f".format(d), focusLenses.associateWith<String, Float?> { d } + ("logical" to d), 0.7, 0.6)
                    ?: return result.put("stopped", "during the sweep")
                val lenses = o.getJSONObject("lenses")
                for (lens in focusLenses) {
                    val sh = lenses.optJSONObject(lens)?.optDouble("sharpness") ?: continue
                    if (!sh.isNaN() && sh > (best[lens]?.second ?: -1.0)) best[lens] = d to sh
                }
            }
            val bestJson = JSONObject()
            best.forEach { (lens, v) -> bestJson.put(lens, JSONObject().put("diopters", v.first.toDouble()).put("sharpness", v.second)) }
            result.put("best", bestJson)
            log("sharpest: " + best.entries.joinToString(", ") {
                val d = it.value.first
                "${it.key} at %.2f D (%s)".format(d, if (d > 0) "%.2f m".format(1 / d) else "infinity")
            })
            if (best.isNotEmpty()) {
                val hold: Map<String, Float?> = best.mapValues { it.value.first } + ("logical" to best.values.first().first)
                runPhase("hold", hold, 0.7, 5.0)
            }
            try { s.stopRepeating() } catch (_: Exception) {}
        } catch (e: Exception) {
            result.put("error", "${e.javaClass.simpleName}: ${e.message}")
            log("ERROR ${e.javaClass.simpleName}: ${e.message}")
        } finally {
            session?.close()
            device.close()
            readers.forEach { it.close() }
            thread.quitSafely()
        }
        return result
    }

    private fun focusLine(name: String, lenses: JSONObject): String {
        val sb = StringBuilder(name.padEnd(7))
        for (lens in lenses.keys().asSequence().sorted()) {
            val l = lenses.getJSONObject(lens)
            sb.append(" | ").append(lens).append(':')
            if (l.has("focus_d")) sb.append(" f ").append(l.get("focus_d")).append(" D")
            if (l.has("focus_min_d") && l.getDouble("focus_min_d") != l.getDouble("focus_max_d")) {
                sb.append(" (").append(l.get("focus_min_d")).append("..").append(l.get("focus_max_d")).append(')')
            }
            if (l.has("sharpness")) sb.append(" sharp ").append(l.get("sharpness"))
        }
        return sb.toString()
    }

    /** Variance of the 4-neighbour Laplacian over a 512x512 patch at the image centre. */
    private fun sharpness(img: android.media.Image): Double {
        val plane = img.planes[0]
        val buf = plane.buffer
        val n = minOf(512, img.width - 2, img.height - 2)
        val x0 = (img.width - n) / 2
        val y0 = (img.height - n) / 2
        val rows = Array(n + 2) { ByteArray(n + 2) }
        for (r in 0 until n + 2) {
            val base = (y0 - 1 + r) * plane.rowStride + (x0 - 1) * plane.pixelStride
            if (plane.pixelStride == 1) {
                buf.position(base)
                buf.get(rows[r])
            } else {
                for (c in 0 until n + 2) rows[r][c] = buf.get(base + c * plane.pixelStride)
            }
        }
        var sum = 0.0
        var sum2 = 0.0
        for (r in 1..n) {
            val up = rows[r - 1]
            val row = rows[r]
            val dn = rows[r + 1]
            for (c in 1..n) {
                val v = (up[c].toInt() and 0xff) + (dn[c].toInt() and 0xff) + (row[c - 1].toInt() and 0xff) +
                    (row[c + 1].toInt() and 0xff) - 4 * (row[c].toInt() and 0xff)
                sum += v
                sum2 += v.toDouble() * v
            }
        }
        val count = n.toDouble() * n
        return sum2 / count - (sum / count) * (sum / count)
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
            // The logical camera's zoom: physical streams can be cropped to match it.
            combo.zoom?.let { set(CaptureRequest.CONTROL_ZOOM_RATIO, it) }
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
                val asked = combo.streams[i].let { "${it.width}x${it.height}" }
                if (st.optString("delivered", asked) != asked) sb.append(" GOT ").append(st.getString("delivered"))
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

        /**
         * The combinations we would actually use: main + ultrawide at 4K with the
         * telephoto at its largest real size (2560x1440), plus fallbacks.
         */
        fun mixedCombos(tele: String?, others: List<String>): List<Combo> {
            if (tele == null) return emptyList()
            val k4 = { id: String -> Stream(id, 3840, 2160) }
            val list = mutableListOf<Combo>()
            for (o in others) list.add(Combo(listOf(k4(o), Stream(tele, 2560, 1440))))
            list.add(Combo(others.map(k4) + Stream(tele, 2560, 1440)))
            list.add(Combo(others.map(k4) + Stream(tele, 1920, 1080)))
            list.add(Combo(others.map { Stream(it, 2560, 1440) } + Stream(tele, 2560, 1440)))
            return list
        }

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
