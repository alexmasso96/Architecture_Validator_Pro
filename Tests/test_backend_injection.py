"""
Source-Level Test Code Injection — backend integration tests.

Exercises the full router surface against a real .arch via TestClient: test
project CRUD, helper-file import, injection hooks, fuzzy resolve/shift, export
(modified + reconstruct), and the build runner / settings. The active release is
seeded with production source so the injection paths have something to anchor to.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath("src"))

from fastapi.testclient import TestClient

from backend.app import create_app
from Application_Logic.Logic_Database import ProjectDatabase
from Tests.test_helpers import make_project_db

TOKEN = "inject-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

MAIN_C = "\n".join([
    "int main(void)",
    "{",
    "    int x = 1;",
    "    int y = 2;",
    "    foo();",
    "    return 0;",
    "}",
    "",
])


@pytest.fixture()
def project_path():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "proj.arch")
        db = make_project_db(
            path,
            layout=[("TC. ID", "Static Text", True)],
            models=[{"name": "Arch_A", "status": "In Work", "rows": []}],
            releases=[{"name": "R1", "description": "first",
                       "rows": [{"TC. ID": {"text": "1"}}]}],
        )
        # Seed the active release with production source to inject into.
        rid = db.get_all_releases()[0]["id"] if hasattr(db, "get_all_releases") else None
        if rid is None:
            # Fall back: query the releases table directly.
            rid = db.execute("SELECT id FROM releases ORDER BY id LIMIT 1").fetchone()[0]
        db.save_release_source_files(rid, [("src/main.c", MAIN_C)])
        db.commit()
        db.close()
        yield path


@pytest.fixture()
def client(project_path):
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        c.post("/api/project/open", json={"path": project_path, "mode": "exclusive"},
               headers=AUTH)
        yield c


def _new_project(client, name="MyTests"):
    r = client.post("/api/injection/projects", json={"name": name}, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# -- test projects ------------------------------------------------------
def test_create_list_delete_project(client):
    pid = _new_project(client)
    listing = client.get("/api/injection/projects", headers=AUTH).json()
    assert any(p["id"] == pid and p["name"] == "MyTests"
               for p in listing["projects"])
    r = client.delete(f"/api/injection/projects/{pid}", headers=AUTH)
    assert r.status_code == 200
    listing = client.get("/api/injection/projects", headers=AUTH).json()
    assert all(p["id"] != pid for p in listing["projects"])


def test_create_empty_name_rejected(client):
    r = client.post("/api/injection/projects", json={"name": "  "}, headers=AUTH)
    assert r.status_code == 409


# -- helper-file import -------------------------------------------------
def test_import_and_read_helper_files(client):
    pid = _new_project(client)
    r = client.post(
        f"/api/injection/projects/{pid}/import",
        json={"files": [
            {"rel_path": "helpers/stub.c", "content": "int stub(void){return 0;}"},
            {"rel_path": "helpers/stub.h", "content": "int stub(void);"},
            {"rel_path": "README.md", "content": "ignored"},  # non-source skipped
        ]},
        headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 2
    files = client.get(f"/api/injection/projects/{pid}/files", headers=AUTH).json()
    names = {f["rel_path"] for f in files["files"]}
    assert names == {"helpers/stub.c", "helpers/stub.h"}
    content = client.get(
        f"/api/injection/projects/{pid}/files/content",
        params={"rel_path": "helpers/stub.h"}, headers=AUTH).json()
    assert content["content"] == "int stub(void);"


# -- production source tree --------------------------------------------
def test_source_files_and_content(client):
    files = client.get("/api/injection/source/files", headers=AUTH).json()
    assert any(f["rel_path"] == "src/main.c" for f in files["files"])
    content = client.get("/api/injection/source/content",
                         params={"rel_path": "src/main.c"}, headers=AUTH).json()
    assert "int main(void)" in content["content"]


# -- injection hooks + resolution --------------------------------------
def _add_hook(client, pid, **kw):
    body = {"src_file_path": "src/main.c", "function_name": "main",
            "line_above_code": "int x = 1;", "line_below_code": "int y = 2;",
            "injected_code": "CHECK(x);"}
    body.update(kw)
    r = client.post(f"/api/injection/projects/{pid}/injections", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["injection_id"]


def test_add_update_list_injection(client):
    pid = _new_project(client)
    iid = _add_hook(client, pid)
    rows = client.get(f"/api/injection/projects/{pid}/injections", headers=AUTH).json()
    hook = next(h for h in rows["injections"] if h["id"] == iid)
    assert hook["confidence"] == 4           # both anchors found, in order
    assert hook["resolved_index"] == 3

    # Update the snippet via the same endpoint (injection_id present).
    r = client.post(f"/api/injection/projects/{pid}/injections",
                    json={"injection_id": iid, "src_file_path": "src/main.c",
                          "function_name": "main", "line_above_code": "int x = 1;",
                          "line_below_code": "int y = 2;",
                          "injected_code": "LOG();"}, headers=AUTH)
    assert r.status_code == 200
    rows = client.get(f"/api/injection/projects/{pid}/injections", headers=AUTH).json()
    assert next(h for h in rows["injections"] if h["id"] == iid)["injected_code"] == "LOG();"


def test_resolve_conflict(client):
    r = client.post("/api/injection/resolve",
                    json={"src_file_path": "src/main.c",
                          "line_above_code": "GONE;", "line_below_code": "MISSING;"},
                    headers=AUTH)
    assert r.status_code == 200
    assert r.json()["index"] is None and r.json()["confidence"] == 0


def test_shift_within_and_out_of_function(client):
    pid = _new_project(client)
    iid = _add_hook(client, pid)  # index 3 (between x and y)
    # First shift up stays inside the function body (index 2).
    r = client.post(f"/api/injection/injections/{iid}/shift",
                    json={"direction": "up"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] and r.json()["index"] == 2
    # Keep shifting up; the block must eventually be refused at the opening brace
    # rather than escaping the function. Bound the loop so a logic bug can't hang.
    refused = False
    for _ in range(6):
        r = client.post(f"/api/injection/injections/{iid}/shift",
                        json={"direction": "up"}, headers=AUTH)
        if r.status_code == 409:
            refused = True
            break
        assert r.json()["index"] >= 1   # never lands on/above the signature line
    assert refused, "shift never hit the function boundary"


def test_delete_injection(client):
    pid = _new_project(client)
    iid = _add_hook(client, pid)
    r = client.delete(f"/api/injection/injections/{iid}", headers=AUTH)
    assert r.status_code == 200
    rows = client.get(f"/api/injection/projects/{pid}/injections", headers=AUTH).json()
    assert all(h["id"] != iid for h in rows["injections"])


# -- export -------------------------------------------------------------
def test_export_modified_only(client):
    pid = _new_project(client)
    _add_hook(client, pid, injected_code="ASSERT(x>0);")
    client.post(f"/api/injection/projects/{pid}/import",
                json={"files": [{"rel_path": "helpers/t.c", "content": "void t(){}"}]},
                headers=AUTH)
    with tempfile.TemporaryDirectory() as out:
        r = client.post(f"/api/injection/projects/{pid}/export",
                        json={"mode": "modified", "out_dir": out, "overwrite": True,
                              "continue_anyway": True},
                        headers=AUTH)
        assert r.status_code == 200, r.text
        injected = open(os.path.join(out, "src", "main.c")).read()
        assert "ASSERT(x>0);" in injected
        assert os.path.exists(os.path.join(out, "helpers", "t.c"))


def test_export_reconstruct(client):
    pid = _new_project(client)
    _add_hook(client, pid, injected_code="ASSERT(1);")
    with tempfile.TemporaryDirectory() as out:
        r = client.post(f"/api/injection/projects/{pid}/export",
                        json={"mode": "reconstruct", "out_dir": out, "overwrite": True,
                              "continue_anyway": True},
                        headers=AUTH)
        assert r.status_code == 200, r.text
        assert os.path.exists(os.path.join(out, "src", "main.c"))


# -- build settings + runner -------------------------------------------
def test_settings_roundtrip(client):
    r = client.post("/api/injection/settings",
                    json={"terminal": "bash", "build_command": "echo hi",
                          "build_cwd": "/tmp"}, headers=AUTH)
    assert r.status_code == 200
    s = client.get("/api/injection/settings", headers=AUTH).json()
    assert s["terminal"] == "bash" and s["build_command"] == "echo hi"


def test_settings_unknown_terminal_rejected(client):
    r = client.post("/api/injection/settings", json={"terminal": "fish"}, headers=AUTH)
    assert r.status_code == 409


def test_build_argv_per_terminal():
    from backend.routers.injection import _build_argv
    assert _build_argv("cmd", "make", "C:\\proj", "") == ["cmd.exe", "/c", "make"]
    assert _build_argv("powershell", "make", "", "") == \
        ["powershell.exe", "-NoProfile", "-Command", "make"]
    assert _build_argv("bash", "make", "/proj", "") == ["bash", "-lc", "make"]
    wsl = _build_argv("wsl", "make all", "/home/u/proj", "Ubuntu")
    assert wsl[:4] == ["wsl.exe", "-d", "Ubuntu", "--"]
    assert wsl[-3:] == ["sh", "-c", "cd /home/u/proj && make all"]


@pytest.mark.skipif(sys.platform == "win32", reason="bash terminal")
def test_build_runner_streams_output():
    """The build runner fans subprocess output over the ``build`` event and ends
    with a ``done`` payload carrying the return code."""
    from backend.routers import injection as inj

    captured = []

    class FakeBus:
        def publish(self, event_type, data):
            captured.append((event_type, data))

    argv = inj._build_argv("bash", "echo BUILD_OK", "", "")
    inj._stream_build("b1", argv, "", "bash", FakeBus())

    assert captured[0][1]["event"] == "start"
    logs = [d["line"] for t, d in captured if d.get("event") == "log"]
    assert any("BUILD_OK" in line for line in logs)
    assert captured[-1][1]["event"] == "done"
    assert captured[-1][1]["returncode"] == 0


def test_build_endpoint_starts(client, tmp_path):
    pid = _new_project(client)
    client.post("/api/injection/settings",
                json={"terminal": "bash"}, headers=AUTH)
    r = client.post(f"/api/injection/projects/{pid}/build",
                    json={"command": "echo hi", "cwd": str(tmp_path)}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert "build_id" in r.json()


def test_make_script_path_means_its_folder(client, tmp_path):
    script = tmp_path / "make.cmd"
    script.write_text("echo hi")
    client.post("/api/injection/settings", json={"build_cwd": str(script)}, headers=AUTH)
    s = client.get("/api/injection/settings", headers=AUTH).json()
    assert s["build_cwd"] == str(tmp_path)


def test_old_make_script_setting_still_gives_the_folder():
    from Application_Logic import Logic_Test_Builds as tb

    class Meta:
        def get_meta(self, key, default=None):
            return {"inject_make_script": "/proj/make/make.cmd"}.get(key, default)

    assert tb.stored_build_folder(Meta()) == "/proj/make"


def test_build_without_a_folder_is_refused(client):
    pid = _new_project(client)
    r = client.post(f"/api/injection/projects/{pid}/build",
                    json={"command": "make 0", "cwd": ""}, headers=AUTH)
    assert r.status_code == 409 and "No build folder" in r.json()["detail"]


def test_build_in_a_missing_folder_is_refused(client, tmp_path):
    pid = _new_project(client)
    r = client.post(f"/api/injection/projects/{pid}/build",
                    json={"command": "make 0", "cwd": str(tmp_path / "gone")},
                    headers=AUTH)
    assert r.status_code == 409 and "can't be found" in r.json()["detail"]


def test_terminal_opens_in_the_build_folder(client, tmp_path, monkeypatch):
    from backend.routers import injection as inj
    opened = []
    monkeypatch.setattr(inj.subprocess, "Popen",
                        lambda argv, **kw: opened.append((argv, kw["cwd"])))
    r = client.post("/api/injection/terminal", json={"cwd": str(tmp_path)}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert opened[0][1] == str(tmp_path)


def test_terminal_needs_a_build_folder(client):
    r = client.post("/api/injection/terminal", json={"cwd": ""}, headers=AUTH)
    assert r.status_code == 409


def test_terminal_window_per_terminal_on_windows(monkeypatch):
    from Application_Logic import Logic_Test_Builds as tb
    monkeypatch.setattr(tb.sys, "platform", "win32")
    assert tb.terminal_window_argv("cmd", "X:\\make", "") == ["cmd.exe"]
    assert tb.terminal_window_argv("powershell", "X:\\make", "") == \
        ["powershell.exe", "-NoExit"]
    assert tb.terminal_window_argv("wsl", "/home/u", "Ubuntu") == \
        ["wsl.exe", "-d", "Ubuntu", "--cd", "/home/u"]


def test_build_no_command_rejected(client):
    pid = _new_project(client)
    r = client.post(f"/api/injection/projects/{pid}/build",
                    json={"command": "  "}, headers=AUTH)
    assert r.status_code == 409


# -- hook placement ------------------------------------------------------
from Application_Logic import Logic_Code_Injection as anchors

TWO_FUNCS = [
    "void a(void)",
    "{",
    "    step_a();",
    "}",
    "",
    "void b(void)",
    "{",
    "    step_b();",
    "    if (x)",
    "    {",
    "        step_c();",
    "    }",
    "",
    "    done();",
    "}",
]


def _resolved(lines, index):
    return anchors.resolve_injection(lines, *anchors.anchor_context(lines, index))


def test_every_cursor_position_resolves_to_itself():
    for index in range(len(TWO_FUNCS) + 1):
        r = _resolved(TWO_FUNCS, index)
        assert r["index"] == index, index
        assert r["confidence"] == 4, index


def test_one_line_anchors_stay_on_their_own_pair():
    # A hook saved before the anchors held several lines, with a common line above.
    r = anchors.resolve_injection(TWO_FUNCS, "{", "    step_b();")
    assert r == {"index": 7, "confidence": 4, "anchor": "both"}
    # A lone "}" could be any of them, so it's reported as a guess.
    assert anchors.resolve_injection(TWO_FUNCS, "    }", "")["confidence"] == 3


def test_identical_places_are_not_called_exact():
    lines = ["void a(void)", "{", "    go();", "}", "void b(void)", "{", "    go();", "}"]
    r = anchors.resolve_injection(lines, "{", "    go();")
    assert r["index"] == 2 and r["confidence"] == 3
    # The function line in the context tells them apart.
    r = anchors.resolve_injection(lines, "void b(void)\n{", "    go();")
    assert r["index"] == 6 and r["confidence"] == 4


def test_hook_survives_lines_added_above_it():
    above, below = anchors.anchor_context(TWO_FUNCS, 8)
    moved = ["#include <x.h>", "", "{", "    other();", "}", ""] + TWO_FUNCS
    assert anchors.resolve_injection(moved, above, below)["index"] == 14


def test_form_feed_doesnt_shift_lines():
    assert anchors.source_lines("a\f\r\nb\nc") == ["a\f", "b", "c"]


def test_at_index_sets_anchors_from_the_source(client):
    pid = _new_project(client)
    r = client.post(f"/api/injection/projects/{pid}/injections",
                    json={"src_file_path": "src/main.c", "at_index": 2,
                          "injected_code": "CHECK();"}, headers=AUTH)
    assert r.status_code == 200, r.text
    hook = client.get(f"/api/injection/projects/{pid}/injections", headers=AUTH)\
        .json()["injections"][0]
    assert hook["resolved_index"] == 2 and hook["confidence"] == 4


# -- helper destination --------------------------------------------------
def test_import_puts_files_in_the_chosen_folder(client):
    pid = _new_project(client)
    body = {"files": [{"rel_path": "harness.c", "content": "void h(void){}"}],
            "dest": "src/tests/"}
    client.post(f"/api/injection/projects/{pid}/import", json=body, headers=AUTH)
    files = client.get(f"/api/injection/projects/{pid}/files", headers=AUTH).json()
    assert [f["rel_path"] for f in files["files"]] == ["src/tests/harness.c"]


def test_import_preview_flags_a_production_file_and_stores_nothing(client):
    pid = _new_project(client)
    body = {"files": [{"rel_path": "main.c", "content": "x"},
                      {"rel_path": "new.c", "content": "y"}],
            "dest": "src", "preview": True}
    r = client.post(f"/api/injection/projects/{pid}/import", json=body, headers=AUTH)
    assert r.json()["files"] == [
        {"rel_path": "src/main.c", "replaces_source": True},
        {"rel_path": "src/new.c", "replaces_source": False},
    ]
    assert client.get(f"/api/injection/projects/{pid}/files", headers=AUTH)\
        .json()["files"] == []


def test_import_refuses_a_destination_outside_the_source(client):
    pid = _new_project(client)
    body = {"files": [{"rel_path": "a.c", "content": "x"}], "dest": "../up"}
    r = client.post(f"/api/injection/projects/{pid}/import", json=body, headers=AUTH)
    assert r.status_code == 409


def test_helper_replacing_a_hooked_file_is_reported(client, tmp_path):
    pid = _new_project(client)
    iid = _add_hook(client, pid)
    client.post(f"/api/injection/projects/{pid}/import",
                json={"files": [{"rel_path": "src/main.c", "content": "stub"}]},
                headers=AUTH)
    r = client.post(f"/api/injection/projects/{pid}/export",
                    json={"mode": "modified", "out_dir": str(tmp_path / "o"),
                          "continue_anyway": True}, headers=AUTH)
    assert {"src_file_path": "src/main.c", "injection_id": iid} in r.json()["conflicts"]


# -- function search -----------------------------------------------------
def test_source_functions_lists_name_file_and_line(client):
    r = client.get("/api/injection/source/functions", headers=AUTH)
    assert {"name": "main", "rel_path": "src/main.c", "line": 1} in r.json()["functions"]


# -- build settings ------------------------------------------------------
def test_cmd_gets_quoted_commands_verbatim_on_windows(monkeypatch):
    from Application_Logic import Logic_Test_Builds as tb
    argv = tb.build_argv("cmd", '"C:\\my proj\\make.bat" 0', "", "")
    assert tb.popen_args(argv) is argv
    monkeypatch.setattr(tb.sys, "platform", "win32")
    assert tb.popen_args(argv) == 'cmd.exe /s /c ""C:\\my proj\\make.bat" 0"'
    assert tb.popen_args(tb.build_argv("bash", "make", "", "")) == ["bash", "-lc", "make"]
    assert tb.default_terminal() == "cmd"


def test_build_runs_command_in_the_working_folder(client, tmp_path):
    (tmp_path / "make").write_text("echo ran-$1-in-$(pwd)\n")
    (tmp_path / "make").chmod(0o755)
    from Application_Logic import Logic_Test_Builds as tb
    lines = []
    code = tb.run_command(tb.build_argv("bash", "./make 0", str(tmp_path), ""),
                          str(tmp_path), "bash", lines.append, __import__("threading").Event())
    assert code == 0 and lines[0].startswith("ran-0-in-")


def test_new_test_build_starts_from_the_project_settings(client):
    pid = _new_project(client)
    client.post("/api/injection/settings",
                json={"build_command": "make 0", "build_cwd": "/proj/make",
                      "terminal": "cmd", "source_path": "/proj/src"}, headers=AUTH)
    r = client.post("/api/test-builds", json={"name": "Wd", "test_project_id": pid},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    made = r.json()
    assert (made["build_command"], made["build_cwd"], made["terminal"],
            made["source_dir"]) == ("make 0", "/proj/make", "cmd", "/proj/src")


# ----------------------------------------------------------------------
# The project folder and changed files
# ----------------------------------------------------------------------
def _project_folder(client, tmp_path):
    """A project folder on disk with a makefile, an ARXML, a binary and .git."""
    root = tmp_path / "ecu"
    (root / "src").mkdir(parents=True)
    (root / "cfg").mkdir()
    (root / ".git").mkdir()
    (root / "src" / "main.c").write_text(MAIN_C)
    (root / "Makefile").write_bytes(b"CFLAGS = -O2\r\nall:\r\n\tcc $(CFLAGS) src/main.c\r\n")
    (root / "cfg" / "Os.arxml").write_text("<AUTOSAR><OS/></AUTOSAR>\n")
    (root / "cfg" / "legacy.txt").write_bytes("Gr\xfc\xdfe".encode("latin-1"))
    (root / "app.bin").write_bytes(b"\x00\x01\x02")
    assert client.post("/api/injection/settings", json={"source_path": str(root)},
                       headers=AUTH).status_code == 200
    return root


def _get(client, url, **params):
    r = client.get(f"/api/injection/{url}", params=params, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def test_project_tree_lists_the_whole_folder_one_level_at_a_time(client, tmp_path):
    pid = _new_project(client)
    assert _get(client, f"projects/{pid}/tree") == {
        "folder": "", "available": False, "dir": "", "entries": []}

    root = _project_folder(client, tmp_path)
    top = _get(client, f"projects/{pid}/tree")
    assert top["available"] and top["folder"] == str(root)
    # Folders first, .git left out, every kind of file listed.
    assert [(e["name"], e["is_dir"]) for e in top["entries"]] == [
        ("cfg", True), ("src", True), ("app.bin", False), ("Makefile", False)]
    cfg = _get(client, f"projects/{pid}/tree", dir="cfg")
    assert [e["rel_path"] for e in cfg["entries"]] == ["cfg/legacy.txt", "cfg/Os.arxml"]
    r = client.get(f"/api/injection/projects/{pid}/tree", params={"dir": "../.."}, headers=AUTH)
    assert r.status_code == 409


def test_any_text_file_can_be_opened_and_binary_or_other_encodings_only_read(client, tmp_path):
    pid = _new_project(client)
    _project_folder(client, tmp_path)
    mk = _get(client, f"projects/{pid}/disk/content", rel_path="Makefile")
    assert mk["editable"] and mk["content"].startswith("CFLAGS = -O2\r\n")   # line ends kept
    assert _get(client, f"projects/{pid}/disk/content", rel_path="cfg/Os.arxml")["editable"]
    binary = _get(client, f"projects/{pid}/disk/content", rel_path="app.bin")
    assert not binary["editable"] and "binary" in binary["reason"]
    legacy = _get(client, f"projects/{pid}/disk/content", rel_path="cfg/legacy.txt")
    assert not legacy["editable"] and "UTF-8" in legacy["reason"]
    r = client.get(f"/api/injection/projects/{pid}/disk/content",
                   params={"rel_path": "../outside.txt"}, headers=AUTH)
    assert r.status_code == 409


def test_changing_a_project_file_keeps_a_copy_in_the_test_project(client, tmp_path):
    pid = _new_project(client)
    root = _project_folder(client, tmp_path)
    new_make = "CFLAGS = -O0 -g\r\nall:\r\n\tcc $(CFLAGS) src/main.c\r\n"
    r = client.put(f"/api/injection/projects/{pid}/files/content", headers=AUTH,
                   json={"rel_path": "Makefile", "content": new_make})
    assert r.status_code == 200 and r.json()["created"] is True
    # The file on disk is untouched: only a test build writes the copy over it.
    assert b"-O2" in (root / "Makefile").read_bytes()

    assert _get(client, f"projects/{pid}/files/content", rel_path="Makefile")["content"] == new_make
    original = _get(client, f"projects/{pid}/files/original", rel_path="Makefile")
    assert original["source"] == "disk" and "-O2" in original["content"]

    client.put(f"/api/injection/projects/{pid}/files/content", headers=AUTH,
               json={"rel_path": "tests/stub.c", "content": "int stub;\n"})
    files = {f["rel_path"]: f for f in _get(client, f"projects/{pid}/changes")["files"]}
    assert files["Makefile"]["status"] == "modified" and not files["Makefile"]["original_changed"]
    assert files["tests/stub.c"]["status"] == "added"

    # Someone changes the original: the copy says so, and saving again keeps
    # the original it was made from.
    (root / "Makefile").write_bytes(b"CFLAGS = -O3\r\n")
    client.put(f"/api/injection/projects/{pid}/files/content", headers=AUTH,
               json={"rel_path": "Makefile", "content": new_make + "# more\r\n"})
    files = {f["rel_path"]: f for f in _get(client, f"projects/{pid}/changes")["files"]}
    assert files["Makefile"]["original_changed"] is True

    # A file that isn't UTF-8 text can't become a changed file.
    r = client.put(f"/api/injection/projects/{pid}/files/content", headers=AUTH,
                   json={"rel_path": "cfg/legacy.txt", "content": "x"})
    assert r.status_code == 409
    # Reverting is removing the copy.
    client.delete(f"/api/injection/projects/{pid}/files", params={"rel_path": "Makefile"},
                  headers=AUTH)
    assert "Makefile" not in {f["rel_path"]
                              for f in _get(client, f"projects/{pid}/changes")["files"]}


def test_changes_list_hooked_files_and_hooks_a_changed_file_overrides(client, tmp_path):
    pid = _new_project(client)
    _project_folder(client, tmp_path)
    client.post(f"/api/injection/projects/{pid}/injections", headers=AUTH,
                json={"src_file_path": "src/main.c", "function_name": "main",
                      "line_above_code": "int x = 1;", "line_below_code": "int y = 2;",
                      "injected_code": "x++;"})
    files = _get(client, f"projects/{pid}/changes")["files"]
    assert files == [{"rel_path": "src/main.c", "size": 0, "status": "hooks", "hooks": 1,
                      "original_changed": False, "hooks_lost": 0}]

    client.put(f"/api/injection/projects/{pid}/files/content", headers=AUTH,
               json={"rel_path": "src/main.c", "content": "int main(void) { return 1; }\n"})
    files = _get(client, f"projects/{pid}/changes")["files"]
    assert [(f["status"], f["hooks_lost"]) for f in files] == [("modified", 1)]


def test_a_changed_makefile_is_exported_with_the_hooked_files(client, tmp_path):
    pid = _new_project(client)
    _project_folder(client, tmp_path)
    client.put(f"/api/injection/projects/{pid}/files/content", headers=AUTH,
               json={"rel_path": "Makefile", "content": "CFLAGS = -O0\n"})
    out = tmp_path / "export"
    r = client.post(f"/api/injection/projects/{pid}/export", headers=AUTH,
                    json={"mode": "modified", "out_dir": str(out), "continue_anyway": True})
    assert r.status_code == 200, r.text
    assert (out / "Makefile").read_text() == "CFLAGS = -O0\n"
