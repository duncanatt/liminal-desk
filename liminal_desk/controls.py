"""Small native disclosure and toolbar controls."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidget


TOOL_STYLE = ('QToolButton { border:0; background:transparent; padding:4px 6px; border-radius:3px; } '
              'QToolButton:hover { background:palette(midlight); } '
              'QToolButton:checked { background:palette(mid); }')


def tool(text, tip='', parent=None):
    button = QToolButton(parent)
    button.setText(text)
    button.setToolTip(tip or text)
    button.setAccessibleName(tip or text)
    button.setAutoRaise(True)
    button.setStyleSheet(TOOL_STYLE)
    return button


class Disclosure(QWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.button = tool(title)
        self.button.setCheckable(True)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setArrowType(Qt.ArrowType.RightArrow)
        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(0, 5, 0, 3)
        self.content.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.content)
        self.button.toggled.connect(self._toggle)

    def _toggle(self, checked):
        self.content.setVisible(checked)
        self.button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def reveal(self):
        self.button.setChecked(True)
