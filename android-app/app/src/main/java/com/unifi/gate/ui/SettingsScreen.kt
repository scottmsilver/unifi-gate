package com.unifi.gate.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.unifi.gate.model.*
import com.unifi.gate.ui.components.CenteredLoadingIndicator
import com.unifi.gate.ui.components.ConnectionIndicator
import com.unifi.gate.ui.components.StatusColors
import com.unifi.gate.ui.components.UiConstants
import com.unifi.gate.util.toRelativeTimeDisplay

@Composable
fun SettingsScreen(
    currentServerUrl: String,
    onSave: (String) -> Unit,
    onBack: () -> Unit,
    onSignOut: (() -> Unit)? = null,
    isAuthenticated: Boolean = false,
    userEmail: String? = null,
    isDevMode: Boolean = false
) {
    val settingsViewModel: SettingsViewModel = viewModel()
    val settingsState by settingsViewModel.uiState.collectAsState()

    var serverUrl by remember(currentServerUrl) { mutableStateOf(currentServerUrl) }
    var isEditing by remember { mutableStateOf(currentServerUrl.isBlank()) }
    var showAllEvents by remember { mutableStateOf(false) }

    // Load data via ViewModel
    LaunchedEffect(currentServerUrl) {
        settingsViewModel.loadData(currentServerUrl)
    }

    val config = settingsState.config
    val devices = settingsState.devices
    val events = settingsState.events

    Surface(
        modifier = Modifier.fillMaxSize(),
        color = MaterialTheme.colorScheme.background
    ) {
        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            // Header
            item {
                Spacer(modifier = Modifier.height(32.dp))
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = config?.siteName ?: "Settings",
                        style = MaterialTheme.typography.titleLarge,
                        fontWeight = FontWeight.Bold
                    )
                    ConnectionIndicator(isConnected = config?.connected == true)
                }
                Spacer(modifier = Modifier.height(16.dp))
            }

            // Account Card (only if authenticated)
            if (isAuthenticated || isDevMode) {
                item {
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        shape = UiConstants.CardShape
                    ) {
                        Column(modifier = Modifier.padding(16.dp)) {
                            Text(
                                text = "Account",
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.Bold
                            )
                            Spacer(modifier = Modifier.height(12.dp))

                            if (isDevMode) {
                                InfoRow("Mode", "Development (Local)",
                                    valueColor = MaterialTheme.colorScheme.tertiary)
                                InfoRow("User", "Guest")
                            } else {
                                InfoRow("Email", userEmail ?: "Unknown")
                            }

                            if (onSignOut != null && !isDevMode) {
                                Spacer(modifier = Modifier.height(12.dp))
                                OutlinedButton(
                                    onClick = onSignOut,
                                    modifier = Modifier.fillMaxWidth(),
                                    colors = ButtonDefaults.outlinedButtonColors(
                                        contentColor = MaterialTheme.colorScheme.error
                                    )
                                ) {
                                    Text("Sign Out")
                                }
                            }
                        }
                    }
                }
            }

            // Connection Status Card
            item {
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    shape = UiConstants.CardShape
                ) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Text(
                            text = "Connection Status",
                            style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.Bold
                        )
                        Spacer(modifier = Modifier.height(12.dp))

                        if (isEditing || currentServerUrl.isBlank()) {
                            OutlinedTextField(
                                value = serverUrl,
                                onValueChange = { serverUrl = it },
                                label = { Text("Server URL") },
                                placeholder = { Text("192.168.1.100:8000") },
                                modifier = Modifier.fillMaxWidth(),
                                singleLine = true
                            )
                            Spacer(modifier = Modifier.height(12.dp))
                            Button(
                                onClick = {
                                    onSave(serverUrl)
                                    isEditing = false
                                },
                                modifier = Modifier.fillMaxWidth(),
                                enabled = serverUrl.isNotBlank()
                            ) {
                                Text("Connect")
                            }
                        } else {
                            InfoRow("Status", if (config?.connected == true) "Connected" else "Disconnected",
                                valueColor = StatusColors.connectionColor(config?.connected == true))
                            InfoRow("Site Name", config?.siteName ?: "—")
                            InfoRow("Server", currentServerUrl, mono = true)
                            if (isDevMode) {
                                InfoRow("Auth Mode", "Dev (No Auth Required)",
                                    valueColor = StatusColors.connectionColor(true))
                            }

                            Spacer(modifier = Modifier.height(12.dp))
                            OutlinedButton(
                                onClick = { isEditing = true },
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Text("Edit / Reconnect")
                            }
                        }

                        Spacer(modifier = Modifier.height(8.dp))
                        Text(
                            text = "Back to Dashboard",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.primary,
                            modifier = Modifier
                                .align(Alignment.CenterHorizontally)
                                .clickable { onBack() }
                        )
                    }
                }
            }

            // Device Debug Card
            if (devices.isNotEmpty()) {
                item {
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        shape = UiConstants.CardShape
                    ) {
                        Column(modifier = Modifier.padding(16.dp)) {
                            Text(
                                text = "Device Debug",
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.Bold
                            )
                            Spacer(modifier = Modifier.height(12.dp))

                            devices.forEach { device ->
                                DeviceDebugPanel(
                                    device = device,
                                    debugData = settingsState.debugData[device.id],
                                    isLoadingDebug = settingsState.loadingDebug.contains(device.id),
                                    onLoadDebug = { settingsViewModel.loadDebugData(currentServerUrl, device.id) }
                                )
                                if (device != devices.last()) {
                                    Spacer(modifier = Modifier.height(8.dp))
                                }
                            }
                        }
                    }
                }
            }

            // Activity Log Card
            if (events.isNotEmpty()) {
                item {
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        shape = UiConstants.CardShape
                    ) {
                        Column(modifier = Modifier.padding(16.dp)) {
                            Text(
                                text = "Activity Log",
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.Bold
                            )
                            Spacer(modifier = Modifier.height(12.dp))

                            val displayEvents = if (showAllEvents) events else events.take(5)
                            displayEvents.forEach { event ->
                                EventRow(event)
                            }

                            if (events.size > 5) {
                                Spacer(modifier = Modifier.height(8.dp))
                                Text(
                                    text = if (showAllEvents) "Show less" else "Show ${events.size - 5} more",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.primary,
                                    modifier = Modifier
                                        .align(Alignment.CenterHorizontally)
                                        .clickable { showAllEvents = !showAllEvents }
                                )
                            }
                        }
                    }
                }
            }

            item {
                Spacer(modifier = Modifier.height(32.dp))
            }
        }
    }
}

