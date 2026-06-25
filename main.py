"""
POS Printer Emulator
====================
Emulates a thermal receipt printer over WiFi (TCP) and Bluetooth (RFCOMM).
Renders ESC/POS commands into a responsive on-screen receipt.

Dependencies:
    pip install PyQt6 pyserial netifaces2
    Bluetooth: Windows uses built-in socket (no extra library needed).
               Just make sure Bluetooth is ON in Windows Settings.
"""

import sys
import socket
import threading
import time
import struct
import re
import netifaces
from datetime import datetime
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QScrollArea, QFrame, QPushButton, QStatusBar,
    QSplitter, QTextEdit, QGroupBox, QLineEdit, QSpinBox,
    QCheckBox, QTabWidget, QSizePolicy, QToolButton, QMenu,
    QFileDialog, QMessageBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QSize
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QPixmap, QPainter, QPen, QFontDatabase,
    QTextDocument, QTextCursor, QTextCharFormat, QTextBlockFormat,
    QIcon, QAction
)

import platform

# Windows has native Bluetooth RFCOMM support via the built-in socket module
# (AF_BTH / BTHPROTO_RFCOMM) — no PyBluez needed.
# On Linux/Mac we attempt PyBluez as fallback.
BLUETOOTH_AVAILABLE = False
BT_BACKEND = None  # 'windows' | 'pybluez' | None

if platform.system() == "Windows":
    try:
        import socket as _test_sock
        # AF_BTH = 32 on Windows
        _s = _test_sock.socket(32, _test_sock.SOCK_STREAM, 3)  # BTHPROTO_RFCOMM=3
        _s.close()
        BLUETOOTH_AVAILABLE = True
        BT_BACKEND = 'windows'
    except OSError:
        # Bluetooth radio not present or disabled
        BLUETOOTH_AVAILABLE = False
        BT_BACKEND = None
else:
    try:
        import bluetooth  # noqa: F401
        BLUETOOTH_AVAILABLE = True
        BT_BACKEND = 'pybluez'
    except ImportError:
        BLUETOOTH_AVAILABLE = False
        BT_BACKEND = None

# ─────────────────────────────────────────────
#  ESC/POS Parser
# ─────────────────────────────────────────────

