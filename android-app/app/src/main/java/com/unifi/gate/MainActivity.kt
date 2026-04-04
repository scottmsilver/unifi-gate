package com.unifi.gate

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.unifi.gate.api.ApiClient
import com.unifi.gate.auth.AuthManager
import com.unifi.gate.ui.GateScreen
import com.unifi.gate.ui.GateViewModel
import com.unifi.gate.ui.LoginScreen
import com.unifi.gate.ui.SettingsScreen
import com.unifi.gate.ui.theme.UniFiGateTheme
import com.unifi.gate.util.SettingsManager
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        // Install splash screen before super.onCreate
        installSplashScreen()

        super.onCreate(savedInstanceState)

        // Enable edge-to-edge display
        enableEdgeToEdge()

        val settingsManager = SettingsManager(this)

        setContent {
            UniFiGateTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {
                    UniFiGateApp(settingsManager)
                }
            }
        }
    }
}

@Composable
fun UniFiGateApp(settingsManager: SettingsManager) {
    val context = LocalContext.current
    val navController = rememberNavController()
    val viewModel: GateViewModel = viewModel()
    val uiState by viewModel.uiState.collectAsState()
    val coroutineScope = rememberCoroutineScope()

    // Auth state
    val authManager = remember { AuthManager.getInstance(context) }
    var isAuthenticated by remember { mutableStateOf(authManager.isSignedIn) }
    var isDevMode by remember { mutableStateOf(false) }

    // Set up the token provider for API requests
    LaunchedEffect(Unit) {
        ApiClient.setTokenProvider {
            // In dev mode (local/LAN URL), don't send token
            if (isDevMode) null else authManager.getFreshToken()
        }
    }

    // Check if we're in dev mode based on server URL
    fun checkDevMode(url: String): Boolean {
        val lowerUrl = url.lowercase()
        return lowerUrl.contains("localhost") ||
            lowerUrl.contains("127.0.0.1") ||
            lowerUrl.matches(Regex(".*192\\.168\\.\\d+\\.\\d+.*")) ||
            lowerUrl.matches(Regex(".*10\\.\\d+\\.\\d+\\.\\d+.*")) ||
            lowerUrl.matches(Regex(".*172\\.(1[6-9]|2\\d|3[0-1])\\.\\d+\\.\\d+.*"))
    }

    // Load cached data and server URL on startup
    LaunchedEffect(Unit) {
        // Give ViewModel access to settings manager for caching
        viewModel.setSettingsManager(settingsManager)

        // Load cached state first for instant display
        val cached = settingsManager.getCachedDevices()
        if (cached != null) {
            viewModel.loadFromCache(cached)
        }

        // Then load server URL and fetch fresh data
        val savedUrl = settingsManager.serverUrl.first()
        if (savedUrl.isNotBlank()) {
            isDevMode = checkDevMode(savedUrl)
            viewModel.setServerUrl(savedUrl)
        } else {
            viewModel.markInitialized()
        }
        viewModel.startAutoRefresh()
    }

    // Clean up on dispose
    DisposableEffect(Unit) {
        onDispose {
            viewModel.stopAutoRefresh()
        }
    }

    // Determine start destination based on auth state
    val startDestination = remember(isAuthenticated, isDevMode) {
        // In dev mode (local/LAN), skip login
        // In prod mode, require auth
        if (isDevMode || isAuthenticated) "gates" else "login"
    }

    NavHost(
        navController = navController,
        startDestination = startDestination
    ) {
        composable("login") {
            LoginScreen(
                onLoginSuccess = {
                    isAuthenticated = true
                    navController.navigate("gates") {
                        popUpTo("login") { inclusive = true }
                    }
                }
            )
        }
        composable("gates") {
            GateScreen(
                uiState = uiState,
                onUnlock = { viewModel.unlockGate(it) },
                onHoldToday = { deviceId, endTime -> viewModel.holdGateOpen(deviceId, endTime) },
                onHoldForever = { viewModel.holdGateForever(it) },
                onStopHold = { viewModel.closeGate(it) },
                onRefresh = { viewModel.refresh() },
                onSettingsClick = { navController.navigate("settings") },
                onLogout = {
                    coroutineScope.launch {
                        authManager.signOut()
                        isAuthenticated = false
                        navController.navigate("login") {
                            popUpTo("gates") { inclusive = true }
                        }
                    }
                },
                serverUrl = uiState.serverUrl,
                userDisplayName = authManager.currentUser?.displayName,
                userEmail = authManager.currentUser?.email,
                userPhotoUrl = authManager.currentUser?.photoUrl?.toString(),
                isDevMode = isDevMode
            )
        }
        composable("settings") {
            SettingsScreen(
                currentServerUrl = uiState.serverUrl,
                onSave = { url ->
                    coroutineScope.launch {
                        settingsManager.saveServerUrl(url)
                        isDevMode = checkDevMode(url)
                        viewModel.setServerUrl(url)
                    }
                },
                onBack = { navController.popBackStack() },
                onSignOut = {
                    coroutineScope.launch {
                        authManager.signOut()
                        isAuthenticated = false
                        // Only navigate to login if not in dev mode
                        if (!isDevMode) {
                            navController.navigate("login") {
                                popUpTo("gates") { inclusive = true }
                            }
                        }
                    }
                },
                isAuthenticated = isAuthenticated,
                userEmail = authManager.currentUser?.email,
                isDevMode = isDevMode
            )
        }
    }
}
