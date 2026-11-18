import logging
import os
from pathlib import Path
import sys

from PySide6.QtCore import QLockFile
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

import quick_itch
from quick_itch.appearance import CheckStyle
from quick_itch.store import Store
from quick_itch.updater import Updater
from quick_itch.window import Window


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Quick Itch Updater")
    app.setApplicationVersion(quick_itch.__version__)
    app.setWindowIcon(QIcon(str(Path(quick_itch.__file__).with_name("quick-itch-updater.ico"))))
    app.setStyle(CheckStyle("Fusion"))
    data = Path(os.environ["LOCALAPPDATA"]) / "Quick Itch Updater"
    data.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(data / "app.lock"))
    if not lock.tryLock(0):
        QMessageBox.information(None, app.applicationName(), "Quick Itch Updater is already open.")
        return 1
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    store = Store(data / "library.sqlite3")
    window = Window(store, Updater(store))
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
