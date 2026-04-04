package com.unifi.gate.ui.components

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.size
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.Dp

/**
 * Consistent loading indicator with predefined sizes
 */
@Composable
fun LoadingIndicator(
    modifier: Modifier = Modifier,
    size: Dp = UiConstants.Loading.DefaultSize
) {
    CircularProgressIndicator(
        modifier = modifier.size(size),
        strokeWidth = UiConstants.Loading.StrokeWidth
    )
}

/**
 * Centered loading indicator, commonly used when loading content
 */
@Composable
fun CenteredLoadingIndicator(
    modifier: Modifier = Modifier,
    size: Dp = UiConstants.Loading.DefaultSize
) {
    Box(
        modifier = modifier.fillMaxWidth(),
        contentAlignment = Alignment.Center
    ) {
        LoadingIndicator(size = size)
    }
}
