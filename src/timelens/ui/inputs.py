"""UI input helpers: file pickers that remember their choice across pages, and a runner that
turns input errors and warnings into messages. Paths are relative to the working folder."""

from __future__ import annotations

import json
import tempfile
import threading
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import streamlit as st
import yaml

from timelens.errors import TimeLensError
from timelens.model.process import ProcessGraph, load_process

UPLOAD, LOCAL = "Upload a file...", "Enter a path on this computer..."
PLOT_LOCK = threading.RLock()  # matplotlib is not thread-safe; Streamlit runs pages in threads

# slot -> (label, upload types, example globs)
SLOTS: dict[str, tuple[str, list[str], list[str]]] = {
    "model": ("Process model (BPMN)", ["bpmn", "xml"], ["examples/*/*.bpmn"]),
    "rules": ("Time rules (JSON)", ["json"], ["examples/*/*.rules.json", "results/*.rules.json",
                                              "results/*.repaired.json"]),  # README CLI name
    "template": ("Rules template (JSON)", ["json"], ["examples/*/*.rules.template.json"]),
    "levers": ("Levers (YAML)", ["yaml", "yml"], ["config/levers_*.yaml"]),
    "mapping": ("Log mapping (YAML)", ["yaml", "yml"], ["config/mapping_*.yaml"]),
    "log": ("Event log (CSV, XES or parquet)", ["csv", "xes", "gz", "parquet"],
            ["data/*.csv", "data/*.xes", "data/*.xes.gz", "data/*.parquet"]),
}
SUFFIXES = (".xes.gz", ".xes", ".csv", ".parquet", ".bpmn", ".xml", ".json", ".yaml", ".yml")
CACHES = (".csv.parquet", ".xes.parquet", ".xes.gz.parquet")  # written by load_log


def _upload_dir() -> Path:
    # shortcut: uploads stay in a temp folder until the OS clears it; fine for one local user.
    if "upload_dir" not in st.session_state:
        st.session_state["upload_dir"] = tempfile.mkdtemp(prefix="timelens-ui-")
    return Path(st.session_state["upload_dir"])


def _store_upload(slot: str) -> None:
    """Uploader callback: save a new upload once, one file per upload name.

    The browser name is not trusted: the file gets a fixed name in the session folder.
    """
    upload = st.session_state.get(f"up:{slot}")
    if upload is None:
        return
    suffix = next((s for s in SUFFIXES if upload.name.lower().endswith(s)), None)
    if suffix is None:
        st.session_state[f"upload_error:{slot}"] = (
            f"{upload.name}: unsupported file type. Use one of {', '.join(SUFFIXES)}.")
        return
    st.session_state.pop(f"upload_error:{slot}", None)
    uploaded = st.session_state.setdefault(f"uploaded:{slot}", {})
    name = f"Uploaded: {upload.name}"
    path = Path(uploaded.get(name) or _upload_dir() / f"{slot}{len(uploaded)}{suffix}")
    path.write_bytes(upload.getvalue())
    uploaded[name] = str(path)
    st.session_state[f"choice:{slot}"] = name


def _touch(slot: str) -> None:
    """Selectbox callback: the user picked a file, so keep it across pages."""
    st.session_state[f"touched:{slot}"] = True
    st.session_state[f"choice:{slot}"] = st.session_state[f"pick:{slot}"]


def _fits(slot: str, path: str, process: ProcessGraph) -> bool:
    """Return True if the file belongs to the model: the same process id, or its task ids."""
    try:
        if slot in ("rules", "template"):
            return bool(json.loads(Path(path).read_text()).get("process_id")
                        == process.process_id)
        data = yaml.safe_load(Path(path).read_text())
        tasks = set(process.tasks)
        if slot == "levers":
            return any(lv["param"].split(".")[0] in tasks for lv in data["levers"])
        if slot == "mapping":
            return bool(data["tasks"]) and set(data["tasks"]) <= tasks
    except (OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError):
        return False  # not a sensible default; if the user picks it, loading reports why
    return False


