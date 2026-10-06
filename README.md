# TodoTracker

A personal TODO app for Windows 11. It is a small local web server written
with the Python standard library only (no pip packages, no build step, no
CDN). The UI opens in an Edge app window; the data stays in a SQLite file
next to the code.

- `pythonw todo.pyw` serves http://127.0.0.1:8765 (localhost only) and opens
  `msedge --app=http://127.0.0.1:8765/ --window-size=1200,820` (or the
  default browser when Edge is missing).
- It starts at Windows login in the background; **Ctrl+Alt+T** brings the
  window up with the cursor in the add box.

## Install

1. Install **Python 3.12 or newer** from python.org (tick *Add python.exe to
   PATH*). Edge is part of Windows 11.
2. Put this folder somewhere permanent, e.g. `D:\TodoTracker`.
3. In that folder run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   This creates `Startup\TodoTracker.lnk` (runs `pythonw.exe "<folder>\todo.pyw" --background`
   at login), a Start-menu shortcut, and starts the app. Add `-OpenAtLogin` to
   open the window at login as well.

`uninstall.ps1` removes both shortcuts and stops the app; the data stays.

**Updating:** replace the code files and start TodoTracker again (Start menu).
The new copy asks the running older one to stop and takes over; open windows
save what is pending and reload themselves.

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
| Ctrl+Alt+T | anywhere in Windows | show TodoTracker, cursor in the add box |
| Ctrl+N | app | focus the add box |
| Ctrl+F | app | search |
| Esc | app | close the details panel, or clear the search |
| ↑ / ↓ | list | move between tasks |
| Enter / Space | list | open the task / tick it (Undo in the toast) |

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
preview. Paste (Ctrl+V) or drop images into the description; they are stored
in `data\images` and clicking one in the preview enlarges it. Everything
saves by itself (text after a 600 ms pause; selects and dates at once).

### Reminders

When a target time arrives a Windows notification appears (date-only targets
at 09:00). More than three at once become one summary. Changing a target date
re-arms its reminder.

## Data

Everything lives in `data` next to the code (the environment variable
`TODOTRACKER_DATA` points elsewhere):

| Path | Contents |
|---|---|
| `data\todo.db` | the SQLite database (WAL mode) |
| `data\images\` | pasted and dropped images |
| `data\backups\todo-YYYY-MM-DD.db` | daily backups, newest 14 kept |
| `data\todo.log` | the log (errors, starts, stops) |

A backup is written at start and checked hourly (one per day); *Back up now*
forces one. *Export JSON* downloads everything (tasks with labels, subtasks,
the label list, dependencies). Images are not in the export: copy
`data\images` along with it.

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
- **Label names** may not contain spaces, commas or `#` (they would break the
  `#label` syntax).
- **Undo for "done"** restores the previous status; a task that is reopened
  and then re-completed gets a new completion time.
- **Ctrl+N** works in the Edge app window. In a normal browser tab the
  browser keeps Ctrl+N for itself.
- **Non-Windows** (development only): the toast is written to the log, there
  is no global hotkey, and `SO_REUSEADDR` stays on (on Linux it only allows
  rebinding over TIME_WAIT; on Windows it is off, see below).

## Robustness

These rules were bugs in an earlier version and are built in:

- Binds 127.0.0.1 only; requests whose `Host` is not localhost/127.0.0.1 are
  refused (DNS rebinding); every write needs the header `X-Todo: 1`; the page
  has a strict Content-Security-Policy.
- Under `pythonw`, `stdout`/`stderr` go to `data\todo.log`.
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
- Field saves are chained and remember which task they belong to. Closing or
  reloading the window sends pending edits with `fetch(…, {keepalive: true})`
  (within Chromium's 64 KB budget; bigger edits are saved normally and the
  page asks before leaving).

## Files

| File | Purpose |
|---|---|
| `todo.pyw` | entry point: logging, single instance, window, threads |
| `server.py` | HTTP server, JSON API, static files, images |
| `db.py` | SQLite schema, migrations, search index, queries |
| `reminders.py` | reminder checks and Windows toasts |
| `hotkey.py` | global Ctrl+Alt+T (ctypes) |
| `backup.py` | daily backups (SQLite backup API) |
| `install.ps1`, `uninstall.ps1` | shortcuts, start, stop |
| `web/index.html`, `web/app.css`, `web/app.js` | page, styles, list page and editor |
| `web/matrix.js` | Eisenhower matrix page |
| `web/markdown.js` | Markdown renderer (escapes first) and checklist conversion |
| `web/parse.js` | quick-add tokens, dates, paste splitting (pure functions) |

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
PATCH  /api/labels/{id}            {name?, color?}   (renaming onto an existing name merges)
DELETE /api/labels/{id}
POST   /api/images                 raw image body (PNG, JPEG, GIF, WebP, BMP; ≤ 25 MB)
POST   /api/backup | /api/shutdown
```

## Tests

Tests never touch the real app or data: they use `TODOTRACKER_DATA=<scratch
folder>` and a free port other than 8765, and stop every process they start.

```
node --test tests/parsers.test.js          # parsers and Markdown (Node 18+)
python -m unittest tests.test_backend -v    # storage, migrations, API, single instance
python -m unittest tests.test_ui -v         # headless Edge/Chromium, real mouse + keyboard
```

The UI tests drive the browser over the DevTools protocol with
`Input.dispatchMouseEvent` presses (~100 ms) and real key events; set
`TT_BROWSER` to the browser executable if it is not found automatically.
