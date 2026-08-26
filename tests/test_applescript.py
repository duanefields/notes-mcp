"""The boundary where text leaves Python and reaches Notes.

This is the whole injection surface of the server. Note bodies and folder names
are attacker-influenced in the ordinary case -- anyone who can send the operator
text they will paste into a note has written part of the input -- so the
property that matters is that none of it is ever *interpreted*.

The defence is structural rather than filtering: values go as ``argv`` and the
script source is a constant. These tests pin that, because an "escape the
quotes" refactor would look reasonable and would reopen it.
"""

import subprocess

import pytest

from notes_mcp import applescript

# Strings that break a naive implementation. Each one would do something in a
# shell or inside an AppleScript string literal.
NASTY = [
    'quote " and backslash \\',
    "$(whoami)",
    "`id`",
    "; do shell script \"rm -rf ~\"",
    '" & (do shell script "id") & "',
    "end tell\ntell application \"Finder\" to empty trash\ntell application \"Notes\"",
    "'; DROP TABLE ZICCLOUDSYNCINGOBJECT; --",
    "line one\nline two",
    "emoji 🧨 and unicode ﷽",
]


@pytest.fixture
def spawned(monkeypatch):
    """Capture what would have been handed to osascript."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="x-coredata://AAAAAAAA-0000-4000-8000-000000000001/ICNote/p1\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_the_script_is_never_built_by_string_interpolation(spawned):
    """The script text must be a constant; values ride alongside it."""
    applescript.create_note("folder-id", "<div>body</div>")
    (argv, kwargs) = spawned[0]
    assert argv[:2] == [applescript.OSASCRIPT, "-"]
    assert argv[2:] == ["folder-id", "<div>body</div>"]
    # The script comes in on stdin and mentions neither value.
    assert "folder-id" not in kwargs["input"]
    assert "<div>body</div>" not in kwargs["input"]


def test_no_shell_is_involved(spawned):
    """A shell would re-interpret $(...) and backticks in a note's text."""
    applescript.create_note("folder-id", "$(whoami)")
    _argv, kwargs = spawned[0]
    assert kwargs.get("shell") in (None, False)


@pytest.mark.parametrize("value", NASTY)
def test_hostile_bodies_are_passed_through_untouched(spawned, value):
    applescript.create_note("folder-id", value)
    argv, kwargs = spawned[0]
    # Verbatim in argv, and absent from the script that will interpret it.
    assert argv[-1] == value
    assert value not in kwargs["input"]


@pytest.mark.parametrize("value", NASTY)
def test_hostile_folder_names_are_passed_through_untouched(spawned, value):
    applescript.create_folder(value)
    argv, kwargs = spawned[0]
    assert value in argv
    assert value not in kwargs["input"]


@pytest.mark.parametrize(
    "call",
    [
        lambda v: applescript.set_body(v, "body"),
        lambda v: applescript.move_note("note", v),
        lambda v: applescript.delete_note(v),
        lambda v: applescript.delete_folder(v),
    ],
)
@pytest.mark.parametrize("value", NASTY[:4])
def test_every_entry_point_passes_ids_as_arguments(spawned, call, value):
    try:
        call(value)
    except applescript.ScriptError:
        # Some entry points validate the value they get back; that is fine.
        pass
    argv, kwargs = spawned[0]
    assert value in argv
    assert value not in kwargs["input"]


# ----------------------------------------------------------------------
# Object ids
# ----------------------------------------------------------------------


def test_object_id_shape():
    assert (
        applescript.object_id("UUID-HERE", "ICNote", 42)
        == "x-coredata://UUID-HERE/ICNote/p42"
    )


def test_a_reply_that_is_not_an_object_id_is_refused(monkeypatch):
    """Notes returning something unexpected must not be parsed as an id."""

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="missing value", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(applescript.ScriptError, match="not an object id"):
        applescript.create_note("folder", "body")


# ----------------------------------------------------------------------
# Failure modes
# ----------------------------------------------------------------------


