package com.unifi.gate.ui.components

import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * Provides consistent status-based color logic across the app.
 * Uses Material 3 semantic colors:
 * - tertiary: positive/connected/open states
 * - error: negative/disconnected/locked states
 * - outline: neutral/offline states
 */
object StatusColors {
    /**
     * Returns the appropriate color for a connected/disconnected status
     */
    @Composable
    fun connectionColor(isConnected: Boolean): Color {
        return if (isConnected) {
            MaterialTheme.colorScheme.tertiary
        } else {
            MaterialTheme.colorScheme.error
        }
    }

    /**
     * Returns the appropriate background color for a connected/disconnected status
     */
    @Composable
    fun connectionContainerColor(isConnected: Boolean): Color {
        return if (isConnected) {
            MaterialTheme.colorScheme.tertiaryContainer
        } else {
            MaterialTheme.colorScheme.errorContainer
        }
    }

    /**
     * Returns the appropriate foreground color for a connected/disconnected container
     */
    @Composable
    fun onConnectionContainerColor(isConnected: Boolean): Color {
        return if (isConnected) {
            MaterialTheme.colorScheme.onTertiaryContainer
        } else {
            MaterialTheme.colorScheme.onErrorContainer
        }
    }

    /**
     * Returns the appropriate color for an online/offline/open/locked device status
     * @param isOnline Whether the device is online
     * @param isOpen Whether the device is in an open/unlocked state
     */
    @Composable
    fun deviceStatusColor(isOnline: Boolean, isOpen: Boolean): Color {
        return when {
            !isOnline -> MaterialTheme.colorScheme.outline
            isOpen -> MaterialTheme.colorScheme.tertiary
            else -> MaterialTheme.colorScheme.error
        }
    }

    /**
     * Returns appropriate background color for primary connected state (avatar fallback)
     */
    @Composable
    fun avatarBackgroundColor(isConnected: Boolean): Color {
        return if (isConnected) {
            MaterialTheme.colorScheme.primaryContainer
        } else {
            MaterialTheme.colorScheme.errorContainer
        }
    }

    /**
     * Returns appropriate foreground color for primary connected state (avatar fallback)
     */
    @Composable
    fun onAvatarBackgroundColor(isConnected: Boolean): Color {
        return if (isConnected) {
            MaterialTheme.colorScheme.onPrimaryContainer
        } else {
            MaterialTheme.colorScheme.onErrorContainer
        }
    }
}
