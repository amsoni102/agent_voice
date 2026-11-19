package com.agentvoice.app.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.Dashboard
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.People
import androidx.compose.material.icons.filled.Tune
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.agentvoice.app.data.ApiClient
import com.agentvoice.app.data.Session
import com.agentvoice.app.data.SessionStore
import com.agentvoice.app.ui.screens.CallDetailScreen
import com.agentvoice.app.ui.screens.CallsScreen
import com.agentvoice.app.ui.screens.ConnectScreen
import com.agentvoice.app.ui.screens.DashboardScreen
import com.agentvoice.app.ui.screens.LeadDetailScreen
import com.agentvoice.app.ui.screens.LeadsScreen
import com.agentvoice.app.ui.screens.MeetingsScreen
import com.agentvoice.app.ui.screens.SettingsScreen
import com.agentvoice.app.ui.screens.TalkScreen
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch

/** Everything a screen needs: the API, a way to show messages, and navigation callbacks. */
class AppContext(
    val api: ApiClient,
    val snackbar: SnackbarHostState,
    private val scope: CoroutineScope,
    val openLead: (Long) -> Unit,
    val openCall: (Long) -> Unit,
    val openTalk: (Long?) -> Unit,
    val back: () -> Unit,
    val signOut: () -> Unit,
) {
    fun toast(message: String) {
        scope.launch { snackbar.showSnackbar(message) }
    }

    /** Runs [block], reporting failures (and optional success text) in the snackbar. */
    fun action(success: String? = null, block: suspend () -> Unit) {
        scope.launch {
            try {
                block()
                if (success != null) snackbar.showSnackbar(success)
            } catch (e: Exception) {
                snackbar.showSnackbar(e.message ?: "Something went wrong")
            }
        }
    }
}

val LocalApp = staticCompositionLocalOf<AppContext> { error("AppContext not provided") }

private data class Tab(val route: String, val label: String, val icon: ImageVector)

private val tabs = listOf(
    Tab("talk", "Talk", Icons.Filled.Mic),
    Tab("dashboard", "Home", Icons.Filled.Dashboard),
    Tab("leads", "Leads", Icons.Filled.People),
    Tab("calls", "Calls", Icons.Filled.Call),
    Tab("settings", "Agent", Icons.Filled.Tune),
)

private object Loading

@Composable
fun AppRoot(store: SessionStore) {
    val state by store.session.collectAsState<Session?, Any?>(initial = Loading)
    when (val s = state) {
        Loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
        null -> ConnectScreen(store)
        is Session -> MainScaffold(s, store)
    }
}

@Composable
private fun MainScaffold(session: Session, store: SessionStore) {
    val nav = rememberNavController()
    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    val api = remember(session) { ApiClient(session.serverUrl, session.token) }
    var talkLeadId by remember { mutableStateOf<Long?>(null) }
    val app = remember(api) {
        AppContext(
            api = api,
            snackbar = snackbar,
            scope = scope,
            openLead = { nav.navigate("lead/$it") },
            openCall = { nav.navigate("call/$it") },
            openTalk = { id ->
                talkLeadId = id
                nav.navigate("talk") {
                    popUpTo(nav.graph.findStartDestination().id) { saveState = true }
                    launchSingleTop = true
                    restoreState = true
                }
            },
            back = { nav.popBackStack() },
            signOut = { scope.launch { store.signOut() } },
        )
    }
    val backStack by nav.currentBackStackEntryAsState()
    val route = backStack?.destination?.route

    CompositionLocalProvider(LocalApp provides app) {
        Scaffold(
            containerColor = MaterialTheme.colorScheme.background,
            snackbarHost = { SnackbarHost(snackbar) },
            bottomBar = {
                NavigationBar(containerColor = MaterialTheme.colorScheme.surface) {
                    tabs.forEach { tab ->
                        NavigationBarItem(
                            selected = route == tab.route,
                            onClick = {
                                nav.navigate(tab.route) {
                                    popUpTo(nav.graph.findStartDestination().id) { saveState = true }
                                    launchSingleTop = true
                                    restoreState = true
                                }
                            },
                            icon = { Icon(tab.icon, contentDescription = tab.label) },
                            label = { Text(tab.label) },
                        )
                    }
                }
            },
        ) { padding ->
            NavHost(nav, startDestination = "talk", modifier = Modifier.padding(padding)) {
                composable("talk") { TalkScreen(talkLeadId) }
                composable("dashboard") { DashboardScreen() }
                composable("leads") { LeadsScreen() }
                composable("calls") { CallsScreen() }
                composable("meetings") { MeetingsScreen() }
                composable("settings") { SettingsScreen() }
                composable("lead/{id}", arguments = listOf(navArgument("id") { type = NavType.LongType })) {
                    LeadDetailScreen(it.arguments!!.getLong("id"))
                }
                composable("call/{id}", arguments = listOf(navArgument("id") { type = NavType.LongType })) {
                    CallDetailScreen(it.arguments!!.getLong("id"))
                }
            }
        }
    }
}
