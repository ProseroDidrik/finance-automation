"""QProcess-wrapper för att köra finance-automation-scripts från GUI:t."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

REPO_ROOT = Path(__file__).resolve().parent


class ScriptRunner(QObject):
    """Kör ett Python-script som subprocess och strömmar stdout till GUI:t.

    Signals:
        output_line(str): en rad text från stdout (utan trailing newline)
        finished(int): exit code när processen slutat
    """

    output_line = Signal(str)
    finished = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._process: QProcess | None = None
        self._buffer = ""

    def is_running(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.NotRunning

    def run(self, script: str, args: list[str]) -> None:
        """Starta `py {script} {args...}` icke-blockerande. Avvisar om något redan körs."""
        if self.is_running():
            self.output_line.emit(f"[GUI] Avvisar: en körning pågår redan.")
            return

        proc = QProcess(self)
        proc.setProgram("py")
        proc.setArguments([script, *args])
        proc.setWorkingDirectory(str(REPO_ROOT))
        proc.setProcessChannelMode(QProcess.MergedChannels)

        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        proc.setProcessEnvironment(env)

        proc.readyReadStandardOutput.connect(self._on_ready_read)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)

        self._process = proc
        self._buffer = ""
        self.output_line.emit(f"[GUI] $ py {script} {' '.join(args)}")
        proc.start()

    def stop(self) -> None:
        if self._process and self._process.state() != QProcess.NotRunning:
            self._process.kill()

    def _on_ready_read(self) -> None:
        if not self._process:
            return
        data = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self.output_line.emit(line.rstrip("\r"))

    def _on_finished(self, exit_code: int, _exit_status) -> None:
        if self._buffer:
            self.output_line.emit(self._buffer.rstrip("\r"))
            self._buffer = ""
        self.finished.emit(int(exit_code))
        self._process = None

    def _on_error(self, err) -> None:
        self.output_line.emit(f"[GUI] QProcess error: {err}")