class EscPosParser:
    """Parse ESC/POS byte streams into structured receipt lines."""

    # Text alignment
    ALIGN_LEFT   = 0
    ALIGN_CENTER = 1
    ALIGN_RIGHT  = 2

    # Text emphasis
    BOLD      = 1 << 0
    UNDERLINE = 1 << 1
    DOUBLE_H  = 1 << 2
    DOUBLE_W  = 1 << 3
    INVERT    = 1 << 4

    def __init__(self):
        self.reset()

    def reset(self):
        self.lines = []          # list of ReceiptLine dicts
        self._buf = bytearray()
        self._align = self.ALIGN_LEFT
        self._emphasis = 0
        self._char_size = 0
        self._underline = 0
        self._cut = False
        self._current_line = []

    def feed(self, data: bytes):
        self._buf.extend(data)
        self._process()

    def _process(self):
        i = 0
        buf = self._buf
        n = len(buf)

        while i < n:
            b = buf[i]

            # ── Printable ASCII ──────────────────────────
            if 0x20 <= b <= 0x7E or b >= 0x80:
                self._current_line.append(chr(b))
                i += 1

            # ── LF – flush line ──────────────────────────
            elif b == 0x0A:
                self._flush_line()
                i += 1

            # ── CR ───────────────────────────────────────
            elif b == 0x0D:
                i += 1

            # ── ESC sequences ────────────────────────────
            elif b == 0x1B:
                if i + 1 >= n:
                    break  # wait for more data
                cmd = buf[i + 1]

                # ESC @ – initialize
                if cmd == 0x40:
                    self._flush_line()
                    self._align = self.ALIGN_LEFT
                    self._emphasis = 0
                    self._char_size = 0
                    self._underline = 0
                    i += 2

                # ESC a – alignment
                elif cmd == 0x61:
                    if i + 2 >= n: break
                    self._align = buf[i + 2] & 0x03
                    i += 3

                # ESC E – bold on/off
                elif cmd == 0x45:
                    if i + 2 >= n: break
                    if buf[i + 2]:
                        self._emphasis |= self.BOLD
                    else:
                        self._emphasis &= ~self.BOLD
                    i += 3

                # ESC - – underline
                elif cmd == 0x2D:
                    if i + 2 >= n: break
                    self._underline = buf[i + 2]
                    if self._underline:
                        self._emphasis |= self.UNDERLINE
                    else:
                        self._emphasis &= ~self.UNDERLINE
                    i += 3

                # ESC ! – print mode
                elif cmd == 0x21:
                    if i + 2 >= n: break
                    mode = buf[i + 2]
                    self._emphasis = 0
                    if mode & 0x08: self._emphasis |= self.BOLD
                    if mode & 0x80: self._emphasis |= self.UNDERLINE
                    if mode & 0x10: self._emphasis |= self.DOUBLE_H
                    if mode & 0x20: self._emphasis |= self.DOUBLE_W
                    i += 3

                # ESC G – double-strike (treat as bold)
                elif cmd == 0x47:
                    if i + 2 >= n: break
                    if buf[i + 2]:
                        self._emphasis |= self.BOLD
                    i += 3

                # ESC d – feed N lines
                elif cmd == 0x64:
                    if i + 2 >= n: break
                    count = buf[i + 2]
                    self._flush_line()
                    for _ in range(count):
                        self.lines.append({'type': 'blank'})
                    i += 3

                # ESC t – code table (skip)
                elif cmd == 0x74:
                    if i + 2 >= n: break
                    i += 3

                # ESC M – font (skip)
                elif cmd == 0x4D:
                    if i + 2 >= n: break
                    i += 3

                else:
                    i += 2  # skip unknown ESC + 1

            # ── GS sequences ─────────────────────────────
            elif b == 0x1D:
                if i + 1 >= n: break
                cmd = buf[i + 1]

                # GS ! – char size
                if cmd == 0x21:
                    if i + 2 >= n: break
                    self._char_size = buf[i + 2]
                    h = (self._char_size >> 4) & 0x07
                    w = self._char_size & 0x07
                    if h > 0: self._emphasis |= self.DOUBLE_H
                    else:     self._emphasis &= ~self.DOUBLE_H
                    if w > 0: self._emphasis |= self.DOUBLE_W
                    else:     self._emphasis &= ~self.DOUBLE_W
                    i += 3

                # GS B – invert
                elif cmd == 0x42:
                    if i + 2 >= n: break
                    if buf[i + 2]:
                        self._emphasis |= self.INVERT
                    else:
                        self._emphasis &= ~self.INVERT
                    i += 3

                # GS V – cut paper
                elif cmd == 0x56:
                    if i + 2 >= n: break
                    self._flush_line()
                    self.lines.append({'type': 'cut'})
                    i += 3

                # GS k – barcode (skip entire block, variable length)
                elif cmd == 0x6B:
                    if i + 2 >= n: break
                    bc_type = buf[i + 2]
                    if bc_type <= 6:
                        # NUL-terminated
                        end = buf.find(b'\x00', i + 3)
                        if end == -1: break
                        bc_data = buf[i + 3:end].decode('ascii', errors='replace')
                        self._flush_line()
                        self.lines.append({'type': 'barcode', 'data': bc_data})
                        i = end + 1
                    else:
                        if i + 3 >= n: break
                        length = buf[i + 3]
                        if i + 4 + length > n: break
                        bc_data = buf[i + 4:i + 4 + length].decode('ascii', errors='replace')
                        self._flush_line()
                        self.lines.append({'type': 'barcode', 'data': bc_data})
                        i += 4 + length

                # GS h / GS w – barcode height/width (skip)
                elif cmd in (0x68, 0x77):
                    if i + 2 >= n: break
                    i += 3

                # GS H – barcode HRI (skip)
                elif cmd == 0x48:
                    if i + 2 >= n: break
                    i += 3

                else:
                    i += 2

            # ── DLE / other control – skip ────────────────
            else:
                i += 1

        self._buf = bytearray(buf[i:])

    def _flush_line(self):
        text = ''.join(self._current_line).rstrip()
        self._current_line = []
        if text == '':
            self.lines.append({'type': 'blank'})
            return
        # Detect dashed/solid separator lines
        stripped = text.strip()
        if re.fullmatch(r'[-=*_~]{3,}', stripped):
            self.lines.append({'type': 'separator', 'char': stripped[0]})
            return
        self.lines.append({
            'type':      'text',
            'text':      text,
            'align':     self._align,
            'bold':      bool(self._emphasis & self.BOLD),
            'underline': bool(self._emphasis & self.UNDERLINE),
            'double_h':  bool(self._emphasis & self.DOUBLE_H),
            'double_w':  bool(self._emphasis & self.DOUBLE_W),
            'invert':    bool(self._emphasis & self.INVERT),
        })

    def get_and_clear(self):
        lines = list(self.lines)
        self.lines.clear()
        return lines


# ─────────────────────────────────────────────
#  Network Servers (background threads)
# ─────────────────────────────────────────────

