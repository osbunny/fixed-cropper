# fixed_cropper/main_window.py
from dataclasses import dataclass
from pathlib import Path
from io import BytesIO

from PySide6.QtCore import Qt, QRectF, QPointF, Signal, QTimer, QEvent, QSettings
from PySide6.QtGui import QAction, QPixmap, QPen, QColor, QBrush, QImage, QIcon, QKeySequence, QDoubleValidator
from PySide6.QtWidgets import (
    QFileDialog, QMainWindow, QMessageBox,
    QGraphicsView, QGraphicsScene, QGraphicsRectItem,
    QGraphicsPixmapItem, QGraphicsLineItem, QInputDialog, QColorDialog,
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QDialog, QScrollArea, QMenu, QLineEdit
)

from PIL import Image, ImageOps

from fixed_cropper.constants import APP_NAME, APP_VER
from fixed_cropper.resources import resource_path

import html as _html

@dataclass
class OutputSize:
    w: int
    h: int

    @property
    def rect(self) -> QRectF:
        return QRectF(0, 0, float(self.w), float(self.h))


class PreviewScrollArea(QScrollArea):
    """プレビュー用：Shift+ホイールで横スクロール。"""
    def wheelEvent(self, event):
        if (event.modifiers() & Qt.ShiftModifier) and not (event.modifiers() & Qt.ControlModifier):
            delta = event.angleDelta().y()
            if delta != 0:
                sb = self.horizontalScrollBar()
                sb.setValue(sb.value() - delta)  # 方向が逆なら +delta に
                event.accept()
                return
        super().wheelEvent(event)


class OutputPreviewDialog(QDialog):
    """
    書き出し結果（固定サイズ画像）を別ウィンドウで表示する。

    UI（オーバーレイ）：
      - 左上：フィット / 100%（押下状態あり）
      - 右上：<w>×<h> px
    """
    def __init__(self, pixmap: QPixmap, title: str, parent=None):
        super().__init__(parent)

        # 最大化（□）ボタンを出す
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
        )

        self.setWindowTitle(title)
        self._pixmap_original = pixmap
        self._zoom_mode_fit = True

        layout = QVBoxLayout(self)

        # 画像表示（スクロール可能）
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("QLabel { background: #202020; }")
        self.label.setPixmap(self._pixmap_original)

        self.scroll = PreviewScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.label)
        layout.addWidget(self.scroll)

        # ---- オーバーレイ（viewportに載せる＝スクロールしても固定）----
        self._overlay_margin = 12

        # 左上：フィット / 100%
        self._overlay_left = QWidget(self.scroll.viewport())
        self._overlay_left.setObjectName("overlayLeft")
        left_lay = QHBoxLayout(self._overlay_left)
        left_lay.setContentsMargins(8, 6, 8, 6)
        left_lay.setSpacing(8)

        self.btn_fit = QPushButton("フィット")
        self.btn_fit.setObjectName("btnFit")
        self.btn_fit.setCheckable(True)

        self.btn_100 = QPushButton("100%")
        self.btn_100.setObjectName("btn100")
        self.btn_100.setCheckable(True)

        # 初期状態：フィット
        self.btn_fit.setChecked(True)

        self.btn_fit.clicked.connect(self._set_fit)
        self.btn_100.clicked.connect(self._set_100)

        left_lay.addWidget(self.btn_fit)
        left_lay.addWidget(self.btn_100)

        self._overlay_left.setStyleSheet("""
            QWidget#overlayLeft {
                background: rgba(0, 0, 0, 140);
                border: 1px solid rgba(255, 255, 255, 70);
                border-radius: 10px;
            }
            QPushButton#btnFit, QPushButton#btn100 {
                color: white;
                background: rgba(255, 255, 255, 18);
                border: 1px solid rgba(255, 255, 255, 70);
                border-radius: 8px;
                padding: 4px 10px;
            }
            QPushButton#btnFit:hover, QPushButton#btn100:hover {
                background: rgba(255, 255, 255, 30);
            }
            /* ★押下（選択）状態 */
            QPushButton#btnFit:checked, QPushButton#btn100:checked {
                background: rgba(255, 255, 255, 55);
                border: 1px solid rgba(255, 255, 255, 160);
            }
            QPushButton#btnFit:pressed, QPushButton#btn100:pressed {
                background: rgba(255, 255, 255, 65);
            }
        """)

        # 右上：サイズ
        self._overlay_right = QWidget(self.scroll.viewport())
        self._overlay_right.setObjectName("overlayRight")
        right_lay = QHBoxLayout(self._overlay_right)
        right_lay.setContentsMargins(8, 6, 8, 6)
        right_lay.setSpacing(8)

        self.size_label = QLabel(f"{pixmap.width()}×{pixmap.height()} px")
        self.size_label.setObjectName("sizeLabel")
        right_lay.addWidget(self.size_label)

        self._overlay_right.setStyleSheet("""
            QWidget#overlayRight {
                background: rgba(0, 0, 0, 140);
                border: 1px solid rgba(255, 255, 255, 70);
                border-radius: 10px;
            }
            QLabel#sizeLabel {
                color: white;
            }
        """)

        self.resize(900, 700)

        # まずは反映
        self._apply_zoom()
        self._reposition_overlays()

        # viewport のサイズはスクロールバーの出入りでも変わるので拾う
        self.scroll.viewport().installEventFilter(self)

        # スクロールバーの range 変化でも viewport サイズが変わり得るので保険
        self.scroll.horizontalScrollBar().rangeChanged.connect(lambda *_: QTimer.singleShot(0, self._reposition_overlays))
        self.scroll.verticalScrollBar().rangeChanged.connect(lambda *_: QTimer.singleShot(0, self._reposition_overlays))

        # ★初回表示のズレ対策：イベントループ後にもう一度だけ再配置
        QTimer.singleShot(0, self._reposition_overlays)

    def showEvent(self, event):
        super().showEvent(event)
        # ★表示直後にもう一度（最大化/非最大化でも安定）
        QTimer.singleShot(0, self._reposition_overlays)

    def _set_fit(self):
        self._zoom_mode_fit = True
        # 排他トグルを明示（クリック連打でも崩れない）
        self.btn_fit.setChecked(True)
        self.btn_100.setChecked(False)
        self._apply_zoom()

    def _set_100(self):
        self._zoom_mode_fit = False
        self.btn_fit.setChecked(False)
        self.btn_100.setChecked(True)
        self._apply_zoom()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._zoom_mode_fit:
            self._apply_zoom()
        self._reposition_overlays()

    def _reposition_overlays(self):
        m = self._overlay_margin
        vp = self.scroll.viewport()

        self._overlay_left.adjustSize()
        self._overlay_left.move(m, m)

        self._overlay_right.adjustSize()
        x = max(0, vp.width() - self._overlay_right.width() - m)
        self._overlay_right.move(x, m)

    def _apply_zoom(self):
        if not self._zoom_mode_fit:
            self.label.setPixmap(self._pixmap_original)
            self.label.adjustSize()
            return

        vp = self.scroll.viewport().size()
        if vp.width() <= 0 or vp.height() <= 0:
            return

        pw = self._pixmap_original.width()
        ph = self._pixmap_original.height()
        if pw <= 0 or ph <= 0:
            return

        scale = min(vp.width() / pw, vp.height() / ph)
        w = max(1, int(pw * scale))
        h = max(1, int(ph * scale))
        scaled = self._pixmap_original.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.label.setPixmap(scaled)
        self.label.adjustSize()


class MovableImageItem(QGraphicsPixmapItem):
    """
    出力フレーム（固定サイズ）の内側で、画像を移動できるアイテム。
    Shift を押しながらドラッグすると、水平/垂直の平行移動（軸固定）。
    """
    def __init__(self, pixmap: QPixmap):
        super().__init__(pixmap)
        self.setZValue(-10)  # 背景(-100)より上、枠(+10)より下
        self.setFlag(QGraphicsPixmapItem.ItemIsMovable, True)
        self.setFlag(QGraphicsPixmapItem.ItemIsSelectable, True)

        self._press_scene_pos: QPointF | None = None
        self._press_item_pos: QPointF | None = None
        self._lock_axis: str | None = None  # "x" or "y"

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_scene_pos = event.scenePos()
            self._press_item_pos = self.pos()
            self._lock_axis = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (event.buttons() & Qt.LeftButton) and self._press_scene_pos is not None and self._press_item_pos is not None:
            if event.modifiers() & Qt.ShiftModifier:
                delta = event.scenePos() - self._press_scene_pos
                if self._lock_axis is None:
                    self._lock_axis = "x" if abs(delta.x()) >= abs(delta.y()) else "y"
                if self._lock_axis == "x":
                    delta = QPointF(delta.x(), 0.0)
                else:
                    delta = QPointF(0.0, delta.y())
                self.setPos(self._press_item_pos + delta)
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_scene_pos = None
        self._press_item_pos = None
        self._lock_axis = None
        super().mouseReleaseEvent(event)


