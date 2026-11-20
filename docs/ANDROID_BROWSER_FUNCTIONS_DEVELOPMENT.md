# Android embedded browser functions

## Requirement and findings

Study Fluxdo's browser functions while keeping DevWisp's native floating window, selectable themes and resize handles. Fluxdo uses Flutter `flutter_inappwebview` 6.2.0-beta.3 (Android adapter 1.2.0-beta.3), backed by Android's system WebView. DevWisp already uses the same underlying engine directly. A framework or engine migration is unnecessary.

Reference: Fluxdo `pubspec.yaml`, `pubspec.lock`, `lib/services/browser_tabs.dart`, `lib/services/browser_navigation.dart`, `lib/pages/webview_page.dart`, and `docs/browser-minimized-tabs.md`. Fluxdo is GPLv3; independently implement the behavior in Kotlin without copying its Dart code, assets or UI. Forum-specific networking, cookie challenges and Rust adapters are outside this task.

## Functional scope

| Function | Existing DevWisp | Planned behavior |
| --- | --- | --- |
| Page lifetime | Dismissal destroys the page | Minimize retains the actual WebView; explicit close releases it |
| Multiple pages | New link replaces current page | Separate in-memory pages, switch/close individually or close all |
| Address entry | URLs only | HTTP(S), local addresses and text search; persisted engine selection |
| Entry | Chat links | Also an own-style sidebar browser entry and library shortcuts in address entry |
| Library | None | Local bounded bookmarks/history with remove/clear actions |
| Page tools | Back/forward/reload/download/external | Also stop loading, find with previous/next, copy URL and share |
| Load failures | WebView default error | Main-frame error with retry; HTTP error documents remain visible |
| Downloads | DownloadManager and APK installation | Reuse the existing flow; retain the originating page's Referer when switching pages |
| Web file inputs | No chooser callback | Native system file picker with cancellation and page ownership |

Page state is kept only for the Activity lifetime. Hidden pages pause; restoring or switching never reloads them. Activity destruction or connection/UI reset releases all instances. Persist only the library and search preference, not page form contents. Keep SSL cancellation, block dangerous address schemes and keep file/content access disabled. No JavaScript bridge is exposed.

## Implementation steps

1. Add independent, testable URL/search resolution and bounded local library/session models.
2. Add native WebView page ownership, retained minimize/restore and page switching to the existing popup; preserve theme and resize behavior.
3. Add compact own-style address/navigation controls, page tools, search preferences, library and retry state. Preserve downloads/APK handling.
4. Add meaningful model regression tests; run Android unit tests and assemble the debug APK.
5. Verify package identity/signature, archive a new APK, update only the existing phone app when reachable, and record validation. Do not install a separate QA app or interact with real conversations.
6. Review changes, update version history and commit locally. Do not push.

## Classification and version

New Features / MINOR: users gain retained multiple pages, text search, a local browser library and page tools. Android target: `0.130.0-dev.1`, versionCode `476`; existing package identity is retained.

## Validation

- Implemented all scoped functions independently in native Kotlin. Existing floating frame, resize ratios, themes and package identity remain in use; no dependencies were added and Fluxdo was not modified.
- Added 8 model regression tests for HTTP(S), LAN/localhost/IPv6/IDN addresses, encoded searches and search operators, unsafe/malformed inputs, retained object identity, page capacity, library persistence, de-duplication, limits and corrupt stored data.
- Android unit tests: 86 passed, 0 failures/errors/skips. The final `:app:testDebugUnitTest :app:assembleDebug --offline --no-daemon -Pkotlin.compiler.execution.strategy=in-process` build passed. `git diff --check` passed.
- Package metadata: `com.sevencnup.codexremote`, `0.130.0-dev.1`, versionCode `476`; APK verification succeeded and the signer SHA-256 matches the previously installed `0.129.5-dev.1` package (`5201a7a330e862cd428823c3b7482bd15d351f45e2debdd246121df5bc0eced1`).
- Archived with exclusive file creation at `artifacts/devwisp-android-0.130.0-dev.1-debug.apk`; historical artifacts were not overwritten. SHA-256: `596ebbe24687acf16e30cf25466c59bf3d3b75d8d768fd086b10daec7fd5df99`.
- Updated the original app with `adb install -r` on the user-specified phone (`10.126.126.2:5555`, PJZ110). Package Manager confirms versionCode `476`, versionName `0.130.0-dev.1`. Cold launch returned `Status: ok` (937 ms), the app process was present and no AndroidRuntime errors were reported for it.
- The phone remained locked. No conversation interaction or separate QA app installation was performed. Browser form/scroll retention, actual rendering, find/share and picker interactions were not manually verified; the checklist below records the remaining device acceptance checks.
- Stopped the task-owned local test HTTP server and removed its ADB reverse mapping. No browser test pages were opened on the locked phone. Temporary build logs were removed; unrelated workspace files and existing ADB connections were retained.

### Interactive verification checklist

Use the sidebar browser entry and controlled local pages only. Check minimize/restore and page switching retain input, scroll and back history without another request; a missing subresource must leave the page visible; HTTP 500 must preserve the site's document; a failed main-frame connection must show retry. Check find next/previous, bookmark removal, history restoration, share cancellation and system file-picker cancellation. Check the popup at its minimum size and with the keyboard visible. Never submit real chat messages during browser verification.
