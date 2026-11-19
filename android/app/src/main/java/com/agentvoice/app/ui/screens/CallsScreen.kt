package com.agentvoice.app.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.CallEnd
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.agentvoice.app.data.TranscriptLine
import com.agentvoice.app.ui.LocalApp
import com.agentvoice.app.ui.components.ChipRow
import com.agentvoice.app.ui.components.EmptyState
import com.agentvoice.app.ui.components.ResourceView
import com.agentvoice.app.ui.components.SectionCard
import com.agentvoice.app.ui.components.formatDuration
import com.agentvoice.app.ui.components.formatTime
import com.agentvoice.app.ui.components.rememberResource
import com.agentvoice.app.ui.theme.Bad

@Composable
fun CallsScreen() {
    val app = LocalApp.current
    val res = rememberResource(pollMs = 5000) { app.api.calls(limit = 200) }
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.padding(start = 16.dp, end = 8.dp, top = 16.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("Calls", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Spacer(Modifier.weight(1f))
            IconButton(onClick = res.reload) { Icon(Icons.Filled.Refresh, "Refresh") }
        }
        ResourceView(res) { calls ->
            LazyColumn(contentPadding = PaddingValues(vertical = 8.dp)) {
                if (calls.isEmpty()) item { EmptyState("No calls yet.") }
                items(calls, key = { it.id }) { call ->
                    Row(
                        Modifier.fillMaxWidth().clickable { app.openCall(call.id) }.padding(horizontal = 16.dp, vertical = 10.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Column(Modifier.weight(1f)) {
                            Text(call.businessName.ifBlank { call.phone.ifBlank { call.kind } }, fontWeight = FontWeight.Medium)
                            Text(
                                "${formatTime(call.createdAt)} · ${call.kind} ${formatDuration(call.durationS)}",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        Column(horizontalAlignment = Alignment.End) {
                            ChipRow(call.status)
                            ChipRow(call.outcome, call.leadScore)
                        }
                    }
                    HorizontalDivider(color = MaterialTheme.colorScheme.outline)
                }
            }
        }
    }
}

@Composable
fun CallDetailScreen(callId: Long) {
    val app = LocalApp.current
    val res = rememberResource(callId, pollMs = 2500) { app.api.call(callId) }
    val listState = rememberLazyListState()

    ResourceView(res) { detail ->
        val call = detail.call
        LaunchedEffect(detail.transcript.size, detail.live) {
            if (detail.live && detail.transcript.isNotEmpty()) listState.animateScrollToItem(detail.transcript.size + 2)
        }
        LazyColumn(
            Modifier.fillMaxSize(),
            state = listState,
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    IconButton(onClick = app.back) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
                    Text(
                        "Call #${call.id}" + if (detail.live) "  ● LIVE" else "",
                        style = MaterialTheme.typography.headlineSmall,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.weight(1f),
                    )
                }
                Text(
                    "${call.businessName.ifBlank { call.kind }} · ${call.phone} · ${formatTime(call.createdAt)} " +
                        formatDuration(call.durationS),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                ChipRow(call.status, call.outcome, call.leadScore)
                if (call.error.isNotBlank()) Text(call.error, color = Bad, style = MaterialTheme.typography.bodySmall)
                if (detail.live) {
                    Button(
                        onClick = { app.action("Hanging up") { app.api.hangup(call.id) } },
                        colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error),
                        modifier = Modifier.padding(top = 8.dp),
                    ) { Icon(Icons.Filled.CallEnd, null); Text(" Hang up") }
                }
            }
            if (call.summary.isNotBlank()) {
                item {
                    SectionCard("Summary") {
                        Text(call.summary)
                        if (call.nextStep.isNotBlank()) Text("Next step: ${call.nextStep}", fontWeight = FontWeight.Medium)
                    }
                }
            }
            item { Text("Transcript", style = MaterialTheme.typography.titleMedium) }
            if (detail.transcript.isEmpty()) item { EmptyState("No transcript yet.") }
            items(detail.transcript) { line -> TranscriptBubble(line) }
        }
    }
}

@Composable
private fun TranscriptBubble(line: TranscriptLine) {
    val isAgent = line.role == "agent"
    val isTool = line.role == "tool"
    Row(Modifier.fillMaxWidth(), horizontalArrangement = if (isAgent || isTool) Arrangement.Start else Arrangement.End) {
        if (isTool) {
            Text(
                line.text,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else {
            Column(
                Modifier
                    .fillMaxWidth(0.85f)
                    .background(
                        if (isAgent) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceVariant,
                        RoundedCornerShape(12.dp),
                    )
                    .padding(10.dp),
            ) {
                Text(
                    if (isAgent) "Agent" else "Prospect",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Text(line.text)
            }
        }
    }
}
