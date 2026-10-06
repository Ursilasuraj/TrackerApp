package android.content;

import java.util.HashMap;
import java.util.Map;

/** Test stand-in: just what the app's reminder and asset code uses. */
public class Context {
    public static final int MODE_PRIVATE = 0;
    public static final String ALARM_SERVICE = "alarm";
    public static final String NOTIFICATION_SERVICE = "notification";
    public final Map<String, SharedPreferences> prefs = new HashMap<String, SharedPreferences>();
    public final android.app.AlarmManager alarms = new android.app.AlarmManager();
    public final android.app.NotificationManager notifications = new android.app.NotificationManager();

    public SharedPreferences getSharedPreferences(String name, int mode) {
        if (!prefs.containsKey(name)) prefs.put(name, new SharedPreferences());
        return prefs.get(name);
    }

    public Object getSystemService(String name) {
        return ALARM_SERVICE.equals(name) ? alarms : notifications;
    }

    public String getString(int id) {
        return "string" + id;
    }

    public Context getApplicationContext() {
        return this;
    }
}
