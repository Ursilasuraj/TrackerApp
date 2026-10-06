package android.app;

import java.util.ArrayList;
import java.util.List;

public class NotificationManager {
    public final List<Notification> posted = new ArrayList<Notification>();
    public final List<Object> channels = new ArrayList<Object>();

    public void notify(int id, Notification n) { n.id = id; posted.add(n); }
    public void createNotificationChannel(NotificationChannel c) { channels.add(c); }   // API 26 (reflection)
}
