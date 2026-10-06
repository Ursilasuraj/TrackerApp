package io.github.ursilasuraj.todotracker;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** The alarm clock went off for the next reminder. */
public final class ReminderReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        Reminders.fire(context.getApplicationContext());
    }
}
