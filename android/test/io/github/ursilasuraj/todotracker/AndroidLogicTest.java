package io.github.ursilasuraj.todotracker;

import android.app.Notification;
import android.content.Context;
import android.content.res.AssetManager;
import android.net.Uri;
import android.webkit.WebResourceResponse;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The app's reminder and asset logic on a plain JVM (tests/test_android.py
 * compiles it with test/fakes/ and Android's org.json, and runs it as
 * Android 7 and as Android 14). Exit code 1 if anything fails.
 */
public final class AndroidLogicTest {
    private static int failures;
    private static final long HOUR = 3600000L;

    public static void main(String[] args) throws Exception {
        reminders();
        assets(args[0]);
        System.out.println(failures == 0 ? "ALL OK" : failures + " FAILED");
        System.exit(failures == 0 ? 0 : 1);
    }

    private static void check(boolean ok, String what) {
        System.out.println((ok ? "ok   " : "FAIL ") + what);
        if (!ok) {
            failures++;
        }
    }

    private static String item(String key, long at, String title, String body) throws Exception {
        return new JSONObject().put("key", key).put("at", at).put("title", title).put("body", body).toString();
    }

    private static String list(String... items) {
        StringBuilder b = new StringBuilder("[");
        for (int i = 0; i < items.length; i++) {
            b.append(i > 0 ? "," : "").append(items[i]);
        }
        return b.append("]").toString();
    }

    private static String shown(String title, String body, String... keys) throws Exception {
        JSONArray k = new JSONArray();
        for (String key : keys) {
            k.put(key);
        }
        return new JSONObject().put("title", title).put("body", body).put("keys", k).toString();
    }

    private static int posted(Context c) {
        return c.notifications.posted.size();
    }

    private static Notification last(Context c) {
        return c.notifications.posted.get(c.notifications.posted.size() - 1);
    }

    static void reminders() throws Exception {
        int sdk = android.os.Build.VERSION.SDK_INT;
        System.out.println("-- reminders as Android API " + sdk);
        Context c = new Context();
        long now = System.currentTimeMillis();

        Reminders.schedule(c, list(item("t1@a", now + HOUR, "One", "Due today 10:00"), item("t2@b", now + 2 * HOUR, "Two", "Due today 11:00")));
        check(c.alarms.at != null && c.alarms.at == now + HOUR && c.alarms.exact, "the alarm is set, exactly, for the first reminder");

        Reminders.fire(c);
        check(posted(c) == 0 && c.alarms.at == now + HOUR, "an early alarm shows nothing and keeps the time");

        Reminders.schedule(c, list(item("t1@a", now - 1000, "One", "Due today 10:00"), item("t2@b", now + 2 * HOUR, "Two", "Due today 11:00")));
        Reminders.fire(c);
        check(posted(c) == 1 && "One".equals(last(c).title) && "Due today 10:00".equals(last(c).text), "a due reminder is shown");
        check(c.alarms.at == now + 2 * HOUR, "then the alarm moves to the next one");
        if (sdk >= 26) {
            check("reminders".equals(last(c).channel) && !c.notifications.channels.isEmpty(), "Android 8+: through the reminders channel");
        }

        Reminders.fire(c);
        Reminders.showNow(c, list(shown("One", "Due today 10:00", "t1@a")));
        check(posted(c) == 1, "each reminder is shown once (alarm first, then the page)");

        Reminders.showNow(c, list(shown("Two", "Due today 11:00", "t2@b")));
        check(posted(c) == 2 && "Two".equals(last(c).title), "the page shows one that is due before its alarm");
        check(c.alarms.at == null, "nothing left: no alarm");
        Reminders.fire(c);
        check(posted(c) == 2, "... and its alarm would not show it again");

        // A summary from the page covers several items, if any of them is new.
        Reminders.showNow(c, list(shown("5 TodoTracker items are due", "x", "t2@b", "t3@c", "t4@d")));
        check(posted(c) == 3, "a summary with something new is shown");
        Reminders.schedule(c, list(item("t3@c", now - 5, "Three", "b"), item("t9@z", now + HOUR, "Nine", "b")));
        Reminders.fire(c);
        check(posted(c) == 3 && c.alarms.at == now + HOUR, "items of a shown summary are not shown again");

        // More than three at once: one summary (as reminders.py).
        Context m = new Context();
        String[] seven = new String[7];
        for (int i = 0; i < 7; i++) {
            seven[i] = item("s" + i + "@x", now - 1000, "Item " + i, "b");
        }
        Reminders.schedule(m, list(seven));
        Reminders.fire(m);
        check(posted(m) == 1 && "7 TodoTracker items are due".equals(last(m).title)
                && "Item 0 · Item 1 · Item 2 · Item 3 · Item 4 … and 2 more".equals(last(m).text), "more than three become one summary");
        Context three = new Context();
        Reminders.schedule(three, list(item("a@1", now - 9, "A", "x"), item("b@1", now - 9, "B", "y"), item("c@1", now - 9, "C", "z")));
        Reminders.fire(three);
        check(posted(three) == 3, "three at once are shown one by one");

        // The local minute wins over the page's epoch time (a new time zone).
        Context z = new Context();
        long local = now + 3 * HOUR;
        String minute = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm", Locale.US).format(new Date(local));
        Reminders.schedule(z, "[" + new JSONObject().put("key", "t5@q").put("at", now + 9 * HOUR).put("local", minute)
                .put("title", "Five").put("body", "b").toString() + "]");
        long expected = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm", Locale.US).parse(minute).getTime();
        check(z.alarms.at == expected, "the alarm follows the local time of day");

        // Android 12+ may refuse exact alarms: then within minutes.
        Context x = new Context();
        x.alarms.allowExact = false;
        Reminders.schedule(x, list(item("t6@q", now + HOUR, "Six", "b")));
        check(x.alarms.at == now + HOUR && x.alarms.exact == (sdk < 31), "no exact alarms allowed: an inexact one (Android 12+)");

        // Old shown keys are forgotten after 90 days; bad input changes nothing.
        Context o = new Context();
        o.getSharedPreferences("reminders", 0).edit().putString("shown",
                new JSONObject().put("old@1", now - 100L * 24 * HOUR).put("new@1", now).toString()).apply();
        Reminders.showNow(o, "[]");
        JSONObject kept = new JSONObject(o.getSharedPreferences("reminders", 0).getString("shown", "{}"));
        check(!kept.has("old@1") && kept.has("new@1"), "shown keys older than 90 days are dropped");
        Reminders.schedule(o, "not json");
        Reminders.showNow(o, "{\"also\": \"bad\"}");
        check(posted(o) == 0, "bad input from the page is ignored");
    }

