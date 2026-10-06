# TodoTracker

A personal TODO app for **Windows, macOS, Linux and Android**.

- **On a computer** it is a small local web server written with the Python
  standard library only (no pip packages, no build step, no CDN). The page
  opens in an app window of Edge, Chrome, Chromium or Brave; the data stays
  in a SQLite file next to the code. `python todo.pyw` serves
  http://127.0.0.1:8765 (this computer only).
- **On an Android phone** it is an app (an APK) with the same pages; the
  data is kept on the phone and reminders come from the phone's alarm clock.
  It needs no computer and no internet.

The phone and the computers keep **separate lists** for now; *Export JSON* /
*Import JSON* moves tasks between them. Syncing through an account is planned
(see [Syncing later](#syncing-later)).

| | Install | Starts at login | Reminders | Global key |
|---|---|---|---|---|
| Windows 10/11 | `install.ps1` | yes (background) | Windows notifications | Ctrl+Alt+T |
| macOS | `sh install.sh` | yes (LaunchAgent) | Notification Centre | your own shortcut |
| Linux | `sh install.sh` | yes (XDG autostart) | desktop notifications | your own shortcut |
| Android 7+ | the APK | — | alarm clock, also when closed | — |

## Install on Windows

1. Install **Python 3.12 or newer** from python.org (tick *Add python.exe to
   PATH*); 3.9 is the minimum. Edge is part of Windows.
2. Put this folder somewhere permanent, e.g. `D:\TodoTracker`.
3. In that folder run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   This creates `Startup\TodoTracker.lnk` (runs `pythonw.exe "<folder>\todo.pyw" --background`
   at login), a Start-menu shortcut, and starts the app. Add `-OpenAtLogin` to
   open the window at login as well.

`uninstall.ps1` removes both shortcuts and stops the app; the data stays.

**Updating (all computers):** replace the code files and start TodoTracker
again. The new copy asks the running older one to stop and takes over; open
windows save what is pending and reload themselves. Starting TodoTracker while
it already runs brings its window to the front instead of opening another.

## Install on macOS or Linux

1. **Python 3.9 or newer.** macOS: from python.org or `brew install python`.
   Linux: the `python3` package (it has SQLite).
2. A Chromium-based browser for a proper app window (Edge, Chrome, Chromium
   or Brave; `TODOTRACKER_BROWSER` picks one). Without one the page opens in
   the default browser.
3. Put this folder somewhere permanent and run, in a terminal in it:

   ```sh
   sh install.sh                  # menu entry, start at login, start now
   sh install.sh --open-at-login  # also open the window at login
   sh install.sh --no-autostart   # menu entry only
   sh uninstall.sh                # remove both and stop the app (keeps data)
   ```

   - **macOS:** `~/Applications/TodoTracker.app` (Launchpad, Spotlight, Dock)
     and a LaunchAgent, `~/Library/LaunchAgents/local.todotracker.plist`.
     macOS may report a new login item. Notifications are shown through
     *Script Editor* (osascript), so they carry its name.
   - **Linux:** `~/.local/share/applications/todotracker.desktop` and the same
     file in `~/.config/autostart/`. Notifications use `notify-send` or
     D-Bus; bringing the window to the front uses `wmctrl` or `xdotool` when
     installed (X11), otherwise a new window opens.
4. **A keyboard shortcut** (instead of Windows' Ctrl+Alt+T): give the command
   the installer prints (`python3 …/todo.pyw`) a shortcut in your system
   settings (GNOME: Settings → Keyboard → Custom Shortcuts; macOS: a
   Shortcuts.app shortcut that runs it as a shell script). On Ubuntu
   Ctrl+Alt+T already opens a terminal, so pick another key.

## Install on Android

1. Get `TodoTracker.apk` (built by `tools/build_apk.py`, or the *Android APK*
   artifact of the repository's GitHub Actions) onto the phone.
2. Open it. Android asks to allow the app you opened it with (Files, Chrome,
   …) to *install unknown apps*: allow it, then install. Play Protect may
   say it does not know the developer: choose *Install anyway*.
3. Open TodoTracker and allow notifications (for reminders).

Things to know:

- **The tasks live in the app's storage on the phone.** Uninstalling the app
  deletes them. Use *Export JSON* in the menu (☰) from time to time; it asks
  where to save (Downloads, Drive, …). *Import JSON* reads such a file.
- **Updates must be signed with the same key** as the installed app, or
  Android refuses them; replacing an app with a differently signed one means
  uninstalling it first (export first!). Keep the signing key and its
  password private and backed up; they are not in this repository.
- Reminders ring at the target time even when the app is closed (exact
  alarms; Android may delay them a little in battery saving). They survive a
  restart of the phone. Date-only targets remind at 09:00.
- The pages need a recent *Android System WebView* (updated through the Play
  Store); the app says so if it is too old.
- Google is introducing developer verification for apps installed outside
  the Play Store (announced for some countries from September 2026 and more
  in 2027). If the phone refuses the APK for that reason, `adb install
  TodoTracker.apk` from a computer still works, or the developer of the copy
  you install registers with Google.

## Using it

### Quick add

Type a title and press **Enter**. The date and time it was written are stored
automatically. **Shift+Enter** adds the task and opens its details. Tokens
anywhere in the text (shown as chips before you submit):

| Token | Meaning |
|---|---|
| `#label` | add a label (created on first use; `#3` is not a label) |
| `!high` `!h`, `!med` `!m`, `!low` `!l` | priority (default Medium) |
| `^today` `^tod`, `^tomorrow` `^tom` | target date |
| `^mon` … `^sun` | the next such weekday (never today) |
| `^+3d`, `^+2w` | in 3 days, in 2 weeks |
| `^2026-10-01`, `^24.12.`, `^24.12.2026` | a date (a past day.month without a year means next year) |
| `^fri@14:30`, `^tom@9` | date with a time |

While typing `#wo…` a list of existing labels appears: **Tab** completes,
the arrow keys move, and **Enter** picks a suggestion only after an arrow key
was used (otherwise Enter adds the task as typed).

In a label-filtered view new tasks get the filtered labels; in the Today view
they are due today.

### Keys

| Key | Where | Does |
|---|---|---|
| Ctrl+Alt+T | anywhere in Windows | show TodoTracker, cursor in the add box (macOS/Linux: your own shortcut) |
| Ctrl+N | app | focus the add box |
| Ctrl+F | app | search |
| Esc | app | close the details panel, or clear the search |
| ↑ / ↓ | list | move between tasks |
| Enter / Space | list | open the task / tick it (Undo in the toast) |
| Enter, Esc, ↑/↓, Alt+↑/↓, Backspace | subtask title | save + next, revert, move, reorder, delete if empty |

### Views, labels, search

Sidebar views (with counts): All open, Overdue (red when not zero), Today,
Next 7 days, No target date, Done, Everything. Click labels to filter;
*Any/All* decides whether a task needs one or every selected label. Rename a
label with ✎ (renaming onto an existing name merges the two), recolour it by
clicking its dot, delete it with 🗑 (tasks stay).

Search (Ctrl+F) looks at titles, descriptions, subtask titles and subtask
notes. Every word must match the start of a word. It uses SQLite FTS5, or a
plain LIKE search if FTS5 is unavailable.

Sort by Newest, Target date (grouped under Overdue / Today / Next 7 days /
Later / No target date) or Priority.

### Details panel

Title, status (Open / In progress / Done), priority, target date with an
optional time (✕ = no date), labels, and a Markdown description with a live
preview. Paste (Ctrl+V) or drop images into the description, or use *Add
image* (on a phone: the gallery or camera); they are stored in `data/images`
(on the phone: in the app) and clicking one in the preview enlarges it.
Everything saves by itself (text after a 600 ms pause; selects and dates at
once).

### On a phone (or any narrow window)

- The views and labels are in the **☰ menu** (a drawer); picking a view
  closes it, a tap beside it or **Back** too.
- **Add** next to the add box (the keyboard stays up for the next task).
- The details fill the screen; **Back** or ✕ closes them.
- Everything you tap is at least 24 × 24 px (icons 40 px); controls that
  appear on hover with a mouse are always shown.
- **The matrix as lists:** Do, Schedule, Delegate, Eliminate one below the
  other (a phone-sized board piles notes up); *Board* switches to the board.
  Tap an item (also in the tray) for a bar with *Open*, *Move to*
  Do/Schedule/Delegate/Eliminate, *Back to unsorted*, *Must happen before…*
  (then tap the item that waits) and its links with ✕ to remove one. On the
  board a swipe scrolls and a short hold picks a note up.

### Subtasks

Any task can have subtasks (details panel → *Add a subtask*):

- **Enter** adds one; **pasting several lines adds one subtask per line**. A
  list marker or checkbox (`-`, `*`, `1.`, `- [ ]`, `- [x]`) is removed only
  when a space follows, so `3.5 kg flour` and `-v flag` stay as they are;
  empty bullets and `---` / `* * *` rules are skipped; `[x]` lines arrive
  ticked. **Esc** clears the draft; a second Esc closes the panel.
- Titles are edited in place: **Enter** saves and moves on, **Esc** reverts,
  **Backspace** in an empty title deletes it (holding Backspace does not
  delete more), **↑/↓** move between titles, **Alt+↑/↓** or the ⋮⋮ grip
  reorder (Esc during a drag cancels it), ✕ removes with **Undo** (same
  position and timestamps).
- "2 of 5 done" with a bar. Ticking the last one offers *Mark task done*;
  nothing is completed automatically, and the offer disappears if a subtask
  is unticked again.
- In the list a task with subtasks shows a progress badge (bar, "2/5", ▼);
  click it to unfold the subtasks and tick them there (mouse, or Tab +
  Space/Enter). While searching, folded tasks show just the matching
  subtasks.
- **Subtask details** (⋯ or **Alt+Enter** in its title): a target date with
  an optional time, labels and notes.
  - Labels: only a subset of the task's own labels (shown as toggle chips).
    A subtask never creates labels; taking a label off the task takes it off
    its subtasks. The database enforces this with triggers, not just the UI.
  - Notes (up to 20,000 characters) save after a 700 ms pause, when you
    leave the box, on Esc (which also closes the details), when you switch
    or close the editor, and when the window closes.
  - Under each title: its date, label chips and "📝 first line of the
    notes"; clicking one opens the details on that field.
  - In a subtask title, `#label` (if the task has that label) and `^date`
    work like in the add box; any other `#word` stays in the title.
- **Subtask dates count for their task**: an open subtask due today puts its
  task in Today, an overdue one in Overdue, one within a week in Next 7 days;
  "No target date" means neither the task nor any open subtask has a date.
  The row shows "↳ ⏱ date" when a subtask is due before the task itself, and
  sorting by target date uses the earliest date that still matters. Subtasks
  get reminders too (the toast names their task).
- While filtering by a label, folded tasks show the subtasks that carry it.
- If a description contains checklist lines (`- [ ]`, `- [x]`, `1. [ ]`,
  outside code blocks), *Turn N checklist lines into subtasks* creates them
  and then removes exactly those lines; the rest of the description is left
  untouched.

### Eisenhower matrix

Open it with *Eisenhower matrix* at the top of the sidebar (the number is the
open items in Do).

- **Board:** Schedule (top left: important, not urgent), Do (top right),
  Eliminate (bottom left), Delegate (bottom right); urgency grows to the
  right, importance upwards. Tasks and subtasks are both notes, placed freely
  (a subtask can sit in another quadrant than its task).
- **Unsorted tray** (right): open tasks and subtasks not on the board,
  grouped by task. Drag an item onto the board, or click its suggestion chip
  ("→ Do"), or press Enter on it; *Place all by suggestion* places everything.
  Drag a note onto the tray to take it off the board. Suggestion: due within
  2 days (or overdue) is urgent; Low priority is not important; a subtask
  uses its own date or else its task's.
- **Priority follows placement** for manual moves of task notes: crossing
  into the top half sets High, into the bottom half Low (with a short
  notice). Moving within a half or accepting suggestions never changes it.
- **Links ("A before B"):** drag a note's ⇢ handle onto the note that must
  wait for it. Arrows turn dashed once the first is done; a note shows
  "⛓ waits for N". Clicking a note highlights its whole chain; clicking an
  arrow selects it (*Remove link* or Delete). Self-links and loops are
  refused with a message.
- **Today strip** (above the board): the open items in Do, overdue first.
  **Warnings**: due by today but in Schedule/Eliminate, more than 8 in Do,
  unsorted items due within 2 days, Do items waiting for something not in
  Do. Click a warning to highlight its items.
- **Filters:** Tasks / Subtasks / Show done, plus the sidebar's label filter
  (a subtask without labels counts with its task's labels) and the search.
- **Keys:** notes are focusable; arrows move a note by 2 % (Shift: 10 %),
  Enter opens it, Space shows its chain, Delete puts it back in the tray, Esc
  clears the highlight. Double-click opens a note (a subtask opens its task
  at that subtask). The details panel floats over the board here.

### Reminders

When a target time arrives a notification appears (date-only targets at
09:00): a Windows toast, the macOS Notification Centre, a Linux desktop
notification, or on Android a notification from the app (also when it is
closed). More than three at once become one summary. Changing a target date
re-arms its reminder.

### Day / Night / Auto

The switch at the bottom of the sidebar: **☀ Day · ☾ Night · ◐ Auto** (Auto
follows the system's light/dark setting live). The choice is remembered and
other open windows follow it. The theme is set before the first paint, so
the wrong one never flashes. Both themes meet **WCAG 2.1 AA**: 4.5:1 for
text, 3:1 for large text and for meaningful non-text UI (focus rings, field
borders, priority edges, arrows, label dots); every control shows a visible
focus ring when used with the keyboard.

### Moving between computers and the phone

On the old device: sidebar → **Export JSON**. On the new one: **Import JSON**
and pick that file. Labels, tasks, subtasks (with dates, labels, notes and
matrix positions) and links are added with their original timestamps;
importing the same file again adds nothing (a task whose title and created
time already exist is skipped). A summary shows what was imported.
Pictures are not inside the export file: between computers copy the old
`data/images` folder into the new `data` folder as well (the summary says
when pictures are missing; pictures cannot be moved to or from the phone
yet).

## Data

On a computer everything lives in `data` next to the code (the environment
variable `TODOTRACKER_DATA` points elsewhere):

| Path | Contents |
|---|---|
| `data/todo.db` | the SQLite database (WAL mode) |
| `data/images/` | pasted and dropped images |
| `data/backups/todo-YYYY-MM-DD.db` | daily backups, newest 14 kept |
| `data/todo.log` | the log (errors, starts, stops) |

A backup is written at start and checked hourly (one per day); *Back up now*
forces one. *Export JSON* downloads everything (tasks with labels, subtasks,
the label list, dependencies). Images are not in the export: copy
`data/images` along with it.

On Android the data is in the app's own storage (IndexedDB of its WebView),
pictures included; *Export JSON* is the backup. Android's own device backup
may include it, but do not rely on that.

## Choices made where the spec left room

- **Status values** are stored as `open`, `in_progress`, `done`.
- **"Overdue"**: a date-only target is overdue from the next day on; a target
  with a time is overdue once that time has passed (it then shows in both
  Overdue and Today).
- **"Next 7 days"** is today plus the following six days (the same window as
  the weekday names in badges).
- **Reminders for targets that are already past when you set them** are not
  shown (for example a task added in the Today view after 09:00); otherwise
  every such task would pop up a notification a minute later.
- **Typing a date with the keyboard**: while the year is being typed the
  browser reports years like 0002 or 0203; those are ignored until the year
  has four digits, then the date is saved at once.
- **Dragging a task from the tray** only fixes a contradiction (a Low task
  dropped in the top half becomes High, a High/Medium task dropped in the
  bottom half becomes Low); a Medium task dropped in the top half stays
  Medium. Crossing the middle line with a note already on the board sets
  High or Low as specified.
- **Double-click on a note** is detected from the click's count rather than
  the `dblclick` event: the first click redraws the board (to show the
  chain), and Chromium then sends no `dblclick`.
- **Folded tasks while filtering by labels** show the subtasks that carry
  the label themselves (a subtask without labels does not count there; on
  the matrix it counts with its task's labels, as specified).
- **Label names** may not contain spaces, commas or `#` (they would break the
  `#label` syntax).
- **Undo for "done"** restores the previous status; a task that is reopened
  and then re-completed gets a new completion time.
- **Ctrl+N** works in the Edge app window. In a normal browser tab the
  browser keeps Ctrl+N for itself.
- **macOS and Linux** have no global hotkey from the standard library; any
  system shortcut that starts `todo.pyw` does the same, because starting it
  again brings the running window to the front. `SO_REUSEADDR` stays on there
  (it only allows rebinding over TIME_WAIT; on Windows it is off, see below).
- **The phone keeps its own list.** You chose separate lists for now and an
  account or drive later, so the phone does not talk to the computer at all
  (see [Syncing later](#syncing-later)).
- **The phone's matrix** shows lists by default: on a 390 px screen the
  board's four quadrants are about 170 px wide and notes cover each other.

## Robustness

These rules were bugs in an earlier version and are built in:

- Binds 127.0.0.1 only; requests whose `Host` is not localhost/127.0.0.1 are
  refused (DNS rebinding); every write needs the header `X-Todo: 1`; the page
  has a strict Content-Security-Policy.
- Under `pythonw`, `stdout`/`stderr` go to `data/todo.log`.
- `allow_reuse_address = False` on Windows: there `SO_REUSEADDR` would let a
  second instance bind the same port (double reminders).
- WAL is switched on only if it is not already, with a short retry loop.
  `PRAGMA foreign_keys = ON` on every connection.
- Migrations check `PRAGMA table_info` and take `BEGIN IMMEDIATE` only when
  something is missing, re-check inside, then `ALTER TABLE … ADD COLUMN`. Two
  processes starting at once never fail; normal starts take no write lock.
- The search index is rebuilt only when it is out of date (wrong columns,
  row-count mismatch, or a dirty flag left by a run without FTS5).
- **Single instance:** `/api/ping` returns `{app, api, build, pid, app_dir,
  data_dir}`; `build` is a SHA-1 of the app's `.py` and `web/` files. A start
  from the same folder and data folder replaces a running instance with a
  lower `api` or a different `build` (shutdown request, then stopping the
  process only if its command line contains `todo.pyw`); otherwise it just
  shows the running window. Migrations run after the old instance stopped.
- The page polls `/api/state` every 1.5 s: a new build makes it save and
  reload; an older server shows a banner.
- The page never rebuilds the DOM while a mouse button is held (that would
  swallow the click); redraws keep focus, typed text, the caret and the
  scroll position; a focused text field is kept as the same node, so undo
  history and IME input survive autosaves.
- Subtask writes go through one serialized queue. Changes show immediately;
  the server's answer is drawn only when no further subtask write is queued,
  and refreshes keep the local subtasks while writes are pending. New
  subtasks carry a temporary id until the server's id arrives.
- When subtasks are appended, the write lock is taken (an `UPDATE`) before
  `max(position)` is read, so concurrent adds never share a position.
- Field saves are chained and remember which task they belong to. Closing or
  reloading the window sends pending edits with `fetch(…, {keepalive: true})`
  (within Chromium's 64 KB budget; bigger edits are saved normally and the
  page asks before leaving).
- **Without a server (Android)** the same API runs in the page
  (`web/localdb.js`, a port of `db.py` and the API of `server.py`): writes
  happen in an overlay and are committed or rolled back as a whole, and are
  written to IndexedDB; only one window at a time may change the data (Web
  Locks), others show it read-only; edits made while the page goes away are
  saved in the same moment, of any size. A conformance test sends the same
  requests to both and compares every answer.

## Files

| File | Purpose |
|---|---|
| `todo.pyw` | entry point: logging, single instance, window, threads |
| `server.py` | HTTP server, JSON API, static files, images |
| `db.py` | SQLite schema, migrations, search index, queries |
| `reminders.py` | reminder checks and their texts |
| `platforms.py` | what differs between Windows, macOS and Linux: notifications, app window, processes |
| `hotkey.py` | global Ctrl+Alt+T on Windows (ctypes) |
| `backup.py` | daily backups (SQLite backup API) |
| `install.ps1`, `uninstall.ps1` | Windows: shortcuts, start, stop |
| `install.sh`, `uninstall.sh` | macOS and Linux (`tools/desktop_install.py` does the work) |
| `web/index.html`, `web/app.css`, `web/app.js` | page, styles, list page and editor |
| `web/matrix.js` | Eisenhower matrix page |
| `web/markdown.js` | Markdown renderer (escapes first) and checklist conversion |
| `web/parse.js` | quick-add tokens, dates, paste splitting (pure functions) |
| `web/localdb.js` | the API and data in the page, for the phone (IndexedDB) |
| `web/manifest.webmanifest`, `web/icon*` | icons; lets browsers install the page as an app |
| `android/` | the Android app: WebView, reminders, files (Java, no Gradle) |
| `tools/build_apk.py` | builds and signs the APK |
| `tools/icons.py` | draws the icons for every platform (standard library) |

`web/parse.js` is an extra file not in the original list: the parsers live
there so that Node can test them without a browser.

## API

All writes need `X-Todo: 1`. Errors are `{"error": "message"}` with 400
(validation), 404 (missing) or 403 (host/header).

```
GET    /api/ping | /api/state | /api/meta | /api/export
GET    /api/tasks?view=open|overdue|today|week|nodate|done|all&labels=1,2&match=any|all&q=…&sort=newest|due|priority
GET    /api/tasks/{id}
POST   /api/tasks                  {title, description?, status?, priority?, due_at?, labels?}
PATCH  /api/tasks/{id}             any of the above
DELETE /api/tasks/{id}
POST   /api/tasks/{id}/subtasks        {title} or {items: [title | {title, done?, position?, created_at?, completed_at?}]}
POST   /api/tasks/{id}/subtasks/order  {ids: [...]}
PATCH  /api/subtasks/{id}              {title?, done?, due_at?, labels?, notes?}
DELETE /api/subtasks/{id}              (every subtask write answers with the whole parent task)
POST   /api/matrix                     {items: [{key: "t12" | "s34", matrix: [urgency, importance] | null, priority?}]}
GET    /api/deps
POST   /api/deps                       {before: "t12", after: "s34"}   (loops and self-links: 400; duplicates: no-op)
DELETE /api/deps/{id}
PATCH  /api/labels/{id}            {name?, color?}   (renaming onto an existing name merges)
DELETE /api/labels/{id}
POST   /api/images                 raw image body (PNG, JPEG, GIF, WebP, BMP; ≤ 25 MB)
POST   /api/import                     an Export JSON file (adds; skips tasks that already exist)
POST   /api/backup | /api/shutdown | /api/show     (show: bring the window to the front)
```

## Tests

Tests never touch the real app or data: they use `TODOTRACKER_DATA=<scratch
folder>` and a free port other than 8765, and stop every process they start.

```
node --test tests/parsers.test.js          # parsers and Markdown (Node 18+)
python -m unittest tests.test_backend -v    # storage, migrations, API, single instance, OS helpers
python -m unittest tests.test_install -v    # install.sh / uninstall.sh (Linux and macOS files)
python -m unittest tests.test_localdb -v    # localdb.js answers like the server (needs Node)
python -m unittest tests.test_android -v    # the Android app's Java logic (JVM) and the APK build
python -m unittest tests.test_ui -v         # headless Edge/Chromium, real mouse, keyboard and touch
```

The UI suite also runs as a phone (390 × 844, touch: taps, swipes, holds)
and runs most of its classes a second time in the Android app's mode, with
the data in the page. GitHub Actions runs everything on every push: the
backend on Windows, macOS and Linux (and Python 3.9), the UI suite, and the
APK build (`.github/workflows/`).

The UI tests drive the browser over the DevTools protocol with
`Input.dispatchMouseEvent` presses (~100 ms) and real key events; set
`TT_BROWSER` to the browser executable if it is not found automatically.
They include a contrast scan of every screen in both themes
(`tests/contrast_scan.js`: text, placeholders, field borders, priority
edges, label dots, quadrant edges, arrows and focus rings).

The Node tests need Node.js 18 or newer, which the app itself does not.

## Building the Android app

```sh
python3 tools/build_apk.py --keystore ~/keys/todotracker.p12 --new-keystore   # the first time: makes your key
python3 tools/build_apk.py --keystore ~/keys/todotracker.p12                  # later builds
```

It needs a JDK and Android's build tools (`aapt`, `zipalign`, `apksigner`,
`d8` or `dx`, an `android.jar`): `--sdk` / `ANDROID_HOME`, or on Debian/Ubuntu
`apt install aapt zipalign apksigner dalvik-exchange android-sdk-platform-23`.
The password comes from `TODOTRACKER_KEYSTORE_PASSWORD` (or is asked for).
The APK lands in `dist/` (not in git, nor is any key).

On GitHub the *Android APK* workflow builds it on every change to `web/` or
`android/`. To sign those builds with your key, add two repository secrets
(Settings → Secrets and variables → Actions): `TODOTRACKER_KEYSTORE_B64`
(the key file in base64: `base64 -w0 todotracker.p12`) and
`TODOTRACKER_KEYSTORE_PASSWORD`. Without them the workflow signs with a
throwaway key, which installs but cannot update an app signed with yours.

## Syncing later

You asked for separate lists now and syncing through an account or a drive
later. What that needs (not built yet):

- **Where:** each person's own cloud storage, e.g. the hidden app folder of
  their Google Drive (`drive.appdata` scope): no server of ours, nothing
  visible to other apps, and it works with the Google account ("mail id")
  the phone already has.
- **Records that can be merged:** a random id (UUID) per task, subtask, label
  and link instead of only numbers; `updated_at` per field group; deletions
  kept as tombstones for a while. Each device keeps its own SQLite or
  IndexedDB and writes a change log to the drive; the others replay it.
  Same edit on two devices: the later one wins per field, and titles and
  descriptions keep both versions if they differ. `web/localdb.js` and
  `db.py` already behave identically (tested), which this needs.
- **Pictures** as separate files in the same folder.
- **Sign-in:** OAuth for installed apps (PKCE); the token stays on each
  device. Desktop and phone need a Google Cloud OAuth client.

The other option you mentioned, a Windows PC reached through Tailscale, is
different: the phone would use the PC's list directly (one list, no merging),
but only while the PC is on and reachable. It does not fit "linked to a mail
id or drive"; choose one of the two before this is built.
