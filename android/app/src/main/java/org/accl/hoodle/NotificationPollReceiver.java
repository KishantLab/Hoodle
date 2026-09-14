package org.accl.hoodle;

import android.app.AlarmManager;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;
import android.os.PowerManager;

import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class NotificationPollReceiver extends BroadcastReceiver {

    public static final String ACTION_POLL = "org.accl.hoodle.ACTION_POLL_NOTIFICATIONS";
    public static final String CHANNEL_ID = "hoodle_notifications_channel";
    private static final long POLL_INTERVAL_MS = 15 * 60 * 1000; // 15 minutes background periodic sync

    private static final ExecutorService executor = Executors.newSingleThreadExecutor();

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null) return;

        // Always reschedule next poll so background alarm repeats continuously even when app is closed
        scheduleRecurringAlarm(context);

        // Acquire WakeLock briefly for network fetch
        PowerManager pm = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
        PowerManager.WakeLock wakeLock = null;
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "HoodleLMS::NotificationPollWakeLock");
            wakeLock.acquire(30000); // 30s timeout max
        }

        final PowerManager.WakeLock finalWakeLock = wakeLock;
        final PendingResult pendingResult = goAsync();

        executor.execute(() -> {
            try {
                pollServerForNotifications(context);
            } finally {
                if (finalWakeLock != null && finalWakeLock.isHeld()) {
                    finalWakeLock.release();
                }
                pendingResult.finish();
            }
        });
    }

    public static void scheduleRecurringAlarm(Context context) {
        AlarmManager am = (AlarmManager) context.getSystemService(Context.ALARM_SERVICE);
        if (am == null) return;

        Intent intent = new Intent(context, NotificationPollReceiver.class);
        intent.setAction(ACTION_POLL);
        PendingIntent pi = PendingIntent.getBroadcast(
            context,
            1001,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        long triggerAtMillis = System.currentTimeMillis() + POLL_INTERVAL_MS;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAtMillis, pi);
        } else {
            am.setInexactRepeating(AlarmManager.RTC_WAKEUP, triggerAtMillis, POLL_INTERVAL_MS, pi);
        }
    }

    private void pollServerForNotifications(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(MainActivity.PREFS_NAME, Context.MODE_PRIVATE);
        String serverUrl = prefs.getString(MainActivity.KEY_SERVER_URL, MainActivity.URL_LAN);
        String token = prefs.getString(MainActivity.KEY_AUTH_TOKEN, "");
        String userId = prefs.getString(MainActivity.KEY_USER_ID, "");

        if (token.isEmpty() || userId.isEmpty()) {
            return;
        }

        try {
            String pollUrlStr = serverUrl;
            if (!pollUrlStr.endsWith("/")) pollUrlStr += "/";
            pollUrlStr += "api/notifications/poll?user_id=" + userId;

            URL url = new URL(pollUrlStr);
            HttpURLConnection conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("GET");
            conn.setRequestProperty("Authorization", "Bearer " + token);
            conn.setConnectTimeout(6000);
            conn.setReadTimeout(6000);

            int responseCode = conn.getResponseCode();
            if (responseCode == 200) {
                BufferedReader reader = new BufferedReader(new InputStreamReader(conn.getInputStream()));
                StringBuilder sb = new StringBuilder();
                String line;
                while ((line = reader.readLine()) != null) {
                    sb.append(line);
                }
                reader.close();

                JSONObject res = new JSONObject(sb.toString());
                if (res.has("notifications")) {
                    JSONArray arr = res.getJSONArray("notifications");
                    for (int i = 0; i < arr.length(); i++) {
                        JSONObject n = arr.getJSONObject(i);
                        String title = n.optString("title", "Hoodle Alert");
                        String body = n.optString("body", "");
                        postNotification(context, title, body);
                    }
                }

                int unreadTotal = res.optInt("unread_total", -1);
                if (unreadTotal == 0) {
                    NotificationManager nm = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
                    if (nm != null) {
                        nm.cancelAll();
                    }
                }
            }
            conn.disconnect();
        } catch (Exception e) {
            // Silently ignore connectivity timeouts in background sync
        }
    }

    private void postNotification(Context context, String title, String body) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(context, android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                return;
            }
        }

        ensureNotificationChannel(context);

        Intent openAppIntent = new Intent(context, MainActivity.class);
        openAppIntent.setFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pendingIntent = PendingIntent.getActivity(
            context,
            (int) System.currentTimeMillis(),
            openAppIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        Uri defaultSoundUri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION);
        NotificationCompat.Builder builder = new NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(new NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setSound(defaultSoundUri)
            .setAutoCancel(true)
            .setContentIntent(pendingIntent);

        NotificationManagerCompat nmc = NotificationManagerCompat.from(context);
        nmc.notify((int) System.currentTimeMillis(), builder.build());
    }

    private void ensureNotificationChannel(Context context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "Hoodle LMS Alerts",
                NotificationManager.IMPORTANCE_HIGH
            );
            channel.setDescription("Assignment, grading, attendance, and message notifications");
            channel.enableVibration(true);
            NotificationManager nm = context.getSystemService(NotificationManager.class);
            if (nm != null) {
                nm.createNotificationChannel(channel);
            }
        }
    }
}
