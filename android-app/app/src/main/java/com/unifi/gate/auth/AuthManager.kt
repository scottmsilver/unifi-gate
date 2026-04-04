package com.unifi.gate.auth

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.google.firebase.auth.FirebaseAuth
import com.google.firebase.auth.FirebaseUser
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.tasks.await

private val Context.authDataStore: DataStore<Preferences> by preferencesDataStore(name = "auth")

/**
 * Manages Firebase authentication and token storage.
 */
class AuthManager(private val context: Context) {
    private val auth = FirebaseAuth.getInstance()

    companion object {
        private val AUTH_TOKEN_KEY = stringPreferencesKey("auth_token")
        private val USER_EMAIL_KEY = stringPreferencesKey("user_email")

        @Volatile
        private var instance: AuthManager? = null

        fun getInstance(context: Context): AuthManager {
            return instance ?: synchronized(this) {
                instance ?: AuthManager(context.applicationContext).also { instance = it }
            }
        }
    }

    /**
     * Get the current Firebase user, if any.
     */
    val currentUser: FirebaseUser?
        get() = auth.currentUser

    /**
     * Check if user is signed in.
     */
    val isSignedIn: Boolean
        get() = auth.currentUser != null

    /**
     * Flow of the current user email.
     */
    val userEmailFlow: Flow<String?> = context.authDataStore.data.map { preferences ->
        preferences[USER_EMAIL_KEY]
    }

    /**
     * Flow of the current auth token.
     */
    val tokenFlow: Flow<String?> = context.authDataStore.data.map { preferences ->
        preferences[AUTH_TOKEN_KEY]
    }

    /**
     * Get the current auth token (cached in DataStore).
     */
    suspend fun getToken(): String? {
        return context.authDataStore.data.first()[AUTH_TOKEN_KEY]
    }

    /**
     * Refresh and get a fresh Firebase ID token.
     * Call this when making API requests or when token might be expired.
     */
    suspend fun getFreshToken(): String? {
        val user = auth.currentUser ?: return null
        return try {
            val result = user.getIdToken(true).await()
            val token = result.token
            if (token != null) {
                saveToken(token, user.email)
            }
            token
        } catch (e: Exception) {
            null
        }
    }

    /**
     * Save token and email to DataStore.
     */
    private suspend fun saveToken(token: String, email: String?) {
        context.authDataStore.edit { preferences ->
            preferences[AUTH_TOKEN_KEY] = token
            if (email != null) {
                preferences[USER_EMAIL_KEY] = email
            }
        }
    }

    /**
     * Clear stored auth data on sign out.
     */
    suspend fun clearAuth() {
        context.authDataStore.edit { preferences ->
            preferences.remove(AUTH_TOKEN_KEY)
            preferences.remove(USER_EMAIL_KEY)
        }
    }

    /**
     * Sign out from Firebase and clear local data.
     */
    suspend fun signOut() {
        auth.signOut()
        clearAuth()
    }

    /**
     * Handle successful Firebase authentication.
     * Called after Google Sign-In completes.
     */
    suspend fun onAuthSuccess(user: FirebaseUser) {
        val token = user.getIdToken(false).await().token
        if (token != null) {
            saveToken(token, user.email)
        }
    }
}
