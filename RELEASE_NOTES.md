# v3.5.0-beta.1 — Target Tests on the ECU, in-app updates, any project file in a test build

This is a beta version. Target Tests has been run on the built-in simulator only, not yet on an ECU, and installing an update in place has not been tried on a packaged build. Install this beta by hand from the files below; later versions can be installed from inside the app.

## 🎯 Target Tests (new)
- **Architecture tests that run on the real ECU through winIDEA.** The AI drives the debugger to find out how a function is reached, and writes what worked as a short test script. You review, correct and approve it. Approved tests then run without the AI.
- **Tests are grouped into test suites, one per architecture model.** A test the AI writes goes into the suite of the model it checks. Fold a suite, tick all its tests, run it, or add a test to it. Move a test with the box next to its title.
- **Stop when a variable is read or written** (`break_on_write`, `break_on_read`), to test a variable port: run, then check who accessed it.
- **More to read on the target:** a caller's arguments (`read(expr, frame=1)`), a symbol's address, size and type, core registers and memory.
- **Call a function on the target** from a test (`call`). It needs **Allow memory writes**, like a write does.
- **The AI can check that the ECU runs the program winIDEA is set up to download,** and can flash it when you ask. Flashing needs the new **Allow flashing** switch, which is off by default.
- **Try it without an ECU:** choose **Simulator** in the connection settings.

## ⤓ In-app updates (new)
- **Download and install an update from inside the app,** on Windows and macOS. The download is checked against GitHub's SHA-256 checksum, and the app restarts into the new version. If the new version doesn't start, the old one is put back.
- **Beta versions:** in **Preferences → Updates**, choose **Beta versions** to see the list of betas. Pick one, read what's new in it, and download it. You can go back to an older version; a project saved with a newer version may not open in it.
- **Releases** stays the default and never offers a beta.

## 🧪 Test Injection: change any file of the project
- **The whole project folder is listed,** not only the C sources: makefiles, ARXML files, linker scripts. Folders are read when you unfold them.
- **Change any text file for a test project** with **✎ Change file**, for example to build without compiler optimizations. The test project keeps its own copy and the original isn't touched. A test build writes the copy over the original, builds, and puts the original back.
- **Changed Files** lists everything a test project changes in a build: the files it replaces, the files it adds and the files its hooks go into. **Compare** shows a copy next to its original.
- **You're told when an original has changed** since you changed its copy: in the list, in the pre-flight check and in the build result.

# v3.4.0 — AI Chat reads your code, VS Code style

## 💬 AI Chat answers from your code
- **Fixed: AI Chat answers rarely pointed at real code.** The chat now lets the AI read the release's source: it searches it, opens files and looks up functions and their callers before it answers. Each answer lists what it looked at.
- **Laid out like VS Code:** files, functions and your chats on the left, the code in the middle, the chat on the right. Drag the borders to resize.
- **Click a function or `file.c:123` in an answer** to open the code and highlight it. Code blocks are coloured and have a **Copy** button, and tables show as tables.
- **Ask about selection:** select code in the middle and send it to the chat.
- **Rebuild your Code Map and mind map once** if they were built before V3.3.0, so the AI gets the new structure.

## 🤖 Models, effort and context
- **One model list for all your AI accounts.** The chat remembers the last model and its settings, and the last model of each account.
- **Pick the reasoning effort** for models that support it, and the **context size**: for Ollama, the window the model runs with; for other accounts, how much of the window the chat may fill. When a long chat no longer fits, the oldest messages are left out, and the answer says so.
- **A ring next to Send shows how full the context is.** Hover over it to see what fills it.

## 💳 Credits
- **See your GitHub Copilot credits** left this month and when they reset.
- **Each chat shows what it used:** Copilot credits, tokens in and out, and the number of requests. Each answer shows its own tokens.

## 🗂️ Chat history
- **Every chat is saved,** per project and per user, encrypted on your computer. It isn't stored in the `.arch` file, so it also works in read-only projects. Open, rename or delete chats under **Chats**.
- **Edit a prompt and send it again**, optionally with another model, **roll back** the chat to a prompt, or **retry** an answer with the same or another model.

## 🧑‍💻 Agents and rules
- **Pick an agent for the chat:** *Ask*, *Test Engineer* or *Code Reviewer*, or create your own with its instructions, its tools and a preferred model.
- **Rules** are added to every request, or only when the file open in the chat matches, for example `**/*.c`. You can now also edit the project's chat rules here.
- **Agents and rules are saved in the project,** so everyone on it shares them. They use the same files as VS Code's Copilot: import them from a checkout's `.github` folder, or export them there.

