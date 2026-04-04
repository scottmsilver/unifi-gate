package com.unifi.gate.util

import java.text.SimpleDateFormat
import java.util.*

/**
 * Ensures URL has http:// or https:// scheme
 */
fun String.toHttpUrl(): String {
    val trimmed = this.trim().trimEnd('/')
    return if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
        trimmed
    } else {
        "http://$trimmed"
    }
}

/**
 * Builds full URL from server base URL and path
 */
fun String.buildUrl(path: String): String {
    return "${this.toHttpUrl()}$path"
}

/**
 * Formats Unix timestamp to short time display (e.g., "6p" or "7:30p")
 */
fun Long.toShortTimeDisplay(): String? {
    return try {
        val date = Date(this * 1000)
        val cal = Calendar.getInstance().apply { time = date }
        val hour = cal.get(Calendar.HOUR)
        val minute = cal.get(Calendar.MINUTE)
        val ampm = if (cal.get(Calendar.AM_PM) == Calendar.AM) "a" else "p"
        val displayHour = if (hour == 0) 12 else hour
        if (minute > 0) {
            "$displayHour:${minute.toString().padStart(2, '0')}$ampm"
        } else {
            "$displayHour$ampm"
        }
    } catch (e: Exception) {
        null
    }
}

/**
 * Formats ISO timestamp to relative time display
 */
fun String.toRelativeTimeDisplay(): String {
    return try {
        val sdf = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US)
        val date = sdf.parse(this) ?: return this.take(16)
        val now = Date()
        val isToday = SimpleDateFormat("yyyyMMdd", Locale.US).format(date) ==
                SimpleDateFormat("yyyyMMdd", Locale.US).format(now)
        if (isToday) {
            SimpleDateFormat("h:mm a", Locale.US).format(date)
        } else {
            SimpleDateFormat("MMM d h:mm a", Locale.US).format(date)
        }
    } catch (e: Exception) {
        this.take(16)
    }
}
