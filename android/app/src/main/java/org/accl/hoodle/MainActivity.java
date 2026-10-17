package org.accl.hoodle;

import android.annotation.SuppressLint;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Bitmap;
import android.location.Location;
import android.location.LocationManager;
import android.net.ConnectivityManager;
import com.google.android.material.button.MaterialButton;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.net.http.SslError;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.GeolocationPermissions;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.SslErrorHandler;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.activity.OnBackPressedCallback;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;
import androidx.core.location.LocationManagerCompat;
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout;

import org.json.JSONObject;

import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends AppCompatActivity {

    public static final String PREFS_NAME = "hoodle_lms_prefs";
    public static final String KEY_SERVER_URL = "server_url";
    public static final String KEY_AUTH_TOKEN = "auth_token";
    public static final String KEY_USER_ID = "user_id";
    public static final String KEY_USER_EMAIL = "user_email";
    public static final String KEY_IS_IIT_BHILAI = "is_iit_bhilai";

    public static final String URL_LAN = "https://10.10.14.104/lms/";
    public static final String URL_INTERNET = "https://hoodle.in/lms/";
    public static final String URL_HTTP = "http://10.10.14.104/lms/";
    public static final String URL_DIRECT = "http://10.10.14.104:8095/lms/";
    private static final String CHANNEL_ID = "hoodle_notifications_channel";
    private static final int REQUEST_LOCATION_PERMISSION_CODE = 103;

    private GeolocationPermissions.Callback pendingGeolocationCallback = null;
    private String pendingGeolocationOrigin = null;

    private WebView webView;
    private ProgressBar progressBar;
    private SwipeRefreshLayout swipeRefreshLayout;
    private LinearLayout offlineLayout;
    private TextView tvCurrentServer;
    private Button btnRetry;
    private Button btnSwitchLan;
    private MaterialButton btnSwitchInternet;
    private Button btnCustomServer;

    private ValueCallback<Uri[]> filePathCallback;
    private ActivityResultLauncher<Intent> fileChooserLauncher;

    private final ExecutorService backgroundExecutor = Executors.newSingleThreadExecutor();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        createNotificationChannel();

        // Request Notification permission for Android 13+ (Tiramisu)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 101);
            }
        }

        // Start background notification polling (every 15 min, even when app is closed)
        NotificationPollReceiver.scheduleRecurringAlarm(this);

        // Request Camera permission for QR Attendance Scanner
        if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{android.Manifest.permission.CAMERA}, 102);
        }

        // Request Location permissions for Attendance Verification & Geofences
        checkAndRequestLocationPermissions();

        // Initialize UI Elements
        webView = findViewById(R.id.webView);
        progressBar = findViewById(R.id.progressBar);
        swipeRefreshLayout = findViewById(R.id.swipeRefreshLayout);
        offlineLayout = findViewById(R.id.offlineLayout);
        tvCurrentServer = findViewById(R.id.tvCurrentServer);
        btnRetry = findViewById(R.id.btnRetry);
        btnSwitchLan = findViewById(R.id.btnSwitchLan);
        btnSwitchInternet = findViewById(R.id.btnSwitchInternet);
        btnCustomServer = findViewById(R.id.btnCustomServer);

        setupSwipeRefresh();
        setupWebView();
        setupBackButtonHandler();
        setupServerSwitchers();

        // Setup File Chooser Launcher (for assignments, reports, code uploads)
        fileChooserLauncher = registerForActivityResult(
            new ActivityResultContracts.StartActivityForResult(),
            result -> {
                if (filePathCallback != null) {
                    Uri[] results = null;
                    if (result.getResultCode() == RESULT_OK && result.getData() != null) {
                        String dataString = result.getData().getDataString();
                        if (dataString != null) {
                            results = new Uri[]{Uri.parse(dataString)};
                        } else if (result.getData().getClipData() != null) {
                            int count = result.getData().getClipData().getItemCount();
                            results = new Uri[count];
                            for (int i = 0; i < count; i++) {
                                results[i] = result.getData().getClipData().getItemAt(i).getUri();
                            }
                        }
                    }
                    filePathCallback.onReceiveValue(results);
                    filePathCallback = null;
                }
            }
        );

        // Immediately render the bundled static offline shell (0ms, instant response, no blank screen)
        webView.loadUrl("file:///android_asset/app_shell.html");

        // Schedule live portal loading in background
        mainHandler.postDelayed(this::loadPortalUrl, 350);
    }

    @Override
    protected void onResume() {
        super.onResume();
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) {
            nm.cancelAll();
        }
    }

    private void setupSwipeRefresh() {
        // Disable gesture pull-to-refresh to prevent accidental page reloads while scrolling
        swipeRefreshLayout.setEnabled(false);
    }

    public boolean isIitBhilaiUser() {
        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        return prefs.getBoolean(KEY_IS_IIT_BHILAI, false);
    }

    public void updateNetworkSwitchersVisibility() {
        boolean isIit = isIitBhilaiUser();
        if (btnSwitchLan != null) btnSwitchLan.setVisibility(isIit ? View.VISIBLE : View.GONE);
        if (btnCustomServer != null) btnCustomServer.setVisibility(isIit ? View.VISIBLE : View.GONE);
        if (btnSwitchInternet != null) btnSwitchInternet.setVisibility(isIit ? View.VISIBLE : View.GONE);
    }

    public void switchToInternet() {
        setSavedServerUrl(URL_INTERNET);
        Toast.makeText(this, "🌐 Switched to Internet (hoodle.in)", Toast.LENGTH_SHORT).show();
        loadPortalUrl();
    }

    public void switchToCampus() {
        if (!isIitBhilaiUser()) {
            Toast.makeText(this, "Campus Intranet is available only for @iitbhilai.ac.in accounts.", Toast.LENGTH_LONG).show();
            return;
        }
        setSavedServerUrl(URL_LAN);
        Toast.makeText(this, "🏫 Switched to Campus Wi-Fi (Intranet)", Toast.LENGTH_SHORT).show();
        loadPortalUrl();
    }

    public void toggleNetworkMode() {
        if (!isIitBhilaiUser()) {
            switchToInternet();
            return;
        }
        String current = getSavedServerUrl();
        if (current != null && (current.contains("hoodle.in") || current.contains("ts.net") || current.contains("100.87.0.15"))) {
            switchToCampus();
        } else {
            switchToInternet();
        }
    }

    private void setupServerSwitchers() {
        updateNetworkSwitchersVisibility();
        btnRetry.setOnClickListener(v -> loadPortalUrl());
        btnSwitchInternet.setOnClickListener(v -> switchToInternet());
        btnSwitchLan.setOnClickListener(v -> switchToCampus());
        btnCustomServer.setOnClickListener(v -> {
            if (isIitBhilaiUser()) {
                showCustomServerDialog();
            }
        });
    }

    private void showCustomServerDialog() {
        if (!isIitBhilaiUser()) return;
        AlertDialog.Builder builder = new AlertDialog.Builder(this);
        builder.setTitle("🌐 Custom Portal Address");
        builder.setMessage("Enter campus IP, reverse proxy URL, or domain:");

        final EditText input = new EditText(this);
        input.setHint("https://10.10.14.104/lms/");
        input.setText(getSavedServerUrl());
        builder.setView(input);

        builder.setPositiveButton("Save & Connect", (dialog, which) -> {
            String newUrl = input.getText().toString().trim();
            if (!newUrl.isEmpty()) {
                if (!newUrl.startsWith("http://") && !newUrl.startsWith("https://")) {
                    newUrl = "https://" + newUrl;
                }
                if (!newUrl.endsWith("/")) {
                    newUrl = newUrl + "/";
                }
                setSavedServerUrl(newUrl);
                loadPortalUrl();
            }
        });
        builder.setNegativeButton("Cancel", (dialog, which) -> dialog.cancel());
        builder.show();
    }

    private String getSavedServerUrl() {
        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        if (!isIitBhilaiUser()) {
            return URL_INTERNET;
        }
        String url = prefs.getString(KEY_SERVER_URL, URL_INTERNET);
        // Normalize obsolete/broken links
        if (url != null && (url.contains(":8095") || url.startsWith("http://10.10.14.104/lms/") || url.contains("ts.net"))) {
            url = URL_INTERNET;
            prefs.edit().putString(KEY_SERVER_URL, url).apply();
        }
        return url;
    }

    private void setSavedServerUrl(String url) {
        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        prefs.edit().putString(KEY_SERVER_URL, url).apply();
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void setupWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setAllowContentAccess(true);
        settings.setLoadsImagesAutomatically(true);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(true);
        settings.setSupportZoom(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);

        // Geolocation Settings for Attendance Geofencing
        settings.setGeolocationEnabled(true);
        try {
            settings.setGeolocationDatabasePath(getFilesDir().getPath());
        } catch (Exception ignored) {}

        // User Agent customization
        String defaultUA = settings.getUserAgentString();
        settings.setUserAgentString(defaultUA + " Hoodle_Android_App/1.0");

        // Enable Cookies (vital for Flask session cookie persistence)
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true);

        // Hardware Acceleration
        webView.setLayerType(View.LAYER_TYPE_HARDWARE, null);

        // Register Android JavaScript Interface
        webView.addJavascriptInterface(new WebAppInterface(this), "Android");

        // WebView Client
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                super.onPageStarted(view, url, favicon);
                progressBar.setVisibility(View.VISIBLE);
                offlineLayout.setVisibility(View.GONE);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                progressBar.setVisibility(View.GONE);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                super.onReceivedError(view, request, error);
                if (request.isForMainFrame()) {
                    showOfflineView();
                }
            }

            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                // Campus intranet server on 10.10.14.104 uses self-signed SSL for local HTTPS
                handler.proceed();
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();

                // Keep internal LMS routes inside the WebView
                if (url.contains("10.10.14.104") || url.contains("ts.net") || url.contains("/lms") || url.contains(":8095") || url.contains("hoodle")) {
                    return false; // Load inside app
                }

                // Open external links in system browser
                try {
                    Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
                    startActivity(intent);
                    return true;
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "Cannot open external link", Toast.LENGTH_SHORT).show();
                    return false;
                }
            }
        });

        // WebChromeClient for Camera permissions, Geolocation prompts, and File Uploads
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                // Grant camera access directly to the WebView HTML5 QR scanner
                request.grant(request.getResources());
            }

            @Override
            public void onGeolocationPermissionsShowPrompt(final String origin, final GeolocationPermissions.Callback callback) {
                if (hasLocationPermission()) {
                    if (!isDeviceLocationEnabled()) {
                        checkAndPromptDeviceGpsEnabled();
                    }
                    callback.invoke(origin, true, true);
                } else {
                    pendingGeolocationCallback = callback;
                    pendingGeolocationOrigin = origin;
                    ActivityCompat.requestPermissions(
                        MainActivity.this,
                        new String[]{
                            android.Manifest.permission.ACCESS_FINE_LOCATION,
                            android.Manifest.permission.ACCESS_COARSE_LOCATION
                        },
                        REQUEST_LOCATION_PERMISSION_CODE
                    );
                }
            }

            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progressBar.setProgress(newProgress);
                if (newProgress == 100) {
                    progressBar.setVisibility(View.GONE);
                }
            }

            @Override
            public boolean onShowFileChooser(WebView webView, ValueCallback<Uri[]> filePathCallback, FileChooserParams fileChooserParams) {
                if (MainActivity.this.filePathCallback != null) {
                    MainActivity.this.filePathCallback.onReceiveValue(null);
                }
                MainActivity.this.filePathCallback = filePathCallback;

                Intent intent = fileChooserParams.createIntent();
                try {
                    fileChooserLauncher.launch(intent);
                } catch (Exception e) {
                    MainActivity.this.filePathCallback = null;
                    Toast.makeText(MainActivity.this, "File picker error", Toast.LENGTH_SHORT).show();
                    return false;
                }
                return true;
            }
        });

        // Download Listener for course attachments, PDFs, templates, and exports
        webView.setDownloadListener((url, userAgent, contentDisposition, mimetype, contentLength) -> {
            if (url == null || url.isEmpty() || url.startsWith("blob:")) {
                return;
            }
            try {
                String filename = URLUtil.guessFileName(url, contentDisposition, mimetype);
                handleDownloadInApp(url, filename, mimetype);
            } catch (Exception e) {
                Toast.makeText(getApplicationContext(), "Download error: " + e.getMessage(), Toast.LENGTH_SHORT).show();
            }
        });
    }

    private void setupBackButtonHandler() {
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                if (webView.canGoBack()) {
                    webView.goBack();
                } else {
                    setEnabled(false);
                    getOnBackPressedDispatcher().onBackPressed();
                }
            }
        });
    }

    private void loadPortalUrl() {
        String url = getSavedServerUrl();
        if (tvCurrentServer != null) {
            tvCurrentServer.setText("Server: " + url);
        }

        if (!isNetworkAvailable()) {
            showOfflineView();
            return;
        }

        offlineLayout.setVisibility(View.GONE);
        webView.setVisibility(View.VISIBLE);
        webView.loadUrl(url);
    }

    private void showOfflineView() {
        webView.setVisibility(View.GONE);
        offlineLayout.setVisibility(View.VISIBLE);
        progressBar.setVisibility(View.GONE);
        if (tvCurrentServer != null) {
            tvCurrentServer.setText("Server: " + getSavedServerUrl());
        }
    }

    private boolean isNetworkAvailable() {
        ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (cm == null) return false;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            NetworkCapabilities capabilities = cm.getNetworkCapabilities(cm.getActiveNetwork());
            return capabilities != null && (
                capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
                capabilities.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) ||
                capabilities.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) ||
                capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
            );
        } else {
            android.net.NetworkInfo activeNetworkInfo = cm.getActiveNetworkInfo();
            return activeNetworkInfo != null && activeNetworkInfo.isConnected();
        }
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "Hoodle LMS Alerts",
                NotificationManager.IMPORTANCE_HIGH
            );
            channel.setDescription("Assignment, grading, attendance, and message notifications");
            channel.enableVibration(true);
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) {
                nm.createNotificationChannel(channel);
            }
        }
    }

    public void postStatusBarNotification(String title, String body) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                return;
            }
        }

        Intent openAppIntent = new Intent(this, MainActivity.class);
        openAppIntent.setFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pendingIntent = PendingIntent.getActivity(
            this,
            (int) System.currentTimeMillis(),
            openAppIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        NotificationCompat.Builder builder = new NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(new NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(pendingIntent);

        NotificationManagerCompat nmc = NotificationManagerCompat.from(this);
        nmc.notify((int) System.currentTimeMillis(), builder.build());
    }

    public void handleDownloadInApp(String rawUrl, String filename, String mimetype) {
        if (rawUrl == null || rawUrl.isEmpty() || rawUrl.startsWith("blob:")) {
            return;
        }

        String fullUrl = rawUrl;
        if (fullUrl.startsWith("/")) {
            String base = getSavedServerUrl();
            if (base.endsWith("/") && fullUrl.startsWith("/")) {
                fullUrl = base.substring(0, base.length() - 1) + fullUrl;
            } else {
                fullUrl = base + fullUrl;
            }
        }

        final String downloadUrl = fullUrl;
        final String finalFilename = (filename != null && !filename.isEmpty()) ? filename : "document.pdf";

        Toast.makeText(this, "⬇ Downloading " + finalFilename + "...", Toast.LENGTH_SHORT).show();

        backgroundExecutor.execute(() -> {
            try {
                URL url = new URL(downloadUrl);
                HttpURLConnection conn;
                if (url.getProtocol().equalsIgnoreCase("https")) {
                    javax.net.ssl.HttpsURLConnection httpsConn = (javax.net.ssl.HttpsURLConnection) url.openConnection();
                    javax.net.ssl.TrustManager[] trustAll = new javax.net.ssl.TrustManager[]{
                        new javax.net.ssl.X509TrustManager() {
                            public java.security.cert.X509Certificate[] getAcceptedIssuers() { return null; }
                            public void checkClientTrusted(java.security.cert.X509Certificate[] certs, String authType) {}
                            public void checkServerTrusted(java.security.cert.X509Certificate[] certs, String authType) {}
                        }
                    };
                    javax.net.ssl.SSLContext sc = javax.net.ssl.SSLContext.getInstance("TLS");
                    sc.init(null, trustAll, new java.security.SecureRandom());
                    httpsConn.setSSLSocketFactory(sc.getSocketFactory());
                    httpsConn.setHostnameVerifier((hostname, session) -> true);
                    conn = httpsConn;
                } else {
                    conn = (HttpURLConnection) url.openConnection();
                }

                conn.setRequestMethod("GET");
                String cookies = CookieManager.getInstance().getCookie(downloadUrl);
                if (cookies != null) {
                    conn.setRequestProperty("Cookie", cookies);
                }
                conn.setRequestProperty("User-Agent", "Hoodle_Android_App/1.0");
                conn.setConnectTimeout(10000);
                conn.setReadTimeout(30000);

                int code = conn.getResponseCode();
                if (code >= 200 && code < 400) {
                    java.io.File destDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
                    if (!destDir.exists()) destDir.mkdirs();
                    java.io.File destFile = new java.io.File(destDir, finalFilename);

                    try (java.io.InputStream in = conn.getInputStream();
                         java.io.FileOutputStream out = new java.io.FileOutputStream(destFile)) {
                        byte[] buffer = new byte[32768];
                        int read;
                        while ((read = in.read(buffer)) != -1) {
                            out.write(buffer, 0, read);
                        }
                    }

                    mainHandler.post(() -> {
                        Toast.makeText(MainActivity.this, "✅ Saved to Downloads: " + finalFilename, Toast.LENGTH_LONG).show();
                        postStatusBarNotification("Download Complete", finalFilename);
                    });
                } else {
                    mainHandler.post(() -> Toast.makeText(MainActivity.this, "Download failed (HTTP " + code + ")", Toast.LENGTH_SHORT).show());
                }
                conn.disconnect();
            } catch (Exception e) {
                mainHandler.post(() -> Toast.makeText(MainActivity.this, "Download error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        });
    }

    public boolean hasLocationPermission() {
        return ContextCompat.checkSelfPermission(this, android.Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED ||
               ContextCompat.checkSelfPermission(this, android.Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    public void checkAndRequestLocationPermissions() {
        if (!hasLocationPermission()) {
            ActivityCompat.requestPermissions(
                this,
                new String[]{
                    android.Manifest.permission.ACCESS_FINE_LOCATION,
                    android.Manifest.permission.ACCESS_COARSE_LOCATION
                },
                REQUEST_LOCATION_PERMISSION_CODE
            );
        } else {
            checkAndPromptDeviceGpsEnabled();
        }
    }

    public boolean isDeviceLocationEnabled() {
        LocationManager locationManager = (LocationManager) getSystemService(Context.LOCATION_SERVICE);
        if (locationManager == null) return false;
        return LocationManagerCompat.isLocationEnabled(locationManager);
    }

    public void checkAndPromptDeviceGpsEnabled() {
        if (!isDeviceLocationEnabled()) {
            new AlertDialog.Builder(this)
                .setTitle("📍 Turn On Location")
                .setMessage("Location is turned off on your device. Hoodle requires location services to auto-detect attendance venues and verify attendance.\n\nPlease turn on location in settings.")
                .setPositiveButton("Turn On", (dialog, which) -> {
                    try {
                        Intent intent = new Intent(Settings.ACTION_LOCATION_SOURCE_SETTINGS);
                        startActivity(intent);
                    } catch (Exception e) {
                        Toast.makeText(MainActivity.this, "Unable to open location settings", Toast.LENGTH_SHORT).show();
                    }
                })
                .setNegativeButton("Cancel", (dialog, which) -> dialog.dismiss())
                .show();
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions, @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQUEST_LOCATION_PERMISSION_CODE) {
            boolean granted = false;
            for (int res : grantResults) {
                if (res == PackageManager.PERMISSION_GRANTED) {
                    granted = true;
                    break;
                }
            }
            if (pendingGeolocationCallback != null && pendingGeolocationOrigin != null) {
                pendingGeolocationCallback.invoke(pendingGeolocationOrigin, granted, false);
                pendingGeolocationCallback = null;
                pendingGeolocationOrigin = null;
            }
            if (granted) {
                checkAndPromptDeviceGpsEnabled();
            } else {
                Toast.makeText(this, "Location permission is required for attendance verification", Toast.LENGTH_LONG).show();
            }
        }
    }

    public String getLatestNativeCoordinatesJson() {
        try {
            if (!hasLocationPermission()) {
                JSONObject err = new JSONObject();
                err.put("success", false);
                err.put("error", "PERMISSION_DENIED");
                err.put("message", "App location permission denied");
                return err.toString();
            }
            LocationManager lm = (LocationManager) getSystemService(Context.LOCATION_SERVICE);
            if (lm == null || !LocationManagerCompat.isLocationEnabled(lm)) {
                JSONObject err = new JSONObject();
                err.put("success", false);
                err.put("error", "LOCATION_DISABLED");
                err.put("message", "Device location is turned off");
                return err.toString();
            }
            Location bestLocation = null;
            for (String provider : lm.getProviders(true)) {
                @SuppressLint("MissingPermission")
                Location l = lm.getLastKnownLocation(provider);
                if (l != null) {
                    if (bestLocation == null || l.getAccuracy() < bestLocation.getAccuracy()) {
                        bestLocation = l;
                    }
                }
            }
            if (bestLocation != null) {
                JSONObject res = new JSONObject();
                res.put("success", true);
                res.put("latitude", bestLocation.getLatitude());
                res.put("longitude", bestLocation.getLongitude());
                res.put("accuracy", (double) bestLocation.getAccuracy());
                res.put("provider", bestLocation.getProvider());
                return res.toString();
            } else {
                JSONObject err = new JSONObject();
                err.put("success", false);
                err.put("error", "POSITION_UNAVAILABLE");
                err.put("message", "Waiting for GPS satellite fix...");
                return err.toString();
            }
        } catch (Exception e) {
            return "{\"success\": false, \"error\": \"" + e.getMessage() + "\"}";
        }
    }

    public static class WebAppInterface {
        private final MainActivity activity;

        WebAppInterface(MainActivity activity) {
            this.activity = activity;
        }

        @JavascriptInterface
        public void downloadUrl(String url, String filename) {
            activity.runOnUiThread(() -> activity.handleDownloadInApp(url, filename, null));
        }

        @JavascriptInterface
        public void clearNotifications() {
            activity.runOnUiThread(() -> {
                NotificationManager nm = (NotificationManager) activity.getSystemService(Context.NOTIFICATION_SERVICE);
                if (nm != null) {
                    nm.cancelAll();
                }
            });
        }

        @JavascriptInterface
        public void setAuthCredentials(String token, String userId) {
            if (token != null && userId != null) {
                SharedPreferences prefs = activity.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
                prefs.edit()
                    .putString(KEY_AUTH_TOKEN, token)
                    .putString(KEY_USER_ID, userId)
                    .apply();
            }
        }

        @JavascriptInterface
        public void switchNetworkMode(String mode) {
            activity.runOnUiThread(() -> {
                if ("internet".equalsIgnoreCase(mode)) {
                    activity.switchToInternet();
                } else {
                    activity.switchToCampus();
                }
            });
        }

        @JavascriptInterface
        public void retryConnection() {
            activity.runOnUiThread(activity::loadPortalUrl);
        }

        @JavascriptInterface
        public void setUserProfile(String email, String role) {
            if (email != null && !email.trim().isEmpty()) {
                boolean isIit = email.toLowerCase().endsWith("@iitbhilai.ac.in");
                SharedPreferences prefs = activity.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
                prefs.edit()
                    .putString(KEY_USER_EMAIL, email)
                    .putBoolean(KEY_IS_IIT_BHILAI, isIit)
                    .apply();
                activity.runOnUiThread(activity::updateNetworkSwitchersVisibility);
            }
        }

        @JavascriptInterface
        public String getActiveNetworkMode() {
            String current = activity.getSavedServerUrl();
            if (current != null && (current.contains("hoodle.in") || current.contains("ts.net") || current.contains("100.87.0.15"))) {
                return "internet";
            }
            return "intranet";
        }

        @JavascriptInterface
        public void switchToHttps() {
            activity.runOnUiThread(activity::switchToCampus);
        }

        @JavascriptInterface
        public boolean isLocationEnabled() {
            return activity.isDeviceLocationEnabled();
        }

        @JavascriptInterface
        public boolean hasLocationPermission() {
            return activity.hasLocationPermission();
        }

        @JavascriptInterface
        public void promptTurnOnLocation() {
            activity.runOnUiThread(activity::checkAndPromptDeviceGpsEnabled);
        }

        @JavascriptInterface
        public void requestLocationPermission() {
            activity.runOnUiThread(activity::checkAndRequestLocationPermissions);
        }

        @JavascriptInterface
        public String getNativeLocation() {
            return activity.getLatestNativeCoordinatesJson();
        }
    }
}
