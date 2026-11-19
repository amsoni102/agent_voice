package com.agentvoice.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import com.agentvoice.app.data.SessionStore
import com.agentvoice.app.ui.AppRoot
import com.agentvoice.app.ui.theme.AgentVoiceTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val store = SessionStore(applicationContext)
        setContent {
            AgentVoiceTheme {
                AppRoot(store)
            }
        }
    }
}
