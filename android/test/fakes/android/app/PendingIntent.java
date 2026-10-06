package android.app;

import android.content.Context;
import android.content.Intent;

public class PendingIntent {
    public static final int FLAG_UPDATE_CURRENT = 0x08000000;
    public static final int FLAG_IMMUTABLE = 0x04000000;
    public final Intent intent;

    PendingIntent(Intent intent) { this.intent = intent; }

    public static PendingIntent getBroadcast(Context c, int code, Intent i, int flags) { return new PendingIntent(i); }
    public static PendingIntent getActivity(Context c, int code, Intent i, int flags) { return new PendingIntent(i); }
}
