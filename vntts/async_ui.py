"""Latest-result-wins Qt workers for blocking diagnostic probes."""

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _TaskSignals(QObject):
    finished = Signal(int, object, object)


class _Task(QRunnable):
    def __init__(self, serial, function, arguments, signals):
        super().__init__()
        self.serial = serial
        self.function = function
        self.arguments = arguments
        self.signals = signals

    def run(self):
        try:
            result = self.function(*self.arguments)
        except Exception as error:
            self.signals.finished.emit(self.serial, None, error)
        else:
            self.signals.finished.emit(self.serial, result, None)


class LatestTaskRunner(QObject):
    """Run blocking calls while accepting only the latest launch identity."""

    finished = Signal(object, object)
    activeChanged = Signal(bool)

    def __init__(self, parent=None, *, thread_pool=None):
        super().__init__(parent)
        self.thread_pool = thread_pool or QThreadPool.globalInstance()
        self._serial = 0
        self._active = False
        self._signals = _TaskSignals(self)
        self._signals.finished.connect(self._task_finished)

    @property
    def active(self):
        return self._active

    def start(self, function, *arguments):
        self._serial += 1
        self._set_active(True)
        self.thread_pool.start(_Task(self._serial, function, arguments, self._signals))
        return self._serial

    def cancel(self):
        if not self._active:
            return False
        self._serial += 1
        self._set_active(False)
        return True

    def _task_finished(self, serial, result, error):
        if serial != self._serial or not self._active:
            return
        self._set_active(False)
        self.finished.emit(result, error)

    def _set_active(self, active):
        active = bool(active)
        if self._active == active:
            return
        self._active = active
        self.activeChanged.emit(active)
