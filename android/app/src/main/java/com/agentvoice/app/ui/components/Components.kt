package com.agentvoice.app.ui.components

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.agentvoice.app.ui.theme.Bad
import com.agentvoice.app.ui.theme.Good
import com.agentvoice.app.ui.theme.Info
import com.agentvoice.app.ui.theme.Warn
import kotlinx.coroutines.delay
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/** Loads data with [loader], optionally re-polling every [pollMs]. Call [reload] to refresh manually. */
class Resource<T>(val data: T?, val error: String?, val loading: Boolean, val reload: () -> Unit)

@Composable
fun <T> rememberResource(vararg keys: Any?, pollMs: Long? = null, loader: suspend () -> T): Resource<T> {
    var data by remember(*keys) { mutableStateOf<T?>(null) }
    var error by remember(*keys) { mutableStateOf<String?>(null) }
    var loading by remember(*keys) { mutableStateOf(true) }
    var version by remember(*keys) { mutableIntStateOf(0) }
    LaunchedEffect(*keys, version) {
        while (true) {
            loading = true
            try {
                data = loader()
                error = null
            } catch (e: Exception) {
                error = e.message ?: "Something went wrong"
            }
            loading = false
            if (pollMs == null) break
            delay(pollMs)
        }
    }
    return Resource(data, error, loading) { version++ }
}

@Composable
fun <T> ResourceView(resource: Resource<T>, content: @Composable (T) -> Unit) {
    val data = resource.data
    when {
        data != null -> content(data)
        resource.error != null -> ErrorState(resource.error, resource.reload)
        else -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
    }
}

@Composable
fun ErrorState(message: String, onRetry: () -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(32.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(message, color = Bad, textAlign = TextAlign.Center)
        TextButton(onClick = onRetry) { Text("Retry") }
    }
}

@Composable
fun EmptyState(text: String) {
    Text(
        text,
        modifier = Modifier.fillMaxWidth().padding(24.dp),
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        textAlign = TextAlign.Center,
    )
}

@Composable
fun SectionCard(title: String? = null, modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
    ) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (title != null) Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            content()
        }
    }
}

@Composable
fun StatTile(value: String, label: String, modifier: Modifier = Modifier) {
    SectionCard(modifier = modifier) {
        Text(value, fontSize = 24.sp, fontWeight = FontWeight.Bold)
        Text(label.uppercase(), fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

fun statusColor(status: String): Color = when (status) {
    "meeting_booked", "interested", "hot", "booked", "completed_meeting" -> Good
    "callback", "retry", "warm", "callback_scheduled", "voicemail", "no_answer", "busy" -> Warn
    "not_interested", "do_not_call", "blocked", "failed", "wrong_number", "cold", "exhausted", "cancelled", "no_show" -> Bad
    "" -> Color.Transparent
    else -> Info
}

@Composable
fun StatusChip(status: String) {
    if (status.isBlank()) return
    val color = statusColor(status)
    Surface(
        shape = RoundedCornerShape(50),
        color = Color.Transparent,
        border = BorderStroke(1.dp, color),
    ) {
        Text(
            status.replace('_', ' '),
            color = color,
            fontSize = 12.sp,
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
        )
    }
}

@Composable
fun ChipRow(vararg statuses: String) {
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
        statuses.filter { it.isNotBlank() }.forEach { StatusChip(it) }
    }
}

@Composable
fun <T> Picker(
    label: String,
    options: List<T>,
    selected: T,
    display: (T) -> String,
    onSelect: (T) -> Unit,
    modifier: Modifier = Modifier,
) {
    var open by remember { mutableStateOf(false) }
    Box(modifier) {
        OutlinedButton(onClick = { open = true }) { Text("$label: ${display(selected)}", maxLines = 1) }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            options.forEach { option ->
                DropdownMenuItem(text = { Text(display(option)) }, onClick = {
                    open = false
                    onSelect(option)
                })
            }
        }
    }
}

private val timeFormat = DateTimeFormatter.ofPattern("MMM d, h:mm a", Locale.US)

fun formatTime(iso: String, zone: ZoneId = ZoneId.systemDefault()): String =
    try {
        timeFormat.format(Instant.parse(iso.replace("+00:00", "Z")).atZone(zone))
    } catch (_: Exception) {
        iso
    }

fun formatDuration(seconds: Int): String =
    if (seconds <= 0) "" else "%d:%02d".format(seconds / 60, seconds % 60)
