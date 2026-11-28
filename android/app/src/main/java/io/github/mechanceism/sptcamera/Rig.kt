package io.github.mechanceism.sptcamera

import android.annotation.SuppressLint
import android.graphics.Bitmap
import android.graphics.ImageFormat
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.CaptureResult
import android.hardware.camera2.TotalCaptureResult
import android.hardware.camera2.params.OutputConfiguration
import android.hardware.camera2.params.SessionConfiguration
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import org.opencv.android.Utils
import org.opencv.core.CvType
import org.opencv.core.Mat
import org.opencv.core.MatOfByte
import org.opencv.core.MatOfInt
import org.opencv.core.Size
import org.opencv.imgcodecs.Imgcodecs
import org.opencv.imgproc.Imgproc
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executor
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The live camera rig: one session on the back logical camera with a YUV stream per
 * lens. Each lens has its own detection thread that always takes the NEWEST frame
 * (older frames are dropped, never queued), so latency stays at about one frame.
 *
 * Focus per lens: null = continuous autofocus, a number = fixed focus in diopters
 * (1 / metres). [lockFocus] freezes every lens at its current autofocus distance.
 */
class Rig(
    private val cm: CameraManager,
    private val logicalId: String,
    val streams: List<StreamTest.Stream>,
    boards: List<BoardSpec>,
    private val listener: Listener,
    private val log: (String) -> Unit,
) {
    class Frame(
        val stream: StreamTest.Stream,
        val width: Int,
        val height: Int,
        val timestampNs: Long,
        val detections: Detections,
        val preview: Bitmap?, // only for the preview stream, at most ~10 per second
        val previewScale: Float, // preview pixels per image pixel
    )

    interface Listener {
        fun onFrame(frame: Frame)
        fun onError(message: String)

        /** A small JPEG of one lens for the PC's window (see [setPcPreview]). */
        fun onPcPreview(stream: StreamTest.Stream, timestampNs: Long, width: Int, height: Int, jpeg: ByteArray) {}
    }

    /** Running numbers per lens, for the status display. */
    class Stats {
        @Volatile var cameraFrames = 0
        @Volatile var processed = 0
        @Volatile var detectMsTotal = 0.0
        @Volatile var delivered: String = "-"
    }

    private val thread = HandlerThread("spt-rig").apply { start() }
    private val handler = Handler(thread.looper)
    private val executor = Executor { handler.post(it) }
    private val chars = cm.getCameraCharacteristics(logicalId)
    private val physKeys = chars.availablePhysicalCameraRequestKeys ?: emptyList()
    val perLensFocus = physKeys.contains(CaptureRequest.LENS_FOCUS_DISTANCE) && physKeys.contains(CaptureRequest.CONTROL_AF_MODE)

    /**
     * The logical camera's zoom, always at its minimum: physical streams are cropped to
     * the logical zoom, so at the default 1.0x the S23's ultrawide came out cropped to
     * the main lens's 72° (upscaled); at 0.6x it delivers its full ~102°.
     */
    val zoom: Float? = chars[CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE]?.lower
    val autofocusLenses: List<String> = streams.mapNotNull { it.lens }.filter { id ->
        (cm.getCameraCharacteristics(id)[CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES] ?: IntArray(0))
            .contains(CameraMetadata.CONTROL_AF_MODE_AUTO)
    }

    private val readers = streams.map { ImageReader.newInstance(it.width, it.height, ImageFormat.YUV_420_888, 3) }
    private val workers: List<ExecutorService> = streams.map { Executors.newSingleThreadExecutor() }
    private val busy = streams.map { AtomicBoolean(false) }
    private val detectors = arrayOfNulls<Detector>(streams.size)
    private val detectorGen = IntArray(streams.size)
    @Volatile var boards: List<BoardSpec> = boards
        private set
    @Volatile private var boardGen = 0
    @Volatile var detectorConfig = DetectorConfig()
        private set
    private val lastStart = LongArray(streams.size)
    private val emptySince = LongArray(streams.size) // when a lens last stopped seeing markers (0 = sees some)

    /** Lens whose JPEG preview goes to the PC, how often and how wide (null = none). */
    @Volatile private var pcPreview: Triple<String, Double, Int>? = null
    private var lastPcPreviewNs = 0L

    fun setPcPreview(lens: String?, fps: Double = 5.0, width: Int = 960) {
        pcPreview = lens?.let { Triple(it, fps, width) }
    }
    val stats = streams.map { Stats() }

    /** Lens -> focus distance it reports right now (diopters). */
    val reportedFocus = ConcurrentHashMap<String, Float>()
    @Volatile var focus: Map<String, Float?> = emptyMap()
        private set
    @Volatile var previewIndex = 0
    @Volatile private var lastPreviewNs = 0L
    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private val running = AtomicBoolean(false)

    @SuppressLint("MissingPermission")
    fun start(): Boolean {
        val opened = CountDownLatch(1)
        cm.openCamera(logicalId, executor, object : CameraDevice.StateCallback() {
            override fun onOpened(d: CameraDevice) { device = d; opened.countDown() }
            override fun onDisconnected(d: CameraDevice) { d.close(); device = null; opened.countDown(); listener.onError("camera disconnected") }
            override fun onError(d: CameraDevice, error: Int) { d.close(); device = null; opened.countDown(); listener.onError("camera error $error") }
        })
        if (!opened.await(10, TimeUnit.SECONDS) || device == null) { log("could not open camera $logicalId"); return false }
        val d = device!!
        readers.forEachIndexed { i, reader ->
            reader.setOnImageAvailableListener({ r ->
                val img = r.acquireLatestImage() ?: return@setOnImageAvailableListener
                stats[i].cameraFrames++
                val now = System.nanoTime()
                // A lens that has seen nothing for a second only looks now and then.
                val searching = emptySince[i] != 0L && now - emptySince[i] > 1_000_000_000L
                val fps = if (searching) minOf(detectorConfig.searchFps, detectorConfig.maxFps) else detectorConfig.maxFps
                val minGap = (1e9 / fps.coerceAtLeast(0.1)).toLong()
                if (!running.get() || now - lastStart[i] < minGap || !busy[i].compareAndSet(false, true)) {
                    img.close(); return@setOnImageAvailableListener
                }
                lastStart[i] = now
                workers[i].execute {
                    try { process(i, img) } catch (t: Throwable) { log("detect error on ${streams[i].name}: ${t.message}") } finally {
                        img.close()
                        busy[i].set(false)
                    }
                }
            }, handler)
        }
        val configured = CountDownLatch(1)
        val outputs = readers.mapIndexed { i, r -> OutputConfiguration(r.surface).apply { streams[i].lens?.let { setPhysicalCameraId(it) } } }
        d.createCaptureSession(SessionConfiguration(SessionConfiguration.SESSION_REGULAR, outputs, executor,
            object : CameraCaptureSession.StateCallback() {
                override fun onConfigured(s: CameraCaptureSession) { session = s; configured.countDown() }
                override fun onConfigureFailed(s: CameraCaptureSession) { configured.countDown() }
            }))
        if (!configured.await(10, TimeUnit.SECONDS) || session == null) { log("session not configured"); stop(); return false }
        running.set(true)
        setFocus(autofocusLenses.associateWith { null })
        return true
    }

    /** Lens -> diopters (null = continuous AF). Lenses not listed keep their setting. */
    fun setFocus(settings: Map<String, Float?>) {
        val d = device ?: return
        val s = session ?: return
        // The S23 ignores a focus distance of exactly 0 (infinity) and stays where it was.
        focus = focus + settings.mapValues { (_, v) -> v?.coerceAtLeast(MIN_DIOPTERS) }
        val physical = streams.mapNotNull { it.lens }.toSet()
        val b = if (perLensFocus) d.createCaptureRequest(CameraDevice.TEMPLATE_RECORD, physical) else d.createCaptureRequest(CameraDevice.TEMPLATE_RECORD)
        readers.forEach { b.addTarget(it.surface) }
        b.set(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE, CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF)
        if ((chars[CameraCharacteristics.LENS_INFO_AVAILABLE_OPTICAL_STABILIZATION] ?: IntArray(0)).contains(1)) {
            b.set(CaptureRequest.LENS_OPTICAL_STABILIZATION_MODE, CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF)
        }
        zoom?.let { b.set(CaptureRequest.CONTROL_ZOOM_RATIO, it) }
        (chars[CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES] ?: emptyArray()).let { ranges ->
            (ranges.firstOrNull { it.lower == 30 && it.upper == 30 } ?: ranges.filter { it.upper == 30 }.maxByOrNull { it.lower })
                ?.let { b.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, it) }
        }
        fun apply(value: Float?, setMode: (Int) -> Unit, setDist: (Float) -> Unit) {
            if (value == null) setMode(CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
            else { setMode(CameraMetadata.CONTROL_AF_MODE_OFF); setDist(value) }
        }
        // The logical request follows the first autofocus lens (the main lens).
        val first = autofocusLenses.firstOrNull()?.let { focus[it] }
        apply(first, { b.set(CaptureRequest.CONTROL_AF_MODE, it) }, { b.set(CaptureRequest.LENS_FOCUS_DISTANCE, it) })
        if (perLensFocus) for (lens in autofocusLenses) {
            apply(focus[lens], { b.setPhysicalCameraKey(CaptureRequest.CONTROL_AF_MODE, it, lens) },
                { b.setPhysicalCameraKey(CaptureRequest.LENS_FOCUS_DISTANCE, it, lens) })
        }
        s.setRepeatingRequest(b.build(), object : CameraCaptureSession.CaptureCallback() {
            override fun onCaptureCompleted(cs: CameraCaptureSession, req: CaptureRequest, res: TotalCaptureResult) {
                for ((id, pr) in res.physicalCameraTotalResults) {
                    pr.get(CaptureResult.LENS_FOCUS_DISTANCE)?.let { reportedFocus[id] = it }
                }
            }
        }, handler)
    }

    /** Change how detection runs (rate cap, ArUco3, smallest marker). */
    fun setDetectorConfig(config: DetectorConfig) {
        detectorConfig = config
        boardGen++
    }

    /** Replace the board definitions (e.g. sent by the PC); each detection thread picks them up. */
    fun setBoards(list: List<BoardSpec>) {
        boards = list
        boardGen++
    }

    /** Freeze every autofocus lens at the distance it is focused at now. */
    fun lockFocus() = setFocus(autofocusLenses.associateWith { reportedFocus[it] })

    fun unlockFocus() = setFocus(autofocusLenses.associateWith { null })

    val focusLocked: Boolean get() = autofocusLenses.isNotEmpty() && autofocusLenses.all { focus[it] != null }

    private fun process(i: Int, img: android.media.Image) {
        if (detectors[i] == null || detectorGen[i] != boardGen) {
            detectors[i] = Detector(boards, detectorConfig)
            detectorGen[i] = boardGen
        }
        val det = detectors[i]!!
        val plane = img.planes[0]
        val gray = Mat(img.height, img.width, CvType.CV_8UC1, plane.buffer, plane.rowStride.toLong())
        val detections = det.detect(gray)
        val tNow = System.nanoTime()
        if (detections.markerIds.isEmpty()) { if (emptySince[i] == 0L) emptySince[i] = tNow } else emptySince[i] = 0L
        val st = stats[i]
        st.processed++
        st.detectMsTotal += detections.detectMs
        st.delivered = "${img.width}x${img.height}"
        var bmp: Bitmap? = null
        var scale = 1f
        val now = System.nanoTime()
        if (i == previewIndex && now - lastPreviewNs > 90_000_000L) {
            lastPreviewNs = now
            scale = 960f / img.width
            val small = Mat()
            Imgproc.resize(gray, small, Size(960.0, (img.height * scale).toDouble()), 0.0, 0.0, Imgproc.INTER_AREA)
            val rgba = Mat()
            Imgproc.cvtColor(small, rgba, Imgproc.COLOR_GRAY2RGBA)
            bmp = Bitmap.createBitmap(rgba.cols(), rgba.rows(), Bitmap.Config.ARGB_8888)
            Utils.matToBitmap(rgba, bmp)
            small.release(); rgba.release()
        }
        pcPreview?.let { (lens, fps, width) ->
            if (lens == streams[i].lens && now - lastPcPreviewNs > (1e9 / fps).toLong()) {
                lastPcPreviewNs = now
                val h = (img.height * width.toDouble() / img.width).toInt()
                val small = Mat()
                Imgproc.resize(gray, small, Size(width.toDouble(), h.toDouble()), 0.0, 0.0, Imgproc.INTER_AREA)
                val buf = MatOfByte()
                Imgcodecs.imencode(".jpg", small, buf, MatOfInt(Imgcodecs.IMWRITE_JPEG_QUALITY, 75))
                listener.onPcPreview(streams[i], img.timestamp, img.width, img.height, buf.toArray())
                small.release(); buf.release()
            }
        }
        gray.release()
        listener.onFrame(Frame(streams[i], img.width, img.height, img.timestamp, detections, bmp, scale))
    }

    companion object {
        /** 100 m: the closest thing to infinity the lenses accept. */
        const val MIN_DIOPTERS = 0.01f
    }

    fun stop() {
        running.set(false)
        try { session?.stopRepeating() } catch (_: Exception) {}
        session?.close(); session = null
        device?.close(); device = null
        workers.forEach { it.shutdown() }
        workers.forEach { it.awaitTermination(2, TimeUnit.SECONDS) }
        readers.forEach { it.close() }
        thread.quitSafely()
    }
}