class ImageView(QGraphicsView):
    """
    Ctrl+ホイール：
      - 画像上：画像ズーム
      - 背景上：表示倍率ズーム（ビュー倍率）

    Shift+ホイール：横スクロール（ただしスクロールロック中は無効）
    通常ホイール：縦スクロール（ただしスクロールロック中は無効）

    矢印キー：画像移動

    オーバーレイ：
      - 右下（上）： 画像倍率
      - 右下（下）： キャンバス初期位置 / プレビュー
      - 右上：キャンバスサイズ（例: 1920×1080 px）
    """
    request_fit_initial = Signal()
    request_preview = Signal()

    request_image_zoom_in = Signal()
    request_image_zoom_out = Signal()

    def eventFilter(self, obj, event):
        # viewport() 側に来る Drag&Drop を拾って、自分のハンドラへ中継する
        if obj is self.viewport():
            et = event.type()

            if et == QEvent.Resize:
                QTimer.singleShot(0, self._reposition_overlays)
                return False

            if et == QEvent.DragEnter:
                self.dragEnterEvent(event)
                return event.isAccepted()

            if et == QEvent.DragMove:
                # ここで accept しておくと “置けるカーソル” が出て安定
                event.acceptProposedAction()
                return True

            if et == QEvent.Drop:
                self.dropEvent(event)
                return event.isAccepted()

        return super().eventFilter(obj, event)

    def _is_image_file(self, path: str) -> bool:
        return path.lower().endswith((
            ".png", ".jpg", ".jpeg", ".jfif", ".bmp", ".webp"
        ))

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            for u in md.urls():
                p = u.toLocalFile()
                if self._is_image_file(p):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        md = event.mimeData()
        if not md.hasUrls():
            event.ignore()
            return

        paths: list[str] = []
        for u in md.urls():
            p = u.toLocalFile()
            if self._is_image_file(p):
                paths.append(p)

        if not paths:
            event.ignore()
            return

        mw = self.window()
        if not mw or not hasattr(mw, "open_image_from_path"):
            event.ignore()
            return

        mw.open_image_from_path(paths[0])
        event.acceptProposedAction()
        return

        for u in md.urls():
            p = u.toLocalFile()
            if not p:
                # 保険：file:///C:/... から取り出す
                s = u.toString()
                if s.startswith("file:///"):
                    p = s[8:].replace("/", "\\")  # Windows想定
                elif s.startswith("file://"):
                    p = s[7:]

            if p.lower().endswith((".png", ".jpg", ".jpeg", ".jfif", ".bmp", ".webp")):
                mw.open_image_from_path(p)
                event.acceptProposedAction()
                return

        event.ignore()

    def __init__(self, scene: QGraphicsScene):
        super().__init__(scene)

        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorViewCenter)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)

        self.setFocusPolicy(Qt.StrongFocus)

        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)  # 保険（環境によって効く）

        self._panning = False
        self._pan_start = None
        self._h0 = 0
        self._v0 = 0

        self._scroll_locked = False

        self.setBackgroundBrush(QBrush(QColor("#3A3A3A")))

        self._overlay_margin = 12

        # ---- 右下の少し上（画像倍率コントロール）----
        self._img_overlay = QWidget(self.viewport())
        self._img_overlay.setObjectName("imgOverlay")

        # VBox にして「上：操作」「下：説明」にする
        img_v = QVBoxLayout(self._img_overlay)
        img_v.setContentsMargins(8, 6, 8, 6)
        img_v.setSpacing(4)

        row = QWidget(self._img_overlay)
        img_lay = QHBoxLayout(row)
        img_lay.setContentsMargins(0, 0, 0, 0)
        img_lay.setSpacing(4)

        img_lay.addStretch(1)

        self._img_label = QLabel("画像倍率:")
        self._img_label.setObjectName("imgLabel")
        img_lay.addWidget(self._img_label)

        self._btn_img_minus = QPushButton("－")
        self._btn_img_minus.setFixedWidth(28)
        self._btn_img_minus.clicked.connect(self.request_image_zoom_out.emit)
        img_lay.addWidget(self._btn_img_minus)

        self._img_value = QLineEdit()
        self._img_value.setObjectName("imgValue")
        self._img_value.setFixedWidth(48)
        self._img_value.setAlignment(Qt.AlignCenter)
        self._img_value.setPlaceholderText("--.-")

        # 1.0〜5000.0、少数1桁（好みで変更OK）
        v = QDoubleValidator(1.0, 300.0, 1, self._img_value)
        v.setNotation(QDoubleValidator.StandardNotation)
        self._img_value.setValidator(v)

        # Enter確定（editingFinishedはフォーカス外れでも呼ばれます）
        self._img_value.returnPressed.connect(self._commit_image_scale_input)
        self._img_value.editingFinished.connect(self._commit_image_scale_input)

        img_lay.addWidget(self._img_value)

        self._img_percent = QLabel("%")
        self._img_percent.setObjectName("imgPercent")
        img_lay.addWidget(self._img_percent)

        self._btn_img_plus = QPushButton("＋")
        self._btn_img_plus.setFixedWidth(28)
        self._btn_img_plus.clicked.connect(self.request_image_zoom_in.emit)
        img_lay.addWidget(self._btn_img_plus)

        img_lay.addStretch(1)

        # ★ 説明ラベル（この下に追記）
        self._img_help = QLabel(
            """
            <table cellspacing="0" cellpadding="0">
              <tr>
                <td style="white-space:nowrap; padding-right:6px;">Ctrl+ホイール</td>
                <td style="width:14px; text-align:center;">：</td>
                <td>ズーム（±15%）</td>
              </tr>
              <tr>
                <td style="white-space:nowrap; padding-right:6px;">Ctrl+Shift+ホイール</td>
                <td style="width:14px; text-align:center;">：</td>
                <td>微調整（±3%）</td>
              </tr>
            </table>
              <div style="margin-top:2px; white-space:nowrap;">
                画像上＝画像ズーム / 背景上＝表示ズーム
              </div>
            """
        )
        self._img_help.setObjectName("imgHelp")
        self._img_help.setWordWrap(False)

        img_v.addWidget(row)
        img_v.addWidget(self._img_help)

        self._img_overlay.setStyleSheet("""
            QWidget#imgOverlay {
                background: rgba(0, 0, 0, 140);
                border: 1px solid rgba(255, 255, 255, 70);
                border-radius: 10px;
            }
            QLabel#imgLabel, QLabel#imgHelp { color: white; }
            QLabel#imgHelp { font-size: 11px; color: rgba(255,255,255,180); }
            QLineEdit#imgValue {
                color: white;
                background: transparent;
                border: 0px;
                font-family: Consolas, Menlo, Monaco, monospace;
            }
            QLineEdit#imgValue:focus {
                border: 1px solid rgba(255,255,255,120);
                border-radius: 4px;
                background: rgba(255,255,255,10);
            }
            QLabel#imgPercent {
                color: white;
                font-family: Consolas, Menlo, Monaco, monospace;
            }
            QPushButton {
                color: white;
                background: rgba(255, 255, 255, 25);
                border: 1px solid rgba(255, 255, 255, 70);
                border-radius: 8px;
                padding: 2px 6px;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 40); }
            QPushButton:pressed { background: rgba(255, 255, 255, 55); }
        """)

        # ---- 右下（中段）：画像移動の説明 ----
        self._move_overlay = QWidget(self.viewport())
        self._move_overlay.setObjectName("moveOverlay")

        mv_lay = QVBoxLayout(self._move_overlay)
        mv_lay.setContentsMargins(8, 6, 8, 6)
        mv_lay.setSpacing(4)

        self._move_title = QLabel("画像移動")
        self._move_title.setObjectName("moveTitle")
        mv_lay.addWidget(self._move_title)

        self._move_help = QLabel(
            """
            <table cellspacing="0" cellpadding="0">
              <tr>
                <td style="white-space:nowrap; padding-right:6px;">矢印キー</td>
                <td style="width:14px; text-align:center;">：</td>
                <td>Shiftで微調整</td>
              </tr>
              <tr>
                <td style="white-space:nowrap; padding-right:6px;">ドラッグ</td>
                <td style="width:14px; text-align:center;">：</td>
                <td>Shiftで平行移動</td>
              </tr>
            </table>
            """
        )
        self._move_help.setObjectName("moveHelp")
        self._move_help.setTextFormat(Qt.RichText)
        self._move_help.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        mv_lay.addWidget(self._move_help)

        self._move_overlay.setStyleSheet("""
            QWidget#moveOverlay {
                background: rgba(0, 0, 0, 140);
                border: 1px solid rgba(255, 255, 255, 70);
                border-radius: 10px;
            }
            QLabel#moveTitle {
                color: white;
                font-weight: bold;
            }
            QLabel#moveHelp {
                color: rgba(255,255,255,220);
                font-size: 11px;
            }
        """)

        # ---- 右下（倍率/ボタン）----
        self._overlay = QWidget(self.viewport())
        self._overlay.setObjectName("overlay")
        self._overlay.setAttribute(Qt.WA_TransparentForMouseEvents, False)

        lay = QHBoxLayout(self._overlay)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(8)

        self._btn_initial = QPushButton("キャンバス初期位置")
        self._btn_initial.setObjectName("btnInitial")
        self._btn_initial.clicked.connect(self.request_fit_initial.emit)
        lay.addWidget(self._btn_initial)

        self._btn_preview = QPushButton("プレビュー")
        self._btn_preview.setObjectName("btnPreview")
        self._btn_preview.clicked.connect(self.request_preview.emit)
        lay.addWidget(self._btn_preview)

        self._overlay.setStyleSheet("""
            QWidget#overlay {
                background: rgba(0, 0, 0, 140);
                border: 1px solid rgba(255, 255, 255, 70);
                border-radius: 10px;
            }
            QPushButton#btnInitial, QPushButton#btnPreview {
                color: white;
                background: rgba(255, 255, 255, 25);
                border: 1px solid rgba(255, 255, 255, 70);
                border-radius: 8px;
                padding: 4px 10px;
            }
            QPushButton#btnInitial:hover, QPushButton#btnPreview:hover {
                background: rgba(255, 255, 255, 40);
            }
            QPushButton#btnInitial:pressed, QPushButton#btnPreview:pressed {
                background: rgba(255, 255, 255, 55);
            }
        """)

        # ---- 右上（キャンバスサイズ）----
        self._size_overlay = QWidget(self.viewport())
        self._size_overlay.setObjectName("sizeOverlay")

        size_lay = QHBoxLayout(self._size_overlay)
        size_lay.setContentsMargins(8, 6, 8, 6)
        size_lay.setSpacing(8)

        self._size_label = QLabel("1920×1080 px")
        self._size_label.setObjectName("sizeLabel")
        size_lay.addWidget(self._size_label)

        self._size_overlay.setStyleSheet("""
            QWidget#sizeOverlay {
                background: rgba(0, 0, 0, 140);
                border: 1px solid rgba(255, 255, 255, 70);
                border-radius: 10px;
            }
            QLabel#sizeLabel {
                color: white;
            }
        """)

        # ---- 左下（ショートカット案内）----
        self._shortcut_overlay = QWidget(self.viewport())
        self._shortcut_overlay.setObjectName("shortcutOverlay")

        sc_lay = QVBoxLayout(self._shortcut_overlay)
        sc_lay.setContentsMargins(10, 10, 10, 10)
        sc_lay.setSpacing(2)

        self._shortcut_title = QLabel("ショートカット")
        self._shortcut_title.setObjectName("shortcutTitle")
        sc_lay.addWidget(self._shortcut_title)

        self._shortcut_label = QLabel("")  # 動的に埋める
        self._shortcut_label.setObjectName("shortcutLabel")
        self._shortcut_label.setTextFormat(Qt.RichText)
        self._shortcut_label.setWordWrap(True)
        sc_lay.addWidget(self._shortcut_label)

        self._shortcut_overlay.setStyleSheet("""
            QWidget#shortcutOverlay {
                background: rgba(0, 0, 0, 140);
                border: 1px solid rgba(255, 255, 255, 70);
                border-radius: 10px;
            }
            QLabel#shortcutTitle {
                color: white;
                font-weight: bold;
            }
            QLabel#shortcutLabel {
                color: white;
                font-size: 11px;
            }
        """)

        # viewport のサイズ変化（スクロールバーの出入り含む）を拾う
        self.viewport().installEventFilter(self)

        # スクロールバーの range 変化でも viewport が変わり得るので保険
        self.horizontalScrollBar().rangeChanged.connect(
            lambda *_: QTimer.singleShot(0, self._reposition_overlays)
        )
        self.verticalScrollBar().rangeChanged.connect(
            lambda *_: QTimer.singleShot(0, self._reposition_overlays)
        )

        # ★ 全部作ってから位置決め
        self._reposition_overlays()

    def set_shortcut_actions(self, actions: list[QAction | None]):
        rows: list[str] = []

        for act in actions:
            if act is None:
                rows.append("""
                    <tr class="sep">
                        <td colspan="2"><div></div></td>
                    </tr>
                """)
                continue


            sc = act.shortcut()
            if sc.isEmpty():
                continue

            name = (act.text() or "").replace("&", "")
            key = sc.toString(QKeySequence.NativeText)

            # 念のためHTMLエスケープ
            name = _html.escape(name)
            key = _html.escape(key)

            rows.append(f"""
                <tr>
                    <td class="key">{key}</td>
                    <td class="desc">{name}</td>
                </tr>
            """)

        html = f"""
        <html>
        <head>
        <style>
            html, body {{
                margin: 0;
                padding: 0;
                background: transparent;
            }}

            table {{
                border-collapse: collapse;
                border-spacing: 0;
            }}

            td {{
                padding: 0;
                margin: 0;
                background: transparent;
            }}

            td.key {{
                padding-right: 8px;
                white-space: nowrap;
                font-family: Consolas, Menlo, Monaco, monospace;
            }}

            td.desc {{
                padding-left: 4px;
            }}

        </style>
        </head>
        <body>
            <table>
                {''.join(rows)}
            </table>
        </body>
        </html>
        """
        self._shortcut_label.setText(html)
        self._reposition_overlays()

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()

        # 矢印キーだけ処理（ビューのスクロールを防ぐ）
        if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            mw = self.window()
            if mw and hasattr(mw, "nudge_image_pos"):
                # 1px、Shiftで10px（好みで変更OK）
                step = 10 if (mods & Qt.ShiftModifier) else 1

                dx = 0
                dy = 0
                if key == Qt.Key_Left:
                    dx = -step
                elif key == Qt.Key_Right:
                    dx = step
                elif key == Qt.Key_Up:
                    dy = -step
                elif key == Qt.Key_Down:
                    dy = step

                mw.nudge_image_pos(dx, dy)
                event.accept()
                return

            event.accept()
            return

        super().keyPressEvent(event)

    def set_scroll_locked(self, locked: bool):
        locked = bool(locked)
        if self._scroll_locked == locked:
            return
        self._scroll_locked = locked

        if locked:
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        else:
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        QTimer.singleShot(0, self._reposition_overlays)

    def set_image_scale_percent(self, percent: float | None):
        """画像倍率(%)をUIに反映。Noneなら未読み込み扱い。"""
        if percent is None:
            self._img_value.setText("--.-")
            self._img_overlay.setEnabled(False)
            self._reposition_overlays()
            return

        self._img_overlay.setEnabled(True)

        # ★編集中は上書きしない（ここ重要）
        if not self._img_value.hasFocus():
            # 表示は 1桁、入力欄は % を付けない運用が楽です
            self._img_value.setText(f"{percent:.1f}")

        self._reposition_overlays()

    def set_canvas_size_text(self, w: int, h: int):
        self._size_label.setText(f"{w}×{h} px")
        self._reposition_overlays()

    def _reposition_overlays(self):
        m = self._overlay_margin
        gap = 6

        # まずはサイズ確定
        self._overlay.adjustSize()
        self._img_overlay.adjustSize()
        self._move_overlay.adjustSize()  # ← 矢印キーbox（名前はあなたの実装に合わせて）

        # ★ ここで幅を揃える
        w = max(self._img_overlay.width(), self._move_overlay.width(), self._overlay.width())
        self._img_overlay.setFixedWidth(w)
        self._move_overlay.setFixedWidth(w)
        self._overlay.setFixedWidth(w)

        # 幅が変わったので、もう一回 adjustSize 相当の扱い（高さだけ見直し）
        self._img_overlay.adjustSize()
        self._move_overlay.adjustSize()
        self._overlay.adjustSize()

        # 下段（ボタン）
        x = max(0, self.viewport().width() - self._overlay.width() - m)
        y = max(0, self.viewport().height() - self._overlay.height() - m)
        self._overlay.move(x, y)

        # 中段（矢印キー説明）
        x_mv = max(0, self.viewport().width() - self._move_overlay.width() - m)
        y_mv = max(0, y - self._move_overlay.height() - gap)
        self._move_overlay.move(x_mv, y_mv)

        # 上段（画像倍率）
        x_img = max(0, self.viewport().width() - self._img_overlay.width() - m)
        y_img = max(0, y_mv - self._img_overlay.height() - gap)
        self._img_overlay.move(x_img, y_img)

        # 右上
        self._size_overlay.adjustSize()
        x2 = max(0, self.viewport().width() - self._size_overlay.width() - m)
        self._size_overlay.move(x2, m)

        # 左下
        self._shortcut_overlay.adjustSize()
        x3 = m
        y3 = max(0, self.viewport().height() - self._shortcut_overlay.height() - m)
        self._shortcut_overlay.move(x3, y3)


    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_overlays()

    def _has_pixmap_item_under_cursor(self, view_pos) -> bool:
        items = self.items(view_pos)
        for it in items:
            if isinstance(it, QGraphicsPixmapItem):
                return True
        return False

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            return

        mods = event.modifiers()

        # Ctrl + ホイール = ズーム（スクロールロックの影響なし）
        if mods & Qt.ControlModifier:
            mw = self.window()
            if not mw:
                event.ignore()
                return

            on_image = self._has_pixmap_item_under_cursor(event.position().toPoint())

            if on_image and hasattr(mw, "zoom_image"):
                # Ctrl+Shift: 微調整
                step = "normal"
                if mods & Qt.ShiftModifier:
                    step = "fine"

                mw.zoom_image(zoom_in=(delta > 0), step=step)
                event.accept()
                return

            if hasattr(mw, "nudge_view_zoom"):
                mw.nudge_view_zoom(zoom_in=(delta > 0))
                event.accept()
                return

            event.ignore()
            return

        # 初期表示中（スクロールロック中）はスクロール無効化
        if self._scroll_locked:
            event.accept()
            return

        # Shift + ホイール = 横スクロール
        if mods & Qt.ShiftModifier:
            sb = self.horizontalScrollBar()
            sb.setValue(sb.value() - delta)  # 逆なら +delta に
            event.accept()
            return

        # 通常ホイール = 縦スクロール
        sb = self.verticalScrollBar()
        sb.setValue(sb.value() - delta)  # 逆なら +delta に
        event.accept()

    def mousePressEvent(self, event):
        # 初期表示中はパンもしない
        if self._scroll_locked:
            super().mousePressEvent(event)
            return

        if event.button() == Qt.LeftButton:
            on_image = self._has_pixmap_item_under_cursor(event.position().toPoint())
            if not on_image:
                self._panning = True
                self._pan_start = event.globalPosition().toPoint()
                self._h0 = self.horizontalScrollBar().value()
                self._v0 = self.verticalScrollBar().value()
                self.setCursor(Qt.ClosedHandCursor)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning and self._pan_start is not None:
            cur = event.globalPosition().toPoint()
            dx = cur.x() - self._pan_start.x()
            dy = cur.y() - self._pan_start.y()
            self.horizontalScrollBar().setValue(self._h0 - dx)
            self.verticalScrollBar().setValue(self._v0 - dy)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._panning:
            self._panning = False
            self._pan_start = None
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _commit_image_scale_input(self):
        # 無効時（未読込）などは無視
        if not self._img_overlay.isEnabled():
            return

        text = (self._img_value.text() or "").strip()
        if not text:
            return

        try:
            p = float(text)
        except ValueError:
            return

        mw = self.window()
        if mw and hasattr(mw, "set_image_scale_percent"):
            mw.set_image_scale_percent(p)

        # 確定したらフォーカスを外す（任意）
        self._img_value.clearFocus()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  v{APP_VER}")
        self.setWindowIcon(QIcon(resource_path("icon.ico")))

        self.scene = QGraphicsScene(self)
        self.output_size = OutputSize(1920, 1080)

        self.bg_color = QColor("#FFFFFF")

        self.canvas_bg_item = QGraphicsRectItem(self.output_size.rect)
        self.canvas_bg_item.setZValue(-100)
        self.canvas_bg_item.setPen(QPen(Qt.NoPen))
        self.canvas_bg_item.setBrush(QBrush(self.bg_color))
        self.scene.addItem(self.canvas_bg_item)

        self.frame_item = QGraphicsRectItem(self.output_size.rect)
        self.frame_item.setZValue(10)
        self.frame_item.setBrush(QBrush(Qt.transparent))
        self.frame_item.setPen(QPen(Qt.white, 2))
        self.scene.addItem(self.frame_item)

        # ---- ガイド線（表示用）----
        self._show_center_guides = True
        self._show_taskbar_guide = True

        self._guide_pen = QPen(Qt.white, 1, Qt.DashLine)
        self._guide_pen.setCosmetic(True)  # ズームしても線幅1pxで一定

        self._guide_center_v = QGraphicsLineItem()
        self._guide_center_h = QGraphicsLineItem()
        self._guide_taskbar = QGraphicsLineItem()

        for it in (self._guide_center_v, self._guide_center_h, self._guide_taskbar):
            it.setZValue(9)  # 画像(-10)より上、枠(10)より下（枠より上にしたければ11）
            it.setPen(self._guide_pen)
            it.setAcceptedMouseButtons(Qt.NoButton)  # 操作の邪魔をしない
            it.setAcceptHoverEvents(False)
            self.scene.addItem(it)

        self._update_guides_geometry()
        self._update_guides_visibility()

        self.setAcceptDrops(True)

        # 設定（前回フォルダ等）を永続化
        self._settings = QSettings("user", APP_NAME)
        self._last_dir = self._settings.value("last_dir", str(Path.home()), type=str)

        self._recent_sizes: list[tuple[int, int]] = []
        self._recent_bg_colors: list[str] = []
        self._recent_size_actions: list[QAction] = []
        self._recent_bg_actions: list[QAction] = []

        self._load_recent_settings()

        self.image_item: MovableImageItem | None = None
        self._image_path: str | None = None

        self.view_fit_padding = 80

        self.view_zoom = 1.0
        self.max_view_zoom: float | None = None
        self._did_initial_fit = False

        self.view = ImageView(self.scene)
        self.setCentralWidget(self.view)
        self.view.request_fit_initial.connect(self.fit_canvas_to_window)
        self.view.request_preview.connect(self.show_export_preview)

        self.view.request_image_zoom_in.connect(lambda: self.nudge_image_scale_percent(+1.0))
        self.view.request_image_zoom_out.connect(lambda: self.nudge_image_scale_percent(-1.0))

        self._build_menus()
        self._apply_scene_rect()
        self._apply_canvas_appearance()

        # オーバーレイ表示を初期化
        self.view.set_canvas_size_text(self.output_size.w, self.output_size.h)

        self.scene.changed.connect(self._on_scene_changed)

        self.menuBar().setStyleSheet("""
        QMenuBar::item {
            padding-top: 4px;
            padding-bottom: 4px;
            padding-left: 8px;
            padding-right: 8px;
        }
        """)

        self._update_image_scale_overlay()

    def open_image_from_path(self, path: str) -> bool:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            QMessageBox.warning(self, "エラー", "画像を読み込めませんでした。")
            return False

        self._image_path = path
        self._set_image_menu_enabled(True)

        if self.image_item:
            self.scene.removeItem(self.image_item)
            self.image_item = None

        self.image_item = MovableImageItem(pixmap)
        self.scene.addItem(self.image_item)

        self._place_image_initial()
        self._clamp_image_pos()
        self._update_image_scale_overlay()
        self.fit_canvas_to_window()

        # 次回の「画像を開く」用に更新
        self._last_dir = str(Path(path).parent)
        self._settings.setValue("last_dir", self._last_dir)

        return True

    def showEvent(self, event):
        super().showEvent(event)
        if not self._did_initial_fit:
            self._did_initial_fit = True
            QTimer.singleShot(0, self.fit_canvas_to_window)

    # ---- 見た目（背景プレビュー＋枠色自動） ----
    def _apply_canvas_appearance(self):
        self.canvas_bg_item.setBrush(QBrush(self.bg_color))
        pen_color = self._auto_frame_color(self.bg_color)
        self.frame_item.setPen(QPen(pen_color, 2))

        if hasattr(self, "_guide_center_v"):
            self._update_guides_pen()

    @staticmethod
    def _auto_frame_color(bg: QColor) -> QColor:
        r = bg.red() / 255.0
        g = bg.green() / 255.0
        b = bg.blue() / 255.0
        luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
        return QColor("#000000") if luminance > 0.6 else QColor("#FFFFFF")

    # ---- シーン領域 ----
    def _apply_scene_rect(self):
        r = self.output_size.rect
        margin = 300
        self.scene.setSceneRect(QRectF(
            r.left() - margin, r.top() - margin,
            r.width() + margin * 2, r.height() + margin * 2
        ))

    # ---- ガイド線 ----
    def _update_guides_geometry(self):
        """キャンバスサイズに合わせてガイド線の位置を更新"""
        w = self.output_size.w
        h = self.output_size.h

        cx = w / 2.0
        cy = h / 2.0

        # 中心ガイドは少しはみ出す
        bleed = 40.0  # 好みで変更OK
        self._guide_center_v.setLine(cx, -bleed, cx, h + bleed)
        self._guide_center_h.setLine(-bleed, cy, w + bleed, cy)

        # タスクバー線：下から48px、はみ出さない
        y = h - 48.0
        self._guide_taskbar.setLine(0.0, y, float(w), y)

    def _update_guides_pen(self):
        """フレーム色と同期してガイド線の色も更新"""
        # frame_item のペン色を使う（auto_frame_colorの結果）
        c = self.frame_item.pen().color()
        p = QPen(c, 1, Qt.DashLine)
        p.setCosmetic(True)

        self._guide_center_v.setPen(p)
        self._guide_center_h.setPen(p)
        self._guide_taskbar.setPen(p)

    def _update_guides_visibility(self):
        self._guide_center_v.setVisible(self._show_center_guides)
        self._guide_center_h.setVisible(self._show_center_guides)
        self._guide_taskbar.setVisible(self._show_taskbar_guide)

    # ---- メニュー ----
    def _build_menus(self):
        # --- File ---
        file_menu = self.menuBar().addMenu("ファイル(&F)")

        act_open = QAction("画像を開く", self)
        act_open.setShortcut(QKeySequence.Open)  # Ctrl+O
        act_open.triggered.connect(self.open_image)
        file_menu.addAction(act_open)

        act_save = QAction("書き出し", self)
        act_save.setShortcut(QKeySequence.Save)  # Ctrl+S
        act_save.triggered.connect(self.export_image)
        file_menu.addAction(act_save)

        file_menu.addSeparator()

        act_clear_image = QAction("画像を消す", self)
        act_clear_image.setShortcut(QKeySequence("Ctrl+Delete"))
        act_clear_image.triggered.connect(self.clear_image)
        file_menu.addAction(act_clear_image)

        # --- Image ---
        self.image_menu = self.menuBar().addMenu("画像(&I)")

        act_fit_width = QAction("幅に合わせる", self)
        act_fit_width.setShortcut(QKeySequence("Ctrl+W"))
        act_fit_width.triggered.connect(self.fit_image_to_width)
        self.image_menu.addAction(act_fit_width)

        act_fit_height = QAction("高さに合わせる", self)
        act_fit_height.setShortcut(QKeySequence("Ctrl+H"))
        act_fit_height.triggered.connect(self.fit_image_to_height)
        self.image_menu.addAction(act_fit_height)

        act_fit_all = QAction("全体が入るように合わせる", self)
        act_fit_all.setShortcut(QKeySequence("Ctrl+Shift+F"))
        act_fit_all.triggered.connect(self.fit_image_contain)
        self.image_menu.addAction(act_fit_all)

        act_reset_pos = QAction("初期位置に戻す", self)
        act_reset_pos.setShortcut(QKeySequence("Ctrl+R"))
        act_reset_pos.triggered.connect(self.reset_image_to_initial)
        self.image_menu.addAction(act_reset_pos)

        self.image_menu.addSeparator()

        # 整列系（機能は残す）
        act_center = QAction("中央に配置", self)
        act_center.triggered.connect(self.align_center)
        self.image_menu.addAction(act_center)

        act_center_v = QAction("上下中央揃え", self)
        act_center_v.triggered.connect(self.align_center_vertical)
        self.image_menu.addAction(act_center_v)

        act_center_h = QAction("左右中央揃え", self)
        act_center_h.triggered.connect(self.align_center_horizontal)
        self.image_menu.addAction(act_center_h)

        self.image_menu.addSeparator()

        act_top = QAction("上端に合わせる", self)
        act_top.triggered.connect(self.align_top)
        self.image_menu.addAction(act_top)

        act_bottom = QAction("下端に合わせる", self)
        act_bottom.triggered.connect(self.align_bottom)
        self.image_menu.addAction(act_bottom)

        act_left = QAction("左端に合わせる", self)
        act_left.triggered.connect(self.align_left)
        self.image_menu.addAction(act_left)

        act_right = QAction("右端に合わせる", self)
        act_right.triggered.connect(self.align_right)
        self.image_menu.addAction(act_right)

        self.image_menu.addSeparator()

        act_reset_scale = QAction("画像サイズを100%にする", self)
        act_reset_scale.setShortcut(QKeySequence("Ctrl+0"))
        act_reset_scale.triggered.connect(self.reset_image_scale_100)
        self.image_menu.addAction(act_reset_scale)

        # ★ 無効化対象をまとめて保持（separator は QAction じゃないので除外）
        self._image_actions = [
            act_fit_width, act_fit_height, act_fit_all, act_reset_pos,
            act_center, act_center_v, act_center_h,
            act_top, act_bottom, act_left, act_right,
            act_reset_scale,
        ]

        # ★ 初期状態は無効
        self._set_image_menu_enabled(False)

        # --- Size ---
        self.size_menu = self.menuBar().addMenu("サイズ(&S)")
        for label, w, h in [
            ("1920×1080（横）", 1920, 1080),
            ("1080×1920（縦）", 1080, 1920),
        ]:
            act = QAction(label, self)
            act.triggered.connect(lambda _=False, ww=w, hh=h: self.set_output_size(ww, hh))
            self.size_menu.addAction(act)

        act_custom = QAction("カスタム", self)
        act_custom.triggered.connect(self.set_custom_size)
        self.size_menu.addSeparator()
        self.size_menu.addAction(act_custom)

        # ★ ここで「記憶したサイズ」をメニューに追加
        self._rebuild_recent_size_menu()


        # --- Background ---
        self.bg_menu = self.menuBar().addMenu("背景色(&B)")

        act_bg_dark = QAction("#251E1C（ダーク）", self)
        act_bg_dark.triggered.connect(lambda: self.set_bg_color(QColor("#251E1C")))
        self.bg_menu.addAction(act_bg_dark)

        act_bg_white = QAction("#FFFFFF（白）", self)
        act_bg_white.triggered.connect(lambda: self.set_bg_color(QColor("#FFFFFF")))
        self.bg_menu.addAction(act_bg_white)

        act_bg_pick = QAction("指定", self)
        act_bg_pick.triggered.connect(self.pick_bg_color)
        self.bg_menu.addSeparator()
        self.bg_menu.addAction(act_bg_pick)

        # ★ ここで「記憶した背景色」をメニューに追加
        self._rebuild_recent_bg_menu()


        # --- View / 表示 ---
        view_menu = self.menuBar().addMenu("表示(&V)")

        act_center_guides = QAction("中心ガイド（十字点線）", self)
        act_center_guides.setCheckable(True)
        act_center_guides.setChecked(True)
        act_center_guides.setShortcut(QKeySequence("Ctrl+G"))  # 好みで変更OK
        act_center_guides.toggled.connect(self.set_center_guides_visible)
        view_menu.addAction(act_center_guides)

        act_taskbar = QAction("タスクバーガイド（下48px）", self)
        act_taskbar.setCheckable(True)
        act_taskbar.setChecked(True)
        act_taskbar.setShortcut(QKeySequence("Ctrl+T"))  # 好みで変更OK
        act_taskbar.toggled.connect(self.set_taskbar_guide_visible)
        view_menu.addAction(act_taskbar)


        # --- Help ---
        help_menu = self.menuBar().addMenu("ヘルプ(&H)")

        act_about = QAction("バージョン情報", self)
        act_about.setShortcut(QKeySequence.HelpContents)  # F1
        act_about.triggered.connect(self.show_about)
        help_menu.addAction(act_about)


        # --- menu書式設定 ---
        TOP_BOTTOM_MARGIN = 4
        for m in (file_menu, self.image_menu, self.size_menu, self.bg_menu, view_menu, help_menu):
            m.setContentsMargins(0, TOP_BOTTOM_MARGIN, 0, TOP_BOTTOM_MARGIN)

        SEPARATOR_LR_MARGIN = 8
        SEPARATOR_TB_MARGIN = 4

        menu_qss = f"""
        QMenu::separator {{
            margin-left: {SEPARATOR_LR_MARGIN}px;
            margin-right: {SEPARATOR_LR_MARGIN}px;
            margin-top: {SEPARATOR_TB_MARGIN}px;
            margin-bottom: {SEPARATOR_TB_MARGIN}px;
            height: 1px;
        }}
        """

        for m in (file_menu, self.image_menu, self.size_menu, self.bg_menu, view_menu, help_menu):
            m.setStyleSheet(menu_qss)

        # --- ショートカット一覧（左下オーバーレイ）を更新 ---
        self._bind_shortcut_overlay([
            act_open,
            act_save,
            act_clear_image,

            None,  # 区切り線（ファイル系 / 画像系）
            act_fit_width,
            act_fit_height,
            act_fit_all,
            act_reset_pos,
            act_reset_scale,

            None,  # 区切り線（画像系 / 表示系）
            act_center_guides,
            act_taskbar,
        ])

    def _set_image_menu_enabled(self, enabled: bool):
        enabled = bool(enabled)
        if hasattr(self, "image_menu"):
            self.image_menu.setEnabled(enabled)
        if hasattr(self, "_image_actions"):
            for a in self._image_actions:
                a.setEnabled(enabled)

    def _bind_shortcut_overlay(self, actions: list[QAction | None]):
        # 参照を保持（あとで更新に使う）
        self._shortcut_actions = actions

        # 既に bind 済みなら一回外す（多重 connect 防止）
        if hasattr(self, "_shortcut_refresh") and hasattr(self, "_shortcut_bound_actions"):
            for a in self._shortcut_bound_actions:
                if a is None:
                    continue
                try:
                    a.changed.disconnect(self._shortcut_refresh)
                except Exception:
                    pass

        def refresh():
            self.view.set_shortcut_actions(self._shortcut_actions)

        self._shortcut_refresh = refresh
        self._shortcut_bound_actions = list(actions)

        for a in actions:
            if a is None:
                continue
            a.changed.connect(self._shortcut_refresh)

        refresh()

    def clear_image(self):
        """現在の画像を削除し、未読込状態に戻す（確認ダイアログ付き）"""
        if not self.image_item:
            return

        ret = QMessageBox.question(
            self,
            "確認",
            "画像を削除しますか？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if ret != QMessageBox.Yes:
            return

        # シーンから削除
        self.scene.removeItem(self.image_item)
        self.image_item = None
        self._image_path = None

        # Image メニューを無効化
        self._set_image_menu_enabled(False)

        # 画像倍率オーバーレイを未読込状態に
        self.view.set_image_scale_percent(None)

        # 表示を整える（キャンバス初期位置）
        QTimer.singleShot(0, self.fit_canvas_to_window)

    # ---- バージョン情報 ----
    def show_about(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("バージョン情報")
        dlg.setModal(True)
        dlg.setWindowIcon(self.windowIcon())

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        # ===== ヘッダ（アイコン + アプリ名 + バージョン）=====
        header = QWidget(dlg)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(12)

        icon_label = QLabel()
        icon_label.setPixmap(self.windowIcon().pixmap(48, 48))
        icon_label.setAlignment(Qt.AlignTop)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)

        title = QLabel(APP_NAME)
        title.setStyleSheet("font-weight: bold; font-size: 14px;")

        version = QLabel(f"v{APP_VER}")
        version.setStyleSheet("color: #AAAAAA; font-size: 11px;")

        text_layout.addWidget(title)
        text_layout.addWidget(version)

        header_layout.addWidget(icon_label)
        header_layout.addLayout(text_layout)
        header_layout.addStretch()

        # ===== 説明・著作権 =====
        desc = QLabel(
            "固定サイズ切り抜きツール\n\n"
            "© 2026 ヨニキ. All rights reserved.\n"
            "supported by PRIMROSE"
        )
        desc.setAlignment(Qt.AlignCenter)
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #666666; font-size: 11px;")

        # ===== OKボタン =====
        btn = QPushButton("OK")
        btn.setDefault(True)
        btn.clicked.connect(dlg.accept)

        # ===== レイアウト =====
        layout.addWidget(header)
        layout.addSpacing(12)
        layout.addWidget(desc)
        layout.addSpacing(12)
        layout.addWidget(btn, alignment=Qt.AlignCenter)

        dlg.exec()

    # ---- 表示倍率（View） ----
    def _update_scroll_lock(self):
        if self.max_view_zoom is None:
            self.view.set_scroll_locked(False)
            return

        locked = abs(self.view_zoom - self.max_view_zoom) < 1.0
        self.view.set_scroll_locked(locked)
        if locked:
            self.view.centerOn(self.output_size.rect.center())

    def _update_image_scale_overlay(self):
        """画像の実倍率（scale=1.0 を 100%）をオーバーレイに反映"""
        if not self.image_item:
            self.view.set_image_scale_percent(None)
            return
        s = float(self.image_item.scale())
        self.view.set_image_scale_percent(s * 100.0)

    def set_view_zoom(self, z: float):
        z = float(z)
        min_z = 0.1
        max_z = float(self.max_view_zoom) if self.max_view_zoom is not None else 4.0
        z = max(min_z, min(max_z, z))

        self.view_zoom = z
        self._apply_view_transform()
        self._update_scroll_lock()

    def nudge_view_zoom(self, zoom_in: bool):
        factor = 1.10 if zoom_in else 1 / 1.10
        self.set_view_zoom(self.view_zoom * factor)

    def _apply_view_transform(self):
        self.view.resetTransform()
        self.view.scale(self.view_zoom, self.view_zoom)

    def fit_canvas_to_window(self):
        # ★fit計算前にスクロールバーを消して viewport を安定させる
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.view.resetTransform()
        pad = float(self.view_fit_padding)
        r = self.output_size.rect.adjusted(-pad, -pad, pad, pad)
        self.view.fitInView(r, Qt.KeepAspectRatio)

        t = self.view.transform()
        self.view_zoom = float(t.m11())
        self.max_view_zoom = self.view_zoom

        # ★初期位置＝ロック
        self.view.set_scroll_locked(True)
        self.view.centerOn(self.output_size.rect.center())

        # ★オーバーレイ再配置（rangeChanged→viewport変化対策）
        QTimer.singleShot(0, self.view._reposition_overlays)

    def nudge_image_scale_percent(self, delta_percent: float):
        if not self.image_item:
            return
        cur = float(self.image_item.scale()) * 100.0
        self.set_image_scale_percent(cur + float(delta_percent))

    def set_image_scale_percent(self, percent: float):
        """画像倍率(%)を指定して反映。scale=1.0が100%"""
        if not self.image_item:
            return

        p = float(percent)
        p = max(1.0, min(5000.0, p))
        new_s = p / 100.0

        # キャンバス中心を基準に拡縮（既存zoom_imageと同じ挙動）
        center = self.output_size.rect.center()
        item = self.image_item

        item_local = item.mapFromScene(center)
        item.setScale(new_s)
        new_center_scene = item.mapToScene(item_local)
        item.setPos(item.pos() + (center - new_center_scene))

        self._clamp_image_pos()
        self._update_image_scale_overlay()

    # ---- 設定変更 ----
    def nudge_image_pos(self, dx: int, dy: int):
        if not self.image_item:
            return
        p = self.image_item.pos()
        self.image_item.setPos(p.x() + dx, p.y() + dy)
        self._clamp_image_pos()

    # ---- 設定変更 ----
    def set_bg_color(self, c: QColor):
        self.bg_color = c
        self._apply_canvas_appearance()

    def pick_bg_color(self):
        c = QColorDialog.getColor(self.bg_color, self, "背景色を選択")
        if c.isValid():
            self.set_bg_color(c)
            self._remember_bg_color(c)

    def set_output_size(self, w: int, h: int):
        self.output_size = OutputSize(int(w), int(h))

        self.canvas_bg_item.setRect(self.output_size.rect)
        self.frame_item.setRect(self.output_size.rect)

        if hasattr(self, "_guide_center_v"):
            self._update_guides_geometry()

        self._apply_scene_rect()
        self._apply_canvas_appearance()

        # メイン右上オーバーレイ更新
        self.view.set_canvas_size_text(self.output_size.w, self.output_size.h)

        if self.image_item:
            self._place_image_initial()

        # まず即時にfit（崩れる瞬間を作らない）
        self.fit_canvas_to_window()

        QTimer.singleShot(0, self.fit_canvas_to_window)

    def set_custom_size(self):
        w, ok = QInputDialog.getInt(self, "カスタムサイズ", "幅（px）", self.output_size.w, 1, 20000, 1)
        if not ok:
            return
        h, ok = QInputDialog.getInt(self, "カスタムサイズ", "高さ（px）", self.output_size.h, 1, 20000, 1)
        if not ok:
            return
        self.set_output_size(w, h)
        self._remember_custom_size(w, h)

    # ---- 画像ロード ----
    def open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "画像を開く",
            self._last_dir,
            "画像 (*.png *.jpg *.jpeg *.jfif *.bmp *.webp);;すべて (*.*)"
        )
        if not path:
            return

        # 次回用にフォルダを保存
        self._last_dir = str(Path(path).parent)
        self._settings.setValue("last_dir", self._last_dir)
        self.open_image_from_path(path)

    def _place_image_initial(self):
        if not self.image_item:
            return
        pm = self.image_item.pixmap()
        if pm.isNull():
            return

        fw, fh = self.output_size.w, self.output_size.h
        iw, ih = pm.width(), pm.height()

        s = min(fw / iw, fh / ih)
        self.image_item.setScale(s)

        scaled_w = iw * s
        scaled_h = ih * s
        self.image_item.setPos((fw - scaled_w) / 2, (fh - scaled_h) / 2)

        self._clamp_image_pos()
        self._update_image_scale_overlay()

    # ---- 画像の合わせ方 ----
    def fit_image_to_width(self):
        if not self._require_image():
            return

        fw = self.output_size.w
        pm = self.image_item.pixmap()
        iw = pm.width()

        s = fw / iw
        self._apply_image_scale_and_center(s)

    def reset_image_to_initial(self):
        if not self._require_image():
            return
        self._place_image_initial()

    def fit_image_to_height(self):
        if not self._require_image():
            return

        fh = self.output_size.h
        pm = self.image_item.pixmap()
        ih = pm.height()

        s = fh / ih
        self._apply_image_scale_and_center(s)


    def fit_image_contain(self):
        if not self._require_image():
            return

        fw, fh = self.output_size.w, self.output_size.h
        pm = self.image_item.pixmap()
        iw, ih = pm.width(), pm.height()

        s = min(fw / iw, fh / ih)
        self._apply_image_scale_and_center(s)


    def _apply_image_scale_and_center(self, scale: float):
        item = self.image_item
        fw, fh = self.output_size.w, self.output_size.h

        item.setScale(scale)

        iw = item.pixmap().width() * scale
        ih = item.pixmap().height() * scale

        item.setPos((fw - iw) / 2, (fh - ih) / 2)
        self._clamp_image_pos()
        self._update_image_scale_overlay()

    # ---- 画像整列 ----
    def _require_image(self) -> bool:
        if not self.image_item:
            QMessageBox.information(self, "情報", "先に画像を開いてください。")
            return False
        return True

    def _scaled_image_size(self) -> tuple[float, float]:
        pm = self.image_item.pixmap()
        s = float(self.image_item.scale())
        return float(pm.width()) * s, float(pm.height()) * s

    def align_center(self):
        if not self._require_image():
            return
        fw, fh = self.output_size.w, self.output_size.h
        iw, ih = self._scaled_image_size()
        self.image_item.setPos((fw - iw) / 2, (fh - ih) / 2)
        self._clamp_image_pos()

    def align_center_vertical(self):
        if not self._require_image():
            return
        fh = self.output_size.h
        _, ih = self._scaled_image_size()
        x = float(self.image_item.pos().x())
        y = (fh - ih) / 2
        self.image_item.setPos(x, y)
        self._clamp_image_pos()

    def align_center_horizontal(self):
        if not self._require_image():
            return
        fw = self.output_size.w
        iw, _ = self._scaled_image_size()
        x = (fw - iw) / 2
        y = float(self.image_item.pos().y())
        self.image_item.setPos(x, y)
        self._clamp_image_pos()

    def align_top(self):
        if not self._require_image():
            return
        self.image_item.setPos(self.image_item.pos().x(), 0.0)
        self._clamp_image_pos()

    def align_bottom(self):
        if not self._require_image():
            return
        fh = self.output_size.h
        _, ih = self._scaled_image_size()
        self.image_item.setPos(self.image_item.pos().x(), fh - ih)
        self._clamp_image_pos()

    def align_left(self):
        if not self._require_image():
            return
        self.image_item.setPos(0.0, self.image_item.pos().y())
        self._clamp_image_pos()

    def align_right(self):
        if not self._require_image():
            return
        fw = self.output_size.w
        iw, _ = self._scaled_image_size()
        self.image_item.setPos(fw - iw, self.image_item.pos().y())
        self._clamp_image_pos()

    # ---- 画像の初期サイズ（100%） ----
    def reset_image_scale_100(self):
        if not self._require_image():
            return

        center = self.output_size.rect.center()
        item = self.image_item

        item_local = item.mapFromScene(center)
        item.setScale(1.0)
        new_center_scene = item.mapToScene(item_local)
        item.setPos(item.pos() + (center - new_center_scene))

        self._clamp_image_pos()
        self._update_image_scale_overlay()

    # ---- 画像ズーム（Ctrl+ホイール、画像上） ----
    def zoom_image(self, zoom_in: bool, step: str = "normal"):
        """
        step:
          - "normal": 既存（約15%刻み）
          - "fine"  : 微調整（約3%刻み）
        """
        if not self.image_item:
            return

        if step == "ultra":
            base = 1.01
        elif step == "fine":
            base = 1.03
        else:
            base = 1.15

        factor = base if zoom_in else (1 / base)

        old_s = float(self.image_item.scale())
        new_s = max(0.02, min(50.0, old_s * factor))
        if abs(new_s - old_s) < 1e-12:
            return

        # キャンバス中心を基準に拡縮（挙動は現状踏襲）
        center = self.output_size.rect.center()
        item = self.image_item

        item_local = item.mapFromScene(center)
        item.setScale(new_s)
        new_center_scene = item.mapToScene(item_local)
        item.setPos(item.pos() + (center - new_center_scene))

        self._clamp_image_pos()
        self._update_image_scale_overlay()

    # ---- 生成（書き出し/プレビュー共通） ----
    def _render_output_pil(self) -> Image.Image:
        bg = (self.bg_color.red(), self.bg_color.green(), self.bg_color.blue())
        canvas = Image.new("RGBA", (self.output_size.w, self.output_size.h), bg + (255,))

        if not self._image_path or not self.image_item:
            return canvas

        with Image.open(self._image_path) as im:
            # EXIFの向き情報を反映して、見た目通りの向きに揃える
            src = ImageOps.exif_transpose(im)

        # 透過を保持するため RGBA に統一（透過なし画像でもOK）
        if src.mode != "RGBA":
            src = src.convert("RGBA")

        s = float(self.image_item.scale())
        x = int(round(float(self.image_item.pos().x())))
        y = int(round(float(self.image_item.pos().y())))

        new_w = max(1, int(round(src.width * s)))
        new_h = max(1, int(round(src.height * s)))
        resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # 透過込みで背景に合成（透明部分は背景色が見える）
        canvas.paste(resized, (x, y), resized)

        return canvas.convert("RGB")

    @staticmethod
    def _pil_to_qpixmap(img: Image.Image) -> QPixmap:
        buf = BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        qimg = QImage.fromData(data, "PNG")
        return QPixmap.fromImage(qimg)

    def show_export_preview(self):
        try:
            pil = self._render_output_pil()
            pm = self._pil_to_qpixmap(pil)
            dlg = OutputPreviewDialog(pm, "書き出しプレビュー", self)
            dlg.exec()
        except Exception as e:
            QMessageBox.warning(self, "エラー", f"プレビュー生成に失敗しました。\n{e}")

    # ---- 移動制約 ----
    def _clamp_image_pos(self):
        if not self.image_item:
            return

        fw, fh = self.output_size.w, self.output_size.h
        pm = self.image_item.pixmap()
        s = float(self.image_item.scale())
        iw, ih = float(pm.width()) * s, float(pm.height()) * s

        pos = self.image_item.pos()
        x, y = float(pos.x()), float(pos.y())

        min_x = -iw + fw * 0.25
        max_x = fw - fw * 0.25
        min_y = -ih + fh * 0.25
        max_y = fh - fh * 0.25

        x = min(max_x, max(min_x, x))
        y = min(max_y, max(min_y, y))
        self.image_item.setPos(x, y)

    def _on_scene_changed(self, _):
        if self.image_item:
            self._clamp_image_pos()

    # ---- 書き出し ----
    def export_image(self):
        # 元ファイル名_c をデフォルト名にする
        if self._image_path:
            src = Path(self._image_path)
            default_name = f"{src.stem}_c{src.suffix}"
            default_path = str(src.with_name(default_name))
        else:
            default_path = str(Path.home() / "output_c.png")

        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "書き出し",
            default_path,
            "PNG (*.png);;JPEG (*.jpg *.jpeg);;すべて (*.*)"
        )
        if not out_path:
            return

        try:
            canvas = self._render_output_pil()

            suffix = Path(out_path).suffix.lower()
            if suffix in [".jpg", ".jpeg"]:
                canvas.save(out_path, quality=95, subsampling=0)
            else:
                canvas.save(out_path)

        except Exception as e:
            QMessageBox.warning(self, "エラー", f"書き出しに失敗しました。\n{e}")
            return

        QMessageBox.information(self, "完了", "書き出しました。")

    # =============================
    # 記憶（カスタムサイズ / 背景色）
    # =============================
    def _load_recent_settings(self):
        # サイズ：["1920x1080", ...]
        raw_sizes = self._settings.value("recent/custom_sizes", [], type=list)
        sizes: list[tuple[int, int]] = []
        for s in raw_sizes:
            try:
                w_str, h_str = str(s).split("x")
                w, h = int(w_str), int(h_str)
                if w > 0 and h > 0:
                    sizes.append((w, h))
            except Exception:
                pass
        self._recent_sizes = sizes

        # 背景色：["#RRGGBB", ...]
        raw_cols = self._settings.value("recent/bg_colors", [], type=list)
        cols: list[str] = []
        for c in raw_cols:
            c = str(c).strip()
            if c.startswith("#") and len(c) == 7:
                cols.append(c.upper())
        self._recent_bg_colors = cols

    def _save_recent_settings(self):
        self._settings.setValue("recent/custom_sizes", [f"{w}x{h}" for w, h in self._recent_sizes])
        self._settings.setValue("recent/bg_colors", list(self._recent_bg_colors))

    def _remember_custom_size(self, w: int, h: int):
        key = (int(w), int(h))
        # 先頭に移動（重複排除）
        self._recent_sizes = [s for s in self._recent_sizes if s != key]
        self._recent_sizes.insert(0, key)
        self._recent_sizes = self._recent_sizes[:10]  # 最大10件
        self._save_recent_settings()
        self._rebuild_recent_size_menu()

    def _remember_bg_color(self, c: QColor):
        s = c.name().upper()  # "#RRGGBB"
        self._recent_bg_colors = [x for x in self._recent_bg_colors if x != s]
        self._recent_bg_colors.insert(0, s)
        self._recent_bg_colors = self._recent_bg_colors[:10]
        self._save_recent_settings()
        self._rebuild_recent_bg_menu()

    def _rebuild_recent_size_menu(self):
        if not hasattr(self, "size_menu"):
            return

        # 既存の動的アクションを削除
        for a in getattr(self, "_recent_size_actions", []):
            self.size_menu.removeAction(a)
        self._recent_size_actions = []

        if not self._recent_sizes:
            return

        # 区切り
        sep = QAction(self)
        sep.setSeparator(True)
        self.size_menu.addAction(sep)
        self._recent_size_actions.append(sep)

        title = QAction("最近のカスタム", self)
        title.setEnabled(False)
        self.size_menu.addAction(title)
        self._recent_size_actions.append(title)

        for (w, h) in self._recent_sizes:
            act = QAction(f"{w}×{h}", self)
            act.triggered.connect(lambda _=False, ww=w, hh=h: self.set_output_size(ww, hh))
            self.size_menu.addAction(act)
            self._recent_size_actions.append(act)

        clear_act = QAction("最近のカスタムをクリア", self)
        clear_act.triggered.connect(self._clear_recent_sizes)
        self.size_menu.addAction(clear_act)
        self._recent_size_actions.append(clear_act)

    def _rebuild_recent_bg_menu(self):
        if not hasattr(self, "bg_menu"):
            return

        for a in getattr(self, "_recent_bg_actions", []):
            self.bg_menu.removeAction(a)
        self._recent_bg_actions = []

        if not self._recent_bg_colors:
            return

        sep = QAction(self)
        sep.setSeparator(True)
        self.bg_menu.addAction(sep)
        self._recent_bg_actions.append(sep)

        title = QAction("最近の指定色", self)
        title.setEnabled(False)
        self.bg_menu.addAction(title)
        self._recent_bg_actions.append(title)

        for s in self._recent_bg_colors:
            act = QAction(s, self)
            act.triggered.connect(lambda _=False, ss=s: self.set_bg_color(QColor(ss)))
            self.bg_menu.addAction(act)
            self._recent_bg_actions.append(act)

        clear_act = QAction("最近の指定色をクリア", self)
        clear_act.triggered.connect(self._clear_recent_bg_colors)
        self.bg_menu.addAction(clear_act)
        self._recent_bg_actions.append(clear_act)

    def _clear_recent_sizes(self):
        ret = QMessageBox.question(self, "確認", "最近のカスタムサイズをクリアしますか？",
                                  QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self._recent_sizes = []
        self._save_recent_settings()
        self._rebuild_recent_size_menu()

    def _clear_recent_bg_colors(self):
        ret = QMessageBox.question(self, "確認", "最近の指定色をクリアしますか？",
                                  QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self._recent_bg_colors = []
        self._save_recent_settings()
        self._rebuild_recent_bg_menu()


    # --- トグル用メソッド ---
    def set_center_guides_visible(self, visible: bool):
        self._show_center_guides = bool(visible)
        self._update_guides_visibility()

    def set_taskbar_guide_visible(self, visible: bool):
        self._show_taskbar_guide = bool(visible)
        self._update_guides_visibility()