def pick_file(slot: str, model: Path | None = None, prefer: str | None = None) -> Path | None:
    """Show a file picker for `slot` and return the chosen file, or None.

    The options are the example files of the project, files uploaded in this session, an
    upload, and a path on this computer. A file the user picks is kept across pages. Until
    then the default is `prefer`, or the first file that belongs to `model`.
    """
    label, types, globs = SLOTS[slot]
    found = {str(p) for g in globs for p in Path().glob(g) if not p.name.endswith(CACHES)}
    uploaded: dict[str, str] = st.session_state.setdefault(f"uploaded:{slot}", {})
    options = [*sorted(found), *uploaded, UPLOAD, LOCAL]
    key = f"pick:{slot}"
    if (asked := st.query_params.get(slot)) is not None:  # a link from a save names the file
        del st.query_params[slot]
        if asked in options:  # the URL is outside input: take only a file this picker lists
            st.session_state[f"choice:{slot}"], st.session_state[f"touched:{slot}"] = asked, True
    # Set the value on every run: after a page switch the old page's value is still in the
    # session state, but the new widget ignores it, so the user's choice must be written back.
    last = st.session_state.get(f"choice:{slot}")
    st.session_state[key] = (last if last in options else
                             prefer if prefer in options else options[0])
    if model is not None and not st.session_state.get(f"touched:{slot}"):
        try:
            process: ProcessGraph | None = load_process(model)
        except TimeLensError:
            process = None  # the model page reports the error when the user runs it
        fit = process and next((o for o in options if o not in (UPLOAD, LOCAL)
                                and _fits(slot, uploaded.get(o, o), process)), None)
        if fit:
            st.session_state[key] = fit
    choice = st.selectbox(label, options, key=key, on_change=_touch, args=(slot,))
    if choice in uploaded:
        return Path(uploaded[choice])
    if choice == UPLOAD:
        upload = st.file_uploader(f"Upload the {label.lower()}", type=types, key=f"up:{slot}",
                                  on_change=_store_upload, args=(slot,))
        if error := st.session_state.get(f"upload_error:{slot}"):
            st.error(error)
            return None
        saved = uploaded.get(f"Uploaded: {upload.name}") if upload is not None else None
        return Path(saved) if saved else None
    if choice == LOCAL:
        text = st.text_input(f"Path to the {label.lower()}",
                             value=st.session_state.get(f"path:{slot}", ""), key=f"text:{slot}")
        st.session_state[f"path:{slot}"] = text
        if not text:
            return None
        path = Path(text).expanduser()
        if not path.is_file():
            st.error(f"No file at {path}.")
            return None
        return path
    return Path(choice)


def run(key: str, spinner: str, fn: Callable[[], dict[str, Any]], inputs: tuple[Any, ...]) -> None:
    """Run `fn` and keep its result, its warnings or its input error under `key`.

    `inputs` are the page inputs of this run, so the page can tell when they change later.
    """
    st.session_state.pop(key, None)  # an unexpected error must not leave the old result
    # shortcut: catch_warnings is process-wide; fine for one local user, not for many.
    with st.spinner(spinner), warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            out = fn()
        except TimeLensError as e:
            out = {"error": str(e)}
    out["warnings"] = sorted({str(w.message) for w in caught
                              if issubclass(w.category, UserWarning)})
    out["run_inputs"] = inputs  # pages use "inputs" for their own text
    st.session_state[key] = out


def messages(res: dict[str, Any], inputs: tuple[Any, ...]) -> bool:
    """Show the warnings and the input error of a result. Return False if there was an error.

    If the page inputs are not those of the result, say so first: the result below is old.
    """
    if res.get("run_inputs") != inputs:  # missing in a result kept from older code
        st.warning("The inputs changed after this result. Run again to update it.",
                   icon=":material/update:")
    for w in res["warnings"]:
        st.warning(w, icon=":material/warning:")
    if "error" in res:
        st.error(f"Input error: {res['error']}", icon=":material/error:")
        return False
    return True


def name_of(path: Path) -> str:
    """Return a short name for a chosen file: the upload name or the path as given."""
    for slot in SLOTS:
        for name, p in st.session_state.get(f"uploaded:{slot}", {}).items():
            if p == str(path):
                return str(name).removeprefix("Uploaded: ")
    return str(path)
