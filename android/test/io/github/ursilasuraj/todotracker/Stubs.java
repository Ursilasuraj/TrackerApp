package io.github.ursilasuraj.todotracker;

/* Stand-ins for the app's classes the tested code refers to (the real ones
   need a phone); R and BuildInfo are made by the build otherwise. */
final class R {
    static final class string {
        static final int channel_reminders = 1;
        static final int channel_reminders_about = 2;
    }

    static final class drawable {
        static final int ic_notification = 3;
    }
}

final class BuildInfo {
    static final int API = 5;
    static final String BUILD = "test-build";
}

class MainActivity {
}

class ReminderReceiver {
}
