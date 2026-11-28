package io.github.mechanceism.sptcamera

import android.graphics.ImageFormat
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraCharacteristics.LENS_FACING_BACK
import android.hardware.camera2.CameraCharacteristics.LENS_FACING_EXTERNAL
import android.hardware.camera2.CameraCharacteristics.LENS_FACING_FRONT
import android.hardware.camera2.CameraManager
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.atan
import kotlin.math.hypot

/**
 * Describes every camera the phone exposes, including the hidden physical lenses
 * behind a logical multi-camera: focal length, sensor size, field of view, the
 * focal length in pixels at 4K / 1080p, focus range, stabilisation and the YUV
 * stream sizes with their maximum frame rates. Read-only: opens no camera.
 */
object LensReport {

    fun build(cm: CameraManager): JSONObject {
        val listed = cm.cameraIdList.toList()
        val ids = LinkedHashSet<String>()
        for (id in listed) {
            ids.add(id)
            ids.addAll(cm.getCameraCharacteristics(id).physicalCameraIds)
        }
        val cams = ids.map { describe(it, cm.getCameraCharacteristics(it), it in listed) }
        guessRoles(cams)
        return JSONObject()
            .put("format", 1)
            .put("device", JSONObject().put("model", Build.MODEL).put("sdk", Build.VERSION.SDK_INT).put("soc", Build.SOC_MODEL))
            .put("cameras", JSONArray(cams))
    }

    /** The back logical multi-camera (the one with physical lenses behind it), if any. */
    fun backLogical(cm: CameraManager): Pair<String, List<String>>? {
        for (id in cm.cameraIdList) {
            val c = cm.getCameraCharacteristics(id)
            if (c[CameraCharacteristics.LENS_FACING] == LENS_FACING_BACK && c.physicalCameraIds.isNotEmpty()) {
                return id to c.physicalCameraIds.sortedBy { it.toIntOrNull() ?: Int.MAX_VALUE }
            }
        }
        return null
    }

