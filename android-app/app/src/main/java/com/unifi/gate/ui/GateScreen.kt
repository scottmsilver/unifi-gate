package com.unifi.gate.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import eu.bambooapps.material3.pullrefresh.PullRefreshIndicator
import eu.bambooapps.material3.pullrefresh.pullRefresh
import eu.bambooapps.material3.pullrefresh.rememberPullRefreshState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import com.unifi.gate.model.Device
import com.unifi.gate.model.getStatusText
import com.unifi.gate.model.hasDoorImage
import com.unifi.gate.model.holdButtonText
import com.unifi.gate.model.isOpen
import com.unifi.gate.model.isOpenOrPending
import com.unifi.gate.model.isPast6pm
import com.unifi.gate.model.statusText
import com.unifi.gate.ui.components.ConnectionIndicator
import com.unifi.gate.ui.components.EmptyState
import com.unifi.gate.ui.components.LoadingIndicator
import com.unifi.gate.ui.components.StatusColors
import com.unifi.gate.ui.components.UiConstants
import com.unifi.gate.util.buildUrl
import androidx.compose.foundation.border
import androidx.compose.foundation.shape.RoundedCornerShape
import java.util.Calendar

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun GateScreen(
    uiState: GateUiState,
    onUnlock: (String) -> Unit,
    onHoldToday: (String, String?) -> Unit,  // (deviceId, endTime) - endTime is "HH:MM" format
    onHoldForever: (String) -> Unit,
    onStopHold: (String) -> Unit,
    onRefresh: () -> Unit,
    onSettingsClick: () -> Unit,
    onLogout: () -> Unit,
    serverUrl: String,
    userDisplayName: String? = null,
    userEmail: String? = null,
    userPhotoUrl: String? = null,
    isDevMode: Boolean = false
) {
    val snackbarHostState = remember { SnackbarHostState() }
    var showProfileMenu by remember { mutableStateOf(false) }

    // Show error in snackbar
    LaunchedEffect(uiState.error) {
        uiState.error?.let { error ->
            if (uiState.devices.isNotEmpty()) {
                snackbarHostState.showSnackbar(
                    message = error,
                    duration = SnackbarDuration.Short
                )
            }
        }
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbarHostState) },
        topBar = {
            TopAppBar(
                title = {
                    Text(
                        text = uiState.siteName,
                        fontWeight = FontWeight.Bold,
                        letterSpacing = (-0.5).sp
                    )
                },
                actions = {
                    // Tappable profile area with dropdown menu
                    Box {
                        ConnectionIndicator(
                            isConnected = uiState.isConnected,
                            userDisplayName = userDisplayName,
                            userEmail = userEmail,
                            userPhotoUrl = userPhotoUrl,
                            isDevMode = isDevMode,
                            onClick = { showProfileMenu = true },
                            modifier = Modifier.padding(end = 8.dp)
                        )
                        DropdownMenu(
                            expanded = showProfileMenu,
                            onDismissRequest = { showProfileMenu = false }
                        ) {
                            // User info header
                            if (userEmail != null && !isDevMode) {
                                Column(
                                    modifier = Modifier
                                        .padding(horizontal = 12.dp, vertical = 8.dp)
                                ) {
                                    Row(
                                        verticalAlignment = Alignment.CenterVertically,
                                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                                    ) {
                                        Text(
                                            text = userDisplayName ?: userEmail,
                                            style = MaterialTheme.typography.bodyMedium,
                                            fontWeight = FontWeight.Medium
                                        )
                                        if (uiState.isAdmin) {
                                            Text(
                                                text = "Admin",
                                                style = MaterialTheme.typography.labelSmall,
                                                fontWeight = FontWeight.Bold,
                                                color = MaterialTheme.colorScheme.primary,
                                                modifier = Modifier
                                                    .background(
                                                        MaterialTheme.colorScheme.primaryContainer,
                                                        RoundedCornerShape(4.dp)
                                                    )
                                                    .padding(horizontal = 6.dp, vertical = 2.dp)
                                            )
                                        }
                                    }
                                    if (userDisplayName != null) {
                                        Text(
                                            text = userEmail,
                                            style = MaterialTheme.typography.bodySmall,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant
                                        )
                                    }
                                }
                                HorizontalDivider(modifier = Modifier.padding(vertical = 4.dp))
                            }
                            DropdownMenuItem(
                                text = { Text("Settings") },
                                onClick = {
                                    showProfileMenu = false
                                    onSettingsClick()
                                },
                                leadingIcon = {
                                    Icon(Icons.Default.Settings, contentDescription = null)
                                }
                            )
                            if (!isDevMode) {
                                DropdownMenuItem(
                                    text = { Text("Logout") },
                                    onClick = {
                                        showProfileMenu = false
                                        onLogout()
                                    },
                                    leadingIcon = {
                                        Icon(Icons.Default.Logout, contentDescription = null)
                                    }
                                )
                            }
                        }
                    }
                }
            )
        }
    ) { paddingValues ->
        val pullRefreshState = rememberPullRefreshState(
            refreshing = uiState.isRefreshing,
            onRefresh = onRefresh
        )

        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(paddingValues)
                .pullRefresh(pullRefreshState)
        ) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(horizontal = 16.dp)
            ) {
                Spacer(modifier = Modifier.height(8.dp))

                when {
                    !uiState.isInitialized || uiState.isLoading -> {
                        Box(
                            modifier = Modifier.fillMaxWidth().weight(1f),
                            contentAlignment = Alignment.Center
                        ) {
                            LoadingIndicator(size = UiConstants.Loading.LargeSize)
                        }
                    }
                    !uiState.isConfigured -> {
                        EmptyState(
                            message = "Server not configured",
                            submessage = "Tap the settings icon to configure your server URL",
                            icon = Icons.Default.Settings
                        )
                    }
                    uiState.error != null && uiState.devices.isEmpty() -> {
                        EmptyState(
                            message = "Connection Error",
                            submessage = uiState.error,
                            icon = Icons.Default.Error
                        )
                    }
                    uiState.devices.isEmpty() -> {
                        EmptyState(
                            message = "No Devices Found",
                            submessage = "No doors are configured on your server",
                            icon = Icons.Default.DoorFront
                        )
                    }
                    else -> {
                        LazyColumn(
                            modifier = Modifier.weight(1f),
                            verticalArrangement = Arrangement.spacedBy(12.dp)
                        ) {
                            items(uiState.devices) { device ->
                                DoorCard(
                                    device = device,
                                    serverUrl = serverUrl,
                                    isActionInProgress = uiState.actionInProgress[device.id] == true,
                                    pendingAction = uiState.pendingAction[device.id],
                                    onUnlock = { onUnlock(device.id) },
                                    onHoldToday = { endTime -> onHoldToday(device.id, endTime) },
                                    onHoldForever = { onHoldForever(device.id) },
                                    onStopHold = { onStopHold(device.id) }
                                )
                            }
                            item {
                                Spacer(modifier = Modifier.height(16.dp))
                            }
                        }
                    }
                }
            }

            PullRefreshIndicator(
                refreshing = uiState.isRefreshing,
                state = pullRefreshState,
                modifier = Modifier.align(Alignment.TopCenter)
            )
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DoorCard(
    device: Device,
    serverUrl: String,
    isActionInProgress: Boolean,
    pendingAction: PendingAction?,
    onUnlock: () -> Unit,
    onHoldToday: (String?) -> Unit,  // endTime in "HH:MM" format, null for default 6pm
    onHoldForever: () -> Unit,
    onStopHold: () -> Unit
) {
    var showTimePicker by remember { mutableStateOf(false) }
    val haptic = LocalHapticFeedback.current
    val isPast6pmNow = remember { isPast6pm() }

    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = UiConstants.CardShape,
        elevation = CardDefaults.cardElevation(defaultElevation = 1.dp)
    ) {
        Column {
            // Hero image (full width) or fallback icon
            if (device.hasDoorImage) {
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(UiConstants.Thumbnail.Height)
                        .background(MaterialTheme.colorScheme.surfaceVariant)
                ) {
                    AsyncImage(
                        model = serverUrl.buildUrl(device.imageUrl!!),
                        contentDescription = device.name,
                        modifier = Modifier.fillMaxSize(),
                        contentScale = ContentScale.Crop
                    )
                }
            }

            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                // Title row: Name, status, and loading indicator
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    // Use optimistic state for immediate feedback
                    val isOpenOptimistic = device.isOpenOrPending(pendingAction)

                    Row(
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        // Lock icon (always show, serves as status indicator)
                        Box(
                            modifier = Modifier
                                .size(40.dp)
                                .clip(CircleShape)
                                .background(StatusColors.connectionContainerColor(isOpenOptimistic)),
                            contentAlignment = Alignment.Center
                        ) {
                            Icon(
                                imageVector = if (isOpenOptimistic) Icons.Default.LockOpen else Icons.Default.Lock,
                                contentDescription = null,
                                tint = StatusColors.onConnectionContainerColor(isOpenOptimistic),
                                modifier = Modifier.size(20.dp)
                            )
                        }

                        // Name and status text
                        Column {
                            Text(
                                text = device.name,
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.Bold,
                                fontSize = 18.sp,
                                lineHeight = 22.sp
                            )
                            Text(
                                text = device.getStatusText(pendingAction),
                                style = MaterialTheme.typography.labelSmall,
                                fontWeight = FontWeight.Medium,
                                color = StatusColors.deviceStatusColor(device.isOnline, isOpenOptimistic),
                                fontSize = 12.sp
                            )
                        }
                    }

                    // Loading indicator
                    if (isActionInProgress) {
                        LoadingIndicator(size = UiConstants.Loading.SmallSize)
                    }
                }

            // Bottom row: Segmented button for open modes
            // Determine selected index: -1=none, 0=once(never stays), 1=until, 2=forever
            val selectedIndex = when (device.holdState) {
                "hold_today" -> 1
                "hold_forever" -> 2
                else -> -1
            }

            SingleChoiceSegmentedButtonRow(
                modifier = Modifier.fillMaxWidth()
            ) {
                // Once button (index 0) - action only, never stays selected
                SegmentedButton(
                    selected = false,  // Never shows as selected
                    onClick = {
                        haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                        onUnlock()
                    },
                    enabled = device.isOnline && !isActionInProgress,
                    shape = SegmentedButtonDefaults.itemShape(index = 0, count = 3),
                    icon = {}  // No checkmark
                ) {
                    Text("Once")
                }

                // Until button (index 1)
                SegmentedButton(
                    selected = selectedIndex == 1,
                    onClick = {
                        haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                        when {
                            device.holdState == "hold_today" -> showTimePicker = true
                            isPast6pmNow -> showTimePicker = true
                            else -> onHoldToday(null)
                        }
                    },
                    enabled = device.isOnline && !isActionInProgress,
                    shape = SegmentedButtonDefaults.itemShape(index = 1, count = 3),
                    icon = { if (selectedIndex == 1) SegmentedButtonDefaults.ActiveIcon() }
                ) {
                    Text(device.holdButtonText(isPast6pmNow))
                }

                // Forever button (index 2)
                SegmentedButton(
                    selected = selectedIndex == 2,
                    onClick = {
                        haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                        if (device.holdState == "hold_forever") onStopHold() else onHoldForever()
                    },
                    enabled = device.isOnline && !isActionInProgress,
                    shape = SegmentedButtonDefaults.itemShape(index = 2, count = 3),
                    icon = { if (selectedIndex == 2) SegmentedButtonDefaults.ActiveIcon() }
                ) {
                    Text("Forever")
                }
            }
            }
        }
    }

    // Time Picker Dialog
    if (showTimePicker) {
        TimePickerDialog(
            onDismiss = { showTimePicker = false },
            onConfirm = { hour, minute ->
                showTimePicker = false
                val endTime = String.format("%02d:%02d", hour, minute)
                android.util.Log.d("GateScreen", "TimePicker confirmed: hour=$hour, minute=$minute, endTime=$endTime")
                onHoldToday(endTime)
            }
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TimePickerDialog(
    onDismiss: () -> Unit,
    onConfirm: (hour: Int, minute: Int) -> Unit
) {
    // Start at the next hour (e.g., if 8:xx, start at 9:00)
    val calendar = Calendar.getInstance()
    val nextHour = (calendar.get(Calendar.HOUR_OF_DAY) + 1).coerceAtMost(23)

    val timePickerState = rememberTimePickerState(
        initialHour = nextHour,
        initialMinute = 0,
        is24Hour = false
    )

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Hold Open Until") },
        text = {
            Box(
                modifier = Modifier.fillMaxWidth(),
                contentAlignment = Alignment.Center
            ) {
                TimePicker(state = timePickerState)
            }
        },
        confirmButton = {
            TextButton(onClick = { onConfirm(timePickerState.hour, timePickerState.minute) }) {
                Text("Set")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("Cancel")
            }
        }
    )
}
