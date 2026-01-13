import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

from fixed_cropper.main_window import MainWindow
from fixed_cropper.resources import resource_path

def main():
    app = QApplication(sys.argv)

    # ✅ アプリ全体のアイコン（全ウィンドウ共通）
    app.setWindowIcon(QIcon(resource_path("resources/icon.ico")))

    w = MainWindow()
    w.resize(1300, 820)
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