    private fun describe(id: String, c: CameraCharacteristics, listed: Boolean): JSONObject {
        val o = JSONObject().put("id", id).put("listed", listed)
        o.put("facing", when (c[CameraCharacteristics.LENS_FACING]) {
            LENS_FACING_FRONT -> "front"
            LENS_FACING_BACK -> "back"
            LENS_FACING_EXTERNAL -> "external"
            else -> "unknown"
        })
        o.put("hardware_level", levelName(c[CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL]))
        val caps = c[CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES] ?: IntArray(0)
        o.put("capabilities", JSONArray(caps.map { capabilityName(it) }))
        if (c.physicalCameraIds.isNotEmpty()) o.put("physical_ids", JSONArray(c.physicalCameraIds.toList()))

        val focals = c[CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS] ?: FloatArray(0)
        o.put("focal_mm", floats(focals))
        c[CameraCharacteristics.LENS_INFO_AVAILABLE_APERTURES]?.let { o.put("apertures", floats(it)) }
        val phys = c[CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE]
        val pixels = c[CameraCharacteristics.SENSOR_INFO_PIXEL_ARRAY_SIZE]
        val active = c[CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE]
        if (phys != null) o.put("sensor_mm", JSONArray(listOf(round(phys.width.toDouble(), 3), round(phys.height.toDouble(), 3))))
        if (pixels != null) o.put("pixel_array", JSONArray(listOf(pixels.width, pixels.height)))
        if (active != null) o.put("active_array", JSONArray(listOf(active.left, active.top, active.width(), active.height())))

        // Derived from the first focal length; the pixel pitch comes from the full array.
        if (focals.isNotEmpty() && phys != null && pixels != null && active != null) {
            val f = focals[0].toDouble()
            val pitch = phys.width / pixels.width.toDouble() // mm per pixel
            val activeW = active.width() * pitch
            val activeH = active.height() * pitch
            val fxActive = f / pitch
            o.put("pixel_um", round(pitch * 1000, 3))
            o.put("hfov_deg", round(Math.toDegrees(2 * atan(activeW / 2 / f)), 1))
            o.put("vfov_deg", round(Math.toDegrees(2 * atan(activeH / 2 / f)), 1))
            o.put("equiv_35mm", round(f * 43.27 / hypot(activeW, activeH), 1))
            o.put("fx_px_active", round(fxActive, 1))
            // A 16:9 output normally uses the full active width, scaled down.
            o.put("fx_px_3840", round(fxActive * 3840 / active.width(), 1))
            o.put("fx_px_1920", round(fxActive * 1920 / active.width(), 1))
        }
        c[CameraCharacteristics.LENS_INTRINSIC_CALIBRATION]?.let { o.put("intrinsics_fx_fy_cx_cy_s", floats(it)) }
        c[CameraCharacteristics.LENS_DISTORTION]?.let { o.put("distortion", floats(it)) }
        c[CameraCharacteristics.LENS_POSE_TRANSLATION]?.let { o.put("pose_translation_m", floats(it)) }
        c[CameraCharacteristics.LENS_POSE_ROTATION]?.let { o.put("pose_rotation_quat", floats(it)) }
        c[CameraCharacteristics.LENS_POSE_REFERENCE]?.let { o.put("pose_reference", it) }

        val minFocus = c[CameraCharacteristics.LENS_INFO_MINIMUM_FOCUS_DISTANCE] ?: 0f
        o.put("min_focus_diopters", round(minFocus.toDouble(), 3))
        o.put("fixed_focus", minFocus == 0f)
        if (minFocus > 0f) o.put("closest_focus_m", round(1.0 / minFocus, 3))
        c[CameraCharacteristics.LENS_INFO_FOCUS_DISTANCE_CALIBRATION]?.let {
            o.put("focus_calibration", when (it) { 0 -> "uncalibrated"; 1 -> "approximate"; 2 -> "calibrated"; else -> "$it" })
        }
        val ois = c[CameraCharacteristics.LENS_INFO_AVAILABLE_OPTICAL_STABILIZATION] ?: IntArray(0)
        o.put("ois", ois.contains(1))
        val vstab = c[CameraCharacteristics.CONTROL_AVAILABLE_VIDEO_STABILIZATION_MODES] ?: IntArray(0)
        o.put("video_stabilization_modes", JSONArray(vstab.toList()))
        c[CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES]?.let { ranges ->
            o.put("ae_fps_ranges", JSONArray(ranges.map { "${it.lower}-${it.upper}" }))
        }
        c[CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE]?.let { o.put("zoom_ratio", JSONArray(listOf(round(it.lower.toDouble(), 2), round(it.upper.toDouble(), 2)))) }

        c[CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP]?.let { map ->
            val yuv = JSONObject()
            map.getOutputSizes(ImageFormat.YUV_420_888)
                ?.filter { it.width >= 1280 }
                ?.sortedByDescending { it.width * it.height }
                ?.forEach { size ->
                    val ns = map.getOutputMinFrameDuration(ImageFormat.YUV_420_888, size)
                    yuv.put("${size.width}x${size.height}", if (ns > 0) round(1e9 / ns, 1) else JSONObject.NULL)
                }
            o.put("yuv_max_fps", yuv)
        }
        return o
    }

    /** Among the back physical lenses: widest = ultrawide, narrowest = telephoto. */
    private fun guessRoles(cams: List<JSONObject>) {
        val back = cams.filter { it.optString("facing") == "back" && !it.has("physical_ids") && it.has("hfov_deg") }
            .distinctBy { it.getString("id") }
            .sortedByDescending { it.getDouble("hfov_deg") }
        if (back.size < 2) return
        back.forEachIndexed { i, cam ->
            cam.put("role_guess", when {
                i == 0 -> "ultrawide"
                i == back.lastIndex && back.size >= 3 -> "telephoto"
                back.size <= 3 -> "main"
                else -> "unknown" // more than three lenses: only the extremes are clear
            })
        }
    }