class WiFiServer(QThread):
    """TCP server bound to a static IP on a chosen port."""
    data_received  = pyqtSignal(bytes)
    client_connected    = pyqtSignal(str)
    client_disconnected = pyqtSignal(str)
    log             = pyqtSignal(str)
    status_changed  = pyqtSignal(bool)

    def __init__(self, host: str, port: int):
        super().__init__()
        self.host = host
        self.port = port
        self._stop_event = threading.Event()
        self._server_sock = None

    def run(self):
        try:
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_sock.bind((self.host, self.port))
            self._server_sock.listen(5)
            self._server_sock.settimeout(1.0)
            self.log.emit(f"[WiFi] Listening on {self.host}:{self.port}")
            self.status_changed.emit(True)
        except OSError as e:
            self.log.emit(f"[WiFi] Bind error: {e}")
            self.status_changed.emit(False)
            return

        while not self._stop_event.is_set():
            try:
                conn, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            client_id = f"{addr[0]}:{addr[1]}"
            self.client_connected.emit(client_id)
            self.log.emit(f"[WiFi] Client connected: {client_id}")
            t = threading.Thread(target=self._handle_client,
                                 args=(conn, client_id), daemon=True)
            t.start()

        self._server_sock.close()
        self.status_changed.emit(False)

    def _handle_client(self, conn: socket.socket, client_id: str):
        conn.settimeout(5.0)
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                self.data_received.emit(bytes(chunk))
        finally:
            conn.close()
            self.client_disconnected.emit(client_id)
            self.log.emit(f"[WiFi] Client disconnected: {client_id}")

    def stop(self):
        self._stop_event.set()
        if self._server_sock:
            try: self._server_sock.close()
            except: pass


class BluetoothServer(QThread):
    """
    Bluetooth RFCOMM server.

    Backend priority:
      1. Windows native  — uses AF_BTH (socket family 32) built into Python's
                           socket module. Zero extra dependencies.
      2. PyBluez         — fallback for Linux / Mac.
      3. Disabled        — if neither is available.

    The server binds to all Bluetooth adapters and listens on RFCOMM channel 1.
    Any paired device that connects and sends ESC/POS data will be rendered.
    """

    data_received       = pyqtSignal(bytes)
    client_connected    = pyqtSignal(str)
    client_disconnected = pyqtSignal(str)
    log                 = pyqtSignal(str)
    status_changed      = pyqtSignal(bool)
    error_occurred      = pyqtSignal(str, str)
    server_started      = pyqtSignal(int)

    # Windows Bluetooth constants (winsock2)
    AF_BTH         = 32
    BTHPROTO_RFCOMM = 3
    RFCOMM_CHANNEL  = 1

    def __init__(self):
        super().__init__()
        self._stop_event = threading.Event()
        self._server_sock = None

    # ── public ────────────────────────────────

    def run(self):
        if not BLUETOOTH_AVAILABLE:
            self.log.emit("[BT] No Bluetooth backend available.")
            self.log.emit("[BT] On Windows, make sure Bluetooth is turned ON.")
            self.status_changed.emit(False)
            return

        if BT_BACKEND == 'windows':
            self._run_windows()
        elif BT_BACKEND == 'pybluez':
            self._run_pybluez()

    def stop(self):
        self._stop_event.set()
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass

    # ── Windows native backend ─────────────────

    def _run_windows(self):
        """
        Uses Python's built-in socket with AF_BTH (family=32).
        Bind address format: (mac, port, 0, 0)
        Use mac='' to bind to all adapters.
        """
        import ctypes

        try:
            sock = socket.socket(self.AF_BTH, socket.SOCK_STREAM, self.BTHPROTO_RFCOMM)
            # Try preferred channel first, fallback to BT_PORT_ANY (-1)
            try:
                sock.bind(("00:00:00:00:00:00", self.RFCOMM_CHANNEL))
                bound_channel = self.RFCOMM_CHANNEL
            except OSError:
                sock.bind(("00:00:00:00:00:00", -1))
                bound_channel = sock.getsockname()[1]

            sock.listen(3)
            sock.settimeout(1.0)
            self._server_sock = sock
            self.log.emit(f"[BT] Windows RFCOMM listening on channel {bound_channel}")
            self.log.emit("[BT] Make sure your PC is set to 'Discoverable' in Windows Bluetooth settings.")
            self.status_changed.emit(True)
            self.server_started.emit(bound_channel)
        except OSError as e:
            self.log.emit(f"[BT] Windows socket error: {e}")
            self.log.emit("[BT] Tip: Open Windows Settings → Bluetooth & devices → turn ON Bluetooth.")
            self.status_changed.emit(False)
            self.error_occurred.emit(str(e), "Open Windows Settings → Bluetooth & devices → turn ON Bluetooth.")
            return

        while not self._stop_event.is_set():
            try:
                conn, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # addr on Windows BT is (mac, channel, 0, 0)
            client_id = addr[0] if addr else "unknown"
            self.client_connected.emit(client_id)
            self.log.emit(f"[BT] Device connected: {client_id}")
            t = threading.Thread(
                target=self._handle_client,
                args=(conn, client_id),
                daemon=True
            )
            t.start()

        self._server_sock.close()
        self.status_changed.emit(False)

    # ── PyBluez backend (Linux / Mac) ──────────

    def _run_pybluez(self):
        import bluetooth  # already confirmed available

        try:
            sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
            sock.bind(("", self.RFCOMM_CHANNEL))
            sock.listen(3)
            sock.settimeout(1.0)
            bluetooth.advertise_service(
                sock,
                "POS Printer Emulator",
                service_classes=[bluetooth.SERIAL_PORT_CLASS],
                profiles=[bluetooth.SERIAL_PORT_PROFILE],
            )
            self._server_sock = sock
            self.log.emit(f"[BT] PyBluez RFCOMM listening on channel {self.RFCOMM_CHANNEL}")
            self.status_changed.emit(True)
            self.server_started.emit(self.RFCOMM_CHANNEL)
        except Exception as e:
            self.log.emit(f"[BT] PyBluez error: {e}")
            self.status_changed.emit(False)
            self.error_occurred.emit(str(e), "Make sure Bluetooth is enabled and PyBluez is configured correctly.")
            return

        while not self._stop_event.is_set():
            try:
                conn, info = self._server_sock.accept()
            except bluetooth.BluetoothError:
                continue
            except OSError:
                break
            client_id = str(info)
            self.client_connected.emit(client_id)
            self.log.emit(f"[BT] Device connected: {client_id}")
            t = threading.Thread(
                target=self._handle_client,
                args=(conn, client_id),
                daemon=True
            )
            t.start()

        self._server_sock.close()
        self.status_changed.emit(False)

    # ── shared client handler ──────────────────

    def _handle_client(self, conn, client_id: str):
        import base64
        try:
            conn.settimeout(10.0)
            while not self._stop_event.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except Exception:
                    break
                if not chunk:
                    break

                # ─── THE FIX: Auto-Decode Base64 ───
                processed_bytes = bytes(chunk)
                
                try:
                    # Check if the incoming data is valid Base64 encoded text
                    # Budget thermal utility text often wraps or sends clean ASCII text blocks
                    if len(processed_bytes) % 4 == 0 and b" " not in processed_bytes:
                        decoded = base64.b64decode(processed_bytes, validate=True)
                        # Ensure it wasn't just short plain text that happened to validate
                        if len(decoded) > 0:
                            processed_bytes = decoded
                except Exception:
                    # If validation fails, it's already raw bytes, leave it as-is
                    pass
                
                self.data_received.emit(processed_bytes)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            self.client_disconnected.emit(client_id)
            self.log.emit(f"[BT] Device disconnected: {client_id}")


