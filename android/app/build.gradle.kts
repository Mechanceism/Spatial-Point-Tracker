plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "io.github.mechanceism.sptcamera"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.mechanceism.sptcamera"
        minSdk = 31
        targetSdk = 35
        versionCode = 2
        versionName = "0.2"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}
// No dependencies: platform APIs only (Camera2, PowerManager, org.json).