@Composable
fun DeviceDebugPanel(
    device: Device,
    debugData: DebugData?,
    isLoadingDebug: Boolean,
    onLoadDebug: () -> Unit
) {
    var expanded by remember { mutableStateOf(false) }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(UiConstants.SmallCardShape)
            .background(MaterialTheme.colorScheme.surfaceVariant)
    ) {
        // Header
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable {
                    expanded = !expanded
                    if (expanded && debugData == null) {
                        onLoadDebug()
                    }
                }
                .padding(12.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                text = device.name,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Medium
            )
            Text(
                text = "${if (expanded) "▼" else "▶"} Debug",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }

        // Expanded Content
        AnimatedVisibility(visible = expanded) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(12.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                if (isLoadingDebug) {
                    CenteredLoadingIndicator()
                } else if (debugData != null) {
                    // WebSocket Status
                    DebugSection(
                        title = "REAL-TIME EVENTS",
                        badge = if (debugData.websocket?.connected == true) "Connected" else "Disconnected",
                        badgeActive = debugData.websocket?.connected == true
                    ) {
                        val events = debugData.websocket?.recentEvents ?: emptyList()
                        if (events.isEmpty()) {
                            Text(
                                text = "No recent events for this device",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        } else {
                            events.take(5).forEach { event ->
                                Text(
                                    text = "${event.event?.replace("access.data.", "")?.replace("access.", "")} - ${event.receivedAt?.takeLast(8) ?: ""}",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.primary
                                )
                            }
                        }
                    }

                    // Hardware Signals
                    debugData.unifi?.hardwareStatus?.let { hw ->
                        DebugSection(title = "HARDWARE SIGNALS (POLLED)") {
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.spacedBy(8.dp)
                            ) {
                                val isUnlocked = hw.doorLockRelayStatus == "unlock"
                                val isOpen = hw.doorPositionStatus == "open"
                                HardwareStatusColumn(
                                    modifier = Modifier.weight(1f),
                                    label = "LOCK RELAY",
                                    value = if (isUnlocked) "UNLOCKED" else "LOCKED",
                                    valueColor = StatusColors.connectionColor(isUnlocked)
                                )
                                HardwareStatusColumn(
                                    modifier = Modifier.weight(1f),
                                    label = "POSITION SENSOR",
                                    value = if (isOpen) "OPEN" else "CLOSED",
                                    valueColor = if (isOpen) StatusColors.connectionColor(true) else MaterialTheme.colorScheme.onSurfaceVariant
                                )
                            }
                        }
                    }

                    // Physical Device
                    debugData.unifi?.physicalDevice?.let { pd ->
                        DebugSection(
                            title = "PHYSICAL DEVICE",
                            badge = if (pd.isOnline) "Online" else "Offline",
                            badgeActive = pd.isOnline
                        ) {
                            InfoRow("Name", pd.name ?: "—")
                            InfoRow("Type", pd.deviceType ?: "—")
                            InfoRow("Model", pd.model ?: "—")
                            InfoRow("Firmware", pd.firmware ?: "—", mono = true)
                            InfoRow("IP", pd.ip ?: "—", mono = true)
                            InfoRow("MAC", pd.mac ?: "—", mono = true)
                        }
                    }

                    // Door Info
                    debugData.unifi?.door?.let { door ->
                        DebugSection(title = "DOOR") {
                            InfoRow("Name", door.name ?: "—")
                            InfoRow("Full Name", door.fullName ?: "—")
                            InfoRow("Floor", door.floor ?: "—")
                            InfoRow("ID", door.uniqueId ?: "—", mono = true)
                        }
                    }

                    // UniFi Schedule
                    debugData.unifi?.schedule?.scheduleInfo?.let { schedule ->
                        DebugSection(title = "UNIFI SCHEDULE") {
                            InfoRow("Name", schedule.name ?: "—", mono = true)
                            InfoRow("Type", schedule.type ?: "—")
                            schedule.userTimezone?.weekSchedule?.let { weekSchedule ->
                                Text(
                                    text = "Week Schedule:",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant
                                )
                                Spacer(modifier = Modifier.height(4.dp))
                                Row(
                                    modifier = Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.SpaceEvenly
                                ) {
                                    listOf("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat").forEachIndexed { index, day ->
                                        val hasSchedule = weekSchedule[index.toString()]?.isNotEmpty() == true
                                        Text(
                                            text = day,
                                            style = MaterialTheme.typography.labelSmall,
                                            color = if (hasSchedule) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant,
                                            fontWeight = if (hasSchedule) FontWeight.Bold else FontWeight.Normal
                                        )
                                    }
                                }
                            }
                        }
                    }

                    // Local Hold State
                    DebugSection(title = "LOCAL HOLD STATE") {
                        Text(
                            text = debugData.local?.holdState ?: "No active hold",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }

                    // Recent Journal
                    val journal = debugData.local?.journalEntries ?: emptyList()
                    if (journal.isNotEmpty()) {
                        DebugSection(title = "RECENT JOURNAL (${journal.size})") {
                            journal.take(4).forEach { entry ->
                                Row(
                                    modifier = Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.SpaceBetween
                                ) {
                                    Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                                        Text(
                                            text = entry.action,
                                            style = MaterialTheme.typography.labelSmall,
                                            color = if (entry.action == "create") MaterialTheme.colorScheme.tertiary else MaterialTheme.colorScheme.error,
                                            fontWeight = FontWeight.Medium
                                        )
                                        Text(
                                            text = "Day ${entry.day} ${entry.startTime.take(5)}-${entry.endTime.take(5)}",
                                            style = MaterialTheme.typography.labelSmall,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant
                                        )
                                    }
                                    Text(
                                        text = entry.timestamp.substring(11, 16),
                                        style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun DebugSection(
    title: String,
    badge: String? = null,
    badgeActive: Boolean = false,
    content: @Composable ColumnScope.() -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(UiConstants.SmallCardShape)
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .padding(12.dp)
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            Text(
                text = title,
                style = MaterialTheme.typography.labelSmall,
                fontWeight = FontWeight.Bold,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                letterSpacing = 0.5.sp
            )
            if (badge != null) {
                Text(
                    text = "● $badge",
                    style = MaterialTheme.typography.labelSmall,
                    color = if (badgeActive) StatusColors.connectionColor(true) else MaterialTheme.colorScheme.onSurfaceVariant,
                    fontSize = 10.sp
                )
            }
        }
        Spacer(modifier = Modifier.height(8.dp))
        content()
    }
}

@Composable
fun InfoRow(
    label: String,
    value: String,
    mono: Boolean = false,
    valueColor: Color = MaterialTheme.colorScheme.onSurface
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Text(
            text = value,
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.Medium,
            fontFamily = if (mono) FontFamily.Monospace else FontFamily.Default,
            color = valueColor
        )
    }
}

/**
 * A column showing a hardware status indicator (e.g., lock relay, position sensor)
 */
@Composable
fun HardwareStatusColumn(
    label: String,
    value: String,
    valueColor: Color,
    modifier: Modifier = Modifier
) {
    Column(
        modifier = modifier
            .clip(RoundedCornerShape(UiConstants.Spacing.Small - 2.dp))
            .background(MaterialTheme.colorScheme.surface)
            .padding(UiConstants.Spacing.Small),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            fontSize = 10.sp
        )
        Text(
            text = value,
            style = MaterialTheme.typography.labelMedium,
            fontWeight = FontWeight.Bold,
            color = valueColor
        )
    }
}

@Composable
fun EventRow(event: EventLogEntry) {
    val action = event.action ?: ""
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 6.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.Top
    ) {
        Row(
            modifier = Modifier.weight(1f),
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            Icon(
                imageVector = when (action) {
                    "unlock", "ws_unlock" -> Icons.Default.LockOpen
                    "hold_today", "hold_forever" -> Icons.Default.Schedule
                    "stop_hold", "ws_lock" -> Icons.Default.Lock
                    "orphan_cleanup" -> Icons.Default.Delete
                    else -> Icons.Default.Circle
                },
                contentDescription = null,
                modifier = Modifier.size(UiConstants.IconSize.Small),
                tint = MaterialTheme.colorScheme.onSurfaceVariant
            )
            Column {
                Row {
                    Text(
                        text = event.actionLabel,
                        style = MaterialTheme.typography.bodySmall,
                        fontWeight = FontWeight.Medium
                    )
                    event.details?.let {
                        Text(
                            text = " $it",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
                val subtitle = listOfNotNull(event.deviceName, event.deviceId?.take(8))
                    .joinToString(" · ")
                if (subtitle.isNotBlank()) {
                    Text(
                        text = subtitle,
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        }
        Text(
            text = event.timestamp.toRelativeTimeDisplay(),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}