# ─────────────────────────────────────────────
#  Receipt Display Widget
# ─────────────────────────────────────────────

class ReceiptWidget(QScrollArea):
    """Scrollable, auto-scaling thermal receipt display."""

    PAPER_COLOR  = "#FEFEFE"
    INK_COLOR    = "#1A1A1A"
    FONT_FAMILY  = "Courier New"
    BASE_PT      = 10

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        # Paper container
        self._paper = QWidget()
        self._paper.setStyleSheet(f"""
            background: {self.PAPER_COLOR};
            border-left:  1px solid #D0D0D0;
            border-right: 1px solid #D0D0D0;
        """)
        self._layout = QVBoxLayout(self._paper)
        self._layout.setContentsMargins(16, 20, 16, 40)
        self._layout.setSpacing(0)
        self._layout.addStretch()
        self.setWidget(self._paper)

        self.setStyleSheet("background: #B0B0B0; border: none;")
        self._receipt_lines = []

    def clear_receipt(self):
        # Remove all widgets except the stretch
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._receipt_lines.clear()

    def append_lines(self, lines: list):
        """Append parsed ESC/POS lines to the receipt display."""
        insert_pos = max(0, self._layout.count() - 1)  # before stretch

        for line in lines:
            ltype = line.get('type', 'blank')

            if ltype == 'blank':
                spacer = QLabel("")
                spacer.setFixedHeight(6)
                self._layout.insertWidget(insert_pos, spacer)
                insert_pos += 1

            elif ltype == 'separator':
                sep = self._make_separator(line.get('char', '-'))
                self._layout.insertWidget(insert_pos, sep)
                insert_pos += 1

            elif ltype == 'cut':
                cut = self._make_cut_mark()
                self._layout.insertWidget(insert_pos, cut)
                insert_pos += 1
                # Timestamp after cut
                ts = self._make_timestamp()
                self._layout.insertWidget(insert_pos, ts)
                insert_pos += 1

            elif ltype == 'barcode':
                bc = self._make_barcode_placeholder(line.get('data', ''))
                self._layout.insertWidget(insert_pos, bc)
                insert_pos += 1

            elif ltype == 'text':
                lbl = self._make_text_label(line)
                self._layout.insertWidget(insert_pos, lbl)
                insert_pos += 1

        self._receipt_lines.extend(lines)
        # Auto-scroll to bottom
        QTimer.singleShot(50, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()
        ))

    def _font(self, bold=False, double_h=False, double_w=False):
        pt = self.BASE_PT
        if double_h: pt = int(pt * 1.8)
        f = QFont(self.FONT_FAMILY, pt)
        f.setBold(bold)
        if double_w:
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 140)
        return f

    def _make_text_label(self, line: dict) -> QLabel:
        lbl = QLabel()
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.PlainText)

        text = line.get('text', '')
        lbl.setText(text)
        lbl.setFont(self._font(
            bold=line.get('bold', False),
            double_h=line.get('double_h', False),
            double_w=line.get('double_w', False),
        ))

        align_map = {
            EscPosParser.ALIGN_LEFT:   Qt.AlignmentFlag.AlignLeft,
            EscPosParser.ALIGN_CENTER: Qt.AlignmentFlag.AlignHCenter,
            EscPosParser.ALIGN_RIGHT:  Qt.AlignmentFlag.AlignRight,
        }
        qt_align = align_map.get(line.get('align', 0), Qt.AlignmentFlag.AlignLeft)
        lbl.setAlignment(qt_align | Qt.AlignmentFlag.AlignVCenter)

        # Build style
        styles = [
            f"color: {self.INK_COLOR};",
            "background: transparent;",
            "padding: 0px 2px;",
        ]
        if line.get('invert'):
            styles = [f"color: {self.PAPER_COLOR};",
                      f"background: {self.INK_COLOR};",
                      "padding: 0px 4px;"]
        if line.get('underline'):
            styles.append("text-decoration: underline;")

        lbl.setStyleSheet(" ".join(styles))
        lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        return lbl

    def _make_separator(self, char='-') -> QFrame:
        frame = QFrame()
        frame.setFixedHeight(10)
        frame.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(0, 4, 0, 4)
        lbl = QLabel(char * 48)
        lbl.setFont(QFont(self.FONT_FAMILY, self.BASE_PT - 1))
        lbl.setStyleSheet(f"color: {self.INK_COLOR}; background: transparent; letter-spacing: 0px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)
        return frame

    def _make_cut_mark(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(22)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 6, 0, 6)
        left_line  = QFrame(); left_line.setFrameShape(QFrame.Shape.HLine)
        right_line = QFrame(); right_line.setFrameShape(QFrame.Shape.HLine)
        for l in (left_line, right_line):
            l.setStyleSheet("color: #999; background: #999;")
        scissors = QLabel("✂")
        scissors.setFont(QFont("Arial", 12))
        scissors.setStyleSheet("color: #888; background: transparent;")
        layout.addWidget(left_line, 1)
        layout.addWidget(scissors, 0)
        layout.addWidget(right_line, 1)
        w.setStyleSheet("background: transparent;")
        return w

    def _make_timestamp(self) -> QLabel:
        lbl = QLabel(f"  Received: {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
        lbl.setFont(QFont(self.FONT_FAMILY, 8))
        lbl.setStyleSheet("color: #999; background: transparent; padding: 2px;")
        return lbl

    def _make_barcode_placeholder(self, data: str) -> QWidget:
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        vl = QVBoxLayout(container)
        vl.setContentsMargins(4, 6, 4, 6)
        vl.setSpacing(2)

        # Draw a simple barcode visual using vertical bars
        barcode_canvas = QLabel()
        barcode_canvas.setFixedHeight(60)
        barcode_canvas.setStyleSheet("background: transparent;")
        barcode_canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)

        pixmap = QPixmap(300, 60)
        pixmap.fill(QColor(self.PAPER_COLOR))
        painter = QPainter(pixmap)
        painter.setPen(QPen(QColor(self.INK_COLOR), 1))
        x = 10
        import random
        rng = random.Random(data)
        while x < 290:
            w = rng.randint(1, 3)
            if rng.random() > 0.4:
                painter.fillRect(x, 5, w, 50, QColor(self.INK_COLOR))
            x += w + rng.randint(1, 2)
        painter.end()
        barcode_canvas.setPixmap(pixmap)

        lbl = QLabel(data)
        lbl.setFont(QFont(self.FONT_FAMILY, 8))
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"color: {self.INK_COLOR}; background: transparent;")

        vl.addWidget(barcode_canvas)
        vl.addWidget(lbl)
        return container


