package com.unifi.gate.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.unifi.gate.api.ApiClient
import com.unifi.gate.model.Device
import com.unifi.gate.model.HoldTodayRequest
import com.unifi.gate.util.CachedState
import com.unifi.gate.util.SettingsManager
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

// Pending action types for optimistic UI updates
enum class PendingAction {
    UNLOCKING,      // "Once" - momentary unlock
    HOLDING_TODAY,  // "Until X" - hold until time
    HOLDING_FOREVER,// "Forever" - hold indefinitely
    CLOSING         // Stopping a hold
}

data class GateUiState(
    val devices: List<Device> = emptyList(),
    val isInitialized: Boolean = false,  // True after we've checked for saved URL
    val isLoading: Boolean = false,      // Initial load only
    val isRefreshing: Boolean = false,   // Pull-to-refresh only
    val error: String? = null,
    val serverUrl: String = "",
    val isConfigured: Boolean = false,
    val actionInProgress: Map<String, Boolean> = emptyMap(),
    val pendingAction: Map<String, PendingAction> = emptyMap(),  // Optimistic action tracking
    val siteName: String = "Access",
    val isConnected: Boolean = false,
    val isAdmin: Boolean = false
)

class GateViewModel : ViewModel() {
    private val _uiState = MutableStateFlow(GateUiState())
    val uiState: StateFlow<GateUiState> = _uiState.asStateFlow()

    private var autoRefreshEnabled = false
    private var settingsManager: SettingsManager? = null

    fun setSettingsManager(manager: SettingsManager) {
        settingsManager = manager
    }

    // Load cached state for instant startup
    fun loadFromCache(cached: CachedState) {
        _uiState.value = _uiState.value.copy(
            devices = cached.devices,
            siteName = cached.siteName,
            isConnected = cached.isConnected,
            isInitialized = true
        )
    }

    fun setServerUrl(url: String) {
        _uiState.value = _uiState.value.copy(
            serverUrl = url,
            isConfigured = url.isNotBlank(),
            isInitialized = true
        )
        if (url.isNotBlank()) {
            ApiClient.clearClient()
            initialLoad()
        }
    }

    // Called when we've checked DataStore but no URL was saved
    fun markInitialized() {
        _uiState.value = _uiState.value.copy(isInitialized = true)
    }

    // Initial load - shows centered spinner
    private fun initialLoad() {
        _uiState.value = _uiState.value.copy(isLoading = true, error = null)
        fetchDevices {
            _uiState.value = _uiState.value.copy(isLoading = false)
        }
    }

    // User pull-to-refresh - shows pull indicator
    fun refresh() {
        _uiState.value = _uiState.value.copy(isRefreshing = true)
        fetchDevices {
            _uiState.value = _uiState.value.copy(isRefreshing = false)
        }
    }

    // Silent refresh for auto-refresh and after actions - no indicator
    fun loadDevices() {
        fetchDevices {}
    }

    // Synchronous fetch that suspends until complete (for use after actions)
    private suspend fun fetchDevicesAndWait() {
        val serverUrl = _uiState.value.serverUrl
        if (serverUrl.isBlank()) return

        try {
            val api = ApiClient.getApi(serverUrl)

            // Fetch site config
            try {
                val config = api.getConfig()
                _uiState.value = _uiState.value.copy(
                    siteName = config.siteName.ifBlank { "Access" },
                    isConnected = config.connected,
                    isAdmin = config.isAdmin
                )
            } catch (e: Exception) {
                // Config fetch failed, use defaults
            }

            val devices = api.getDevices()

            // Fetch status for each device and merge
            val devicesWithStatus = devices.map { device ->
                try {
                    val status = api.getDeviceStatus(device.id)
                    device.copy(
                        isHeld = status.isHeld,
                        holdState = status.holdState,
                        expiresAt = status.expiresAt
                    )
                } catch (e: Exception) {
                    device
                }
            }

            _uiState.value = _uiState.value.copy(
                devices = devicesWithStatus,
                error = null
            )

            // Cache for instant startup next time
            settingsManager?.let { manager ->
                val state = _uiState.value
                manager.cacheDevices(devicesWithStatus, state.siteName, state.isConnected)
            }
        } catch (e: Exception) {
            // Silently ignore errors during post-action refresh
        }
    }

