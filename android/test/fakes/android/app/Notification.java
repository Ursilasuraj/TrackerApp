package android.app;

import android.content.Context;

public class Notification {
    public static final String CATEGORY_REMINDER = "reminder";
    public static final int PRIORITY_HIGH = 1;
    public static final int DEFAULT_ALL = -1;
    public int id;
    public String title;
    public String text;
    public String channel;

    public static class Style { }

    public static class BigTextStyle extends Style {
        public BigTextStyle bigText(CharSequence s) { return this; }
    }

    public static class Builder {
        private final Notification n = new Notification();
        public Builder(Context c) { }
        public Builder setSmallIcon(int i) { return this; }
        public Builder setContentTitle(CharSequence s) { n.title = String.valueOf(s); return this; }
        public Builder setContentText(CharSequence s) { n.text = String.valueOf(s); return this; }
        public Builder setStyle(Style s) { return this; }
        public Builder setContentIntent(PendingIntent p) { return this; }
        public Builder setAutoCancel(boolean b) { return this; }
        public Builder setShowWhen(boolean b) { return this; }
        public Builder setWhen(long w) { return this; }
        public Builder setCategory(String c) { return this; }
        public Builder setPriority(int p) { return this; }
        public Builder setDefaults(int d) { return this; }
        public Builder setColor(int c) { return this; }
        public Builder setChannelId(String id) { n.channel = id; return this; }   // API 26 (reflection)
        public Notification build() { return n; }
    }
}
