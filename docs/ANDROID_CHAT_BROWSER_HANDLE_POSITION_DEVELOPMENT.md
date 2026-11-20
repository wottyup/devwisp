# Chat browser handle placement

## Requirement and classification

Let the minimized browser handle on the chat page be repositioned like the browser toolbar handle. Support vertical dragging, snapping to either edge and remembering the position. Keep tap-to-restore, long-press page-list access, flat styling and the browser's existing saved handle location.

This is a PATCH usability/consistency correction to an existing shortcut; the task of restoring and managing browser pages is unchanged. Target Android `0.133.2-dev.1`, versionCode `487`. Preserve historical APKs and the installed package/signing identity; commit locally without pushing.

## Implementation steps

1. Move edge-drag interaction into the shared handle component. Preserve native tap/long-press handling and cancel native press callbacks when a drag starts. Keep the current browser handle preference keys and behavior.
2. Enable placement for the chat handle using independent preferences. Default to its current position, clamp movement within safe screen edges and above the composer/keyboard, and re-position on root/composer layout changes. Point its restore chevron inward on either edge.
3. Protect active handle gestures from the chat's sidebar/workspace swipe handler and protect the small handle area from Android edge-back. Remove layout listeners when the dock is removed.
4. Increment the patch version and summarize the change in the changelog. Build offline, run the existing Android tests and verify APK identity/signature.
5. Archive a new APK without overwriting, verify update selection/full download with isolated fixture authentication, record validation limits, clean temporary files and make a local commit.

## Validation record

- Shared `BrowserEdgeHandle` now owns drag handling, snap-to-edge positioning and preference persistence. Chat placement uses `browser_chat_handle_*`; browser controls retain `browser_handle_*`, including previously saved positions. Chat defaults to its former location until the user moves it.
- Native tap/long-press handling receives normal down/up events. Crossing the drag threshold sends a native cancel to clear pending tap/long-press callbacks; drag release/cancellation is consumed appropriately. Additional pointers cancel placement without saving. Active dock gestures are excluded from the chat's custom sidebar/workspace swipe recognition, and API 29+ edge-back exclusion applies only to the handle area.
- Chat bounds use safe screen sides/top and the area above the composer. Root and composer layout listeners recalculate placement after keyboard/composer size changes and are both removed with the dock. The stored vertical fraction is bounded and normalized; the restore chevron points inward after switching sides. Press/focus feedback remains confined to the flat handle face.
- Offline `:app:assembleDebug :app:testDebugUnitTest` passed in 1m 24s: 12 suites, 101 tests, zero failures/errors/skips. No new tests were added for this reversible shortcut-positioning change. Gesture and layout paths were reviewed in code, not exercised on a device.
- Verified APK identity/signing: package `com.sevencnup.codexremote`, version `0.133.2-dev.1`, versionCode `487`, minSdk 26 / targetSdk 35. Signer SHA-256 remains `5201a7a330e862cd428823c3b7482bd15d351f45e2debdd246121df5bc0eced1`.
- Exclusively archived `artifacts/devwisp-android-0.133.2-dev.1-debug.apk`: 7,665,106 bytes; SHA-256 `b47d3efb30e3937300a05cd1799389ec2d96809bd7488cd8b32ce6fb327ee143`. Confirmed the preceding `0.133.1-dev.1` artifact's checksum is unchanged.
- Exercised the actual Vite update middleware with isolated fixture authentication: unauthenticated access rejected; latest version, filename, size and checksum correct; complete APK download byte-identical. The temporary loopback server was closed; no real pairing credentials or conversations were used.
- No ADB device is attached and no emulator is available. Native dragging, long-press, keyboard/rotation behavior and persisted placement after process restart still require phone validation. No real chats/terminals were operated and no phone/QA application installed.
- Classification remains PATCH: position adjustment makes the existing restore shortcut consistent and more usable. No dependencies, remote push or service restart were needed.
