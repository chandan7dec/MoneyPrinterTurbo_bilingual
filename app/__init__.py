"""MoneyPrinterTurbo 应用包元数据。"""

__version__ = "1.3.5"


def _ensure_raqm_shaping() -> None:
    """Enable complex-text shaping for Pillow on Windows (Devanagari etc.).

    Pillow's `_imagingft` extension loads ``libfribidi-0.dll`` through a plain
    ``LoadLibrary`` call that searches PATH but not the package directory. When
    the DLL cannot be found, Raqm stays disabled and Devanagari text renders
    with broken shaping (half letters, reph, matra reordering all wrong) in the
    intro/outro cards and burned-in subtitles.

    We vendor the tiny fribidi DLL in ``app/libs`` and prepend that directory to
    PATH *before* anything imports PIL. On non-Windows, or when the DLL is
    missing / already loadable, this is a no-op.
    """
    import os
    import sys

    if sys.platform != "win32":
        return
    if os.environ.get("MPT_SKIP_RAQM_BOOTSTRAP"):
        return

    from pathlib import Path

    for base in (Path(__file__).resolve().parent, Path(sys.executable).parent):
        candidate = base / "libs" / "libfribidi-0.dll"
        if candidate.is_file():
            dll_dir = str(candidate.parent)
            try:
                os.add_dll_directory(dll_dir)
            except (AttributeError, OSError):
                pass
            path_value = os.environ.get("PATH", "")
            if dll_dir.lower() not in path_value.lower():
                os.environ["PATH"] = dll_dir + os.pathsep + path_value
            return


_ensure_raqm_shaping()
