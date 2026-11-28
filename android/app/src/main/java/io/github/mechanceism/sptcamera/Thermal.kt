package io.github.mechanceism.sptcamera

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.PowerManager
import org.json.JSONObject

/**
 * Temperature watchdog. Uses only what a normal app may read: the system thermal
 * status, the thermal headroom forecast and the battery temperature.
 *
 * Camera work stops when the phone gets warm, well before Android or One UI would
 * start throttling, and only resumes once it has cooled down.
 */
class Thermal(private val context: Context) {

    data class Snapshot(val status: Int, val headroom: Float, val batteryC: Float, val charging: Boolean) {
        fun text(): String {
            val head = if (headroom.isNaN()) "-" else "%.2f".format(headroom)
            return "thermal ${statusName(status)}   headroom $head   battery %.1f °C%s"
                .format(batteryC, if (charging) "  (charging)" else "")
        }

        fun json(): JSONObject = JSONObject()
            .put("status", statusName(status))
            .put("headroom", if (headroom.isNaN()) JSONObject.NULL else round(headroom.toDouble(), 3))
            .put("battery_c", round(batteryC.toDouble(), 1))
            .put("charging", charging)
    }

    private val power = context.getSystemService(PowerManager::class.java)
    private var last: Snapshot? = null
    private var lastAt = 0L

    /** Cached for a second: the headroom call returns NaN when polled faster. */
    @Synchronized
    fun snapshot(): Snapshot {
        val now = System.nanoTime()
        last?.let { if (now - lastAt < 1_000_000_000L) return it }
        val headroom = try { power.getThermalHeadroom(10) } catch (_: Throwable) { Float.NaN }
        val battery = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED), Context.RECEIVER_NOT_EXPORTED)
        val tenths = battery?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Int.MIN_VALUE) ?: Int.MIN_VALUE
        val plugged = (battery?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0) != 0
        val snap = Snapshot(
            power.currentThermalStatus,
            headroom,
            if (tenths == Int.MIN_VALUE) Float.NaN else tenths / 10f,
            plugged,
        )
        last = snap
        lastAt = now
        return snap
    }

    fun tooHot(s: Snapshot = snapshot()): Boolean =
        s.status >= PowerManager.THERMAL_STATUS_MODERATE ||
            s.batteryC >= STOP_BATTERY_C ||
            (!s.headroom.isNaN() && s.headroom >= STOP_HEADROOM)

    fun coolEnough(s: Snapshot = snapshot()): Boolean =
        s.status <= PowerManager.THERMAL_STATUS_LIGHT &&
            s.batteryC <= RESUME_BATTERY_C &&
            (s.headroom.isNaN() || s.headroom <= RESUME_HEADROOM)

    companion object {
        /** Stop camera work at or above this battery temperature. */
        const val STOP_BATTERY_C = 41f
        const val RESUME_BATTERY_C = 38f

        /** Headroom 1.0 = the point where the system starts severe throttling. */
        const val STOP_HEADROOM = 0.85f
        const val RESUME_HEADROOM = 0.70f

        fun statusName(s: Int) = when (s) {
            PowerManager.THERMAL_STATUS_NONE -> "none"
            PowerManager.THERMAL_STATUS_LIGHT -> "light"
            PowerManager.THERMAL_STATUS_MODERATE -> "moderate"
            PowerManager.THERMAL_STATUS_SEVERE -> "severe"
            PowerManager.THERMAL_STATUS_CRITICAL -> "critical"
            PowerManager.THERMAL_STATUS_EMERGENCY -> "emergency"
            PowerManager.THERMAL_STATUS_SHUTDOWN -> "shutdown"
            else -> "unknown($s)"
        }
    }
}

fun round(v: Double, digits: Int): Double {
    val f = Math.pow(10.0, digits.toDouble())
    return Math.round(v * f) / f
}