    fun summary(cam: JSONObject): String {
        val sb = StringBuilder()
        sb.append("id ").append(cam.getString("id")).append(' ').append(cam.optString("facing"))
        if (cam.has("physical_ids")) sb.append("  LOGICAL -> ").append(cam.getJSONArray("physical_ids").join(","))
        if (!cam.getBoolean("listed")) sb.append("  (hidden physical)")
        cam.optString("role_guess").takeIf { it.isNotEmpty() }?.let { sb.append("  [").append(it).append(']') }
        sb.append('\n')
        val f = cam.optJSONArray("focal_mm")
        if (f != null && f.length() > 0) sb.append("   f ").append(f.join("/")).append(" mm")
        if (cam.has("equiv_35mm")) sb.append(" (").append(cam.get("equiv_35mm")).append(" mm eq)")
        if (cam.has("hfov_deg")) sb.append("  HFOV ").append(cam.get("hfov_deg")).append("°")
        if (cam.has("pixel_array")) sb.append("  ").append(cam.getJSONArray("pixel_array").join("x"))
        if (cam.has("pixel_um")) sb.append(" @ ").append(cam.get("pixel_um")).append(" µm")
        sb.append('\n')
        if (cam.has("fx_px_3840")) sb.append("   fx ≈ ").append(cam.get("fx_px_3840")).append(" px @3840, ")
            .append(cam.get("fx_px_1920")).append(" px @1920")
        sb.append(if (cam.getBoolean("fixed_focus")) "   fixed focus" else "   focus ≥ ${cam.opt("closest_focus_m")} m")
        sb.append(if (cam.getBoolean("ois")) "   OIS" else "")
        sb.append("   ").append(cam.optString("hardware_level"))
        cam.optJSONObject("yuv_max_fps")?.let { yuv ->
            val wanted = listOf("3840x2160", "1920x1080")
            val parts = wanted.filter { yuv.has(it) }.map { "$it ${yuv.get(it)} fps" }
            sb.append('\n').append("   YUV: ").append(if (parts.isEmpty()) "no 4K/1080p listed" else parts.joinToString(", "))
                .append("  (").append(yuv.length()).append(" sizes ≥1280)")
        }
        return sb.toString()
    }

    private fun floats(a: FloatArray) = JSONArray(a.map { round(it.toDouble(), 5) })

    private fun levelName(level: Int?) = when (level) {
        CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY -> "LEGACY"
        CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LIMITED -> "LIMITED"
        CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_FULL -> "FULL"
        CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_3 -> "LEVEL_3"
        CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_EXTERNAL -> "EXTERNAL"
        null -> "none"
        else -> "level_$level"
    }

    private fun capabilityName(cap: Int) = when (cap) {
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_BACKWARD_COMPATIBLE -> "BACKWARD_COMPATIBLE"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_MANUAL_SENSOR -> "MANUAL_SENSOR"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_MANUAL_POST_PROCESSING -> "MANUAL_POST_PROCESSING"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_RAW -> "RAW"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_READ_SENSOR_SETTINGS -> "READ_SENSOR_SETTINGS"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_BURST_CAPTURE -> "BURST_CAPTURE"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_DEPTH_OUTPUT -> "DEPTH_OUTPUT"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_CONSTRAINED_HIGH_SPEED_VIDEO -> "HIGH_SPEED_VIDEO"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_MOTION_TRACKING -> "MOTION_TRACKING"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_LOGICAL_MULTI_CAMERA -> "LOGICAL_MULTI_CAMERA"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_MONOCHROME -> "MONOCHROME"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_SECURE_IMAGE_DATA -> "SECURE_IMAGE_DATA"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_SYSTEM_CAMERA -> "SYSTEM_CAMERA"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_OFFLINE_PROCESSING -> "OFFLINE_PROCESSING"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_ULTRA_HIGH_RESOLUTION_SENSOR -> "ULTRA_HIGH_RESOLUTION"
        CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_REMOSAIC_REPROCESSING -> "REMOSAIC_REPROCESSING"
        else -> "cap_$cap"
    }
}