    static String body(WebResourceResponse r) throws Exception {
        InputStream in = r.data;
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) {
            out.write(buf, 0, n);
        }
        return out.toString("UTF-8");
    }

    static void assets(String root) throws Exception {
        System.out.println("-- assets");
        AssetServer s = new AssetServer(new AssetManager(root));
        WebResourceResponse r = s.serve(Uri.parse("https://appassets.androidplatform.net/"));
        String html = body(r);
        check(r.status == 200 && "text/html".equals(r.mime) && "UTF-8".equals(r.encoding), "index.html is served");
        check(html.contains("data-mode=\"local\"") && html.contains("data-api=\"5\"") && html.contains("data-build=\"test-build\"")
                && !html.contains("__"), "with mode local, the API and the build id");
        Matcher csp = Pattern.compile("'nonce-([A-Za-z0-9_-]{16,})'").matcher(r.headers.get("Content-Security-Policy"));
        check(csp.find() && html.contains("nonce=\"" + csp.group(1) + "\""), "the script nonce matches the CSP");
        String again = body(s.serve(Uri.parse("https://appassets.androidplatform.net/index.html")));
        check(!again.contains("nonce=\"" + csp.group(1) + "\""), "a fresh nonce every time");
        WebResourceResponse js = s.serve(Uri.parse("https://appassets.androidplatform.net/app.js?b=1"));
        check(js.status == 200 && "text/javascript".equals(js.mime) && body(js).contains("TodoTracker"), "app files are served");
        for (String bad : new String[]{"/nope.js", "/../secret.js", "/%2e%2e/x.js", "/images/abc.png", "/api/tasks", "/web/app.js", "/app.php"}) {
            check(s.serve(Uri.parse("https://appassets.androidplatform.net" + bad)).status == 404, "404 for " + bad);
        }
        check(s.serve(Uri.parse("https://example.com/app.js")) == null && s.serve(Uri.parse("http://appassets.androidplatform.net/")) == null,
                "other sites (and plain http) are left to the WebView");
    }
}
