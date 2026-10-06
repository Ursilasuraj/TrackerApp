package android.content.res;

import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.IOException;
import java.io.InputStream;

/** Reads assets from a folder (the APK's assets/ in the real app). */
public class AssetManager {
    private final String root;

    public AssetManager(String root) { this.root = root; }

    public InputStream open(String name) throws IOException {
        if (name.contains("..")) throw new FileNotFoundException(name);
        return new FileInputStream(root + "/" + name);
    }
}