# ─────────────────────────────────────────────
#  Connection Indicator Dot
# ─────────────────────────────────────────────

class StatusDot(QLabel):
    def __init__(self, label="", parent=None):
        super().__init__(parent)
        self._label = label
        self.set_active(False)

    def set_active(self, active: bool):
        color = "#22C55E" if active else "#6B7280"
        self.setStyleSheet(f"""
            color: #E5E7EB;
            font-size: 11px;
            padding: 2px 8px;
        """)
        dot = "●"
        self.setText(f'<span style="color:{color};">{dot}</span>  {self._label}')
        self.setTextFormat(Qt.TextFormat.RichText)


# ─────────────────────────────────────────────
#  Settings Panel
# ─────────────────────────────────────────────

class SettingsPanel(QWidget):
    apply_clicked = pyqtSignal(str, int, bool)  # host, port, bt_enabled

    def __init__(self):
        super().__init__()
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # ── WiFi ──────────────────────────────
        wifi_box = QGroupBox("WiFi / TCP Settings")
        wifi_box.setStyleSheet("""
            QGroupBox { color: #E5E7EB; font-weight: bold; border: 1px solid #374151;
                        border-radius: 6px; margin-top: 8px; padding-top: 6px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; }
        """)
        wifi_layout = QVBoxLayout(wifi_box)

        # IP selector
        ip_row = QHBoxLayout()
        ip_lbl = QLabel("Static IP:")
        ip_lbl.setStyleSheet("color: #9CA3AF;")
        self._ip_edit = QLineEdit()
        self._ip_edit.setPlaceholderText("e.g. 192.168.1.100")
        self._ip_edit.setStyleSheet("""
            QLineEdit { background: #1F2937; color: #F9FAFB; border: 1px solid #374151;
                        border-radius: 4px; padding: 4px 8px; }
        """)
        # Auto-detect local IPs
        detected = self._detect_local_ips()
        self._ip_edit.setText("0.0.0.0")
        ip_row.addWidget(ip_lbl)
        ip_row.addWidget(self._ip_edit)
        wifi_layout.addLayout(ip_row)

        # Port
        port_row = QHBoxLayout()
        port_lbl = QLabel("Port:")
        port_lbl.setStyleSheet("color: #9CA3AF;")
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(9100)   # default ESC/POS port
        self._port_spin.setStyleSheet("""
            QSpinBox { background: #1F2937; color: #F9FAFB; border: 1px solid #374151;
                       border-radius: 4px; padding: 4px 8px; }
        """)
        port_row.addWidget(port_lbl)
        port_row.addStretch()
        port_row.addWidget(self._port_spin)
        wifi_layout.addLayout(port_row)

        # IP hint
        if detected:
            hint = QLabel("Detected: " + "  |  ".join(detected[:3]))
            hint.setStyleSheet("color: #6B7280; font-size: 10px;")
            hint.setWordWrap(True)
            wifi_layout.addWidget(hint)

        layout.addWidget(wifi_box)

        # ── Bluetooth ─────────────────────────
        bt_box = QGroupBox("Bluetooth Settings")
        bt_box.setStyleSheet(wifi_box.styleSheet())
        bt_layout = QVBoxLayout(bt_box)

        self._bt_check = QCheckBox("Enable Bluetooth (RFCOMM)")
        self._bt_check.setStyleSheet("color: #E5E7EB;")
        if not BLUETOOTH_AVAILABLE:
            self._bt_check.setEnabled(False)
            self._bt_check.setToolTip("PyBluez not installed")
        else:
            self._bt_check.setChecked(True)
        bt_layout.addWidget(self._bt_check)

        if not BLUETOOTH_AVAILABLE:
            msg = QLabel("⚠ PyBluez not available.\nInstall: pip install pybluez")
            msg.setStyleSheet("color: #F59E0B; font-size: 10px;")
            bt_layout.addWidget(msg)

        bt_info = QLabel("Channel: RFCOMM 1\nService: POS Printer Emulator")
        bt_info.setStyleSheet("color: #6B7280; font-size: 10px;")
        bt_layout.addWidget(bt_info)

        layout.addWidget(bt_box)

        # ── Apply button ──────────────────────
        self._apply_btn = QPushButton("▶  Start / Restart Servers")
        self._apply_btn.setStyleSheet("""
            QPushButton {
                background: #2563EB; color: white; border: none;
                border-radius: 6px; padding: 8px 16px; font-weight: bold;
            }
            QPushButton:hover  { background: #1D4ED8; }
            QPushButton:pressed { background: #1E40AF; }
        """)
        self._apply_btn.clicked.connect(self._on_apply)
        layout.addWidget(self._apply_btn)
        layout.addStretch()

    def _detect_local_ips(self) -> list:
        ips = []
        try:
            for iface in netifaces.interfaces():
                addrs = netifaces.ifaddresses(iface)
                if netifaces.AF_INET in addrs:
                    for a in addrs[netifaces.AF_INET]:
                        ip = a.get('addr', '')
                        if ip and not ip.startswith('127.'):
                            ips.append(ip)
        except Exception:
            pass
        return ips

    def _on_apply(self):
        host = self._ip_edit.text().strip() or "0.0.0.0"
        port = self._port_spin.value()
        bt   = self._bt_check.isChecked()
        self.apply_clicked.emit(host, port, bt)

    def get_config(self):
        return (
            self._ip_edit.text().strip() or "0.0.0.0",
            self._port_spin.value(),
            self._bt_check.isChecked() and BLUETOOTH_AVAILABLE,
        )


