package io.github.mechanceism.sptcamera

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Typeface
import android.hardware.camera2.CameraManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.WindowInsets
import android.view.WindowManager
import android.widget.Button
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import org.opencv.android.OpenCVLoader
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors

/**
 * Live view: streams every back lens, detects markers and ChArUco boards on the
 * phone, and shows one lens with the detections drawn on top. Tap "Lens" to switch
 * the preview, "Lock focus" to freeze the autofocus lenses where they are.
 * The camera stops when the screen is left, and pauses when the phone gets warm.
 *
 * While this screen is open, a PC can connect over USB (see [Server]) and receives
 * every frame's detections; it can also lock / set focus and send board definitions.
 */
class LiveActivity : Activity(), Rig.Listener {

    private lateinit var image: ImageView
    private lateinit var info: TextView
    private lateinit var lensButton: Button
    private lateinit var focusButton: Button
    private lateinit var thermal: Thermal
    private val ui = Handler(Looper.getMainLooper())
    private val control = Executors.newSingleThreadExecutor() // start/stop in order, off the UI thread
    @Volatile private var rig: Rig? = null
    @Volatile private var roles: Map<String, String> = emptyMap()
    private val latest = ConcurrentHashMap<Int, Rig.Frame>()
    private val lastCounts = HashMap<Int, Pair<Int, Int>>()
    private var lastTick = 0L
    private var pausedHot = false
    @Volatile private var status = "starting..."
    private var server: Server? = null
    @Volatile private var lensInfo: JSONObject = JSONObject()
    @Volatile private var boards: List<BoardSpec> = BoardSpec.DEFAULTS
    @Volatile private var wantedLenses: List<String>? = null // null = every back lens; set by the PC
    @Volatile private var detectorConfig = DetectorConfig()
    @Volatile private var pcPreview: Triple<String, Double, Int>? = null