## 🔐 AI accounts
- **Fixed: the saved AI keys could be lost** when the app couldn't read its credentials file. That file is now kept aside, not overwritten.

# v3.3.2 — Newer Copilot models work in chat

## 🤖 AI models
- **Fixed: some newer Copilot models failed with "HTTP 400 … not accessible via the /chat/completions endpoint".** Models such as `gpt-6-luna` only work through a newer Copilot API, and the app now uses it for them. Other models work as before.

# v3.3.1 — Your full list of AI models

## 🤖 AI models
- **Fixed: the model picker only showed an old, built-in list** for GitHub Copilot, OpenAI, Anthropic and Gemini. It now shows the models your account actually has, including the newest ones.
- **Copilot shows the same models as VS Code:** chat models your account or company allows. Models you still need to turn on in your Copilot settings are listed too.
- **The list is checked again every 10 minutes,** and right away when you sign in, change your API key or change the GitHub host. If a provider can't be reached, the built-in list is shown instead.

# v3.3.0 — Better AI mind maps, a sharper Code Map, GitHub Enterprise sign-in, a separate Sync Worker

## 🧠 AI mind maps built from your Code Map
- **The mind map now comes from the Code Map and your ports' matched functions,** not from guessing by keyword. It lists each port with its function, parameters, return type and line, what runs it, the data it uses and the most important functions around it.
- **One map per model, plus one for the whole project.** In AI Chat, pick **Mind Map: This model** or **Whole project**. The project map shows what runs each model and the calls and data shared between models. Small and self-hosted models answer better with less to read.
- **Rebuild the mind map once to get the new format.** Older maps still work until then.

## 🗺️ The Code Map reads your source better
- **Source files are now read with tree-sitter,** a real C parser. The old scan stays as the fallback for C++ files and for files it can't parse.
- **Functions that return `const` pointers are found.** The old scan missed them, for example every `*_GetState` function in the test project.
- **AUTOSAR-style macros such as `FUNC`, `P2VAR`, `P2CONST` and `STATIC` are understood,** including the macros your project defines itself.
- **Calls are listed in the order the code makes them,** not alphabetically.
- **Test-column results don't change:** reads, writes and conditions are worked out the same way as before.

## 🔐 Copilot through GitHub Enterprise
- **Sign in to Copilot with a GitHub Enterprise Cloud account (GHE.com).** Enter your company's GitHub host for Copilot under **Preferences → AI Settings**, then sign in as usual.
- **GitHub Enterprise Server isn't supported,** because it doesn't offer Copilot. The app now tells you so instead of failing quietly.

## 🔄 Sync Worker
- **The Sync Worker is now its own small program,** shown as **Architecture Validator Pro Sync** in Task Manager, next to the app. It starts faster and uses less memory.
- **It has its own icon,** with a sync badge, and the app has a new icon too.
- **Sync now shows that it's checking,** with the tray icon turning for a few seconds, and then tells you when nothing changed.
- **Fixed: a shared project could stay locked** when opening an encrypted one was stopped, for example at the master password prompt. Others couldn't open it until the lock timed out.
- **Lock messages show the time in your time zone,** not UTC.

## ⚠️ Known issue
- **The update pill can stay yellow** when GitHub's limit on update checks is used up. Hover over it to see why. It doesn't affect the app, and it's planned for a later release.

# v3.2.2 — Test Injection fixes, search, new guides

## 🪝 Hooks land where you put them
- **Fixed: a hook could land in the wrong place and still show exact.** A line such as `{` or `}` appears all over a file, so a hook could end up after the first one. Hooks now remember 3 lines of code on each side, and blank lines, comments and indentation don't count.
- **Hooks you already have keep working.** One that can't be told apart from another place now shows **fuzzy** instead of **exact**, so check those in the preview.
- **The source view is read-only in Edit mode.** Typing there was never saved, and it could shift the place of the next hook.
- **Hook at cursor asks you to click a line first,** so a hook can't land on line 1 by accident.

## 🔍 Find your code
- **Search the source by function or file name.** Picking a function opens its file at that line.
- **Folders start closed.**

## 📁 Your own test files
- **The app asks where they go in the source tree,** shows the path each file will get, and warns you if a file would replace a source file.
- **Remove a test file** with the ✕ that appears when you hover over it.
- **A test file that replaces a hooked source file is now reported as a conflict.** Before, those hooks were lost without a word.

