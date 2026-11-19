package com.agentvoice.app.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.agentvoice.app.ui.LocalApp
import com.agentvoice.app.ui.components.Picker
import com.agentvoice.app.ui.components.ResourceView
import com.agentvoice.app.ui.components.SectionCard
import com.agentvoice.app.ui.components.rememberResource
import org.json.JSONObject

private val VOICES = listOf("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")

private data class SettingsBundle(val agent: JSONObject, val playbook: JSONObject)

@Composable
fun SettingsScreen() {
    val app = LocalApp.current
    val res = rememberResource { SettingsBundle(app.api.agentSettings(), app.api.playbook()) }
    ResourceView(res) { bundle -> SettingsForm(bundle, onSaved = res.reload) }
}

@Composable
private fun SettingsForm(bundle: SettingsBundle, onSaved: () -> Unit) {
    val app = LocalApp.current
    val a = bundle.agent
    val p = bundle.playbook

    var voice by remember { mutableStateOf(a.optString("voice", "marin")) }
    var speed by remember { mutableStateOf(a.optDouble("speech_speed", 1.0).toString()) }
    var windowStart by remember { mutableStateOf(a.optString("call_window_start")) }
    var windowEnd by remember { mutableStateOf(a.optString("call_window_end")) }
    var requireConsent by remember { mutableStateOf(a.optBoolean("require_consent", true)) }
    var leaveVoicemail by remember { mutableStateOf(a.optString("voicemail_action") == "leave_message") }
    var transfer by remember { mutableStateOf(a.optString("transfer_number")) }
    var testPhone by remember { mutableStateOf(a.optString("test_phone_number")) }
    var meetingTz by remember { mutableStateOf(a.optString("meeting_timezone")) }

    var agentName by remember { mutableStateOf(p.optString("agent_name")) }
    var company by remember { mutableStateOf(p.optString("company_name")) }
    var companySite by remember { mutableStateOf(p.optString("company_website")) }
    var callback by remember { mutableStateOf(p.optString("callback_number")) }
    var disclosure by remember { mutableStateOf(p.optString("disclosure")) }
    var offer by remember { mutableStateOf(p.optString("offer_summary")) }
    var pricing by remember { mutableStateOf(p.optString("pricing_guidance")) }
    var meetingGoal by remember { mutableStateOf(p.optString("meeting_goal")) }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Agent", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)

        SectionCard("Voice") {
            Picker("Voice", VOICES, voice, { it }, { voice = it })
            Text(
                "marin and cedar sound the most natural on the phone.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            TextField("Speaking speed (0.8 – 1.2)", speed, KeyboardType.Decimal) { speed = it }
        }

        SectionCard("Calling rules (prospect's local time)") {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(windowStart, { windowStart = it }, label = { Text("From") }, singleLine = true, modifier = Modifier.weight(1f))
                OutlinedTextField(windowEnd, { windowEnd = it }, label = { Text("Until (max 20:00)") }, singleLine = true, modifier = Modifier.weight(1f))
            }
            Toggle("Only call leads with recorded consent (recommended)", requireConsent) { requireConsent = it }
            Toggle("Leave a short voicemail (otherwise hang up)", leaveVoicemail) { leaveVoicemail = it }
        }

        SectionCard("Phones & calendar") {
            TextField("Transfer hot leads to (your phone)", transfer, KeyboardType.Phone) { transfer = it }
            TextField("Test phone number (your phone)", testPhone, KeyboardType.Phone) { testPhone = it }
            TextField("Meeting time zone", meetingTz) { meetingTz = it }
        }

        Button(
            onClick = {
                app.action("Agent settings saved") {
                    app.api.saveAgentSettings(
                        JSONObject()
                            .put("voice", voice)
                            .put("speech_speed", speed.toDoubleOrNull() ?: 1.0)
                            .put("call_window_start", windowStart.trim())
                            .put("call_window_end", windowEnd.trim())
                            .put("require_consent", requireConsent)
                            .put("voicemail_action", if (leaveVoicemail) "leave_message" else "hangup")
                            .put("transfer_number", transfer.trim())
                            .put("test_phone_number", testPhone.trim())
                            .put("meeting_timezone", meetingTz.trim()),
                    )
                    onSaved()
                }
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Save agent settings") }

        Text("Sales playbook", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
        Text(
            "Lists (services, discovery questions, objection handling) are edited in the desktop dashboard.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        SectionCard("Identity") {
            TextField("Agent's first name", agentName) { agentName = it }
            TextField("Company name", company) { company = it }
            TextField("Company website", companySite, KeyboardType.Uri) { companySite = it }
            TextField("Callback number", callback, KeyboardType.Phone) { callback = it }
            TextField("AI + recording disclosure (required)", disclosure, multiline = true) { disclosure = it }
        }
        SectionCard("Offer") {
            TextField("Offer summary", offer, multiline = true) { offer = it }
            TextField("Pricing guidance", pricing, multiline = true) { pricing = it }
            TextField("What the agent books", meetingGoal, multiline = true) { meetingGoal = it }
        }
        Button(
            onClick = {
                app.action("Playbook saved") {
                    app.api.savePlaybook(
                        JSONObject()
                            .put("agent_name", agentName.trim())
                            .put("company_name", company.trim())
                            .put("company_website", companySite.trim())
                            .put("callback_number", callback.trim())
                            .put("disclosure", disclosure.trim())
                            .put("offer_summary", offer.trim())
                            .put("pricing_guidance", pricing.trim())
                            .put("meeting_goal", meetingGoal.trim()),
                    )
                    onSaved()
                }
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Save playbook") }
    }
}

@Composable
private fun TextField(
    label: String,
    value: String,
    keyboard: KeyboardType = KeyboardType.Text,
    multiline: Boolean = false,
    onChange: (String) -> Unit,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onChange,
        label = { Text(label) },
        singleLine = !multiline,
        minLines = if (multiline) 2 else 1,
        keyboardOptions = KeyboardOptions(keyboardType = keyboard),
        modifier = Modifier.fillMaxWidth(),
    )
}

@Composable
private fun Toggle(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(label, modifier = Modifier.weight(1f))
        Switch(checked = checked, onCheckedChange = onChange)
    }
}
