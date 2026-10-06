package android.app;

/** Remembers the one alarm the app keeps (or none). */
public class AlarmManager {
    public static final int RTC_WAKEUP = 0;
    public Long at;            // null: no alarm
    public boolean exact;
    public boolean allowExact = true;

    public void setExactAndAllowWhileIdle(int type, long when, PendingIntent pi) { at = when; exact = true; }
    public void setAndAllowWhileIdle(int type, long when, PendingIntent pi) { at = when; exact = false; }
    public void cancel(PendingIntent pi) { at = null; }
    public boolean canScheduleExactAlarms() { return allowExact; }   // API 31 (called by reflection)
}
