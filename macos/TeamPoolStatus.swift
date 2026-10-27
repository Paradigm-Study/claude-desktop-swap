import AppKit
import Darwin
import Foundation

// Local status stays read-only. Pool actions use the installed team client bridge;
// the coordinator remains the sole owner of provider refresh credentials.
private let teamService = "study.paradigm.claude-desktop-swap.team"
private let stateURL = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".config/claude-desktop-swap/team.json")

private struct PoolStatus {
    var heading = "Checking shared pool…"
    var detail = "Native subscription mode"
    var account: String?
    var accountID: String?
    var canSwitch = false
    var syncedAt: Date?
    var expiresAt: Date?
    var needsAttention = false
    var contribution: [String: Any]?
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

private func readStatus(statePath: URL = stateURL, runningOverride: Bool? = nil) -> PoolStatus {
    let running = runningOverride ?? serviceRunning()
    var status = PoolStatus()
    let state: [String: Any]
    do {
        let attributes = try FileManager.default.attributesOfItem(atPath: statePath.path)
        guard attributes[.type] as? FileAttributeType == .typeRegular,
              let size = attributes[.size] as? NSNumber, size.intValue <= 1_048_576,
              let value = try JSONSerialization.jsonObject(with: Data(contentsOf: statePath)) as? [String: Any]
        else { throw CocoaError(.fileReadCorruptFile) }
        state = value
    } catch {
        status.heading = "Shared pool is not ready"
        status.detail = FileManager.default.fileExists(atPath: statePath.path)
            ? "Local status could not be read" : "Enroll this Mac to join the pool"
        status.needsAttention = true
        return status
    }

    status.contribution = state["contribution"] as? [String: Any]
    status.accountID = state["account_id"] as? String
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
        status.canSwitch = true
        return status
    } else {
        status.heading = "Shared pool sync is overdue"
        status.detail = "The background service has not completed a recent sync"
    }
    status.needsAttention = true
    return status
}

private struct BridgeFailure: Error {
    let message: String
}

// Pipes are drained on a dedicated queue so a helper cannot block waiting for the
// menu to read. Neither credentials nor helper stderr are logged by this app.
private final class BoundedOutput: @unchecked Sendable {
    private let lock = NSLock()
    private var bytes = Data()
    private var overflow = false

    func append(_ data: Data) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if bytes.count + data.count > 1_048_576 { overflow = true; return false }
        bytes.append(data)
        return true
    }

    func result() throws -> Data {
        lock.lock()
        defer { lock.unlock() }
        if overflow { throw BridgeFailure(message: "Pool response exceeded the supported size.") }
        return bytes
    }
}

private func helperConfiguration(configurationURL: URL? = nil) throws -> String {
    guard let url = configurationURL ?? Bundle.main.url(forResource: "helper", withExtension: "json") else {
        throw BridgeFailure(message: "Update Claude Swap with the setup command to enable pool controls.")
    }
    let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
    guard attributes[.type] as? FileAttributeType == .typeRegular,
          (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
          let permissions = attributes[.posixPermissions] as? NSNumber,
          permissions.intValue & 0o022 == 0,
          let size = attributes[.size] as? NSNumber, size.intValue < 8192,
          let value = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any],
          value["version"] as? Int == 1,
          value["module"] as? String == "team_pool.menu_bridge",
          let python = value["python"] as? String,
          python.hasPrefix("/"), !python.contains("\0"),
          FileManager.default.isExecutableFile(atPath: python) else {
        throw BridgeFailure(message: "The installed pool helper needs repair. Run the setup command again.")
    }
    return python
}

private func helperTimeout(for action: String?) -> TimeInterval {
    // Native writes and OAuth exchange can include a full coordinator timeout.
    // The client separately bounds lock acquisition so this is not a queue wait.
    ["switch", "contribute-complete"].contains(action ?? "") ? 240 : 120
}

