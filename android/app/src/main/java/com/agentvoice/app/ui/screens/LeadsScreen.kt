package com.agentvoice.app.ui.screens

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.UploadFile
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.agentvoice.app.data.Campaign
import com.agentvoice.app.data.LEAD_STATUSES
import com.agentvoice.app.data.Lead
import com.agentvoice.app.ui.LocalApp
import com.agentvoice.app.ui.components.ChipRow
import com.agentvoice.app.ui.components.EmptyState
import com.agentvoice.app.ui.components.Picker
import com.agentvoice.app.ui.components.ResourceView
import com.agentvoice.app.ui.components.rememberResource
import com.agentvoice.app.ui.theme.Warn
import kotlinx.coroutines.delay
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject

@Composable
fun LeadsScreen() {
    val app = LocalApp.current
    var campaignId by rememberSaveable { mutableStateOf<Long?>(null) }
    var status by rememberSaveable { mutableStateOf("") }
    var query by rememberSaveable { mutableStateOf("") }
    var debounced by rememberSaveable { mutableStateOf("") }
    var showAdd by remember { mutableStateOf(false) }
    var importUri by remember { mutableStateOf<Uri?>(null) }

    androidx.compose.runtime.LaunchedEffect(query) {
        delay(350)
        debounced = query
    }
    val campaigns = rememberResource { app.api.campaigns() }
    val leads = rememberResource(campaignId, status, debounced) {
        app.api.leads(campaignId, status.ifBlank { null }, debounced.ifBlank { null }, limit = 200)
    }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) importUri = uri
    }

    Column(Modifier.fillMaxSize()) {
        Row(Modifier.padding(start = 16.dp, end = 8.dp, top = 16.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("Leads", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Spacer(Modifier.weight(1f))
            IconButton(onClick = { picker.launch(arrayOf("text/*", "text/csv", "application/vnd.ms-excel")) }) {
                Icon(Icons.Filled.UploadFile, "Import CSV")
            }
            IconButton(onClick = { showAdd = true }) { Icon(Icons.Filled.Add, "Add lead") }
        }
        OutlinedTextField(
            value = query,
            onValueChange = { query = it },
            leadingIcon = { Icon(Icons.Filled.Search, null) },
            placeholder = { Text("Search name, phone, city…") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
        )
        val campaignList = campaigns.data.orEmpty()
        Row(
            Modifier.horizontalScroll(rememberScrollState()).padding(horizontal = 16.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Picker(
                "Campaign",
                listOf<Campaign?>(null) + campaignList,
                campaignList.firstOrNull { it.id == campaignId },
                { it?.name ?: "All" },
                { campaignId = it?.id },
            )
            Picker("Status", listOf("") + LEAD_STATUSES, status, { it.ifBlank { "Any" }.replace('_', ' ') }, { status = it })
        }
        ResourceView(leads) { page ->
            LazyColumn(contentPadding = PaddingValues(vertical = 8.dp)) {
                item {
                    Text(
                        "${page.total} lead(s)",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(horizontal = 16.dp),
                    )
                }
                if (page.items.isEmpty()) item { EmptyState("No leads match. Import a CSV or add one.") }
                items(page.items, key = { it.id }) { lead ->
                    LeadRow(lead) { app.openLead(lead.id) }
                    HorizontalDivider(color = MaterialTheme.colorScheme.outline)
                }
            }
        }
    }

    if (showAdd) {
        AddLeadDialog(campaignList = campaigns.data.orEmpty(), defaultCampaign = campaignId, onDismiss = { showAdd = false }) {
            showAdd = false
            leads.reload()
        }
    }
    importUri?.let { uri ->
        ImportDialog(uri, campaigns.data.orEmpty(), campaignId, onDismiss = { importUri = null }) {
            importUri = null
            leads.reload()
        }
    }
}

@Composable
private fun LeadRow(lead: Lead, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onClick).padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(lead.businessName.ifBlank { lead.phone }, fontWeight = FontWeight.Medium)
            Text(
                listOf(lead.contactName, lead.phone, listOf(lead.city, lead.state).filter { it.isNotBlank() }.joinToString(", "))
                    .filter { it.isNotBlank() }.joinToString(" · "),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Column(horizontalAlignment = Alignment.End) {
            ChipRow(lead.status, lead.score)
            if (!lead.consent) Text("no consent", color = Warn, style = MaterialTheme.typography.labelSmall)
        }
    }
}

@Composable
private fun AddLeadDialog(campaignList: List<Campaign>, defaultCampaign: Long?, onDismiss: () -> Unit, onSaved: () -> Unit) {
    val app = LocalApp.current
    var phone by remember { mutableStateOf("") }
    var business by remember { mutableStateOf("") }
    var contact by remember { mutableStateOf("") }
    var website by remember { mutableStateOf("") }
    var state by remember { mutableStateOf("") }
    var campaign by remember { mutableStateOf(campaignList.firstOrNull { it.id == defaultCampaign }) }
    var consent by remember { mutableStateOf(false) }
    var consentSource by remember { mutableStateOf("") }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Add lead") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                OutlinedTextField(phone, { phone = it }, label = { Text("Phone *") }, singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone))
                OutlinedTextField(business, { business = it }, label = { Text("Business name") }, singleLine = true)
                OutlinedTextField(contact, { contact = it }, label = { Text("Contact name") }, singleLine = true)
                OutlinedTextField(website, { website = it }, label = { Text("Website") }, singleLine = true)
                OutlinedTextField(state, { state = it }, label = { Text("State (e.g. TX)") }, singleLine = true)
                Picker("Campaign", listOf<Campaign?>(null) + campaignList, campaign, { it?.name ?: "None" }, { campaign = it })
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(consent, { consent = it })
                    Text("Has prior express written consent", style = MaterialTheme.typography.bodySmall)
                }
                if (consent) OutlinedTextField(consentSource, { consentSource = it }, label = { Text("Consent source") }, singleLine = true)
            }
        },
        confirmButton = {
            TextButton(enabled = phone.isNotBlank(), onClick = {
                app.action("Lead added") {
                    val body = JSONObject()
                        .put("phone", phone)
                        .put("business_name", business)
                        .put("contact_name", contact)
                        .put("website", website)
                        .put("state", state)
                        .put("consent", consent)
                        .put("consent_source", consentSource)
                    campaign?.let { body.put("campaign_id", it.id) }
                    app.api.createLead(body)
                    onSaved()
                }
            }) { Text("Save") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun ImportDialog(uri: Uri, campaignList: List<Campaign>, defaultCampaign: Long?, onDismiss: () -> Unit, onDone: () -> Unit) {
    val app = LocalApp.current
    val context = LocalContext.current
    var campaign by remember { mutableStateOf(campaignList.firstOrNull { it.id == defaultCampaign }) }
    var consent by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Import leads") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(uri.lastPathSegment ?: "CSV file", style = MaterialTheme.typography.bodySmall)
                Text(
                    "Columns are detected automatically: phone (required), business, contact, email, website, " +
                        "industry, city, state, consent, notes.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Picker("Campaign", listOf<Campaign?>(null) + campaignList, campaign, { it?.name ?: "None" }, { campaign = it })
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(consent, { consent = it })
                    Text(
                        "Rows without a consent column have documented prior express written consent",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        },
        confirmButton = {
            TextButton(enabled = !busy, onClick = {
                busy = true
                app.action {
                    try {
                        val text = withContext(Dispatchers.IO) {
                            context.contentResolver.openInputStream(uri)?.bufferedReader()?.use { it.readText() }
                        } ?: error("Couldn't read the file")
                        val result = app.api.importLeads(text, campaign?.id, consent)
                        app.toast(
                            "Imported ${result.optInt("imported")}, updated ${result.optInt("updated")}, " +
                                "skipped ${result.optInt("skipped_count")}"
                        )
                        onDone()
                    } finally {
                        busy = false
                    }
                }
            }) { Text(if (busy) "Importing…" else "Import") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}
