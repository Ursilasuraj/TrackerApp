package android.util;

public class Base64 {
    public static final int NO_PADDING = 1;
    public static final int NO_WRAP = 2;
    public static final int URL_SAFE = 8;

    public static String encodeToString(byte[] data, int flags) {
        java.util.Base64.Encoder e = (flags & URL_SAFE) != 0 ? java.util.Base64.getUrlEncoder() : java.util.Base64.getEncoder();
        if ((flags & NO_PADDING) != 0) e = e.withoutPadding();
        return e.encodeToString(data);
    }
}
