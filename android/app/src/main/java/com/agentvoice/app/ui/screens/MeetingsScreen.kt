package com.agentvoice.app.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.agentvoice.app.ui.LocalApp
import com.agentvoice.app.ui.components.ChipRow
import com.agentvoice.app.ui.components.EmptyState
import com.agentvoice.app.ui.components.Picker
import com.agentvoice.app.ui.components.ResourceView
import com.agentvoice.app.ui.components.SectionCard
import com.agentvoice.app.ui.components.formatTime
import com.agentvoice.app.ui.components.rememberResource
import java.time.ZoneId

private val MEETING_STATUSES = listOf("booked", "completed", "no_show", "cancelled")

@Composable
fun MeetingsScreen() {
    val app = LocalApp.current
    var upcoming by rememberSaveable { mutableStateOf(true) }
    val res = rememberResource(upcoming, pollMs = 15000) { app.api.meetings(upcoming) }

    Column(Modifier.fillMaxSize()) {
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("Meetings", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Spacer(Modifier.weight(1f))
            FilterChip(selected = upcoming, onClick = { upcoming = !upcoming }, label = { Text("Upcoming only") })
        }
        ResourceView(res) { meetings ->
            LazyColumn(
                contentPadding = PaddingValues(start = 16.dp, end = 16.dp, bottom = 16.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                if (meetings.isEmpty()) item { EmptyState("Meetings the agent books show up here.") }
                items(meetings, key = { it.id }) { m ->
                    SectionCard {
                        Text(m.businessName.ifBlank { m.contactName }, fontWeight = FontWeight.SemiBold)
                        Text(formatTime(m.startsAt) + " (your time)")
                        val prospectZone = runCatching { ZoneId.of(m.timezone) }.getOrNull()
                        if (prospectZone != null && prospectZone != ZoneId.systemDefault()) {
                            Text(
                                formatTime(m.startsAt, prospectZone) + " their time (${m.timezone})",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        Field("Contact", m.contactName)
                        Field("Email", m.email)
                        Field("Phone", m.phone)
                        Field("Website", m.website)
                        Field("Notes", m.notes)
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            ChipRow(m.status)
                            Spacer(Modifier.weight(1f))
                            Picker("Set", MEETING_STATUSES, m.status, { it.replace('_', ' ') }, { status ->
                                app.action("Updated") { app.api.setMeetingStatus(m.id, status); res.reload() }
                            })
                        }
                    }
                }
            }
        }
    }
}
