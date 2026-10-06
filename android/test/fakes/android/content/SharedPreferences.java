package android.content;

import java.util.HashMap;
import java.util.Map;

public class SharedPreferences {
    final Map<String, Object> values = new HashMap<String, Object>();

    public String getString(String key, String dflt) {
        return values.containsKey(key) ? (String) values.get(key) : dflt;
    }

    public boolean getBoolean(String key, boolean dflt) {
        return values.containsKey(key) ? (Boolean) values.get(key) : dflt;
    }

    public Editor edit() {
        return new Editor();
    }

    public class Editor {
        public Editor putString(String k, String v) { values.put(k, v); return this; }
        public Editor putBoolean(String k, boolean v) { values.put(k, v); return this; }
        public void apply() { }
    }
}