private func bridge(_ request: [String: Any], configurationURL: URL? = nil,
                    timeout: TimeInterval? = nil) throws -> [String: Any] {
    let python = try helperConfiguration(configurationURL: configurationURL)
    var input = try JSONSerialization.data(withJSONObject: request)
    input.append(0x0a)
    guard input.count <= 16_384 else { throw BridgeFailure(message: "The request is too large.") }
    let task = Process()
    let stdin = Pipe()
    let stdout = Pipe()
    let buffer = BoundedOutput()
    let exited = DispatchSemaphore(value: 0)
    let drained = DispatchGroup()
    task.executableURL = URL(fileURLWithPath: python)
    task.arguments = ["-I", "-m", "team_pool.menu_bridge"]
    task.standardInput = stdin
    task.standardOutput = stdout
    task.standardError = FileHandle.nullDevice
    var environment = ProcessInfo.processInfo.environment
    environment.removeValue(forKey: "ANTHROPIC_AUTH_TOKEN")
    environment.removeValue(forKey: "ANTHROPIC_BASE_URL")
    task.environment = environment
    task.terminationHandler = { _ in exited.signal() }
    try task.run()
    defer {
        try? stdin.fileHandleForWriting.close()
        try? stdout.fileHandleForReading.close()
        if task.isRunning { kill(task.processIdentifier, SIGKILL) }
    }
    drained.enter()
    DispatchQueue.global(qos: .utility).async {
        defer { drained.leave() }
        while let data = try? stdout.fileHandleForReading.read(upToCount: 65_536), !data.isEmpty {
            if !buffer.append(data) { task.terminate(); break }
        }
    }
    do {
        try stdin.fileHandleForWriting.write(contentsOf: input)
        try stdin.fileHandleForWriting.close()
    } catch {
        task.terminate()
        throw BridgeFailure(message: "The pool helper could not receive the request.")
    }
    input.resetBytes(in: input.startIndex..<input.endIndex)
    if exited.wait(timeout: .now() + (timeout ?? helperTimeout(for: request["action"] as? String))) == .timedOut {
        task.terminate()
        if exited.wait(timeout: .now() + 2) == .timedOut { kill(task.processIdentifier, SIGKILL) }
        throw BridgeFailure(message: "The pool request timed out. Refresh status before retrying. An interrupted sign-in update may need recovery; do not resubmit an authorization code.")
    }
    guard drained.wait(timeout: .now() + 2) == .success else {
        throw BridgeFailure(message: "The pool helper did not finish its response.")
    }
    let output = try buffer.result()
    guard let reply = try? JSONSerialization.jsonObject(with: output) as? [String: Any],
          let ok = reply["ok"] as? Bool else {
        throw BridgeFailure(message: "The pool helper returned an unreadable response. Run setup again if this continues.")
    }
    guard ok, task.terminationStatus == 0 else {
        throw BridgeFailure(message: safeLabel(reply["error"]) ?? "The pool request did not complete.")
    }
    guard let result = reply["result"] as? [String: Any] else {
        throw BridgeFailure(message: "The pool helper returned an incomplete response.")
    }
    return result
}

private let availabilityReasons = [
    "quarantined": "Needs account recovery",
    "refreshing": "Refreshing access",
    "unverified": "Account has not been verified",
    "token-expiring": "Waiting for access renewal",
    "quota-unavailable": "Usage is unavailable",
    "quota-stale": "Waiting for fresh usage",
    "quota-exhausted": "Quota threshold reached",
]

private func percentage(_ value: Any?) -> String {
    guard let value = value as? NSNumber,
          CFGetTypeID(value) != CFBooleanGetTypeID(), value.doubleValue.isFinite,
          value.doubleValue >= 0 else { return "—" }
    return String(format: "%.0f%%", value.doubleValue)
}

private func claudeAuthorizationURL(_ raw: Any?) -> URL? {
    guard let raw = raw as? String, raw.count <= 4096,
          let parts = URLComponents(string: raw), parts.scheme == "https",
          parts.host == "claude.com", parts.path == "/cai/oauth/authorize",
          parts.port == nil, parts.user == nil, parts.password == nil,
          parts.fragment == nil else { return nil }
    return parts.url
}

