package com.unifi.gate.model

import com.google.gson.annotations.SerializedName

// Site config from /config endpoint
data class SiteConfig(
    @SerializedName("site_name")
    val siteName: String = "Access",
    val configured: Boolean = false,
    val connected: Boolean = false,
    @SerializedName("site_timezone")
    val siteTimezone: String? = null,
    @SerializedName("is_admin")
    val isAdmin: Boolean = false
)

// Device from /devices endpoint, merged with /status data
data class Device(
    val id: String,
    val name: String,
    @SerializedName("is_online")
    val isOnline: Boolean = true,
    val status: String? = null,
    val imageUrl: String? = null,
    // From /status endpoint
    @SerializedName("is_held")
    val isHeld: Boolean = false,
    @SerializedName("hold_state")
    val holdState: String? = null,
    @SerializedName("expires_at")
    val expiresAt: Long? = null
)

// Status from /status/{id} endpoint
data class DeviceStatus(
    @SerializedName("device_id")
    val deviceId: String,
    @SerializedName("is_held")
    val isHeld: Boolean = false,
    @SerializedName("hold_state")
    val holdState: String? = null,
    @SerializedName("hold_status")
    val holdStatus: String? = null,
    @SerializedName("expires_at")
    val expiresAt: Long? = null
)

// Type alias - /devices returns a plain array
typealias DevicesResponse = List<Device>

// Response from action endpoints
data class ActionResponse(
    val status: String,
    val action: String? = null,
    val message: String? = null,
    val error: String? = null
)

// Request body for hold/today
data class HoldTodayRequest(
    @SerializedName("end_time")
    val endTime: String  // Format: "HH:MM" (24-hour)
)

// Event log entry
data class EventLogEntry(
    val timestamp: String = "",
    val action: String? = null,
    val actor: String = "",
    @SerializedName("device_id")
    val deviceId: String? = null,
    @SerializedName("device_name")
    val deviceName: String? = null,
    val details: String? = null
)

// /events returns a plain array
typealias EventLogResponse = List<EventLogEntry>

// Debug data from /debug/{id}
data class DebugData(
    val local: LocalDebugData? = null,
    val unifi: UnifiDebugData? = null,
    val websocket: WebsocketDebugData? = null
)

data class LocalDebugData(
    @SerializedName("hold_state")
    val holdState: String? = null,
    @SerializedName("journal_entries")
    val journalEntries: List<JournalEntry> = emptyList()
)

data class JournalEntry(
    val action: String,
    val day: String,
    @SerializedName("device_id")
    val deviceId: String,
    @SerializedName("start_time")
    val startTime: String,
    @SerializedName("end_time")
    val endTime: String,
    val timestamp: String
)

data class UnifiDebugData(
    val door: DoorDebugInfo? = null,
    @SerializedName("hardware_status")
    val hardwareStatus: HardwareStatus? = null,
    @SerializedName("physical_device")
    val physicalDevice: PhysicalDevice? = null,
    val schedule: ScheduleDebugData? = null
)

data class ScheduleDebugData(
    @SerializedName("schedule_info")
    val scheduleInfo: ScheduleInfo? = null
)

data class ScheduleInfo(
    val name: String? = null,
    val type: String? = null,
    @SerializedName("user_timezone")
    val userTimezone: UserTimezone? = null
)

data class UserTimezone(
    @SerializedName("week_schedule")
    val weekSchedule: Map<String, List<Any>>? = null
)

data class DoorDebugInfo(
    val name: String? = null,
    @SerializedName("full_name")
    val fullName: String? = null,
    val floor: String? = null,
    @SerializedName("unique_id")
    val uniqueId: String? = null
)

data class HardwareStatus(
    @SerializedName("door_lock_relay_status")
    val doorLockRelayStatus: String? = null,
    @SerializedName("door_position_status")
    val doorPositionStatus: String? = null
)

data class PhysicalDevice(
    val name: String? = null,
    @SerializedName("device_type")
    val deviceType: String? = null,
    val model: String? = null,
    val firmware: String? = null,
    val ip: String? = null,
    val mac: String? = null,
    @SerializedName("is_online")
    val isOnline: Boolean = false
)

data class WebsocketDebugData(
    val connected: Boolean = false,
    @SerializedName("recent_events")
    val recentEvents: List<WebsocketEvent> = emptyList()
)

data class WebsocketEvent(
    val event: String? = null,
    @SerializedName("_received_at")
    val receivedAt: String? = null
)
