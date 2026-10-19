import CmuxNextCompat
import Darwin
import Foundation

/// Proof only (not for landing): timestamps of the first WebKit tab's
/// request in the app. Notes go to memory; one serial queue appends them to
/// /tmp/cmux-wk-app.log, so a note never waits on the disk.
public enum WkDiag {
    private static let pending = Mutex<[String]>([])
    private static let flusher = DispatchQueue(label: "com.cmuxterm.next.wkdiag.flush", qos: .utility)

    public static func note(_ message: String) {
        var now = timespec()
        clock_gettime(CLOCK_REALTIME, &now)
        let ms = Int64(now.tv_sec) * 1000 + Int64(now.tv_nsec) / 1_000_000
        let line = "\(ms) qos=\(qos_class_self().rawValue) \(message)\n"
        pending.withLock { $0.append(line) }
        flusher.async { flush() }
    }

    private static func flush() {
        let lines = pending.withLock { lines -> [String] in
            defer { lines.removeAll() }
            return lines
        }
        guard !lines.isEmpty else { return }
        let fd = Darwin.open("/tmp/cmux-wk-app.log", O_WRONLY | O_CREAT | O_APPEND, 0o644)
        guard fd >= 0 else { return }
        let text = lines.joined()
        _ = text.withCString { Darwin.write(fd, $0, strlen($0)) }
        Darwin.close(fd)
    }

    static func mentions(_ data: Data, _ needle: String) -> Bool {
        data.range(of: Data(needle.utf8)) != nil
    }
}
