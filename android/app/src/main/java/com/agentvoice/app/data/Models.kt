package com.agentvoice.app.data

import org.json.JSONArray
import org.json.JSONObject

data class Stats(
    val dials: Int,
    val conversations: Int,
    val voicemails: Int,
    val talkMinutes: Double,
    val meetingsBooked: Int,
    val activeCalls: Int,
)

data class ServerStatus(
    val version: String,
    val twilioConfigured: Boolean,
    val openaiConfigured: Boolean,
    val voiceAvailable: Boolean,
    val voiceBackend: String,
    val ollamaModel: String,
    val fromNumber: String,
    val runningCampaigns: List<Long>,
    val today: Stats,
    val week: Stats,
)

data class Campaign(
    val id: Long,
    val name: String,
    val status: String,
    val extraInstructions: String,
    val maxConcurrent: Int,
    val maxAttempts: Int,
    val retryMinutes: Int,
    val leadCount: Int,
    val remaining: Int,
    val meetings: Int,
)

data class CallItem(
    val id: Long,
    val leadId: Long?,
    val kind: String,
    val direction: String,
    val phone: String,
    val status: String,
    val outcome: String,
    val leadScore: String,
    val summary: String,
    val nextStep: String,
    val error: String,
    val answeredBy: String,
    val durationS: Int,
    val createdAt: String,
    val businessName: String,
)

data class TranscriptLine(val role: String, val text: String, val at: String)

data class CallDetail(val call: CallItem, val transcript: List<TranscriptLine>, val live: Boolean)

data class Lead(
    val id: Long,
    val campaignId: Long?,
    val phone: String,
    val businessName: String,
    val contactName: String,
    val email: String,
    val website: String,
    val industry: String,
    val city: String,
    val state: String,
    val consent: Boolean,
    val notes: String,
    val status: String,
    val statusReason: String,
    val attempts: Int,
    val score: String,
    val updatedAt: String,
    val qualification: Map<String, String>,
    val talkingPoints: List<String>,
    val callableNow: Boolean?,
    val callableReason: String,
    val localTimezone: String,
    val dnc: Boolean,
    val calls: List<CallItem>,
)

data class LeadPage(val items: List<Lead>, val total: Int)

data class Meeting(
    val id: Long,
    val startsAt: String,
    val timezone: String,
    val contactName: String,
    val email: String,
    val phone: String,
    val notes: String,
    val status: String,
    val businessName: String,
    val website: String,
)

val LEAD_STATUSES = listOf(
    "new", "calling", "retry", "callback", "contacted", "interested", "meeting_booked",
    "not_interested", "do_not_call", "wrong_number", "invalid", "exhausted", "blocked",
)

// ----------------------------------------------------------------- parsing
private fun JSONObject.str(key: String): String = if (isNull(key)) "" else optString(key, "")
private fun JSONObject.longOrNull(key: String): Long? = if (has(key) && !isNull(key)) optLong(key) else null

inline fun <T> JSONArray.mapObjects(block: (JSONObject) -> T): List<T> =
    (0 until length()).map { block(getJSONObject(it)) }

fun JSONObject.toStats() = Stats(
    dials = optInt("dials"),
    conversations = optInt("conversations"),
    voicemails = optInt("voicemails"),
    talkMinutes = optDouble("talk_minutes", 0.0),
    meetingsBooked = optInt("meetings_booked"),
    activeCalls = optInt("active_calls"),
)

fun JSONObject.toServerStatus(): ServerStatus {
    val running = optJSONArray("running_campaigns") ?: JSONArray()
    return ServerStatus(
        version = str("version"),
        twilioConfigured = optBoolean("twilio_configured"),
        openaiConfigured = optBoolean("openai_configured"),
        voiceAvailable = if (has("voice_available")) optBoolean("voice_available") else optBoolean("openai_configured"),
        voiceBackend = str("voice_backend"),
        ollamaModel = str("ollama_model"),
        fromNumber = str("from_number"),
        runningCampaigns = (0 until running.length()).map { running.getLong(it) },
        today = getJSONObject("today").toStats(),
        week = getJSONObject("last_7_days").toStats(),
    )
}

fun JSONObject.toCampaign() = Campaign(
    id = getLong("id"),
    name = str("name"),
    status = str("status"),
    extraInstructions = str("extra_instructions"),
    maxConcurrent = optInt("max_concurrent", 1),
    maxAttempts = optInt("max_attempts", 3),
    retryMinutes = optInt("retry_minutes", 1440),
    leadCount = optInt("lead_count"),
    remaining = optInt("remaining"),
    meetings = optInt("meetings"),
)

fun JSONObject.toCallItem() = CallItem(
    id = getLong("id"),
    leadId = longOrNull("lead_id"),
    kind = str("kind"),
    direction = str("direction"),
    phone = str("phone"),
    status = str("status"),
    outcome = str("outcome"),
    leadScore = str("lead_score"),
    summary = str("summary"),
    nextStep = str("next_step"),
    error = str("error"),
    answeredBy = str("answered_by"),
    durationS = optInt("duration_s"),
    createdAt = str("created_at"),
    businessName = str("business_name").ifEmpty { optJSONObject("lead")?.str("business_name") ?: "" },
)

fun JSONObject.toCallDetail() = CallDetail(
    call = toCallItem(),
    transcript = (optJSONArray("transcript") ?: JSONArray()).mapObjects {
        TranscriptLine(it.str("role"), it.str("text"), it.str("at"))
    },
    live = optBoolean("live"),
)

fun JSONObject.toLead(): Lead {
    val qual = optJSONObject("qualification") ?: JSONObject()
    val research = optJSONObject("research") ?: JSONObject()
    val points = research.optJSONArray("talking_points") ?: JSONArray()
    return Lead(
        id = getLong("id"),
        campaignId = longOrNull("campaign_id"),
        phone = str("phone"),
        businessName = str("business_name"),
        contactName = str("contact_name"),
        email = str("email"),
        website = str("website"),
        industry = str("industry"),
        city = str("city"),
        state = str("state"),
        consent = optBoolean("consent"),
        notes = str("notes"),
        status = str("status"),
        statusReason = str("status_reason"),
        attempts = optInt("attempts"),
        score = str("score"),
        updatedAt = str("updated_at"),
        qualification = qual.keys().asSequence().associateWith { qual.optString(it) },
        talkingPoints = (0 until points.length()).map { points.getString(it) },
        callableNow = if (has("callable_now")) optBoolean("callable_now") else null,
        callableReason = str("callable_reason"),
        localTimezone = str("local_timezone"),
        dnc = optBoolean("dnc"),
        calls = (optJSONArray("calls") ?: JSONArray()).mapObjects { it.toCallItem() },
    )
}

fun JSONObject.toMeeting() = Meeting(
    id = getLong("id"),
    startsAt = str("starts_at"),
    timezone = str("timezone"),
    contactName = str("contact_name"),
    email = str("email"),
    phone = str("phone"),
    notes = str("notes"),
    status = str("status"),
    businessName = str("business_name"),
    website = str("website"),
)
