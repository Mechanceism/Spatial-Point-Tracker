package io.github.mechanceism.sptcamera

import org.json.JSONArray
import org.json.JSONObject
import org.opencv.core.CvType
import org.opencv.core.Mat
import org.opencv.core.Size
import org.opencv.objdetect.ArucoDetector
import org.opencv.objdetect.CharucoBoard
import org.opencv.objdetect.CharucoDetector
import org.opencv.objdetect.DetectorParameters
import org.opencv.objdetect.Objdetect

/**
 * A ChArUco board, defined exactly like spt/boards.py on the PC: squares across x and
 * y, square and marker size in mm, marker IDs from `firstId` upwards.
 */
data class BoardSpec(val name: String, val squaresX: Int, val squaresY: Int, val squareMm: Float, val markerMm: Float, val firstId: Int) {
    val markerCount: Int get() = squaresX * squaresY / 2
    val ids: IntRange get() = firstId until firstId + markerCount

    fun json(): JSONObject = JSONObject().put("name", name).put("squares_x", squaresX).put("squares_y", squaresY)
        .put("square_mm", squareMm.toDouble()).put("marker_mm", markerMm.toDouble()).put("first_id", firstId)

    companion object {
        // Same as spt/boards.py (REFERENCE, TARGET, PROBE).
        val DEFAULTS = listOf(
            BoardSpec("reference", 7, 5, 30f, 22f, 0),
            BoardSpec("target", 5, 4, 25f, 18f, 20),
            BoardSpec("probe", 4, 8, 15f, 10.5f, 40),
        )

        fun fromJson(o: JSONObject) = BoardSpec(
            o.getString("name"), o.getInt("squares_x"), o.getInt("squares_y"),
            o.getDouble("square_mm").toFloat(), o.getDouble("marker_mm").toFloat(), o.getInt("first_id"),
        )
    }
}

/**
 * How hard to work. `aruco3` finds markers on a reduced image and refines them at full
 * resolution; `minMarkerPx` (smallest marker side to look for, in pixels) sets how much
 * it may reduce. `maxFps` caps detections per lens per second (heat and battery).
 */
data class DetectorConfig(
    val maxFps: Double = 10.0,
    val aruco3: Boolean = true,
    val minMarkerPx: Double = 40.0,
    val searchFps: Double = 2.0, // rate for a lens that has seen no marker for a second
) {
    fun json(): JSONObject = JSONObject().put("max_fps", maxFps).put("aruco3", aruco3).put("min_marker_px", minMarkerPx)
        .put("search_fps", searchFps)

    fun with(o: JSONObject) = DetectorConfig(
        o.optDouble("max_fps", maxFps), o.optBoolean("aruco3", aruco3), o.optDouble("min_marker_px", minMarkerPx),
        o.optDouble("search_fps", searchFps),
    )
}

/** What one frame shows: every marker (refined corners) and the ChArUco corners per board. */
class Detections(
    val markerIds: IntArray,
    val markerCorners: List<FloatArray>, // 8 floats per marker: x0 y0 .. x3 y3 (TL, TR, BR, BL)
    val boards: Map<String, Pair<IntArray, FloatArray>>, // name -> (corner ids, x y per corner)
    val detectMs: Double,
) {
    fun json(): JSONObject {
        val markers = JSONArray()
        for (i in markerIds.indices) {
            markers.put(JSONObject().put("id", markerIds[i]).put("c", JSONArray(markerCorners[i].map { round(it.toDouble(), 3) })))
        }
        val b = JSONObject()
        for ((name, v) in boards) {
            b.put(name, JSONObject().put("ids", JSONArray(v.first.toList())).put("c", JSONArray(v.second.map { round(it.toDouble(), 3) })))
        }
        return JSONObject().put("markers", markers).put("boards", b).put("detect_ms", round(detectMs, 1))
    }
}

/**
 * Detects ArUco markers (DICT_5X5_250, sub-pixel corners) and ChArUco board corners in a
 * grayscale image. One marker pass serves every board. Not thread-safe: use one
 * Detector per thread.
 */
class Detector(boards: List<BoardSpec>, private val config: DetectorConfig = DetectorConfig()) {
    // DICT_5X5_250: its first 100 markers are DICT_5X5_100 (boards 0-55, prisms 60-83),
    // and loose reference markers use IDs 100-249.
    private val dictionary = Objdetect.getPredefinedDictionary(Objdetect.DICT_5X5_250)
    private val params = DetectorParameters().apply {
        set_cornerRefinementMethod(Objdetect.CORNER_REFINE_SUBPIX)
        set_useAruco3Detection(config.aruco3)
    }
    private val markerDetector = ArucoDetector(dictionary, params)
    private var ratioFor = 0 // image size the ArUco3 ratio was set for
    private val boardDetectors = boards.map { spec ->
        val ids = Mat(spec.markerCount, 1, CvType.CV_32S).apply { put(0, 0, spec.ids.toList().toIntArray()) }
        val board = CharucoBoard(Size(spec.squaresX.toDouble(), spec.squaresY.toDouble()), spec.squareMm, spec.markerMm, dictionary, ids)
        Triple(spec, CharucoDetector(board), spec.ids)
    }

    fun detect(gray: Mat): Detections {
        val t0 = System.nanoTime()
        val side = maxOf(gray.cols(), gray.rows())
        if (config.aruco3 && side != ratioFor) {
            // ArUco3 works on an image reduced so the smallest wanted marker is ~32 px.
            params.set_minMarkerLengthRatioOriginalImg((config.minMarkerPx / side).toFloat())
            markerDetector.setDetectorParameters(params)
            ratioFor = side
        }
        val corners = ArrayList<Mat>()
        val idsMat = Mat()
        markerDetector.detectMarkers(gray, corners, idsMat)
        // OpenCV 5's Java binding returns the IDs as one row (1 x n), so count elements,
        // not rows. Keep IDs and corners paired even if the two ever disagree.
        val n = minOf(idsMat.total().toInt(), corners.size)
        val ids = IntArray(idsMat.total().toInt())
        if (ids.isNotEmpty()) idsMat.get(0, 0, ids)
        val flat = corners.take(n).map { m -> FloatArray(8).also { m.get(0, 0, it) } }
        val boards = HashMap<String, Pair<IntArray, FloatArray>>()
        for ((spec, detector, range) in boardDetectors) {
            val pick = (0 until n).filter { ids[it] in range }
            if (pick.size < 2) continue
            val subCorners = ArrayList<Mat>(pick.map { corners[it] })
            val subIds = Mat(pick.size, 1, CvType.CV_32S).apply { put(0, 0, pick.map { ids[it] }.toIntArray()) }
            val chCorners = Mat()
            val chIds = Mat()
            detector.detectBoard(gray, chCorners, chIds, subCorners, subIds)
            val k = minOf(chIds.total().toInt(), chCorners.total().toInt())
            if (k > 0) {
                val cid = IntArray(chIds.total().toInt()).also { chIds.get(0, 0, it) }
                val cxy = FloatArray(2 * chCorners.total().toInt()).also { chCorners.get(0, 0, it) }
                boards[spec.name] = cid.copyOf(k) to cxy.copyOf(2 * k)
            }
            subIds.release(); chCorners.release(); chIds.release()
        }
        idsMat.release()
        corners.forEach { it.release() }
        return Detections(ids.copyOf(n), flat, boards, (System.nanoTime() - t0) / 1e6)
    }
}
