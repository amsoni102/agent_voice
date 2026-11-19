package com.agentvoice.app.ui.screens

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.CallEnd
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.scale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.agentvoice.app.data.Lead
import com.agentvoice.app.data.PracticeSession
import com.agentvoice.app.ui.LocalApp
import com.agentvoice.app.ui.components.Picker
import com.agentvoice.app.ui.components.rememberResource
import com.agentvoice.app.ui.theme.Bad
import com.agentvoice.app.ui.theme.Info

data class TalkLine(val role: String, val text: String)

@Composable
fun TalkScreen(initialLeadId: Long?) {
    val app = LocalApp.current
    val context = LocalContext.current
    val leadsRes = rememberResource { app.api.leads(null, null, null, limit = 200) }
    val leads = leadsRes.data?.items.orEmpty()

    var leadId by remember(initialLeadId) { mutableStateOf(initialLeadId) }
    var live by remember { mutableStateOf(false) }
    var talking by remember { mutableStateOf(false) }
    var status by remember { mutableStateOf("Tap Start to talk to the agent. This is the same voice that will later go over Zadarma.") }
    val lines = remember { mutableStateListOf<TalkLine>() }
    var session by remember { mutableStateOf<PracticeSession?>(null) }
    val listState = rememberLazyListState()
    val pulse by animateFloatAsState(if (talking) 1.12f else 1f, label = "orb")

    DisposableEffect(Unit) {
        onDispose { session?.stop() }
    }
    LaunchedEffect(lines.size) {
        if (lines.isNotEmpty()) listState.animateScrollToItem(lines.lastIndex)
    }

    fun endCall() {
        session?.stop()
        session = null
        live = false
        talking = false
    }

    fun begin() {
        lines.clear()
        status = "Connecting…"
        live = true
        val next = PracticeSession(
            context,
            app.api.practiceWsUrl(leadId),
            object : PracticeSession.Listener {
                override fun onReady(callId: Long?) {
                    status = "Live. Say “Hello?” — you are the business owner."
                }
                override fun onTranscript(role: String, text: String) {
                    if (text.isNotBlank()) lines += TalkLine(role, text)
                }
                override fun onTool(name: String, result: String) {
                    lines += TalkLine("tool", "$name → $result")
                }
                override fun onStatus(text: String) {
                    status = text
                }
                override fun onTalking(isTalking: Boolean) {
                    talking = isTalking
                }
                override fun onEnded(reason: String) {
                    live = false
                    talking = false
                    session = null
                    status = reason
                }
                override fun onError(message: String) {
                    app.toast(message)
                    status = message
                }
            },
        )
        session = next
        next.start()
    }

    val mic = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) begin() else app.toast("Microphone permission is required to talk to the agent")
    }

    Column(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("Talk to the agent", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
        Text(
            "No phone carrier yet. Your mic goes to the Linux server, the agent talks back on speaker. " +
                "When Zadarma is linked, this same session will ride on the real call.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Picker(
            "Pretend to be",
            listOf<Lead?>(null) + leads,
            leads.firstOrNull { it.id == leadId },
            { it?.let { l -> l.businessName.ifBlank { l.phone } } ?: "A generic small business" },
            { if (!live) leadId = it?.id },
            Modifier.fillMaxWidth(),
        )
        Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
            Box(
                Modifier
                    .size(120.dp)
                    .scale(pulse)
                    .background(if (talking) Info else MaterialTheme.colorScheme.primaryContainer, CircleShape),
            )
        }
        Text(status, textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth(), color = MaterialTheme.colorScheme.onSurfaceVariant)
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
            if (live) {
                Button(
                    onClick = { endCall() },
                    colors = ButtonDefaults.buttonColors(containerColor = Bad),
                ) {
                    Icon(Icons.Filled.CallEnd, null)
                    Text(" End")
                }
            } else {
                Button(onClick = {
                    val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
                        PackageManager.PERMISSION_GRANTED
                    if (granted) begin() else mic.launch(Manifest.permission.RECORD_AUDIO)
                }) {
                    Icon(Icons.Filled.Call, null)
                    Text(" Start")
                }
            }
        }
        Text("Transcript", style = MaterialTheme.typography.titleMedium)
        LazyColumn(
            state = listState,
            modifier = Modifier.fillMaxWidth().weight(1f),
            contentPadding = PaddingValues(bottom = 8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            if (lines.isEmpty()) {
                item {
                    Text(
                        "Nothing yet. After Start, answer as if your phone just rang.",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            items(lines) { line -> TranscriptRow(line) }
        }
    }
}

@Composable
private fun TranscriptRow(line: TalkLine) {
    if (line.role == "tool") {
        Text(line.text, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        return
    }
    val agent = line.role == "agent"
    Row(Modifier.fillMaxWidth(), horizontalArrangement = if (agent) Arrangement.Start else Arrangement.End) {
        Column(
            Modifier
                .fillMaxWidth(0.85f)
                .background(
                    if (agent) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceVariant,
                    RoundedCornerShape(12.dp),
                )
                .padding(10.dp),
        ) {
            Text(if (agent) "Agent" else "You", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(2.dp))
            Text(line.text)
        }
    }
}
