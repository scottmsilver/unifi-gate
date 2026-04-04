package com.unifi.gate.util

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import com.unifi.gate.model.Device
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "settings")

class SettingsManager(private val context: Context) {

    companion object {
        private val SERVER_URL_KEY = stringPreferencesKey("server_url")
        private val CACHED_DEVICES_KEY = stringPreferencesKey("cached_devices")
        private val CACHED_SITE_NAME_KEY = stringPreferencesKey("cached_site_name")
        private val CACHED_CONNECTED_KEY = stringPreferencesKey("cached_connected")
        const val DEFAULT_SERVER_URL = ""
    }

    private val gson = Gson()

    val serverUrl: Flow<String> = context.dataStore.data.map { preferences ->
        preferences[SERVER_URL_KEY] ?: DEFAULT_SERVER_URL
    }

    suspend fun saveServerUrl(url: String) {
        context.dataStore.edit { preferences ->
            preferences[SERVER_URL_KEY] = url
        }
    }

    // Cache devices for instant startup
    suspend fun cacheDevices(devices: List<Device>, siteName: String, isConnected: Boolean) {
        context.dataStore.edit { preferences ->
            preferences[CACHED_DEVICES_KEY] = gson.toJson(devices)
            preferences[CACHED_SITE_NAME_KEY] = siteName
            preferences[CACHED_CONNECTED_KEY] = isConnected.toString()
        }
    }

    // Get cached devices (returns null if no cache)
    suspend fun getCachedDevices(): CachedState? {
        val prefs = context.dataStore.data.first()
        val devicesJson = prefs[CACHED_DEVICES_KEY] ?: return null
        return try {
            val type = object : TypeToken<List<Device>>() {}.type
            val devices: List<Device> = gson.fromJson(devicesJson, type)
            CachedState(
                devices = devices,
                siteName = prefs[CACHED_SITE_NAME_KEY] ?: "Access",
                isConnected = prefs[CACHED_CONNECTED_KEY]?.toBoolean() ?: false
            )
        } catch (e: Exception) {
            null
        }
    }

    suspend fun clearSettings() {
        context.dataStore.edit { preferences ->
            preferences.clear()
        }
    }
}

data class CachedState(
    val devices: List<Device>,
    val siteName: String,
    val isConnected: Boolean
)
