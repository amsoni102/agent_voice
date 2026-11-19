package com.agentvoice.app.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Block
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.agentvoice.app.data.LEAD_STATUSES
import com.agentvoice.app.ui.LocalApp
import com.agentvoice.app.ui.components.ChipRow
import com.agentvoice.app.ui.components.Picker
import com.agentvoice.app.ui.components.ResourceView
import com.agentvoice.app.ui.components.SectionCard
import com.agentvoice.app.ui.components.formatDuration
import com.agentvoice.app.ui.components.formatTime
import com.agentvoice.app.ui.components.rememberResource
import com.agentvoice.app.ui.theme.Warn
import org.json.JSONObject

@Composable
fun LeadDetailScreen(leadId: Long) {
    val app = LocalApp.current
    val res = rememberResource(leadId, pollMs = 6000) { app.api.lead(leadId) }
    var confirm by remember { mutableStateOf<String?>(null) }

    ResourceView(res) { lead ->
        LazyColumn(
            Modifier.fillMaxSize(),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    IconButton(onClick = app.back) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
                    Text(
                        lead.businessName.ifBlank { lead.phone },
                        style = MaterialTheme.typography.headlineSmall,
                        fontWeight = FontWeight.Bold,
                    )
                }
                ChipRow(lead.status, lead.score, if (lead.dnc) "do_not_call" else "")
                if (lead.statusReason.isNotBlank()) {
                    Text(lead.statusReason, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            item {
                SectionCard("Contact") {
                    Field("Phone", lead.phone)
                    Field("Contact", lead.contactName)
                    Field("Email", lead.email)
                    Field("Website", lead.website)
                    Field("Industry", lead.industry)
                    Field("Location", listOf(lead.city, lead.state).filter { it.isNotBlank() }.joinToString(", "))
                    Field("Local time zone", lead.localTimezone)
                    Field("Consent", if (lead.consent) "yes" else "not recorded")
                    Field("Attempts", lead.attempts.toString())
                    Field("Notes", lead.notes)
                }
            }
            item {
                if (lead.callableNow == false) {
                    Text("Not callable right now: ${lead.callableReason}", color = Warn, style = MaterialTheme.typography.bodySmall)
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(
                        enabled = lead.callableNow == true,
                        onClick = { app.action("Dialing…") { app.api.callLead(lead.id); res.reload() } },
                    ) { Icon(Icons.Filled.Call, null); Text(" Call now") }
                    OutlinedButton(onClick = { confirm = "dnc" }, enabled = !lead.dnc) {
                        Icon(Icons.Filled.Block, null); Text(" Do not call")
                    }
                    IconButton(onClick = { confirm = "delete" }) {
                        Icon(Icons.Filled.Delete, "Delete", tint = MaterialTheme.colorScheme.error)
                    }
                }
                Picker("Status", LEAD_STATUSES, lead.status, { it.replace('_', ' ') }, { status ->
                    app.action("Status updated") {
                        app.api.updateLead(lead.id, JSONObject().put("status", status))
                        res.reload()
                    }
                }, Modifier.padding(top = 8.dp))
            }
            if (lead.qualification.isNotEmpty()) {
                item {
                    SectionCard("What the agent learned") {
                        lead.qualification.forEach { (k, v) -> Field(k.replace('_', ' ').replaceFirstChar { it.uppercase() }, v) }
                    }
                }
            }
            if (lead.talkingPoints.isNotEmpty()) {
                item {
                    SectionCard("Website research") {
                        lead.talkingPoints.forEach { Text("• $it", style = MaterialTheme.typography.bodyMedium) }
                    }
                }
            }
            item {
                SectionCard("Calls (${lead.calls.size})") {
                    if (lead.calls.isEmpty()) Text("No calls yet.", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    lead.calls.forEach { call ->
                        Column(Modifier.fillMaxWidth().clickable { app.openCall(call.id) }.padding(vertical = 6.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(formatTime(call.createdAt), modifier = Modifier.weight(1f))
                                Text(formatDuration(call.durationS), color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                            ChipRow(call.status, call.outcome)
                            if (call.summary.isNotBlank()) {
                                Text(call.summary.take(220), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }
                    }
                }
            }
        }

        when (confirm) {
            "dnc" -> ConfirmDialog(
                "Add to do-not-call?",
                "This number will never be dialed again.",
                onDismiss = { confirm = null },
            ) {
                confirm = null
                app.action("Added to do-not-call") { app.api.addDnc(lead.phone, "manual (app)"); res.reload() }
            }
            "delete" -> ConfirmDialog("Delete lead?", "Call history is kept.", onDismiss = { confirm = null }) {
                confirm = null
                app.action("Lead deleted") { app.api.deleteLead(lead.id); app.back() }
            }
        }
    }
}

@Composable
fun Field(label: String, value: String) {
    if (value.isBlank()) return
    Row {
        Text(label, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.weight(0.4f))
        Text(value, modifier = Modifier.weight(0.6f))
    }
}

@Composable
fun ConfirmDialog(title: String, text: String, onDismiss: () -> Unit, onConfirm: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = { Text(text) },
        confirmButton = { TextButton(onClick = onConfirm) { Text("Confirm") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}
