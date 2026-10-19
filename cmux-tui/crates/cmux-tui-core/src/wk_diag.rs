//! Proof only (not for landing): timestamps of the first WebKit tab's
//! request in the daemon, appended to /tmp/cmux-wk-daemon.log.

use std::io::Write;

pub(crate) fn note(message: &str) {
    if std::env::var_os("CMUX_WK_DAEMON_TRACE").is_none() {
        return;
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or_default();
    let thread = std::thread::current();
    let line = format!("{now} {} {message}\n", thread.name().unwrap_or("?"));
    if let Ok(mut file) =
        std::fs::OpenOptions::new().create(true).append(true).open("/tmp/cmux-wk-daemon.log")
    {
        let _ = file.write_all(line.as_bytes());
    }
}