# ─────────────────────────────────────────────
#  Log Panel
# ─────────────────────────────────────────────

class LogPanel(QTextEdit):
    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setFont(QFont("Courier New", 9))
        self.setStyleSheet("""
            QTextEdit {
                background: #0F172A; color: #94A3B8;
                border: none; border-top: 1px solid #1E293B;
            }
        """)
        self.setMaximumHeight(160)

    def append_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.append(f"[{ts}]  {msg}")
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


# ─────────────────────────────────────────────
#  Main Window
# ─────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("POS Printer Emulator")
        self.resize(900, 700)
        self.setMinimumSize(640, 480)

        self._parser = EscPosParser()
        self._wifi_server: WiFiServer | None = None
        self._bt_server:   BluetoothServer | None = None
        self._bt_approved = False

        self._build_ui()
        self._apply_dark_theme()

        # Auto-start with defaults after a short delay
        QTimer.singleShot(300, lambda: self._start_servers(*self._settings.get_config()))

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top bar ───────────────────────────
        topbar = QWidget()
        topbar.setFixedHeight(46)
        topbar.setStyleSheet("background: #111827; border-bottom: 1px solid #1F2937;")
        tb_layout = QHBoxLayout(topbar)
        tb_layout.setContentsMargins(14, 0, 14, 0)

        icon_lbl = QLabel("🖨")
        icon_lbl.setFont(QFont("Arial", 18))
        title_lbl = QLabel("POS Printer Emulator")
        title_lbl.setStyleSheet("color: #F9FAFB; font-size: 15px; font-weight: bold;")

        self._dot_wifi = StatusDot("WiFi")
        self._dot_bt   = StatusDot("Bluetooth")

        clear_btn = QPushButton("Clear")
        clear_btn.setStyleSheet("""
            QPushButton { background: #374151; color: #9CA3AF; border: none;
                          border-radius: 4px; padding: 4px 10px; font-size: 11px; }
            QPushButton:hover { background: #4B5563; color: #F9FAFB; }
        """)
        clear_btn.clicked.connect(self._clear_receipt)

        save_btn = QPushButton("Save Receipt")
        save_btn.setStyleSheet("""
            QPushButton { background: #374151; color: #9CA3AF; border: none;
                          border-radius: 4px; padding: 4px 10px; font-size: 11px; }
            QPushButton:hover { background: #4B5563; color: #F9FAFB; }
        """)
        save_btn.clicked.connect(self._save_receipt)

        tb_layout.addWidget(icon_lbl)
        tb_layout.addWidget(title_lbl)
        tb_layout.addStretch()
        tb_layout.addWidget(self._dot_wifi)
        tb_layout.addWidget(self._dot_bt)
        tb_layout.addSpacing(12)
        tb_layout.addWidget(clear_btn)
        tb_layout.addWidget(save_btn)
        root.addWidget(topbar)

        # ── Main splitter ─────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background: #1F2937; width: 2px; }")

        # Left: receipt
        receipt_container = QWidget()
        receipt_container.setStyleSheet("background: #6B7280;")
        rc_layout = QVBoxLayout(receipt_container)
        rc_layout.setContentsMargins(20, 20, 20, 20)
        self._receipt = ReceiptWidget()
        rc_layout.addWidget(self._receipt)

        # Right: settings
        right_panel = QWidget()
        right_panel.setFixedWidth(240)
        right_panel.setStyleSheet("background: #111827;")
        rp_layout = QVBoxLayout(right_panel)
        rp_layout.setContentsMargins(0, 0, 0, 0)

        self._settings = SettingsPanel()
        self._settings.apply_clicked.connect(self._start_servers)
        rp_layout.addWidget(self._settings)

        splitter.addWidget(receipt_container)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        root.addWidget(splitter, 1)

        # ── Log panel ─────────────────────────
        self._log = LogPanel()
        root.addWidget(self._log)

        # ── Status bar ────────────────────────
        sb = self.statusBar()
        sb.setStyleSheet("background: #0F172A; color: #6B7280; font-size: 10px;")
        self._sb_label = QLabel("Ready")
        sb.addWidget(self._sb_label)

    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow { background: #111827; }
            QScrollBar:vertical { background: #1E293B; width: 8px; }
            QScrollBar::handle:vertical { background: #374151; border-radius: 4px; }
        """)

    # ── Server management ─────────────────────

    def _start_servers(self, host: str, port: int, bt_enabled: bool):
        self._stop_servers()

        if bt_enabled and not self._bt_approved:
            reply = QMessageBox.question(
                self,
                "Bluetooth Server Permission",
                "POS Printer Emulator wants to start a Bluetooth RFCOMM server.\n"
                "This requires Bluetooth to be enabled on your PC.\n\n"
                "Do you want to start the Bluetooth server?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._bt_approved = True
            else:
                bt_enabled = False
                self._settings._bt_check.setChecked(False)

        if not bt_enabled:
            self._bt_approved = False

        self._log.append_log(f"Starting WiFi server on {host}:{port} ...")

        self._wifi_server = WiFiServer(host, port)
        self._wifi_server.data_received.connect(self._on_data)
        self._wifi_server.client_connected.connect(
            lambda c: self._log.append_log(f"[WiFi] ↗ Connected: {c}"))
        self._wifi_server.client_disconnected.connect(
            lambda c: self._log.append_log(f"[WiFi] ↘ Disconnected: {c}"))
        self._wifi_server.log.connect(self._log.append_log)
        self._wifi_server.status_changed.connect(self._dot_wifi.set_active)
        self._wifi_server.start()

        if bt_enabled:
            self._log.append_log("Starting Bluetooth server ...")
            self._bt_server = BluetoothServer()
            self._bt_server.data_received.connect(self._on_data)
            self._bt_server.client_connected.connect(
                lambda c: self._log.append_log(f"[BT] ↗ Connected: {c}"))
            self._bt_server.client_disconnected.connect(
                lambda c: self._log.append_log(f"[BT] ↘ Disconnected: {c}"))
            self._bt_server.log.connect(self._log.append_log)
            self._bt_server.status_changed.connect(self._dot_bt.set_active)
            self._bt_server.error_occurred.connect(self._on_bt_error)
            self._bt_server.server_started.connect(
                lambda ch: self._sb_label.setText(f"WiFi: {host}:{port}  |  BT: RFCOMM {ch}"))
            self._bt_server.start()
        else:
            self._dot_bt.set_active(False)

        self._sb_label.setText(f"WiFi: {host}:{port}  |  BT: {'starting...' if bt_enabled else 'off'}")

    def _on_bt_error(self, err_msg, tip):
        self._settings._bt_check.setChecked(False)
        self._dot_bt.set_active(False)
        self._sb_label.setText(f"WiFi: {self._settings._ip_edit.text().strip() or '0.0.0.0'}:{self._settings._port_spin.value()}  |  BT: off")
        
        reply = QMessageBox.warning(
            self,
            "Bluetooth Error",
            f"An error occurred while starting the Bluetooth server:\n\n{err_msg}\n\n{tip}\n\n"
            "Would you like to open Windows Bluetooth settings to turn Bluetooth ON?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                import os
                os.startfile("ms-settings:bluetooth")
            except Exception as e:
                self._log.append_log(f"Failed to open settings: {e}")

    def _stop_servers(self):
        if self._wifi_server:
            self._wifi_server.stop()
            self._wifi_server.wait(2000)
            self._wifi_server = None
        if self._bt_server:
            self._bt_server.stop()
            self._bt_server.wait(2000)
            self._bt_server = None
        self._dot_wifi.set_active(False)
        self._dot_bt.set_active(False)

    # ── Data handling ─────────────────────────

    def _on_data(self, data: bytes):
        self._log.append_log(f"Received {len(data)} bytes")
        self._parser.feed(data)
        lines = self._parser.get_and_clear()
        if lines:
            self._receipt.append_lines(lines)

    def _clear_receipt(self):
        self._receipt.clear_receipt()
        self._parser.reset()
        self._log.append_log("Receipt cleared.")

    def _save_receipt(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Receipt", "receipt.txt",
            "Text files (*.txt);;All files (*)"
        )
        if not path:
            return
        lines = self._receipt._receipt_lines
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                t = line.get('type')
                if t == 'text':
                    f.write(line.get('text', '') + "\n")
                elif t == 'separator':
                    f.write(line.get('char', '-') * 42 + "\n")
                elif t == 'cut':
                    f.write("- - - - - - - - - - - - - - - - - -\n")
                elif t == 'blank':
                    f.write("\n")
                elif t == 'barcode':
                    f.write(f"[BARCODE: {line.get('data', '')}]\n")
        self._log.append_log(f"Receipt saved → {path}")

    def closeEvent(self, event):
        self._stop_servers()
        super().closeEvent(event)


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("POS Printer Emulator")
    app.setStyle("Fusion")

    # Palette tweak so Qt-native widgets look dark
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,          QColor("#111827"))
    pal.setColor(QPalette.ColorRole.WindowText,      QColor("#F9FAFB"))
    pal.setColor(QPalette.ColorRole.Base,            QColor("#1F2937"))
    pal.setColor(QPalette.ColorRole.AlternateBase,   QColor("#374151"))
    pal.setColor(QPalette.ColorRole.Text,            QColor("#F9FAFB"))
    pal.setColor(QPalette.ColorRole.ButtonText,      QColor("#F9FAFB"))
    pal.setColor(QPalette.ColorRole.Button,          QColor("#374151"))
    pal.setColor(QPalette.ColorRole.Highlight,       QColor("#2563EB"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    app.setPalette(pal)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
