package com.agentvoice.app.data

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.os.Handler
import android.os.Looper
import android.util.Base64
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Live conversation with the sales agent over the phone-less practice WebSocket:
 * PCM16 24 kHz in both directions, same protocol the Linux dashboard uses.
 * Zadarma (or any SIP path) will later replace this mic/speaker hop with call audio.
 */
class PracticeSession(
    context: Context,
    private val url: String,
    private val listener: Listener,
) {
    interface Listener {
        fun onReady(callId: Long?)
        fun onTranscript(role: String, text: String)
        fun onTool(name: String, result: String)
        fun onStatus(text: String)
        fun onTalking(talking: Boolean)
        fun onEnded(reason: String)
        fun onError(message: String)
    }

    private val app = context.applicationContext
    private val main = Handler(Looper.getMainLooper())
    private val running = AtomicBoolean(false)
    private val cleaned = AtomicBoolean(false)

    private var socket: WebSocket? = null
    private var recorder: AudioRecord? = null
    private var track: AudioTrack? = null
    private var echo: AcousticEchoCanceler? = null
    private var noise: NoiseSuppressor? = null
    private var agc: AutomaticGainControl? = null
    private var captureThread: Thread? = null
    private var queuedMs = 0.0
    private var lastPlayAt = 0L

    fun start() {
        if (!running.compareAndSet(false, true)) return
        val request = Request.Builder().url(url).build()
        socket = httpClient().newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (!running.get()) return
                main.post { listener.onStatus("Connected. Say “Hello?” like you’re answering the phone.") }
                startAudio()
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleMessage(text)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                stopInternal("Call ended")
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                main.post { listener.onError(t.message ?: "Connection failed") }
                stopInternal("Connection failed")
            }
        })
    }

    fun stop() {
        try {
            socket?.send(JSONObject().put("type", "stop").toString())
        } catch (_: Exception) {
        }
        socket?.close(1000, "user hangup")
        stopInternal("You hung up")
    }

    private fun handleMessage(text: String) {
        val msg = try {
            JSONObject(text)
        } catch (_: Exception) {
            return
        }
        when (msg.optString("type")) {
            "audio" -> playPcm(msg.optString("audio"))
            "clear" -> main.post { flushPlayback() }
            "mark" -> scheduleMark(msg.optString("name"))
            "ready" -> {
                val id = if (msg.has("call_id") && !msg.isNull("call_id")) msg.optLong("call_id") else null
                main.post { listener.onReady(id) }
            }
            "error" -> main.post { listener.onError(msg.optString("message", "Server error")) }
            "hangup" -> main.post {
                listener.onStatus("The agent ended the call.")
                stopInternal("Agent hung up")
            }
            "event" -> {
                val event = msg.optJSONObject("event") ?: return
                when (event.optString("type")) {
                    "transcript" -> main.post {
                        listener.onTranscript(event.optString("role"), event.optString("text"))
                    }
                    "tool" -> main.post {
                        listener.onTool(event.optString("name"), event.opt("result")?.toString().orEmpty())
                    }
                    "speech_started" -> main.post { flushPlayback() }
                }
            }
        }
    }

    private fun startAudio() {
        val buf = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            .coerceAtLeast(CHUNK_BYTES * 4)
        val rec = AudioRecord(
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            buf,
        )
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            main.post { listener.onError("Microphone is not available") }
            stopInternal("mic failed")
            return
        }
        attachFx(rec.audioSessionId)
        val out = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(SAMPLE_RATE)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build(),
            )
            .setBufferSizeInBytes(buf)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        recorder = rec
        track = out
        val am = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        am.mode = AudioManager.MODE_IN_COMMUNICATION
        am.isSpeakerphoneOn = true
        out.play()
        rec.startRecording()
        captureThread = Thread({ captureLoop(rec) }, "agentvoice-mic").also { it.start() }
    }

    private fun captureLoop(rec: AudioRecord) {
        val chunk = ByteArray(CHUNK_BYTES)
        while (running.get()) {
            val n = rec.read(chunk, 0, chunk.size)
            if (n <= 0 || !running.get()) continue
            val payload = Base64.encodeToString(chunk, 0, n, Base64.NO_WRAP)
            socket?.send(JSONObject().put("type", "audio").put("audio", payload).toString())
        }
    }

    private fun playPcm(b64: String) {
        val pcm = try {
            Base64.decode(b64, Base64.DEFAULT)
        } catch (_: Exception) {
            return
        }
        if (pcm.isEmpty()) return
        val t = track ?: return
        t.write(pcm, 0, pcm.size)
        queuedMs += pcm.size / BYTES_PER_MS
        lastPlayAt = System.currentTimeMillis()
        main.post { listener.onTalking(true) }
        main.removeCallbacks(idleTalking)
        main.postDelayed(idleTalking, 250)
    }

    private val idleTalking = Runnable { listener.onTalking(false) }

    private fun flushPlayback() {
        main.removeCallbacks(idleTalking)
        queuedMs = 0.0
        lastPlayAt = 0L
        try {
            track?.pause()
            track?.flush()
            track?.play()
        } catch (_: Exception) {
        }
        listener.onTalking(false)
    }

    private fun scheduleMark(name: String) {
        val elapsed = if (lastPlayAt == 0L) queuedMs else (queuedMs - (System.currentTimeMillis() - lastPlayAt)).coerceAtLeast(0.0)
        main.postDelayed({
            if (!running.get()) return@postDelayed
            try {
                socket?.send(JSONObject().put("type", "mark").put("name", name).toString())
            } catch (_: Exception) {
            }
        }, elapsed.toLong().coerceIn(0L, 20_000L))
    }

    private fun attachFx(sessionId: Int) {
        if (AcousticEchoCanceler.isAvailable()) {
            echo = AcousticEchoCanceler.create(sessionId)?.also { it.enabled = true }
        }
        if (NoiseSuppressor.isAvailable()) {
            noise = NoiseSuppressor.create(sessionId)?.also { it.enabled = true }
        }
        if (AutomaticGainControl.isAvailable()) {
            agc = AutomaticGainControl.create(sessionId)?.also { it.enabled = true }
        }
    }

    private fun stopInternal(reason: String) {
        running.set(false)
        if (!cleaned.compareAndSet(false, true)) return
        main.removeCallbacksAndMessages(null)
        captureThread?.interrupt()
        captureThread = null
        try {
            recorder?.stop()
        } catch (_: Exception) {
        }
        recorder?.release()
        recorder = null
        try {
            track?.stop()
        } catch (_: Exception) {
        }
        track?.release()
        track = null
        echo?.release(); echo = null
        noise?.release(); noise = null
        agc?.release(); agc = null
        val am = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        am.isSpeakerphoneOn = false
        am.mode = AudioManager.MODE_NORMAL
        socket?.cancel()
        socket = null
        main.post { listener.onEnded(reason) }
    }

    companion object {
        const val SAMPLE_RATE = 24_000
        const val CHUNK_BYTES = 960 * 2 // 40 ms of PCM16 mono
        const val BYTES_PER_MS = 48.0

        private val sharedHttp: OkHttpClient by lazy {
            OkHttpClient.Builder()
                .readTimeout(0, TimeUnit.MILLISECONDS)
                .pingInterval(20, TimeUnit.SECONDS)
                .build()
        }

        private fun httpClient(): OkHttpClient = sharedHttp
    }
}
