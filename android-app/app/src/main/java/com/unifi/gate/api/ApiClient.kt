package com.unifi.gate.api

import kotlinx.coroutines.runBlocking
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.security.SecureRandom
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager

object ApiClient {
    private var retrofit: Retrofit? = null
    private var currentBaseUrl: String? = null
    private var tokenProvider: (suspend () -> String?)? = null

    /**
     * Set the token provider function.
     * This will be called for each request to get the current auth token.
     */
    fun setTokenProvider(provider: suspend () -> String?) {
        tokenProvider = provider
        // Force rebuild of client with new token provider
        retrofit = null
    }

    fun getApi(baseUrl: String): UniFiGateApi {
        val normalizedUrl = normalizeUrl(baseUrl)

        if (retrofit == null || currentBaseUrl != normalizedUrl) {
            currentBaseUrl = normalizedUrl

            val loggingInterceptor = HttpLoggingInterceptor().apply {
                level = HttpLoggingInterceptor.Level.BODY
            }

            val okHttpClientBuilder = getUnsafeOkHttpClient()
                .addInterceptor(loggingInterceptor)
                .addInterceptor(AuthInterceptor(tokenProvider))
                .connectTimeout(30, TimeUnit.SECONDS)
                .readTimeout(30, TimeUnit.SECONDS)
                .writeTimeout(30, TimeUnit.SECONDS)

            retrofit = Retrofit.Builder()
                .baseUrl(normalizedUrl)
                .client(okHttpClientBuilder.build())
                .addConverterFactory(GsonConverterFactory.create())
                .build()
        }

        return retrofit!!.create(UniFiGateApi::class.java)
    }

    /**
     * Interceptor that adds Authorization header with Bearer token.
     */
    private class AuthInterceptor(
        private val tokenProvider: (suspend () -> String?)?
    ) : Interceptor {
        override fun intercept(chain: Interceptor.Chain): Response {
            val originalRequest = chain.request()

            // Get token (blocking is OK here since OkHttp runs on IO thread)
            val token = tokenProvider?.let {
                runBlocking { it() }
            }

            val request = if (token != null) {
                originalRequest.newBuilder()
                    .header("Authorization", "Bearer $token")
                    .build()
            } else {
                originalRequest
            }

            return chain.proceed(request)
        }
    }

    // Trust all certificates for development
    private fun getUnsafeOkHttpClient(): OkHttpClient.Builder {
        val trustAllCerts = arrayOf<TrustManager>(object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
            override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
            override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
        })

        val sslContext = SSLContext.getInstance("SSL")
        sslContext.init(null, trustAllCerts, SecureRandom())
        val sslSocketFactory = sslContext.socketFactory

        return OkHttpClient.Builder()
            .sslSocketFactory(sslSocketFactory, trustAllCerts[0] as X509TrustManager)
            .hostnameVerifier { _, _ -> true }
    }

    private fun normalizeUrl(url: String): String {
        var normalized = url.trim()
        if (!normalized.startsWith("http://") && !normalized.startsWith("https://")) {
            normalized = "http://$normalized"
        }
        if (!normalized.endsWith("/")) {
            normalized = "$normalized/"
        }
        return normalized
    }

    fun clearClient() {
        retrofit = null
        currentBaseUrl = null
    }
}
