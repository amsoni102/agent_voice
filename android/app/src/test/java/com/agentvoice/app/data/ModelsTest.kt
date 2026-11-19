package com.agentvoice.app.data

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ModelsTest {
    @Test
    fun normalizesServerUrls() {
        assertEquals("https://agent.example.com", ApiClient.normalizeBaseUrl(" agent.example.com/ "))
        assertEquals("http://192.168.1.20:8080", ApiClient.normalizeBaseUrl("http://192.168.1.20:8080/"))
    }

    @Test
    fun extractsServerErrorMessages() {
        assertEquals("Invalid API token", ApiClient.errorMessage(401, "{}"))
        assertEquals("cannot call now: outside calling hours", ApiClient.errorMessage(409, """{"detail":"cannot call now: outside calling hours"}"""))
        assertEquals("a; b", ApiClient.errorMessage(422, """{"detail":[{"msg":"a"},{"msg":"b"}]}"""))
        assertEquals("Server error (500)", ApiClient.errorMessage(500, "<html>"))
    }

    @Test
    fun parsesLeadDetail() {
        val json = JSONObject(
            """
            {"id": 7, "campaign_id": null, "phone": "+15125550142", "business_name": "Joe's Tacos",
             "contact_name": "Joe", "email": "", "website": "joestacos.com", "industry": "restaurant",
             "city": "Austin", "state": "TX", "consent": true, "notes": "", "status": "meeting_booked",
             "status_reason": "", "attempts": 1, "score": "hot", "updated_at": "2026-11-19T18:00:00+00:00",
             "qualification": {"pain_points": "no online ordering"},
             "research": {"talking_points": ["Their site is slow."]},
             "callable_now": false, "callable_reason": "outside calling hours", "local_timezone": "America/Chicago",
             "dnc": false,
             "calls": [{"id": 3, "lead_id": 7, "kind": "campaign", "direction": "outbound", "phone": "+15125550142",
                        "status": "completed", "outcome": "meeting_booked", "lead_score": "hot", "summary": "Booked.",
                        "next_step": "", "error": "", "answered_by": "human", "duration_s": 184,
                        "created_at": "2026-11-19T17:00:00+00:00", "business_name": "Joe's Tacos"}]}
            """.trimIndent()
        )
        val lead = json.toLead()
        assertNull(lead.campaignId)
        assertEquals("Joe's Tacos", lead.businessName)
        assertEquals("no online ordering", lead.qualification["pain_points"])
        assertEquals(listOf("Their site is slow."), lead.talkingPoints)
        assertEquals(false, lead.callableNow)
        assertEquals(184, lead.calls.single().durationS)
        assertTrue(lead.consent)
    }
}
