package com.unifi.gate.api

import com.unifi.gate.model.ActionResponse
import com.unifi.gate.model.DebugData
import com.unifi.gate.model.DevicesResponse
import com.unifi.gate.model.DeviceStatus
import com.unifi.gate.model.EventLogResponse
import com.unifi.gate.model.HoldTodayRequest
import com.unifi.gate.model.SiteConfig
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query

interface UniFiGateApi {

    @GET("/config")
    suspend fun getConfig(): SiteConfig

    @GET("/devices")
    suspend fun getDevices(): DevicesResponse

    @GET("/status/{deviceId}")
    suspend fun getDeviceStatus(@Path("deviceId") deviceId: String): DeviceStatus

    @POST("/unlock/{deviceId}")
    suspend fun unlockGate(@Path("deviceId") deviceId: String): ActionResponse

    @POST("/hold/today/{deviceId}")
    suspend fun holdGateOpen(
        @Path("deviceId") deviceId: String,
        @retrofit2.http.Body body: HoldTodayRequest? = null
    ): ActionResponse

    @POST("/hold/forever/{deviceId}")
    suspend fun holdGateForever(@Path("deviceId") deviceId: String): ActionResponse

    @POST("/hold/stop/{deviceId}")
    suspend fun closeGate(@Path("deviceId") deviceId: String): ActionResponse

    @POST("/force-sync/{deviceId}")
    suspend fun forceSync(@Path("deviceId") deviceId: String): ActionResponse

    @GET("/events")
    suspend fun getEvents(
        @Query("limit") limit: Int = 50,
        @Query("offset") offset: Int = 0
    ): EventLogResponse

    @GET("/debug/{deviceId}")
    suspend fun getDebugInfo(@Path("deviceId") deviceId: String): DebugData
}
