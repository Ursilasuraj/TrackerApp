package io.github.ursilasuraj.todotracker;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * Alarms do not survive a restart (or an app update), and a new clock or
 * time zone moves them: show what came due meanwhile and set the next one.
 */
public final class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? null : intent.getAction();
        if (Intent.ACTION_BOOT_COMPLETED.equals(action) || Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)
                || Intent.ACTION_TIME_CHANGED.equals(action) || Intent.ACTION_TIMEZONE_CHANGED.equals(action)) {
            Reminders.fire(context.getApplicationContext());
        }
    }
}
