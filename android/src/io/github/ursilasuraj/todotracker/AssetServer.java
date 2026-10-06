package io.github.ursilasuraj.todotracker;

import android.content.res.AssetManager;
import android.net.Uri;
import android.util.Base64;
import android.webkit.WebResourceResponse;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.security.SecureRandom;
import java.util.HashMap;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Serves the web app (assets/web/) at https://appassets.androidplatform.net/,
 * the host Android reserves for an app's own assets, so the page is a secure
 * origin of its own. Same rules as LocalPageServer in tests/helpers.py:
 * index.html with mode "local", the API version, the build id and a fresh
 * nonce for its Content-Security-Policy; the other files as they are;
 * nothing else exists (there is no API: the page keeps the data itself).
 */
final class AssetServer {
    static final String HOST = "appassets.androidplatform.net";
    static final String START = "https://" + HOST + "/";

    private static final Pattern NAME = Pattern.compile("^/([A-Za-z0-9_-]+\\.(html|js|css|svg|png|ico|webmanifest))$");
    private static final Map<String, String> TYPES = new HashMap<String, String>();

    static {
        TYPES.put("html", "text/html");
        TYPES.put("js", "text/javascript");
        TYPES.put("css", "text/css");
        TYPES.put("svg", "image/svg+xml");
        TYPES.put("png", "image/png");
        TYPES.put("ico", "image/x-icon");
        TYPES.put("webmanifest", "application/manifest+json");
    }

    private final AssetManager assets;
    private final SecureRandom random = new SecureRandom();

    AssetServer(AssetManager assets) {
        this.assets = assets;
    }

    static boolean isOwn(Uri uri) {
        return uri != null && "https".equals(uri.getScheme()) && HOST.equals(uri.getHost());
    }

    /** The answer for a request, or null to let the WebView load it (other hosts). */
    WebResourceResponse serve(Uri uri) {
        if (!isOwn(uri)) {
            return null;
        }
        String path = uri.getPath() == null || uri.getPath().isEmpty() ? "/" : uri.getPath();
        try {
            if (path.equals("/") || path.equals("/index.html")) {
                return index();
            }
            Matcher m = NAME.matcher(path);
            if (m.matches()) {
                InputStream in = assets.open("web/" + m.group(1));
                return response(200, "OK", TYPES.get(m.group(2)), in, headers("no-cache"));
            }
        } catch (IOException e) {
            // not in the APK: 404 below
        }
        return response(404, "Not Found", "text/plain", new ByteArrayInputStream(new byte[0]), headers("no-store"));
    }

    private WebResourceResponse index() throws IOException {
        byte[] bytes = new byte[16];
        random.nextBytes(bytes);
        String nonce = Base64.encodeToString(bytes, Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING);
        String html = read("web/index.html")
                .replace("__BUILD__", BuildInfo.BUILD)
                .replace("__API__", String.valueOf(BuildInfo.API))
                .replace("__MODE__", "local")
                .replace("__NONCE__", nonce);
        Map<String, String> h = headers("no-store");
        h.put("Content-Security-Policy", "default-src 'self'; script-src 'self' 'nonce-" + nonce + "'; "
                + "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: http: https:; "
                + "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'");
        return response(200, "OK", "text/html", new ByteArrayInputStream(html.getBytes("UTF-8")), h);
    }

    private String read(String name) throws IOException {
        InputStream in = assets.open(name);
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
            return out.toString("UTF-8");
        } finally {
            in.close();
        }
    }

    private static Map<String, String> headers(String cache) {
        Map<String, String> h = new HashMap<String, String>();
        h.put("Cache-Control", cache);
        h.put("X-Content-Type-Options", "nosniff");
        h.put("Referrer-Policy", "no-referrer");
        return h;
    }

    private static WebResourceResponse response(int status, String reason, String mime, InputStream in, Map<String, String> h) {
        String encoding = mime.startsWith("text/") || mime.startsWith("application/manifest") ? "UTF-8" : null;
        return new WebResourceResponse(mime, encoding, status, reason, h, in);
    }
}