final class StatusApp: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var item: NSStatusItem!
    private var timer: Timer?
    private var refreshing = false
    private var bridgeBusy = false
    private var current = PoolStatus()
    private var catalog: [String: Any]?
    private var lastCatalogAttempt = Date.distantPast
    private var catalogError: String?
    private var notice: String?
    private var operation: String?
    private var contribution: [String: Any]?
    private var enteringCode = false
    private var localRevision = 0

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "Claude Swap"
        item.button?.imagePosition = .imageLeading
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        render()
        refreshLocal()
        refreshPool()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            self?.refreshLocal()
            self?.refreshPool()
        }
    }

    func menuWillOpen(_ menu: NSMenu) {
        refreshLocal()
        refreshPool()
    }

    private func refreshLocal() {
        guard !refreshing else { return }
        refreshing = true
        let revision = localRevision
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let value = readStatus()
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.refreshing = false
                guard revision == self.localRevision else {
                    self.refreshLocal()
                    return
                }
                self.current = value
                // In-flight requests own their receipt until the next local read.
                if !self.bridgeBusy { self.contribution = value.contribution }
                self.render()
            }
        }
    }

    private func request(_ action: String, fields: [String: Any] = [:],
                         progress: String, completion: @escaping (Result<[String: Any], Error>) -> Void) {
        guard !bridgeBusy else { return }
        bridgeBusy = true
        operation = progress
        render()
        var payload = fields
        payload["action"] = action
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result: Result<[String: Any], Error>
            do { result = .success(try bridge(payload)) }
            catch { result = .failure(error) }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.bridgeBusy = false
                self.operation = nil
                completion(result)
                self.render()
            }
        }
    }

    private func message(_ error: Error) -> String {
        (error as? BridgeFailure)?.message ?? "The pool helper could not run. Run the setup command again."
    }

    private func refreshPool(force: Bool = false) {
        guard !bridgeBusy, !enteringCode,
              Date().timeIntervalSince(lastCatalogAttempt) >= (force ? 3 : 60) else { return }
        lastCatalogAttempt = Date()
        request("list", progress: "Refreshing shared pool…") { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let value): self.catalog = value; self.catalogError = nil
            case .failure(let error): self.catalogError = self.message(error)
            }
        }
    }

    @objc private func refresh() {
        refreshLocal()
        refreshPool(force: true)
    }

    private func line(_ text: String, tooltip: String? = nil, in menu: NSMenu? = nil) {
        let row = NSMenuItem(title: text, action: nil, keyEquivalent: "")
        row.isEnabled = false
        row.toolTip = tooltip
        (menu ?? item.menu)?.addItem(row)
    }

    private func action(_ title: String, _ selector: Selector, enabled: Bool = true,
                        value: Any? = nil, in menu: NSMenu? = nil) {
        let row = NSMenuItem(title: title, action: selector, keyEquivalent: "")
        row.target = self
        row.isEnabled = enabled
        row.representedObject = value
        (menu ?? item.menu)?.addItem(row)
    }

    private func render() {
        guard let menu = item.menu else { return }
        menu.removeAllItems()
        menu.autoenablesItems = false
        let attention = current.needsAttention || catalogError != nil
        item.button?.image = NSImage(systemSymbolName: attention
            ? "exclamationmark.circle" : "arrow.triangle.2.circlepath", accessibilityDescription: current.heading)
        item.button?.image?.isTemplate = true
        item.button?.toolTip = current.heading
        line(current.heading)
        line(current.detail)
        if let account = current.account { line("This Mac: \(account)") }
        if let operation = operation { line(operation) }
        if let notice = notice { line(notice) }
        menu.addItem(.separator())
        if let error = catalogError {
            line(catalog == nil ? "Pool could not be loaded" : "Showing last known pool — refresh failed")
            line(error)
        }
        if let catalog = catalog, let accounts = catalog["accounts"] as? [[String: Any]] {
            let selected = current.accountID
            line("Shared accounts · % used")
            if accounts.isEmpty { line("No accounts in the pool yet") }
            for account in accounts {
                guard let id = account["id"] as? String,
                      let details = account["account"] as? [String: Any] else { continue }
                let email = safeLabel(details["emailAddress"]) ?? "Subscription account"
                let usage = account["usage"] as? [String: Any] ?? [:]
                let eligible = account["eligible"] as? Bool == true
                let isCurrent = id == selected
                let reason = account["reason"] as? String
                let available = eligible ? "Available" : availabilityReasons[reason ?? ""] ?? "Currently unavailable"
                let row = NSMenuItem(title: "\(email)  ·  5h \(percentage(usage["five_hour"]))  ·  week \(percentage(usage["seven_day"]))",
                                     action: nil, keyEquivalent: "")
                row.state = isCurrent ? .on : .off
                row.toolTip = "\(isCurrent ? "Assigned to this Mac. " : "")\(available)"
                let detail = NSMenu()
                detail.autoenablesItems = false
                line(email, in: detail)
                if let plan = safeLabel(details["subscriptionType"]) { line("Plan: \(plan)", in: detail) }
                line("5-hour usage: \(percentage(usage["five_hour"]))", in: detail)
                line("Weekly usage: \(percentage(usage["seven_day"]))", in: detail)
                for key in usage.keys.sorted() where key != "five_hour" && key != "seven_day" {
                    let label = key.replacingOccurrences(of: "seven_day_", with: "Weekly ")
                        .replacingOccurrences(of: "weekly_scoped:", with: "Weekly ")
                    line("\(String(label.prefix(80))): \(percentage(usage[key]))", in: detail)
                }
                line(available, in: detail)
                if let fetched = timestamp(account["fetched"]) {
                    let relative = RelativeDateTimeFormatter()
                    line("Usage checked \(relative.localizedString(for: fetched, relativeTo: Date()))", in: detail)
                }
                detail.addItem(.separator())
                action(isCurrent ? "Assigned to this Mac" : "Switch This Mac to This Account",
                       #selector(switchAccount(_:)), enabled: current.canSwitch && eligible && !isCurrent && !bridgeBusy && catalogError == nil,
                       value: id, in: detail)
                row.submenu = detail
                menu.addItem(row)
            }
            if let threshold = catalog["threshold"] { line("Automatic switching threshold: \(percentage(threshold))") }
            if let checked = timestamp(catalog["catalog_at"]) {
                let relative = RelativeDateTimeFormatter()
                line("Pool checked \(relative.localizedString(for: checked, relativeTo: Date()))")
            }
        } else if catalogError == nil { line("Loading shared accounts…") }
        menu.addItem(.separator())
        let contributionState = contribution?["state"] as? String ?? "none"
        if contributionState == "pending" {
            line("Account addition: awaiting Claude authorization")
            action("Enter Authorization Code…", #selector(enterAuthorizationCode), enabled: !bridgeBusy && !enteringCode)
            action("Check Account Addition", #selector(checkContribution), enabled: !bridgeBusy)
            action("Cancel Account Addition", #selector(cancelContribution), enabled: !bridgeBusy)
        } else if ["submitted", "exchanging", "uncertain"].contains(contributionState) {
            line(contributionState == "uncertain" ? "Account addition: outcome uncertain" : "Account addition: checking authorization")
            action("Check Account Addition", #selector(checkContribution), enabled: !bridgeBusy)
            if contributionState == "uncertain" {
                line("Do not reuse the previous authorization code")
                action("Start New Account Authorization…", #selector(addAccount), enabled: !bridgeBusy && !enteringCode)
            }
        } else {
            action("Add Account to Shared Pool…", #selector(addAccount), enabled: !bridgeBusy && !enteringCode)
        }
        menu.addItem(.separator())
        if let synced = current.syncedAt {
            let relative = RelativeDateTimeFormatter()
            line("This Mac synced \(relative.localizedString(for: synced, relativeTo: Date()))")
        }
        action("Refresh Pool and Status", #selector(refresh), enabled: !bridgeBusy)
        action("Shared Pool Setup Guide…", #selector(openGuide))
        action("Quit Menu Bar", #selector(quit), enabled: !bridgeBusy && !enteringCode)
    }

    @objc private func switchAccount(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String, current.canSwitch, !bridgeBusy else { return }
        notice = nil
        request("switch", fields: ["account_id": id], progress: "Switching this Mac…") { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let value):
                self.localRevision += 1
                self.current.accountID = value["account_id"] as? String
                self.current.account = safeLabel(value["account_email"])
                self.current.expiresAt = timestamp(value["expires_at"])
                self.current.canSwitch = value["state"] as? String == "active"
                    && value["write_recovery_pending"] as? Bool != true
                    && value["credential_expired"] as? Bool != true
                self.notice = "This Mac switched; automatic switching remains active"
            case .failure(let error): self.notice = self.message(error)
            }
            self.refreshLocal()
            self.lastCatalogAttempt = .distantPast
            self.refreshPool(force: true)
        }
    }

    @objc private func addAccount() {
        guard !bridgeBusy else { return }
        let alert = NSAlert()
        alert.messageText = "Add a subscription account to the shared pool?"
        alert.informativeText = "Sign into the Claude account you want to share. Team devices will be able to use its subscription quota and account permissions."
        if contribution?["state"] as? String == "uncertain" {
            alert.informativeText += " The previous attempt had an uncertain outcome. Start a fresh authorization and use only its new code."
        }
        alert.addButton(withTitle: "Continue to Claude")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        notice = nil
        request("contribute-start", progress: "Starting account authorization…") { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let value):
                self.contribution = value
                if value["state"] as? String == "pending" {
                    guard let url = claudeAuthorizationURL(value["authorization_url"]) else {
                        self.notice = "Claude returned an unrecognized sign-in URL. Cancel this addition and try again."
                        return
                    }
                    self.notice = NSWorkspace.shared.open(url)
                        ? "Complete Claude sign-in, then choose Enter Authorization Code…"
                        : "Could not open Claude sign-in. Cancel this addition and try again."
                } else { self.updateContributionNotice(value) }
            case .failure(let error): self.notice = self.message(error); self.refreshLocal()
            }
        }
    }

    @objc private func enterAuthorizationCode() {
        guard !bridgeBusy, !enteringCode,
              contribution?["state"] as? String == "pending",
              let id = contribution?["contribution_id"] as? String else { return }
        enteringCode = true
        let alert = NSAlert()
        alert.messageText = "Finish adding your Claude account"
        alert.informativeText = "Paste the complete authorization code from Claude, including #state. The code is hidden and sent directly to the shared coordinator."
        let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 420, height: 24))
        field.placeholderString = "Complete authorization code"
        field.maximumNumberOfLines = 1
        alert.accessoryView = field
        alert.addButton(withTitle: "Add Account")
        alert.addButton(withTitle: "Later")
        alert.window.initialFirstResponder = field
        NSApp.activate(ignoringOtherApps: true)
        let accepted = alert.runModal() == .alertFirstButtonReturn
        var code = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        field.stringValue = ""
        enteringCode = false
        guard accepted else { code = ""; render(); return }
        guard !code.isEmpty, code.utf8.count <= 4225 else {
            notice = "Paste the complete code from Claude. No code was submitted."
            code = ""
            render()
            return
        }
        request("contribute-complete", fields: ["contribution_id": id, "code": code],
                progress: "Adding account to shared pool…") { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let value): self.contribution = value; self.updateContributionNotice(value)
            case .failure(let error):
                // Submission can succeed even if its response is lost. Do not
                // offer resubmission until a coordinator status check resolves it.
                self.contribution = ["contribution_id": id, "state": "submitted"]
                self.notice = self.message(error)
            }
            self.refreshLocal()
            self.lastCatalogAttempt = .distantPast
            self.refreshPool(force: true)
        }
        code = ""
    }

    private func updateContributionNotice(_ value: [String: Any]) {
        let messages = ["added": "Account added to the shared pool", "duplicate": "That account is already in the shared pool",
                        "pending": "Account addition is awaiting authorization", "exchanging": "Authorization is being checked",
                        "uncertain": "Addition outcome is uncertain. Check its status or start a fresh authorization; do not reuse the code.",
                        "cancelled": "Account addition cancelled", "expired": "Authorization expired; start a new account addition",
                        "failed": "Authorization failed; start a new account addition", "none": "No account addition is pending"]
        notice = messages[value["state"] as? String ?? ""] ?? "Account addition status updated"
    }

    @objc private func checkContribution() { inspectContribution(cancel: false) }
    @objc private func cancelContribution() { inspectContribution(cancel: true) }

    private func inspectContribution(cancel: Bool) {
        request(cancel ? "contribute-cancel" : "contribute-status", progress: "Checking account addition…") { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let value): self.contribution = value; self.updateContributionNotice(value)
            case .failure(let error): self.notice = self.message(error)
            }
            self.refreshLocal()
            self.lastCatalogAttempt = .distantPast
            self.refreshPool(force: true)
        }
    }

    @objc private func openGuide() {
        NSWorkspace.shared.open(URL(string: "https://github.com/Paradigm-Study/claude-desktop-swap/blob/codex/team-pool/docs/share-org-setup.md")!)
    }

    @objc private func quit() { NSApp.terminate(nil) }
}

let delegate = StatusApp()
let application = NSApplication.shared
application.delegate = delegate
application.run()
