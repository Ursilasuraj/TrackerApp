package android.webkit;

import java.io.InputStream;
import java.util.Map;

public class WebResourceResponse {
    public final String mime;
    public final String encoding;
    public final int status;
    public final Map<String, String> headers;
    public final InputStream data;

    public WebResourceResponse(String mime, String encoding, int status, String reason, Map<String, String> headers, InputStream data) {
        this.mime = mime; this.encoding = encoding; this.status = status; this.headers = headers; this.data = data;
    }
}
