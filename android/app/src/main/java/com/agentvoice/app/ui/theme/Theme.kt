package com.agentvoice.app.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

val Good = Color(0xFF34C38F)
val Warn = Color(0xFFF1B44C)
val Bad = Color(0xFFF46A6A)
val Info = Color(0xFF4F8CFF)

private val Colors = darkColorScheme(
    primary = Color(0xFF4F8CFF),
    onPrimary = Color.White,
    primaryContainer = Color(0xFF1F3156),
    onPrimaryContainer = Color(0xFFDCE6FF),
    secondary = Color(0xFF7AA7FF),
    background = Color(0xFF0F1218),
    onBackground = Color(0xFFE7EAF0),
    surface = Color(0xFF171B23),
    onSurface = Color(0xFFE7EAF0),
    surfaceVariant = Color(0xFF1E2430),
    onSurfaceVariant = Color(0xFF8B93A7),
    surfaceContainer = Color(0xFF171B23),
    surfaceContainerHigh = Color(0xFF1E2430),
    outline = Color(0xFF2A3140),
    error = Bad,
)

@Composable
fun AgentVoiceTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = Colors, typography = Typography(), content = content)
}
