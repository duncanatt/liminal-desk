"""JSON editor with native Qt highlighting and inline diagnostics."""
import re

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFontDatabase, QPainter, QPalette,
                           QSyntaxHighlighter, QTextCharFormat, QTextCursor)
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QToolTip, QWidget

from .validation import validate_json

TOKEN = re.compile(r'"(?:\\.|[^"\\])*"?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}\[\],:]')


def utf16_length(text):
    return len(text.encode('utf-16-le', errors='surrogatepass')) // 2


def pretty_json(text):
    """Format validated JSON without changing numbers, escapes, or duplicate keys."""
    tokens = [m.group() for m in TOKEN.finditer(text)]
    out, depth = [], 0
    for index, token in enumerate(tokens):
        previous = tokens[index - 1] if index else ''
        following = tokens[index + 1] if index + 1 < len(tokens) else ''
        if token in ('{', '['):
            out.append(token)
            depth += 1
            if following not in ('}', ']'):
                out.append('\n' + '    ' * depth)
        elif token in ('}', ']'):
            depth -= 1
            if previous not in ('{', '['):
                out.append('\n' + '    ' * depth)
            out.append(token)
        elif token == ',':
            out.append(',\n' + '    ' * depth)
        elif token == ':':
            out.append(': ')
        else:
            out.append(token)
    return ''.join(out)


class JsonHighlighter(QSyntaxHighlighter):
    def __init__(self, editor):
        super().__init__(editor.document())
        self.editor = editor

    def highlightBlock(self, text):
        dark = self.editor.palette().color(QPalette.ColorRole.Base).lightness() < 128
        colors = (['#9cdcfe', '#ce9178', '#b5cea8', '#569cd6', '#d4d4d4', '#d7ba7d'] if dark
                  else ['#005a9e', '#a33a20', '#176e48', '#7441aa', '#59616c', '#8b5812'])
        formats = []
        for color in colors:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            formats.append(fmt)
        last, offset = 0, 0
        for match in TOKEN.finditer(text):
            value = match.group()
            offset += utf16_length(text[last:match.start()])
            length = utf16_length(value)
            if value.startswith('"'):
                following = re.compile(r'\S').search(text, match.end())
                is_key = following is not None and text[following.start()] == ':'
                block = self.currentBlock().next()
                if following is None:
                    while block.isValid():
                        tail = block.text().lstrip()
                        if tail:
                            is_key = tail.startswith(':')
                            break
                        block = block.next()
                kind = 0 if is_key else 1
            elif value in ('true', 'false', 'null'):
                kind = 3
            elif value[0] in '-0123456789':
                kind = 2
            else:
                kind = 4
            self.setFormat(offset, length, formats[kind])
            if value.startswith('"'):
                escape_last, escape_offset = 0, offset
                for escape in re.finditer(r'\\(?:u[0-9a-fA-F]{0,4}|.)', value):
                    escape_offset += utf16_length(value[escape_last:escape.start()])
                    escape_length = utf16_length(escape.group())
                    self.setFormat(escape_offset, escape_length, formats[5])
                    escape_offset += escape_length
                    escape_last = escape.end()
            offset += length
            last = match.end()



