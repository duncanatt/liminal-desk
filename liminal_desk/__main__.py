import sys
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from .window import MainWindow


def main():
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus)
    app = QApplication(sys.argv)
    app.setApplicationName('Liminal Desk')
    app.setOrganizationName('Liminal')
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