def test_a_nonzero_exit_carries_the_reason(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="Notes got an error: Can't get folder. (-1728)"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(applescript.ScriptError, match="-1728"):
        applescript.delete_note("note-id")


def test_a_timeout_names_the_consent_dialog(monkeypatch):
    """The documented failure mode is a hang on an unattended host, so the
    message has to say what is actually blocking rather than "timed out"."""

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(applescript.ScriptTimeout, match="consent dialog"):
        applescript.delete_note("note-id")


def test_osascript_missing_is_reported_as_a_script_error(monkeypatch):
    def fake_run(argv, **kwargs):
        raise OSError("No such file or directory")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(applescript.ScriptError, match="Could not run osascript"):
        applescript.delete_note("note-id")


def test_every_call_has_a_timeout(spawned):
    """Without one, a consent prompt hangs the tool call forever."""
    applescript.delete_note("note-id")
    _argv, kwargs = spawned[0]
    assert kwargs["timeout"] > 0


def test_notes_is_running_is_not_asked_over_appleevents(monkeypatch):
    """Asking Notes would need the very permission whose absence hangs it."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert applescript.notes_is_running() is True
    assert seen["argv"][0].endswith("pgrep")


def test_notes_not_running(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, b"", b""),
    )
    assert applescript.notes_is_running() is False


# ----------------------------------------------------------------------
# What /health is told about the last write
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def forget_the_last_write():
    """The record lives in the module, so without this one test's write shows
    up in the next one's assertions."""
    applescript.reset_last_write()
    yield
    applescript.reset_last_write()


def test_nothing_is_reported_before_the_first_write():
    """A freshly restarted server has not been asked to write. That is not a
    failure, and a monitor must not read it as one."""
    assert applescript.last_write() == {
        "at": None,
        "ok": None,
        "action": None,
        "error": None,
    }


def test_a_successful_write_is_recorded(spawned):
    applescript.create_note("folder-id", "<div>x</div>")
    record = applescript.last_write()
    assert record["ok"] is True
    assert record["action"] == "create_note"
    assert record["error"] is None
    assert record["at"] is not None


@pytest.mark.parametrize(
    "call,action",
    [
        (lambda: applescript.set_body("n", "b"), "set_body"),
        (lambda: applescript.move_note("n", "f"), "move_note"),
        (lambda: applescript.delete_note("n"), "delete_note"),
        (lambda: applescript.create_folder("x"), "create_folder"),
        (lambda: applescript.create_folder("x", parent_id="p"), "create_folder"),
        (lambda: applescript.delete_folder("f"), "delete_folder"),
    ],
)
def test_every_write_names_itself(spawned, call, action):
    """Recording happens inside run(), so a write tool added later cannot
    forget to do it -- but the action label still has to be passed."""
    try:
        call()
    except applescript.ScriptError:
        pass
    assert applescript.last_write()["action"] == action


def test_a_timeout_is_recorded_as_a_failed_write(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(applescript.ScriptTimeout):
        applescript.create_note("folder-id", "<div>x</div>")
    record = applescript.last_write()
    assert record["ok"] is False
    assert record["error"] == "ScriptTimeout"


def test_a_refusal_is_recorded_as_a_failed_write(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Notes: -1728")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(applescript.ScriptError):
        applescript.delete_note("note-id")
    assert applescript.last_write() == {
        "at": applescript.last_write()["at"],
        "ok": False,
        "action": "delete_note",
        "error": "ScriptError",
    }


def test_the_published_failure_is_a_class_name_never_a_message(monkeypatch):
    """/health is unauthenticated and healthcheck.sh forwards it off the host.
    osascript's stderr quotes the arguments it was given, which for a write is
    the note's entire body."""
    secret = "Bank PIN is 1234 and the spare key is under the mat"

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=secret)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(applescript.ScriptError, match="1234"):
        applescript.set_body("note-id", f"<div>{secret}</div>")

    # The exception the caller sees may carry it; what /health publishes must not.
    published = applescript.last_write()
    assert published["error"] == "ScriptError"
    assert secret not in repr(published)


def test_a_read_only_run_records_nothing(spawned):
    """Only writes are recorded; run() without an action is not a write."""
    applescript.run("on run\nreturn 1\nend run")
    assert applescript.last_write()["action"] is None
