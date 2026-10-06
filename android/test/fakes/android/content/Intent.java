package android.content;

public class Intent {
    public static final int FLAG_ACTIVITY_NEW_TASK = 0x10000000;
    public static final int FLAG_ACTIVITY_SINGLE_TOP = 0x20000000;
    public final Class<?> target;

    public Intent(Context ctx, Class<?> target) { this.target = target; }
    public Intent addFlags(int flags) { return this; }
}