## 🔨 Building
- **`make 0` runs from your make script's folder.** Before, with no working directory set, it ran from the app's install folder and `make` wasn't found. Your existing make script setting keeps working.
- **Preferences → Paths is now Build folder and Build command.** You no longer have to pick a script, and you can enter a folder or a script path.
- **Settings reach the Build Console straight away,** and new test builds start from them.
- **Press Enter in the command box to build,** or click **Terminal** to open an empty terminal in the build folder.
- **Command Prompt is the default terminal on Windows** for new projects, and commands with quoted paths now work there.
- **Fixed: the Build Console slid under the source view** after collapsing and expanding it.

## 📖 Guides
- **New guide pages for Test Builds and Shared Projects & Sync,** and a rewritten Test Injection page.
- **New in-app tutorials** for Test Builds and shared projects, under **Preferences → Tutorials**, and an updated Test Injection tutorial.

## ⚠️ Known issue
- **The update pill can stay yellow** when GitHub's limit on update checks is used up. Hover over it to see why. It doesn't affect the app, and it's planned for a later release.

# v3.2.1 — Sync Worker fixes for Windows, clearer progress, update check

## 🪟 Sync Worker window
- **The window opens from the tray on Windows.** It now does the same Windows start-up as the app and has its own WebView2 profile.
- **Opening it twice brings the open window forward,** and a window that crashed can't stop the next one from opening.
- **The worker and its window keep log files,** `sync-worker.log` and `sync-window.log` in the app's `sync` data folder, so a failure can be traced.

## 🔄 Clearer progress for shared projects
- **Adding a project shows every step,** and if it fails, you see why. Before, some failures showed nothing at all.
- **Sharing the open project keeps you in it.** A progress card shows each step, the app no longer drops back to the first window, and a failed share leaves your session as it was.
- **Opening a shared project for the first time shows its progress,** including on the start screen.
- **The tray icon turns while data is moving,** and so does the sync icon in the status bar.
- **Setting up a shared project is lighter on the share,** with one write where there used to be about a hundred.

## 🔔 Update check
- **The pill can't stay on Checking.** The check uses the repository's current address, has a time limit, and tries once more if it fails.
- **If the check fails, hover over the pill to see why.**

# v3.2.0 — Test Builds, shared projects on a NAS, Sync Worker

## 🔨 New: Test Builds
- **Named builds per release**, such as `Watchdog build`, set up under Test Injection → **Test Builds**. Each has its own build and clean commands, and you choose which files, folders and patterns to keep.
- **Overnight runs.** Builds run one after another, unattended. Main is built first if the release has no clean capture, a failed build doesn't stop the rest, and your source folder is put back after every build, so hooks can't reach the next one. An interrupted run can be resumed, and the machine stays awake until the run ends.
- **Switch the mapped release.** Pick a build, and the folder your debugger points at gets it. Only files that differ are transferred, files changed outside the app are never overwritten without asking, and an interrupted switch can be finished or undone. Flash from your debugger as usual.
- **Clean-release lock.** Before code changes, the app checks that the release's clean source and `.o` files are imported. You can continue anyway, which is recorded under your name, and importing later is never blocked.
- **Binaries stay on the share**, next to the project in `Project.arch.binaries/`, with each identical file stored once. Copying a project elsewhere needs that folder too.
- Test builds are for real ECU testing only. They're never compared with Main, and they never change a release's symbols, Code Map or results.

## 🔄 New: shared projects and the Sync Worker
- **Share a project on a NAS.** You work on a local copy, and only the parts that changed travel. One user edits at a time, and everyone else can keep viewing.
- **Sync Worker.** A small tray applet that keeps shared projects current in the background, even with the app closed. Turn on **Start at login** in its menu.
- **Changes are tracked in the Sync Worker,** for example `alex → Release 2.1 → Code Map`, so they don't clutter the app or the change history.
- **Safe when things go wrong.** Work that can't be published is kept as a conflict copy and never merged. A share that drops out leaves your session open, and encrypted projects sync without the master password reaching the worker.

## 🔒 Edit lock
- **A wrong clock on another PC can no longer free a lock that's in use.**
- **A heartbeat can no longer leave a half-written lock,** which another session could have treated as free.
- **A lock left behind by a crash is free after 10 minutes of silence.** With the Sync Worker running, that needs no waiting in the app.