    private val markerPaint = Paint().apply { color = Color.GREEN; style = Paint.Style.STROKE; strokeWidth = 2f; isAntiAlias = true }
    private val textPaint = Paint().apply { color = Color.GREEN; textSize = 18f; isAntiAlias = true }
    private val boardColors = mapOf("reference" to Color.CYAN, "target" to Color.YELLOW, "probe" to Color.MAGENTA)
    private val dotPaint = Paint().apply { style = Paint.Style.FILL; isAntiAlias = true }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        thermal = Thermal(this)
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setBackgroundColor(Color.BLACK) }
        image = ImageView(this).apply { scaleType = ImageView.ScaleType.FIT_CENTER; adjustViewBounds = true }
        info = TextView(this).apply { typeface = Typeface.MONOSPACE; textSize = 11f; setTextColor(Color.WHITE) }
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        lensButton = Button(this).apply { isAllCaps = false; text = "Lens"; setOnClickListener { nextLens() } }
        focusButton = Button(this).apply { isAllCaps = false; text = "Lock focus"; setOnClickListener { toggleFocus() } }
        val back = Button(this).apply { isAllCaps = false; text = "Back"; setOnClickListener { finish() } }
        row.addView(lensButton); row.addView(focusButton); row.addView(back)
        root.addView(row)
        root.addView(image, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        root.addView(info)
        root.setOnApplyWindowInsetsListener { v, insets ->
            val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
            v.setPadding(bars.left + 12, bars.top + 8, bars.right + 12, bars.bottom + 8)
            insets
        }
        setContentView(root)
        if (!OpenCVLoader.initLocal()) status = "OpenCV failed to load"
    }

    override fun onResume() {
        super.onResume()
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            status = "camera permission missing - use Stream test on the first screen once"
        } else {
            startRig()
        }
        try {
            server = Server(::hello, ::command, ::log).also { it.start() }
        } catch (e: Exception) {
            status = "PC link unavailable: ${e.message}"
        }
        ui.post(tick)
    }

    override fun onPause() {
        super.onPause()
        ui.removeCallbacks(tick)
        stopRig() // never use the camera off screen
        val s = server
        server = null
        if (s != null) control.execute { s.stop() }
    }

    override fun onDestroy() {
        super.onDestroy()
        control.shutdown()
    }

    private fun startRig() {
        control.execute {
            if (rig != null) return@execute
            val cm = getSystemService(CameraManager::class.java)
            val (logical, physical) = LensReport.backLogical(cm) ?: run { status = "no back multi-camera"; return@execute }
            roles = LensReport.roles(cm)
            lensInfo = LensReport.build(cm)
            // Every lens asked for 4K: main and ultrawide deliver 3840x2160, the tele its
            // full sensor at 1920x1440. (Main 4K + tele 2560x1440 kills the camera.)
            val lenses = wantedLenses?.filter { it in physical }?.takeIf { it.isNotEmpty() } ?: physical
            val streams = lenses.map { StreamTest.Stream(it, 3840, 2160) }
            val r = Rig(cm, logical, streams, boards, this, ::log)
            r.setDetectorConfig(detectorConfig)
            pcPreview?.let { (lens, fps, width) -> r.setPcPreview(lens, fps, width) }
            if (r.start()) {
                rig = r
                server?.send(JSONObject().put("type", "streams").put("streams", streamsJson(r)))
                status = "running" + if (r.perLensFocus) "" else " (focus is shared by all lenses)"
                r.previewIndex = streams.indexOfFirst { roles[it.lens] == "main" }.coerceAtLeast(0)
            } else {
                status = "could not start the camera"
            }
        }
    }

    private fun stopRig() {
        val r = rig
        rig = null
        latest.clear()
        lastCounts.clear()
        if (r != null) control.execute { r.stop() }
    }

    private fun nextLens() {
        val r = rig ?: return
        r.previewIndex = (r.previewIndex + 1) % r.streams.size
    }

    private fun toggleFocus() {
        val r = rig ?: return
        control.execute { if (r.focusLocked) r.unlockFocus() else r.lockFocus() }
    }

    override fun onFrame(frame: Rig.Frame) {
        val r = rig ?: return
        val idx = r.streams.indexOf(frame.stream)
        latest[idx] = frame
        server?.let { srv ->
            if (srv.connected) {
                val lens = frame.stream.lens
                val msg = frame.detections.json().put("type", "frame").put("lens", lens ?: "logical")
                    .put("role", roles[lens] ?: JSONObject.NULL).put("t_ns", frame.timestampNs)
                    .put("width", frame.width).put("height", frame.height)
                if (lens != null && lens in r.autofocusLenses) {
                    msg.put("focus_d", r.reportedFocus[lens]?.toDouble() ?: JSONObject.NULL).put("focus_locked", r.focus[lens] != null)
                }
                srv.send(msg)
            }
        }
        val bmp = frame.preview ?: return
        val s = frame.previewScale
        val c = Canvas(bmp)
        val det = frame.detections
        for (k in det.markerIds.indices) {
            val p = det.markerCorners[k]
            val path = Path().apply {
                moveTo(p[0] * s, p[1] * s); lineTo(p[2] * s, p[3] * s); lineTo(p[4] * s, p[5] * s); lineTo(p[6] * s, p[7] * s); close()
            }
            c.drawPath(path, markerPaint)
            c.drawText(det.markerIds[k].toString(), p[0] * s, p[1] * s - 3, textPaint)
        }
        for ((name, v) in det.boards) {
            dotPaint.color = boardColors[name] ?: Color.RED
            val xy = v.second
            for (j in 0 until xy.size / 2) c.drawCircle(xy[2 * j] * s, xy[2 * j + 1] * s, 3f, dotPaint)
        }
        ui.post { image.setImageBitmap(bmp) }
    }

    override fun onError(message: String) {
        status = "camera: $message"
        log(message)
    }

    private val tick = object : Runnable {
        override fun run() {
            updateInfo()
            ui.postDelayed(this, 500)
        }
    }

    /** First message to a newly connected PC: what is streaming and how. */
    private fun hello(): JSONObject {
        val r = rig
        val o = JSONObject().put("app", "SPT Camera").put("version", BuildConfigInfo.VERSION)
            .put("device", android.os.Build.MODEL).put("boards", JSONArray(boards.map { it.json() }))
            .put("lenses", lensInfo.optJSONArray("cameras") ?: JSONArray())
        if (r != null) {
            o.put("streams", streamsJson(r))
            o.put("autofocus", JSONArray(r.autofocusLenses)).put("per_lens_focus", r.perLensFocus)
            o.put("detector", r.detectorConfig.json())
            o.put("zoom", r.zoom?.toDouble() ?: JSONObject.NULL)
        }
        return o
    }

    private fun streamsJson(r: Rig) = JSONArray(r.streams.map {
        JSONObject().put("lens", it.lens ?: "logical").put("role", roles[it.lens] ?: JSONObject.NULL)
            .put("requested", JSONArray(listOf(it.width, it.height))).put("zoom", r.zoom?.toDouble() ?: JSONObject.NULL)
    })

    override fun onPcPreview(stream: StreamTest.Stream, timestampNs: Long, width: Int, height: Int, jpeg: ByteArray) {
        server?.takeIf { it.connected }?.send(JSONObject().put("type", "image").put("lens", stream.lens ?: "logical")
            .put("t_ns", timestampNs).put("width", width).put("height", height)
            .put("jpeg", android.util.Base64.encodeToString(jpeg, android.util.Base64.NO_WRAP)))
    }

    /** A command from the PC; returns the reply. */
    private fun command(c: JSONObject): JSONObject? {
        val r = rig
        val reply = JSONObject().put("type", "reply").put("cmd", c.optString("cmd"))
        when (c.optString("cmd")) {
            "ping" -> {}
            "lock_focus" -> { r ?: return reply.put("ok", false).put("message", "camera not running"); r.lockFocus() }
            "unlock_focus" -> { r ?: return reply.put("ok", false).put("message", "camera not running"); r.unlockFocus() }
            "focus" -> {
                r ?: return reply.put("ok", false).put("message", "camera not running")
                val lens = c.getString("lens")
                r.setFocus(mapOf(lens to if (c.isNull("diopters")) null else c.getDouble("diopters").toFloat()))
            }
            "boards" -> {
                val arr = c.getJSONArray("boards")
                boards = (0 until arr.length()).map { BoardSpec.fromJson(arr.getJSONObject(it)) }
                r?.setBoards(boards)
                log("boards from the PC: ${boards.joinToString { it.name }}")
            }
            "detector" -> {
                detectorConfig = detectorConfig.with(c)
                r?.setDetectorConfig(detectorConfig)
                reply.put("detector", detectorConfig.json())
            }
            "streams" -> {
                // Run only these lenses (fewer streams = less heat). Restarts the camera;
                // a "streams" message follows once it runs again.
                val arr = c.optJSONArray("lenses")
                wantedLenses = arr?.let { a -> (0 until a.length()).map { a.getString(it) } }
                stopRig()
                startRig()
            }
            "pc_preview" -> {
                pcPreview = if (c.isNull("lens")) null else Triple(c.getString("lens"), c.optDouble("fps", 5.0), c.optInt("width", 960))
                val p = pcPreview
                r?.setPcPreview(p?.first, p?.second ?: 5.0, p?.third ?: 960)
            }
            "preview" -> { r ?: return reply.put("ok", false); r.previewIndex = r.streams.indexOfFirst { it.lens == c.getString("lens") }.coerceAtLeast(0) }
            else -> return reply.put("ok", false).put("message", "unknown command")
        }
        return reply.put("ok", true)
    }

    private fun lensName(lens: String?) = "${roles[lens] ?: "lens"} ${lens ?: "logical"}"

    private fun updateInfo() {
        val snap = thermal.snapshot()
        val r = rig
        // Temperature watchdog: pause the camera when warm, resume when cool again.
        if (r != null && thermal.tooHot(snap)) {
            pausedHot = true
            stopRig()
            log("too warm, camera paused: ${snap.text()}")
        } else if (r == null && pausedHot && thermal.coolEnough(snap)) {
            pausedHot = false
            startRig()
        }
        val now = System.nanoTime()
        val dt = if (lastTick == 0L) 0.0 else (now - lastTick) / 1e9
        lastTick = now
        val sb = StringBuilder()
        if (r != null) {
            lensButton.text = "Lens: ${lensName(r.streams[r.previewIndex].lens)}"
            focusButton.text = if (r.focusLocked) "Unlock focus" else "Lock focus"
            r.streams.forEachIndexed { i, st ->
                val stats = r.stats[i]
                val (cam0, proc0) = lastCounts[i] ?: (stats.cameraFrames to stats.processed)
                lastCounts[i] = stats.cameraFrames to stats.processed
                val camFps = if (dt > 0) (stats.cameraFrames - cam0) / dt else 0.0
                val detFps = if (dt > 0) (stats.processed - proc0) / dt else 0.0
                val ms = if (stats.processed > 0) stats.detectMsTotal / stats.processed else 0.0
                val f = latest[i]
                sb.append(if (i == r.previewIndex) "> " else "  ")
                sb.append(lensName(st.lens).padEnd(14)).append(stats.delivered.padEnd(10))
                sb.append("cam %4.1f  det %4.1f fps %4.0f ms".format(camFps, detFps, ms))
                if (f != null) {
                    sb.append("  mk ").append(f.detections.markerIds.size)
                    for ((name, v) in f.detections.boards) sb.append("  ").append(name.take(3)).append(' ').append(v.first.size)
                }
                st.lens?.let { lens ->
                    r.reportedFocus[lens]?.let { d ->
                        if (lens in r.autofocusLenses) {
                            sb.append("  f %.2fD".format(d)).append(if (d > 0) " (%.2f m)".format(1 / d) else " (inf)")
                            sb.append(if (r.focus[lens] == null) " AF" else " LOCK")
                        }
                    }
                }
                sb.append('\n')
            }
        }
        sb.append(snap.text()).append(if (pausedHot) "  PAUSED (warm)" else "").append('\n').append(status)
        server?.let { srv ->
            sb.append(if (srv.connected) "   PC connected" else "   PC: not connected")
            if (srv.dropped > 0) sb.append(" (").append(srv.dropped).append(" dropped)")
            if (srv.connected) srv.send(JSONObject().put("type", "status").put("thermal", snap.json())
                .put("camera", if (r != null) "running" else if (pausedHot) "paused_warm" else "stopped")
                .put("focus_locked", r?.focusLocked ?: false).put("dropped", srv.dropped))
        }
        info.text = sb.toString()
    }

    private fun log(line: String) = Log.i(MainActivity.TAG, line)
}
