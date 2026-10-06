package io.github.ursilasuraj.todotracker;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.webkit.ConsoleMessage;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * TodoTracker on Android: the same web app as on the PC, in a WebView, with
 * its data kept on the phone (web/localdb.js on IndexedDB). This activity
 * serves the app's files, lets the page pick and save files, passes the Back
 * button to the page, and asks once for permission to show reminders.
 */
public final class MainActivity extends Activity {
    private static final String TAG = "TodoTracker";
    private static final int PICK_FILES = 1;
    private static final int SAVE_FILE = 2;
    private static final int ASK_NOTIFICATIONS = 3;
    private static final int MIN_WEBVIEW = 111;     // color-mix() in the style sheet

    private WebView web;
    private AssetServer assets;
    private ValueCallback<Uri[]> fileCallback;
    private String pendingSave;
    private String pendingSaveName;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        assets = new AssetServer(getAssets());
        Reminders.createChannel(this);

        if ((getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0) {
            WebView.setWebContentsDebuggingEnabled(true);
        }
        web = new WebView(this);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);           // localStorage (theme, view)
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSupportMultipleWindows(false);
        s.setMediaPlaybackRequiresUserGesture(true);
        // Follow the phone's font size setting.
        s.setTextZoom(Math.round(getResources().getConfiguration().fontScale * 100));

        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                return assets.serve(request.getUrl());
            }

            // (Android 7+ calls this for every navigation through the newer
            // WebResourceRequest variant's default implementation.)
            @Override
            @SuppressWarnings("deprecation")
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                Uri uri = Uri.parse(url);
                if (AssetServer.isOwn(uri)) {
                    return false;
                }
                // Links in descriptions open in the browser, never inside the app.
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE));
                } catch (ActivityNotFoundException e) {
                    Toast.makeText(MainActivity.this, "No app can open " + uri, Toast.LENGTH_SHORT).show();
                }
                return true;
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                return pickFiles(callback, params);
            }

            @Override
            public boolean onConsoleMessage(ConsoleMessage m) {
                Log.i(TAG, m.message() + " (" + m.sourceId() + ":" + m.lineNumber() + ")");
                return true;
            }
        });
        web.addJavascriptInterface(new Bridge(this), "TodoTrackerAndroid");
        web.loadUrl(AssetServer.START);

        askForNotifications();
        checkWebView();
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        // The page closes what is open (picture, details, drawer, matrix); when
        // nothing is left the app goes to the background, as a home screen app.
        web.evaluateJavascript("(window.TT && TT.back) ? TT.back() : false", new ValueCallback<String>() {
            @Override
            public void onReceiveValue(String handled) {
                if (!"true".equals(handled)) {
                    moveTaskToBack(true);
                }
            }
        });
    }

    @Override
    protected void onPause() {
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
        }
        super.onDestroy();
    }

    // -- files -----------------------------------------------------------------

    private boolean pickFiles(ValueCallback<Uri[]> callback, WebChromeClient.FileChooserParams params) {
        if (fileCallback != null) {
            fileCallback.onReceiveValue(null);
        }
        fileCallback = callback;
        boolean images = params.getAcceptTypes().length > 0;
        for (String t : params.getAcceptTypes()) {
            if (t == null || !t.startsWith("image/")) {
                images = false;
            }
        }
        Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        // JSON exports have no reliable type on phones: offer every file.
        intent.setType(images ? "image/*" : "*/*");
        if (params.getMode() == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE) {
            intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        }
        try {
            startActivityForResult(Intent.createChooser(intent, images ? "Add pictures" : "Choose a file"), PICK_FILES);
            return true;
        } catch (ActivityNotFoundException e) {
            fileCallback = null;
            Toast.makeText(this, "No app on this phone can pick files.", Toast.LENGTH_LONG).show();
            return false;
        }
    }

    /** Export JSON: the user picks where to save it (Downloads, Drive, ...). */
    void saveFile(String name, String type, String text) {
        pendingSave = text;
        pendingSaveName = name;
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType(type == null || type.isEmpty() ? "application/json" : type);
        intent.putExtra(Intent.EXTRA_TITLE, name);
        try {
            startActivityForResult(intent, SAVE_FILE);
        } catch (ActivityNotFoundException e) {
            pendingSave = null;
            Toast.makeText(this, "No app on this phone can save files.", Toast.LENGTH_LONG).show();
        }
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == PICK_FILES) {
            ValueCallback<Uri[]> cb = fileCallback;
            fileCallback = null;
            if (cb == null) {
                return;
            }
            List<Uri> uris = new ArrayList<Uri>();
            if (result == RESULT_OK && data != null) {
                ClipData clip = data.getClipData();
                if (clip != null) {
                    for (int i = 0; i < clip.getItemCount(); i++) {
                        uris.add(clip.getItemAt(i).getUri());
                    }
                } else if (data.getData() != null) {
                    uris.add(data.getData());
                }
            }
            cb.onReceiveValue(uris.isEmpty() ? null : uris.toArray(new Uri[0]));
        } else if (request == SAVE_FILE) {
            String text = pendingSave;
            pendingSave = null;
            if (result != RESULT_OK || data == null || data.getData() == null || text == null) {
                return;
            }
            try {
                OutputStream out = getContentResolver().openOutputStream(data.getData(), "wt");
                try {
                    out.write(text.getBytes("UTF-8"));
                } finally {
                    out.close();
                }
                Toast.makeText(this, "Saved " + pendingSaveName, Toast.LENGTH_SHORT).show();
            } catch (Exception e) {
                Log.w(TAG, "save failed", e);
                Toast.makeText(this, "Could not save the file: " + e.getMessage(), Toast.LENGTH_LONG).show();
            }
        }
    }

    // -- permissions and checks ------------------------------------------------

    private void askForNotifications() {
        if (Build.VERSION.SDK_INT < 33) {
            return;                              // allowed until Android 13
        }
        String permission = "android.permission.POST_NOTIFICATIONS";
        SharedPreferences p = getSharedPreferences("app", MODE_PRIVATE);
        if (checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED || p.getBoolean("asked_notifications", false)) {
            return;
        }
        p.edit().putBoolean("asked_notifications", true).apply();
        requestPermissions(new String[]{permission}, ASK_NOTIFICATIONS);
    }

    /** The page needs a recent WebView (updated through the Play Store). */
    private void checkWebView() {
        String agent;
        try {
            agent = WebSettings.getDefaultUserAgent(this);
        } catch (Exception e) {
            return;
        }
        Matcher m = Pattern.compile("Chrome/(\\d+)").matcher(agent);
        if (!m.find() || Integer.parseInt(m.group(1)) >= MIN_WEBVIEW) {
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle("Please update Android System WebView")
                .setMessage("TodoTracker shows its pages with Android System WebView. This phone has version "
                        + m.group(1) + "; TodoTracker needs " + MIN_WEBVIEW + " or newer. Update \"Android System WebView\" "
                        + "(or Chrome) in the Play Store; until then some colours may be missing.")
                .setPositiveButton(android.R.string.ok, null)
                .show();
    }
}
