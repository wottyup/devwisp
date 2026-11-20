# Android browser controls, bookmark identity and download deletion

## Requirements and release category

Address the five browser requests together: put the sidebar browser entry immediately after Settings in its existing footer; move the toolbar toggle away from bottom page navigation and make it movable; prevent duplicate bookmarks for equivalent links and clean existing duplicates; allow deleting historical downloads and their files; and keep system back/edge-back inside the browser, leaving return-to-chat to an explicit button.

Target Android `0.133.0-dev.1`, versionCode `485`. Category: MINOR, because download-file deletion and movable browser controls add user capabilities. Sidebar placement, bookmark de-duplication and back navigation are fixes bundled into this release. Preserve the installed package/signing identity and historical artifacts. Commit locally; do not push or install a separate application.

## Design and behavior

- Footer order: Settings, Browser, Switch connection, Reconnect. Use the existing sidebar theme and compact button treatment.
- Use a small side handle with a 44 dp minimum touch target. Default to the middle of a side, let it be dragged to either side and vertically, and remember placement. Keep it clear of bottom navigation by default; it cannot guarantee that every webpage has an empty edge, so moving it is the escape route. It still toggles both browser rows without reserving page height.
- Match bookmarks by a canonical identity (case-normalized authority, default ports, empty root path, equivalent percent escapes and dot segments), while preserving distinct path case, queries and fragment routes. Apply the identity to adding/removing/checking and persisted-data de-duplication. Retain the original valid target URL.
- Add a separate delete action to completed/failed download rows. Require an in-app confirmation explaining that both the download record and downloaded file are removed; use DownloadManager's app-owned deletion API. Do not cancel active downloads as part of historical deletion. Handle missing entries and failed removal with visible feedback.
- Android back closes address editing or navigates to the previous WebView history entry. With no previous page it stays in the browser and can reveal hidden controls. Register browser-scoped Android 13+ back handling as well as the existing older Android handler. Explicit minimize/close actions keep their existing meaning. Do not intercept webpage horizontal scrolling with a new whole-page gesture.
- Translate new UI text into English and keep accessibility labels, disabled states, keyboard handling and theme support.

## Implementation sequence

1. Implement canonical library identity and regression tests for equivalent and distinct URLs, repeated bookmarking and legacy duplicate entries.
2. Move the sidebar entry, implement bounded/persisted side-handle dragging, and route platform back within browser history.
3. Implement confirmed historical download deletion and row state handling without changing other download behavior.
4. Update version and concise changelog; build offline, run existing plus targeted unit tests, and inspect package/version/signature.
5. Archive a new versioned APK without overwriting, exercise update selection/download with fixture authentication, record limitations, clean temporary files and commit locally.

## Verification

- Implemented all five requests. The sidebar footer now places Browser immediately after Settings. The toolbar toggle has a 26 dp visible face inside a 44 × 56 dp target, defaults to the middle of an edge, snaps to either side after dragging and remembers a bounded vertical fraction. Layout changes re-position it within the current viewport. Its small gesture-exclusion area on API 29+ allows dragging without stealing back gestures elsewhere.
- Browser back navigates WebView history before revealing collapsed controls, and never minimizes as a no-history fallback. API 33+ registration exists only while the browser is shown and is removed during cleanup. The existing explicit minimize and tab-close buttons remain available. No new whole-page swipe interceptor was added.
- Download rows expose delete only for successful/failed jobs. Confirmation names the file and explains record/file removal. The app re-queries its own record after confirmation, rejects active jobs, uses `DownloadManager.remove(id)` for owned file/record deletion, updates empty state, and reports missing records, service-query failure or removal failure. Real files were not deleted during development.
- The first offline unit run passed. After final gesture-exclusion and error-handling changes, `:app:assembleDebug :app:testDebugUnitTest` passed in 1m 25s: 12 suites, 101 tests, zero failures/errors/skips. The browser suite has 12 tests, including four new regression cases covering equivalent addresses, preserved distinct routes, legacy duplicate cleanup, repeated toggling, Unicode and IPv6 identities. Existing navigation, page-retention and library-limit tests still pass.
- Package verified by `aapt`: `com.sevencnup.codexremote`, version `0.133.0-dev.1`, versionCode `485`, minSdk 26 / targetSdk 35. `apksigner` verification passed; signer SHA-256 remains `5201a7a330e862cd428823c3b7482bd15d351f45e2debdd246121df5bc0eced1`.
- Archived using exclusive file creation: `artifacts/devwisp-android-0.133.0-dev.1-debug.apk`, 7,661,522 bytes; SHA-256 `ca467977eb876eefbff69a5699b8194e734830191a88ce06fcdefea5936d3849`. Verified that the preceding `0.132.2-dev.1` artifact's checksum is unchanged.
- Ran the actual Vite update middleware on a temporary loopback server with isolated fixture authentication: unauthenticated access rejected; latest version, filename, size and digest correct; full APK download byte-identical. The fixture server was closed, and no real pairing credentials or live chats were used.
- `adb devices -l` lists no attached device, and no emulator is available. Native edge-back, drag/rotation/keyboard interaction, theme appearance and physical DownloadManager file deletion still need phone validation. No app was installed or real chat/terminal operated.
- Category remains MINOR for the new download deletion and movable controls, bundling the other fixes. No dependencies were added and no remote push or service restart was performed.
