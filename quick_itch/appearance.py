from string import Template

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QProxyStyle, QStyle, QStyledItemDelegate


THEMES = (("Follow Windows", "system"), ("Light", "light"), ("Dark", "dark"))
LIGHT = {
    "canvas": "#f4f6fa", "surface": "#ffffff", "alternate": "#f7f9fc", "header": "#e9eef6",
    "text": "#172740", "muted": "#435774", "disabled": "#64748b", "border": "#b7c5d7",
    "check_border": "#506382", "hover": "#e9effb", "selection": "#dce8ff", "accent": "#234fba",
    "accent_hover": "#173d9c", "on_accent": "#ffffff", "disabled_bg": "#edf0f5",
}
DARK = {
    "canvas": "#161c25", "surface": "#1e2733", "alternate": "#232e3c", "header": "#2a3646",
    "text": "#edf3fb", "muted": "#b8c7d9", "disabled": "#a0aec1", "border": "#53657c",
    "check_border": "#a8bdd4", "hover": "#344257", "selection": "#314969", "accent": "#a1bdff",
    "accent_hover": "#bbd0ff", "on_accent": "#101f3c", "disabled_bg": "#283443",
}
STATUS_COLORS = {
    False: {"Update available": "#204bb0", "Possible change": "#875000", "Check failed": "#9e3030",
            "Files missing locally": "#9e3030", "Up to date": "#246247"},
    True: {"Update available": "#bdceff", "Possible change": "#ffcc83", "Check failed": "#ffaaaa",
           "Files missing locally": "#ffaaaa", "Up to date": "#a0e2bb"},
}

STYLE = Template("""
QWidget { color: $text; font-family: 'Segoe UI'; font-size: 10pt; }
QMainWindow, QDialog { background: $canvas; }
QLabel#title { font-size: 25pt; font-weight: 700; }
QLabel#subtitle, QLabel#status, QLabel#location { color: $muted; }
QPushButton, QLineEdit, QComboBox {
    background: $surface; border: 1px solid $border; border-radius: 6px; padding: 7px 11px;
}
QComboBox { padding-right: 24px; }
QComboBox QAbstractItemView { background: $surface; selection-background-color: $selection; selection-color: $text; }
QPushButton:hover, QComboBox:hover { background: $hover; }
QPushButton:disabled, QLineEdit:disabled { color: $disabled; background: $disabled_bg; }
QPushButton#primary { background: $accent; color: $on_accent; border-color: $accent; font-weight: 600; }
QPushButton#primary:hover { background: $accent_hover; }
QPushButton#primary:disabled { background: $disabled_bg; color: $disabled; border-color: $border; }
QCheckBox { border: 1px solid transparent; padding: 2px; spacing: 5px; }
QLineEdit:focus, QPushButton:focus, QComboBox:focus, QCheckBox:focus { border: 1px solid $accent; }
QPushButton#primary:focus { border-color: $text; }
QTableWidget, QListWidget { background: $surface; alternate-background-color: $alternate;
    border: 1px solid $border; selection-background-color: $selection; selection-color: $text; }
QTableWidget::item:focus, QListWidget::item:focus { border: 1px solid $accent; }
QListWidget::item { padding: 5px; }
QHeaderView::section { background: $header; color: $text; padding: 8px; border: 0; font-weight: 600; }
QProgressBar { border: 1px solid $border; border-radius: 4px; background: $surface; min-height: 12px; }
QProgressBar::chunk { background: $accent; }
""")


def apply_theme(app, preference):
    dark = preference == "dark" or (preference == "system" and app.styleHints().colorScheme() == Qt.ColorScheme.Dark)
    colors = DARK if dark else LIGHT
    role = QPalette.ColorRole
    palette = app.style().standardPalette()
    for item, color in {
        role.Window: "canvas", role.WindowText: "text", role.Base: "surface", role.AlternateBase: "alternate",
        role.Text: "text", role.Button: "surface", role.ButtonText: "text", role.PlaceholderText: "muted",
        role.Highlight: "selection", role.HighlightedText: "text", role.Link: "accent", role.Mid: "check_border",
        role.BrightText: "on_accent", role.ToolTipBase: "header", role.ToolTipText: "text",
    }.items():
        palette.setColor(item, QColor(colors[color]))
    for item in (role.WindowText, role.Text, role.ButtonText, role.Link, role.Mid):
        palette.setColor(QPalette.ColorGroup.Disabled, item, QColor(colors["disabled"]))
    app.setPalette(palette)
    app.setStyleSheet(STYLE.substitute(colors))
    return dark


class CheckStyle(QProxyStyle):
    """Keep Qt's checkbox metrics and paint an explicit, high-contrast outline."""

    def drawPrimitive(self, element, option, painter, widget=None):
        if element not in (QStyle.PrimitiveElement.PE_IndicatorCheckBox,
                           QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck):
            return super().drawPrimitive(element, option, painter, widget)
        rect = QRectF(option.rect).adjusted(1.1, 1.1, -1.1, -1.1)
        palette = option.palette
        state = QStyle.StateFlag
        role = QPalette.ColorRole
        group = QPalette.ColorGroup.Active if option.state & state.State_Enabled else QPalette.ColorGroup.Disabled
        selected = option.state & (state.State_On | state.State_NoChange)
        outline = palette.color(group, role.Link if selected else role.Mid)
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(outline, 1.8))
            painter.setBrush(outline if selected else palette.color(group, role.Base))
            painter.drawRoundedRect(rect, 2, 2)
            if selected:
                left, top, width, height = rect.left(), rect.top(), rect.width(), rect.height()
                mark = QPainterPath(QPointF(left + width * .22, top + height * .51))
                if option.state & state.State_NoChange:
                    mark.lineTo(left + width * .78, top + height * .51)
                else:
                    mark.lineTo(left + width * .43, top + height * .71)
                    mark.lineTo(left + width * .79, top + height * .29)
                painter.setPen(QPen(palette.color(group, role.BrightText), 1.8, Qt.PenStyle.SolidLine,
                                    Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                painter.drawPath(mark)
        finally:
            painter.restore()


class CheckCellDelegate(QStyledItemDelegate):
    """Toggle a checkable cell by clicking its label or checkbox."""

    def editorEvent(self, event, model, option, index):
        flags = index.flags()
        if (event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton
                and flags & Qt.ItemFlag.ItemIsUserCheckable and flags & Qt.ItemFlag.ItemIsEnabled
                and option.rect.contains(event.position().toPoint())):
            checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value
            value = Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked
            return model.setData(index, value.value, Qt.ItemDataRole.CheckStateRole)
        return super().editorEvent(event, model, option, index)
