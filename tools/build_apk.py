"""Builds the Android app (an APK) without Gradle.

    python3 tools/build_apk.py --keystore KEY.jks              signed with your key
    python3 tools/build_apk.py --sdk "$ANDROID_HOME" ...       with the Android SDK

Needs a JDK (javac, keytool) and Android's build tools: aapt, zipalign,
apksigner and d8 (or the older dx), plus an android.jar of API 23 or newer.
They are found in --sdk (build-tools/<newest>, platforms/<newest>), else on
PATH (Debian/Ubuntu: apt install aapt zipalign apksigner dalvik-exchange
android-sdk-platform-23).

The key: Android installs an update only if it is signed with the same key
as the installed app. Keep KEY.jks and its password safe and private (never
in the repository); without them the app can only be replaced by
uninstalling it, which deletes the tasks on the phone (export them first).
The password comes from the TODOTRACKER_KEYSTORE_PASSWORD environment
variable (or is asked for). --new-keystore makes a key the first time.

The APK holds web/ unchanged; android/ has the Java code and resources.
"""

import argparse
import getpass
import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(TOOLS_DIR)
ANDROID_DIR = os.path.join(APP_DIR, 'android')
PACKAGE = 'io.github.ursilasuraj.todotracker'
WEB_FILES = re.compile(r'^[A-Za-z0-9_-]+\.(html|js|css|svg|png|ico|webmanifest)$')

sys.path.insert(0, TOOLS_DIR)
import icons  # noqa: E402


