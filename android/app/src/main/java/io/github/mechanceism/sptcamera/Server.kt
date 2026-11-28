package io.github.mechanceism.sptcamera

import android.net.LocalServerSocket
import android.net.LocalSocket
import org.json.JSONObject
import java.io.BufferedReader
import java.io.BufferedWriter
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.util.concurrent.LinkedBlockingDeque
import java.util.concurrent.TimeUnit

/**
 * Link to the PC over the USB cable, with no network and no extra permission.
 *
 * The app listens on a local (Unix, abstract) socket called "sptcamera". Only adb
 * can reach it: on the PC, `adb forward tcp:8765 localabstract:sptcamera` makes it
 * appear as localhost:8765. One client at a time.
 *
 * Phone -> PC: one JSON object per line ("hello" on connect, then "frame" per
 * detected frame, "status" now and then). PC -> phone: one JSON command per line,
 * handed to `onCommand`. If the PC reads too slowly, the oldest frames are dropped.
 */
class Server(
    private val hello: () -> JSONObject,
    private val onCommand: (JSONObject) -> JSONObject?, // returns a reply, if any
    private val log: (String) -> Unit,
) {
    companion object {
        const val SOCKET_NAME = "sptcamera"
        const val QUEUE = 120 // lines (~1.3 s at 3 x 30 fps)
    }

    private val queue = LinkedBlockingDeque<String>(QUEUE)
    @Volatile var connected = false
        private set
    @Volatile var dropped = 0L
        private set
    @Volatile private var running = true
    private var server: LocalServerSocket? = null
    @Volatile private var client: LocalSocket? = null
    private val acceptThread = Thread({ acceptLoop() }, "spt-server")

    fun start() {
        server = LocalServerSocket(SOCKET_NAME)
        acceptThread.start()
    }

    /** Queue a message for the PC (dropped when no PC is connected). */
    fun send(message: JSONObject) {
        if (!connected) return
        val line = message.toString()
        while (!queue.offerLast(line)) {
            queue.pollFirst()
            dropped++
        }
    }

    private fun acceptLoop() {
        while (running) {
            val socket = try { server?.accept() } catch (_: Exception) { null } ?: break
            if (!running) { socket.close(); break }
            client = socket
            queue.clear()
            connected = true
            log("PC connected")
            val writer = BufferedWriter(OutputStreamWriter(socket.outputStream, Charsets.UTF_8))
            val sender = Thread({
                try {
                    writer.write(hello().put("type", "hello").toString()); writer.write("\n"); writer.flush()
                    while (connected) {
                        val line = queue.pollFirst(200, TimeUnit.MILLISECONDS) ?: continue
                        writer.write(line); writer.write("\n")
                        if (queue.isEmpty()) writer.flush()
                    }
                } catch (_: Exception) {
                    connected = false
                }
            }, "spt-sender").apply { start() }
            try {
                val reader = BufferedReader(InputStreamReader(socket.inputStream, Charsets.UTF_8))
                while (running) {
                    val line = reader.readLine() ?: break
                    val reply = try { onCommand(JSONObject(line)) } catch (e: Exception) {
                        JSONObject().put("type", "error").put("message", "bad command: ${e.message}")
                    }
                    reply?.let { send(it) }
                }
            } catch (_: Exception) {
            } finally {
                connected = false
                sender.join(1000)
                try { socket.close() } catch (_: Exception) {}
                client = null
                log("PC disconnected")
            }
        }
    }

    fun stop() {
        running = false
        connected = false
        try { client?.shutdownInput() } catch (_: Exception) {}
        try { client?.close() } catch (_: Exception) {}
        // LocalServerSocket.accept() is not interrupted by close() on every Android
        // version: wake it with a throwaway connection first, then close.
        try { LocalSocket().apply { connect(android.net.LocalSocketAddress(SOCKET_NAME)) }.close() } catch (_: Exception) {}
        acceptThread.join(1000)
        try { server?.close() } catch (_: Exception) {}
    }
}
