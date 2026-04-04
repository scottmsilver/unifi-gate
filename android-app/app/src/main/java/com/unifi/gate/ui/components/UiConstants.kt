package com.unifi.gate.ui.components

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.unit.dp

/**
 * Shared UI constants to avoid magic numbers and ensure consistency
 */
object UiConstants {
    // Corner radii
    val CardCornerRadius = 12.dp
    val CardShape = RoundedCornerShape(CardCornerRadius)

    val SmallCornerRadius = 8.dp
    val SmallCardShape = RoundedCornerShape(SmallCornerRadius)

    val PillCornerRadius = 20.dp
    val PillShape = RoundedCornerShape(PillCornerRadius)

    // Spacing
    object Spacing {
        val XSmall = 4.dp
        val Small = 8.dp
        val Medium = 12.dp
        val Large = 16.dp
        val XLarge = 24.dp
        val XXLarge = 32.dp
    }

    // Loading indicator
    object Loading {
        val SmallSize = 20.dp
        val DefaultSize = 24.dp
        val LargeSize = 32.dp
        val StrokeWidth = 2.dp
    }

    // Icon sizes
    object IconSize {
        val Small = 16.dp
        val Default = 24.dp
        val Large = 48.dp
        val XLarge = 64.dp
    }

    // Avatar sizes
    object AvatarSize {
        val Small = 24.dp
        val Large = 48.dp
    }

    // Thumbnail sizes (door images)
    object Thumbnail {
        val Width = 192.dp
        val Height = 112.dp
    }
}
