package io.github.ursilasuraj.todotracker;

import android.content.Context;
import android.webkit.JavascriptInterface;

/**
 * What the page may ask of the app, as window.TodoTrackerAndroid (web/app.js):
 * show reminders now, keep the upcoming ones for the alarm clock, and save a
 * file (Export JSON) where the user chooses. Only our own page is loaded in
 * the WebView; links to other sites open in the browser.
 */
final class Bridge {
    private final MainActivity activity;
    private final Context app;

    Bridge(MainActivity activity) {
        this.activity = activity;
        this.app = activity.getApplicationContext();
    }

    /** [{keys: [...], title, body}] - each shown unless all its items were shown already. */
    @JavascriptInterface
    public void showReminders(String json) {
        Reminders.showNow(app, json);
    }

    /** [{key, local: "YYYY-MM-DDTHH:MM", at: epoch ms, title, body}] - replaces the list. */
    @JavascriptInterface
    public void scheduleReminders(String json) {
        Reminders.schedule(app, json);
    }

    @JavascriptInterface
    public void saveFile(final String name, final String type, final String text) {
        activity.runOnUiThread(new Runnable() {
            @Override
            public void run() {
                activity.saveFile(name, type, text);
            }
        });
    }
}