    private fun fetchDevices(onComplete: () -> Unit) {
        val serverUrl = _uiState.value.serverUrl
        if (serverUrl.isBlank()) {
            _uiState.value = _uiState.value.copy(
                error = "Server URL not configured",
                isLoading = false,
                isRefreshing = false
            )
            return
        }

        viewModelScope.launch {
            try {
                val api = ApiClient.getApi(serverUrl)

                // Fetch site config
                try {
                    val config = api.getConfig()
                    _uiState.value = _uiState.value.copy(
                        siteName = config.siteName.ifBlank { "Access" },
                        isConnected = config.connected,
                        isAdmin = config.isAdmin
                    )
                } catch (e: Exception) {
                    // Config fetch failed, use defaults
                }

                val devices = api.getDevices()

                // Fetch status for each device and merge
                val devicesWithStatus = devices.map { device ->
                    try {
                        val status = api.getDeviceStatus(device.id)
                        device.copy(
                            isHeld = status.isHeld,
                            holdState = status.holdState,
                            expiresAt = status.expiresAt
                        )
                    } catch (e: Exception) {
                        device // Keep original if status fetch fails
                    }
                }

                _uiState.value = _uiState.value.copy(
                    devices = devicesWithStatus,
                    error = null
                )

                // Cache for instant startup next time
                settingsManager?.let { manager ->
                    val state = _uiState.value
                    manager.cacheDevices(devicesWithStatus, state.siteName, state.isConnected)
                }
            } catch (e: Exception) {
                // Only show error if we have no devices yet
                if (_uiState.value.devices.isEmpty()) {
                    _uiState.value = _uiState.value.copy(
                        error = e.message ?: "Unknown error"
                    )
                }
            } finally {
                onComplete()
            }
        }
    }

    fun unlockGate(deviceId: String) {
        performAction(deviceId, PendingAction.UNLOCKING) { api ->
            api.unlockGate(deviceId)
        }
    }

    fun holdGateOpen(deviceId: String, endTime: String? = null) {
        performAction(deviceId, PendingAction.HOLDING_TODAY) { api ->
            val time = endTime ?: "18:00"
            android.util.Log.d("GateViewModel", "holdGateOpen: deviceId=$deviceId, endTime=$time")
            api.holdGateOpen(deviceId, HoldTodayRequest(time))
        }
    }

    fun holdGateForever(deviceId: String) {
        performAction(deviceId, PendingAction.HOLDING_FOREVER) { api ->
            api.holdGateForever(deviceId)
        }
    }

    fun closeGate(deviceId: String) {
        performAction(deviceId, PendingAction.CLOSING) { api ->
            api.closeGate(deviceId)
        }
    }

    fun forceSync(deviceId: String) {
        // No optimistic state for sync
        viewModelScope.launch {
            val actionMap = _uiState.value.actionInProgress.toMutableMap()
            actionMap[deviceId] = true
            _uiState.value = _uiState.value.copy(actionInProgress = actionMap)
            try {
                val api = ApiClient.getApi(_uiState.value.serverUrl)
                api.forceSync(deviceId)
                delay(300)
                loadDevices()
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(error = "Sync failed: ${e.message}")
            } finally {
                val finalMap = _uiState.value.actionInProgress.toMutableMap()
                finalMap.remove(deviceId)
                _uiState.value = _uiState.value.copy(actionInProgress = finalMap)
            }
        }
    }

    private fun performAction(
        deviceId: String,
        pendingAction: PendingAction,
        action: suspend (api: com.unifi.gate.api.UniFiGateApi) -> Unit
    ) {
        viewModelScope.launch {
            // Set optimistic state immediately
            val actionMap = _uiState.value.actionInProgress.toMutableMap()
            val pendingMap = _uiState.value.pendingAction.toMutableMap()
            actionMap[deviceId] = true
            pendingMap[deviceId] = pendingAction
            _uiState.value = _uiState.value.copy(
                actionInProgress = actionMap,
                pendingAction = pendingMap
            )

            try {
                val api = ApiClient.getApi(_uiState.value.serverUrl)
                action(api)

                // For unlock (momentary), show "Unlocked" briefly
                // For holds, just a brief pause before refresh
                if (pendingAction == PendingAction.UNLOCKING) {
                    delay(2500)  // Show "Unlocked" for 2.5 seconds
                } else {
                    delay(500)   // Brief pause for server
                }

                // Fetch new state - this suspends until complete
                fetchDevicesAndWait()

            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    error = "Action failed: ${e.message}"
                )
            } finally {
                // Clear pending state AFTER devices are loaded
                val finalActionMap = _uiState.value.actionInProgress.toMutableMap()
                val finalPendingMap = _uiState.value.pendingAction.toMutableMap()
                finalActionMap.remove(deviceId)
                finalPendingMap.remove(deviceId)
                _uiState.value = _uiState.value.copy(
                    actionInProgress = finalActionMap,
                    pendingAction = finalPendingMap
                )
            }
        }
    }

    fun startAutoRefresh() {
        if (autoRefreshEnabled) return
        autoRefreshEnabled = true
        viewModelScope.launch {
            while (autoRefreshEnabled) {
                delay(5000)
                if (_uiState.value.isConfigured && !_uiState.value.isLoading && !_uiState.value.isRefreshing) {
                    loadDevices()  // Silent refresh
                }
            }
        }
    }

    fun stopAutoRefresh() {
        autoRefreshEnabled = false
    }

    fun clearError() {
        _uiState.value = _uiState.value.copy(error = null)
    }
}
