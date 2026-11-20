# Android embedded browser UI refinement

## Requirement and classification

Upgrade the embedded browser's basic widget appearance while preserving Devwisp's own theme and the existing full-screen, bottom-controls layout. This is a PATCH usability and presentation improvement: browsing, tabs, bookmarks, history, downloads, address editing and toolbar collapse retain their existing purpose and behavior.

Target Android version: `0.132.2-dev.1`; independent `versionCode`: `484`. Do not push or install a separate test application. Preserve historical APKs and create a local commit when the work is verified.

## Design and scope

- Use the selected theme's surface, border, accent, text, icon and font tokens; no external dependencies or copied browser branding.
- Refine the bottom dock with a rounded address surface, clear site/title hierarchy, inline URL editing, subtle borders and consistent navigation targets. Never imply HTTP is secure.
- Keep every interactive icon target at least 44 dp. Add visible pressed, focused, selected and disabled states without changing widgets elsewhere in the app.
- Replace generic browser dialogs with themed bottom sheets, including new-page input and downloads; unify action rows, tab/library entries, active-page indication and useful empty states.
- Retain full-screen content and both controls-collapse and whole-browser-minimize behaviors, WebView instances, page state and address drafts.
- Refine loading and failure states, and provide Chinese/English copy for new interface text. Preserve light, dark and special theme support.

## Implementation steps

1. Add browser-scoped drawable/control helpers and refine the bottom dock and failure state.
2. Restyle shared browser sheets, new-page input, downloads, action rows and tab/bookmark/history entries; handle empty libraries after deletion.
3. Review keyboard, URL editing, narrow widths, enlarged fonts, disabled/selected states and collapsed controls in code. Update Android version and concise release history.
4. Run the offline Android build and existing unit tests. Verify APK package, version and signing certificate; archive under a new canonical versioned filename.
5. Verify the update middleware selects and serves the new artifact using isolated fixture authentication. Record results and limitations, clean temporary files, and commit only task changes locally.

## Verification record

- Implemented the address card, 44 dp minimum icon targets, tab-count badge, themed interaction states, bottom sheets, active-tab rows, library empty states and a scrollable retry screen. Loading reserves its 2 dp track so page content does not jump. Existing WebViews, URL resolution, storage and collapse/minimize behavior remain in place.
- Reviewed URL drafts during progress/title updates, long titles/addresses, enlarged text, narrow-width navigation scrolling, editable fields and keyboard handling. API 28 accessibility heading calls are guarded for the API 26 minimum. Theme-specific icons, fonts and pixel frames continue through existing helpers. This is code review, not native visual validation.
- Offline `:app:assembleDebug :app:testDebugUnitTest` succeeded in 1m 23s: 12 suites, 97 tests, zero failures/errors/skips. No new tests were added for cosmetic details.
- APK package: `com.sevencnup.codexremote`; version `0.132.2-dev.1`; versionCode `484`; minimum SDK 26, target SDK 35. APK signature verification passed and its signer SHA-256 matches the existing install identity: `5201a7a330e862cd428823c3b7482bd15d351f45e2debdd246121df5bc0eced1`.
- Archived with exclusive creation: `artifacts/devwisp-android-0.132.2-dev.1-debug.apk`, 7,653,878 bytes; SHA-256 `7847527469f616a6eb6a2429e51014ae40a6e68f15bf72ad0a918a143b3191de`. The preceding `0.132.1-dev.1` artifact's checksum is unchanged.
- Exercised the actual Vite update middleware on a temporary loopback server with isolated fixture authentication. Verified unauthenticated rejection, latest version/filename/size/checksum and a byte-for-byte APK download. The fixture server closed; no real credentials or live phone pairing were used.
- `adb devices -l` returned no attached devices. No emulator is available. Native screenshots, touch interaction, theme appearance, rotation and keyboard behavior still need device validation; the build was not installed on a phone or in a separate QA application.
- Change category remains PATCH: this refines existing browser presentation and usability without adding a new user task. No dependencies were installed and no remote push or service restart is needed.
