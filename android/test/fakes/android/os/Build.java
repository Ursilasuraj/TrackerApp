package android.os;

public class Build {
    public static class VERSION {
        /** Not a constant, as on a phone: tests run as Android -Dsdk=24 ... 34. */
        public static final int SDK_INT = Integer.getInteger("sdk", 34);
    }
}
