package com.agentvoice.app.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Logout
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.CallEnd
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
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
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.agentvoice.app.data.CallItem
import com.agentvoice.app.data.Campaign
import com.agentvoice.app.data.ServerStatus
import com.agentvoice.app.ui.LocalApp
import com.agentvoice.app.ui.components.ChipRow
import com.agentvoice.app.ui.components.EmptyState
import com.agentvoice.app.ui.components.ResourceView
import com.agentvoice.app.ui.components.SectionCard
import com.agentvoice.app.ui.components.StatTile
import com.agentvoice.app.ui.components.formatTime
import com.agentvoice.app.ui.components.rememberResource
import com.agentvoice.app.ui.theme.Warn

private data class DashboardData(
    val status: ServerStatus,
    val active: List<CallItem>,
    val campaigns: List<Campaign>,
)

@Composable
fun DashboardScreen() {
    val app = LocalApp.current
    val res = rememberResource(pollMs = 4000) {
        DashboardData(app.api.status(), app.api.calls(activeOnly = true, limit = 50), app.api.campaigns())
    }
    var editing by remember { mutableStateOf<Campaign?>(null) }
    var creating by remember { mutableStateOf(false) }

    ResourceView(res) { data ->
        LazyColumn(
            Modifier.fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Overview", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
                    Spacer(Modifier.weight(1f))
                    IconButton(onClick = res.reload) { Icon(Icons.Filled.Refresh, "Refresh") }
                    IconButton(onClick = app.signOut) { Icon(Icons.AutoMirrored.Filled.Logout, "Sign out") }
                }
                Text(
                    "Server v${data.status.version} · ${app.api.baseUrl}",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            val warnings = buildList {
                if (!data.status.voiceAvailable) add("No voice backend: start Ollama or set OPENAI_API_KEY.")
                else if (data.status.voiceBackend == "ollama") add("Voice is local Ollama (${data.status.ollamaModel.ifBlank { "llama3.2" }}). Use the Talk tab.")
                if (!data.status.twilioConfigured) add("Phone carrier is not configured yet. Talk on this phone still works.")
            }
            items(warnings) { SectionCard { Text(it, color = Warn) } }

            item {
                val t = data.status.today
                Text("Today", style = MaterialTheme.typography.titleMedium)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    StatTile("${t.dials}", "Dials", Modifier.weight(1f))
                    StatTile("${t.conversations}", "Talks", Modifier.weight(1f))
                    StatTile("${t.meetingsBooked}", "Meetings", Modifier.weight(1f))
                }
            }
            item {
                val w = data.status.week
                Text("Last 7 days", style = MaterialTheme.typography.titleMedium)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    StatTile("${w.dials}", "Dials", Modifier.weight(1f))
                    StatTile("${w.meetingsBooked}", "Meetings", Modifier.weight(1f))
                    val rate = if (w.conversations > 0) "${100 * w.meetingsBooked / w.conversations}%" else "–"
                    StatTile(rate, "Talk→meet", Modifier.weight(1f))
                }
            }

            item {
                SectionCard("Live calls (${data.active.size})") {
                    if (data.active.isEmpty()) {
                        Text("No calls in progress.", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    data.active.forEach { call ->
                        Row(
                            Modifier.fillMaxWidth().clickable { app.openCall(call.id) }.padding(vertical = 6.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text(call.businessName.ifBlank { call.phone }, fontWeight = FontWeight.Medium)
                                ChipRow(call.status, call.kind)
                            }
                            IconButton(onClick = { app.action("Hanging up") { app.api.hangup(call.id) } }) {
                                Icon(Icons.Filled.CallEnd, "Hang up", tint = MaterialTheme.colorScheme.error)
                            }
                        }
                    }
                }
            }

            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Campaigns", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                    TextButton(onClick = { creating = true }) {
                        Icon(Icons.Filled.Add, null)
                        Text("New")
                    }
                }
            }
            if (data.campaigns.isEmpty()) item { EmptyState("No campaigns yet. Create one, then import leads.") }
            items(data.campaigns, key = { it.id }) { c ->
                SectionCard {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f).clickable { editing = c }) {
                            Text(c.name, fontWeight = FontWeight.SemiBold)
                            Text(
                                "${c.leadCount} leads · ${c.remaining} left · ${c.meetings} meetings",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            ChipRow(c.status)
                        }
                        if (c.status == "running") {
                            OutlinedButton(onClick = {
                                app.action("Campaign paused") { app.api.pauseCampaign(c.id); res.reload() }
                            }) { Icon(Icons.Filled.Pause, null); Text("Pause") }
                        } else {
                            Button(onClick = {
                                app.action("Campaign started") { app.api.startCampaign(c.id); res.reload() }
                            }) { Icon(Icons.Filled.PlayArrow, null); Text("Start") }
                        }
                    }
                }
            }
            item {
                OutlinedButton(
                    onClick = { app.action("Calling your test phone…") { app.api.testCall() } },
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("Test call to my phone") }
                Text(
                    "Last updated ${formatTime(java.time.Instant.now().toString())}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }
        }
    }

    if (creating || editing != null) {
        CampaignDialog(editing, onDismiss = { creating = false; editing = null }, onSaved = {
            creating = false
            editing = null
            res.reload()
        })
    }
}

@Composable
private fun CampaignDialog(campaign: Campaign?, onDismiss: () -> Unit, onSaved: () -> Unit) {
    val app = LocalApp.current
    var name by remember { mutableStateOf(campaign?.name ?: "") }
    var extra by remember { mutableStateOf(campaign?.extraInstructions ?: "") }
    var concurrent by remember { mutableStateOf((campaign?.maxConcurrent ?: 1).toString()) }
    var attempts by remember { mutableStateOf((campaign?.maxAttempts ?: 3).toString()) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(if (campaign == null) "New campaign" else "Edit campaign") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(name, { name = it }, label = { Text("Name") }, singleLine = true)
                OutlinedTextField(
                    extra, { extra = it },
                    label = { Text("Campaign focus for the agent") },
                    placeholder = { Text("e.g. These are dentists; lead with online booking.") },
                    minLines = 3,
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(
                        concurrent, { concurrent = it.filter(Char::isDigit) },
                        label = { Text("Parallel calls") }, singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                        modifier = Modifier.weight(1f),
                    )
                    OutlinedTextField(
                        attempts, { attempts = it.filter(Char::isDigit) },
                        label = { Text("Attempts") }, singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                        modifier = Modifier.weight(1f),
                    )
                }
            }
        },
        confirmButton = {
            TextButton(enabled = name.isNotBlank(), onClick = {
                app.action("Saved") {
                    val c = concurrent.toIntOrNull() ?: 1
                    val a = attempts.toIntOrNull() ?: 3
                    if (campaign == null) app.api.createCampaign(name.trim(), extra, c, a)
                    else app.api.updateCampaign(campaign.id, name.trim(), extra, c, a)
                    onSaved()
                }
            }) { Text("Save") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}
