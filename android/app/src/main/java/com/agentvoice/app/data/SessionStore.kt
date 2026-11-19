package com.agentvoice.app.data

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "agentvoice")

data class Session(val serverUrl: String, val token: String)

class SessionStore(private val context: Context) {
    private val urlKey = stringPreferencesKey("server_url")
    private val tokenKey = stringPreferencesKey("api_token")

    /** Emits null until the user has connected to a server. */
    val session: Flow<Session?> = context.dataStore.data.map { prefs ->
        val url = prefs[urlKey].orEmpty()
        val token = prefs[tokenKey].orEmpty()
        if (url.isNotEmpty() && token.isNotEmpty()) Session(url, token) else null
    }

    val lastUrl: Flow<String> = context.dataStore.data.map { it[urlKey].orEmpty() }

    suspend fun save(session: Session) {
        context.dataStore.edit {
            it[urlKey] = session.serverUrl
            it[tokenKey] = session.token
        }
    }

    suspend fun signOut() {
        context.dataStore.edit { it.remove(tokenKey) }
    }
}
