# 12. Test Builds

[← Test Injection](11-test-injection.md) · **Test Builds** · [Next: Shared Projects & Sync →](13-shared-projects-and-sync.md)

---

![Test Builds](../../Media/images/test_builds.png)

**Test Builds** prepares named builds for testing on a real ECU, such as `Watchdog build`, builds them one after another overnight, and swaps them into the folder your debugger points at. You keep one mapped folder instead of several copies of a release.

Open it with **Test Builds** at the top of the **Test Injection** view.

> 💡 **Preferences → Tutorials → Test Builds** walks through it on a simulated screen.

## What a test build is

A test build is a [test project](11-test-injection.md) attached to one release. Test builds aren't compared with Main or analysed. They can hold code that a normal pipeline would fail, such as a `while(1)` for a watchdog test or an MPU violation, so a build is judged only on whether its build command succeeded. They never change the release's symbols, Code Map, call graph or test results.

**Main** is the release's clean build, with no test code. It's what you switch back to.

## Set up a build

1. Select the release in the table. Builds always belong to one release, so R2 needs its own builds even if it uses the same test project.
2. Under **Builds for the selected release**, enter a name, choose a test project, and click **Add build**. A new build starts with the settings from **Preferences → Paths**.
3. Check its settings: build command, clean command, terminal, working directory, source folder and build output folder. The clean command matters: without it, objects from the previous build can end up in this one.
4. Under **Tracked files**, choose what to keep: files or folders, with a role of **Flash**, **Symbols**, **Source** or **Objects**. Folders accept patterns such as `**/*.hex`. The project-wide list applies unless a build has its own. If you don't track any Source files, the files your hooks change and your test files are kept.

Track your `.o` files with the **Objects** role. They're kept for Main only, so you can import them later from clean output.

## Clean release data first

Architecture testing needs the release's clean source and `.o` files, so they should be imported before injected code exists. If either is missing, a run or **Export** tells you which one and offers **Continue anyway**, which is recorded under your name. You might choose this because the release hasn't been built yet. Importing later is never blocked: use **Import from Main capture** so that no test code gets in. A release with no source can still build Main, but builds with hooks are skipped.

## Run overnight

Tick the builds and click **Check and run…**. The app lists anything that would fail, and you can drop those builds and start the rest. The run then:

- builds **Main** first if the release has no complete Main capture yet. If Main fails, that release's test builds are skipped, because there'd be nothing clean to switch back to
- for each build, applies its hooks, runs the clean and build commands, saves the log and captures the tracked files
- puts your source folder back after each build, so one build's hooks can't reach the next
- carries on if a build fails, and marks a build **Incomplete** if a tracked file is missing

The run holds the edit lock and keeps the machine awake. If the app or machine stops, the next start offers **Resume**, which restores the source folder and continues from the first unfinished build. On a [shared project](13-shared-projects-and-sync.md), a finished run publishes by itself.

## Switch the mapped release

Under **Mapped release**, choose the folder your debugger points at. It's saved for you on this PC, because paths differ between PCs. Pick a build and click **Switch**, and the app tells you when it's finished. Then flash from your debugger as usual.

- Only files that differ are transferred, and files the app didn't place there are left alone.
- If a file was changed outside the app, such as by a manual rebuild, you're asked before it's overwritten.
- If the debugger has a file open, the app names it. Close the debugger and switch again.
- If the NAS drops during the download, the mapped folder doesn't change.
- If a switch is interrupted while files are being replaced, the next start offers to finish it or go back.

Switching back to **Main** is the same action, and it doesn't need the edit lock. If you change the release in the table, that release's builds are listed instead, and the mapped folder stays as it is. The app warns you if it holds a build from another release.

## Where binaries are kept

Binaries are stored next to the project in `Project.arch.binaries/`, not inside it and not on each PC. Each file is stored once, so a file that's the same in Main and in a test build takes no extra space. **To copy a project elsewhere, copy this folder too.** Deleting a build keeps its files until you click **Clean up** under **Binary store**.

---

[← Test Injection](11-test-injection.md) · [Guide home](README.md) · [Next: Shared Projects & Sync →](13-shared-projects-and-sync.md)
