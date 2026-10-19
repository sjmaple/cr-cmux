#!/usr/bin/env python3
"""Guest half of scripts/cmux-next/os-smoke/run.sh. Runs INSIDE a disposable
Tart VM (stdlib only), never on a laptop or a shared Mac.

Launches one cmux-next build the way the GUI proofs do (no-activate,
automation socket, scratch config, bounded window), waits with deadlines for
the first window and the control socket, opens one terminal tab and types a
command into it, opens the New Tab page, a Chromium and a WebKit browser tab, the Settings tab
and toggles the sidebar through the socket, takes a debug.window_snapshot
after each step (terminal.png, newtab.png, window.png, webkit.png,
settings.png, sidebar.png), opens an agent chat (agentchat.png), checks the app
is still alive 60 s after launch (alive60.png, processes.txt), quits, copies
crash reports and the unified log, dyld-loads every Mach-O in the bundle on
this OS (an executable for 3 s, a dylib through dlopen) and writes result.json. When the guest macOS is older than LSMinimumSystemVersion it
also records what LaunchServices (`open`) says, then still execs the binary
directly so the dyld error is captured.

Usage: guest-probe.py APP_PATH OUT_DIR
"""
import glob, http.server, json, os, platform, plistlib, re, signal, socket, subprocess, sys, tempfile, threading, time

APP, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)
LAUNCH_DEADLINE = float(os.environ.get("OS_SMOKE_LAUNCH_DEADLINE", "180"))
TAB_DEADLINE = float(os.environ.get("OS_SMOKE_TAB_DEADLINE", "120"))
TITLE = "cmux-os-smoke"
result = {"steps": {}, "notes": []}


STALLS_SEEN = [0]


def step(name, ok, detail=None):
    result["steps"][name] = {"ok": bool(ok), **({"detail": detail} if detail is not None else {})}
    print("%-9s %s %s" % (name, "ok" if ok else "FAIL", "" if detail is None else json.dumps(detail)[:400]), flush=True)
    if not ok and "did not finish within" in json.dumps(detail):
        # What a timed-out action.run waited on (data.waiting_on: its stages and command tickets).
        def find_waits(value):
            if isinstance(value, dict):
                if "waiting_on" in value:
                    return value["waiting_on"]
                for v in value.values():
                    found = find_waits(v)
                    if found is not None:
                        return found
            return None
        print("waiting   %s %s" % (name, json.dumps(find_waits(detail))[:1500]), flush=True)
    if not ok and "did not finish within" in json.dumps(detail) and DEBUG_ROUTER:
        # A control deadline passed: the main-thread stalls since the last report say what held it.
        records = (rpc("debug.hangs", {"after": STALLS_SEEN[0]}) or {}).get("records") or []
        STALLS_SEEN[0] = max([STALLS_SEEN[0]] + [r.get("sequence", 0) for r in records])
        stalls = [{"ms": round(r.get("duration_ms", 0)), "cpu_ms": round(r.get("cpu_ms", 0)), "top": (r.get("frames") or [])[:4]}
                  for r in records if r.get("duration_ms", 0) >= 300]
        result["steps"][name]["stalls_300ms"] = stalls
        print("stalls    %s %s" % (name, json.dumps(stalls)[:1500]), flush=True)


def version_tuple(text):
    return tuple(int(p) for p in re.findall(r"\d+", text or "0")[:3])


info = plistlib.load(open(os.path.join(APP, "Contents/Info.plist"), "rb"))
bundle_id = info.get("CFBundleIdentifier", "")
min_os = info.get("LSMinimumSystemVersion", "")
guest_os = platform.mac_ver()[0]
result.update({"guest_os": guest_os, "guest_build": subprocess.run(["sw_vers", "-buildVersion"], capture_output=True, text=True).stdout.strip(),
               "bundle_id": bundle_id, "ls_minimum_system_version": min_os, "app": os.path.basename(APP)})
BINARY = os.path.join(APP, "Contents/MacOS", info.get("CFBundleExecutable", ""))


def socket_path():
    """Mirror of BuildIdentity.controlSocket (CmuxNextCompat BuildIdentity+AppNames,
    schemas/build-identity/vectors.json app_daemon): kept names where they belong to
    one build or DEV tag, else /tmp/cmux-<kind>--<fnv1a32 of the build key>.sock."""
    slug = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or None
    name = os.path.basename(APP)[:-len(".app")] if APP.endswith(".app") else ""
    shipped = slug((info.get("LSEnvironment") or {}).get("CMUX_TAG") or "")
    if shipped is None and name.startswith("cmux DEV "):
        shipped = slug(name[len("cmux DEV "):])
    kinds = [("com.cmuxterm.app.nightly", "nightly"), ("com.cmuxterm.app.staging", "staging"),
             ("com.cmuxterm.app.debug", "debug"), ("com.cmuxterm.app.rc", "rc"), ("com.cmuxterm.app", "release")]
    kind, suffix = "debug", None
    for prefix, k in kinds:
        if bundle_id == prefix:
            kind = k
            break
        if bundle_id.startswith(prefix + "."):
            kind, suffix = ("debug" if k == "release" else k), bundle_id[len(prefix) + 1:]
            break
    suffix_tag = slug(suffix) if suffix is not None else None
    owned = ("nightly", "rc", "staging", "cmux-next")
    if bundle_id == "com.cmuxterm.app.debug":
        if shipped and shipped not in owned:
            return "/tmp/cmux-debug-%s.sock" % shipped
    elif shipped is None or shipped == suffix_tag:
        if bundle_id == "com.cmuxterm.app":
            return os.path.expanduser("~/.local/state/cmux/cmux.sock")
        if bundle_id in ("com.cmuxterm.app.nightly", "com.cmuxterm.app.rc", "com.cmuxterm.app.staging"):
            return "/tmp/cmux-%s.sock" % kind
        if bundle_id == "com.cmuxterm.app.nightly.cmux-next":
            return "/tmp/cmux-nightly-cmux-next.sock"
        if (bundle_id.startswith("com.cmuxterm.app.debug.") and re.fullmatch(r"[a-z0-9]+(\.[a-z0-9]+)*", suffix)
                and suffix_tag not in owned):
            return "/tmp/cmux-debug-%s.sock" % suffix_tag

    def fnv(text):
        value = 0x811C9DC5
        for byte in text.encode():
            value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
        return "%08x" % value
    component = re.sub(r"[^A-Za-z0-9._]+", "-", bundle_id).strip("-.")
    key = component if component == bundle_id else "%s--%s" % (component or "unbundled", fnv(bundle_id))
    if shipped and shipped != suffix_tag:
        key += "--" + shipped
    return "/tmp/cmux-%s--%s.sock" % (kind, fnv(key))


