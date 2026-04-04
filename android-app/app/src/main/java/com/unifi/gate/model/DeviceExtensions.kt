package com.unifi.gate.model

import androidx.compose.ui.graphics.Color
import com.unifi.gate.ui.PendingAction
import com.unifi.gate.ui.theme.AppColors
import com.unifi.gate.util.toShortTimeDisplay
import java.util.Calendar

/**
 * Whether the device is currently in an open/unlocked state
 */
val Device.isOpen: Boolean
    get() = isHeld

/**
 * Whether device should appear open (actual state OR optimistic pending action)
 */
fun Device.isOpenOrPending(pendingAction: PendingAction?): Boolean {
    return when (pendingAction) {
        PendingAction.UNLOCKING, PendingAction.HOLDING_TODAY, PendingAction.HOLDING_FOREVER -> true
        PendingAction.CLOSING -> false
        null -> isOpen
    }
}

/**
 * Formatted expiry time for display (e.g., "6p" or "7:30p")
 */
val Device.expiryDisplay: String?
    get() = expiresAt?.toShortTimeDisplay()

/**
 * Status text for display in UI (without pending action)
 */
val Device.statusText: String
    get() = getStatusText(null)

/**
 * Status text with optimistic pending action support
 */
fun Device.getStatusText(pendingAction: PendingAction?): String = when {
    !isOnline -> "Offline"
    // Show optimistic status based on pending action
    pendingAction == PendingAction.UNLOCKING -> "Unlocking..."
    pendingAction == PendingAction.HOLDING_TODAY -> "Opening..."
    pendingAction == PendingAction.HOLDING_FOREVER -> "Opening..."
    pendingAction == PendingAction.CLOSING -> "Closing..."
    // Normal status
    isOpen && holdState == "hold_forever" -> "Open · forever"
    isOpen && holdState == "hold_today" && expiryDisplay != null -> "Open · until $expiryDisplay"
    isOpen -> "Open"
    else -> "Locked"
}

/**
 * Whether this device has a door image URL
 */
val Device.hasDoorImage: Boolean
    get() = imageUrl != null && imageUrl.startsWith("/door-image/")

/**
 * Status color for display in UI
 */
val Device.statusColor: Color
    get() = when {
        !isOnline -> AppColors.StatusGray
        isOpen -> AppColors.StatusGreen
        else -> AppColors.StatusRed
    }

/**
 * Icon background color based on device state
 */
val Device.iconBackgroundColor: Color
    get() = if (isOpen) AppColors.IconBgGreen else AppColors.IconBgRed

/**
 * Icon tint color based on device state
 */
val Device.iconColor: Color
    get() = if (isOpen) AppColors.IconGreen else AppColors.IconRed

/**
 * Hold button text - encapsulates the business logic for "Until" button display
 */
fun Device.holdButtonText(isPast6pm: Boolean): String = when {
    holdState == "hold_today" && expiryDisplay != null -> "Until $expiryDisplay"
    isPast6pm -> "Until..."
    else -> "Until 6p"
}

/**
 * Check if current time is past 6pm (for hold button behavior)
 */
fun isPast6pm(): Boolean = Calendar.getInstance().get(Calendar.HOUR_OF_DAY) >= 18

/**
 * Display label for event action
 */
val EventLogEntry.actionLabel: String
    get() {
        val actionValue = action
        return when (actionValue) {
            "unlock" -> "Opened once"
            "hold_today" -> "Held open"
            "hold_forever" -> "Held open forever"
            "stop_hold" -> "Released hold"
            "orphan_cleanup" -> "Auto cleanup"
            "sync" -> "Sync"
            "ws_unlock" -> "Unlocked"
            "ws_lock" -> "Locked"
            "ws_rex" -> "REX pressed"
            "ws_door_position" -> "Door moved"
            null -> ""
            else -> actionValue
        }
    }
