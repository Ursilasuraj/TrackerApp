package android.util;

public class Log {
    public static int w(String tag, String msg, Throwable t) { System.err.println("W " + msg + ": " + t); return 0; }
    public static int w(String tag, String msg) { System.err.println("W " + msg); return 0; }
    public static int i(String tag, String msg) { return 0; }
}
