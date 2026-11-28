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
        versionCode = 4
        versionName = "0.4"
        // The S23 (and any recent phone) is 64-bit ARM; leaving out the other ABIs
        // keeps OpenCV's native library from bloating the APK.
        ndk {
            abiFilters += listOf("arm64-v8a")
        }
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
dependencies {
    // OpenCV's official Android package, the same version as the PC (opencv-python 5.1.0),
    // so markers and ChArUco boards are detected identically on both.
    implementation("org.opencv:opencv:5.1.0")
}
