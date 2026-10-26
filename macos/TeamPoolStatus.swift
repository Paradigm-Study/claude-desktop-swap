import AppKit
import Darwin
import Foundation

// This process reads local operational metadata only. Authentication and rotation
// belong to the independent team client LaunchAgent.
private let teamService = "study.paradigm.claude-desktop-swap.team"
private let stateURL = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".config/claude-desktop-swap/team.json")

private struct PoolStatus {
    var heading = "Checking shared pool…"
    var detail = "Native subscription mode"
    var account: String?
    var syncedAt: Date?
    var expiresAt: Date?
    var needsAttention = false
}

private func timestamp(_ value: Any?) -> Date? {
    guard let number = value as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID(),
          number.doubleValue.isFinite, number.doubleValue > 0 else { return nil }
    return Date(timeIntervalSince1970: number.doubleValue)
}

private func safeLabel(_ value: Any?) -> String? {
    guard let value = value as? String else { return nil }
    let cleaned = value.components(separatedBy: .controlCharacters).joined(separator: " ")
        .trimmingCharacters(in: .whitespacesAndNewlines)
    return cleaned.isEmpty ? nil : String(cleaned.prefix(160))
}

private func serviceRunning() -> Bool {
    let task = Process()
    let output = Pipe()
    task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    task.arguments = ["print", "gui/\(getuid())/\(teamService)"]
    task.standardOutput = output
    task.standardError = FileHandle.nullDevice
    do {
        try task.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        guard task.terminationStatus == 0,
              let result = String(data: data, encoding: .utf8) else { return false }
        return result.split(separator: "\n").contains {
            $0.trimmingCharacters(in: .whitespaces) == "state = running"
        }
    } catch { return false }
}

private func readStatus() -> PoolStatus {
    let running = serviceRunning()
    var status = PoolStatus()
    let state: [String: Any]
    do {
        let attributes = try FileManager.default.attributesOfItem(atPath: stateURL.path)
        guard attributes[.type] as? FileAttributeType == .typeRegular,
              let size = attributes[.size] as? NSNumber, size.intValue <= 1_048_576,
              let value = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as? [String: Any]
        else { throw CocoaError(.fileReadCorruptFile) }
        state = value
    } catch {
        status.heading = "Shared pool is not ready"
        status.detail = FileManager.default.fileExists(atPath: stateURL.path)
            ? "Local status could not be read" : "Enroll this Mac to join the pool"
        status.needsAttention = true
        return status
    }

    status.account = safeLabel(state["account_label"]) ?? safeLabel(state["account_email"])
    status.syncedAt = timestamp(state["synced_at"])
    status.expiresAt = timestamp(state["expires_at"])
    let mode = state["state"] as? String ?? "unknown"
    let reason = state["reason"] as? String ?? ""
    let details = [
        "native_setup_or_policy": "Native setup needs attention",
        "native_credentials_changed": "Native sign-in changed outside the pool",
        "partial_native_write": "Interrupted sign-in update needs recovery",
        "coordinator_unavailable": "Shared coordinator is unavailable",
        "quota_stale": "Waiting for fresh account usage",
        "quota_exhausted": "All available pool accounts have reached the quota threshold",
        "device_revoked": "This Mac’s pool access was revoked",
        "keychain_access_required": "Keychain approval is needed in Terminal",
        "native_store_capacity": "Native credential data exceeds the supported writer size",
        "recovery_retained": "Pool access ended; local recovery is available",
    ]
    if mode == "revoked" || mode == "left" {
        status.heading = "Shared pool access ended"
        status.detail = details[reason] ?? "Enroll this Mac again to join the pool"
    } else if state["pending_write"] != nil || mode == "paused" {
        status.heading = "Automatic switching paused"
        status.detail = details[reason] ?? "A sign-in update needs attention"
    } else if !running {
        status.heading = "Automatic switching paused"
        status.detail = "Shared pool background service is stopped"
    } else if let expiry = status.expiresAt, expiry <= Date() {
        status.heading = "Shared sign-in expired"
        status.detail = "Waiting for the background service to renew access"
    } else if mode == "enrolled" || state["managed"] as? Bool != true {
        status.heading = "Shared pool setup incomplete"
        status.detail = "Complete the first native subscription sync"
    } else if mode == "degraded" {
        status.heading = "Shared pool needs attention"
        status.detail = details[reason] ?? "Background sync has reported a problem"
    } else if let synced = status.syncedAt,
              Date().timeIntervalSince(synced) <= max(180, (state["renew_after"] as? Double ?? 60) * 3),
              status.expiresAt != nil, mode == "active" {
        status.heading = "Automatic switching active"
        status.detail = "Shared pool · Native subscription mode"
        return status
    } else {
        status.heading = "Shared pool sync is overdue"
        status.detail = "The background service has not completed a recent sync"
    }
    status.needsAttention = true
    return status
}

final class StatusApp: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var item: NSStatusItem!
    private var timer: Timer?
    private var refreshing = false
    private var current = PoolStatus()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "Claude Swap"
        item.button?.imagePosition = .imageLeading
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        render()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    func menuWillOpen(_ menu: NSMenu) { refresh() }

    @objc private func refresh() {
        guard !refreshing else { return }
        refreshing = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let value = readStatus()
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.current = value
                self.refreshing = false
                self.render()
            }
        }
    }

    private func line(_ text: String, tooltip: String? = nil) {
        let row = NSMenuItem(title: text, action: nil, keyEquivalent: "")
        row.isEnabled = false
        row.toolTip = tooltip
        item.menu?.addItem(row)
    }

    private func render() {
        item.menu?.removeAllItems()
        item.button?.image = NSImage(systemSymbolName: current.needsAttention
            ? "exclamationmark.circle" : "arrow.triangle.2.circlepath", accessibilityDescription: current.heading)
        item.button?.image?.isTemplate = true
        item.button?.toolTip = current.heading
        line(current.heading)
        line(current.detail)
        if let account = current.account { line("Account: \(account)") }
        item.menu?.addItem(.separator())
        let dates = DateFormatter()
        dates.dateStyle = .medium
        dates.timeStyle = .short
        if let synced = current.syncedAt {
            let relative = RelativeDateTimeFormatter()
            relative.unitsStyle = .full
            line("Last sync: \(relative.localizedString(for: synced, relativeTo: Date()))",
                 tooltip: dates.string(from: synced))
        } else { line("Last sync: not yet completed") }
        line(current.expiresAt.map { "Access expires: \(dates.string(from: $0))" } ?? "Access expires: unavailable")
        item.menu?.addItem(.separator())
        for (title, action) in [("Refresh Status", #selector(refresh)),
                                ("Shared Pool Setup Guide…", #selector(openGuide)),
                                ("Quit Menu Bar", #selector(quit))] {
            let row = NSMenuItem(title: title, action: action, keyEquivalent: "")
            row.target = self
            item.menu?.addItem(row)
        }
    }

    @objc private func openGuide() {
        NSWorkspace.shared.open(URL(string: "https://github.com/Paradigm-Study/claude-desktop-swap/blob/codex/team-pool/docs/cloudflare-pool.md")!)
    }

    @objc private func quit() { NSApp.terminate(nil) }
}

let delegate = StatusApp()
let application = NSApplication.shared
application.delegate = delegate
application.run()
