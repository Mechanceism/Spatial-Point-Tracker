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
 * (lens_report.json, streams_latest.json, mixed_latest.json, focus_latest.json,
 * snapshot_latest.json + snap_*.jpg). The camera is closed as soon as the app
 * leaves the screen. Can also be started from the PC:
 *   adb shell am start -n io.github.mechanceism.sptcamera/.MainActivity --es run report|streams|mixed|focus|snapshot|live [--es seconds 6]
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
        val row1 = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        val row2 = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        fun button(row: LinearLayout, label: String, action: () -> Unit) = Button(this).apply {
            text = label
            isAllCaps = false
            setOnClickListener { action() }
            row.addView(this)
        }
        button(row1, "Lens report") { start("report", DEFAULT_SECONDS) }
        button(row1, "Stream test") { start("streams", DEFAULT_SECONDS) }
        button(row1, "Stop") { stopRequested = true; log("stop requested") }
        button(row1, "Live") { openLive() }
        button(row2, "Mixed streams") { start("mixed", DEFAULT_SECONDS) }
        button(row2, "Focus test") { start("focus", DEFAULT_SECONDS) }
        button(row2, "Snapshot") { start("snapshot", DEFAULT_SECONDS) }
        statusView = TextView(this).apply { textSize = 14f; setPadding(0, 12, 0, 12) }
        logView = TextView(this).apply {
            typeface = Typeface.MONOSPACE
            textSize = 11f
            setTextIsSelectable(true)
        }
        scroll = ScrollView(this).apply { addView(logView) }
        root.addView(row1)
        root.addView(row2)
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

    private fun openLive() {
        if (worker?.isAlive == true) { log("busy - press Stop first"); return }
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            log("asking for the camera permission - tap \"While using the app\", then Live again")
            requestPermissions(arrayOf(Manifest.permission.CAMERA), REQUEST_CAMERA)
            return
        }
        startActivity(Intent(this, LiveActivity::class.java))
    }

    private fun handleIntent(intent: Intent?) {
        val mode = intent?.getStringExtra("run") ?: return
        if (mode == "live") { intent.removeExtra("run"); openLive(); return }
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
                    "streams" -> streamTest(seconds, mixed = false)
                    "mixed" -> streamTest(seconds, mixed = true)
                    "focus" -> focusTest()
                    "snapshot" -> snapshot()
                    else -> log("unknown mode '$mode' (use report, streams, mixed, focus or snapshot)")
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

    /** The back lenses by role (from the lens report's field-of-view guess). */
    private class Lenses(val logical: String, val physical: List<String>, val roles: Map<String, String>, val autofocus: List<String>) {
        val tele: String? get() = roles.entries.firstOrNull { it.value == "telephoto" }?.key
        val others: List<String> get() = physical.filter { it != tele }

        /**
         * The set we would use: every lens asked for 4K (the tele then delivers 1920x1440,
         * its full sensor). Main 4K + tele 2560x1440 is "supported" on paper but kills
         * the camera device on the S23.
         */
        val useful: StreamTest.Combo
            get() = StreamTest.Combo(physical, 3840, 2160)
    }

    private fun lenses(cm: CameraManager): Lenses? {
        val (logical, physical) = LensReport.backLogical(cm) ?: run { log("no back multi-camera found"); return null }
        val cams = LensReport.build(cm).getJSONArray("cameras")
        val roles = HashMap<String, String>()
        val af = ArrayList<String>()
        for (i in 0 until cams.length()) {
            val c = cams.getJSONObject(i)
            val id = c.getString("id")
            if (id !in physical) continue
            c.optString("role_guess").takeIf { it.isNotEmpty() }?.let { roles[id] = it }
            val modes = c.optJSONArray("af_modes")
            if (modes != null && (0 until modes.length()).any { modes.getString(it) == "AUTO" }) af.add(id)
        }
        return Lenses(logical, physical, roles, af)
    }

    private fun streamTest(seconds: Double, mixed: Boolean) {
        val cm = getSystemService(CameraManager::class.java)
        val lenses = lenses(cm) ?: return
        val logical = lenses.logical
        val physical = lenses.physical
        val combos = if (mixed) StreamTest.mixedCombos(lenses.tele, lenses.others) else StreamTest.combos(physical)
        val kind = if (mixed) "Mixed streams" else "Stream test"
        log("=== $kind: logical $logical, lenses ${physical.joinToString(",")} ${lenses.roles}, ${combos.size} runs x ${seconds}s ===")
        log("start: ${thermal.snapshot().text()}")
        val result = StreamTest(cm, thermal, ::log) { stopRequested }.sweep(logical, combos, seconds)
        result.optString("error").takeIf { it.isNotEmpty() }?.let { log("ERROR $it") }
        result.optString("stopped").takeIf { it.isNotEmpty() }?.let { log("sweep stopped: $it") }
        log("end: ${thermal.snapshot().text()}")
        val stamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
        val text = result.toString(2)
        val prefix = if (mixed) "mixed" else "streams"
        save("${prefix}_$stamp.json", text)
        save("${prefix}_latest.json", text)
    }

    /** Continuous AF, a fixed-focus sweep and a hold, in the useful 3-lens session. */
    private fun focusTest() {
        val cm = getSystemService(CameraManager::class.java)
        val lenses = lenses(cm) ?: return
        if (lenses.autofocus.isEmpty()) { log("no lens with autofocus"); return }
        val combo = lenses.useful
        log("=== Focus test ${combo.label}, autofocus lenses ${lenses.autofocus.joinToString(",")} ===")
        log("point the phone at something detailed and hold it still (about 15 s)")
        val diopters = listOf(0f, 0.5f, 1f, 1.5f, 2f, 2.5f, 3f, 4f, 5f, 6f, 8f, 10f)
        val result = StreamTest(cm, thermal, ::log) { stopRequested }.focusSweep(lenses.logical, combo, lenses.autofocus, diopters)
        result.optString("error").takeIf { it.isNotEmpty() }?.let { log("ERROR $it") }
        result.optString("stopped").takeIf { it.isNotEmpty() }?.let { log("stopped: $it") }
        save("focus_latest.json", result.toString(2))
    }

    /** Frames per lens from a few sessions, to compare fields of view (see snapshot_latest.json). */
    private fun snapshot() {
        val cm = getSystemService(CameraManager::class.java)
        val lenses = lenses(cm) ?: return
        val logical = lenses.logical
        val runs = org.json.JSONArray()
        // Does the ultrawide's field of view depend on the session? (Alone it calibrated
        // at the main lens's ~72°, inside the 3-lens session it showed ~84°.)
        val uw = lenses.roles.entries.firstOrNull { it.value == "ultrawide" }?.key
        val main = lenses.roles.entries.firstOrNull { it.value == "main" }?.key
        // Zoom 0.6 gives the ultrawide its full view; does it change main or tele?
        val combos = listOfNotNull(
            main?.let { StreamTest.Combo(listOf(it), 3840, 2160) },
            main?.let { StreamTest.Combo(listOf(it), 3840, 2160, zoom = 0.6f) },
            StreamTest.Combo(lenses.physical, 3840, 2160, zoom = 0.6f),
        )
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
    const val VERSION = "0.9"
}
