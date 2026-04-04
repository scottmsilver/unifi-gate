package com.unifi.gate.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.unifi.gate.api.ApiClient
import com.unifi.gate.model.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class SettingsUiState(
    val isLoading: Boolean = true,
    val config: SiteConfig? = null,
    val devices: List<Device> = emptyList(),
    val events: List<EventLogEntry> = emptyList(),
    val debugData: Map<String, DebugData> = emptyMap(),
    val loadingDebug: Set<String> = emptySet()
)

class SettingsViewModel : ViewModel() {
    private val _uiState = MutableStateFlow(SettingsUiState())
    val uiState: StateFlow<SettingsUiState> = _uiState.asStateFlow()

    fun loadData(serverUrl: String) {
        if (serverUrl.isBlank()) {
            _uiState.value = _uiState.value.copy(isLoading = false)
            return
        }

        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true)
            try {
                val api = ApiClient.getApi(serverUrl)
                val config = try { api.getConfig() } catch (e: Exception) { null }
                val devices = try { api.getDevices() } catch (e: Exception) { emptyList() }
                val events = try { api.getEvents(limit = 50) } catch (e: Exception) { emptyList() }

                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    config = config,
                    devices = devices,
                    events = events
                )
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(isLoading = false)
            }
        }
    }

    fun loadDebugData(serverUrl: String, deviceId: String) {
        if (_uiState.value.debugData.containsKey(deviceId)) return
        if (_uiState.value.loadingDebug.contains(deviceId)) return

        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(
                loadingDebug = _uiState.value.loadingDebug + deviceId
            )
            try {
                val api = ApiClient.getApi(serverUrl)
                val debugData = api.getDebugInfo(deviceId)
                _uiState.value = _uiState.value.copy(
                    debugData = _uiState.value.debugData + (deviceId to debugData),
                    loadingDebug = _uiState.value.loadingDebug - deviceId
                )
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    loadingDebug = _uiState.value.loadingDebug - deviceId
                )
            }
        }
    }
}
