"""Action icons shared by the competition page and its dialogs."""
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QPushButton, QStyle

try:
    from plugin_manager.icons import action_icon
except ImportError:
    action_icon = None


_STANDARD = QStyle.StandardPixmap
_ACTIONS = {
    "authorize": _STANDARD.SP_DialogYesButton,
    "cancel": _STANDARD.SP_DialogCancelButton,
    "close": _STANDARD.SP_DialogCloseButton,
    "confirm": _STANDARD.SP_DialogApplyButton,
    "info": _STANDARD.SP_MessageBoxInformation,
    "log": _STANDARD.SP_FileDialogDetailedView,
    "refresh": _STANDARD.SP_BrowserReload,
    "settings": _STANDARD.SP_FileDialogContentsView,
    "tag": _STANDARD.SP_FileLinkIcon,
    "upload": _STANDARD.SP_ArrowUp,
    "window-close": _STANDARD.SP_DialogCloseButton,
}


def tournament_icon() -> QIcon:
    return QIcon(str(Path(__file__).with_name("icon.svg")))


def ui_icon(name: str) -> QIcon:
    if name == "copy":
        return QIcon.fromTheme("edit-copy", QApplication.style().standardIcon(_STANDARD.SP_FileIcon))
    if name == "score":
        return QIcon.fromTheme("office-chart-bar", ui_icon("log"))
    if action_icon is not None:
        return action_icon(name)
    return QApplication.style().standardIcon(_ACTIONS[name])


def icon_button(text: str, name: str) -> QPushButton:
    button = QPushButton(text)
    button.setIcon(ui_icon(name))
    button.setIconSize(QSize(16, 16))
    return button
