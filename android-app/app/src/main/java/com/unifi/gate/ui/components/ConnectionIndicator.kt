package com.unifi.gate.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage

/**
 * Reusable connection status indicator showing user label and colored dot
 * Uses Material 3 theme colors (tertiary for connected, error for disconnected)
 */
@Composable
fun ConnectionIndicator(
    isConnected: Boolean,
    userDisplayName: String? = null,
    userEmail: String? = null,
    userPhotoUrl: String? = null,
    isDevMode: Boolean = false,
    onClick: (() -> Unit)? = null,
    modifier: Modifier = Modifier
) {
    Row(
        modifier = modifier
            .clip(UiConstants.PillShape)
            .then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier)
            .padding(horizontal = UiConstants.Spacing.Small, vertical = UiConstants.Spacing.XSmall),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp)
    ) {
        // Show user's first name, or fallback to email prefix
        val displayText = when {
            isDevMode -> "Guest"
            userDisplayName != null -> userDisplayName.split(" ").first().take(12)
            userEmail != null -> userEmail.substringBefore("@").take(12)
            else -> "Not signed in"
        }

        Text(
            text = displayText,
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.Medium,
            color = MaterialTheme.colorScheme.outline,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis
        )

        // User avatar: profile photo, letter fallback, or connection dot
        if (!isDevMode && userEmail != null) {
            val borderColor = StatusColors.connectionColor(isConnected)

            if (userPhotoUrl != null) {
                // Show profile photo with colored border
                AsyncImage(
                    model = userPhotoUrl,
                    contentDescription = "Profile",
                    modifier = Modifier
                        .size(UiConstants.AvatarSize.Small)
                        .clip(CircleShape)
                        .border(2.dp, borderColor, CircleShape),
                    contentScale = ContentScale.Crop
                )
            } else {
                // Fallback to letter avatar
                Box(
                    modifier = Modifier
                        .size(UiConstants.AvatarSize.Small)
                        .clip(CircleShape)
                        .background(StatusColors.avatarBackgroundColor(isConnected)),
                    contentAlignment = Alignment.Center
                ) {
                    Text(
                        text = (userDisplayName?.firstOrNull() ?: userEmail.first()).uppercase(),
                        style = MaterialTheme.typography.labelSmall,
                        fontWeight = FontWeight.Bold,
                        color = StatusColors.onAvatarBackgroundColor(isConnected)
                    )
                }
            }
        } else {
            // Simple dot for guest/dev mode
            Box(
                modifier = Modifier
                    .size(UiConstants.Spacing.Small)
                    .clip(CircleShape)
                    .background(StatusColors.connectionColor(isConnected))
            )
        }
    }
}
