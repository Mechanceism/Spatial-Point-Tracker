package io.github.mechanceism.sptcamera

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.hardware.camera2.CameraManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.WindowInsets
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * SPT Camera, step 1: a test tool for the phone's cameras.
 *
 *  - "Lens report": describes every lens (no camera is opened).
 *  - "Stream test": streams 1, 2 and 3 lenses at once at 1080p and 4K and measures
 *    the real frame rate of each, under the temperature watchdog.
 *
 * Results go to the screen, to logcat (tag SPT) and to the app's private files
 * (lens_report.json, streams_latest.json, snapshot_latest.json + snap_*.jpg). The camera is closed as soon as the app
 * leaves the screen. Can also be started from the PC:
 *   adb shell am start -n io.github.mechanceism.sptcamera/.MainActivity --es run report|streams|snapshot [--es seconds 6]
 */
class MainActivity : Activity() {

    private lateinit var logView: TextView
    private lateinit var statusView: TextView
    private lateinit var scroll: ScrollView
    private lateinit var thermal: Thermal
    private val ui = Handler(Looper.getMainLooper())
    private var worker: Thread? = null
    @Volatile private var stopRequested = false
    private var pending: Pair<String, Double>? = null

    private val statusTick = object : Runnable {
        override fun run() {
            val s = thermal.snapshot()
            statusView.text = s.text() + if (thermal.tooHot(s)) "   TOO WARM" else ""
            ui.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        thermal = Thermal(this)

        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        fun button(label: String, action: () -> Unit) = Button(this).apply {
            text = label
            isAllCaps = false
            setOnClickListener { action() }
            buttons.addView(this)
        }
        button("Lens report") { start("report", DEFAULT_SECONDS) }
        button("Stream test") { start("streams", DEFAULT_SECONDS) }
        button("Snapshot") { start("snapshot", DEFAULT_SECONDS) }
        button("Stop") { stopRequested = true; log("stop requested") }
        statusView = TextView(this).apply { textSize = 14f; setPadding(0, 12, 0, 12) }
        logView = TextView(this).apply {
            typeface = Typeface.MONOSPACE
            textSize = 11f
            setTextIsSelectable(true)
        }
        scroll = ScrollView(this).apply { addView(logView) }
        root.addView(buttons)
        root.addView(statusView)
        root.addView(scroll, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        root.setOnApplyWindowInsetsListener { v, insets ->
            val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
            v.setPadding(bars.left + 24, bars.top + 16, bars.right + 24, bars.bottom + 16)
            insets
        }
        setContentView(root)
        log("SPT Camera ${BuildConfigInfo.VERSION}. Only the camera permission is used; nothing leaves the phone except over USB.")
        handleIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        val mode = intent?.getStringExtra("run") ?: return
        val seconds = intent.getStringExtra("seconds")?.toDoubleOrNull() ?: DEFAULT_SECONDS
        intent.removeExtra("run")
        start(mode, seconds)
    }

    override fun onResume() {
        super.onResume()
        ui.post(statusTick)
    }

    override fun onPause() {
        super.onPause()
        ui.removeCallbacks(statusTick)
        // Never use the camera while the app is not on screen.
        if (worker?.isAlive == true) {
            stopRequested = true
            log("app left the screen - stopping")
            worker?.join(3000)
        }
    }

    private fun start(mode: String, seconds: Double) {
        if (worker?.isAlive == true) { log("busy - press Stop first"); return }
        if (mode != "report" && checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            pending = mode to seconds
            log("asking for the camera permission - tap \"While using the app\"")
            requestPermissions(arrayOf(Manifest.permission.CAMERA), REQUEST_CAMERA)
            return
        }
        stopRequested = false
        worker = Thread({
            try {
                when (mode) {
                    "report" -> lensReport()
                    "streams" -> streamTest(seconds)
                    "snapshot" -> snapshot()
                    else -> log("unknown mode '$mode' (use report, streams or snapshot)")
                }
            } catch (t: Throwable) {
                log("ERROR ${t.javaClass.simpleName}: ${t.message}")
                Log.e(TAG, "worker failed", t)
            }
        }, "spt-worker").apply { start() }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_CAMERA) return
        val next = pending
        pending = null
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED && next != null) start(next.first, next.second)
        else log("camera permission not granted - the stream test needs it")
    }

    private fun lensReport() {
        val cm = getSystemService(CameraManager::class.java)
        val report = LensReport.build(cm)
        log("=== Lens report (${report.getJSONObject("device").getString("model")}) ===")
        val cams = report.getJSONArray("cameras")
        for (i in 0 until cams.length()) log(LensReport.summary(cams.getJSONObject(i)))
        save("lens_report.json", report.toString(2))
    }

    private fun streamTest(seconds: Double) {
        val cm = getSystemService(CameraManager::class.java)
        val back = LensReport.backLogical(cm)
        if (back == null) { log("no back multi-camera found"); return }
        val (logical, physical) = back
        val combos = StreamTest.combos(physical)
        log("=== Stream test: logical $logical, lenses ${physical.joinToString(",")}, ${combos.size} runs x ${seconds}s ===")
        log("start: ${thermal.snapshot().text()}")
        val result = StreamTest(cm, thermal, ::log) { stopRequested }.sweep(logical, combos, seconds)
        result.optString("error").takeIf { it.isNotEmpty() }?.let { log("ERROR $it") }
        result.optString("stopped").takeIf { it.isNotEmpty() }?.let { log("sweep stopped: $it") }
        log("end: ${thermal.snapshot().text()}")
        val stamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
        val text = result.toString(2)
        save("streams_$stamp.json", text)
        save("streams_latest.json", text)
    }

    /** One frame per lens from a 3-lens 4K session (default, then raw optics), plus the telephoto alone at its largest listed size. */
    private fun snapshot() {
        val cm = getSystemService(CameraManager::class.java)
        val (logical, physical) = LensReport.backLogical(cm) ?: run { log("no back multi-camera found"); return }
        val runs = org.json.JSONArray()
        val combos = mutableListOf(StreamTest.Combo(physical, 3840, 2160), StreamTest.Combo(physical, 3840, 2160, raw = true))
        physical.lastOrNull()?.let { combos.add(StreamTest.Combo(listOf(it), 2560, 1440)) }
        for (combo in combos) {
            if (stopRequested) break
            log("=== Snapshot ${combo.label} ===")
            val r = StreamTest(cm, thermal, ::log) { stopRequested }.snapshot(logical, combo, filesDir)
            r.optString("error").takeIf { it.isNotEmpty() }?.let { log("ERROR $it") }
            r.optString("warning").takeIf { it.isNotEmpty() }?.let { log("warning: $it") }
            runs.put(r)
        }
        save("snapshot_latest.json", org.json.JSONObject().put("format", 1).put("runs", runs).toString(2))
    }

    private fun save(name: String, text: String) {
        File(filesDir, name).writeText(text)
        log("saved files/$name")
    }

    private fun log(line: String) {
        line.lines().forEach { Log.i(TAG, it) }
        ui.post {
            logView.append(line + "\n")
            scroll.post { scroll.fullScroll(View.FOCUS_DOWN) }
        }
    }

    companion object {
        const val TAG = "SPT"
        const val REQUEST_CAMERA = 1
        const val DEFAULT_SECONDS = 6.0
    }
}

object BuildConfigInfo {
    const val VERSION = "0.2"
}