class Gutter(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setMouseTracking(True)

    def sizeHint(self):
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_gutter(event)

    def mousePressEvent(self, event):
        if self.editor.diagnostic:
            self.editor.jump_to_error()

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip and self.editor.diagnostic:
            QToolTip.showText(event.globalPos(), self.editor.error_text(), self)
            return True
        return super().event(event)


class JsonEditor(QPlainTextEdit):
    diagnostic_changed = Signal(object)
    byte_count_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.diagnostic = None
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(' ') * 4)
        self.gutter = Gutter(self)
        self.highlighter = JsonHighlighter(self)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.validate)
        self.textChanged.connect(self._edited)
        self.blockCountChanged.connect(self.update_margin)
        self.updateRequest.connect(self.update_gutter)
        self.update_margin()
        self.setPlainText('{}')
        self.validate()

    def gutter_width(self):
        return 27 + self.fontMetrics().horizontalAdvance('9') * len(str(max(1, self.blockCount())))

    def update_margin(self, *_):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_margin()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        rect = self.contentsRect()
        self.gutter.setGeometry(QRect(rect.left(), rect.top(), self.gutter_width(), rect.height()))

    def paint_gutter(self, event):
        painter = QPainter(self.gutter)
        base = self.palette().color(QPalette.ColorRole.Base)
        painter.fillRect(event.rect(), base.lighter(125) if base.lightness() < 128 else base.darker(103))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        while block.isValid() and top <= event.rect().bottom():
            height = round(self.blockBoundingRect(block).height())
            if block.isVisible() and top + height >= event.rect().top():
                color = self.palette().color(QPalette.ColorRole.Text)
                color.setAlpha(150)
                painter.setPen(color)
                painter.drawText(16, top, self.gutter.width() - 23, self.fontMetrics().height(),
                                 Qt.AlignmentFlag.AlignRight, str(number + 1))
                if self.diagnostic and self.diagnostic.line == number + 1:
                    painter.fillRect(5, top + 5, 7, 7, QColor('#e34b55'))
            block = block.next()
            number += 1
            top += height

    def _edited(self):
        text = self.toPlainText()
        # Qt can emit textChanged for highlighting/palette changes as well.
        # Keep diagnostics intact when only presentation changed.
        if getattr(self, '_last_text', None) == text:
            return
        self._last_text = text
        # Remove locations derived from older text immediately; fresh validation
        # follows after the debounce interval.
        self.diagnostic = None
        self.timer.start()
        self.setExtraSelections([])
        self.diagnostic_changed.emit(None)
        self.gutter.update()
        self.byte_count_changed.emit(len(self.toPlainText().encode('utf-8', errors='replace')))
        self.timer.start()

    def error_text(self):
        error = self.diagnostic
        return f'{error.message} — line {error.line}, column {error.column}' if error else ''

    def diagnose(self, text):
        return validate_json(text)

    def validate(self):
        self.timer.stop()
        text = self.toPlainText()
        self.diagnostic = self.diagnose(text)
        selections = []
        if self.diagnostic and self.diagnostic.position < len(text):
            selection = QTextEdit.ExtraSelection()
            selection.cursor = QTextCursor(self.document())
            start = utf16_length(text[:self.diagnostic.position])
            length = utf16_length(text[self.diagnostic.position:self.diagnostic.position + 1])
            selection.cursor.setPosition(start)
            selection.cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
            selection.format.setUnderlineColor(QColor('#e34b55'))
            selection.format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
            selection.format.setToolTip(self.error_text())
            selections.append(selection)
        self.setExtraSelections(selections)
        self.gutter.update()
        self.viewport().update()
        self.diagnostic_changed.emit(self.diagnostic)
        return self.diagnostic is None

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.diagnostic and self.diagnostic.position >= len(self.toPlainText()):
            cursor = QTextCursor(self.document())
            cursor.movePosition(QTextCursor.MoveOperation.End)
            rect = self.cursorRect(cursor)
            painter = QPainter(self.viewport())
            painter.setPen(QColor('#e34b55'))
            for offset in range(0, 12, 4):
                x, y = rect.x() + offset, rect.bottom() - 1
                painter.drawLine(x, y, x + 2, y - 2)
                painter.drawLine(x + 2, y - 2, x + 4, y)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.ToolTip and self.diagnostic:
            cursor = self.cursorForPosition(event.pos())
            if cursor.blockNumber() + 1 == self.diagnostic.line:
                QToolTip.showText(event.globalPos(), self.error_text(), self)
                return True
        return super().viewportEvent(event)

    def jump_to_error(self):
        if not self.diagnostic:
            return
        cursor = self.textCursor()
        cursor.setPosition(utf16_length(self.toPlainText()[:self.diagnostic.position]))
        self.setTextCursor(cursor)
        self.setFocus()
        self.ensureCursorVisible()

    def format_json(self):
        if not self.validate():
            self.jump_to_error()
            return False
        cursor = self.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(pretty_json(self.toPlainText()))
        cursor.endEditBlock()
        self.validate()
        return True

    def keyPressEvent(self, event):
        if self.isReadOnly():
            return super().keyPressEvent(event)
        if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            cursor = self.textCursor()
            if event.key() == Qt.Key.Key_Tab and not cursor.hasSelection():
                cursor.insertText(' ' * (4 - cursor.positionInBlock() % 4))
            else:
                first = self.document().findBlock(cursor.selectionStart())
                end = cursor.selectionEnd()
                last = self.document().findBlock(max(cursor.selectionStart(), end - 1))
                blocks = []
                while first.isValid() and first.blockNumber() <= last.blockNumber():
                    blocks.append(first)
                    first = first.next()
                cursor.beginEditBlock()
                for block in reversed(blocks):
                    cursor.setPosition(block.position())
                    if event.key() == Qt.Key.Key_Backtab:
                        count = min(4, len(block.text()) - len(block.text().lstrip(' ')))
                        for _ in range(count):
                            cursor.deleteChar()
                    else:
                        cursor.insertText('    ')
                cursor.endEditBlock()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            cursor = self.textCursor()
            before = cursor.block().text()[:cursor.positionInBlock()]
            indent = re.match(r' *', before).group()
            if before.rstrip().endswith(('{', '[')):
                indent += '    '
            cursor.insertText('\n' + indent)
            return
        super().keyPressEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, 'highlighter'):
            self.highlighter.rehighlight()
            self.gutter.update()
