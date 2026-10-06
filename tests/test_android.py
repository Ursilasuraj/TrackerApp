"""The Android app: its Java logic on a plain JVM, and the APK build.

Run:  python -m unittest tests.test_android -v
The JVM tests need a JDK and Android's org.json (Debian/Ubuntu:
apt install libandroid-json-java, or set TT_ANDROID_JSON_JAR). The build test
needs the Android build tools (see tools/build_apk.py). Missing tools skip.
"""

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
SRC = os.path.join(APP_DIR, 'android', 'src', 'io', 'github', 'ursilasuraj', 'todotracker')
TEST = os.path.join(APP_DIR, 'android', 'test')
JSON_JAR = os.environ.get('TT_ANDROID_JSON_JAR') or next(iter(glob.glob('/usr/share/java/com.android.json*.jar')), None)
sys.path.insert(0, os.path.join(APP_DIR, 'tools'))


@unittest.skipUnless(shutil.which('javac') and shutil.which('java') and JSON_JAR, 'needs a JDK and org.json')
class JavaLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix='tt-jvm-')
        cls.classes = os.path.join(cls.dir, 'classes')
        sources = glob.glob(os.path.join(TEST, 'fakes', '**', '*.java'), recursive=True)
        sources += glob.glob(os.path.join(TEST, 'io', '**', '*.java'), recursive=True)
        sources += [os.path.join(SRC, 'Reminders.java'), os.path.join(SRC, 'AssetServer.java')]
        subprocess.run(['javac', '-nowarn', '-encoding', 'UTF-8', '-cp', JSON_JAR, '-d', cls.classes] + sources,
                       check=True, capture_output=True, text=True)
        cls.assets = os.path.join(cls.dir, 'assets')
        shutil.copytree(os.path.join(APP_DIR, 'web'), os.path.join(cls.assets, 'web'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def run_as(self, sdk):
        r = subprocess.run(['java', f'-Dsdk={sdk}', '-cp', os.pathsep.join([self.classes, JSON_JAR]),
                            'io.github.ursilasuraj.todotracker.AndroidLogicTest', self.assets],
                           capture_output=True, text=True, timeout=120)
        failed = [line for line in r.stdout.splitlines() if line.startswith('FAIL')]
        self.assertEqual((r.returncode, failed), (0, []), r.stdout + r.stderr)
        self.assertIn('ALL OK', r.stdout)

    def test_as_android_7(self):
        self.run_as(24)

    def test_as_android_14(self):
        self.run_as(34)


def find_build_tools():
    try:
        import build_apk
        return build_apk.find_tools(os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT'))
    except SystemExit:
        return None


@unittest.skipUnless(find_build_tools() and shutil.which('keytool'), 'needs the Android build tools')
class ApkBuildTests(unittest.TestCase):
    def test_build(self):
        import build_apk
        tmp = tempfile.mkdtemp(prefix='tt-apk-test-')
        self.addCleanup(shutil.rmtree, tmp, True)
        key = os.path.join(tmp, 'throwaway.p12')
        apk = os.path.join(tmp, 'TodoTracker.apk')
        env = dict(os.environ, TODOTRACKER_KEYSTORE_PASSWORD='test-only-password')
        r = subprocess.run([sys.executable, os.path.join(APP_DIR, 'tools', 'build_apk.py'), '--keystore', key,
                            '--new-keystore', '--out', apk, '--version-code', '7'],
                           env=env, capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        tools = find_build_tools()
        badging = subprocess.run([tools['aapt'], 'dump', 'badging', apk], capture_output=True, text=True, check=True).stdout
        self.assertIn(f"package: name='{build_apk.PACKAGE}' versionCode='7'", badging)
        self.assertIn("sdkVersion:'24'", badging)
        self.assertIn("targetSdkVersion:'34'", badging)
        self.assertIn(f"launchable-activity: name='{build_apk.PACKAGE}.MainActivity'", badging)
        for perm in ('POST_NOTIFICATIONS', 'USE_EXACT_ALARM', 'RECEIVE_BOOT_COMPLETED'):
            self.assertIn(f"android.permission.{perm}'", badging)
        with zipfile.ZipFile(apk) as z:
            names = set(z.namelist())
            self.assertIn('classes.dex', names)
            self.assertIn('res/drawable-xxhdpi-v4/ic_notification.png', names)
            web = [n for n in os.listdir(os.path.join(APP_DIR, 'web')) if build_apk.WEB_FILES.match(n)]
            for n in web:
                with open(os.path.join(APP_DIR, 'web', n), 'rb') as f:
                    self.assertEqual(z.read('assets/web/' + n), f.read(), n)
        v = subprocess.run([tools['apksigner'], 'verify', '--verbose', apk], capture_output=True, text=True)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        self.assertIn('Verified using v2 scheme (APK Signature Scheme v2): true', v.stdout)


if __name__ == '__main__':
    unittest.main()
