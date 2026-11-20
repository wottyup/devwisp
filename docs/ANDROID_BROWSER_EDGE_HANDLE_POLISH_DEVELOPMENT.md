# Android browser edge-handle polish

## Requirements and classification

Remove the oversized shadow/feedback around the browser toolbar handle shown in the supplied screenshot. Replace the chat page's floating globe with the same compact edge-handle component, flush against the edge. Translate the browser tools action “用浏览器打开” when English is selected.

This is a PATCH presentation/usability and localization correction: existing restore, toolbar collapse, page-list and browsing operations retain their purpose. Target Android `0.133.1-dev.1`, versionCode `486`. Preserve installed package/signing identity, historical APKs and existing user work. Commit locally without pushing.

## Implementation sequence

1. Introduce one browser-scoped edge-handle component shared by browser controls and the minimized chat entry. Give it a transparent 44 × 56 dp touch target with a compact 26 × 44 dp visible face. Remove elevation, translation-Z and platform state animations. Confine pressed/focused feedback to the visible face, with an accessible focus border.
2. Use the shared handle in the browser without changing its draggable placement, saved position or collapse behavior. Replace the chat globe with a restore chevron; align its face flush with the safe screen edge while retaining tap-to-restore and long-press page-list access.
3. Add the external-browser action's English translation, increment the patch version and add one concise changelog entry.
4. Build offline, run existing Android unit tests, verify APK package/version/signature, and archive a new canonical APK with exclusive creation. Check update selection and full APK download using isolated fixture authentication.
5. Record checks and device limitations, clean temporary files, inspect the diff and make a local commit.

## Verification

- The two supplied images were used as bug evidence. Code inspection found the browser handle's 3 dp face elevation plus a pressed/focused background over its larger touch wrapper. Both locations now use `BrowserEdgeHandle`: transparent wrapper, zero elevation/translation-Z, no state animator or shadow outline, and pressed/focused drawable states confined to its narrow face. Keyboard focus retains an accent border.
- The chat restore entry is a chevron handle at the safe left screen edge, above the composer. Tap-to-restore and long-press page-list behavior remain. Browser handle dragging, saved left/right/vertical placement, edge-back exclusion and toolbar collapse are retained; no navigation/storage/download logic changed.
- Added `用浏览器打开` → `Open in browser` to the existing UI dictionary used by browser action rows. The selected app language continues to determine this label.
- Offline `:app:assembleDebug :app:testDebugUnitTest` succeeded in 1m 27s: 12 suites, 101 tests, no failures/errors/skips. No new tests were added for this reversible visual/translation correction.
- Verified by `aapt`: package `com.sevencnup.codexremote`, version `0.133.1-dev.1`, versionCode `486`, minimum SDK 26 and target SDK 35. `apksigner` verification passed; signer SHA-256 remains `5201a7a330e862cd428823c3b7482bd15d351f45e2debdd246121df5bc0eced1`.
- Archived with exclusive creation: `artifacts/devwisp-android-0.133.1-dev.1-debug.apk`, 7,662,698 bytes, SHA-256 `44e4bbfc1b002859559b6f66007bc23254c886d18a27ed26445fc96be5c1f197`. The previous `0.133.0-dev.1` APK checksum is unchanged.
- The actual Vite update middleware selected the new version and served a byte-identical APK in an isolated fixture-authentication test. Unauthenticated access was rejected. The temporary loopback server was closed; no live pairing credentials or chats were used.
- `adb devices -l` returned no attached devices. Native tap/drag/focus appearance and the chat-side placement still need device validation. No separate QA application or phone installation was performed.
- Classification remains PATCH: styling and localization corrections preserve existing user tasks. No dependencies, remote push or service restart were required.