SOCKET = socket_path()
result["socket"] = SOCKET


SLOW = []


def _grab_slow(method, started):
    """Proof only: what the app's threads and the guest's CPUs do while a request is slow."""
    if len(SLOW) >= 3 or "app" not in globals():
        return
    ps = subprocess.run(["/bin/ps", "-Ao", "pcpu=,pri=,comm=", "-r"], capture_output=True, text=True).stdout.splitlines()[:6]
    policy = []
    path = "/tmp/wk-slow-%d.txt" % len(SLOW)
    subprocess.run(["/usr/bin/sample", str(app.pid), "1", "-mayDie", "-file", path], capture_output=True, timeout=60)
    try:
        text = open(path, errors="replace").read()
    except OSError:
        text = ""
    SLOW.append({"method": method, "at": round(time.time() - started, 2), "ps": ps, "text": text, "policy": policy})


def _heavy(text):
    out, thread, path, depth = {}, None, [], -1
    for line in text.splitlines():
        h = re.match(r"^\s+(\d+) (Thread_\d+.*)$", line)
        if h:
            if thread and path:
                out[thread] = path
            thread, path, depth = h.group(2), [h.group(1)], -1
            continue
        if thread is None:
            continue
        m = re.match(r"^(\s*[+!:| ]*)(\d+)\s+(.*)$", line)
        if not m:
            if len(path) > 1:
                out[thread] = path
            thread = None
            continue
        d = len(m.group(1))
        if d > depth:
            depth = d
            name = re.sub(r"\s+\[0x[0-9a-f]+\]$", "", m.group(3))
            name = re.sub(r"\s+\+ \d+$", "", name)
            path.append(re.sub(r"\s+\(in [^)]*\)", "", name)[:70])
        elif len(path) > 1:
            out[thread] = path
            thread = None
    if thread and len(path) > 1:
        out[thread] = path
    return out


IDLE_LEAF = ("__workq_kernreturn", "mach_msg2_trap", "kevent", "__psynch_cvwait", "semaphore_wait_trap", "read",
             "__select", "poll", "__semwait_signal", "__accept", "__recvfrom", "__ulock_wait", "start_wqthread")


def dump_slow():
    for entry in SLOW[:1]:
        print("wkslowrpc %s at %ss ncpu=%s ps: %s" % (entry["method"], entry["at"], os.cpu_count(),
              " | ".join(re.sub(r"\s+", " ", l.strip())[:14] + "~" + l.strip()[-22:] for l in entry["ps"])), flush=True)

        rows = []
        for t, p in _heavy(entry["text"]).items():
            frames = p[1:]
            leaf = frames[-1].split(" ")[0]
            if leaf in ("__workq_kernreturn", "kevent64", "kevent", "mach_msg2_trap", "__select", "poll") and "cooperative" not in t:
                continue
            rows.append("wkthr  %s %s| %s" % (p[0], t[:60], " < ".join(reversed(frames[-12:]))))
        for r in rows[:22]:
            print(r[:1100], flush=True)


def rpc(method, params=None, timeout=30):
    timer = None
    if method == "action.run" and (params or {}).get("action") == "openBrowser" and ((params or {}).get("args") or {}).get("engine") == "webkit":
        timer = threading.Timer(0.7, _grab_slow, (method, time.time()))
        timer.daemon = True
        timer.start()
    try:
        return _rpc(method, params, timeout)
    finally:
        if timer:
            timer.cancel()