def run(cmd, **kw):
    print('$', ' '.join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def by_version(paths, name_of):
    """Paths with a plain version number ("35.0.0", "android-34"), newest
    first; previews ("36.0.0-rc1", "android-35-ext14", "android-Baklava")
    are left out."""
    found = []
    for p in paths:
        m = re.fullmatch(r'(?:android-)?(\d+(?:\.\d+)*)', name_of(p))
        if m:
            found.append((tuple(int(x) for x in m.group(1).split('.')), p))
    return [p for _, p in sorted(found, reverse=True)]


def find_tools(sdk):
    tools = {}
    if sdk:
        # Each tool from the newest build-tools that has it (newer ones may
        # no longer ship aapt).
        for bt in by_version(glob.glob(os.path.join(sdk, 'build-tools', '*')), os.path.basename):
            for name in ('aapt', 'zipalign', 'apksigner', 'd8'):
                for file in (name, name + '.exe', name + '.bat'):     # Windows: aapt.exe, d8.bat
                    path = os.path.join(bt, file)
                    if name not in tools and os.path.exists(path):
                        tools[name] = path
        jars = by_version(glob.glob(os.path.join(sdk, 'platforms', 'android-*', 'android.jar')),
                          lambda p: os.path.basename(os.path.dirname(p)))
        if jars:
            tools['android.jar'] = jars[0]
    for name in ('aapt', 'zipalign', 'apksigner', 'd8'):
        tools.setdefault(name, shutil.which(name))
    if not tools.get('d8'):
        tools['dx'] = shutil.which('dalvik-exchange') or shutil.which('dx')
    if not tools.get('android.jar'):
        jars = sorted(glob.glob('/usr/lib/android-sdk/platforms/android-*/android.jar'))
        tools['android.jar'] = jars[-1] if jars else None
    tools['javac'] = shutil.which('javac')
    tools['jar'] = shutil.which('jar')
    tools['keytool'] = shutil.which('keytool')
    missing = [k for k in ('aapt', 'zipalign', 'apksigner', 'android.jar', 'javac', 'jar') if not tools.get(k)]
    if not tools.get('d8') and not tools.get('dx'):
        missing.append('d8 or dx')
    if missing:
        raise SystemExit('Missing: ' + ', '.join(missing) + '. See the top of tools/build_apk.py.')
    return tools


def web_build_id():
    """Like todo.pyw's build id (any change to web/ gives a new one)."""
    h = hashlib.sha1()
    web = os.path.join(APP_DIR, 'web')
    for name in sorted(os.listdir(web)):
        if WEB_FILES.match(name):
            h.update(name.encode() + b'\0')
            with open(os.path.join(web, name), 'rb') as f:
                h.update(f.read())
    return h.hexdigest()


def api_version():
    with open(os.path.join(APP_DIR, 'server.py'), encoding='utf-8') as f:
        return int(re.search(r'^API = (\d+)', f.read(), re.M).group(1))


def git_count():
    try:
        out = subprocess.run(['git', '-C', APP_DIR, 'rev-list', '--count', 'HEAD'], capture_output=True, text=True, check=True)
        return int(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 1


def make_keystore(path, password):
    tools = {'keytool': shutil.which('keytool')}
    if not tools['keytool']:
        raise SystemExit('keytool (part of the JDK) is needed to make a key.')
    if os.path.exists(path):
        raise SystemExit(f'{path} exists already; it is your key, keep it.')
    run([tools['keytool'], '-genkeypair', '-keystore', path, '-storetype', 'PKCS12', '-alias', 'todotracker',
         '-keyalg', 'RSA', '-keysize', '4096', '-validity', '36500', '-dname', 'CN=TodoTracker',
         '-storepass:env', 'TT_KS_PASS', '-keypass:env', 'TT_KS_PASS'], env=dict(os.environ, TT_KS_PASS=password))


def build(args, password):
    tools = find_tools(args.sdk)
    work = tempfile.mkdtemp(prefix='tt-apk-')
    try:
        res = os.path.join(work, 'res')
        shutil.copytree(os.path.join(ANDROID_DIR, 'res'), res)
        icons.make_android(res)
        assets = os.path.join(work, 'assets', 'web')
        os.makedirs(assets)
        for name in sorted(os.listdir(os.path.join(APP_DIR, 'web'))):
            if WEB_FILES.match(name):
                shutil.copy2(os.path.join(APP_DIR, 'web', name), assets)
        gen = os.path.join(work, 'gen')
        pkg_dir = os.path.join(gen, *PACKAGE.split('.'))
        os.makedirs(pkg_dir)
        with open(os.path.join(pkg_dir, 'BuildInfo.java'), 'w', encoding='utf-8') as f:
            f.write(f'package {PACKAGE};\n\n/** Written by tools/build_apk.py. */\n'
                    f'final class BuildInfo {{\n    static final int API = {api_version()};\n'
                    f'    static final String BUILD = "{web_build_id()}";\n\n    private BuildInfo() {{\n    }}\n}}\n')

        code = args.version_code or git_count()
        name = args.version_name or f'1.{code}'
        unaligned = os.path.join(work, 'app.unaligned.apk')
        run([tools['aapt'], 'package', '-f', '-m', '-J', gen, '-M', os.path.join(ANDROID_DIR, 'AndroidManifest.xml'),
             '-S', res, '-A', os.path.join(work, 'assets'), '-I', tools['android.jar'],
             '--min-sdk-version', '24', '--target-sdk-version', '34',
             '--version-code', str(code), '--version-name', name, '-F', unaligned])

        classes = os.path.join(work, 'classes')
        os.makedirs(classes)
        sources = glob.glob(os.path.join(ANDROID_DIR, 'src', '**', '*.java'), recursive=True)
        sources += glob.glob(os.path.join(gen, '**', '*.java'), recursive=True)
        run([tools['javac'], '-source', '8', '-target', '8', '-encoding', 'UTF-8', '-nowarn', '-Xlint:-options',
             '-bootclasspath', tools['android.jar'], '-d', classes] + sources)
        dex_dir = os.path.join(work, 'dex')
        os.makedirs(dex_dir)
        if tools.get('d8'):
            jar = os.path.join(work, 'classes.jar')
            run([tools['jar'], 'cf', jar, '-C', classes, '.'])
            run([tools['d8'], '--release', '--min-api', '24', '--lib', tools['android.jar'], '--output', dex_dir, jar])
        else:
            run([tools['dx'], '--dex', '--min-sdk-version=24', '--output=' + os.path.join(dex_dir, 'classes.dex'), classes])
        run([tools['aapt'], 'add', '-k', unaligned, 'classes.dex'], cwd=dex_dir)
        if not os.path.exists(os.path.join(dex_dir, 'classes.dex')):
            raise SystemExit('no classes.dex was made')

        aligned = os.path.join(work, 'app.aligned.apk')
        run([tools['zipalign'], '-f', '-p', '4', unaligned, aligned])
        out = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        env = dict(os.environ, TT_KS_PASS=password)
        run([tools['apksigner'], 'sign', '--ks', args.keystore, '--ks-key-alias', 'todotracker',
             '--ks-pass', 'env:TT_KS_PASS', '--key-pass', 'env:TT_KS_PASS', '--out', out, aligned], env=env)
        run([tools['apksigner'], 'verify', '--print-certs', out])
        print(f'\nBuilt {out} (version {name}, code {code}, {os.path.getsize(out) // 1024} KB)')
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    p = argparse.ArgumentParser(description='Build the TodoTracker APK.')
    p.add_argument('--keystore', required=True, help='your signing key (a .jks/.p12 file)')
    p.add_argument('--new-keystore', action='store_true', help='make the key first (once)')
    p.add_argument('--sdk', default=os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT'),
                   help='Android SDK folder (default: $ANDROID_HOME)')
    p.add_argument('--out', default=os.path.join(APP_DIR, 'dist', 'TodoTracker.apk'))
    p.add_argument('--version-code', type=int, help='default: the number of commits')
    p.add_argument('--version-name')
    args = p.parse_args(argv)
    password = os.environ.get('TODOTRACKER_KEYSTORE_PASSWORD') or getpass.getpass('Keystore password: ')
    if len(password) < 6:
        raise SystemExit('The keystore password must have at least 6 characters.')
    if args.new_keystore:
        make_keystore(args.keystore, password)
    elif not os.path.exists(args.keystore):
        raise SystemExit(f'{args.keystore} does not exist (use --new-keystore to make it once).')
    build(args, password)
    return 0


if __name__ == '__main__':
    sys.exit(main())
