package android.net;

public class Uri {
    private final java.net.URI u;

    private Uri(String s) { u = java.net.URI.create(s); }

    public static Uri parse(String s) { return new Uri(s); }
    public String getScheme() { return u.getScheme(); }
    public String getHost() { return u.getHost(); }
    public String getPath() { return u.getRawPath(); }
    @Override public String toString() { return u.toString(); }
}