def _rpc(method, params=None, timeout=30):
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        conn.connect(SOCKET)
        conn.sendall((json.dumps({"id": 1, "method": method, "params": params or {}}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
        conn.close()
        reply = json.loads(buf)
        return reply.get("result") if reply.get("ok") else {"error": reply.get("error")}
    except (OSError, ValueError) as error:
        return {"error": str(error)}


def wait(predicate, seconds, alive=None, step_s=0.5):
    """Bounded poll with a deadline; stops early when `alive` turns false."""
    end = time.time() + seconds
    while time.time() < end:
        value = predicate()
        if value:
            return value
        if alive is not None and not alive():
            return None
        time.sleep(step_s)
    return None


# A release build's router drops every debug.* method; the probe then drives it
# through the public methods only and takes screenshots with screencapture.
DEBUG_ROUTER = None


def crash_frames(since, count=16):
    """The crashed thread's top frames of the app's newest crash report since `since`, or None."""
    reports = [r for r in glob.glob(os.path.expanduser("~/Library/Logs/DiagnosticReports/cmux*.ips"))
               if os.path.getmtime(r) >= since - 1]
    if not reports:
        return None
    try:
        header, body = open(max(reports, key=os.path.getmtime), errors="replace").read().split("\n", 1)
        body = json.loads(body)
        images = body.get("usedImages") or []
        thread = next((t for t in body.get("threads") or [] if t.get("triggered")), {})
        frames = []
        for frame in (thread.get("frames") or [])[:count]:
            index = frame.get("imageIndex", -1)
            image = images[index] if 0 <= index < len(images) else {}
            frames.append("%s %s" % (image.get("name", "?"), frame.get("symbol") or hex(frame.get("imageOffset", 0))))
        return {"exception": body.get("exception"), "thread": thread.get("id"), "frames": frames}
    except ValueError:
        return None  # still being written: the caller's wait reads it again
    except (OSError, KeyError, IndexError) as error:
        return {"error": str(error)}


def debug_router():
    global DEBUG_ROUTER
    if DEBUG_ROUTER is None:
        reply = rpc("debug.windows")
        DEBUG_ROUTER = isinstance(reply, dict) and "error" not in reply
        result["debug_router"] = DEBUG_ROUTER
    return DEBUG_ROUTER


def snap(name, file, kind="main"):
    """Snapshot into OUT/file: debug.window_snapshot of the `kind` window in a DEBUG build,
    a screencapture of the guest display in a release build. True when the PNG exists."""
    path = os.path.join(OUT, file)
    if debug_router():
        reply = rpc("debug.window_snapshot", {"kind": kind, "path": path}, timeout=60)
    else:
        run = subprocess.run(["/usr/sbin/screencapture", "-x", "-t", "png", path], capture_output=True, text=True, timeout=60)
        reply = {"screencapture_exit": run.returncode, "stderr": run.stderr.strip()[-300:]}
    result.setdefault("snapshots", {})[name] = {"file": file, "reply": reply}
    return os.path.exists(path) and os.path.getsize(path) > 0


def stalls_after(sequence, floor_ms):
    """Main-thread stalls (debug.hangs) after hang log `sequence`, at least `floor_ms` each,
    and the newest sequence."""
    records = (rpc("debug.hangs", {"after": sequence}) or {}).get("records") or []
    newest = max([sequence] + [r.get("sequence", 0) for r in records])
    return [{"ms": round(r.get("duration_ms", 0)), "cpu_ms": round(r.get("cpu_ms", 0)), "top": (r.get("frames") or [])[:3],
             "t_ns": r.get("start_uptime_ns")}
            for r in records if r.get("kind", "stall") == "stall" and r.get("duration_ms", 0) >= floor_ms], newest

def windows_now():
    if not os.path.exists(SOCKET):
        return None
    if debug_router():
        return (rpc("debug.windows") or {}).get("windows")
    return ((rpc("snapshot.get") or {}).get("topology") or {}).get("windows")


def terminal_count():
    if debug_router():
        return (rpc("debug.surfaces") or {}).get("live_terminals") or 0
    return (rpc("snapshot.get") or {}).get("tab_count") or 0


def write_result():
    failed = [name for name, s in result["steps"].items() if not s["ok"]]
    result["ok"] = bool(result["steps"]) and not failed
    result["failed_steps"] = failed
    with open(os.path.join(OUT, "result.json"), "w") as f:
        json.dump(result, f, indent=1)


# 1. OS gate: what LaunchServices would do with this bundle on this guest.
below_floor = bool(min_os) and version_tuple(guest_os) < version_tuple(min_os)
result["below_ls_minimum"] = below_floor
if below_floor:
    ls = subprocess.run(["/usr/bin/open", "-g", "-n", APP], capture_output=True, text=True, timeout=60)
    result["launchservices"] = {"exit": ls.returncode, "output": (ls.stdout + ls.stderr).strip()[-2000:]}
    print("launchservices (open) exit %d: %s" % (ls.returncode, result["launchservices"]["output"][:400]), flush=True)
    subprocess.run(["/usr/bin/pkill", "-f", BINARY], capture_output=True)

# 2. Launch the binary directly (the GUI-proof launch path).
if os.path.exists(SOCKET):
    os.unlink(SOCKET)
scratch = tempfile.mkdtemp(prefix="os-smoke-")
open(os.path.join(scratch, "cmux.json"), "w").write("{}\n")
open(os.path.join(scratch, "ghostty"), "w").write("")
env = {"HOME": os.environ["HOME"], "USER": os.environ.get("USER", ""), "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
       "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CMUX_NEXT_NO_ACTIVATE": "1", "CMUX_NEXT_SOCKET_MODE": "automation",
       "CMUX_NEXT_CONFIG_FILE": os.path.join(scratch, "cmux.json"), "CMUX_NEXT_GHOSTTY_CONFIG": os.path.join(scratch, "ghostty"),
       "CMUX_NEXT_TEST_WINDOW_FRAME": "40,40,1200,800"}
# The guest display before the app starts: shows what the image itself has open.
subprocess.run(["/usr/sbin/screencapture", "-x", "-t", "png", os.path.join(OUT, "baseline.png")], capture_output=True, timeout=60)
# A fresh profile for the lazy-launch check: no record of an earlier Chromium tab (ChromiumUseRecord).
USE_RECORD = os.path.expanduser("~/Library/Application Support/%s/Chromium/cmux-last-tab-opened.json" % bundle_id)
if os.path.exists(USE_RECORD):
    os.unlink(USE_RECORD)
log_path = os.path.join(OUT, "app.log")
log = open(log_path, "a")
started = time.time()
app = subprocess.Popen([BINARY], env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
alive = lambda: app.poll() is None
try:
    windows = wait(windows_now, LAUNCH_DEADLINE, alive)
    result["launch_seconds"] = round(time.time() - started, 1)
    if not alive():
        code = app.returncode
        tail = open(log_path, errors="replace").read()[-3000:]
        dyld = [l for l in tail.splitlines() if "dyld" in l or "Symbol not found" in l or "Library not loaded" in l]
        step("launch", False, {"exit": code, "signal": -code if code < 0 else None, "dyld": dyld[:6], "log_tail": tail[-1200:]})
        for report in glob.glob(os.path.expanduser("~/Library/Logs/DiagnosticReports/*.ips")):
            if os.path.getmtime(report) >= started - 1:
                subprocess.run(["cp", report, OUT])
                result["notes"].append("crash report " + os.path.basename(report))
    else:
        if not windows:
            # Alive with no window or socket: record where the main thread is (a launch hang).
            sample = os.path.join(OUT, "hang-sample.txt")
            subprocess.run(["/usr/bin/sample", str(app.pid), "5", "-file", sample], capture_output=True, timeout=60)
            result["notes"].append("hang sample " + os.path.basename(sample))
        step("launch", True, {"pid": app.pid, "seconds": result["launch_seconds"]})
        step("socket", os.path.exists(SOCKET), SOCKET)
        step("window", bool(windows), {"windows": windows} if windows else "no window before the deadline")
    if windows and alive():
        snap("launch", "launch.png")
        before = terminal_count()
        # The first window opens on Home (no tabs): a new workspace brings its terminal.
        opened = rpc("action.run", {"action": "newTab", "focus": True, "wait": True})
        grown = wait(lambda: terminal_count() > before, TAB_DEADLINE, alive)
        step("terminal", grown and "error" not in (opened or {}), {"run": opened, "count_before": before,
                                                                   "after": terminal_count()})
        # Typing: one key-down per character into the focused terminal (debug.key, no system input).
        # The typed command echoes a line and creates a file: the file proves the keys reached the
        # shell, and the snapshot after it shows the typed line and the echo in the terminal.
        marker = "/tmp/%s-typed" % TITLE
        if os.path.exists(marker):
            os.unlink(marker)
        command = "echo %s-typed; touch %s" % (TITLE, marker)
        if not debug_router():
            # No debug.key in a release build: wait for the shell prompt to paint instead.
            wait(lambda: False, 3, alive)
            snap("terminal", "terminal.png")
            result["notes"].append("release router: typing and the New Tab page need debug.* methods; skipped")
        typed = [] if not debug_router() else [rpc("debug.key", {"key": "space" if ch == " " else ch}) for ch in command]
        if debug_router():
            typed.append(rpc("debug.key", {"key": "return"}))
            typed_errors = [t for t in typed if not isinstance(t, dict) or "error" in t]
            ran = wait(lambda: os.path.exists(marker), TAB_DEADLINE, alive)
            snap("terminal", "terminal.png")
            step("typing", grown and not typed_errors and bool(ran), {"keys": len(typed), "command": command,
                                                                      "shell_ran_it": bool(ran), "errors": typed_errors[:3]})
            # A fresh profile (no Chromium tab ever opened) pays nothing at launch: Chromium is not
            # started (ChromiumWarmup warms at launch only after a Chromium tab in the last 7 days).
            # Observed past the app's 3 s launch preload delay (a bounded wait, the app does nothing).
            wait(lambda: False, max(0.0, 4.5 - (time.time() - started)), alive)
            lazy = rpc("debug.cef") or {}
            lazy_ps = [l for l in subprocess.run(["/bin/ps", "-axo", "command"], capture_output=True, text=True).stdout.splitlines() if APP in l]
            result["lazy_launch"] = {k: lazy.get(k) for k in ("state", "trigger", "likely", "preloaded", "spare_window",
                                                               "engine_start_main_ms", "footprint_mb")}
            lazy_seconds = round(time.time() - started, 1)
            result["lazy_launch"].update({"seconds_after_launch": lazy_seconds, "bundle_processes": len(lazy_ps)})
            # Past the 3 s launch preload delay: nothing started, nothing predicted, framework not mapped.
            step("lazy_launch", lazy_seconds > 4 and lazy.get("state") == "idle" and lazy.get("trigger") is None
                 and lazy.get("likely") is None and not lazy.get("preloaded"), result["lazy_launch"])
            newtab = rpc("debug.new_tab", {"action": "open_and_type", "text": ""})
            newtab_state = rpc("debug.new_tab", {"action": "state"})
            snap("newtab", "newtab.png")
            step("newtab", isinstance(newtab, dict) and "error" not in newtab and bool(newtab.get("opening")),
                 {"run": newtab, "state": newtab_state})

        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = ("<!doctype html><title>%s</title><h1>%s</h1>" % (TITLE, TITLE)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        page = "http://127.0.0.1:%d/" % server.server_address[1]
        # Before the first Chromium tab: how far Chromium's warm start got (CefInitialize and the
        # spare window at idle moments after launch), its main-thread cost, the stalls since launch.
        cef_before = rpc("debug.cef") if debug_router() else {}
        launch_stalls, cursor = stalls_after(0, 100) if debug_router() else ([], 0)
        result["prewarm"] = {k: (cef_before or {}).get(k) for k in ("state", "trigger", "likely", "initialize_ms", "spare_window",
                                                                     "engine_start_main_ms", "footprint_mb", "ready_after_launch_s")}
        result["prewarm"]["seconds_after_launch"] = round(time.time() - started, 1)
        result["prewarm"]["stalls_100ms"] = launch_stalls
        print("prewarm   %s" % json.dumps(result["prewarm"])[:1500], flush=True)
        opened = rpc("action.run", {"action": "openBrowser", "args": {"url": page}, "focus": True})
        state = wait(lambda: (lambda s: s if isinstance(s, dict) and s.get("title") == TITLE else None)(rpc("browser.page.state")),
                     TAB_DEADLINE, alive)
        # The first tab's open: every main-thread stall (50 ms watchdog threshold) from the request
        # to the page's title. Goal: none over 100 ms.
        first_stalls, cursor = stalls_after(cursor, 0) if debug_router() else ([], cursor)
        STALLS_SEEN[0] = cursor  # a later control timeout reports only the stalls after the first tab
        cef_after = rpc("debug.cef") if debug_router() else {}
        result["first_tab"] = {"max_ms": max([x["ms"] for x in first_stalls] or [0]), "total_ms": sum(x["ms"] for x in first_stalls),
                               "stalls": first_stalls, "spare_window": (cef_after or {}).get("spare_window"),
                               "trigger": (cef_after or {}).get("trigger"),
                               "engine_start_main_ms": (cef_after or {}).get("engine_start_main_ms"),
                               "steps": (cef_after or {}).get("start_steps"), "prefetch": (cef_after or {}).get("prefetch"),
                               "initialize_ms": (cef_after or {}).get("initialize_ms"), "load_ms": (cef_after or {}).get("load_ms")}
        print("firsttab  %s" % json.dumps({k: v for k, v in result["first_tab"].items() if k not in ("steps", "stalls")})[:600], flush=True)
        # One timeline: Chromium's start steps and every main-thread stall, in ms from the first step.
        timeline = [("step", m.get("name"), m.get("start_uptime_ns"), m.get("ms")) for m in (result["first_tab"]["steps"] or [])]
        timeline += [("stall", "%s cpu=%s %s" % (x["ms"], x["cpu_ms"], (x["top"] or ["?"])[0][:60]), x.get("t_ns"), x["ms"]) for x in first_stalls]
        timeline = sorted([t for t in timeline if t[2]], key=lambda t: t[2])
        origin = timeline[0][2] if timeline else 0
        print("coldsteps %s" % json.dumps([[kind, name, round((t - origin) / 1e6), round(ms or 0)] for kind, name, t, ms in timeline])[:2200], flush=True)
        # The deeper stack of each long stall (the watchdog's sample about 50 ms into it).
        long_records = [r for r in ((rpc("debug.hangs") or {}).get("records") or [])
                        if r.get("duration_ms", 0) >= 200 and any(r.get("start_uptime_ns") == x.get("t_ns") for x in first_stalls)]
        for r in long_records[:3]:
            frames = [re.sub(r"^Chromium Embedded Framework ", "CEF ", f)[:90] for f in (r.get("frames") or [])[:28]]
            print("coldstack %d ms cpu %d: %s" % (r.get("duration_ms", 0), r.get("cpu_ms", 0), " < ".join(frames))[:2600], flush=True)
        step("browser", bool(state) and "error" not in (opened or {}),
             {"run": opened, "state": state or rpc("browser.page.state"),
              "cef": rpc("debug.cef") if debug_router() else None})
        if not result["steps"]["browser"]["ok"] and debug_router():
            # The run's artifacts are deleted with the VM: the step log keeps Chromium's state.
            diag = rpc("debug.cef") or {}
            print("cefdiag   %s" % json.dumps({k: v for k, v in diag.items() if k not in ("devtools", "start_steps", "pump", "windows")})[:1200], flush=True)
            steps = diag.get("start_steps") or []
            origin = min([m.get("start_uptime_ns") or 0 for m in steps] or [0])
            print("cefsteps  %s" % json.dumps([[m.get("name"), round(((m.get("start_uptime_ns") or 0) - origin) / 1e6), round(m.get("ms") or 0)]
                                               for m in sorted(steps, key=lambda m: m.get("start_uptime_ns") or 0)])[:1500], flush=True)
        step("snapshot", snap("browser", "window.png"), result["snapshots"].get("browser"))
        # The engine's own frame of the page (no screen capture), then the screen again 5 s
        # later: a page the engine rendered but the window never showed is a compositing bug.
        page_shot = rpc("browser.page.screenshot", {}, timeout=60)
        if isinstance(page_shot, dict) and page_shot.get("png_base64"):
            import base64
            open(os.path.join(OUT, "browser-engine-frame.png"), "wb").write(base64.b64decode(page_shot["png_base64"]))
        elif isinstance(page_shot, dict) and page_shot.get("path") and os.path.exists(page_shot["path"]):
            subprocess.run(["cp", page_shot["path"], os.path.join(OUT, "browser-engine-frame.png")])
        result["browser_engine_frame"] = {k: v for k, v in (page_shot or {}).items() if k != "png_base64"}
        wait(lambda: False, 5, alive)
        snap("browser_5s", "window-5s.png")
        # Cmd-T again while the last opening still holds its keys: the new opening releases the
        # old hold inside the key's action scope, so its objects deinit inline there. With
        # Chromium loaded (its allocator checks every free), macOS 15.4 to 15.x trapped here when
        # that scope's task local lived in the thread's fallback storage (NEXT 3889579838401,
        # macOS 15.8.1, 2026-10-16). Each debug.key runs from a run-loop block, as a real key does.
        if debug_router():
            burst = [rpc("debug.key", {"key": "t", "modifiers": ["cmd"]}) for _ in range(6)]
            wait(lambda: not alive(), 3, step_s=0.5)
            # Each key must reach the router as the New Tab action, or the step proves nothing.
            burst_errors = [b for b in burst if not isinstance(b, dict) or "error" in b
                            or b.get("handled_by") != "app" or not str(b.get("action") or "").startswith("newTab")]
            # A crash's report names the trap (the run's artifacts are deleted with the VM).
            crash = None if alive() else wait(lambda: crash_frames(started), 30, step_s=1)
            step("newtab_burst", alive() and not burst_errors,
                 {"keys": len(burst), "alive": alive(), "errors": burst_errors[:3],
                  "exit": None if alive() else app.returncode, "routed": [(b or {}).get("action") for b in burst],
                  "crash": crash})
        # Settings is a page tab of the main window; focus true selects it (no app activation).
        # DIAG (proof only, not for landing): the WebKit step in a loop, each after a Cmd-T burst,
        # with a sample of the daemon and the app while a reply is late.
        def heavy_paths(text, wanted):
            out, thread, path, depth = {}, None, [], -1
            for line in text.splitlines():
                h = re.match(r"^\s+\d+ (Thread_\d+.*)$", line)
                if h:
                    if thread and path:
                        out[thread] = path
                    thread, path, depth = h.group(1), [], -1
                    if not any(w in thread for w in wanted):
                        thread = None
                    continue
                if thread is None:
                    continue
                m = re.match(r"^(\s*[+!:| ]*)(\d+)\s+(.*)$", line)
                if not m:
                    if path:
                        out[thread] = path
                    thread = None
                    continue
                d = len(m.group(1))
                if d > depth:
                    depth = d
                    name = re.sub(r"\s+\[0x[0-9a-f]+\]$", "", m.group(3))
                    name = re.sub(r"\s+\+ \d+$", "", name)
                    path.append((m.group(2), name[:110]))
                elif path:
                    out[thread] = path
                    thread = None
            if thread and path:
                out[thread] = path
            return out

        def sample_pid(pid, tag, label):
            f = os.path.join(OUT, "sample-%s-%s.txt" % (tag, pid))
            subprocess.run(["/usr/bin/sample", str(pid), "1", "-mayDie", "-file", f], capture_output=True, timeout=90)
            try:
                return open(f, errors="replace").read()
            except OSError:
                return ""

        def daemon_pids():
            ps = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
            rows = []
            for l in ps.splitlines():
                parts = l.strip().split(None, 1)
                if len(parts) != 2 or int(parts[0]) == app.pid or APP not in parts[1]:
                    continue
                if "Helper" in parts[1] or "__terminal-host" in parts[1] or "WebKit" in parts[1]:
                    continue
                rows.append((int(parts[0]), parts[1].replace(APP, "APP")[-150:]))
            return rows

        DIAG = []
        LATE = []
        def webkit_attempt(i):
            url = page + "webkit%d" % i
            done = threading.Event()
            box = {}
            def call():
                t0 = time.time()
                box["reply"] = rpc("action.run", {"action": "openBrowser", "args": {"url": url, "engine": "webkit"}, "focus": True})
                box["ms"] = round((time.time() - t0) * 1000)
                done.set()
            threading.Thread(target=call, daemon=True).start()
            samples = {}
            if not done.wait(0.45):
                samples["ps"] = subprocess.run(["/bin/ps", "-Ao", "pcpu=,pri=,comm=", "-r"], capture_output=True, text=True).stdout.splitlines()[:5]
                samples["app_text"] = sample_pid(app.pid, "a%d" % i, "app")
            if False and not done.wait(1.2):
                pids = daemon_pids()
                threads = [threading.Thread(target=lambda p=p: samples.__setitem__("d%d" % p, sample_pid(p, "d%d" % i, "daemon")))
                           for p, _ in pids[:4]]
                threads.append(threading.Thread(target=lambda: samples.__setitem__("app", sample_pid(app.pid, "a%d" % i, "app"))))
                ns = subprocess.run(["/usr/sbin/netstat", "-f", "unix", "-n"], capture_output=True, text=True).stdout
                for t in threads: t.start()
                ns2 = subprocess.run(["/usr/sbin/netstat", "-f", "unix", "-n"], capture_output=True, text=True).stdout
                for t in threads: t.join(95)
                ns3 = subprocess.run(["/usr/sbin/netstat", "-f", "unix", "-n"], capture_output=True, text=True).stdout
                def queued(text):
                    rows = []
                    for l in text.splitlines():
                        f = l.split()
                        if len(f) >= 4 and f[1] == "stream" and (f[2] != "0" or f[3] != "0"):
                            rows.append(" ".join(f[1:4] + f[-1:])[-120:])
                    return rows
                samples["netstat"] = [queued(ns), queued(ns2), queued(ns3)]
                samples["pids"] = pids
            done.wait(10)
            state = wait(lambda: (lambda s: s if isinstance(s, dict) and s.get("url") == url and s.get("title") == TITLE else None)(
                rpc("browser.page.state")), 15, alive)
            reply = box.get("reply") or {}
            ok = bool(state) and "error" not in reply
            err = reply.get("error") if isinstance(reply, dict) else None
            waits = ((err or {}).get("data") or {}).get("waiting_on") if isinstance(err, dict) else None
            entry = {"i": i, "ok": ok, "ms": box.get("ms"), "waits": waits}
            print("wkloop    %s" % json.dumps(entry)[:700], flush=True)
            if not ok or (box.get("ms") or 0) > 900:
                if not ok:
                    wait(lambda: False, 20, alive)
                LATE.append("wklate    %d reply=%s state=%s" % (i, json.dumps(reply)[:500], json.dumps(rpc("browser.page.state"))[:200]))
                text = samples.get("app_text") or ""
                for t, p in heavy_paths(text, [""]).items():
                    names = [n for _, n in p]
                    idle_leaf = names[-1].startswith(("__workq_kernreturn", "mach_msg2_trap", "kevent", "__psynch_cvwait", "semaphore_wait_trap", "read ", "__select", "poll ", "__semwait_signal", "__accept", "__recvfrom", "__ulock_wait"))
                    if "main-thread" in t or ("DispatchQueue" not in t and idle_leaf):
                        continue
                    leaf = [re.sub(r"\s+\(in [^)]*\)", "", n)[:60] for n in names[-6:]]
                    LATE.append("wkstack   %d %s %s| %s" % (i, t[:46], p[0][0], " < ".join(reversed(leaf)))[:420])
                LATE.append("wkps      %d %s" % (i, " | ".join(re.sub(r"\s+", " ", l.strip())[-70:] for l in samples.get("ps") or [])))
                LATE[:] = LATE[:1] + [r for r in LATE[1:] if r.startswith(("wkstack", "wkps"))][:13] + [r for r in LATE[1:] if not r.startswith(("wkstack", "wkps"))]
                for name, keep in (("/tmp/cmux-wk-app.log", 9), ("/tmp/cmux-wk-daemon.log", 0)):
                    try:
                        rows = [r for r in open(name, errors="replace").read().splitlines()
                                if '{"data":{"generation"' not in r and "wrote reply" not in r and "writing reply" not in r
                                and "read reply" not in r and "waiter found" not in r and "drain wr" not in r]
                    except OSError as error:
                        rows = ["(%s)" % error]
                    for row in (rows[-keep:] if keep else []):
                        LATE.append("wktrace   %d %s %s" % (i, os.path.basename(name)[8:-4], row[:330]))
                logs = subprocess.run(["/usr/bin/log", "show", "--last", "4m", "--style", "compact", "--predicate",
                                       'subsystem BEGINSWITH "com.cmuxterm"'], capture_output=True, text=True).stdout.splitlines()
                hits = [l for l in logs if re.search(r"hand|connection lost|connected to cmux|rejected|failed|timed out|refus", l, re.I)]
                for l in hits[-1:]:
                    LATE.append("wklog     %d %s" % (i, l[:330]))
            if samples:
                summary = {"pids": samples.get("pids")}
                for key, text in samples.items():
                    if key in ("pids", "netstat", "ps") or not isinstance(text, str) or not text:
                        continue
                    paths = heavy_paths(text, [""])
                    if key == "app":
                        summary["app_writer"] = [t[:90] + " :: " + p[-1][1][:60] for t, p in paths.items() if "daemon.write" in t or "SocketWriter" in " ".join(n for _, n in p)]
                        summary["app_gcd"] = sum(1 for t in paths if "DispatchQueue" in t or "Thread_" in t)
                    idle = ("__workq_kernreturn", "kevent", "__psynch_cvwait", "mach_msg2_trap", "__semwait_signal", "__select", "poll", "__accept")
                    summary[key] = {t[:70]: [n for _, n in p[-7:]] for t, p in paths.items()
                                    if "main-thread" in t or "reader" in t or "dispatch" in t or not any(p[-1][1].startswith(x) for x in idle)}
                    summary[key + "_threads"] = len(paths)
                summary["netstat"] = samples.get("netstat")
                entry["samples"] = summary
                DIAG.append(entry)
            return ok

        results = [webkit_attempt(0)]
        result["steps"]["webkit"] = {"ok": results[0]}
        def relaunch_cycle(i):
            global app
            rpc("action.run", {"action": "quitEndEverything"}, timeout=10)
            try:
                app.wait(timeout=30)
            except subprocess.TimeoutExpired:
                app.send_signal(signal.SIGKILL)
                app.wait()
            if os.path.exists(USE_RECORD):
                os.unlink(USE_RECORD)
            if os.path.exists(SOCKET):
                os.unlink(SOCKET)
            app = subprocess.Popen([BINARY], env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
            t0 = time.time()
            if not wait(windows_now, LAUNCH_DEADLINE, alive):
                print("wkcycle   %d no window" % i, flush=True)
                return False
            before = terminal_count()
            rpc("action.run", {"action": "newTab", "focus": True, "wait": True})
            wait(lambda: terminal_count() > before, TAB_DEADLINE, alive)
            wait(lambda: False, max(0.0, 4.5 - (time.time() - t0)), alive)
            chrome = page + "c%d" % i
            rpc("action.run", {"action": "openBrowser", "args": {"url": chrome}, "focus": True})
            wait(lambda: (lambda st: st if isinstance(st, dict) and st.get("title") == TITLE else None)(rpc("browser.page.state")),
                 TAB_DEADLINE, alive)
            rpc("browser.page.screenshot", {}, timeout=60)
            wait(lambda: False, 5, alive)
            [rpc("debug.key", {"key": "t", "modifiers": ["cmd"]}) for _ in range(6)]
            wait(lambda: not alive(), 3, step_s=0.5)
            return webkit_attempt(i)

        for i in range(1, int(os.environ.get("OS_SMOKE_WK_CYCLES", "0")) + 1):
            if not alive():
                break
            results.append(relaunch_cycle(i))
        step("wkloop", all(results), {"runs": len(results), "failed": [i for i, r in enumerate(results) if not r]})
        if alive():
            rpc("action.run", {"action": "quitEndEverything"}, timeout=10)
            try:
                app.wait(timeout=30)
            except subprocess.TimeoutExpired:
                app.send_signal(signal.SIGKILL)
                app.wait()
        write_result()
        print("wksummary %s" % json.dumps({"runs": len(results), "failed": [i for i, r in enumerate(results) if not r]}), flush=True)
        rows = [r for r in LATE if r.startswith("wktrace") and re.search(r"perform|pool probe|request|submit", r)][-6:]
        if rows:
            print("wktl %s" % " ; ".join(r.split(" app ", 1)[-1][:120] for r in rows), flush=True)
        dump_slow()
        for entry in []:
            print("wkslow    %d ms=%s ok=%s %s" % (entry["i"], entry["ms"], entry["ok"], json.dumps(entry["waits"])[:900]), flush=True)
            samples = entry.get("samples") or {}
            print("wknet     %d %s" % (entry["i"], json.dumps(samples.get("netstat"))[:1500]), flush=True)
            print("wkwriter  %d %s gcd=%s" % (entry["i"], json.dumps(samples.get("app_writer"))[:800], samples.get("app_gcd")), flush=True)
            for key, paths in samples.items():
                if key in ("pids", "netstat", "app_writer", "app_gcd") or not isinstance(paths, dict) or key != "app" and "_" in key:
                    continue
                print("wkthreads %d %s total=%s shown=%d" % (entry["i"], key, samples.get(key + "_threads"), len(paths)), flush=True)
                for thread, frames in [(t, f) for t, f in paths.items() if key == "app" or "dispatch" in t or "out" in t][:8]:
                    print("wks %d %s | %s | %s" % (entry["i"], key, thread[:60], " < ".join(reversed(frames[-4:]))[:300]), flush=True)
            print("wkslow2   %d ms=%s ok=%s %s" % (entry["i"], entry["ms"], entry["ok"], json.dumps(entry["waits"])[:900]), flush=True)
        sys.stdout.flush()
        os._exit(0 if all(results) else 1)
        settings = rpc("settings.open", {"focus": True})
        # Wait on the page's first frame (debug.page `painted`), then snapshot.
        if debug_router():
            page_state = wait(lambda: (lambda s: s if isinstance(s, dict) and s.get("painted") else None)(
                rpc("debug.page", {"page": "cmux.settings"})), TAB_DEADLINE, alive)
        else:  # no debug.page: give the page a bounded moment to paint before the screenshot
            wait(lambda: False, 3, alive)
            page_state = {"page": "cmux.settings", "painted": None}
        settings_shot = snap("settings", "settings.png")
        step("settings", isinstance(settings, dict) and settings.get("opened") is True and bool(page_state) and settings_shot,
             {"run": settings, "page": {k: (page_state or {}).get(k) for k in ("page", "painted", "painted_ms", "language")}})
        sidebar = rpc("action.run", {"action": "toggleSidebar"})
        snap("sidebar", "sidebar.png")
        rpc("action.run", {"action": "toggleSidebar"})
        step("sidebar", isinstance(sidebar, dict) and "error" not in sidebar, {"run": sidebar})
        # Agent chat: the palette action opens a chat pane; the guest has no agent CLI, so the
        # pane shows its empty or setup state. The step proves the pane opens and paints.
        chat = rpc("action.run", {"action": "palette.newAgentChat", "focus": True})
        if debug_router():
            chat_state = wait(lambda: (lambda s: s if isinstance(s, dict) and "error" not in s else None)(
                rpc("debug.agent_pane", {"action": "chat_state"}, timeout=40)), TAB_DEADLINE, alive)
        else:
            wait(lambda: False, 3, alive)
            chat_state = {"release_router": True}
        snap("agentchat", "agentchat.png")
        step("agentchat", isinstance(chat, dict) and "error" not in chat and bool(chat_state),
             {"run": chat, "state": chat_state})
        with open(os.path.join(OUT, "surfaces.json"), "w") as f:
            json.dump(rpc("debug.surfaces") if debug_router() else rpc("snapshot.get"), f, indent=1)
        # Still alive 60 s after launch, with its helper processes.
        remaining = 60 - (time.time() - started)
        if remaining > 0:
            wait(lambda: not alive(), remaining, step_s=1)
        ps = subprocess.run(["/bin/ps", "-axo", "pid,ppid,command"], capture_output=True, text=True).stdout
        helpers = [l.strip() for l in ps.splitlines() if APP in l]
        open(os.path.join(OUT, "processes.txt"), "w").write("\n".join(helpers) + "\n")
        step("alive60", alive(), {"seconds": round(time.time() - started, 1), "bundle_processes": len(helpers)})
        snap("alive60", "alive60.png")
finally:
    if alive():
        # End everything (sessions and layout): the warm launch below then restores no tab of its own.
        quit_reply = rpc("action.run", {"action": "quitEndEverything"}, timeout=10)
        try:
            app.wait(timeout=30)
            step("quit", True, {"run": quit_reply, "exit": app.returncode})
        except subprocess.TimeoutExpired:
            app.send_signal(signal.SIGKILL)
            app.wait()
            step("quit", False, {"run": quit_reply, "detail": "no exit 30 s after quitEndEverything; SIGKILL"})
    # Warm launch: the run above opened a Chromium tab, so this launch starts Chromium and its
    # spare window at idle moments after launch (ChromiumWarmup), and the first Chromium tab opens
    # with no long main-thread stall (goal: none over 100 ms, debug.hangs).
    if (result.get("debug_router") and result["steps"].get("browser", {}).get("ok") and result["steps"].get("quit", {}).get("ok")
            and not alive()):
        if os.path.exists(SOCKET):
            os.unlink(SOCKET)
        app = subprocess.Popen([BINARY], env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
        warm_started = time.time()
        try:
            if wait(windows_now, LAUNCH_DEADLINE, alive):
                ready = wait(lambda: (lambda c: c if isinstance(c, dict) and c.get("spare_window") == "live" else None)(rpc("debug.cef")),
                             TAB_DEADLINE, alive)
                cef_warm = rpc("debug.cef") or {}
                warm_ps = [l for l in subprocess.run(["/bin/ps", "-axo", "command"], capture_output=True, text=True).stdout.splitlines() if APP in l]
                warm_stalls, cursor = stalls_after(0, 100)
                result["warm_launch"] = {k: cef_warm.get(k) for k in ("state", "trigger", "likely", "initialize_ms", "spare_window",
                                                                       "engine_start_main_ms", "footprint_mb", "ready_after_launch_s")}
                result["warm_launch"].update({"ready_seconds": round(time.time() - warm_started, 1), "bundle_processes": len(warm_ps),
                                              "stalls_100ms": warm_stalls})
                step("warm_launch", bool(ready) and cef_warm.get("trigger") == "launch", result["warm_launch"])
                opened = rpc("action.run", {"action": "openBrowser", "args": {"url": page + "warm"}, "focus": True})
                state = wait(lambda: (lambda st: st if isinstance(st, dict) and st.get("title") == TITLE else None)(rpc("browser.page.state")),
                             TAB_DEADLINE, alive)
                warm_tab, cursor = stalls_after(cursor, 0)
                result["warm_first_tab"] = {"max_ms": max([x["ms"] for x in warm_tab] or [0]), "total_ms": sum(x["ms"] for x in warm_tab),
                                            "stalls": warm_tab, "spare_window": (rpc("debug.cef") or {}).get("spare_window"), "run": opened}
                step("warm_tab", bool(state) and "error" not in (opened or {}), result["warm_first_tab"])
                print("warmtab100 %s max_ms=%s" % ("ok" if result["warm_first_tab"]["max_ms"] < 100 else "OVER",
                                                    result["warm_first_tab"]["max_ms"]), flush=True)
            else:
                step("warm_launch", False, "no window before the deadline")
        finally:
            if alive():
                rpc("action.run", {"action": "quitEndEverything"}, timeout=10)
                try:
                    app.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    app.send_signal(signal.SIGKILL)
                    app.wait()
    # Crash reports of any process (app, helpers, daemons) since the launch.
    for report in glob.glob(os.path.expanduser("~/Library/Logs/DiagnosticReports/*.ips")) + glob.glob("/Library/Logs/DiagnosticReports/*.ips"):
        if os.path.getmtime(report) >= started - 1 and not os.path.exists(os.path.join(OUT, os.path.basename(report))):
            subprocess.run(["cp", report, OUT])
            result["notes"].append("crash report " + os.path.basename(report))
    result["crash_reports"] = [n for n in result["notes"] if n.startswith("crash report")]
    with open(os.path.join(OUT, "unified.log"), "w") as f:
        subprocess.run(["/usr/bin/log", "show", "--style", "compact", "--start",
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started - 1)),
                        "--predicate", 'process CONTAINS "cmux" OR process CONTAINS "ghostty" OR senderImagePath CONTAINS "cmux"'],
                       stdout=f, stderr=subprocess.STDOUT, timeout=120)
    # Privacy: which TCC services the app asked for during the run (none expected; the early
    # LocalAuthentication ask must not be one).
    try:
        tcc = subprocess.run(["/usr/bin/log", "show", "--style", "compact", "--start",
                              time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started - 1)),
                              "--predicate", 'subsystem == "com.apple.TCC" AND eventMessage CONTAINS "%s"' % (bundle_id or "-none-")],
                             capture_output=True, text=True, timeout=120).stdout
    except subprocess.TimeoutExpired:
        tcc = "log show timed out"
    services = sorted(set(re.findall(r"kTCCService\w+", tcc)))
    result["tcc_services"] = services
    print("tcc       %s" % json.dumps({"services": services, "lines": len([l for l in tcc.splitlines() if bundle_id in l])}), flush=True)
    # Which process opened any other app (System Settings, a browser) during the run.
    with open(os.path.join(OUT, "launches.log"), "w") as f:
        subprocess.run(["/usr/bin/log", "show", "--style", "compact", "--start",
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started - 1)),
                        "--predicate", 'subsystem == "com.apple.launchservices" AND (eventMessage CONTAINS "LAUNCH" OR eventMessage CONTAINS "open")'],
                       stdout=f, stderr=subprocess.STDOUT, timeout=120)
    # dyld load check of every Mach-O in the bundle on this OS: an executable is started for
    # at most 3 s (dyld binds every strong symbol before main), a dylib is dlopen'ed. A
    # missing library or symbol prints "dyld[...]: Library not loaded / Symbol not found".
    loads = {"checked": 0, "failures": []}
    for root, _dirs, names in os.walk(APP):
        for name in names:
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            try:
                with open(path, "rb") as handle:
                    head = handle.read(32)
                    if head[:4] == b"\xca\xfe\xba\xbe":  # fat: the first slice's header
                        handle.seek(int.from_bytes(head[16:20], "big"))
                        head = handle.read(32)
            except OSError:
                continue
            if head[:4] != b"\xcf\xfa\xed\xfe":
                continue
            loads["checked"] += 1
            if int.from_bytes(head[12:16], "little") == 2:  # MH_EXECUTE
                try:
                    run = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=3,
                                         env={"HOME": scratch, "PATH": "/usr/bin:/bin", "TMPDIR": scratch})
                    text = run.stderr
                except subprocess.TimeoutExpired as expired:
                    text = (expired.stderr or b"").decode(errors="replace") if isinstance(expired.stderr, bytes) else (expired.stderr or "")
            else:
                text = subprocess.run(["/usr/bin/python3", "-I", "-c", "import ctypes,sys; ctypes.CDLL(sys.argv[1])", path],
                                      capture_output=True, text=True, timeout=30).stderr
            bad = [l for l in text.splitlines() if "Library not loaded" in l or "Symbol not found" in l
                   or ("dyld[" in l and "incompatible" in l) or "built for macOS" in l]
            if bad:
                loads["failures"].append({"file": os.path.relpath(path, APP), "dyld": bad[:4]})
    step("dyld_all", loads["checked"] > 0 and not loads["failures"], loads)
    write_result()
sys.exit(0 if result["ok"] else 1)
