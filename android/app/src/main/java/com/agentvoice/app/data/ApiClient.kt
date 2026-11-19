package com.agentvoice.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

class ApiException(val code: Int, message: String) : Exception(message)

/** Thin client for the AgentVoice server REST API. */
class ApiClient(baseUrl: String, private val token: String) {
    val baseUrl: String = normalizeBaseUrl(baseUrl)

    // ------------------------------------------------------------- status
    suspend fun status(): ServerStatus = obj("GET", "/api/status").toServerStatus()

    // ---------------------------------------------------------- campaigns
    suspend fun campaigns(): List<Campaign> = arr("GET", "/api/campaigns").mapObjects { it.toCampaign() }

    suspend fun createCampaign(name: String, extra: String, concurrent: Int, attempts: Int): Campaign =
        obj(
            "POST", "/api/campaigns",
            JSONObject()
                .put("name", name)
                .put("extra_instructions", extra)
                .put("max_concurrent", concurrent)
                .put("max_attempts", attempts),
        ).toCampaign()

    suspend fun updateCampaign(id: Long, name: String, extra: String, concurrent: Int, attempts: Int): Campaign =
        obj(
            "PATCH", "/api/campaigns/$id",
            JSONObject()
                .put("name", name)
                .put("extra_instructions", extra)
                .put("max_concurrent", concurrent)
                .put("max_attempts", attempts),
        ).toCampaign()

    suspend fun startCampaign(id: Long): Campaign = obj("POST", "/api/campaigns/$id/start").toCampaign()
    suspend fun pauseCampaign(id: Long): Campaign = obj("POST", "/api/campaigns/$id/pause").toCampaign()

    // -------------------------------------------------------------- leads
    suspend fun leads(campaignId: Long?, status: String?, query: String?, offset: Int = 0, limit: Int = 100): LeadPage {
        val params = buildList {
            add("limit=$limit")
            add("offset=$offset")
            campaignId?.let { add("campaign_id=$it") }
            if (!status.isNullOrBlank()) add("status=${enc(status)}")
            if (!query.isNullOrBlank()) add("q=${enc(query)}")
        }.joinToString("&")
        val json = obj("GET", "/api/leads?$params")
        return LeadPage(json.getJSONArray("items").mapObjects { it.toLead() }, json.optInt("total"))
    }

    suspend fun lead(id: Long): Lead = obj("GET", "/api/leads/$id").toLead()

    suspend fun createLead(fields: JSONObject): Lead = obj("POST", "/api/leads", fields).toLead()

    suspend fun updateLead(id: Long, fields: JSONObject): Lead = obj("PATCH", "/api/leads/$id", fields).toLead()

    suspend fun deleteLead(id: Long) {
        request("DELETE", "/api/leads/$id")
    }

    suspend fun callLead(id: Long): CallItem = obj("POST", "/api/leads/$id/call").toCallItem()

    suspend fun importLeads(csv: String, campaignId: Long?, consentConfirmed: Boolean): JSONObject {
        val params = buildList {
            campaignId?.let { add("campaign_id=$it") }
            if (consentConfirmed) add("consent_confirmed=true")
        }.joinToString("&")
        return JSONObject(request("POST", "/api/leads/import?$params", csv, "text/csv"))
    }

    // -------------------------------------------------------------- calls
    suspend fun calls(activeOnly: Boolean = false, limit: Int = 150): List<CallItem> =
        arr("GET", "/api/calls?limit=$limit&active=$activeOnly").mapObjects { it.toCallItem() }

    suspend fun call(id: Long): CallDetail = obj("GET", "/api/calls/$id").toCallDetail()

    suspend fun hangup(id: Long) {
        request("POST", "/api/calls/$id/hangup")
    }

    suspend fun testCall(): CallItem = obj("POST", "/api/test-call", JSONObject()).toCallItem()

    // ----------------------------------------------------------- meetings
    suspend fun meetings(upcoming: Boolean): List<Meeting> =
        arr("GET", "/api/meetings?upcoming=$upcoming").mapObjects { it.toMeeting() }

    suspend fun setMeetingStatus(id: Long, status: String): Meeting =
        obj("PATCH", "/api/meetings/$id", JSONObject().put("status", status)).toMeeting()

    // ---------------------------------------------------------------- dnc
    suspend fun addDnc(phone: String, reason: String) {
        request("POST", "/api/dnc", JSONObject().put("phone", phone).put("reason", reason).toString())
    }

    // ----------------------------------------------------------- settings
    suspend fun agentSettings(): JSONObject = obj("GET", "/api/settings/agent")
    suspend fun saveAgentSettings(body: JSONObject): JSONObject = obj("PUT", "/api/settings/agent", body)
    suspend fun playbook(): JSONObject = obj("GET", "/api/settings/playbook")
    suspend fun savePlaybook(body: JSONObject): JSONObject = obj("PUT", "/api/settings/playbook", body)

    // ------------------------------------------------------------ plumbing
    private suspend fun obj(method: String, path: String, body: JSONObject? = null): JSONObject =
        JSONObject(request(method, path, body?.toString()))

    private suspend fun arr(method: String, path: String): JSONArray = JSONArray(request(method, path))

    private suspend fun request(
        method: String,
        path: String,
        body: String? = null,
        contentType: String = "application/json",
    ): String = withContext(Dispatchers.IO) {
        val conn = URL(baseUrl + path).openConnection() as HttpURLConnection
        try {
            // HttpURLConnection has no PATCH; the server honours X-HTTP-Method-Override.
            if (method == "PATCH") {
                conn.requestMethod = "POST"
                conn.setRequestProperty("X-HTTP-Method-Override", "PATCH")
            } else {
                conn.requestMethod = method
            }
            conn.connectTimeout = 10_000
            conn.readTimeout = 30_000
            conn.setRequestProperty("Authorization", "Bearer $token")
            conn.setRequestProperty("Accept", "application/json")
            if (body != null) {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "$contentType; charset=utf-8")
                conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            } else if (method == "POST" || method == "PUT") {
                conn.doOutput = true
                conn.setFixedLengthStreamingMode(0)
                conn.outputStream.close()
            }
            val code = conn.responseCode
            val stream = if (code >= 400) conn.errorStream else conn.inputStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
            if (code >= 400) throw ApiException(code, errorMessage(code, text))
            text.ifEmpty { "{}" }
        } catch (e: IOException) {
            throw ApiException(0, "Can't reach the server at $baseUrl (${e.message ?: e.javaClass.simpleName})")
        } finally {
            conn.disconnect()
        }
    }

    companion object {
        fun normalizeBaseUrl(raw: String): String {
            var url = raw.trim().trimEnd('/')
            if (url.isNotEmpty() && !url.startsWith("http://") && !url.startsWith("https://")) {
                url = "https://$url"
            }
            return url
        }

        fun errorMessage(code: Int, body: String): String {
            if (code == 401) return "Invalid API token"
            return try {
                val detail = JSONObject(body).opt("detail")
                when (detail) {
                    is String -> detail
                    is JSONArray -> detail.mapObjects { it.optString("msg") }.joinToString("; ")
                    else -> "Server error ($code)"
                }
            } catch (_: Exception) {
                "Server error ($code)"
            }
        }

        private fun enc(value: String) = URLEncoder.encode(value, "UTF-8")
    }
}
