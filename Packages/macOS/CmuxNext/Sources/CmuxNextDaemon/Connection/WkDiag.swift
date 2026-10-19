import Darwin
import Foundation

/// Proof only (not for landing): timestamps of the first WebKit tab's
/// request in the app, appended to /tmp/cmux-wk-app.log.
enum WkDiag {
    static func note(_ message: String) {
        var now = timespec()
        clock_gettime(CLOCK_REALTIME, &now)
        let ms = Int64(now.tv_sec) * 1000 + Int64(now.tv_nsec) / 1_000_000
        let line = "\(ms) qos=\(qos_class_self().rawValue) \(message)\n"
        let fd = Darwin.open("/tmp/cmux-wk-app.log", O_WRONLY | O_CREAT | O_APPEND, 0o644)
        guard fd >= 0 else { return }
        _ = line.withCString { Darwin.write(fd, $0, strlen($0)) }
        Darwin.close(fd)
    }

    static func mentions(_ data: Data, _ needle: String) -> Bool {
        data.range(of: Data(needle.utf8)) != nil
    }
}
