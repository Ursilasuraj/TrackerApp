package io.github.ursilasuraj.todotracker;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.text.ParseException;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;

/**
 * Reminders on the phone. The page sends the upcoming ones (scheduleReminders)
 * and those due while it is open (showReminders); an exact alarm wakes
 * ReminderReceiver for the next one while the app is closed. Every item is
 * shown once, whoever gets there first: shown keys ("t12@2026-10-07T09:00")
 * are remembered for 90 days.
 */
final class Reminders {
    static final String CHANNEL = "reminders";
    private static final String TAG = "TodoTracker";
    private static final String PREFS = "reminders";
    private static final long KEEP_SHOWN_MS = 90L * 24 * 3600 * 1000;
    private static final int MAX_SINGLE = 3;          // more at once become one summary (as reminders.py)

    private Reminders() {
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static JSONObject shown(Context ctx) {
        try {
            return new JSONObject(prefs(ctx).getString("shown", "{}"));
        } catch (JSONException e) {
            return new JSONObject();
        }
    }

    private static void saveShown(Context ctx, JSONObject shown) {
        long cutoff = System.currentTimeMillis() - KEEP_SHOWN_MS;
        List<String> old = new ArrayList<String>();
        Iterator<String> keys = shown.keys();
        while (keys.hasNext()) {
            String k = keys.next();
            if (shown.optLong(k, 0) < cutoff) {
                old.add(k);
            }
        }
        for (String k : old) {
            shown.remove(k);
        }
        prefs(ctx).edit().putString("shown", shown.toString()).apply();
    }

    private static JSONArray upcoming(Context ctx) {
        try {
            return new JSONArray(prefs(ctx).getString("upcoming", "[]"));
        } catch (JSONException e) {
            return new JSONArray();
        }
    }

    /** The minute an item is due, in the phone's current time zone (else the page's epoch time). */
    private static long dueAt(JSONObject item) {
        String local = item.optString("local", "");
        if (local.length() >= 16) {
            SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm", Locale.US);
            f.setLenient(false);
            try {
                return f.parse(local.substring(0, 16)).getTime();
            } catch (ParseException e) {
                // use "at"
            }
        }
        return item.optLong("at", Long.MAX_VALUE);
    }

    /** Due while the page is open: show those not shown yet. */
    static synchronized void showNow(Context ctx, String json) {
        try {
            JSONArray items = new JSONArray(json);
            JSONObject shown = shown(ctx);
            long now = System.currentTimeMillis();
            for (int i = 0; i < items.length(); i++) {
                JSONObject item = items.getJSONObject(i);
                JSONArray keys = item.optJSONArray("keys");
                boolean fresh = keys == null || keys.length() == 0;
                for (int k = 0; keys != null && k < keys.length(); k++) {
                    if (!shown.has(keys.getString(k))) {
                        fresh = true;
                    }
                }
                if (!fresh) {
                    continue;
                }
                for (int k = 0; keys != null && k < keys.length(); k++) {
                    shown.put(keys.getString(k), now);
                }
                post(ctx, item.optString("title"), item.optString("body"),
                        keys != null && keys.length() > 0 ? keys.getString(0) : item.optString("title"));
            }
            saveShown(ctx, shown);
        } catch (JSONException e) {
            Log.w(TAG, "bad reminders from the page", e);
        }
        arm(ctx);
    }

    /** The page's list of reminders still to come (replaces the last one). */
    static synchronized void schedule(Context ctx, String json) {
        try {
            new JSONArray(json);
        } catch (JSONException e) {
            Log.w(TAG, "bad schedule from the page", e);
            return;
        }
        prefs(ctx).edit().putString("upcoming", json).apply();
        arm(ctx);
    }

    /** The alarm went off (or the phone restarted): show what is due, then arm the next. */
    static synchronized void fire(Context ctx) {
        JSONArray items = upcoming(ctx);
        JSONObject shown = shown(ctx);
        long now = System.currentTimeMillis();
        List<JSONObject> due = new ArrayList<JSONObject>();
        for (int i = 0; i < items.length(); i++) {
            JSONObject item = items.optJSONObject(i);
            if (item == null || shown.has(item.optString("key"))) {
                continue;
            }
            if (dueAt(item) <= now + 30000) {
                due.add(item);
            }
        }
        try {
            for (JSONObject item : due) {
                shown.put(item.optString("key"), now);
            }
        } catch (JSONException e) {
            Log.w(TAG, "could not remember shown reminders", e);
        }
        saveShown(ctx, shown);
        if (due.size() > MAX_SINGLE) {
            StringBuilder body = new StringBuilder();
            for (int i = 0; i < Math.min(5, due.size()); i++) {
                if (i > 0) {
                    body.append(" · ");
                }
                body.append(due.get(i).optString("title"));
            }
            if (due.size() > 5) {
                body.append(" … and ").append(due.size() - 5).append(" more");
            }
            post(ctx, due.size() + " TodoTracker items are due", body.toString(), "summary@" + now);
        } else {
            for (JSONObject item : due) {
                post(ctx, item.optString("title"), item.optString("body"), item.optString("key"));
            }
        }
        arm(ctx);
    }

    /** Set the alarm clock for the earliest reminder not shown yet. */
    static synchronized void arm(Context ctx) {
        JSONArray items = upcoming(ctx);
        JSONObject shown = shown(ctx);
        long next = Long.MAX_VALUE;
        for (int i = 0; i < items.length(); i++) {
            JSONObject item = items.optJSONObject(i);
            if (item != null && !shown.has(item.optString("key"))) {
                next = Math.min(next, dueAt(item));
            }
        }
        AlarmManager am = (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        PendingIntent pi = PendingIntent.getBroadcast(ctx, 0, new Intent(ctx, ReminderReceiver.class),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        if (next == Long.MAX_VALUE) {
            am.cancel(pi);
            return;
        }
        next = Math.max(next, System.currentTimeMillis() + 1000);
        if (canScheduleExact(am)) {
            am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, next, pi);
        } else {
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, next, pi);   // within minutes instead
        }
    }

    private static boolean canScheduleExact(AlarmManager am) {
        if (Build.VERSION.SDK_INT < 31) {
            return true;
        }
        try {
            return (Boolean) AlarmManager.class.getMethod("canScheduleExactAlarms").invoke(am);
        } catch (Exception e) {
            return false;
        }
    }

    /** Android 8+ shows notifications only through a channel (API 26, by reflection). */
    static void createChannel(Context ctx) {
        if (Build.VERSION.SDK_INT < 26) {
            return;
        }
        try {
            NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
            Class<?> cls = Class.forName("android.app.NotificationChannel");
            Object channel = cls.getConstructor(String.class, CharSequence.class, int.class)
                    .newInstance(CHANNEL, ctx.getString(R.string.channel_reminders), 4 /* IMPORTANCE_HIGH */);
            cls.getMethod("setDescription", String.class).invoke(channel, ctx.getString(R.string.channel_reminders_about));
            NotificationManager.class.getMethod("createNotificationChannel", cls).invoke(nm, channel);
        } catch (Exception e) {
            Log.w(TAG, "could not create the notification channel", e);
        }
    }

    @SuppressWarnings("deprecation")
    private static void post(Context ctx, String title, String body, String key) {
        createChannel(ctx);
        Intent open = new Intent(ctx, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(ctx, 0, open, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = new Notification.Builder(ctx)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setShowWhen(true)
                .setWhen(System.currentTimeMillis())
                .setCategory(Notification.CATEGORY_REMINDER)
                .setPriority(Notification.PRIORITY_HIGH)
                .setDefaults(Notification.DEFAULT_ALL)
                .setColor(0xff2563eb);
        if (Build.VERSION.SDK_INT >= 26) {
            try {
                Notification.Builder.class.getMethod("setChannelId", String.class).invoke(b, CHANNEL);
            } catch (Exception e) {
                Log.w(TAG, "setChannelId", e);
            }
        }
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        try {
            nm.notify(key.hashCode(), b.build());
        } catch (SecurityException e) {
            Log.w(TAG, "notifications are not allowed", e);
        }
    }
}
